"""FastAPI + SSE server for the peek-behind-the-curtain UI, and -- when Slack
tokens are present -- the host for the real Slack integration too, so a run
started in Slack shows up live in the peek view.

Live Slack mode: every request enters through Slack. The web UI's scenario
buttons post a seeded message into the channel and the Socket Mode listener
picks it up, so even a web-triggered demo exercises the real path end to end.
Approval comes from a Slack button (the clicker is identified by Slack's
verified user id) or from the web view with the operator token.

Mock mode (no tokens): the owned Slack-like surface, runs start directly.

Every state-changing endpoint requires the operator token printed at startup
(or OPERATOR_TOKEN). The read-only endpoints are meant for localhost."""
from __future__ import annotations

import asyncio
import json
import os
import queue
import secrets
import sqlite3
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from app import config
from app.boards import make_board
from app.events import Event, EventBus
from app.graph import build_graph
from app.langfuse_sink import langfuse_status
from app.retrieval import HandbookIndex
from app.slack_adapter import MockSlackAdapter
from cli import load_seed_requests

INDEX = HandbookIndex()
BOARD = make_board()  # MCP over stdio by default (BOARD_BACKEND)
SEED_REQUESTS = {r["id"]: r for r in load_seed_requests()}
# One durable checkpointer for every run (thread_id = run_id), so a paused approval
# outlives the process. Slack-side facts the graph state doesn't hold (which message
# carries the buttons) go in a small sidecar next to it.
SAVER = SqliteSaver(sqlite3.connect(config.CHECKPOINT_DB_PATH, check_same_thread=False))
_PENDING_PATH = config.DATA_DIR / "pending_approvals.json"
_pending_lock = threading.Lock()

OPERATOR_TOKEN = os.environ.get("OPERATOR_TOKEN") or secrets.token_urlsafe(18)
# Whoever holds the operator token approves as this identity; the request body
# never names the approver.
WEB_APPROVER = {"id": "web-operator", "name": os.environ.get("WEB_APPROVER_NAME", "Web operator"),
                "role": "admin", "via": "web"}
AGENT_NAME = "helpdesk-agent"

if config.SLACK_BOT_TOKEN and config.SLACK_APP_TOKEN:
    from app.real_slack_adapter import RealSlackAdapter

    SLACK = RealSlackAdapter(config.SLACK_BOT_TOKEN)
    SLACK_MODE = "live"
else:
    SLACK = MockSlackAdapter()
    SLACK_MODE = "mock"
CHANNEL = getattr(SLACK, "channel_name", config.SLACK_CHANNEL)

# One pool for every run. LangGraph's .invoke is sync, and runs start from both
# async endpoints and the Slack listener's threads.
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="run")
_subscribers: list[queue.Queue] = []
_subscribers_lock = threading.Lock()
_active: dict[str, dict] = {}  # run_id -> {"graph", "cfg", "bus", "channel", "thread_ts", ...}
_resolve_lock = threading.Lock()
_slack_handler = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _slack_handler
    port = os.environ.get("HELPDESK_PORT", "8731")  # the link only; uvicorn's --port picks the real port
    print(f"Operator link (approve/submit from the web view): http://127.0.0.1:{port}/#token={OPERATOR_TOKEN}", flush=True)
    if SLACK_MODE == "live":
        import logging

        from app.slack_listener import start_listener

        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
        for name in ("helpdesk.slack", "slack_bolt", "slack_sdk.socket_mode"):
            logging.getLogger(name).setLevel(logging.INFO)

        _slack_handler = start_listener(
            SLACK, config.SLACK_BOT_TOKEN, config.SLACK_APP_TOKEN, _slack_request, _slack_decision
        )
    yield
    if _slack_handler is not None:
        _slack_handler.close()


app = FastAPI(title="Northwire Helpdesk Agent", lifespan=lifespan)


def _require_operator(x_operator_token: str | None = Header(default=None)) -> None:
    if not x_operator_token or not secrets.compare_digest(x_operator_token, OPERATOR_TOKEN):
        raise HTTPException(status_code=401, detail="operator token required")


def _broadcast(ev: Event) -> None:
    d = ev.to_dict()
    with _subscribers_lock:
        for q in _subscribers:
            q.put_nowait(d)


def _broadcast_meta(run_id: str, phase: str, **extra) -> None:
    _broadcast(Event(run_id=run_id, node="_meta", event_type="phase", data={"phase": phase, **extra}))


def _mrkdwn(text: str) -> str:
    # Slack mrkdwn control characters; request text and model output must not become links or mentions.
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class NewRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    requester_name: str = Field(default="Web user", min_length=1, max_length=60)


class ApproveBody(BaseModel):
    approved: bool
    action_digest: str | None = None  # must match what the approver was shown


MANUAL_ESTIMATE = {
    "answerable": "manual: ~3–5 min, self-service or a quick lookup",
    "needs_write": "manual: ~10–15 min, handbook + Waypoint ticket + wait for provisioning",
    "escalate": "manual: human judgment required, no fixed time",
}


def _approval_blocks(run_id: str, action: dict, verdict: dict, digest: str) -> list[dict]:
    # Show the raw arguments that will execute, not just the model's prose summary.
    raw = "\n".join(f"`{k}`: {_mrkdwn(action.get(k))}" for k in
                    ("action_type", "target_system", "target_tier", "assignee", "title") if action.get(k) is not None)
    value = json.dumps({"run_id": run_id, "digest": digest})
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Approval needed* (approvers only; not the requester)\n{raw}"}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"Model's description: {_mrkdwn(action.get('description', ''))}"},
            {"type": "mrkdwn", "text": f"Permission check: {_mrkdwn(verdict.get('reason', ''))} · action sha256:{digest[:12]}"},
        ]},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Approve"}, "style": "primary", "action_id": "approve_request", "value": value},
                {"type": "button", "text": {"type": "plain_text", "text": "Deny"}, "style": "danger", "action_id": "deny_request", "value": value},
            ],
        },
    ]


def _fail_run(run_id: str, exc: Exception, channel: str | None, thread_ts: str | None) -> None:
    traceback.print_exc()  # full detail stays in the server log
    _broadcast_meta(run_id, "error", error=f"{type(exc).__name__}: {str(exc)[:200]}")
    if channel and thread_ts:
        try:
            SLACK.reply_in_thread(channel, thread_ts, "Sorry, something went wrong on my side, so I didn't act on this. "
                                                     "An IT/Ops teammate will pick it up.")
        except Exception:
            traceback.print_exc()


def _run_graph(run_id: str, initial_state: dict, origin: str) -> None:
    # Runs in the pool with nothing awaiting it -- an uncaught exception here
    # would otherwise vanish and leave the UI looking stuck with no signal.
    try:
        _broadcast_meta(run_id, "started", origin=origin, channel=initial_state["channel"],
                        thread_ts=initial_state["thread_ts"], requester=initial_state["requester_name"])
        bus = EventBus(run_id=run_id, jsonl_path=config.RUNS_DIR / f"{run_id}.jsonl")
        bus.subscribe(_broadcast)
        graph = build_graph(bus, INDEX, BOARD, checkpointer=SAVER)
        cfg = {"configurable": {"thread_id": run_id}}
        _active[run_id] = {"graph": graph, "cfg": cfg, "bus": bus, "channel": initial_state["channel"],
                           "thread_ts": initial_state["thread_ts"], "requester_id": initial_state.get("requester_id")}

        result = graph.invoke(initial_state, config=cfg)
        if "__interrupt__" in result:
            payload = result["__interrupt__"][0].value
            action, verdict, digest = payload["proposed_action"], payload["permission_verdict"], payload["action_digest"]
            bus.publish(node="approval_gate", event_type="approval_requested", proposed_action=action, action_digest=digest)
            pending = {"channel": initial_state["channel"], "thread_ts": initial_state["thread_ts"],
                       "requester_id": initial_state.get("requester_id")}
            if SLACK_MODE == "live":
                posted = SLACK.reply_in_thread(initial_state["channel"], initial_state["thread_ts"],
                                               f"Approval needed: {action.get('title', '')}",
                                               blocks=_approval_blocks(run_id, action, verdict, digest))
                pending.update(approval_ts=posted["ts"], approval_title=action.get("title", ""))
            _active[run_id].update(pending)
            _pending_put(run_id, pending)
            _broadcast_meta(run_id, "awaiting_approval")
            return  # paused; a Slack button or /api/approve resumes it
        _finish_run(run_id, result)
    except Exception as e:
        _fail_run(run_id, e, initial_state.get("channel"), initial_state.get("thread_ts"))


def _pending_all() -> dict:
    try:
        return json.loads(_PENDING_PATH.read_text())
    except FileNotFoundError:
        return {}


def _pending_put(run_id: str, info: dict | None) -> None:
    with _pending_lock:
        data = _pending_all()
        if info is None:
            data.pop(run_id, None)
        else:
            data[run_id] = info
        tmp = _PENDING_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, _PENDING_PATH)  # atomic: a crash never leaves half a file


def _rehydrate(run_id: str) -> dict | None:
    """Rebuild a paused run after a restart: fresh graph over the durable
    checkpoint, plus the Slack facts from the sidecar."""
    info = _pending_all().get(run_id)
    if info is None:
        return None
    bus = EventBus(run_id=run_id, jsonl_path=config.RUNS_DIR / f"{run_id}.jsonl")
    bus.subscribe(_broadcast)
    graph = build_graph(bus, INDEX, BOARD, checkpointer=SAVER)
    cfg = {"configurable": {"thread_id": run_id}}
    if not graph.get_state(cfg).next:
        return None
    _broadcast_meta(run_id, "rehydrated")
    return {"graph": graph, "cfg": cfg, "bus": bus, **info}


def _paused_entry(run_id: str) -> dict | None:
    with _resolve_lock:
        entry = _active.get(run_id)
        if entry is None:
            entry = _rehydrate(run_id)
            if entry is not None:
                _active[run_id] = entry
        return entry


def _expire(where: tuple[str, str] | None) -> None:
    if SLACK_MODE == "live" and where:
        note = "This approval has expired (its run is no longer paused). Re-post the request to start over."
        SLACK.update_message(where[0], where[1], note, blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"_{note}_"}}])


def _slack_decision(run_id: str, approved: bool, user_id: str, action_digest: str | None,
                    where: tuple[str, str] | None) -> None:
    """A Slack button click. The clicker is Slack's verified user id; authorization
    happens here, before the run is touched, so an unauthorized click leaves the
    request paused for someone who is allowed to decide."""
    entry = _paused_entry(run_id)
    if entry is None:
        _expire(where)
        return
    channel = where[0] if where else entry["channel"]
    role = SLACK.role_for(user_id)
    if role not in config.APPROVER_ROLES:
        SLACK.ephemeral(channel, user_id, "Only approvers can approve or deny requests. This one is still waiting "
                                          "for an approver.", thread_ts=entry.get("thread_ts"))
        return
    if approved and user_id == entry.get("requester_id"):
        SLACK.ephemeral(channel, user_id, "You can't approve your own request. Another approver has to.",
                        thread_ts=entry.get("thread_ts"))
        return
    approver = {"id": user_id, "name": SLACK.display_name(user_id), "role": role, "via": "slack"}
    _resume_graph(run_id, approved, approver, action_digest)


def _resume_graph(run_id: str, approved: bool, approver: dict, action_digest: str | None = None) -> None:
    entry = None
    try:
        with _resolve_lock:  # Slack button and web UI can race; first one wins
            entry = _active.get(run_id) or _rehydrate(run_id)
            if entry is None or entry.get("resolved"):
                return
            _active[run_id] = entry
            entry["resolved"] = True
        result = entry["graph"].invoke(
            Command(resume={"approved": approved, "approver": approver, "action_digest": action_digest}),
            config=entry["cfg"],
        )
        if SLACK_MODE == "live" and entry.get("approval_ts"):
            # Update the card from what actually happened, not from the click.
            record = result.get("approval") or {}
            at = datetime.now().strftime("%-I:%M %p")
            if record.get("decision") == "approved":
                note = f"Approved by {approver.get('name')} at {at}: {entry.get('approval_title', '')}"
            elif record.get("reason"):
                note = f"Not executed: {record['reason']}"
            else:
                note = f"Denied by {approver.get('name')} at {at}: {entry.get('approval_title', '')}"
            SLACK.update_message(entry["channel"], entry["approval_ts"], note,
                                 blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"*{_mrkdwn(note)}*"}}])
        _finish_run(run_id, result)
    except Exception as e:
        if entry is not None:
            entry["resolved"] = False  # a transient failure can be retried
        _fail_run(run_id, e, entry and entry.get("channel"), entry and entry.get("thread_ts"))


def _finish_run(run_id: str, result: dict) -> None:
    entry = _active.pop(run_id)
    _pending_put(run_id, None)
    SLACK.reply_in_thread(entry["channel"], entry["thread_ts"], result.get("final_response", "(no response)"))
    entry["bus"].close()
    _broadcast_meta(run_id, "done", final_response=result.get("final_response"))


def _new_run_id(prefix: str) -> str:
    return f"run-{prefix}-{uuid.uuid4().hex[:10]}"  # always server-generated


def _start_run(thread_ts: str, text: str, requester_name: str, requester_role: str, channel: str,
               request_id: str | None = None, run_id: str | None = None, origin: str = "web",
               requester_id: str | None = None) -> str:
    run_id = run_id or _new_run_id(origin)
    initial_state = {
        "run_id": run_id,
        "request_id": request_id or run_id,
        "requester_name": requester_name,
        "requester_role": requester_role,
        "requester_id": requester_id,
        "channel": channel,
        "thread_ts": thread_ts,
        "message": text,
        "auto_approve": False,
    }
    _EXECUTOR.submit(_run_graph, run_id, initial_state, origin)
    return run_id


def _slack_request(thread_ts: str, text: str, requester_name: str, requester_role: str,
                   request_id: str | None = None, run_id: str | None = None, requester_id: str | None = None) -> None:
    _start_run(thread_ts, text, requester_name, requester_role, CHANNEL,
               request_id=request_id, run_id=run_id, origin="slack", requester_id=requester_id)


SEED_FALLBACK_SECONDS = float(os.environ.get("SEED_FALLBACK_SECONDS", "20"))


def _seed_fallback(ts: str, message: str) -> None:
    seed = SLACK.claim_seed(ts)
    if seed is None:
        return  # the listener already picked it up
    print(f"Slack listener missed seeded message {ts}; starting {seed['run_id']} directly.", flush=True)
    _slack_request(thread_ts=ts, text=message, **seed)


def _submit(requester_name: str, requester_role: str, message: str, request_id: str | None, label: str) -> dict:
    run_id = _new_run_id(request_id or "web")
    posted = SLACK.post_message(CHANNEL, requester_name, message, label=label)
    if SLACK_MODE == "live":
        # Don't start the run here -- let it come back in through the listener,
        # so a web-triggered demo still goes through real Slack end to end.
        SLACK.register_seed(posted["ts"], {"requester_name": requester_name, "requester_role": requester_role,
                                           "request_id": request_id, "run_id": run_id})
        # Socket Mode doesn't replay events a connection missed: right after a restart the new
        # listener may not be receiving yet (or Slack is still delivering to the dead socket).
        # If the message hasn't come back through the listener in time, start the run here.
        threading.Timer(SEED_FALLBACK_SECONDS, _seed_fallback, args=(posted["ts"], message)).start()
    else:
        _start_run(posted["thread_ts"], message, requester_name, requester_role, CHANNEL,
                   request_id=request_id, run_id=run_id, origin="web")
    return {"run_id": run_id, "thread_ts": posted["thread_ts"], "channel": CHANNEL}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "ui" / "index.html")


@app.get("/api/status")
def status() -> dict:
    return {
        "langfuse": langfuse_status(),
        "model": config.MODEL_ID,
        "slack": SLACK_MODE,
        "slack_team": getattr(SLACK, "team", None),
        "slack_channel": CHANNEL,
        "seed_requests": list(SEED_REQUESTS.keys()),
    }


@app.get("/api/seed_requests")
def seed_requests() -> list[dict]:
    out = []
    for r in SEED_REQUESTS.values():
        preview = r["message"].strip().replace("\n", " ")
        if len(preview) > 100:
            preview = preview[:97] + "..."
        out.append({
            "id": r["id"],
            "preview": preview,
            "expected_category": r["expected_category"],
            "manual_estimate": MANUAL_ESTIMATE.get(r["expected_category"], ""),
        })
    return out


@app.get("/api/board")
def board() -> list[dict]:
    return BOARD.list_tickets()


@app.get("/api/thread/{channel}/{thread_ts}")
def thread(channel: str, thread_ts: str) -> list[dict]:
    if channel.lstrip("#") != CHANNEL:
        raise HTTPException(status_code=404, detail="only the helpdesk channel is readable here")
    return SLACK.get_thread(channel, thread_ts)


@app.post("/api/requests/seed/{request_id}", dependencies=[Depends(_require_operator)])
def start_seed_request(request_id: str) -> dict:
    req = SEED_REQUESTS.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="unknown seeded request")
    return _submit(req["requester_name"], req["requester_role"], req["message"], req["id"], label="seeded")


@app.post("/api/requests/custom", dependencies=[Depends(_require_operator)])
def start_custom_request(body: NewRequest) -> dict:
    name = body.requester_name.strip()
    if not name or name.lower().startswith(AGENT_NAME):
        raise HTTPException(status_code=422, detail="that requester name is reserved")
    # Web requests always come in as a viewer; roles come from the server, never the client.
    return _submit(name, "viewer", body.message, None, label="via web")


@app.post("/api/approve/{run_id}", dependencies=[Depends(_require_operator)])
def approve(run_id: str, body: ApproveBody) -> dict:
    _EXECUTOR.submit(_resume_graph, run_id, body.approved, WEB_APPROVER, body.action_digest)
    return {"ok": True}


@app.get("/api/stream")
async def stream():
    q: queue.Queue = queue.Queue()
    with _subscribers_lock:
        _subscribers.append(q)

    async def gen():
        try:
            while True:
                try:
                    ev = await asyncio.get_running_loop().run_in_executor(None, q.get, True, 15.0)
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _subscribers_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")
