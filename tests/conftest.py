"""Runs a private copy of the whole demo stack, on its own ports, for the act tests.

Needs the Temporal CLI: the API starts its own dev server on a private port (17233), with eager
Workflow start and eager Activities on, so the tests never share a server with a running demo.
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
    "WORKER_A_PORT": "18331",
    "WORKER_B_PORT": "18332",
    "TEMPORAL_ADDRESS": "localhost:17233",
    "TEMPORAL_UI": "http://localhost:18233",
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
    yield Api(client, Path(log.name))
    proc.terminate()
    proc.wait(10)


class Api:
    def __init__(self, client: httpx.Client, log: Path) -> None:
        self.client = client
        self.log = log

    def _ok(self, response: httpx.Response) -> dict:
        """The JSON body; for an error, the API's own log tail, which holds its traceback."""
        if response.is_error:
            tail = "".join(self.log.read_text(errors="replace").splitlines(keepends=True)[-60:])
            raise AssertionError(f"{response.request.method} {response.request.url.path} -> {response.status_code}: "
                                 f"{response.text[:300]}\n--- api.log (last 60 lines) ---\n{tail}")
        return response.json()

    def post(self, path: str, body: dict | None = None) -> dict:
        return self._ok(self.client.post(path, json=body or {}))

    def state(self) -> dict:
        return self._ok(self.client.get("/api/state"))

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

