"""The static page's replay engine (static/replay-engine.js) must play the
recordings exactly as the hosted demo's Python replay does: the same events in
the same order, the same thread and the same board, for every scenario, taken
both approved and with a wrong digest. Ticket ids are random on both sides,
so they're normalized before comparing."""
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.demo.engine import ReplayLibrary, Sandbox

ROOT = Path(__file__).resolve().parent.parent
REQ = re.compile(r"REQ-[0-9a-f]{6}|\d{1,2}:\d{2} [AP]M")


def _norm(obj):
    return json.loads(REQ.sub("REQ-x", json.dumps(obj)))


def _events(evs):
    # thread_ts is a clock reading on both sides; everything else must match exactly.
    return [_norm({"node": e["node"], "event_type": e["event_type"],
                   "data": {k: v for k, v in e["data"].items() if k != "thread_ts"}}) for e in evs]


def _python_side(scenario_ids):
    lib = ReplayLibrary(ROOT / "demo" / "replays", max_gap=0)
    out = {}
    for sid in scenario_ids:
        for choice in ("approve", "wrong-digest"):
            box = Sandbox(sid="t")
            events = []
            box.subscribers.append(type("Q", (), {"put_nowait": staticmethod(events.append)})())
            r = lib.start(box, sid)
            rec = lib.by_id[sid]
            end = time.time() + 10
            done = lambda: any(e["node"] == "_meta" and e["data"].get("phase") in ("done", "awaiting_approval") for e in events)
            while not done() and time.time() < end:
                time.sleep(0.01)
            if rec.get("paused"):
                lib.resume(box, r["run_id"], True, rec["action_digest"] if choice == "approve" else "wrong")
                while not any(e["node"] == "_meta" and e["data"].get("phase") == "done" for e in events) and time.time() < end:
                    time.sleep(0.01)
            out[f"{sid}/{choice}"] = {"events": events, "thread": box.slack.get_thread("helpdesk-requests", r["thread_ts"]),
                                      "board": box.board.list_tickets()}
    return out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_replay_matches_python_replay(tmp_path):
    data_path = tmp_path / "data.json"
    subprocess.run(["uv", "run", "--quiet", "python", "scripts/export_static.py", "--out", str(tmp_path / "site")],
                   cwd=ROOT, check=True, capture_output=True)
    shutil.copy(tmp_path / "site" / "data.json", data_path)
    js = json.loads(subprocess.run(["node", str(ROOT / "tests" / "static_parity_driver.js"), str(data_path)],
                                   check=True, capture_output=True, text=True).stdout)
    data = json.loads(data_path.read_text())
    py = _python_side([s["id"] for s in data["scenarios"]])
    assert set(js) == set(py) and len(js) == 2 * len(data["scenarios"])
    for key in py:
        assert len(py[key]["events"]) >= 6 and len(py[key]["thread"]) == 2, key  # a real run, not an empty one
        assert _events(js[key]["events"]) == _events(py[key]["events"]), key
        assert [(m["user"], REQ.sub("REQ-x", m["text"])) for m in js[key]["thread"]] == \
               [(m["user"], REQ.sub("REQ-x", m["text"])) for m in py[key]["thread"]], key
        fields = ("title", "status", "assignee", "labels")
        assert [{f: t.get(f) for f in fields} for t in js[key]["board"]] == \
               [{f: t.get(f) for f in fields} for t in py[key]["board"]], key
    # And the two endings really differ where the run paused: approving opens a ticket, a wrong digest doesn't.
    for s in data["scenarios"]:
        if data["replays"][s["id"]]["paused"]:
            assert js[f"{s['id']}/approve"]["board"] and not js[f"{s['id']}/wrong-digest"]["board"]
