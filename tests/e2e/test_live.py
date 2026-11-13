"""The deployed demo, as visitors see it: each gets a stage of their own, on Daytona.

    E2E_BASE_URL=https://temporal-adk-compare.fly.dev uv run --group e2e pytest tests/e2e/test_live.py

Skips without E2E_BASE_URL. The visitors here set their own ids (the front door takes any), so
the sandboxes they started can be deleted afterwards (with DAYTONA_API_KEY; else they stop when
idle and are deleted a day later, like anyone's).
"""

import os
import time

import pytest

from player import next_step, open_scenario, play_to_the_end, state

BASE = (os.environ.get("E2E_BASE_URL") or "").rstrip("/")
RUN = os.environ.get("GITHUB_RUN_ID") or str(int(time.time()))
BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/131.0.0.0 Safari/537.36")
pytestmark = pytest.mark.skipif(not BASE, reason="E2E_BASE_URL is not set: no deployed demo to test")


@pytest.fixture(scope="module")
def visitors():
    """The visitor ids this module uses; their sandboxes are deleted when it's done."""
    ids = []
    yield ids
    if ids and os.environ.get("DAYTONA_API_KEY"):
        _delete_sandboxes(ids)


def _delete_sandboxes(ids: list[str]) -> None:
    # The synchronous client: Playwright's sync API keeps an event loop running in this thread.
    from daytona import Daytona, ListSandboxesQuery

    daytona = Daytona()
    for visitor in ids:
        for sandbox in daytona.list(ListSandboxesQuery(labels={"app": "adk-compare", "visitor": visitor})):
            sandbox.delete()


def visit(browser, visitors, name: str, page_errors):
    visitor = f"e2e-{RUN}-{name}"
    visitors.append(visitor)
    # A desktop Chrome's user agent: the front door turns away headless browsers, as it does bots.
    context = browser.new_context(viewport={"width": 1490, "height": 811}, color_scheme="dark", user_agent=BROWSER)
    context.add_cookies([{"name": "stage", "value": visitor, "url": BASE}])
    page = context.new_page()
    page.on("pageerror", lambda err: page_errors.append(str(err)))
    return context, page


def test_a_visitor_gets_a_stage_of_their_own_and_plays_a_scenario(browser, visitors, page_errors):
    context, page = visit(browser, visitors, "a", page_errors)
    started = time.time()
    page.goto(BASE)
    # The waiting page first (a new visitor has no stage yet), then the demo.
    page.wait_for_selector("text=Your own stage", timeout=30_000)
    page.wait_for_selector("#player .acts button", timeout=240_000)
    print(f"stage ready after {time.time() - started:.0f}s")
    st = state(page, BASE)
    assert st["run"] is None, "a new stage starts clean"

    play_to_the_end(page, 0)
    st = state(page, BASE)
    assert st["adk"]["status"] == "completed" and st["temporal"]["status"] == "COMPLETED"
    assert sum(1 for r in st["business"]["refunds"] if not r["replayed"]) == 2, "one refund on each side"
    context.close()
    assert not page_errors, page_errors


def test_stages_are_isolated(browser, visitors, page_errors):
    """One visitor's crash doesn't reach another's stage."""
    a_context, a = visit(browser, visitors, "b", page_errors)
    b_context, b = visit(browser, visitors, "c", page_errors)
    for page in (a, b):
        page.goto(BASE)
    for page in (a, b):
        page.wait_for_selector("#player .acts button", timeout=240_000)
    open_scenario(a, 0)
    for _ in range(2):
        next_step(a)                # set the stage, then ADK host A dies
    a.wait_for_selector("#player .waiting", state="detached", timeout=120_000)
    a_state, b_state = state(a, BASE), state(b, BASE)
    adk_a = lambda st: next(p["state"] for p in st["processes"] if p["key"] == "adk-a")  # noqa: E731
    assert a_state["run"] and adk_a(a_state) == "crashed"
    assert b_state["run"] is None and adk_a(b_state) == "running", "the other visitor's stage was touched"
    a_context.close()
    b_context.close()
    assert not page_errors, page_errors
