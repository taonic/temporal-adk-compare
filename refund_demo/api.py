"""Control plane for the demo UI.

Starts every process, starts refunds on both sides, kills things on request,
and reports what each side has persisted next to what really happened in the
business systems. It holds no refund state of its own: the ADK session
database, the Temporal event history and the business systems' ledger are
the sources of truth, and /api/state reads all three on every poll.
"""

import asyncio
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from pydantic import BaseModel
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.google_adk import GoogleAdkPlugin
from temporalio.service import RPCError

from . import config, faults, source_view
from .adk_side.recovery import invocation_status
from .procs import ManagedProcess, Supervisor
from .temporal_side.workflow import RefundRequest, RefundWorkflow

STATIC = Path(__file__).parent / "static"
CUSTOMER = "C-1042"


class Demo:
    """Everything the API knows: the processes, the settings, and which run is on screen."""

    def __init__(self) -> None:
        self.supervisor = Supervisor()
        self.client: Client | None = None
        self.sessions: SqliteSessionService | None = None
        self.use_idempotency_key = False
        self.run: dict[str, Any] | None = None
        self.request: RefundRequest | None = None  # the run on screen, to start on the other side later
        self.runs = 0
        self.events: list[tuple[float, str]] = []  # presenter-facing system log
        self.recovered: dict | None = None
        sup = self.supervisor
        sup.add("business", "Business systems", "system", "refund_demo.business.service")
        sup.add("adk-a", "ADK host A", "adk", "refund_demo.adk_side.host", "--name", "a")
        sup.add("adk-b", "ADK host B", "adk", "refund_demo.adk_side.host", "--name", "b")
        sup.add("worker-a", "Worker A", "temporal", "refund_demo.temporal_side.worker", "--name", "a")
        sup.add("worker-b", "Worker B", "temporal", "refund_demo.temporal_side.worker", "--name", "b")
        self.temporal_server: ManagedProcess | None = None

    def note(self, text: str) -> None:
        self.events.append((time.time(), text))
        del self.events[:-100]


demo = Demo()


async def _connect_temporal() -> Client:
    try:
        return await Client.connect(config.TEMPORAL_ADDRESS, namespace=config.TEMPORAL_NAMESPACE,
                                    plugins=[GoogleAdkPlugin()])
    except RuntimeError:
        pass
    # No Temporal Server yet: start a local dev server, owned by this API.
    proc = ManagedProcess("temporal", "Temporal dev server", "system",
                          ["temporal", "server", "start-dev", "--db-filename", str(config.VAR / "temporal.db"),
                           "--log-level", "error", *(["--ui-ip", config.TEMPORAL_UI_IP] if config.TEMPORAL_UI_IP else [])])
    demo.supervisor.processes["temporal"] = proc
    await demo.supervisor.start("temporal")
    for _ in range(60):
        await asyncio.sleep(0.5)
        try:
            return await Client.connect(config.TEMPORAL_ADDRESS, namespace=config.TEMPORAL_NAMESPACE,
                                        plugins=[GoogleAdkPlugin()])
        except RuntimeError:
            continue
    raise RuntimeError(f"Temporal Server did not come up on {config.TEMPORAL_ADDRESS}")


async def _wait_http(url: str, attempts: int = 60) -> None:
    async with httpx.AsyncClient(timeout=1) as client:
        for _ in range(attempts):
            try:
                await client.get(url)
                return
            except httpx.HTTPError:
                await asyncio.sleep(0.25)


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.VAR.mkdir(parents=True, exist_ok=True)
    faults.arm("adk", None)
    faults.arm("temporal", None)
    faults.end_model_outage()
    demo.client = await _connect_temporal()
    demo.sessions = SqliteSessionService(str(config.ADK_SESSIONS_DB))
    for key in ("business", "adk-a", "worker-a"):
        await demo.supervisor.start(key)
    await _wait_http(f"{config.BUSINESS_URL}/state")
    demo.note("Demo ready: business systems, ADK host A and Worker A are up.")
    yield
    await demo.supervisor.stop_all()


app = FastAPI(title="Resumable agent vs durable agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


# ---- Presenter actions ------------------------------------------------------

class StartRun(BaseModel):
    amount_cents: int = 50000
    # Several line items: the model refunds each one, all in one turn (parallel tool calls).
    line_items: list[int] | None = None
    reason: str = "item arrived damaged"
    approval: bool = False
    approval_deadline_seconds: int = 20
    # "both" starts the two sides at once. "adk" or "temporal" starts one; the scenarios then
    # start the same run on the other side with /api/runs/continue, so one column plays at a time.
    side: str = "both"


SIDES = ("adk", "temporal")


@app.post("/api/runs")
async def start_run(body: StartRun):
    sides = SIDES if body.side == "both" else (body.side,)
    if any(side not in SIDES for side in sides):
        raise HTTPException(422, f"Unknown side {body.side!r}")
    if "adk" in sides and _live_adk_host() is None:
        raise HTTPException(409, "No ADK host is running")
    demo.runs += 1
    run_id = f"r{demo.runs:02d}-{secrets.token_hex(2)}"
    order_id = f"O-{7780 + demo.runs}"
    threshold = 25000 if body.approval else None
    if body.line_items:
        body.amount_cents = sum(body.line_items)
    demo.run = {"run_id": run_id, "order_id": order_id, "amount_cents": body.amount_cents,
                "line_items": body.line_items,
                "approval_threshold_cents": threshold, "deadline_seconds": body.approval_deadline_seconds,
                "use_idempotency_key": demo.use_idempotency_key, "started_at": time.time(),
                "started": {}, "workflow_id": f"refund-{run_id}"}
    demo.recovered = None
    demo.request = RefundRequest(run_id=run_id, customer_id=CUSTOMER, order_id=order_id,
                                 amount_cents=body.amount_cents, reason=body.reason, line_items=body.line_items,
                                 use_idempotency_key=demo.use_idempotency_key, approval_threshold_cents=threshold,
                                 approval_deadline_seconds=body.approval_deadline_seconds)
    where = await asyncio.gather(*[_start_on(side) for side in sides])
    items = f" in {len(body.line_items)} line items" if body.line_items else ""
    demo.note(f"Run {run_id}: refund ${body.amount_cents / 100:.2f}{items} for {order_id} started "
              f"{'on both sides' if len(sides) > 1 else 'on ' + sides[0].upper()} ({'; '.join(where)}).")
    return demo.run


class ContinueRun(BaseModel):
    side: str


@app.post("/api/runs/continue")
async def continue_run(body: ContinueRun):
    """Start the run on screen on the side that has not run it yet: the same request, the same ids."""
    if body.side not in SIDES:
        raise HTTPException(422, f"Unknown side {body.side!r}")
    if not demo.run or not demo.request:
        raise HTTPException(409, "No run to continue")
    if body.side in demo.run["started"]:
        raise HTTPException(409, f"Run {demo.run['run_id']} already started on {body.side}")
    where = await _start_on(body.side)
    demo.note(f"Run {demo.run['run_id']}: the same refund started on {body.side.upper()} ({where}).")
    return demo.run


async def _start_on(side: str) -> str:
    """Start the run on screen on one side; say where it went."""
    request = demo.request
    if side == "adk":
        host = _live_adk_host()
        if host is None:
            raise HTTPException(409, "No ADK host is running")
        await _post(f"{_host_url(host)}/runs", {**request.__dict__})
        where = f"ADK host {host.upper()}"
    else:
        await demo.client.start_workflow(
            RefundWorkflow.run, request, id=demo.run["workflow_id"], task_queue=config.TASK_QUEUE,
            task_timeout=timedelta(seconds=4))
        where = f"Temporal task queue '{config.TASK_QUEUE}'"
    demo.run["started"][side] = time.time()
    return where


class Settings(BaseModel):
    use_idempotency_key: bool | None = None
    auto_restart: bool | None = None


@app.post("/api/settings")
async def settings(body: Settings):
    if body.use_idempotency_key is not None:
        demo.use_idempotency_key = body.use_idempotency_key
        demo.note(f"Idempotency key {'ON' if body.use_idempotency_key else 'OFF'} for new runs.")
    if body.auto_restart is not None:
        demo.supervisor.auto_restart = body.auto_restart
        demo.note(f"Supervisor auto-restart {'ON' if body.auto_restart else 'OFF'}.")
    return _settings()


class Fault(BaseModel):
    side: str  # adk | temporal | both
    point: str | None


@app.post("/api/faults")
async def set_fault(body: Fault):
    for side in (("adk", "temporal") if body.side == "both" else (body.side,)):
        faults.arm(side, body.point)
    demo.note(f"Crash point {'armed: ' + body.point if body.point else 'disarmed'} ({body.side}).")
    return _faults()


@app.post("/api/processes/{key}/{action}")
async def process_action(key: str, action: str):
    if key not in demo.supervisor.processes or action not in ("start", "stop", "kill"):
        raise HTTPException(404)
    await getattr(demo.supervisor, action)(key)
    demo.note(f"{demo.supervisor.processes[key].label}: {action}.")
    return {"ok": True}


class Recover(BaseModel):
    # "one": the first live host runs the scan. "all": every live host runs it at
    # the same moment, as replicas that each scan on startup or on a schedule would.
    hosts: str = "one"


@app.post("/api/adk/recover")
async def adk_recover(body: Recover | None = None):
    """What an operator, or code you write, must do on the ADK side after a crash."""
    live = [name for name in ("a", "b") if demo.supervisor.processes[f"adk-{name}"].state == "running"]
    if not live:
        raise HTTPException(409, "No ADK host is running to resume on")
    if not (body and body.hosts == "all"):
        live = live[:1]
    # The scan sees every session; the demo only resumes the run on screen.
    session_id = demo.run["run_id"] if demo.run else "-"
    replies = await asyncio.gather(*[_post(f"{_host_url(name)}/recover", {"session_id": session_id})
                                     for name in live])
    resumed = [{**item, "host": reply["host"]} for reply in replies for item in reply["resumed"]]
    demo.recovered = {"at": time.time(), "resumed": resumed}
    hosts = " and ".join(f"host {name.upper()}" for name in live)
    demo.note(f"ADK recovery scan on {hosts}: resumed {len(resumed)} invocation(s)"
              + (", the same one on every host." if len(live) > 1 and len(resumed) > 1 else "."))
    return demo.recovered


class Outage(BaseModel):
    seconds: float = 20


@app.post("/api/model-outage")
async def model_outage(body: Outage):
    """The model provider rejects every call (429) for a while, on both sides."""
    if body.seconds > 0:
        faults.start_model_outage(body.seconds)
        demo.note(f"Model outage: every model call fails for {body.seconds:.0f}s.")
    else:
        faults.end_model_outage()
        demo.note("Model outage ended.")
    return {"model_outage_left": faults.model_outage_left()}


class Decision(BaseModel):
    side: str
    approved: bool


@app.post("/api/approve")
async def approve(body: Decision):
    if not demo.run:
        raise HTTPException(409, "No run")
    verdict = "approved" if body.approved else "rejected"
    if body.side == "temporal":
        handle = demo.client.get_workflow_handle(demo.run["workflow_id"])
        try:
            await handle.execute_update(RefundWorkflow.decide, body.approved)
        except Exception as err:  # noqa: BLE001 - shown to the presenter
            raise HTTPException(409, f"Temporal rejected the decision: {err}") from err
    else:
        adk = await _adk_state()
        if not adk.get("awaiting"):
            raise HTTPException(409, "The ADK invocation is not waiting for approval")
        host = _live_adk_host()
        if host is None:
            raise HTTPException(409, "No ADK host is running")
        await _post(f"{_host_url(host)}/confirm", {"customer_id": CUSTOMER, "session_id": demo.run["run_id"],
                    "interrupt_id": adk["awaiting"]["interrupt_id"], "approved": body.approved})
    demo.note(f"Manager {verdict} the refund on the {body.side.upper()} side.")
    return {"ok": True}


@app.post("/api/reset")
async def reset():
    """A clean stage: one host and one Worker, nothing armed, empty ledger."""
    # The previous run's Workflow stops too, so it can't go on moving money after the reset.
    if demo.run and "temporal" in demo.run["started"]:
        try:
            await demo.client.get_workflow_handle(demo.run["workflow_id"]).terminate("demo reset")
        except RPCError:
            pass  # already closed
    for key in ("adk-b", "worker-b"):
        await demo.supervisor.stop(key)
    for key in ("business", "adk-a", "worker-a"):
        proc = demo.supervisor.processes[key]
        if proc.state != "running":
            await demo.supervisor.start(key)
    await _wait_http(f"{config.BUSINESS_URL}/state")
    faults.arm("adk", None)
    faults.arm("temporal", None)
    faults.end_model_outage()
    demo.supervisor.auto_restart = True
    demo.use_idempotency_key = False
    demo.run, demo.request, demo.recovered = None, None, None
    async with httpx.AsyncClient(timeout=5) as client:
        await client.post(f"{config.BUSINESS_URL}/reset")
    for proc in demo.supervisor.processes.values():
        proc.logs.clear()
    demo.events.clear()
    demo.note("Reset: ledger cleared, crash points and outage cleared, auto-restart on, idempotency key off.")
    return {"ok": True}


# ---- State --------------------------------------------------------------------

@app.get("/api/state")
async def state():
    adk, temporal, business = await asyncio.gather(_adk_state(), _temporal_state(), _business_state())
    sup = demo.supervisor
    return {
        "now": time.time(),
        "temporal_ui": config.TEMPORAL_UI,
        "settings": _settings(),
        "faults": _faults(),
        "fault_points": faults.POINTS,
        "model_outage_left": faults.model_outage_left(),
        "processes": [{"key": p.key, "label": p.label, "side": p.side, "state": p.state, "pid": p.pid,
                       "exit_code": p.exit_code, "restarts": p.restarts} for p in sup.processes.values()],
        "logs": {side: _logs(side) for side in ("adk", "temporal")},
        "notes": [{"ts": ts, "text": text} for ts, text in demo.events[-40:]],
        "run": demo.run,
        "adk": adk,
        "temporal": temporal,
        "business": business,
        "recovered": demo.recovered,
    }


@app.get("/api/source/{node}")
async def source(node: str, side: str):
    try:
        return source_view.node_source(side, node)
    except KeyError as err:
        raise HTTPException(404, f"No source for {side}/{node}") from err


def _settings() -> dict:
    return {"use_idempotency_key": demo.use_idempotency_key, "auto_restart": demo.supervisor.auto_restart}


def _faults() -> dict:
    return {"adk": faults.armed("adk"), "temporal": faults.armed("temporal")}


def _logs(side: str) -> list[dict]:
    lines = [{"ts": ts, "proc": p.label, "line": line}
             for p in demo.supervisor.processes.values() if p.side == side for ts, line in p.logs]
    return sorted(lines, key=lambda x: x["ts"])[-80:]


RETRY_LINE = re.compile(r"in-process retry (\d+) of (\d+) in (\d+)s")


def _adk_retrying() -> dict | None:
    """An ADK host retrying the model in process.

    In-process retries write nothing to the session database. The attempt
    count lives in the host's memory, and its log is the only trace, so this
    reads the latest line of each live host. A host that died took its retry
    loop with it.
    """
    for proc in demo.supervisor.processes.values():
        if proc.side != "adk" or proc.state != "running" or not proc.logs:
            continue
        ts, line = proc.logs[-1]
        match = RETRY_LINE.search(line)
        if match and ts >= (proc.started_at or 0):
            return {"host": proc.label, "attempt": int(match[1]), "attempts": int(match[2]),
                    "delay": int(match[3]), "ts": ts}
    return None


def _live_adk_host() -> str | None:
    for name in ("a", "b"):
        if demo.supervisor.processes[f"adk-{name}"].state == "running":
            return name
    return None


def _host_url(name: str) -> str:
    return f"http://127.0.0.1:{config.ADK_HOST_PORTS[name]}"


async def _post(url: str, body: dict) -> dict:
    """POST, retrying while a just-started process opens its port."""
    async with httpx.AsyncClient(timeout=5) as client:
        for attempt in range(40):
            try:
                response = await client.post(url, json=body)
                response.raise_for_status()
                return response.json()
            except httpx.ConnectError:
                if attempt == 39:
                    raise
                await asyncio.sleep(0.25)
    return {}


async def _business_state() -> dict:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            rows = (await client.get(f"{config.BUSINESS_URL}/state")).json()
    except httpx.HTTPError:
        return {"error": "business systems unreachable"}
    # Only the run on screen. With none (after a reset, or a restart of this API: the ledger is a
    # file that outlives both), nothing: earlier runs' rows must not show up as this run's.
    run_id = demo.run["run_id"] if demo.run else None
    return {table: [r for r in items if r["run_id"] == run_id] for table, items in rows.items()}


async def _adk_state() -> dict:
    if not demo.run:
        return {}
    session = await demo.sessions.get_session(app_name=config.APP_NAME, user_id=CUSTOMER,
                                              session_id=demo.run["run_id"])
    if session is None:
        return {"status": "not_started", "events": []}
    events = [_adk_event(i, e) for i, e in enumerate(session.events)]
    status = invocation_status(session.events)
    out: dict[str, Any] = {"status": status, "events": events,
                           "invocation_id": session.events[-1].invocation_id if session.events else None}
    if status == "unfinished":
        out["retrying"] = _adk_retrying()
    if status == "awaiting_approval":
        call = next(p.function_call for p in session.events[-1].content.parts if p.function_call)
        since = session.events[-1].timestamp
        out["awaiting"] = {"interrupt_id": call.id, "since": since,
                           "deadline_at": since + demo.run["deadline_seconds"]}
    return out


def _adk_event(i: int, event) -> dict:
    parts = (event.content.parts if event.content else None) or []
    item: dict[str, Any] = {"i": i, "id": event.id, "ts": event.timestamp, "author": event.author,
                            "invocation_id": event.invocation_id, "kind": "event", "name": None}
    # Parallel tool calls share one event: every call, and later every result, is a part of it.
    item["host"] = (event.custom_metadata or {}).get("host")
    item["calls"] = [{"name": p.function_call.name, "call_id": p.function_call.id, "data": p.function_call.args}
                     for p in parts if p.function_call]
    item["responses"] = [{"name": p.function_response.name, "call_id": p.function_response.id,
                          "data": p.function_response.response} for p in parts if p.function_response]
    for part in parts:
        if part.function_call:
            name = part.function_call.name
            item.update(kind="approval_request" if name == "adk_request_confirmation" else "function_call",
                        name=name, call_id=part.function_call.id, data=part.function_call.args)
            break
        if part.function_response:
            name = part.function_response.name
            item.update(kind="approval_answer" if name == "adk_request_confirmation" else "function_response",
                        name=name, call_id=part.function_response.id, data=part.function_response.response)
            break
        if part.text:
            item.update(kind="user_message" if event.author == "user" else "final_text", data=part.text)
            break
    if event.actions.end_of_agent:
        item.update(kind="end_of_agent")
    return item


async def _temporal_state() -> dict:
    if not demo.run or "temporal" not in demo.run["started"]:
        return {}
    workflow_id = demo.run["workflow_id"]
    handle = demo.client.get_workflow_handle(workflow_id)
    out: dict[str, Any] = {"workflow_id": workflow_id,
                           "ui_url": f"{config.TEMPORAL_UI}/namespaces/{config.TEMPORAL_NAMESPACE}"
                                     f"/workflows/{workflow_id}"}
    try:
        description = await handle.describe()
        history = await handle.fetch_history()
    except RPCError as err:
        return {**out, "error": str(err)}
    out["status"] = WorkflowExecutionStatus(description.status).name if description.status else "UNKNOWN"
    # A retrying Activity has no history event per attempt; the attempt count and
    # last failure live on the pending Activity until it completes.
    out["pending"] = [{"activity": pa.activity_type.name, "attempt": pa.attempt,
                       "last_failure": pa.last_failure.message if pa.HasField("last_failure") else None}
                      for pa in description.raw_description.pending_activities]
    scheduled: dict[int, dict] = {}
    events = []
    for event in history.events:
        events.append(_history_event(event, scheduled))
    out["history"] = events
    timer = next((e for e in reversed(events) if e["type"] == "TimerStarted"), None)
    if timer and not any(e["type"] in ("TimerFired", "TimerCanceled") and e.get("timer_id") == timer["timer_id"]
                         for e in events):
        out["awaiting"] = {"since": timer["ts"], "deadline_at": timer["ts"] + timer["timeout_seconds"]}
    return out


def _history_event(event, scheduled: dict[int, dict]) -> dict:
    type_name = EventType.Name(event.event_type).removeprefix("EVENT_TYPE_")
    type_name = "".join(w.capitalize() for w in type_name.split("_"))
    item: dict[str, Any] = {"id": event.event_id, "type": type_name, "ts": event.event_time.ToSeconds()
                            + event.event_time.nanos / 1e9}
    if event.HasField("activity_task_scheduled_event_attributes"):
        attrs = event.activity_task_scheduled_event_attributes
        scheduled[event.event_id] = {"activity": attrs.activity_type.name}
        item.update(activity=attrs.activity_type.name, data=_payloads(attrs.input))
    elif event.HasField("activity_task_started_event_attributes"):
        attrs = event.activity_task_started_event_attributes
        item.update(activity=scheduled.get(attrs.scheduled_event_id, {}).get("activity"),
                    attempt=attrs.attempt, identity=attrs.identity)
    elif event.HasField("activity_task_completed_event_attributes"):
        attrs = event.activity_task_completed_event_attributes
        item.update(activity=scheduled.get(attrs.scheduled_event_id, {}).get("activity"),
                    data=_payloads(attrs.result), by=attrs.identity)
    elif event.HasField("activity_task_timed_out_event_attributes"):
        attrs = event.activity_task_timed_out_event_attributes
        item.update(activity=scheduled.get(attrs.scheduled_event_id, {}).get("activity"))
    elif event.HasField("activity_task_failed_event_attributes"):
        attrs = event.activity_task_failed_event_attributes
        item.update(activity=scheduled.get(attrs.scheduled_event_id, {}).get("activity"),
                    data=attrs.failure.message)
    elif event.HasField("workflow_task_started_event_attributes"):
        item.update(identity=event.workflow_task_started_event_attributes.identity)
    elif event.HasField("workflow_task_timed_out_event_attributes"):
        item.update(data="the Worker running this task stopped responding")
    elif event.HasField("timer_started_event_attributes"):
        attrs = event.timer_started_event_attributes
        item.update(timer_id=attrs.timer_id, timeout_seconds=attrs.start_to_fire_timeout.ToSeconds())
    elif event.HasField("timer_fired_event_attributes"):
        item.update(timer_id=event.timer_fired_event_attributes.timer_id)
    elif event.HasField("workflow_execution_update_accepted_event_attributes"):
        attrs = event.workflow_execution_update_accepted_event_attributes
        item.update(data=_payloads(attrs.accepted_request.input.args))
    elif event.HasField("workflow_execution_completed_event_attributes"):
        item.update(data=_payloads(event.workflow_execution_completed_event_attributes.result))
    return item


def _payloads(payloads) -> Any:
    values = []
    for payload in payloads.payloads:
        try:
            values.append(json.loads(payload.data))
        except ValueError:
            values.append(f"<{len(payload.data)} bytes>")
    if len(values) == 1:
        values = values[0]
    text = json.dumps(values, default=str)
    return values if len(text) < 1500 else text[:1500] + "…"


if __name__ == "__main__":
    import uvicorn

    print(f"Open http://localhost:{config.API_PORT}", flush=True)
    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT, log_level="warning")
