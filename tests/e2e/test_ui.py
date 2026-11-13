"""The page, driven as a presenter drives it, against a private copy of the demo.

Every scenario is played through the player to its end, and its outcome checked in the ledger;
then the player's rules: Next waits for the step's outcome, a new scenario starts on a clean stage,
the page has one call to action, and the code links open the code.
"""

import pytest

from player import NEXT, SCENARIOS, finish_typing, next_step, open_scenario, play_to_the_end, side_effects, state, step_count

BASE = "http://127.0.0.1:18300"


@pytest.fixture
def page(api, browser, page_errors):
    """A presenter's window on the demo (the `api` fixture runs it), at a laptop's size."""
    context = browser.new_context(viewport={"width": 1490, "height": 811}, color_scheme="dark")
    page = context.new_page()
    page.on("pageerror", lambda err: page_errors.append(str(err)))
    page.goto(BASE)
    page.wait_for_selector("#player .acts button")
    yield page
    context.close()
    assert not page_errors, f"errors on the page: {page_errors}"


@pytest.mark.parametrize("index", range(4), ids=[s["short"] for s in SCENARIOS[:4]])
def test_every_scenario_plays_to_its_end(page, index):
    play_to_the_end(page, index)
    st = state(page, BASE)
    assert st["adk"]["status"] == "completed" and st["temporal"]["status"] == "COMPLETED"
    expected = {
        # Scenario 1: the refund was done before the crash; recovery and the other Worker each finish it once.
        0: {"adk": (1, 0), "temporal": (1, 0)},
        # Scenario 2: retries; one refund each once the model is back.
        1: {"adk": (1, 0), "temporal": (1, 0)},
        # Scenario 3 ends on its idempotency-key run: one refund and one deduplicated retry on each side.
        2: {"adk": (1, 1), "temporal": (1, 1)},
        # Scenario 4: ADK's late approval refunds; Temporal's deadline fired, so it escalated and refunded nothing.
        3: {"adk": (1, 0), "temporal": (0, 0)},
    }[index]
    for side, (moved, replayed) in expected.items():
        assert (side_effects(st, side), side_effects(st, side, replayed=True)) == (moved, replayed), side
    if index == 3:
        assert sum(1 for e in st["business"]["escalations"] if e["side"] == "temporal") == 1
    # The end of a scenario offers the next one.
    assert "On to" in page.locator("#player .reply.is-primary").inner_text()


def test_next_waits_for_the_steps_outcome(page):
    open_scenario(page, 0)
    next_step(page)                 # set the stage
    next_step(page)                 # ADK refunds and host A dies: wait for the invocation to stop
    finish_typing(page)
    page.wait_for_selector("#player .waiting")
    assert page.locator("#player .reply.is-primary").is_disabled()
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    assert step_count(page) == "2/5", "→ moved on before the step's outcome arrived"
    page.wait_for_selector(NEXT, timeout=60_000)
    assert page.locator("#player .waiting").count() == 0


def test_a_new_scenario_starts_on_a_clean_stage(page):
    open_scenario(page, 0)
    for _ in range(2):
        next_step(page)
    page.wait_for_selector(NEXT, timeout=60_000)
    assert state(page, BASE)["run"], "Scenario 1 started a run"
    open_scenario(page, 1)          # its tab resets the stage
    st = state(page, BASE)
    assert st["run"] is None and not st["business"]["refunds"]
    assert {p["key"]: p["state"] for p in st["processes"] if p["side"] != "system"} == \
        {"adk-a": "running", "adk-b": "stopped", "worker-a": "running", "worker-b": "stopped"}
    assert page.locator(".flow .node.dup, .flow .node.limbo, .flow .node.done").count() == 0, "a side effect shown with no run"


def test_one_call_to_action(page):
    """Next is the only filled button on the page, even when the ADK column needs recovering."""
    open_scenario(page, 0)
    for _ in range(2):
        next_step(page)
    page.wait_for_selector(NEXT, timeout=60_000)  # ADK is stranded: its recovery button is relevant now
    filled = page.evaluate("""() => {
        const accent = getComputedStyle(document.querySelector('#player .reply.is-primary')).backgroundColor;
        return [...document.querySelectorAll('button, a')].filter((e) => e.offsetParent && getComputedStyle(e).backgroundColor === accent)
          .map((e) => e.textContent.trim()); }""")
    assert filled == ["Next →"], filled


def test_code_links_open_the_code(page):
    open_scenario(page, 0)
    next_step(page)
    finish_typing(page)
    page.locator("#player a[data-code='adk/agent']").click()
    page.wait_for_selector("dialog[open] .part-title code")
    assert page.locator("dialog .part-title code").all_inner_texts()[:2] == ["build_agent", "make_runner"]
    page.locator("dialog .head button").click()
    # The recovery view shows where ADK resumes: drive's runner.run_async, highlighted.
    page.locator("#side-adk .panel.clickable").click()
    page.wait_for_selector("dialog[open] pre.code .cl.hl")
    assert "runner.run_async" in page.locator("dialog pre.code .cl.hl").inner_text()


def test_the_columns_fit_one_screen(page):
    """The processes, the flow and the panels of both columns, without scrolling, at 1490x811."""
    open_scenario(page, 0)
    bottom = page.evaluate("Math.max(...[...document.querySelectorAll('.side')].map((e) => e.getBoundingClientRect().bottom))")
    assert bottom <= 811, f"the columns end at {bottom}px"


def test_code_in_a_caption_opens_that_code(page):
    """A caption's code opens where it is: the wrap-up's `retry_options` highlights the model's retry options."""
    open_scenario(page, 4)
    next_step(page)
    finish_typing(page)
    page.locator("#player .msg-slot a[data-hl='retry_options=MODEL_RETRY']").first.click()
    page.wait_for_selector("dialog[open] pre.code .cl.hl")
    assert all("retry_options=MODEL_RETRY" in line for line in page.locator("dialog pre.code .cl.hl").all_inner_texts())
