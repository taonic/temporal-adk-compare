"""The front door: every visitor gets a stage of their own, a Daytona sandbox running the demo.

    the demo         every path, on PORT: proxied to the visitor's sandbox, port 8400
    the Temporal UI  every path, on UI_PORT: proxied to the sandbox's port 8233 (the UI uses
                     root-absolute paths, so it needs an origin of its own; same host, so the
                     visitor's cookie reaches it)

Until the visitor's stage is ready, the demo's paths answer with a waiting page that starts it
and shows its progress. A sandbox that stopped while idle is noticed by the proxy, and the page
goes back to waiting while it wakes. Adapted from temporal-agent-governance-workshop's
course/runner/proxy.mjs and server.mjs.

A stage costs a sandbox, so only a person's browser starts one, never a crawler:
    - loading a page starts nothing; the waiting page's script asks for the stage (POST /_stage/start)
    - that request needs the cookie the page set and a header only the page's script sends
    - known bots and headless browsers are turned away, and robots.txt keeps crawlers off /_stage/
    - each IP starts at most STARTS_PER_IP_PER_HOUR stages
"""

import os
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from .sandboxes import DEMO_PORT, TEMPORAL_UI_PORT, SandboxManager, Settings

COOKIE = "stage"
WAITING = (Path(__file__).parent / "waiting.html").read_text()
DROP_REQUEST = {"host", "connection", "content-length", "keep-alive", "upgrade", "cookie", "transfer-encoding"}
DROP_RESPONSE = {"connection", "keep-alive", "transfer-encoding", "set-cookie", "x-frame-options",
                 "content-security-policy"}
UNREACHABLE = {502, 503, 504}
CSP_META = re.compile(r"<meta[^>]*http-equiv=[\"']?content-security-policy[\"']?[^>]*>", re.I)
# Sent only by the waiting page's script: a link, a form or a crawler's request doesn't carry it.
START_HEADER = "x-stage-start"
BOTS = re.compile(r"bot|crawl|spider|slurp|scrape|fetch|preview|monitor|headless|phantom|lighthouse|python|curl|wget|"
                  r"httpx|axios|node-fetch|go-http|java/|okhttp|libwww|facebookexternalhit|embedly|whatsapp|"
                  r"telegram|discord|slack|linkedin|skype", re.I)
STARTS_PER_IP_PER_HOUR = int(os.environ.get("STARTS_PER_IP_PER_HOUR", 10))
starts_by_ip: dict[str, deque[float]] = defaultdict(deque)

settings = Settings(
    snapshot=os.environ.get("SNAPSHOT") or Settings().snapshot,
    max_sandboxes=int(os.environ.get("MAX_SANDBOXES", 20)),
    auto_stop_minutes=int(os.environ.get("AUTO_STOP_MINUTES", 15)),
    auto_delete_minutes=int(os.environ.get("AUTO_DELETE_MINUTES", 24 * 60)),
    public_ui_base=os.environ.get("PUBLIC_UI_BASE", f"http://localhost:{os.environ.get('UI_PORT', 8081)}").rstrip("/"),
)
manager: SandboxManager | None = None
client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global manager, client
    if manager is None:
        manager = SandboxManager(settings)
        client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=120), follow_redirects=False)
    yield


def visitor_of(request: Request) -> tuple[str, bool]:
    """The visitor's id from their cookie, or a new one (new=True: set it on the response)."""
    known = request.cookies.get(COOKIE)
    return (known, False) if known else (secrets.token_urlsafe(18), True)


def remember(response: Response, request: Request, visitor: str, new: bool) -> Response:
    if new:
        https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        response.set_cookie(COOKIE, visitor, max_age=30 * 24 * 3600, httponly=True, samesite="lax", secure=https)
    return response


async def proxy(request: Request, visitor: str, port: int, path: str) -> Response | None:
    """Forward the request to the visitor's sandbox; None if it isn't reachable (not ready, or stopped)."""
    base = await manager.preview_url(visitor, port)
    if base is None:
        return None
    headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQUEST}
    headers["x-daytona-skip-preview-warning"] = "true"
    url = f"{base}/{path}" + (f"?{request.url.query}" if request.url.query else "")
    upstream_request = client.build_request(request.method, url, headers=headers, content=await request.body())
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.TransportError:
        manager.forget(visitor)
        return None
    # The demo never answers 502-504 itself: those come from Daytona's proxy, for a stopped sandbox.
    if upstream.status_code in UNREACHABLE:
        await upstream.aclose()
        manager.forget(visitor)
        return None
    out = {k: v for k, v in upstream.headers.items() if k.lower() not in DROP_RESPONSE}
    # The Temporal UI's page carries a CSP <meta> (strict-dynamic, with hashes) that blocks its own
    # scripts once the page has come through the preview proxy: drop it, as the workshop's proxy does.
    if port == TEMPORAL_UI_PORT and "text/html" in upstream.headers.get("content-type", ""):
        html = (await upstream.aread()).decode("utf-8", "replace")
        await upstream.aclose()
        for header in ("content-encoding", "content-length"):
            out.pop(header, None)
        return HTMLResponse(CSP_META.sub("", html), status_code=upstream.status_code, headers=out)
    return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code, headers=out,
                             background=BackgroundTask(upstream.aclose))


def is_bot(request: Request) -> bool:
    agent = request.headers.get("user-agent", "")
    return not agent or bool(BOTS.search(agent))


def client_ip(request: Request) -> str:
    # Fly puts the visitor's address in Fly-Client-IP; behind any other proxy, the peer's.
    return request.headers.get("fly-client-ip") or (request.client.host if request.client else "unknown")


def may_start(request: Request, visitor: str, new: bool) -> str | None:
    """Why this request may not start a stage, or None if it may."""
    if new or request.headers.get(START_HEADER) != "1":
        return "Open the demo page in a browser to start your stage."
    if is_bot(request):
        return "This page starts a live demo for a person in a browser, not for bots."
    if manager.status(visitor).state != "none":
        return None  # already starting, or a stopped stage to wake: not a new one
    recent = starts_by_ip[client_ip(request)]
    while recent and recent[0] < time.time() - 3600:
        recent.popleft()
    if len(recent) >= STARTS_PER_IP_PER_HOUR:
        return "Too many stages started from your network in the last hour. Try again later, or run it locally (make demo)."
    recent.append(time.time())
    return None


def not_ready(request: Request, visitor: str, new: bool) -> Response:
    """The demo's paths while the stage isn't ready: the waiting page (which asks for the stage),
    or a 503 its poll understands. Neither starts anything."""
    wants_page = request.method == "GET" and "text/html" in request.headers.get("accept", "")
    if wants_page:
        return remember(HTMLResponse(WAITING, headers={"cache-control": "no-store"}), request, visitor, new)
    return remember(JSONResponse({"stage": manager.status(visitor).state}, status_code=503), request, visitor, new)


# ---- The demo ------------------------------------------------------------------------

app = FastAPI(title="ADK vs Temporal: front door", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/_stage/status")
async def stage_status(request: Request):
    visitor, new = visitor_of(request)
    progress = manager.status(visitor)
    return remember(JSONResponse({"state": progress.state, "message": progress.message}), request, visitor, new)


@app.post("/_stage/start")
async def stage_start(request: Request):
    visitor, new = visitor_of(request)
    refused = may_start(request, visitor, new)
    if refused:
        return remember(JSONResponse({"state": "refused", "message": refused}, status_code=403), request, visitor, new)
    progress = manager.ensure(visitor)
    return remember(JSONResponse({"state": progress.state, "message": progress.message}), request, visitor, new)


@app.get("/robots.txt")
async def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /_stage/\n")


@app.get("/_stage/health")
async def health():
    return {"ok": True, "snapshot": settings.snapshot}


@app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
async def demo(request: Request, path: str):
    visitor, new = visitor_of(request)
    if not new and manager.status(visitor).state == "ready":
        response = await proxy(request, visitor, DEMO_PORT, path)
        if response is not None:
            return response
    return not_ready(request, visitor, new)


# ---- The Temporal UI ------------------------------------------------------------------

ui = FastAPI(title="ADK vs Temporal: Temporal UI", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@ui.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
async def temporal_ui(request: Request, path: str):
    visitor, new = visitor_of(request)
    if not new and manager.status(visitor).state == "ready":
        response = await proxy(request, visitor, TEMPORAL_UI_PORT, path)
        if response is not None:
            return response
    return HTMLResponse("<!doctype html><meta charset=utf-8><title>Temporal UI</title>"
                        "<p style='font:15px system-ui;padding:24px'>Your stage isn't running. "
                        "Open the demo first; this link works once your stage is ready.</p>", status_code=503)
