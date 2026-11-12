"""A deterministic stand-in for the LLM, so every run takes the same steps.

The demo is about what happens around the model, not the model's reasoning, so
a scripted model keeps the crash points repeatable on stage. It reads the
conversation the agent sends it and picks the next tool call the way a real
model following INSTRUCTION in agent.py would.

Set REFUND_DEMO_MODEL to a Gemini model name to use a real model instead.
"""

import asyncio
import re
from typing import AsyncGenerator, Optional

from google.adk.models import BaseLlm, LLMRegistry
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from . import config, faults


class FakeRefundLlm(BaseLlm):
    """Decides: refund, then record, then notify. Or notify only, if the refund was rejected.

    An order with several line items gets one refund_payment call per item, all
    in one model turn, as a real model would batch them.
    """

    # The same field, with the same meaning, as Gemini.retry_options: retries
    # inside this process, in this call's memory. None means one attempt.
    retry_options: Optional[types.HttpRetryOptions] = None

    @classmethod
    def supported_models(cls) -> list[str]:
        return [r"demo-refund-.*"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        retry = self.retry_options
        attempts = max(1, (retry.attempts or 1) if retry else 1)
        for attempt in range(1, attempts + 1):
            await asyncio.sleep(config.FAKE_LLM_DELAY)
            left = faults.model_outage_left()
            if left <= 0:
                break
            if attempt == attempts:
                # What a provider returns when it is overloaded.
                raise ModelUnavailable(f"429 RESOURCE_EXHAUSTED: model overloaded, retry later "
                                       f"(simulated outage, {left:.0f}s left)")
            # Backoff as google-genai computes it (without jitter). The attempt
            # count exists only here, in this coroutine; the process log is its only trace.
            delay = min((retry.initial_delay or 1.0) * (retry.exp_base or 2.0) ** (attempt - 1), retry.max_delay or 60.0)
            print(f"model call failed: 429 overloaded; in-process retry {attempt + 1} of {attempts} "
                  f"in {delay:.0f}s", flush=True)
            await asyncio.sleep(delay)
        request = _request_details(llm_request)
        yield _next_step(request, _tool_results(llm_request))


class ModelUnavailable(RuntimeError):
    """The model provider is rejecting calls."""


def _next_step(request: dict, results: dict[str, list[dict]]) -> LlmResponse:
    """Refund every line item, then record each refund, then notify. Notify only if a refund was rejected."""
    order_id = request["order_id"]
    items = request["line_items"] or [request["amount_cents"]]
    total = f"${request['amount_cents'] / 100:.2f}"
    refunds = results.get("refund_payment", [])
    if not refunds:
        if len(items) == 1:
            return _calls(("refund_payment", dict(order_id=order_id, amount_cents=items[0], reason=request["reason"])))
        # One turn, several function calls: the runtime runs them in parallel.
        return _calls(*[("refund_payment", dict(order_id=order_id, amount_cents=cents,
                                                reason=f"{request['reason']} (item {i} of {len(items)})"))
                        for i, cents in enumerate(items, 1)])
    if any("error" in r or r.get("status") != "refunded" for r in refunds):
        if "notify_customer" not in results:
            return _calls(("notify_customer", dict(
                order_id=order_id,
                message="Your refund needs a manager's review. We will update you within 2 business days.")))
        return _text(f"Refund for {order_id} was not approved; the customer has been told it is under review.")
    if "record_refund" not in results:
        return _calls(*[("record_refund", dict(order_id=order_id, refund_id=r["refund_id"], amount_cents=cents))
                        for r, cents in zip(refunds, items)])
    if "notify_customer" not in results:
        return _calls(("notify_customer", dict(order_id=order_id, message=f"Your refund of {total} is on its way.")))
    return _text(f"Refunded {total} for {order_id}, recorded it and told the customer.")


def _request_details(llm_request: LlmRequest) -> dict:
    """The order, amount and reason from the customer's message."""
    text = " ".join(p.text for c in llm_request.contents if c.role == "user" for p in (c.parts or []) if p.text)
    order = re.search(r"\b(O-\d+)\b", text)
    amount = re.search(r"\$(\d+(?:\.\d{2})?)", text)
    reason = re.search(r"[Rr]eason:\s*(.+?)(?:\.|$)", text)
    items = re.search(r"line items \(([^)]*)\)", text)
    return {
        "order_id": order.group(1) if order else "O-0000",
        "amount_cents": round(float(amount.group(1)) * 100) if amount else 0,
        "reason": reason.group(1) if reason else "customer request",
        "line_items": [round(float(a) * 100) for a in re.findall(r"\$(\d+(?:\.\d{2})?)", items.group(1))]
                      if items else [],
    }


def _tool_results(llm_request: LlmRequest) -> dict[str, list[dict]]:
    """The latest response to each function call, grouped by tool, in call order.

    A call can be answered twice: a tool awaiting a manager's confirmation first
    gets a placeholder response, then the real one under the same call id.
    """
    by_call: dict[str, dict[str, dict]] = {}
    for content in llm_request.contents:
        for part in content.parts or []:
            response = part.function_response
            if response and response.name:
                calls = by_call.setdefault(response.name, {})
                calls[response.id or str(len(calls))] = dict(response.response or {})
    return {name: list(calls.values()) for name, calls in by_call.items()}


def _calls(*calls: tuple[str, dict]) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[
        types.Part(function_call=types.FunctionCall(name=name, args=args)) for name, args in calls]))


def _text(text: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


LLMRegistry.register(FakeRefundLlm)
