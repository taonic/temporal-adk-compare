"""Temporal side: a Worker. Start as many as you like; any of them can continue any refund.

A Worker holds no state of its own. It polls the task queue, and when it
picks up a Workflow it replays that Workflow's event history to rebuild where
it was. That is why a crashed refund continues with no one asking: the next
Worker to poll the queue picks it up.

Where a run starts, and stays, is up to Temporal's task routing, not to luck:
    - the API asks this Worker (POST /start) to start the Workflow with an eager start: the Server
      hands its first Workflow Task straight back to the Worker that started it
    - the Worker caches the Workflow, so its next Workflow Tasks go to this Worker's sticky queue
    - each Activity it schedules on its own task queue is executed eagerly, by this Worker
So a refund started on Worker A runs on Worker A, with Worker B idle beside it, until Worker A dies;
then its tasks go back to the shared queue and Worker B takes them.
"""

import argparse
import asyncio
from datetime import timedelta

from typing import Any

import uvicorn
from fastapi import FastAPI
from temporalio import activity
from temporalio.client import Client
from temporalio.google_adk import GoogleAdkPlugin
from temporalio.worker import (ActivityInboundInterceptor, ExecuteActivityInput, Interceptor, Worker)
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from .. import config
from . import activities
from .workflow import RefundRequest, RefundWorkflow


class _LogActivities(Interceptor):
    """Prints each Activity attempt, for the demo's log pane."""

    def __init__(self, name: str) -> None:
        self._name = name

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        name = self._name

        class Inbound(ActivityInboundInterceptor):
            async def execute_activity(self, input: ExecuteActivityInput) -> Any:
                info = activity.info()
                print(f"[worker {name}] {info.activity_type} attempt {info.attempt} started", flush=True)
                result = await super().execute_activity(input)
                print(f"[worker {name}] {info.activity_type} completed; result recorded in history", flush=True)
                return result

        return Inbound(next)


async def main(name: str) -> None:
    client = await Client.connect(config.TEMPORAL_ADDRESS, namespace=config.TEMPORAL_NAMESPACE,
                                  plugins=[GoogleAdkPlugin()])
    worker = Worker(
        client,
        task_queue=config.TASK_QUEUE,
        identity=f"worker-{name}",
        workflows=[RefundWorkflow],
        activities=[activities.refund_payment, activities.record_refund, activities.notify_customer,
                    activities.escalate_to_manager],
        interceptors=[_LogActivities(name)],
        # A Worker caches the Workflows it runs and gets their next tasks on its
        # own queue. If it dies, the Server moves those tasks back to the shared
        # queue after this timeout (default 10s); shorter for the demo.
        sticky_queue_schedule_to_start_timeout=timedelta(seconds=2),
        # The agent definition and its tools are shared with the ADK side and
        # hold no workflow state, so the sandbox can import them once.
        workflow_runner=SandboxedWorkflowRunner(restrictions=SandboxRestrictions.default.with_passthrough_modules(
            "refund_demo.config", "refund_demo.agent", "refund_demo.tools", "refund_demo.faults",
            "refund_demo.fake_llm", "refund_demo.temporal_side.activities", "httpx")),
    )
    print(f"[worker {name}] polling task queue '{config.TASK_QUEUE}'", flush=True)
    server = uvicorn.Server(uvicorn.Config(_control(client, name), host="127.0.0.1",
                                           port=config.WORKER_PORTS[name], log_level="warning"))
    # Either one ending ends the process: the supervisor restarts it whole, as a crash.
    done, pending = await asyncio.wait([asyncio.create_task(worker.run()), asyncio.create_task(server.serve())],
                                       return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        task.result()


def _control(client: Client, name: str) -> FastAPI:
    """POST /start: start a refund's Workflow here, eagerly, so this Worker runs it."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/start")
    async def start(body: dict) -> dict:
        handle = await client.start_workflow(
            RefundWorkflow.run, RefundRequest(**body["request"]), id=body["workflow_id"],
            task_queue=config.TASK_QUEUE, task_timeout=timedelta(seconds=4),
            # Same client as this Worker: the Server returns the first Workflow Task in the response.
            request_eager_start=True)
        eager = bool(getattr(handle, "__temporal_eagerly_started", False))
        print(f"[worker {name}] started {body['workflow_id']}"
              + (" eagerly: its first Workflow Task came straight back to me" if eager
                 else " (not eagerly: any Worker on the queue may take it)"), flush=True)
        return {"eager": eager}

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="a")
    asyncio.run(main(parser.parse_args().name))
