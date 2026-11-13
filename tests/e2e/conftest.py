"""Browser tests: the page itself, driven as a presenter drives it.

Two kinds:
    test_ui.py    the player, against a private copy of the demo (the `api` fixture in
                  tests/conftest.py). In CI before every deploy.
    test_live.py  a deployed front door (E2E_BASE_URL): a visitor's own stage on Daytona.
                  After every deploy, daily, and by hand (.github/workflows/e2e.yml).

Needs the e2e group and a browser:
    uv sync --group e2e && uv run playwright install chromium
E2E_BROWSER_CHANNEL=chrome uses an installed Chrome instead of Playwright's Chromium.
"""

import os

import pytest

playwright = pytest.importorskip("playwright.sync_api", reason="browser tests need the e2e group (uv sync --group e2e)")


@pytest.fixture(scope="session")
def browser():
    with playwright.sync_playwright() as p:
        channel = os.environ.get("E2E_BROWSER_CHANNEL") or None
        chromium = p.chromium.launch(channel=channel)
        yield chromium
        chromium.close()


@pytest.fixture
def page_errors():
    """Uncaught errors on any page a test opens; a test fails on them."""
    return []
