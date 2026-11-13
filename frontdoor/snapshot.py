"""The visitors' sandbox snapshot on Daytona.

    uv run --group frontdoor python -m frontdoor.snapshot           # build it (needs DAYTONA_API_KEY)
    uv run --group frontdoor python -m frontdoor.snapshot --name    # print the name the front door uses
    uv run --group frontdoor python -m frontdoor.snapshot --force   # rebuild it

The name carries a hash of the snapshot's inputs, so a deploy whose dependencies changed asks
for a snapshot that doesn't exist yet, and says so, instead of running visitors on stale
dependencies. Adapted from temporal-agent-governance-workshop's course/snapshot/build.mjs.
"""

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Per-sandbox resources (a snapshot fixes them for every sandbox made from it): a Temporal dev
# server and five Python processes (API, business systems, an ADK host, a Worker, and a second
# of each in Scenario 1). The same as the single Fly machine that ran the whole demo before.
# They're part of the snapshot's name, so changing them builds a new snapshot.
RESOURCES = {"cpu": 2, "memory": 2, "disk": 5}

# What the snapshot depends on: its name is a hash of these, of RESOURCES, and of the packages
# the sandbox installs (the demo's locked dependencies only: a change to the dev or front door
# groups changes uv.lock, but not what a sandbox runs).
INPUTS = ["frontdoor/sandbox.Dockerfile", ".python-version"]


def installed() -> bytes:
    return subprocess.run(["uv", "export", "--frozen", "--no-default-groups", "--no-emit-project", "--no-hashes",
                           "--no-header", "--no-annotate"], cwd=ROOT, check=True, capture_output=True).stdout


def name() -> str:
    digest = hashlib.sha256()
    for rel in INPUTS:
        digest.update(f"{rel}\0".encode())
        digest.update((ROOT / rel).read_bytes())
    digest.update(installed())
    digest.update(repr(sorted(RESOURCES.items())).encode())
    return f"adk-compare-{digest.hexdigest()[:12]}"


def _stage() -> Path:
    """The build context: the Dockerfile and the three files it copies, nothing else."""
    context = Path(tempfile.mkdtemp(prefix="adk-compare-snapshot-"))
    shutil.copy(ROOT / "frontdoor/sandbox.Dockerfile", context / "Dockerfile")
    for rel in ("pyproject.toml", "uv.lock", ".python-version"):
        shutil.copy(ROOT / rel, context / rel)
    return context


def build(force: bool = False) -> str:
    from daytona import CreateSnapshotParams, Daytona, DaytonaError, Image, Resources

    snapshot = name()
    daytona = Daytona()
    try:
        existing = daytona.snapshot.get(snapshot)
        if not force:
            print(f"✓ Snapshot {snapshot} already exists ({existing.state}). --force rebuilds it.")
            return snapshot
        print(f"Deleting the existing {snapshot}…")
        daytona.snapshot.delete(existing)
    except DaytonaError as err:
        if "not found" not in str(err).lower():
            raise
    context = _stage()
    print(f"Building snapshot {snapshot} on Daytona from {context}…")
    daytona.snapshot.create(
        CreateSnapshotParams(name=snapshot, image=Image.from_dockerfile(context / "Dockerfile"),
                             resources=Resources(**RESOURCES), entrypoint=["sleep", "infinity"]),
        on_logs=lambda chunk: print(chunk, end="" if chunk.endswith("\n") else "\n", flush=True),
        timeout=0,
    )
    print(f"\n✓ Snapshot {snapshot} is ready.")
    return snapshot


if __name__ == "__main__":
    if "--name" in sys.argv:
        print(name())
    else:
        build(force="--force" in sys.argv)
