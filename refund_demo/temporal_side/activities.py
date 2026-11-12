"""Temporal side: the agent's tools as Activities, plus one business-process Activity.

Each Activity is a thin shell around the plain ADK tool in tools.py, so both
columns of the demo run the same tool code. The shell only gives Temporal
what it needs:

- `@activity.defn`, so a Worker can run it;
- `tool_context: ToolContextSnapshot`. The Temporal ADK plugin replaces ADK's
  live ToolContext (which can't leave the process) with this serializable
  snapshot. The snapshot has the two things the tools read: the session
  state and the function call id.

The docstring is copied from the tool, because ADK builds the schema the
model sees from it.
"""

import httpx
from temporalio import activity
from temporalio.google_adk.workflow import ToolContextSnapshot

from refund_demo import config, tools


@activity.defn
async def refund_payment(order_id: str, amount_cents: int, reason: str, tool_context: ToolContextSnapshot) -> dict:
    return await tools.refund_payment(order_id, amount_cents, reason, tool_context)


@activity.defn
async def record_refund(order_id: str, refund_id: str, amount_cents: int, tool_context: ToolContextSnapshot) -> dict:
    return await tools.record_refund(order_id, refund_id, amount_cents, tool_context)


@activity.defn
async def notify_customer(order_id: str, message: str, tool_context: ToolContextSnapshot) -> dict:
    return await tools.notify_customer(order_id, message, tool_context)


for _activity, _tool in ((refund_payment, tools.refund_payment), (record_refund, tools.record_refund),
                         (notify_customer, tools.notify_customer)):
    _activity.__doc__ = _tool.__doc__


@activity.defn
async def escalate_to_manager(run_id: str, order_id: str, amount_cents: int, reason: str) -> dict:
    """Open a ticket in the managers' queue. Business-process code around the agent, not an agent tool."""
    async with httpx.AsyncClient(base_url=config.BUSINESS_URL, timeout=10) as client:
        response = await client.post("/escalations", json={
            "side": "temporal", "run_id": run_id, "order_id": order_id,
            "amount_cents": amount_cents, "reason": reason})
        response.raise_for_status()
        return response.json()
