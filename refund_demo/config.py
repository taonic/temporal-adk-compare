"""Ports, paths and timings shared by every process in the demo.

Every value can be overridden with an environment variable of the same name,
so tests can run a private copy of the stack on other ports.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAR = Path(os.environ.get("REFUND_DEMO_VAR", ROOT / "var"))

API_PORT = int(os.environ.get("API_PORT", 8400))
# The UI and API listen here. 0.0.0.0 in a container; loopback on a laptop.
API_HOST = os.environ.get("API_HOST", "127.0.0.1")
BUSINESS_PORT = int(os.environ.get("BUSINESS_PORT", 8420))
# One port per ADK host instance: host "a" -> 8411, host "b" -> 8412.
ADK_HOST_PORTS = {"a": int(os.environ.get("ADK_HOST_A_PORT", 8411)),
                  "b": int(os.environ.get("ADK_HOST_B_PORT", 8412))}
# Each Temporal Worker also listens here, so the API can ask Worker A itself to start a run: an
# eager start, whose first Workflow Task goes straight to the Worker that started it.
WORKER_PORTS = {"a": int(os.environ.get("WORKER_A_PORT", 8431)),
                "b": int(os.environ.get("WORKER_B_PORT", 8432))}

BUSINESS_URL = os.environ.get("BUSINESS_URL", f"http://127.0.0.1:{BUSINESS_PORT}")

TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
TEMPORAL_UI = os.environ.get("TEMPORAL_UI", "http://localhost:8233")
# Where the dev server the API starts binds its Web UI. Set to 0.0.0.0 in a container to expose it.
TEMPORAL_UI_IP = os.environ.get("TEMPORAL_UI_IP", "")
TASK_QUEUE = os.environ.get("TASK_QUEUE", "refunds")

ADK_SESSIONS_DB = VAR / "adk_sessions.db"
BUSINESS_DB = VAR / "business.db"
FAULTS_DIR = VAR / "faults"

# The model behind the agent. "demo-refund-model" is the deterministic fake in
# fake_llm.py; set REFUND_DEMO_MODEL=gemini-2.5-flash (and GOOGLE_API_KEY) for
# a real model.
MODEL = os.environ.get("REFUND_DEMO_MODEL", "demo-refund-model")
# Seconds the fake model "thinks" per call, so the UI can show each step.
FAKE_LLM_DELAY = float(os.environ.get("FAKE_LLM_DELAY", 0.8))
# Seconds each business-system call takes, for the same reason.
BUSINESS_DELAY = float(os.environ.get("BUSINESS_DELAY", 0.5))

APP_NAME = "refunds"
