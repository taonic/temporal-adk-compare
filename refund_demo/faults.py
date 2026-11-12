"""Fault injection: kill the process for real at a named point.

This is demo harness, not application logic. The presenter (or a test) arms a
crash point for one side; the first process on that side to reach the point
disarms it and dies with os._exit, the same as `kill -9`. Nothing is flushed,
no `finally` block runs, no checkpoint is written.

Crash points, in the order the agent reaches them:

- refund_charged:   inside refund_payment, after the payment provider has
                    refunded the money, before the tool returns.
- refund_returned:  after refund_payment has returned, before the runtime has
                    recorded its result (ADK's after_tool_callback).
- before_notify:    just before notify_customer runs.
- batch_partial:    inside one of several refund_payment calls made in the
                    same model turn, before its money moves. That call holds
                    on until the other line items are refunded, then dies.

Separately, a model outage makes the fake model reject every call for a number
of seconds, on both sides at once, the way a provider's 429s would.
"""

import asyncio
import json
import os
import sys
import time

from . import config

POINTS = {
    "refund_charged": "inside refund_payment, after the money moved, before the tool returns",
    "refund_returned": "after refund_payment returned, before the runtime records the result",
    "before_notify": "just before notify_customer runs",
    "batch_partial": "one of several parallel refunds, before its money moves, after the others finished",
}


def arm(side: str, point: str | None) -> None:
    """Arm `point` for the next process on `side` to reach it, or disarm with None."""
    path = config.FAULTS_DIR / f"{side}.json"
    if point is None:
        path.unlink(missing_ok=True)
        return
    if point not in POINTS:
        raise ValueError(f"unknown crash point {point!r}")
    config.FAULTS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"point": point, "armed_at": time.time()}))


def armed(side: str) -> str | None:
    try:
        return json.loads((config.FAULTS_DIR / f"{side}.json").read_text())["point"]
    except (FileNotFoundError, ValueError, KeyError):
        return None


def maybe_crash(side: str, point: str) -> None:
    """Die here if `point` is armed for `side`. Safe to call from workflow code."""
    try:
        from temporalio import workflow

        in_workflow = workflow.in_workflow()
    except ImportError:  # pragma: no cover - the ADK side never needs temporalio here
        in_workflow = False
    if not in_workflow:
        _crash_if_armed(side, point)
        return
    # Workflow code runs in Temporal's sandbox and is replayed after a crash.
    # Fault injection steps outside both: it is not part of the workflow.
    if workflow.unsafe.is_replaying():
        return
    with workflow.unsafe.sandbox_unrestricted():
        _crash_if_armed(side, point)


async def maybe_crash_after_siblings(side: str, point: str) -> None:
    """Die here if `point` is armed for `side`, after giving parallel calls time to finish first.

    The first call to arrive claims the point, so its siblings run normally.
    There is no await between the check and the claim: calls sharing one event
    loop cannot both claim it.
    """
    if armed(side) != point:
        return
    arm(side, None)
    print(f"⏳ {point}: holding this call while the parallel ones finish", flush=True)
    await asyncio.sleep(config.BUSINESS_DELAY + 1.5)
    _die(point)


def start_model_outage(seconds: float) -> None:
    config.FAULTS_DIR.mkdir(parents=True, exist_ok=True)
    (config.FAULTS_DIR / "model.json").write_text(json.dumps({"until": time.time() + seconds}))


def model_outage_left() -> float:
    """Seconds until the simulated model outage ends; 0 when the model is up."""
    try:
        until = json.loads((config.FAULTS_DIR / "model.json").read_text())["until"]
    except (FileNotFoundError, ValueError, KeyError):
        return 0.0
    return max(0.0, until - time.time())


def end_model_outage() -> None:
    (config.FAULTS_DIR / "model.json").unlink(missing_ok=True)


def _crash_if_armed(side: str, point: str) -> None:
    if armed(side) != point:
        return
    arm(side, None)
    _die(point)


def _die(point: str) -> None:
    print(f"💥 CRASH at {point}: {POINTS[point]} (os._exit, like kill -9)", flush=True)
    sys.stdout.flush()
    os._exit(137)
