"""Temporal side: the same refund agent, run inside a Temporal Workflow.

The Temporal ADK plugin (`temporalio-google-adk`) runs the agent loop in
Workflow code and turns every model call and every tool call into an
Activity. Temporal records each Activity's result in the Workflow's event
history the moment it completes. After a crash, any Worker replays the
history: recorded results are reused, and only unfinished work runs again.

Around the agent sits ordinary business-process code: an approval with a
deadline, and an escalation when the deadline passes.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
    from temporalio.google_adk import TemporalModel, hitl_confirmation_response, pending_hitl_requests
    from temporalio.google_adk.workflow import activity_as_tool
    from temporalio.workflow import ActivityConfig

    from refund_demo import config
    from refund_demo.temporal_side import activities
    from refund_demo.agent import build_agent, request_text

TOOL_OPTIONS = dict(
    # A Worker that dies mid-call is noticed when this timeout passes; the call
    # is then retried on any live Worker.
    start_to_close_timeout=timedelta(seconds=6),
    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=5),
)

AGENT = build_agent(
    model=TemporalModel(config.MODEL, activity_config=ActivityConfig(
        # A Worker that dies mid-call is noticed when this passes. Short for the
        # demo's fake model; allow for the slowest real model response.
        start_to_close_timeout=timedelta(seconds=10), summary="refund_agent decides the next step",
        # A model provider returning 429s is retried until it recovers, however long that takes.
        # The attempt count and the next retry time live on the Temporal Service, not in a Worker.
        retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_interval=timedelta(seconds=4)))),
    refund_payment=activity_as_tool(activities.refund_payment, summary="refund_payment", **TOOL_OPTIONS),
    record_refund=activity_as_tool(activities.record_refund, summary="record_refund", **TOOL_OPTIONS),
    notify_customer=activity_as_tool(activities.notify_customer, summary="notify_customer", **TOOL_OPTIONS),
)


@dataclass
class RefundRequest:
    run_id: str
    customer_id: str
    order_id: str
    amount_cents: int
    reason: str
    line_items: list[int] | None = None
    use_idempotency_key: bool = False
    approval_threshold_cents: int | None = None
    # 48 hours in a real deployment; seconds in the demo.
    approval_deadline_seconds: int = 48 * 3600


@workflow.defn
class RefundWorkflow:
    def __init__(self) -> None:
        self._phase = "starting"
        self._awaiting: dict | None = None
        self._decision: bool | None = None
        self._outcome: str | None = None

    @workflow.run
    async def run(self, request: RefundRequest) -> str:
        sessions = InMemorySessionService()
        runner = Runner(agent=AGENT, app_name=config.APP_NAME, session_service=sessions)
        await sessions.create_session(
            app_name=config.APP_NAME, user_id=request.customer_id, session_id=request.run_id,
            state={"side": "temporal", "run_id": request.run_id,
                   "use_idempotency_key": request.use_idempotency_key,
                   "approval_threshold_cents": request.approval_threshold_cents})
        message = types.Content(role="user", parts=[types.Part(text=request_text(
            request.order_id, request.amount_cents, request.reason, request.line_items))])

        while True:
            self._phase = "agent running"
            pending, final_text = [], ""
            async for event in runner.run_async(user_id=request.customer_id, session_id=request.run_id,
                                                new_message=message):
                pending.extend(pending_hitl_requests(event))
                if event.is_final_response() and event.content and event.content.parts:
                    final_text = "".join(p.text or "" for p in event.content.parts) or final_text
            if not pending:
                self._phase = "done"
                self._outcome = final_text
                return final_text
            approved = await self._await_manager(request, pending[0].interrupt_id)
            message = types.Content(role="user", parts=[
                hitl_confirmation_response(pending[0].interrupt_id, confirmed=approved)])

    async def _await_manager(self, request: RefundRequest, interrupt_id: str) -> bool:
        """Wait for a manager's decision, durably, until the deadline. No compute is held while waiting."""
        deadline = timedelta(seconds=request.approval_deadline_seconds)
        self._phase = "awaiting approval"
        self._awaiting = {"interrupt_id": interrupt_id,
                          "deadline": (workflow.now() + deadline).isoformat()}
        try:
            await workflow.wait_condition(lambda: self._decision is not None, timeout=deadline)
            approved = bool(self._decision)
        except asyncio.TimeoutError:
            # Nobody decided in time: deny the refund and hand it to a person.
            self._phase = "deadline passed: escalating"
            await workflow.execute_activity(
                activities.escalate_to_manager,
                args=[request.run_id, request.order_id, request.amount_cents, "approval deadline passed"],
                start_to_close_timeout=timedelta(seconds=10))
            approved = False
        self._awaiting = None
        return approved

    @workflow.update
    async def decide(self, approved: bool) -> str:
        self._decision = approved
        return "approved" if approved else "rejected"

    @decide.validator
    def _validate_decide(self, approved: bool) -> None:
        if self._awaiting is None:
            raise ValueError("this refund is not waiting for approval")

    @workflow.query
    def status(self) -> dict:
        return {"phase": self._phase, "awaiting": self._awaiting, "outcome": self._outcome}
