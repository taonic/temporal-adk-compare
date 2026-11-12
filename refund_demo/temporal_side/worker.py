"""Temporal side: a Worker. Start as many as you like; any of them can continue any refund.

A Worker holds no state of its own. It polls the task queue, and when it
picks up a Workflow it replays that Workflow's event history to rebuild where
it was. That is why a crashed refund continues with no one asking: the next
Worker to poll the queue picks it up.
"""

import argparse
import asyncio
from datetime import timedelta

from typing import Any

from temporalio import activity
from temporalio.client import Client
from temporalio.google_adk import GoogleAdkPlugin
from temporalio.worker import (ActivityInboundInterceptor, ExecuteActivityInput, Interceptor, Worker)
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from .. import config
from . import activities
from .workflow import RefundWorkflow


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
        # own queue. If it dies, the Service moves those tasks back to the shared
        # queue after this timeout (default 10s); shorter for the demo.
        sticky_queue_schedule_to_start_timeout=timedelta(seconds=2),
        # The agent definition and its tools are shared with the ADK side and
        # hold no workflow state, so the sandbox can import them once.
        workflow_runner=SandboxedWorkflowRunner(restrictions=SandboxRestrictions.default.with_passthrough_modules(
            "refund_demo.config", "refund_demo.agent", "refund_demo.tools", "refund_demo.faults",
            "refund_demo.fake_llm", "refund_demo.temporal_side.activities", "httpx")),
    )
    print(f"[worker {name}] polling task queue '{config.TASK_QUEUE}'", flush=True)
    await worker.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="a")
    asyncio.run(main(parser.parse_args().name))
