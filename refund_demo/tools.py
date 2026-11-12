"""The agent's tools: calls into the business systems (payments, orders, email).

Plain ADK function tools, with no Temporal in them. Both columns of the demo
run this exact code:

- ADK calls them directly, passing its live ToolContext.
- Temporal runs each one inside an Activity (temporal_side/activities.py),
  passing the plugin's serializable snapshot of the ToolContext instead.

The tools read only `tool_context.state` and `tool_context.function_call_id`,
which both carry. ADK hides `tool_context` from the schema the model sees.
"""

import httpx
from google.adk.tools import ToolContext

from . import config, faults


async def refund_payment(order_id: str, amount_cents: int, reason: str, tool_context: ToolContext) -> dict:
    """Refund a customer's payment for an order through the payment provider.

    Args:
        order_id: The order to refund, like O-7781.
        amount_cents: The amount to refund, in cents.
        reason: Why the customer is being refunded.
    """
    side, run_id = tool_context.state["side"], tool_context.state["run_id"]
    headers = {}
    if tool_context.state.get("use_idempotency_key"):
        # The function call id is stable across retries and resumes on both
        # runtimes, so it identifies "this refund" rather than "this attempt".
        headers["Idempotency-Key"] = f"{run_id}:{tool_context.function_call_id}"
    # Demo harness: with several line items, one call can die before its money moves.
    await faults.maybe_crash_after_siblings(side, "batch_partial")
    async with httpx.AsyncClient(base_url=config.BUSINESS_URL, timeout=10) as client:
        response = await client.post("/payments/refunds", headers=headers, json={
            "side": side, "run_id": run_id, "order_id": order_id,
            "amount_cents": amount_cents, "reason": reason})
        response.raise_for_status()
        refund = response.json()
    faults.maybe_crash(side, "refund_charged")  # the money has moved; nothing has recorded it
    return {"status": "refunded", "refund_id": refund["refund_id"]}


async def record_refund(order_id: str, refund_id: str, amount_cents: int, tool_context: ToolContext) -> dict:
    """Record a completed refund against the order in the order system.

    Args:
        order_id: The refunded order.
        refund_id: The refund id the payment provider returned.
        amount_cents: The refunded amount, in cents.
    """
    async with httpx.AsyncClient(base_url=config.BUSINESS_URL, timeout=10) as client:
        response = await client.post(f"/orders/{order_id}/refunds", json={
            "side": tool_context.state["side"], "run_id": tool_context.state["run_id"],
            "refund_id": refund_id, "amount_cents": amount_cents})
        response.raise_for_status()
    return {"recorded": True}


async def notify_customer(order_id: str, message: str, tool_context: ToolContext) -> dict:
    """Email the customer about their refund.

    Args:
        order_id: The order the message is about.
        message: The text to send the customer.
    """
    async with httpx.AsyncClient(base_url=config.BUSINESS_URL, timeout=10) as client:
        response = await client.post("/notifications", json={
            "side": tool_context.state["side"], "run_id": tool_context.state["run_id"],
            "order_id": order_id, "message": message})
        response.raise_for_status()
    return {"sent": True}
