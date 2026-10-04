"""The public demo: the same UI as server.py, safe to put on the internet.

    uv run uvicorn demo_server:app --port 8080

What's different from server.py, on purpose:
- No Slack and no operator endpoints. Every endpoint acts inside the visitor's
  own sandbox (a cookie session): their own in-memory board, thread and stream.
- Visitors pick from fixed scenarios. Free text is off unless DEMO_FREE_TEXT=1.
- DEMO_MODE=replay (the default) plays recorded runs back with no model calls.
  DEMO_MODE=live runs the real agent, behind per-IP rate limits, a cap on
  concurrent runs, and a hard daily spend cap. When a guard says no, the visitor
  gets the recorded run instead, and the page says why.
Settings are env vars, listed in DEPLOY.md."""
from __future__ import annotations

import asyncio
import json
import os
import queue
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app import agent_lab, config
from app.demo.engine import CHANNEL, LiveRunner, ReplayLibrary, Sandboxes, valid_session
from app.demo.limits import RateLimiter, SpendCap
from app.retrieval import HandbookIndex
from cli import load_seed_requests


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


MODE = os.environ.get("DEMO_MODE", "replay")  # "replay" or "live"
FREE_TEXT = os.environ.get("DEMO_FREE_TEXT") == "1"
TRUST_PROXY = os.environ.get("DEMO_TRUST_PROXY") == "1"  # read X-Forwarded-For (behind Fly/Render/Caddy)
SECURE_COOKIE = os.environ.get("DEMO_SECURE_COOKIE", "1") == "1"
DATA_DIR = Path(os.environ.get("DEMO_DATA_DIR", config.DATA_DIR / "demo"))
REPLAY_DIR = Path(os.environ.get("DEMO_REPLAY_DIR", config.REPO_ROOT / "demo" / "replays"))
SCENARIOS = os.environ.get(
    "DEMO_SCENARIOS", "req-012,req-011,req-019,req-020,req-021,req-024,req-005,req-009").split(",")

SPEND = SpendCap(float(os.environ.get("DEMO_DAILY_USD", "2.00")), DATA_DIR / "spend.json")
LIVE_LIMIT = RateLimiter(_env_int("DEMO_RATE_PER_MINUTE", 3), _env_int("DEMO_RATE_PER_DAY", 20))
REPLAY_LIMIT = RateLimiter(_env_int("DEMO_REPLAY_PER_MINUTE", 20), _env_int("DEMO_REPLAY_PER_DAY", 500))
SANDBOXES = Sandboxes(max_count=_env_int("DEMO_MAX_SESSIONS", 500))
REPLAYS = ReplayLibrary(REPLAY_DIR)
_live_runner: LiveRunner | None = None

_all_requests = {r["id"]: r for r in load_seed_requests()}
REQUESTS = {sid: _all_requests[sid] for sid in SCENARIOS if sid in _all_requests}


def _runner() -> LiveRunner:
    global _live_runner
    if _live_runner is None:
        _live_runner = LiveRunner(HandbookIndex(), on_spend=SPEND.record,
                                  max_concurrent=_env_int("DEMO_MAX_CONCURRENT", 4))
    return _live_runner


def live_unavailable() -> str | None:
    """Why a live run can't start right now, or None if it can."""
    if MODE != "live":
        return "this demo is serving recorded runs"
    if not config.ANTHROPIC_API_KEY:
        return "no model key configured, so recorded runs are shown"
    if SPEND.exhausted():
        return "today's live-run budget is used up, so recorded runs are shown until tomorrow (UTC)"
    return None


def _client_ip(request: Request) -> str:
    if TRUST_PROXY and request.headers.get("x-forwarded-for"):
        return request.headers["x-forwarded-for"].split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _box(response: Response | None, demo_sid: str | None):
    box = SANDBOXES.get(demo_sid)
    if response is not None and box.sid != demo_sid:
        response.set_cookie("demo_sid", box.sid, httponly=True, samesite="lax", secure=SECURE_COOKIE, max_age=3600)
    return box


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Live runs go to Agent Lab's bench (AGENT_LAB_URL, default the local one); a silent no-op without one.
    agent_lab.init()
    yield


app = FastAPI(title="Northwire Helpdesk Agent: public demo", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


class CustomRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)


class ApproveBody(BaseModel):
    approved: bool
    action_digest: str | None = None


@app.get("/")
def index(response: Response, demo_sid: str | None = Cookie(default=None), bench_session: str | None = None):
    resp = FileResponse(config.REPO_ROOT / "ui" / "index.html")
    box = SANDBOXES.get(demo_sid)
    # Opened inside Agent Lab's side-by-side shell: this visitor's live runs are filed under its session.
    if valid_session(bench_session):
        box.bench_session = bench_session
    if box.sid != demo_sid:
        resp.set_cookie("demo_sid", box.sid, httponly=True, samesite="lax", secure=SECURE_COOKIE, max_age=3600)
    return resp


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/api/status")
def status(response: Response, demo_sid: str | None = Cookie(default=None)) -> dict:
    _box(response, demo_sid)
    reason = live_unavailable()
    return {
        "demo": True,
        "mode": "live" if reason is None else "replay",
        "mode_reason": reason,
        "free_text": FREE_TEXT and reason is None,
        "model": config.MODEL_ID,
        "langfuse": "off (public demo)",
        "slack": "mock",
        "slack_channel": CHANNEL,
        "seed_requests": list(REQUESTS),
    }


@app.get("/api/seed_requests")
def seed_requests() -> list[dict]:
    replay_only = live_unavailable() is not None
    out = []
    for r in REQUESTS.values():
        if replay_only and r["id"] not in REPLAYS.by_id:
            continue
        preview = r["message"].strip().replace("\n", " ")
        out.append({"id": r["id"], "preview": preview[:97] + "..." if len(preview) > 100 else preview,
                    "expected_category": r["expected_category"],
                    "manual_estimate": config.MANUAL_ESTIMATE.get(r["expected_category"], "")})
    return out


@app.post("/api/requests/seed/{request_id}")
def start_scenario(request_id: str, request: Request, response: Response,
                   demo_sid: str | None = Cookie(default=None)) -> dict:
    box = _box(response, demo_sid)
    req = REQUESTS.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="unknown scenario")
    ip = _client_ip(request)
    reason = live_unavailable()
    if reason is None:
        refused = LIVE_LIMIT.check(ip)
        if refused is None:
            started = _runner().try_start(box, req)
            if started:
                return started
            reason = "the live demo is busy right now, so a recorded run is shown"
        else:
            reason = f"{refused}, so a recorded run is shown"
    if REPLAY_LIMIT.check(ip):
        raise HTTPException(status_code=429, detail="too many requests; try again in a minute")
    started = REPLAYS.start(box, request_id)
    if started is None:
        raise HTTPException(status_code=503, detail=f"{reason}, and there's no recording for this scenario")
    return {**started, "mode_reason": reason}


@app.post("/api/requests/custom")
def start_custom(body: CustomRequest, request: Request, response: Response,
                 demo_sid: str | None = Cookie(default=None)) -> dict:
    if not FREE_TEXT or live_unavailable() is not None:
        raise HTTPException(status_code=404, detail="free-text requests aren't enabled on this demo")
    box = _box(response, demo_sid)
    refused = LIVE_LIMIT.check(_client_ip(request))
    if refused:
        raise HTTPException(status_code=429, detail=refused)
    started = _runner().try_start(box, {"id": "custom", "requester_name": "Visitor", "requester_role": "viewer",
                                        "message": body.message})
    if not started:
        raise HTTPException(status_code=503, detail="the live demo is busy; try a scenario instead")
    return started


@app.post("/api/approve/{run_id}")
def approve(run_id: str, body: ApproveBody, response: Response, demo_sid: str | None = Cookie(default=None)) -> dict:
    box = _box(response, demo_sid)
    kind = (box.runs.get(run_id) or {}).get("kind")
    ok = (_runner().resume(box, run_id, body.approved, body.action_digest) if kind == "live"
          else REPLAYS.resume(box, run_id, body.approved, body.action_digest) if kind == "replay" else False)
    if not ok:
        raise HTTPException(status_code=404, detail="no paused run with that id in this session")
    return {"ok": True}


@app.get("/api/board")
def board(response: Response, demo_sid: str | None = Cookie(default=None)) -> list[dict]:
    return _box(response, demo_sid).board.list_tickets()


@app.get("/api/thread/{channel}/{thread_ts}")
def thread(channel: str, thread_ts: str, response: Response, demo_sid: str | None = Cookie(default=None)) -> list[dict]:
    if channel.lstrip("#") != CHANNEL:
        raise HTTPException(status_code=404, detail="only the helpdesk channel is readable here")
    return _box(response, demo_sid).slack.get_thread(CHANNEL, thread_ts)


@app.get("/api/stream")
async def stream(demo_sid: str | None = Cookie(default=None)):
    box = SANDBOXES.get(demo_sid)
    q: queue.Queue = queue.Queue(maxsize=1000)
    box.subscribers.append(q)

    async def gen():
        try:
            while True:
                try:
                    ev = await asyncio.get_running_loop().run_in_executor(None, q.get, True, 15.0)
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            if q in box.subscribers:
                box.subscribers.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")
