"""Recovery for the ADK side: the code you write yourself.

ADK can resume an interrupted invocation, but only when something calls
`runner.run_async(..., invocation_id=...)`. After a host dies, nothing in ADK
does that. A new host starts idle, and other hosts don't know the work exists.

This module is the smallest version of what you would build to close that
gap: scan the session store for invocations that started but never finished.
A production version would also need:

- a scheduler or sweeper that runs this scan, and decides how soon a quiet
  invocation counts as dead, not just slow;
- a lease, so two hosts never resume the same invocation at once;
- a deadline per invocation (an approval that times out, say), and something
  that wakes the invocation when the deadline passes;
- a way to cope with code changes between the crash and the resume.
"""

from typing import Any

from google.adk.events import Event
from google.adk.sessions import BaseSessionService


def invocation_status(events: list[Event]) -> str:
    """'completed', 'awaiting_approval', 'unfinished' or 'not_started' for the session's latest invocation."""
    if not events:
        return "not_started"
    latest = events[-1].invocation_id
    current = [e for e in events if e.invocation_id == latest]
    if any(e.actions.end_of_agent for e in current):
        return "completed"
    last = current[-1]
    if last.long_running_tool_ids and any(
        p.function_call and p.function_call.name == "adk_request_confirmation"
        for p in (last.content.parts if last.content else []) or []
    ):
        return "awaiting_approval"
    return "unfinished"


async def find_unfinished_invocations(session_service: BaseSessionService, app_name: str) -> list[dict[str, Any]]:
    """Every session whose latest invocation started but did not finish or pause."""
    found = []
    listing = await session_service.list_sessions(app_name=app_name)
    for summary in listing.sessions:
        session = await session_service.get_session(
            app_name=app_name, user_id=summary.user_id, session_id=summary.id)
        if session and invocation_status(session.events) == "unfinished":
            found.append({
                "customer_id": session.user_id,
                "session_id": session.id,
                "invocation_id": session.events[-1].invocation_id,
                "last_event_at": session.events[-1].timestamp,
            })
    return found
