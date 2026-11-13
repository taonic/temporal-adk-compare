"""Driving the scenario player as a presenter does, for the browser tests."""

import json
from pathlib import Path

SCENARIOS = json.loads((Path(__file__).resolve().parents[2] / "refund_demo/static/scenarios.json").read_text())["acts"]

NEXT = "#player .replies:not([hidden]) .reply.is-primary:not([disabled])"


def open_scenario(page, index: int) -> None:
    """Its tab (which also cleans the stage), then its intro, typed out."""
    page.locator("#player .acts button").nth(index).click()
    page.wait_for_selector(NEXT, timeout=60_000)  # the reset done and the intro typed out


def finish_typing(page) -> None:
    bubble = page.locator("#player .guide-bubble.is-streaming")
    if bubble.count():
        bubble.first.click()


def next_step(page, timeout: float = 120_000) -> None:
    """Next, once it can be pressed: the message has typed out and the step's outcome arrived."""
    finish_typing(page)
    page.locator(NEXT).click(timeout=timeout)


def step_count(page) -> str:
    return page.locator("#player .segs .count").inner_text()


def play_to_the_end(page, index: int) -> None:
    """Start the scenario and press Next through every step, then wait for the last one's outcome."""
    steps = len(SCENARIOS[index]["steps"])
    open_scenario(page, index)
    for _ in range(steps):
        next_step(page)
    finish_typing(page)
    assert step_count(page) == f"{steps}/{steps}"
    page.wait_for_selector("#player .waiting", state="detached", timeout=120_000)


def state(page, base: str) -> dict:
    return page.request.get(f"{base}/api/state").json()


def side_effects(st: dict, side: str, replayed: bool = False) -> int:
    return sum(1 for r in st["business"]["refunds"] if r["side"] == side and bool(r["replayed"]) == replayed)
