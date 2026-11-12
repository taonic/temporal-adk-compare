"""Starts, kills and (optionally) restarts the demo's child processes.

Each ADK host and each Temporal Worker is a real OS process. Killing one is a
real SIGKILL; a crash point is a real os._exit. With auto-restart on, this
module plays the part of Kubernetes or systemd: it restarts a dead process
after a short delay, and does nothing else. In particular it does not tell
the restarted process what it was doing.
"""

import asyncio
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field

from . import config

RESTART_DELAY = float(os.environ.get("RESTART_DELAY", 2.0))


@dataclass
class ManagedProcess:
    key: str
    label: str
    side: str  # "adk", "temporal" or "system"
    argv: list[str]
    state: str = "stopped"  # stopped | running | crashed | restarting
    exit_code: int | None = None
    restarts: int = 0
    started_at: float | None = None
    logs: deque = field(default_factory=lambda: deque(maxlen=400))
    _proc: asyncio.subprocess.Process | None = None
    _stopping: bool = False
    _watch: asyncio.Task | None = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc and self._proc.returncode is None else None

    def log(self, line: str) -> None:
        self.logs.append((time.time(), line))


class Supervisor:
    def __init__(self, extra_env: dict[str, str] | None = None) -> None:
        self.processes: dict[str, ManagedProcess] = {}
        self.auto_restart = True
        self._env = {**os.environ, "PYTHONUNBUFFERED": "1", **(extra_env or {})}

    def add(self, key: str, label: str, side: str, module: str, *args: str) -> ManagedProcess:
        proc = ManagedProcess(key, label, side, [sys.executable, "-m", module, *args])
        self.processes[key] = proc
        return proc

    async def start(self, key: str) -> None:
        proc = self.processes[key]
        if proc.state == "running":
            return
        proc._stopping = False
        proc._proc = await asyncio.create_subprocess_exec(
            *proc.argv, cwd=config.ROOT, env=self._env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        proc.state, proc.exit_code, proc.started_at = "running", None, time.time()
        proc.log(f"▶ started (pid {proc._proc.pid})")
        proc._watch = asyncio.create_task(self._watch(proc, proc._proc))

    async def kill(self, key: str) -> None:
        """SIGKILL, as if the machine died. Counts as a crash, so auto-restart applies."""
        proc = self.processes[key]
        if proc._proc and proc._proc.returncode is None:
            proc.log("✖ SIGKILL from the presenter")
            proc._proc.send_signal(signal.SIGKILL)

    async def stop(self, key: str) -> None:
        """Stop on purpose. Not a crash: auto-restart does not apply."""
        proc = self.processes[key]
        proc._stopping = True
        if proc._proc and proc._proc.returncode is None:
            proc._proc.send_signal(signal.SIGKILL)
            await proc._proc.wait()
        if proc._watch:
            await asyncio.gather(proc._watch, return_exceptions=True)
        proc.state = "stopped"

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop(k) for k in self.processes))

    async def _watch(self, proc: ManagedProcess, child: asyncio.subprocess.Process) -> None:
        assert child.stdout
        async for raw in child.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line and not _noise(line):
                proc.log(line)
        code = await child.wait()
        if proc._proc is not child:
            return
        proc.exit_code = code
        if proc._stopping:
            proc.state = "stopped"
            proc.log("■ stopped")
            return
        proc.state = "crashed"
        proc.log(f"✖ process died (exit {code})")
        if self.auto_restart:
            proc.state = "restarting"
            proc.log(f"↻ supervisor restarts it in {RESTART_DELAY:.0f}s (like Kubernetes would)")
            await asyncio.sleep(RESTART_DELAY)
            if proc.state == "restarting" and not proc._stopping:
                proc.restarts += 1
                await self.start(proc.key)


_NOISE = ("Warning", "warn(", "UserWarning", "resumability_config=", "token usage metadata", "experimental",
          "app = App(", "EXPERIMENTAL")


def _noise(line: str) -> bool:
    # Tracebacks: keep only their last line, the exception and its message.
    if line.startswith((" ", "\t", "Traceback ", "The above exception", "During handling")) or set(line) <= set("^~ "):
        return True
    return any(n in line for n in _NOISE)
