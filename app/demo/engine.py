"""The public demo's engine: a sandbox per visitor, live runs behind the
guards in limits.py, and recorded replays that play back through the same
event stream with no model calls.

A sandbox holds its own in-memory ticket board, a mock Slack thread store and
its own event subscribers, so visitors never see each other's runs. The
visitor approves their own sandbox's writes: the approval gate still checks
the digest, and still refuses anything the policy refuses."""
from __future__ import annotations

import json
import queue
import re
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from .. import config
from ..board_adapter import BoardAdapter
from ..events import Event, EventBus
from ..graph import build_graph
from ..slack_adapter import MockSlackAdapter

CHANNEL = "helpdesk-requests"
_APPROVED_AT = re.compile(r"(Approved by [^.\n]*? at )\d{1,2}:\d{2} [AP]M")
_SESSION_OK = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
VISITOR = {"id": "demo-visitor", "name": "You (demo)", "role": "admin", "via": "web"}


@dataclass
class Sandbox:
    sid: str
    board: BoardAdapter = field(default_factory=lambda: BoardAdapter(Path(":memory:")))
    slack: MockSlackAdapter = field(default_factory=MockSlackAdapter)
    subscribers: list = field(default_factory=list)
    runs: dict = field(default_factory=dict)  # run_id -> live graph entry, or replay state
    last_seen: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Set when the page was opened inside Agent Lab's side-by-side shell: the visitor's live runs
    # carry it as their session (Agent Lab's library sends them; see LiveRunner._run).
    bench_session: str | None = None

    def publish(self, ev: dict) -> None:
        for q in list(self.subscribers):
            q.put_nowait(ev)

    def meta(self, run_id: str, phase: str, **extra) -> None:
        self.publish(Event(run_id=run_id, node="_meta", event_type="phase", data={"phase": phase, **extra}).to_dict())


def valid_session(s: str | None) -> str | None:
    """A bench session id as the shell passes it, or None if it doesn't look like one."""
    return s if s and _SESSION_OK.match(s) else None


class Sandboxes:
    """Session id -> Sandbox, with an idle timeout and a hard count cap."""

    def __init__(self, max_count: int = 500, idle_seconds: float = 1800):
        self.max_count, self.idle_seconds = max_count, idle_seconds
        self._by_sid: dict[str, Sandbox] = {}
        self._lock = threading.Lock()

    def get(self, sid: str | None) -> Sandbox:
        now = time.time()
        with self._lock:
            for k in [k for k, s in self._by_sid.items() if now - s.last_seen > self.idle_seconds]:
                del self._by_sid[k]
            box = self._by_sid.get(sid or "")
            if box is None:
                if len(self._by_sid) >= self.max_count:  # evict the least recently seen
                    del self._by_sid[min(self._by_sid, key=lambda k: self._by_sid[k].last_seen)]
                box = Sandbox(sid=secrets.token_urlsafe(18))
                self._by_sid[box.sid] = box
            box.last_seen = now
            return box


class LiveRunner:
    """Real runs in a visitor's sandbox. Every llm_call's cost goes to `on_spend`
    (the spend cap). At most `max_concurrent` live runs at once, which also bounds
    how far the cap can be overshot by runs already in flight."""

    def __init__(self, index, on_spend, max_concurrent: int = 4, llm=None):
        self.index, self.on_spend, self.llm = index, on_spend, llm
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._pool = ThreadPoolExecutor(max_workers=max_concurrent * 2, thread_name_prefix="demo-run")

    def try_start(self, box: Sandbox, req: dict, recorder: list | None = None) -> dict | None:
        if not self._slots.acquire(blocking=False):
            return None  # busy; the caller falls back to replay
        posted = box.slack.post_message(CHANNEL, req["requester_name"], req["message"])
        run_id = f"run-demo-{uuid.uuid4().hex[:10]}"
        self._pool.submit(self._run, box, req, run_id, posted["ts"], recorder)
        return {"run_id": run_id, "thread_ts": posted["ts"], "channel": CHANNEL, "mode": "live"}

    def _bus(self, box: Sandbox, run_id: str, recorder: list | None) -> EventBus:
        bus = EventBus(run_id=run_id)  # no JSONL on disk for public visitors

        def forward(ev: Event) -> None:
            d = ev.to_dict()
            box.publish(d)
            if recorder is not None:
                recorder.append(d)
            if ev.event_type == "llm_call":
                self.on_spend(float(ev.data.get("cost_usd") or 0.0))

        bus.subscribe(forward)
        return bus

    def _run(self, box: Sandbox, req: dict, run_id: str, thread_ts: str, recorder: list | None) -> None:
        released = False
        try:
            box.meta(run_id, "started", origin="demo", channel=CHANNEL, thread_ts=thread_ts,
                     requester=req["requester_name"])
            bus = self._bus(box, run_id, recorder)
            graph = build_graph(bus, self.index, box.board, checkpointer=InMemorySaver(), llm=self.llm)
            cfg = {"configurable": {"thread_id": run_id}}
            if box.bench_session:  # Agent Lab files the run (and its resume, which reuses cfg) under this session
                cfg["metadata"] = {"session_id": box.bench_session}
            state = {"run_id": run_id, "request_id": req.get("id", run_id), "requester_name": req["requester_name"],
                     "requester_role": req["requester_role"], "requester_id": None, "channel": CHANNEL,
                     "thread_ts": thread_ts, "message": req["message"], "auto_approve": False}
            result = graph.invoke(state, config=cfg)
            if "__interrupt__" in result:
                payload = result["__interrupt__"][0].value
                bus.publish(node="approval_gate", event_type="approval_requested",
                            proposed_action=payload["proposed_action"], action_digest=payload["action_digest"])
                box.runs[run_id] = {"kind": "live", "graph": graph, "cfg": cfg, "bus": bus, "thread_ts": thread_ts}
                box.meta(run_id, "awaiting_approval")
                self._slots.release()  # a paused run holds no model call; don't hold a slot while a human decides
                released = True
                return
            self._finish(box, run_id, thread_ts, result)
        except Exception as e:  # never leave the visitor's UI hanging
            box.meta(run_id, "error", error=type(e).__name__)
        finally:
            if not released:
                self._slots.release()

    def resume(self, box: Sandbox, run_id: str, approved: bool, digest: str | None) -> bool:
        with box.lock:
            entry = box.runs.get(run_id)
            if not entry or entry.get("kind") != "live" or entry.get("resolved"):
                return False
            entry["resolved"] = True
        self._pool.submit(self._resume, box, run_id, entry, approved, digest)
        return True

    def _resume(self, box: Sandbox, run_id: str, entry: dict, approved: bool, digest: str | None) -> None:
        try:
            result = entry["graph"].invoke(Command(resume={"approved": approved, "approver": VISITOR,
                                                           "action_digest": digest}), config=entry["cfg"])
            box.runs.pop(run_id, None)
            self._finish(box, run_id, entry["thread_ts"], result)
        except Exception as e:
            box.meta(run_id, "error", error=type(e).__name__)

    def _finish(self, box: Sandbox, run_id: str, thread_ts: str, result: dict) -> None:
        box.slack.reply_in_thread(CHANNEL, thread_ts, result.get("final_response", "(no response)"))
        box.meta(run_id, "done", final_response=result.get("final_response"))


class ReplayLibrary:
    """Recorded runs (demo/replays/*.json), one per scenario. A replay plays the
    recorded events back through the visitor's stream with their original pacing
    (gaps capped), pauses at the approval card if the run had one, then plays the
    approved or denied tail. No model is called."""

    def __init__(self, directory: Path, max_gap: float = 1.2, speed: float = 1.0):
        self.max_gap, self.speed = max_gap, speed
        self.by_id: dict[str, dict] = {}
        for p in sorted(directory.glob("*.json")) if directory.exists() else []:
            rec = json.loads(p.read_text())
            self.by_id[rec["scenario"]] = rec
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="demo-replay")
        self._clocks: dict[str, dict] = {}  # run_id -> {"vt", "paused_at"}: each run's own clock (see _emit)

    def start(self, box: Sandbox, scenario_id: str) -> dict | None:
        rec = self.by_id.get(scenario_id)
        if rec is None:
            return None
        posted = box.slack.post_message(CHANNEL, rec["requester_name"], rec["message"])
        run_id = f"run-replay-{uuid.uuid4().hex[:10]}"
        self._pool.submit(self._play_pre, box, rec, run_id, posted["ts"])
        return {"run_id": run_id, "thread_ts": posted["ts"], "channel": CHANNEL, "mode": "replay"}

    def _emit(self, box: Sandbox, events: list[dict], run_id: str) -> None:
        """Sleeps the recorded gaps capped at max_gap, but stamps events on the run's own clock,
        which advances by the recorded gaps (uncapped) and by the visitor's real wait at the
        approval card: shown times are the recording's plus the real human wait (static twin:
        static/replay-engine.js)."""
        clock = self._clocks.setdefault(run_id, {"vt": time.time()})
        prev = None
        for ev in events:
            if prev is not None:
                time.sleep(min(max(ev["ts"] - prev, 0.0), self.max_gap) / self.speed)
                clock["vt"] += max(ev["ts"] - prev, 0.0)
            prev = ev["ts"]
            box.publish({**ev, "run_id": run_id, "ts": clock["vt"]})

    def _meta(self, box: Sandbox, run_id: str, phase: str, **extra) -> None:
        clock = self._clocks.get(run_id)
        box.publish(Event(run_id=run_id, node="_meta", event_type="phase", data={"phase": phase, **extra},
                          ts=clock["vt"] if clock else time.time()).to_dict())

    def _play_pre(self, box: Sandbox, rec: dict, run_id: str, thread_ts: str) -> None:
        self._clocks[run_id] = {"vt": time.time()}
        self._meta(box, run_id, "started", origin="replay", channel=CHANNEL, thread_ts=thread_ts,
                   requester=rec["requester_name"])
        self._emit(box, rec["pre"], run_id)
        if rec.get("paused"):
            box.runs[run_id] = {"kind": "replay", "rec": rec, "thread_ts": thread_ts}
            self._clocks[run_id]["paused_at"] = time.time()
            self._meta(box, run_id, "awaiting_approval")
            return
        self._finish(box, rec, run_id, thread_ts, rec["final"])

    def resume(self, box: Sandbox, run_id: str, approved: bool, digest: str | None) -> bool:
        with box.lock:
            entry = box.runs.get(run_id)
            if not entry or entry.get("kind") != "replay" or entry.get("resolved"):
                return False
            entry["resolved"] = True
        clock = self._clocks.get(run_id)
        if clock and clock.get("paused_at") is not None:  # the real wait at the approval card
            clock["vt"] += max(0.0, time.time() - clock.pop("paused_at"))
        rec = entry["rec"]
        # Same rule as a live run: an approval must carry the digest of the action shown.
        tail = "approved" if approved and digest == rec.get("action_digest") else "denied"
        self._pool.submit(self._play_tail, box, rec, run_id, entry["thread_ts"], tail)
        return True

    def _play_tail(self, box: Sandbox, rec: dict, run_id: str, thread_ts: str, tail: str) -> None:
        # Open the recorded tickets on this visitor's board first, then swap the recorded
        # ticket ids for the new ones everywhere they appear, so reply and board agree.
        ids = {}
        for t in rec["tails"][tail].get("tickets", []):
            new = box.board.create_ticket(t["title"], t.get("description") or "", created_by=t.get("created_by") or "",
                                          labels=[x for x in (t.get("labels") or "").split(",") if x],
                                          assignee=t.get("assignee"), idempotency_key=run_id)
            ids[t["id"]] = new["id"]

        now = datetime.now().strftime("%-I:%M %p")

        def swap(text: str) -> str:
            for old, new in ids.items():
                text = text.replace(old, new)
            # The recording's approval time becomes the visitor's.
            return _APPROVED_AT.sub(lambda m: f"{m.group(1)}{now}", text)

        events = json.loads(swap(json.dumps(rec["tails"][tail]["events"])))
        self._emit(box, events, run_id)
        box.runs.pop(run_id, None)
        self._finish(box, rec, run_id, thread_ts, swap(rec["tails"][tail]["final"]))

    def _finish(self, box: Sandbox, rec: dict, run_id: str, thread_ts: str, final: str) -> None:
        box.slack.reply_in_thread(CHANNEL, thread_ts, final)
        self._meta(box, run_id, "done", final_response=final, replay=True)
        self._clocks.pop(run_id, None)
