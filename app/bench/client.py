"""Talks to an observability bench, if one is configured. Off unless BENCH_URL is set
(e.g. http://127.0.0.1:8790). Registers this app's map and story, and sends bench events
converted here by adapter.py. Everything goes out on one background thread, fire-and-forget:
a slow or missing bench never touches a run."""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import urllib.request
from pathlib import Path

from .adapter import AgentLabsAdapter

HERE = Path(__file__).resolve().parent
APP_ID = "slack-helpdesk"
BASE = os.environ.get("BENCH_URL", "").strip().rstrip("/")
_SESSION_OK = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_q: queue.Queue = queue.Queue(maxsize=5000)
_started = False
_lock = threading.Lock()
_adapters: dict[str, AgentLabsAdapter] = {}


def enabled() -> bool:
    return bool(BASE)


def valid_session(s: str | None) -> str | None:
    return s if s and _SESSION_OK.match(s) else None


def topology() -> dict:
    return json.loads((HERE / "topology.json").read_text())


def story() -> str:
    return (HERE / "story.js").read_text()


def _post(method: str, path: str, body) -> None:
    req = urllib.request.Request(BASE + path, data=json.dumps(body, default=str).encode(), method=method,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=3).close()


def _worker() -> None:
    while True:
        method, path, body = _q.get()
        try:
            _post(method, path, body)
        except Exception:
            pass  # the bench is an observer; losing something there is acceptable


def _enqueue(method: str, path: str, body) -> None:
    global _started
    if not BASE:
        return
    if not _started:
        with _lock:
            if not _started:
                threading.Thread(target=_worker, name="bench-client", daemon=True).start()
                _started = True
    try:
        _q.put_nowait((method, path, body))
    except queue.Full:
        pass


def register() -> None:
    """Hands the bench this app's map and story. Safe to call at every startup."""
    _enqueue("PUT", f"/apps/{APP_ID}", {"topology": topology(), "story": story()})


def send(session: str, ev: dict) -> None:
    """One source event from the event bus, converted and sent as bench events."""
    if not BASE or not ev.get("run_id"):
        return
    key = f"{session}|{ev['run_id']}"
    with _lock:
        a = _adapters.get(key)
        if a is None:
            if len(_adapters) > 1000:  # runs abandoned mid-way, e.g. a closed tab at the gate
                _adapters.pop(next(iter(_adapters)))
            mode = "full" if os.environ.get("TRACE_CONTENT") == "full" else "redacted"
            a = _adapters[key] = AgentLabsAdapter(session_id=session, content_mode=mode)
        out = a.feed(ev)
        if a.finished:
            _adapters.pop(key, None)
    if out:
        _enqueue("POST", "/ingest", out)
