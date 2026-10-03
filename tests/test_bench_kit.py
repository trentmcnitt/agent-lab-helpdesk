"""The bench kit (app/bench/): every recorded run and run log converts into bench events that
validate against the bench's schema, carry what the old peek view rendered, and name branches
that exist in the map. The schema is read from the bench repo when it's checked out beside
this one (BENCH_REPO overrides); these tests skip otherwise."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.bench import adapter, client, recordings

REPO = Path(__file__).resolve().parents[1]
BENCH = Path(os.environ.get("BENCH_REPO", REPO.parents[1] / "agent-lab-bench"))
REPLAYS = sorted((REPO / "demo/replays").glob("*.json"))
RUNS = sorted((REPO / "runs").glob("*.jsonl"))

jsonschema = pytest.importorskip("jsonschema")
if not (BENCH / "schema/bench-event.schema.json").exists():
    pytest.skip(f"bench repo not found at {BENCH}", allow_module_level=True)
EVENT = jsonschema.Draft202012Validator(json.loads((BENCH / "schema/bench-event.schema.json").read_text()))
TOPO = jsonschema.Draft202012Validator(json.loads((BENCH / "schema/bench-topology.schema.json").read_text()))
MAP = client.topology()
BRANCHES = {(e["from"], e.get("from_branch")) for e in MAP["edges"]}


def check_run(events):
    for e in events:
        errs = sorted(EVENT.iter_errors(e), key=str)
        assert not errs, f"{e['event_type']}@{e['node']}: {errs[0].message}"
    assert [e["seq"] for e in events] == list(range(len(events)))
    assert [e["ts"] for e in events] == sorted(e["ts"] for e in events)
    assert events[0]["event_type"] == "run_started"
    opened = {e["step_id"] for e in events if e["event_type"] == "step_started"}
    assert opened == {e["step_id"] for e in events if e["event_type"] == "step_finished"}
    assert {e["node"] for e in events} <= {n["id"] for n in MAP["nodes"]} | {"_run"}
    for e in events:
        if e["event_type"] == "llm_call":
            d = e["data"]
            assert d["input_tokens"] >= d.get("cache_read_tokens", 0) + d.get("cache_write_tokens", 0)
        if e["event_type"] == "decision" and "branch" in e["data"]:
            assert (e["node"], e["data"]["branch"]) in BRANCHES, e["data"]["branch"]


def test_map_is_valid():
    assert not list(TOPO.iter_errors(MAP))
    ids = {n["id"] for n in MAP["nodes"]}
    assert all(e["from"] in ids and e["to"] in ids for e in MAP["edges"])
    story_panels = {p["id"] for p in MAP["panels"] if p.get("story")}
    src = client.story()
    assert "BenchStory.register('slack-helpdesk'" in src
    assert all(f"{pid}: function" in src for pid in story_panels), "every story panel in the map needs a function"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_story_parses():
    r = subprocess.run(["node", "--check", str(REPO / "app/bench/story.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("path", REPLAYS, ids=lambda p: p.stem)
@pytest.mark.parametrize("tail", ["approved", "denied"])
def test_replays_convert(path, tail):
    events = adapter.convert_replay(path, tail)
    check_run(events)
    assert events[-1]["event_type"] == "run_finished" and events[-1]["data"].get("output")
    types = {e["event_type"] for e in events}
    assert {"retrieval", "decision", "llm_call"} <= types
    if json.loads(path.read_text()).get("paused"):
        assert {"permission_verdict", "gate_waiting", "gate_resolved"} <= types
        resolved = next(e for e in events if e["event_type"] == "gate_resolved")
        assert resolved["data"]["approved"] is (tail == "approved")


@pytest.mark.skipif(not RUNS, reason="no run logs")
def test_all_run_logs_convert():
    n = 0
    for p in RUNS:
        events = adapter.convert_jsonl(p)
        if events:
            check_run(events)
            n += 1
    assert n > 50


def test_live_stream_meta_phases():
    src = [
        {"run_id": "r1", "node": "_meta", "event_type": "phase", "data": {"phase": "started", "origin": "slack"}, "ts": 1.0},
        {"run_id": "r1", "node": "ingest", "event_type": "node_enter", "data": {"message": "hi"}, "ts": 1.1},
        {"run_id": "r1", "node": "retrieve", "event_type": "retrieval_hits", "data": {"hits": []}, "ts": 1.2},
        {"run_id": "r1", "node": "_meta", "event_type": "phase", "data": {"phase": "error", "error": "boom"}, "ts": 1.5},
    ]
    out = adapter.convert(src, session_id="s1")
    check_run(out)
    assert out[-1]["data"]["status"] == "error" and any(e["event_type"] == "error" for e in out)


def test_recordings_are_self_contained(tmp_path):
    written = recordings.build(REPO / "demo/replays", tmp_path)
    assert len(written) == len(REPLAYS) + sum(1 for p in REPLAYS if json.loads(p.read_text()).get("paused"))
    head = json.loads(written[0].read_text().splitlines()[0])
    assert head["v"] == "bench-recording/0" and head["topology"] == MAP and head["story"] == client.story()


def _close(a, b, path=""):
    """Equal, allowing the last digit of a rounded latency to differ (Python rounds half to even)."""
    if isinstance(a, dict) and isinstance(b, dict):
        assert a.keys() == b.keys(), f"{path}: keys {sorted(a)} != {sorted(b)}"
        for k in a:
            _close(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list) and isinstance(b, list):
        assert len(a) == len(b), f"{path}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            _close(x, y, f"{path}[{i}]")
    elif isinstance(a, float) or isinstance(b, float):
        assert abs(float(a) - float(b)) <= 0.11, f"{path}: {a} != {b}"
    else:
        assert a == b, f"{path}: {a!r} != {b!r}"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("path", REPLAYS, ids=lambda p: p.stem)
@pytest.mark.parametrize("tail", ["approved", "denied"])
def test_browser_adapter_matches_python(path, tail):
    """The static demo drives the bench with adapter.js; it must say exactly what adapter.py says."""
    r = subprocess.run(["node", str(REPO / "tests/bench_adapter_driver.js"), str(path), tail], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    js = json.loads(r.stdout)
    py = adapter.convert_replay(path, tail)
    _close(js, py)
