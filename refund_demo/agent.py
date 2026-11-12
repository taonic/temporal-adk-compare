"""The refund agent. Both columns of the demo run this exact definition.

Only two things differ between the runtimes, and each runtime passes them in:

- the model: a model name for ADK; `TemporalModel(name)` for Temporal, which
  runs each model call as an Activity;
- the tools: the plain ADK functions in tools.py for ADK; for Temporal, the
  Activities in temporal_side/activities.py (each one calls the same function
  in tools.py), wrapped by `activity_as_tool`.

Everything else, from the instruction and the approval policy to the callbacks,
is identical.
"""

from typing import Any, Callable

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm
from google.adk.tools import FunctionTool

from . import fake_llm  # noqa: F401  registers the deterministic model
from . import faults

INSTRUCTION = """\
You handle refund requests for an online store.
1. Call refund_payment for the order, the amount and the customer's reason.
   If the order has several line items, refund each line item separately,
   with all the refund_payment calls in one turn.
2. If the refund succeeded, call record_refund with the refund id it returned
   (one call per refund).
3. Call notify_customer to tell the customer what happened.
If refund_payment is rejected, do not record anything: tell the customer the
refund is under review. Never refund the same order twice.
"""


def request_text(order_id: str, amount_cents: int, reason: str, line_items: list[int] | None = None) -> str:
    """The customer's message. Both runtimes send exactly this text to the agent."""
    items = ""
    if line_items:
        items = f" in {len(line_items)} line items ({', '.join(f'${c / 100:.2f}' for c in line_items)})"
    return f"Please refund order {order_id} for ${amount_cents / 100:.2f}{items}. Reason: {reason}."


def needs_manager_approval(order_id: str, amount_cents: int, reason: str, tool_context: Any) -> bool:
    """Approval policy: refunds above the run's threshold wait for a manager."""
    threshold = tool_context.state.get("approval_threshold_cents")
    return threshold is not None and amount_cents > threshold


def build_agent(*, model: str | BaseLlm, refund_payment: Callable, record_refund: Callable,
                notify_customer: Callable) -> LlmAgent:
    return LlmAgent(
        name="refund_agent",
        model=model,
        instruction=INSTRUCTION,
        tools=[
            FunctionTool(refund_payment, require_confirmation=needs_manager_approval),
            record_refund,
            notify_customer,
        ],
        before_tool_callback=_before_tool,
        after_tool_callback=_after_tool,
    )


# Fault-injection hooks. In production these callbacks would not exist; here
# they let the demo kill the process at an exact point. See faults.py.

def _before_tool(tool, args, tool_context) -> None:
    if tool.name == "notify_customer":
        faults.maybe_crash(tool_context.state["side"], "before_notify")
    return None


def _after_tool(tool, args, tool_context, tool_response) -> None:
    if tool.name == "refund_payment" and tool_response.get("status") == "refunded":
        faults.maybe_crash(tool_context.state["side"], "refund_returned")
    return None
