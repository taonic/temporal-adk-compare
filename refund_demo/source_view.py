"""The code behind each node of the UI's diagrams, colour-coded for display.

Read from the running modules with `inspect`, so the viewer always shows the
code that is actually executing. Code that `inspect` cannot fetch as one
object (a module-level statement, a route inside a factory) is a Snippet: the
lines from one marker to the next.
"""

import builtins
import inspect
import io
import keyword
import textwrap
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Union

from . import agent, faults, fake_llm, tools
from .adk_side import host, recovery
from .temporal_side import activities, worker
from .temporal_side.workflow import RefundWorkflow

PACKAGE = Path(__file__).parent


@dataclass
class Snippet:
    """Lines of `path` from the first starting with `first` through the first after it starting with `last`."""

    name: str
    path: str
    first: str
    last: str


Part = Union[Callable[..., Any], Snippet]

ADK_RUN_ROUTE = Snippet("POST /runs", "adk_side/host.py", '    @app.post("/runs")', "        return")
ADK_CONFIRM_ROUTE = Snippet("POST /confirm", "adk_side/host.py", '    @app.post("/confirm")', "        return")
ADK_RESUME_ROUTE = Snippet("POST /resume", "adk_side/host.py", '    @app.post("/resume")', "        return")
ADK_RECOVER_ROUTE = Snippet("POST /recover (each host runs the scan)", "adk_side/host.py", '    @app.post("/recover")', "        return")
ADK_DRIVE = Snippet("drive: where runner.run_async runs (a new run, or a resume with invocation_id)", "adk_side/host.py",
                    "    async def drive(", "        await start_deadline_timer(")
ADK_DEADLINE_TIMER = Snippet("start_deadline_timer (an asyncio task in this host)", "adk_side/host.py",
                             "    async def start_deadline_timer(", "        task.add_done_callback(tasks.discard)")
ADK_MODEL_RETRY = Snippet("MODEL_RETRY (in-process model retries)", "adk_side/host.py", "MODEL_RETRY = ", "                                     http_status_codes")
TEMPORAL_AGENT = Snippet("AGENT (model and tools as Activities)", "temporal_side/workflow.py", "AGENT = build_agent(", ")")
TOOL_OPTIONS = Snippet("TOOL_OPTIONS (timeouts and retries)", "temporal_side/workflow.py", "TOOL_OPTIONS = ", ")")
TEMPORAL_START = Snippet("start the Workflow", "api.py", "    temporal_call = demo.client.start_workflow(",
                         "        task_timeout=")

SOURCES: dict[str, dict[str, list[Part]]] = {
    "adk": {
        "request": [ADK_RUN_ROUTE, host.request_message],
        "decide": [ADK_MODEL_RETRY, host.make_model, fake_llm.FakeRefundLlm.generate_content_async],
        "approval": [agent.needs_manager_approval, ADK_DEADLINE_TIMER, ADK_CONFIRM_ROUTE],
        "refund_payment": [tools.refund_payment],
        "record_refund": [tools.record_refund],
        "notify_customer": [tools.notify_customer],
        "agent": [agent.build_agent, host.make_runner],
        "runtime": [host.make_runner, ADK_RUN_ROUTE, ADK_RESUME_ROUTE, ADK_DRIVE],
        "persistence": [host.make_runner, recovery.invocation_status],
        "recovery": [recovery.find_unfinished_invocations, recovery.invocation_status, ADK_RECOVER_ROUTE, ADK_DRIVE],
        "crash": [faults.maybe_crash, faults._die, agent._after_tool, agent._before_tool],
    },
    "temporal": {
        "request": [TEMPORAL_START, RefundWorkflow.run],
        "decide": [TEMPORAL_AGENT, fake_llm.FakeRefundLlm.generate_content_async],
        "approval": [agent.needs_manager_approval, RefundWorkflow._await_manager, RefundWorkflow.decide],
        "refund_payment": [tools.refund_payment, activities.refund_payment, TEMPORAL_AGENT, TOOL_OPTIONS],
        "record_refund": [tools.record_refund, activities.record_refund, TEMPORAL_AGENT],
        "notify_customer": [tools.notify_customer, activities.notify_customer, TEMPORAL_AGENT],
        "agent": [agent.build_agent, TEMPORAL_AGENT],
        "runtime": [RefundWorkflow.run, worker.main],
        "persistence": [RefundWorkflow.run],
        "recovery": [worker.main],
        "crash": [faults.maybe_crash, faults._die, agent._after_tool, agent._before_tool],
    },
}


def node_source(side: str, node: str) -> list[dict[str, Any]]:
    """Each piece of code behind `node` on `side`: its name, file, first line and highlighted code."""
    parts = []
    for item in SOURCES[side][node]:
        if isinstance(item, Snippet):
            name, file = item.name, item.path
            lines, first_line = _snippet_lines(item)
        else:
            target = inspect.unwrap(item)
            name = target.__qualname__
            file = str(Path(inspect.getsourcefile(target)).relative_to(PACKAGE))
            lines, first_line = inspect.getsourcelines(target)
        code = textwrap.dedent("".join(lines))
        parts.append({"name": name, "file": file, "line": first_line, "runs": highlight(code)})
    return parts


def _snippet_lines(snippet: Snippet) -> tuple[list[str], int]:
    lines = (PACKAGE / snippet.path).read_text().splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith(snippet.first))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith(snippet.last))
    return lines[start:end + 1], start + 1


_BUILTINS = set(dir(builtins))
_STRING_TYPES = {tokenize.STRING} | {getattr(tokenize, n) for n in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
                                     if hasattr(tokenize, n)}


def highlight(code: str) -> list[list[str]]:
    """[kind, text] runs covering `code` exactly. Kinds: kw, str, num, com, def, deco, bi, or ''."""
    runs: list[list[str]] = []
    lines = code.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    pos = 0
    prev = None
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return [["", code]]
    for tok in tokens:
        if tok.type == tokenize.ENDMARKER:
            break
        start = offsets[tok.start[0] - 1] + tok.start[1]
        end = offsets[tok.end[0] - 1] + tok.end[1]
        if start > pos:
            runs.append(["", code[pos:start]])
        kind = ""
        if tok.type == tokenize.NAME:
            if keyword.iskeyword(tok.string) or tok.string in ("self", "cls"):
                kind = "kw"
            elif prev is not None and prev.string in ("def", "class"):
                kind = "def"
            elif prev is not None and prev.string == "@":
                kind = "deco"
            elif tok.string in _BUILTINS:
                kind = "bi"
        elif tok.type in _STRING_TYPES:
            kind = "str"
        elif tok.type == tokenize.NUMBER:
            kind = "num"
        elif tok.type == tokenize.COMMENT:
            kind = "com"
        elif tok.type == tokenize.OP and tok.string == "@":
            kind = "deco"
        if end > start:
            runs.append([kind, code[start:end]])
        pos = max(pos, end)
        if tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT):
            prev = tok
    if pos < len(code):
        runs.append(["", code[pos:]])
    return runs
