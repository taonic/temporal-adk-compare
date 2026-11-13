"""ADK side: a small agent host, like `adk api_server`, running the refund agent.

ADK at full strength:
- `App(..., resumability_config=ResumabilityConfig(is_resumable=True))`, so an
  interrupted invocation can be resumed by its invocation id;
- `SqliteSessionService`, so every event is persisted to a database shared by
  all hosts, and survives a host crash.

A host only works on what it is asked to: /runs starts an invocation, /resume
continues one, /confirm answers an approval request. Nothing inside ADK tells
a freshly started host that an invocation was cut short; see recovery.py.
/recover runs that recovery scan on this host, the way each replica would on
startup or on a schedule.
"""

import argparse
import asyncio
import time

import httpx

from fastapi import FastAPI
from google.adk.agents.run_config import RunConfig
from google.adk.apps import App, ResumabilityConfig
from google.adk.models import BaseLlm
from google.adk.models.google_llm import Gemini
from google.adk.runners import Runner
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.adk.tools.tool_confirmation import ToolConfirmation
from google.genai import types
from pydantic import BaseModel

from .. import config, tools
from ..agent import build_agent, request_text
from ..fake_llm import FakeRefundLlm
from .recovery import find_unfinished_invocations, invocation_status


# ADK's model retries, switched on: the same schedule as the Temporal side's
# model retry policy (1s, doubling, capped at 4s), but in this process's memory.
MODEL_RETRY = types.HttpRetryOptions(attempts=10, initial_delay=1, max_delay=4, exp_base=2,
                                     http_status_codes=[429, 503])


def make_model() -> BaseLlm:
    if config.MODEL.startswith("demo-refund-"):
        return FakeRefundLlm(model=config.MODEL, retry_options=MODEL_RETRY)
    return Gemini(model=config.MODEL, retry_options=MODEL_RETRY)


def make_runner() -> Runner:
    agent = build_agent(
        model=make_model(),
        refund_payment=tools.refund_payment,
        record_refund=tools.record_refund,
        notify_customer=tools.notify_customer,
    )
    app = App(name=config.APP_NAME, root_agent=agent,
              resumability_config=ResumabilityConfig(is_resumable=True))
    config.VAR.mkdir(parents=True, exist_ok=True)
    session_service = SqliteSessionService(str(config.ADK_SESSIONS_DB))
    return Runner(app=app, session_service=session_service)


class RunIn(BaseModel):
    run_id: str
    customer_id: str
    order_id: str
    amount_cents: int
    reason: str
    line_items: list[int] | None = None
    use_idempotency_key: bool = False
    approval_threshold_cents: int | None = None
    approval_deadline_seconds: float = 48 * 3600


class RecoverIn(BaseModel):
    # The demo resumes only the run on screen; a real scan would take them all.
    session_id: str | None = None


class ResumeIn(BaseModel):
    customer_id: str
    session_id: str
    invocation_id: str


class ConfirmIn(BaseModel):
    customer_id: str
    session_id: str
    interrupt_id: str
    approved: bool


def request_message(order_id: str, amount_cents: int, reason: str, line_items: list[int] | None = None) -> types.Content:
    text = request_text(order_id, amount_cents, reason, line_items)
    return types.Content(role="user", parts=[types.Part(text=text)])


def approval_answer(interrupt_id: str, approved: bool) -> types.Content:
    """The manager's answer to ADK's tool confirmation request."""
    answer = types.Part(function_response=types.FunctionResponse(
        id=interrupt_id, name="adk_request_confirmation",
        response=ToolConfirmation(confirmed=approved).model_dump(mode="json")))
    return types.Content(role="user", parts=[answer])


def create_app(name: str) -> FastAPI:
    runner = make_runner()
    app = FastAPI(title=f"ADK host {name}")
    tasks: set[asyncio.Task] = set()

    async def drive(label: str, **run_kwargs) -> None:
        print(f"[host {name}] {label}", flush=True)
        try:
            # A new run, or a resume: /resume and /recover pass invocation_id=… in run_kwargs.
            async for event in runner.run_async(**run_kwargs):
                _log_event(name, event)
        except Exception as err:  # noqa: BLE001 - a failed invocation stays unfinished in the session store
            print(f"[host {name}] invocation failed: {type(err).__name__}: {str(err)[:120]}", flush=True)
            return
        print(f"[host {name}] invocation returned control", flush=True)
        await start_deadline_timer(run_kwargs["user_id"], run_kwargs["session_id"])

    async def start_deadline_timer(user_id: str, session_id: str) -> None:
        """If the invocation paused for a manager, enforce the deadline the simplest way an ADK host can:
        an asyncio task in this process's memory. It is lost if this process stops before it fires."""
        session = await runner.session_service.get_session(app_name=config.APP_NAME, user_id=user_id,
                                                           session_id=session_id)
        if session is None or invocation_status(session.events) != "awaiting_approval":
            return
        paused = session.events[-1]
        call = next(p.function_call for p in paused.content.parts if p.function_call)
        deadline_at = paused.timestamp + session.state.get("approval_deadline_seconds", 48 * 3600)
        print(f"[host {name}] approval deadline in {deadline_at - time.time():.0f}s: "
              f"timer held in this process's memory (asyncio)", flush=True)
        task = asyncio.create_task(deadline_timer(user_id, session_id, call.id, deadline_at))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def deadline_timer(user_id: str, session_id: str, interrupt_id: str, deadline_at: float) -> None:
        await asyncio.sleep(max(0.0, deadline_at - time.time()))
        session = await runner.session_service.get_session(app_name=config.APP_NAME, user_id=user_id,
                                                           session_id=session_id)
        if session is None or invocation_status(session.events) != "awaiting_approval":
            return  # a manager answered in time
        print(f"[host {name}] approval deadline passed: in-memory timer fired; escalating and rejecting", flush=True)
        state = session.state
        async with httpx.AsyncClient(base_url=config.BUSINESS_URL, timeout=10) as client:
            response = await client.post("/escalations", json={
                "side": "adk", "run_id": state["run_id"], "order_id": state["order_id"],
                "amount_cents": state["amount_cents"], "reason": "approval deadline passed"})
            response.raise_for_status()
        background("approval answered: rejected (deadline passed)", user_id=user_id, session_id=session_id,
                   new_message=approval_answer(interrupt_id, False))

    # Every event this host appends carries its name, so the page can say which host did each step.
    stamp = RunConfig(custom_metadata={"host": f"host-{name}"})

    def background(label: str, **run_kwargs) -> None:
        task = asyncio.create_task(drive(label, run_config=stamp, **run_kwargs))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @app.post("/runs")
    async def start_run(body: RunIn):
        await runner.session_service.create_session(
            app_name=config.APP_NAME, user_id=body.customer_id, session_id=body.run_id,
            state={"side": "adk", "run_id": body.run_id, "use_idempotency_key": body.use_idempotency_key,
                   "approval_threshold_cents": body.approval_threshold_cents,
                   "approval_deadline_seconds": body.approval_deadline_seconds,
                   "order_id": body.order_id, "amount_cents": body.amount_cents})
        background(f"new invocation for session {body.run_id}", user_id=body.customer_id, session_id=body.run_id,
                   new_message=request_message(body.order_id, body.amount_cents, body.reason, body.line_items))
        return {"accepted": True, "host": name}

    @app.post("/resume")
    async def resume(body: ResumeIn):
        background(f"resuming invocation {body.invocation_id}", user_id=body.customer_id,
                   session_id=body.session_id, invocation_id=body.invocation_id)
        return {"accepted": True, "host": name}

    @app.post("/recover")
    async def recover(body: RecoverIn):
        found = await find_unfinished_invocations(runner.session_service, config.APP_NAME)
        found = [item for item in found if body.session_id in (None, item["session_id"])]
        print(f"[host {name}] recovery scan: {len(found)} unfinished invocation(s)", flush=True)
        for item in found:
            background(f"resuming invocation {item['invocation_id']}", user_id=item["customer_id"],
                       session_id=item["session_id"], invocation_id=item["invocation_id"])
        return {"host": name, "resumed": found}

    @app.post("/confirm")
    async def confirm(body: ConfirmIn):
        background(f"approval answered: {'approved' if body.approved else 'rejected'}",
                   user_id=body.customer_id, session_id=body.session_id,
                   new_message=approval_answer(body.interrupt_id, body.approved))
        return {"accepted": True, "host": name}

    @app.get("/health")
    async def health():
        return {"host": name, "busy": len(tasks)}

    return app


def _log_event(host: str, event) -> None:
    parts = (event.content.parts if event.content else []) or []
    calls = [p.function_call.name for p in parts if p.function_call]
    results = [p.function_response.name for p in parts if p.function_response]
    if len(calls) > 1:
        print(f"[host {host}] event: {event.author} calls {len(calls)} tools at once: {', '.join(calls)}", flush=True)
        return
    if len(results) > 1:
        print(f"[host {host}] event: {len(results)} results persisted together, in one event", flush=True)
        return
    for part in parts:
        if part.function_call:
            print(f"[host {host}] event: {event.author} calls {part.function_call.name}", flush=True)
        elif part.function_response:
            print(f"[host {host}] event: {part.function_response.name} result persisted", flush=True)
        elif part.text:
            print(f"[host {host}] event: {event.author}: {part.text[:90]}", flush=True)


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="a", choices=sorted(config.ADK_HOST_PORTS))
    args = parser.parse_args()
    print(f"[host {args.name}] ADK host up on :{config.ADK_HOST_PORTS[args.name]}, idle until asked", flush=True)
    uvicorn.run(create_app(args.name), host="127.0.0.1", port=config.ADK_HOST_PORTS[args.name], log_level="warning")
