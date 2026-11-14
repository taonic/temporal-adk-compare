"""Each act of the demo, run headless against real processes and real crashes.

These pin down the claims the demo makes on stage, so that they are
reproducible rather than staged.
"""

import time

import pytest

from conftest import proc_state, refunds, restarts, table

TEMPORAL_DONE = lambda s: s["temporal"].get("status") == "COMPLETED"  # noqa: E731
ADK_DONE = lambda s: s["adk"].get("status") == "completed"  # noqa: E731


@pytest.fixture(autouse=True)
def clean_stage(api):
    api.post("/api/reset")


def test_act3_crash_after_tool_returned(api):
    """Same crash point: ADK refunds twice once resumed; Temporal refunds once, with no one resuming it."""
    api.post("/api/faults", {"side": "both", "point": "refund_returned"})
    before = restarts(api.state(), "adk-a")
    api.post("/api/runs", {})

    state = api.wait(TEMPORAL_DONE, what="Temporal to finish on its own")
    assert refunds(state, "temporal") == 1
    assert restarts(state, "adk-a") > before
    assert state["adk"]["status"] == "unfinished", "the restarted ADK host did not pick the work up"
    assert refunds(state, "adk") == 1

    api.post("/api/adk/recover")
    state = api.wait(ADK_DONE, what="ADK resume to finish")
    assert refunds(state, "adk") == 2, "ADK resume re-ran refund_payment"
    assert refunds(state, "temporal") == 1
    assert table(state, "notifications", "adk") == 1
    assert table(state, "notifications", "temporal") == 1


@pytest.mark.parametrize("use_key", [False, True])
def test_act3_crash_inside_tool_needs_idempotency_on_both(api, use_key):
    """Crash after the money moved but before the tool returned: neither runtime can know. Both need a key."""
    api.post("/api/settings", {"use_idempotency_key": use_key})
    api.post("/api/faults", {"side": "both", "point": "refund_charged"})
    before = restarts(api.state(), "adk-a")
    api.post("/api/runs", {})

    api.wait(lambda s: restarts(s, "adk-a") > before and proc_state(s, "adk-a") == "running",
             what="ADK host A to crash and be restarted")
    api.post("/api/adk/recover")
    state = api.wait(lambda s: TEMPORAL_DONE(s) and ADK_DONE(s), timeout=60, what="both sides to finish")

    for side in ("adk", "temporal"):
        if use_key:
            assert refunds(state, side) == 1 and refunds(state, side, replayed=True) == 1, side
        else:
            assert refunds(state, side) == 2, side


def test_act1_nobody_resumes_adk(api):
    """A second ADK host sits idle next to the unfinished invocation; a second Worker just continues.

    Played as the act plays it: the run starts on ADK alone, then the same run on Temporal. Two hosts
    and two Workers run side by side from the start, and each run stays on A until A dies: ADK's
    because the API sends it to host A, Temporal's because Worker A starts it eagerly and its sticky
    queue and eager Activities keep it there.
    """
    api.post("/api/settings", {"auto_restart": False})
    api.post("/api/processes/adk-b/start")
    api.post("/api/processes/worker-b/start")
    api.post("/api/faults", {"side": "both", "point": "before_notify"})
    run = api.post("/api/runs", {"side": "adk"})
    assert list(run["started"]) == ["adk"]

    api.wait(lambda s: proc_state(s, "adk-a") == "crashed", what="ADK host A to crash")
    time.sleep(3)
    state = api.state()
    assert state["temporal"] == {}, "Temporal has not started this run yet"
    assert proc_state(state, "adk-b") == "running"
    assert state["adk"]["status"] == "unfinished", "nothing in ADK picked the invocation up"
    assert table(state, "notifications", "adk") == 0

    api.post("/api/adk/recover")
    state = api.wait(ADK_DONE, what="ADK resume on host B")
    assert table(state, "notifications", "adk") == 1
    assert refunds(state, "adk") == 1, "resuming after a crash before notify does not repeat the refund"

    # Worker B is up, idle, the whole time: Temporal keeps the run on Worker A until it dies.
    run = api.post("/api/runs/continue", {"side": "temporal"})
    assert run["run_id"] == state["run"]["run_id"] and set(run["started"]) == {"adk", "temporal"}
    state = api.wait(TEMPORAL_DONE, what="Worker B to finish the refund after Worker A dies")
    assert proc_state(state, "worker-a") == "crashed" and proc_state(state, "worker-b") == "running"
    assert any(l["proc"] == "Worker A" and "eagerly: its first Workflow Task came straight back to me" in l["line"]
               for l in state["logs"]["temporal"]), "Worker A started the Workflow eagerly"
    assert table(state, "notifications", "temporal") == 1
    assert refunds(state, "temporal") == 1
    completed_by = {}  # each Activity's first completion: the model is called again after the email
    for e in state["temporal"]["history"]:
        if e["type"] == "ActivityTaskCompleted":
            completed_by.setdefault(e["activity"], e["by"])
    assert (completed_by["invoke_model"], completed_by["refund_payment"], completed_by["record_refund"],
            completed_by["notify_customer"]) == ("worker-a", "worker-a", "worker-a", "worker-b"), \
        "everything before the crash ran on Worker A (eager start, sticky queue, eager Activities); the rest on B"
    # ADK's events carry the host that wrote them: the refund on host A, the email after the resume on B.
    hosts = {r["name"]: e["host"] for e in state["adk"]["events"] for r in e["responses"]}
    assert (hosts["refund_payment"], hosts["notify_customer"]) == ("host-a", "host-b")


def test_act4_in_memory_timer_fires_while_the_host_lives(api):
    """Both pause the same way and both enforce the deadline, as long as every process stays up."""
    api.post("/api/runs", {"approval": True, "approval_deadline_seconds": 4})
    api.wait(lambda s: s["adk"].get("status") == "awaiting_approval" and s["temporal"].get("awaiting"),
             what="both sides to wait for a manager")
    assert any("timer held in this process's memory" in l["line"] for l in api.state()["logs"]["adk"])

    state = api.wait(lambda s: TEMPORAL_DONE(s) and ADK_DONE(s), what="both deadlines to fire")
    for side in ("adk", "temporal"):
        assert table(state, "escalations", side) == 1
        assert refunds(state, side) == 0
        assert table(state, "notifications", side) == 1


def test_act4_timers_when_every_process_stops(api):
    """Stop every process during the wait. Temporal's timer fires on the Server; ADK's died with its host."""
    api.post("/api/runs", {"approval": True, "approval_deadline_seconds": 4})
    state = api.wait(lambda s: s["adk"].get("status") == "awaiting_approval" and s["temporal"].get("awaiting"),
                     what="both sides to wait for a manager")
    deadline_at = state["adk"]["awaiting"]["deadline_at"]
    api.post("/api/processes/adk-a/stop")
    api.post("/api/processes/worker-a/stop")
    api.wait(lambda s: s["now"] > deadline_at + 1, what="the deadline to pass with no process running")
    api.post("/api/processes/adk-a/start")
    api.post("/api/processes/worker-a/start")

    state = api.wait(TEMPORAL_DONE, what="Temporal to act on the timer that fired while no Worker ran")
    assert table(state, "escalations", "temporal") == 1
    assert refunds(state, "temporal") == 0
    time.sleep(2)
    state = api.state()
    assert state["adk"]["status"] == "awaiting_approval", "the in-memory timer died with the host"
    assert table(state, "escalations", "adk") == 0

    api.post("/api/approve", {"side": "adk", "approved": True})
    state = api.wait(ADK_DONE, what="ADK to finish after a late approval")
    assert refunds(state, "adk") == 1


def test_act4_approval_in_time(api):
    api.post("/api/runs", {"approval": True, "approval_deadline_seconds": 60})
    api.wait(lambda s: s["temporal"].get("awaiting"), what="Temporal to wait for a manager")
    api.post("/api/approve", {"side": "temporal", "approved": True})
    state = api.wait(TEMPORAL_DONE, what="Temporal to finish after approval")
    assert refunds(state, "temporal") == 1
    assert table(state, "escalations", "temporal") == 0


ITEMS = [20000, 15000, 15000]


def test_parallel_calls_crash_mid_batch(api):
    """Three refunds in one model turn, crash before the last one pays.

    ADK stores a turn's results as one event, after every call returns, so a
    resume repeats the two that had finished. Temporal recorded each Activity
    as it completed and runs only the unfinished one: no key needed here.
    """
    api.post("/api/faults", {"side": "both", "point": "batch_partial"})
    before = restarts(api.state(), "adk-a")
    api.post("/api/runs", {"line_items": ITEMS})

    state = api.wait(lambda s: TEMPORAL_DONE(s) and restarts(s, "adk-a") > before
                     and proc_state(s, "adk-a") == "running" and s["adk"].get("status") == "unfinished",
                     timeout=60, what="Temporal to finish and ADK host A to crash and restart")
    assert refunds(state, "temporal") == 3
    assert table(state, "order_refunds", "temporal") == 3
    assert refunds(state, "adk") == 2, "two line items were refunded before the crash"
    assert not any(r for e in state["adk"]["events"] for r in e["responses"] if r["name"] == "refund_payment"), \
        "ADK persisted none of the finished results"

    api.post("/api/adk/recover")
    state = api.wait(ADK_DONE, what="ADK resume to finish")
    assert refunds(state, "adk") == 5, "resume re-ran all three calls, including the two that had finished"
    assert table(state, "order_refunds", "adk") == 3
    assert refunds(state, "temporal") == 3


@pytest.mark.parametrize("use_key", [False, True])
def test_two_hosts_resume_the_same_invocation(api, use_key):
    """Each host runs the recovery scan: both resume, both refund, one then fails to save."""
    api.post("/api/settings", {"use_idempotency_key": use_key})
    api.post("/api/processes/adk-b/start")
    api.post("/api/processes/worker-b/start")
    api.post("/api/faults", {"side": "both", "point": "refund_returned"})
    before = restarts(api.state(), "adk-a")
    api.post("/api/runs", {})
    api.wait(lambda s: TEMPORAL_DONE(s) and restarts(s, "adk-a") > before and proc_state(s, "adk-a") == "running"
             and s["adk"].get("status") == "unfinished", timeout=60, what="Temporal to finish, ADK to crash")

    resumed = api.post("/api/adk/recover", {"hosts": "all"})["resumed"]
    assert sorted(r["host"] for r in resumed) == ["a", "b"]
    state = api.wait(ADK_DONE, what="ADK to finish on one of the hosts")
    state = api.wait(lambda s: any("StaleSessionError" in l["line"] for l in s["logs"]["adk"]),
                     what="the other host to fail with StaleSessionError")
    if use_key:
        assert refunds(state, "adk") == 1 and refunds(state, "adk", replayed=True) == 2
    else:
        assert refunds(state, "adk") == 3, "the original refund, plus one per resuming host"
    assert table(state, "notifications", "adk") == 1
    assert refunds(state, "temporal") == 1


def test_act2_retries_in_memory_vs_persisted(api):
    """Both sides retry the model on the same schedule. A crash mid-retry ends ADK's
    in-process retries; Temporal's attempt count lives on the Server and carries on."""
    api.post("/api/model-outage", {"seconds": 30})
    api.post("/api/runs", {})

    def model_attempt(s):
        return max([p["attempt"] for p in s["temporal"].get("pending", []) if p["activity"] == "invoke_model"], default=0)

    state = api.wait(lambda s: (s["adk"].get("retrying") or {}).get("attempt", 0) >= 2 and model_attempt(s) >= 2,
                     what="both sides to retry the model")
    before = model_attempt(state)
    api.post("/api/processes/adk-a/kill")
    api.post("/api/processes/worker-a/kill")
    api.post("/api/model-outage", {"seconds": 4})

    state = api.wait(TEMPORAL_DONE, timeout=45, what="Temporal to finish after the restart and the outage")
    started = [e["attempt"] for e in state["temporal"]["history"]
               if e["type"] == "ActivityTaskStarted" and e["activity"] == "invoke_model"]
    assert max(started) > before, "the restarted Worker carried on with the attempt count"
    assert refunds(state, "temporal") == 1
    time.sleep(1)
    state = api.state()
    assert state["adk"]["status"] == "unfinished", "the retry loop died with the host"
    assert not state["adk"].get("retrying")
    assert refunds(state, "adk") == 0

    api.post("/api/adk/recover")
    state = api.wait(ADK_DONE, what="ADK resume after the crash")
    assert refunds(state, "adk") == 1
