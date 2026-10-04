"""This app on Agent Lab. Nothing Agent Lab shows is typed twice, so these tests check the few
things written by hand against the code, and the demo's recordings against a fresh run:

- the words (`@lab.step`, path words, the never list, the story's node ids) match the graph,
  and no worded step changed since someone last confirmed its words (`lab.verify`, strict);
- the map Agent Lab derives from the graph is the one pinned here (a deliberate change to the
  graph changes this test, and nothing else needs editing);
- every model call and fact lands on its own step, with or without another handler on the run;
- the checked-in recordings are what scripts/regen_demo.py makes from the cassettes today, they
  validate against the bench's schemas, and the story renders them in both modes.

The bench checkout beside this repo (AGENT_LAB_BENCH_DIR) supplies the schemas, the normalizer
and the story harness; the tests that need it skip without it."""
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import agentlab as lab
import pytest
from agentlab.manifest import find
from agentlab.testing import capture
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app import config, redact
from app.agent_lab import APP_ID, PRICE_BASIS, price
from app.board_adapter import BoardAdapter
from app.demo.engine import VISITOR
from app.demo.replay_model import ReplayModel, StalePrompt
from app.events import EventBus
from app.graph import build_graph, lab_graph
from app.retrieval import CORPUS_ID, HandbookIndex
from scripts.regen_demo import RECORDED_TZ

REPO = Path(__file__).resolve().parents[1]
BENCH = Path(os.environ.get("AGENT_LAB_BENCH_DIR", REPO.parent / "agent-lab")).resolve()
CASSETTES = sorted((REPO / "demo/cassettes").glob("*.json"))
RECORDINGS = sorted((REPO / "demo/bench-recordings").glob("*.recording.jsonl"))
HAS_BENCH = (BENCH / "bench" / "record.py").exists()
needs_bench = pytest.mark.skipif(not HAS_BENCH, reason=f"bench checkout not found at {BENCH}")
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


def _manifest(graph) -> dict:
    return json.loads(find(graph).manifest_doc().json)


# ---- the words and the map -----------------------------------------------------------------------

def test_agent_lab_words_match_code():
    """Every word written for Agent Lab names a real step or branch, the never list has one line per
    forbidden type, and no worded step's code changed since its words were last confirmed. After a
    deliberate change: re-read the words, then `uv run python -m agentlab lock app.graph:lab_graph`."""
    lab.verify(lab_graph(), strict=True)


def test_the_map_is_derived_from_the_graph():
    m = _manifest(lab_graph())
    assert [n["id"] for n in m["nodes"]] == ["ingest", "retrieve", "classify", "draft_answer", "grounding_check",
                                             "propose_action", "permission_check", "approval_gate", "execute_action",
                                             "handoff", "respond"]
    edges = sorted((e["from"], e.get("from_branch"), e["to"]) for e in m["edges"])
    assert edges == sorted([
        ("ingest", None, "retrieve"), ("retrieve", None, "classify"),
        ("classify", "answerable", "draft_answer"), ("classify", "needs_write", "propose_action"),
        ("classify", "escalate", "handoff"),
        ("draft_answer", None, "grounding_check"),
        ("grounding_check", "grounded", "respond"), ("grounding_check", "not_grounded", "handoff"),
        ("propose_action", None, "permission_check"),
        ("permission_check", "allowed", "approval_gate"), ("permission_check", "forbidden", "handoff"),
        ("approval_gate", "approved", "execute_action"), ("approval_gate", "denied", "handoff"),
        ("execute_action", None, "respond"), ("handoff", None, "respond"),
    ])
    kinds = {n["id"]: (n["kind"], n.get("actor")) for n in m["nodes"]}   # actor only where something says who
    assert kinds["classify"] == ("llm", "ai") and kinds["approval_gate"] == ("gate", "person")
    assert {k for k, (kind, _) in kinds.items() if kind == "check"} == {"grounding_check", "permission_check"}
    assert kinds["respond"][0] == "terminal"
    # Every step and every branch carries plain words, and every step its code's own description.
    assert all(n.get("plain_label") and n.get("description") and n.get("doc") for n in m["nodes"]), \
        [n["id"] for n in m["nodes"] if not (n.get("plain_label") and n.get("description") and n.get("doc"))]
    assert all(e.get("plain_label") for e in m["edges"] if e.get("from_branch"))


def test_sources_never_and_actions_come_from_the_code():
    m = _manifest(lab_graph())
    index = HandbookIndex(mode="bm25")
    (handbook,) = [s for s in m["sources"] if s["id"] == CORPUS_ID]
    assert handbook["items"] == [{"id": c.chunk_id, "title": c.section} for c in index.chunks]
    assert handbook["count"] == len(index.chunks) and handbook["title"] == index.title
    assert len(m["never"]) == len(config.FORBIDDEN_ACTION_TYPES)
    assert [a["id"] for a in m["actions"]] == ["create_ticket"]
    assert m["app"]["id"] == APP_ID and m["app"]["baseline"] and m["app"]["track_record"]
    assert m["story"]["sha256"]
    # Story panels name real nodes (verify checks it too); the three generic panels are added.
    assert {p["id"] for p in m["panels"]} >= {"retrieval", "classify", "grounding", "permission", "gate",
                                               "llm", "tools", "errors"}


def test_a_split_handbook_is_what_the_map_shows(tmp_path):
    """Renumber or split the handbook and the sources follow, with nothing else edited."""
    text = config.HANDBOOK_PATH.read_text() + "\n## 16. A section added later\nNew policy text.\n"
    handbook = tmp_path / "handbook.md"
    handbook.write_text(text)
    graph = build_graph(EventBus(run_id="t"), HandbookIndex(handbook, mode="bm25"), BoardAdapter(Path(":memory:")),
                        llm=ReplayModel(cassette={"calls": []}))
    items = next(s for s in _manifest(graph)["sources"] if s["id"] == CORPUS_ID)["items"]
    assert items[-1] == {"id": f"sec-{len(items) - 1}", "title": "16. A section added later"}
    HandbookIndex(mode="bm25")  # put the real handbook back for the tests that follow


# ---- runs: every fact on its own step ----------------------------------------------------------

class _Recorder(BaseCallbackHandler):
    """Stands in for another handler on the run (e.g. Langfuse's)."""

    def __init__(self):
        self.chat_nodes = []

    def on_chat_model_start(self, serialized, messages, *, metadata=None, **kwargs):
        self.chat_nodes.append((metadata or {}).get("langgraph_node"))


def _run(scenario: str, *, approve: bool | None = True, callbacks=None):
    cassette = json.loads((REPO / "demo/cassettes" / f"{scenario}.json").read_text())
    with capture(redact=redact.redact, price=price, price_basis=PRICE_BASIS, service_name=APP_ID) as spans:
        graph = build_graph(EventBus(run_id=cassette["run_id"]), HandbookIndex(), BoardAdapter(Path(":memory:")),
                            checkpointer=InMemorySaver(), llm=ReplayModel(cassette=cassette, fast=True))
        cfg = {"configurable": {"thread_id": cassette["run_id"]}, **({"callbacks": callbacks} if callbacks else {})}
        result = graph.invoke(dict(cassette["input"]), config=cfg)
        if "__interrupt__" in result and approve is not None:
            digest = result["__interrupt__"][0].value["action_digest"]
            graph.invoke(Command(resume={"approved": approve, "approver": VISITOR, "action_digest": digest}), config=cfg)
    return spans


@pytest.mark.parametrize("with_other_handler", [False, True])
def test_every_model_call_is_on_its_node(with_other_handler):
    """A11 check 1: Agent Lab sees every model call, on the node that made it, whether or not another
    handler (Langfuse's) is on the run. An in-node `callbacks=` would hide them (graph.py _lf_config)."""
    other = _Recorder()
    spans = _run("req-012", callbacks=[other] if with_other_handler else None)
    chats = [s.attributes.get("agentlab.node") for s in spans if s.attributes.get("agentlab.kind") == "chat"]
    assert chats == ["classify", "propose_action"]
    if with_other_handler:
        assert other.chat_nodes == ["classify", "propose_action"]
    for s in spans:
        if s.attributes.get("agentlab.kind") == "chat":
            assert s.attributes["agentlab.cost.usd"] > 0 and s.attributes["agentlab.cost.basis"] == PRICE_BASIS
            assert s.attributes.get("agentlab.request.json_schema")


def test_langfuse_on_the_graph_and_agent_lab_both_see_every_model_call(monkeypatch):
    """The production wiring: build_graph puts Langfuse's handler on the compiled graph, then
    instrument() adds Agent Lab's. Both must see every model call, each on its node."""
    other = _Recorder()
    monkeypatch.setattr("app.graph.get_langfuse_handler", lambda: other)
    spans = _run("req-012")
    assert other.chat_nodes == ["classify", "propose_action"]
    assert [s.attributes.get("agentlab.node") for s in spans
            if s.attributes.get("agentlab.kind") == "chat"] == ["classify", "propose_action"]


def test_facts_land_on_their_steps():
    spans = _run("req-012")
    events = {(s.attributes.get("agentlab.node"), e.name) for s in spans for e in s.events}
    assert {("classify", "agentlab.decision"), ("classify", "agentlab.check"), ("classify", "agentlab.event"),
            ("permission_check", "agentlab.check"), ("permission_check", "agentlab.event"),
            ("approval_gate", "agentlab.gate.waiting"), ("approval_gate", "agentlab.gate.resolved")} <= events
    retrieval = next(s for s in spans if s.attributes.get("agentlab.kind") == "retrieval")
    assert retrieval.attributes["agentlab.node"] == "retrieve"
    assert retrieval.attributes["gen_ai.data_source.id"] == CORPUS_ID
    run = [s for s in spans if s.attributes.get("agentlab.kind") == "run"]
    assert [s.attributes.get("agentlab.run.status") for s in run] == ["paused", "ok"]
    assert run[-1].attributes["agentlab.run.outcome"] == "executed"


def test_a_handed_off_run_has_its_outcome():
    spans = _run("req-019")
    (run,) = [s for s in spans if s.attributes.get("agentlab.kind") == "run"]
    assert run.attributes["agentlab.run.outcome"] == "escalated"


def test_a_changed_prompt_is_refused_not_paired_with_an_old_answer():
    cassette = json.loads((REPO / "demo/cassettes/req-019.json").read_text())
    cassette["input"] = {**cassette["input"], "message": cassette["input"]["message"] + " (edited)"}
    graph = build_graph(EventBus(run_id="t"), HandbookIndex(), BoardAdapter(Path(":memory:")),
                        checkpointer=InMemorySaver(), llm=ReplayModel(cassette=cassette, fast=True))
    with pytest.raises(StalePrompt, match="stale: prompt for classify changed"):
        graph.invoke(dict(cassette["input"]), config={"configurable": {"thread_id": "t"}})


# ---- the demo's recordings -------------------------------------------------------------------------

_VARIES = re.compile(r"REQ-[0-9a-f]{6}|\b\d{1,2}:\d{2} [AP]M\b|\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")


def _stable(obj, key=None):
    """A recording with what legitimately changes between two regenerations taken out: times,
    latencies, ticket ids and the approval's clock time ("Approved by ... at 3:12 PM", its ISO record)."""
    if isinstance(obj, dict):
        return {k: _stable(v, k) for k, v in obj.items() if k not in ("ts", "latency_ms", "created_at")}
    if isinstance(obj, list):
        return [_stable(v) for v in obj]
    if isinstance(obj, str):
        return _VARIES.sub("<varies>", obj)
    return obj


@needs_bench
def test_recordings_are_what_the_cassettes_make_today(tmp_path):
    """Each checked-in recording and replay is what scripts/regen_demo.py writes from the cassettes
    with today's code (every cassette key still matches its prompt, or regen stops as stale)."""
    r = subprocess.run([sys.executable, str(REPO / "scripts/regen_demo.py"), "--fast", "--out", str(tmp_path)],
                       cwd=REPO, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    for kind, pattern in (("bench-recordings", "*.recording.jsonl"), ("replays", "*.json")):
        fresh = sorted((tmp_path / kind).glob(pattern))
        assert [p.name for p in fresh] == [p.name for p in sorted((REPO / "demo" / kind).glob(pattern))]
        for p in fresh:
            read = (lambda f: [json.loads(x) for x in f.read_text().splitlines() if x.strip()]) \
                if kind == "bench-recordings" else (lambda f: json.loads(f.read_text()))
            assert _stable(read(p)) == _stable(read(REPO / "demo" / kind / p.name)), \
                f"stale: {kind}/{p.name}; run uv run scripts/regen_demo.py"


@needs_bench
@pytest.mark.parametrize("path", RECORDINGS, ids=lambda p: p.name.split(".")[0])
def test_recordings_validate_and_tell_the_run(path):
    jsonschema = pytest.importorskip("jsonschema")
    event_schema = jsonschema.Draft202012Validator(json.loads((BENCH / "schema/bench-event.schema.json").read_text()))
    map_schema = jsonschema.Draft202012Validator(json.loads((BENCH / "schema/bench-topology.schema.json").read_text()))
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    head, events = rows[0], rows[1:]
    assert not list(map_schema.iter_errors(head["topology"]))
    assert head["title"] and head["group"] and head["regen_id"]
    assert head["story"] == (REPO / "app/bench/story.js").read_text()
    for e in events:
        errs = sorted(event_schema.iter_errors(e), key=str)
        assert not errs, f"{e['event_type']}@{e['node']}: {errs[0].message}"
    assert [e["seq"] for e in events] == list(range(len(events)))
    assert events[0]["event_type"] == "run_started" and events[-1]["event_type"] == "run_finished"
    nodes = {n["id"] for n in head["topology"]["nodes"]}
    assert {e["node"] for e in events} <= nodes | {"_run"}
    types = {(e["node"], e["event_type"]) for e in events}
    assert {("retrieve", "retrieval"), ("classify", "llm_call"), ("classify", "decision"),
            ("classify", "check_result")} <= types
    name = path.name.split(".")[0]
    if name.endswith(("-approved", "-denied")):
        assert {("permission_check", "check_result"), ("approval_gate", "gate_waiting"),
                ("approval_gate", "gate_resolved")} <= types
        resolved = next(e["data"] for e in events if e["event_type"] == "gate_resolved")
        assert resolved["approved"] is name.endswith("-approved")
        assert resolved["by"] == "the reviewer"
        if name.endswith("-approved"):
            # The reply quotes the approval's time; it is the recorded approval's own time.
            at = next(e["ts"] for e in events if e["event_type"] == "gate_resolved")
            reply = events[-1]["data"]["output"]["final_response"]
            stamped = datetime.fromtimestamp(at, RECORDED_TZ).strftime('%-I:%M %p')   # the zone regen stamps in
            assert f"Approved by the reviewer at {stamped}." in reply
    # A recording never says the person watching it approved anything.
    assert "You (demo)" not in json.dumps(events, ensure_ascii=False)
    # Retrieved sections are found inside the prompt (the bench's "given to the AI").
    hits = [h for e in events if e["event_type"] == "retrieval" for h in e["data"]["hits"]]
    call = next(e["data"] for e in events if e["event_type"] == "llm_call" and e["node"] == "classify")
    prompt = (call.get("system") or "") + "\n".join(str(m["content"]) for m in call["messages"])
    assert hits and all(h["text"] in prompt for h in hits)


@needs_node
def test_story_parses():
    r = subprocess.run(["node", "--check", str(REPO / "app/bench/story.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@needs_node
def test_story_renders_every_panel_in_both_modes():
    """Every story panel at every event of every recording, Presentation and Engineering: no throw, no
    empty result in Engineering (in Presentation a panel may have nothing to add), no
    undefined/NaN/[object Object], and no scores, ids or confidence in Presentation."""
    r = subprocess.run(["node", str(REPO / "tests/story_driver.js"), *map(str, RECORDINGS)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == []


@needs_node
@needs_bench
def test_story_passes_the_bench_harness():
    """The bench's own harness (its real ctx.h helpers and reduce()) over every recording, both modes."""
    r = subprocess.run(["node", str(BENCH / "tests/story_harness.js"), "--story", str(REPO / "app/bench/story.js"),
                        *map(str, RECORDINGS)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
