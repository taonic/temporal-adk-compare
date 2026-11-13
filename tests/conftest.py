"""Runs a private copy of the whole demo stack, on its own ports, for the act tests.

Needs a Temporal Server on localhost:7233 (`temporal server start-dev`); the
API starts one itself if none is running.
"""

import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent

TEST_ENV = {
    "API_PORT": "18300",
    "BUSINESS_PORT": "18320",
    "BUSINESS_URL": "http://127.0.0.1:18320",
    "ADK_HOST_A_PORT": "18311",
    "ADK_HOST_B_PORT": "18312",
    "TASK_QUEUE": f"refunds-test-{secrets.token_hex(3)}",
    "FAKE_LLM_DELAY": "0.1",
    "BUSINESS_DELAY": "0.05",
    "RESTART_DELAY": "0.5",
    "PYTHONUNBUFFERED": "1",
}


@pytest.fixture(scope="session")
def api(tmp_path_factory):
    env = {**os.environ, **TEST_ENV, "REFUND_DEMO_VAR": str(tmp_path_factory.mktemp("var"))}
    log = open(tmp_path_factory.getbasetemp() / "api.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "refund_demo.api"], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    client = httpx.Client(base_url=f"http://127.0.0.1:{TEST_ENV['API_PORT']}", timeout=15)
    deadline = time.time() + 60
    while True:
        try:
            if client.get("/api/state").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.time() > deadline or proc.poll() is not None:
            raise RuntimeError(f"API did not start; see {log.name}")
        time.sleep(0.3)
    yield Api(client)
    proc.terminate()
    proc.wait(10)


class Api:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def post(self, path: str, body: dict | None = None) -> dict:
        response = self.client.post(path, json=body or {})
        response.raise_for_status()
        return response.json()

    def state(self) -> dict:
        return self.client.get("/api/state").json()

    def wait(self, predicate, timeout: float = 45, what: str = "condition") -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.state()
            if predicate(state):
                return state
            time.sleep(0.3)
        raise AssertionError(f"timed out waiting for {what}\n{summary(state)}")


def summary(state: dict) -> str:
    """The parts of /api/state worth reading when a test fails."""
    lines = ["processes: " + ", ".join(f"{p['key']}={p['state']}" for p in state["processes"]),
             f"adk: {state['adk'].get('status')}   temporal: {state['temporal'].get('status')}",
             "refunds: " + str([(r["side"], r["replayed"]) for r in state["business"].get("refunds", [])])]
    for side in ("adk", "temporal"):
        lines += [f"  {side} | {entry['line'][:160]}" for entry in state["logs"][side][-20:]]
    lines += [f"  history | {e['type']} {e.get('activity') or ''} {e.get('identity') or ''}"
              for e in state["temporal"].get("history", [])[-15:]]
    return "\n".join(lines)


def refunds(state: dict, side: str, replayed: bool = False) -> int:
    """How many times money really moved (or, with replayed=True, was asked again and deduplicated)."""
    return sum(1 for r in state["business"]["refunds"] if r["side"] == side and bool(r["replayed"]) == replayed)


def table(state: dict, name: str, side: str) -> int:
    return sum(1 for r in state["business"][name] if r["side"] == side)


def proc_state(state: dict, key: str) -> str:
    return next(p["state"] for p in state["processes"] if p["key"] == key)


def restarts(state: dict, key: str) -> int:
    return next(p["restarts"] for p in state["processes"] if p["key"] == key)

