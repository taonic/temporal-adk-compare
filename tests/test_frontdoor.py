"""The front door starts a sandbox only for a person's browser, never for a crawler.

    uv run --group frontdoor pytest tests/test_frontdoor.py

No Daytona: the sandbox manager is replaced by one that only counts what it was asked to start.
"""

import pytest

pytest.importorskip("daytona", reason="the front door needs the frontdoor group (uv run --group frontdoor)")

from fastapi.testclient import TestClient  # noqa: E402

from frontdoor import app as frontdoor  # noqa: E402
from frontdoor.sandboxes import Progress  # noqa: E402

BROWSER = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


class CountingManager:
    def __init__(self):
        self.started: list[str] = []

    def status(self, visitor):
        return Progress("finding", "Looking for your stage…") if visitor in self.started else Progress("none", "No stage yet.")

    def ensure(self, visitor):
        if visitor not in self.started:
            self.started.append(visitor)
        return self.status(visitor)


@pytest.fixture
def door(monkeypatch):
    manager = CountingManager()
    monkeypatch.setattr(frontdoor, "manager", manager)
    monkeypatch.setattr(frontdoor, "starts_by_ip", frontdoor.defaultdict(frontdoor.deque))
    client = TestClient(frontdoor.app, headers={"user-agent": BROWSER, "accept": "text/html"})
    return client, manager


def start(client, **headers):
    return client.post("/_stage/start", headers={"x-stage-start": "1", "accept": "application/json", **headers})


def test_a_browser_gets_the_waiting_page_and_its_script_starts_the_stage(door):
    client, manager = door
    page = client.get("/")
    assert "Your own stage" in page.text and "stage" in page.cookies
    assert manager.started == [], "loading the page started a sandbox"
    assert start(client).status_code == 200
    assert len(manager.started) == 1


def test_page_loads_without_cookies_start_nothing(door):
    client, manager = door
    for path in ("/", "/wp-login.php", "/api/state", "/robots.txt"):
        client.cookies.clear()
        client.get(path)
    assert manager.started == []


def test_a_start_without_the_pages_cookie_or_header_is_refused(door):
    client, manager = door
    assert start(client).status_code == 403, "no cookie: not from the waiting page"
    client.get("/")
    assert client.post("/_stage/start").status_code == 403, "no header: not the waiting page's script"
    assert manager.started == []


@pytest.mark.parametrize("agent", ["Googlebot/2.1 (+http://www.google.com/bot.html)", "curl/8.4.0", "",
                                   "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 HeadlessChrome/131.0.0.0 Safari/537.36"])
def test_bots_are_refused(door, agent):
    client, manager = door
    client.get("/")
    assert start(client, **{"user-agent": agent}).status_code == 403
    assert manager.started == []


def test_starts_per_ip_are_capped(door, monkeypatch):
    client, manager = door
    monkeypatch.setattr(frontdoor, "STARTS_PER_IP_PER_HOUR", 2)
    codes = []
    for _ in range(3):
        client.cookies.clear()
        client.get("/")
        codes.append(start(client).status_code)
    assert codes == [200, 200, 403] and len(manager.started) == 2


def test_robots_keep_crawlers_off_the_start(door):
    client, _ = door
    assert "Disallow: /_stage/" in client.get("/robots.txt").text
