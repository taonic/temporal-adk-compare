"""Fast checks with no processes: the fake model, the payment provider and session classification."""

from fastapi.testclient import TestClient
from google.adk.events import Event, EventActions
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from refund_demo import config
from refund_demo.adk_side.recovery import invocation_status
from refund_demo.agent import request_text
from refund_demo.business.service import Store, create_app
from refund_demo.fake_llm import FakeRefundLlm


def _user(text):
    return types.Content(role="user", parts=[types.Part(text=text)])


def _result(name, response):
    return types.Content(role="user", parts=[types.Part(
        function_response=types.FunctionResponse(name=name, response=response))])


async def _next_call(*contents):
    config.FAKE_LLM_DELAY = 0
    llm = FakeRefundLlm(model="demo-refund-model")
    [response] = [r async for r in llm.generate_content_async(LlmRequest(contents=list(contents)))]
    part = response.content.parts[0]
    return (part.function_call.name, dict(part.function_call.args)) if part.function_call else ("text", part.text)


async def test_fake_model_follows_the_instruction():
    request = _user("Please refund order O-7781 for $500.00. Reason: item arrived damaged.")
    name, args = await _next_call(request)
    assert (name, args) == ("refund_payment", {"order_id": "O-7781", "amount_cents": 50000,
                                               "reason": "item arrived damaged"})
    refunded = _result("refund_payment", {"status": "refunded", "refund_id": "re_1"})
    assert (await _next_call(request, refunded))[0] == "record_refund"
    recorded = _result("record_refund", {"recorded": True})
    assert (await _next_call(request, refunded, recorded))[0] == "notify_customer"
    sent = _result("notify_customer", {"sent": True})
    assert (await _next_call(request, refunded, recorded, sent))[0] == "text"


async def test_fake_model_skips_recording_a_rejected_refund():
    request = _user("Please refund order O-7781 for $500.00. Reason: x.")
    rejected = _result("refund_payment", {"error": "This tool call is rejected."})
    name, args = await _next_call(request, rejected)
    assert name == "notify_customer" and "review" in args["message"]


def test_payment_provider_honours_idempotency_keys(tmp_path):
    config.BUSINESS_DELAY = 0
    client = TestClient(create_app(Store(tmp_path / "b.db")))
    body = {"side": "adk", "run_id": "r1", "order_id": "O-1", "amount_cents": 100}
    first = client.post("/payments/refunds", json=body, headers={"Idempotency-Key": "k"}).json()
    again = client.post("/payments/refunds", json=body, headers={"Idempotency-Key": "k"}).json()
    unkeyed = client.post("/payments/refunds", json=body).json()
    assert again == {"refund_id": first["refund_id"], "replayed": True}
    assert unkeyed["refund_id"] != first["refund_id"]
    rows = client.get("/state").json()["refunds"]
    assert [r["replayed"] for r in rows] == [0, 1, 0]


def _event(invocation, content=None, end=False, long_running=None):
    return Event(invocation_id=invocation, author="refund_agent", content=content,
                 actions=EventActions(end_of_agent=end or None), long_running_tool_ids=long_running)


def test_invocation_status():
    call = types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
        id="c1", name="refund_payment", args={}))])
    confirm = types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
        id="c2", name="adk_request_confirmation", args={}))])
    assert invocation_status([]) == "not_started"
    assert invocation_status([_event("i1", call)]) == "unfinished"
    assert invocation_status([_event("i1", call), _event("i1", end=True)]) == "completed"
    assert invocation_status([_event("i1", confirm, long_running={"c2"})]) == "awaiting_approval"


async def test_fake_model_refunds_line_items_in_one_turn():
    config.FAKE_LLM_DELAY = 0
    llm = FakeRefundLlm(model="demo-refund-model")
    request = _user(request_text("O-7781", 50000, "item arrived damaged", [20000, 15000, 15000]))
    [response] = [r async for r in llm.generate_content_async(LlmRequest(contents=[request]))]
    calls = [p.function_call for p in response.content.parts]
    assert [(c.name, c.args["amount_cents"]) for c in calls] == [
        ("refund_payment", 20000), ("refund_payment", 15000), ("refund_payment", 15000)]
    assert calls[2].args["reason"] == "item arrived damaged (item 3 of 3)"
