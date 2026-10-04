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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_static_page_plays_each_runs_bench_events(tmp_path):
    """Inside Agent Lab's shell the static page sends the bench the events of the same run it plays
    (scripts/regen_demo.py made both from one run of the real graph): all of them, once, in order,
    under the page's run id, the pause then the ending the visitor chose, and the map and story the
    recordings carry."""
    subprocess.run(["uv", "run", "--quiet", "python", "scripts/export_static.py", "--out", str(tmp_path / "site")],
                   cwd=ROOT, check=True, capture_output=True)
    data_path = tmp_path / "site" / "data.json"
    js = json.loads(subprocess.run(["node", str(ROOT / "tests" / "static_parity_driver.js"), str(data_path)],
                                   check=True, capture_output=True, text=True).stdout)
    data = json.loads(data_path.read_text())
    head = json.loads((ROOT / "demo/bench-recordings/req-005.recording.jsonl").read_text().split("\n", 1)[0])
    assert data["bench"] == {"topology": head["topology"], "story": head["story"]}
    for s in data["scenarios"]:
        rec = data["replays"][s["id"]]
        for choice in ("approve", "wrong-digest"):
            got = js[f"{s['id']}/{choice}"]
            if rec["paused"]:
                want = rec["bench"]["pre"] + rec["bench"]["tails"]["approved" if choice == "approve" else "denied"]
            else:
                want = rec["bench"]["events"]
            assert [(e["node"], e["event_type"], e["seq"]) for e in got["bench"]] == \
                   [(e["node"], e["event_type"], e["seq"]) for e in want], s["id"]
            assert {e["run_id"] for e in got["bench"]} == {got["run_id"]}
            started = {e["step_id"] for e in got["bench"] if e["event_type"] == "step_started"}
            assert started == {e["step_id"] for e in got["bench"] if e["event_type"] == "step_finished"}
            assert all(sid.startswith(got["run_id"] + ":") for sid in started)
            assert [e["ts"] for e in got["bench"]] == sorted(e["ts"] for e in got["bench"])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_replays_keep_recorded_times_and_add_the_real_wait(tmp_path):
    """Both engines sleep a capped gap (so a visitor doesn't wait out a 4 s model call) but stamp
    events on the run's own clock: the recording's gaps, plus the visitor's real wait at the card.
    The bench beside the app then shows the same work time as the bench's replay of the recording."""
    rec = json.loads((ROOT / "demo/replays/req-012.json").read_text())
    recorded = rec["pre"][-1]["ts"] - rec["pre"][0]["ts"]
    script = """
      const E = require(process.argv[1]); const rec = require(process.argv[2]);
      let t = 1000; const evs = [];
      const e = new E({ replays: { r: rec }, scenarios: [] }, { sleep: async (s) => { t += s; }, now: () => t });
      e.subscribe((ev) => evs.push(ev));
      (async () => {
        const s = e.start('r'); await s.done;
        const pre = evs.filter((x) => x.node !== '_meta');
        t += 20;                                   // the visitor takes 20 s to approve
        await e.resume(s.run_id, true, rec.action_digest);
        const all = evs.filter((x) => x.node !== '_meta');
        console.log(JSON.stringify({ pre: pre[pre.length - 1].ts - pre[0].ts, tail0: all[pre.length].ts - pre[pre.length - 1].ts,
                                     metaLast: evs[evs.length - 1].ts, last: all[all.length - 1].ts }));
      })();"""
    out = json.loads(subprocess.run(["node", "-e", script, str(ROOT / "static/replay-engine.js"), str(ROOT / "demo/replays/req-012.json")],
                                    check=True, capture_output=True, text=True).stdout)
    assert abs(out["pre"] - recorded) < 1e-6          # 7.7 s of recorded work, not ~2.4 s of capped sleeps
    assert abs(out["tail0"] - 20) < 0.01              # the real wait at the card
    assert out["metaLast"] >= out["last"]             # the run ends on its own clock, after its last step

    box, lib = Sandbox(sid="t"), ReplayLibrary(ROOT / "demo/replays", max_gap=0.0)
    events = []
    box.subscribers.append(type("Q", (), {"put_nowait": staticmethod(events.append)})())
    r = lib.start(box, "req-012")
    end = time.time() + 10
    while not any(e["node"] == "_meta" and e["data"].get("phase") == "awaiting_approval" for e in events) and time.time() < end:
        time.sleep(0.02)
    steps = [e for e in events if e["node"] != "_meta"]
    assert abs((steps[-1]["ts"] - steps[0]["ts"]) - recorded) < 1e-6
    lib.resume(box, r["run_id"], True, rec["action_digest"])
