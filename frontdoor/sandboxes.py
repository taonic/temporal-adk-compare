"""One Daytona sandbox per visitor, each running the whole demo.

A visitor's sandbox is found by its labels, so the front door keeps no state that matters: a
restart finds the same sandboxes. A new sandbox is created from the snapshot (frontdoor/snapshot.py)
and gets the current refund_demo/ uploaded; then `python -m refund_demo.api` starts everything,
exactly as `make demo` does locally. A sandbox stops when idle and is deleted a day after that;
the next visit starts it again and the demo with it.

Adapted from temporal-agent-governance-workshop's course/runner/sandboxes.mjs.
"""

import asyncio
import hashlib
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from daytona import (AsyncDaytona, CreateSandboxFromSnapshotParams, FileUpload, ListSandboxesQuery,
                     SessionExecuteRequest)

from . import snapshot

ROOT = Path(__file__).resolve().parent.parent
APP_LABEL = "adk-compare"
APP_DIR = "/app"
DEMO_PORT = 8400
TEMPORAL_UI_PORT = 8233
STAMP = f"{APP_DIR}/.code-stamp"
DEMO_SESSION = "demo"

RUNNING = {"started", "starting", "creating", "restoring", "pulling_snapshot", "resuming"}
GONE = {"destroyed", "destroying", "error", "build_failed"}
SKIP = ("__pycache__", ".DS_Store", ".pyc")


def code_files() -> list[Path]:
    """Every file of the demo a sandbox runs: refund_demo/, as it is in this deploy."""
    return sorted(p for p in (ROOT / "refund_demo").rglob("*")
                  if p.is_file() and not any(s in str(p) for s in SKIP))


def code_stamp() -> str:
    digest = hashlib.sha256()
    for path in code_files():
        digest.update(f"{path.relative_to(ROOT)}\0".encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def state_of(sandbox) -> str:
    state = getattr(sandbox, "state", None)
    return str(getattr(state, "value", state) or "").lower()


@dataclass
class Settings:
    snapshot: str = field(default_factory=snapshot.name)
    max_sandboxes: int = 20
    auto_stop_minutes: int = 15
    auto_delete_minutes: int = 24 * 60
    # Where visitors reach the Temporal UI (the front door's UI port): the demo links to it.
    public_ui_base: str = "http://localhost:8081"


@dataclass
class Progress:
    state: str  # finding | creating | waking | starting | ready | full | error
    message: str
    started_at: float = field(default_factory=time.time)


class SandboxManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.daytona = AsyncDaytona()
        self.stamp = code_stamp()
        self.progress: dict[str, Progress] = {}
        self.inflight: dict[str, asyncio.Task] = {}
        self.ready: dict[str, object] = {}  # visitor -> sandbox, verified up
        self.previews: dict[tuple[str, int], tuple[str, float]] = {}  # (sandbox id, port) -> (url, expires)
        self.creating = 0

    async def close(self) -> None:
        await self.daytona.close()

    # ---- Finding ------------------------------------------------------------------

    async def _list(self, labels: dict[str, str]) -> list:
        found = []
        async def read():
            async for sandbox in self.daytona.list(ListSandboxesQuery(labels=labels)):
                found.append(sandbox)
        await asyncio.wait_for(read(), 30)
        return found

    async def find(self, visitor: str):
        rows = await self._list({"app": APP_LABEL, "visitor": visitor})
        return next((s for s in rows if state_of(s) not in GONE), None)

    async def _count_running(self) -> int:
        rows = await self._list({"app": APP_LABEL})
        return sum(1 for s in rows if state_of(s) in RUNNING) + self.creating

    # ---- Getting a visitor's stage up ----------------------------------------------

    def status(self, visitor: str) -> Progress:
        if visitor in self.ready and visitor not in self.inflight:
            return Progress("ready", "Your stage is ready.")
        return self.progress.get(visitor) or Progress("none", "No stage yet.")

    def ensure(self, visitor: str) -> Progress:
        """Start getting the visitor's stage ready, once; every caller shares the same run."""
        if visitor in self.ready:
            return self.status(visitor)
        if visitor not in self.inflight:
            self.progress[visitor] = Progress("finding", "Looking for your stage…")
            task = asyncio.create_task(self._ensure(visitor))
            self.inflight[visitor] = task
            task.add_done_callback(lambda _: self.inflight.pop(visitor, None))
        return self.progress[visitor]

    def _step(self, visitor: str, state: str, message: str) -> None:
        self.progress[visitor] = Progress(state, message)

    async def _ensure(self, visitor: str) -> None:
        try:
            sandbox = await self.find(visitor)
            fresh = sandbox is None
            if fresh:
                if await self._count_running() >= self.settings.max_sandboxes:
                    self._step(visitor, "full", f"All {self.settings.max_sandboxes} stages are in use right now. "
                                                "Try again in a few minutes, or run the demo locally (make demo).")
                    return
                self._step(visitor, "creating", "Creating your own stage…")
                self.creating += 1
                try:
                    sandbox = await self.daytona.create(CreateSandboxFromSnapshotParams(
                        snapshot=self.settings.snapshot, labels={"app": APP_LABEL, "visitor": visitor},
                        auto_stop_interval=self.settings.auto_stop_minutes,
                        auto_delete_interval=self.settings.auto_delete_minutes), timeout=240)
                finally:
                    self.creating -= 1
            elif state_of(sandbox) != "started":
                self._step(visitor, "waking", "Waking your stage…")
                for _ in range(60):  # a sandbox that is stopping can't be started until it has stopped
                    if state_of(sandbox) not in ("stopping", "archiving"):
                        break
                    await asyncio.sleep(2)
                    await sandbox.refresh_data()
                await sandbox.start(timeout=180)

            self._step(visitor, "starting", "Starting the demo: Temporal, the ADK hosts, the Workers…")
            await self._bring_up(sandbox, fresh)
            self.ready[visitor] = sandbox
            self._step(visitor, "ready", "Your stage is ready.")
        except Exception as err:  # noqa: BLE001 - shown on the waiting page
            self._step(visitor, "error", f"Couldn't start your stage: {str(err)[:300]}")

    async def _exec(self, sandbox, command: str, timeout: int = 60) -> str:
        response = await sandbox.process.exec(command, cwd=APP_DIR, timeout=timeout)
        return (response.result or "").strip()

    async def _bring_up(self, sandbox, fresh: bool) -> None:
        current = "" if fresh else await self._exec(sandbox, f"cat {STAMP} 2>/dev/null || true", 20)
        healthy = not fresh and await self._healthy(sandbox)
        if current == self.stamp and healthy:
            return
        if current != self.stamp:
            # This deploy's code: refund_demo/ replaced as a whole, so nothing stale is left over.
            await self._exec(sandbox, "pkill -f 'refund_demo|temporal server' || true; rm -rf refund_demo", 30)
            await sandbox.fs.upload_files([FileUpload(source=p.read_bytes(), destination=f"{APP_DIR}/{p.relative_to(ROOT)}")
                                           for p in code_files()])
            await self._exec(sandbox, f"echo {self.stamp} > {STAMP}", 20)
        elif not healthy:
            await self._exec(sandbox, "pkill -f 'refund_demo|temporal server' || true", 30)
        env = {"API_HOST": "0.0.0.0", "TEMPORAL_UI_IP": "0.0.0.0", "PYTHONUNBUFFERED": "1",
               "TEMPORAL_UI": self.settings.public_ui_base}
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
        # A long-running process runs as an asynchronous command in a session of its own: a plain
        # exec waits for it, even backgrounded.
        try:
            await sandbox.process.delete_session(DEMO_SESSION)
        except Exception:  # noqa: BLE001 - there was none
            pass
        await sandbox.process.create_session(DEMO_SESSION)
        await sandbox.process.execute_session_command(DEMO_SESSION, SessionExecuteRequest(
            command=f"cd {APP_DIR} && mkdir -p var && {exports} .venv/bin/python -m refund_demo.api > var/api.log 2>&1",
            run_async=True))
        for _ in range(90):
            if await self._healthy(sandbox):
                return
            await asyncio.sleep(2)
        tail = await self._exec(sandbox, "tail -20 var/api.log", 20)
        raise RuntimeError(f"the demo didn't come up in 3 minutes. Its log:\n{tail}")

    async def _healthy(self, sandbox) -> bool:
        out = await self._exec(sandbox, f"curl -sf -o /dev/null http://localhost:{DEMO_PORT}/api/state "
                                        f"&& curl -sf -o /dev/null http://localhost:{TEMPORAL_UI_PORT}/ && echo up || echo down", 20)
        return out.endswith("up")

    def forget(self, visitor: str) -> None:
        """The proxy couldn't reach the sandbox (stopped while idle, say): check it again next time."""
        sandbox = self.ready.pop(visitor, None)
        if sandbox is not None:
            for key in [k for k in self.previews if k[0] == sandbox.id]:
                self.previews.pop(key, None)

    # ---- Reaching it -------------------------------------------------------------------

    async def preview_url(self, visitor: str, port: int) -> str | None:
        """A signed preview URL for one of the visitor's ports, valid for an hour, reused for 50 minutes."""
        sandbox = self.ready.get(visitor)
        if sandbox is None:
            return None
        key = (sandbox.id, port)
        cached = self.previews.get(key)
        if cached and cached[1] > time.time():
            return cached[0]
        signed = await sandbox.create_signed_preview_url(port, expires_in_seconds=3600)
        url = signed.url.rstrip("/")
        self.previews[key] = (url, time.time() + 50 * 60)
        return url
