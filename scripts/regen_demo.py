"""Regenerates the public demo's recordings from the cassettes, with no model calls.

    uv run scripts/regen_demo.py [--fast] [--only req-012 ...] [--out DIR]

For each cassette (demo/cassettes/<scenario>.json, the only thing that cost money) it runs the
real graph: real retrieval, real checks, the real event bus and Agent Lab's real instrumentation,
with app/demo/replay_model.py answering each model call from the cassette. Everything the demo
plays comes out of those runs, so none of it can disagree with the code:

- demo/bench-recordings/<scenario>[-approved|-denied].recording.jsonl: Agent Lab recordings,
  written by the bench's own normalizer (`bench.record`, run from the bench checkout).
- demo/replays/<scenario>.json: the app page's replay (its event bus), plus `bench`: the bench
  events of the same runs, which the static page sends to the bench beside it on one clock.

A run that pauses for approval runs four times: approved and denied, each once with the
approver "the reviewer" (the bench recordings: a replay never says the viewer approved it) and
once with "You (demo)" (the app page, where the visitor is the approver). The app writes those
names itself; no text is rewritten afterwards.

Times are the original recording's: every pass is moved so its run starts at the cassette's `t0`,
the bus events and the bench events by the same amount, and the graph's clock (the time an
approval is stamped with, which the reply quotes) runs on that same timeline, so the reply and
the recorded approval say the same time. The original runs resumed the moment they paused, so a
recording's "waiting for a person" is under a second: that is what happened, not a gap to fill. Without --fast each model call takes its
recorded time (about 1-2 minutes in all); --fast skips the waits (tests).

A prompt the cassette never recorded stops everything with "stale: ..." and a non-zero exit.
The fix is a new recording, a paid model call: never pair a changed prompt with an old answer.
The bench checkout is found through AGENT_LAB_BENCH_DIR (default ../agent-lab, a clone of trentmcnitt/agent-lab beside this repo)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentlab.testing import capture, to_otlp_json  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app import redact  # noqa: E402
from app.agent_lab import APP_ID, PRICE_BASIS, STORY_FILE, price  # noqa: E402
from app.board_adapter import BoardAdapter  # noqa: E402
from app.demo.engine import VISITOR  # noqa: E402
from app.demo.replay_model import ReplayModel, StalePrompt  # noqa: E402
from app.events import EventBus  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.retrieval import HandbookIndex  # noqa: E402

CASSETTES = ROOT / "demo" / "cassettes"
BENCH = Path(os.environ.get("AGENT_LAB_BENCH_DIR", ROOT.parent / "agent-lab")).resolve()
# The approver in a bench recording: someone at recording time, never the person watching it.
REVIEWER = {**VISITOR, "id": "recorded-reviewer", "name": "the reviewer"}
DENIED = " (a person says no)"
# The zone the demo's runs were recorded in (09-29-26, Wisconsin), so the time a reply quotes is the
# same whichever machine regenerates it.
RECORDED_TZ = ZoneInfo("America/Chicago")


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def run_pass(cassette: dict, index: HandbookIndex, tail: str | None, approver: dict, fast: bool) -> dict:
    """One run of the real graph on the cassette: its bus events (moved to the cassette's clock),
    its OTLP spans, and what the visitor would see. `tail` picks the resume for a paused run."""
    run_id = cassette["run_id"]
    events: list[dict] = []
    bus = EventBus(run_id=run_id)
    bus.subscribe(lambda ev: events.append(ev.to_dict()))
    board = BoardAdapter(Path(":memory:"))
    # The recording's clock: the cassette's t0 when the run starts, then real time (the shift below
    # moves every event the same way, so an approval stamped by this clock matches its event's ts).
    began = [time.time()]

    def clock() -> datetime:
        return datetime.fromtimestamp(cassette["t0"] + (time.time() - began[0]), RECORDED_TZ)

    with capture(redact=redact.redact if redact.enabled() else None, price=price, price_basis=PRICE_BASIS,
                 service_name=APP_ID) as spans:
        graph = build_graph(bus, index, board, checkpointer=InMemorySaver(),
                            llm=ReplayModel(cassette=cassette, fast=fast), clock=clock)
        cfg = {"configurable": {"thread_id": run_id}}
        began[0] = time.time()
        result = graph.invoke(dict(cassette["input"]), config=cfg)
        paused = "__interrupt__" in result
        if paused != bool(cassette.get("resumes")):
            raise SystemExit(f"{cassette['scenario']}: the run {'paused' if paused else 'did not pause'} for approval, "
                             "unlike the recording")
        out: dict = {"paused": paused}
        if paused:
            payload = result["__interrupt__"][0].value
            # What the server does when it sees the pause (server.py, app/demo/engine.py).
            bus.publish(node="approval_gate", event_type="approval_requested",
                        proposed_action=payload["proposed_action"], action_digest=payload["action_digest"])
            out["split"] = len(events)
            out["action_digest"] = payload["action_digest"]
            result = graph.invoke(Command(resume={**cassette["resumes"][tail], "approver": approver,
                                                  "action_digest": payload["action_digest"]}), config=cfg)
            out["tickets"] = board.list_tickets()
    start = min(s.start_time for s in spans if s.attributes.get("agentlab.kind") == "run") / 1e9
    shift = cassette["t0"] - start
    for ev in events:
        ev["ts"] = round(ev["ts"] + shift, 6)
    out.update(events=events, final=result.get("final_response", ""), otlp=to_otlp_json(spans))
    return out


def bench_record(otlp: dict, out: Path, name: str, cassette: dict, regen_id: str, title: str | None) -> list[dict]:
    """The bench's normalizer over one pass's spans; returns the recording's rows (header first)."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "spans.json"
        src.write_text(json.dumps(otlp))
        cmd = ["uv", "run", "--quiet", "--project", str(BENCH), "python", "-m", "bench.record", "--otlp", str(src),
               "--out", str(out), "--name", name, "--stories", f"{APP_ID}={STORY_FILE}", "--t0", repr(cassette["t0"]),
               "--regen-id", regen_id]
        if title:
            cmd += ["--title", title]
        if cassette.get("group"):
            cmd += ["--group", cassette["group"]]
        r = subprocess.run(cmd, cwd=BENCH, capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"bench.record failed for {name}:\n{r.stdout}{r.stderr}")
    path = out / f"{name}.recording.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _manifest_hash(otlp: dict) -> str:
    for rs in otlp.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                for attr in span.get("attributes", []):
                    if attr["key"] == "agentlab.manifest.hash":
                        return attr["value"]["stringValue"]
    raise SystemExit("no agentlab.manifest.hash in the spans: is the graph instrumented?")


def regen(cassette: dict, index: HandbookIndex, out: Path, fast: bool) -> list[str]:
    scenario = cassette["scenario"]
    recordings, replays = out / "bench-recordings", out / "replays"
    recordings.mkdir(parents=True, exist_ok=True)
    replays.mkdir(parents=True, exist_ok=True)
    tails = ("approved", "denied") if cassette.get("resumes") else (None,)
    shown = {t: run_pass(cassette, index, t, VISITOR, fast) for t in tails}          # the app page's runs
    first = shown[tails[0]]
    # One id per regeneration input: the cassette and the map its runs carried (code + words + corpora).
    regen_id = hashlib.sha256(_canon(cassette) + _manifest_hash(first["otlp"]).encode()).hexdigest()[:16]

    written = []
    replay = {"scenario": scenario, "requester_name": cassette["input"]["requester_name"],
              "message": cassette["input"]["message"],
              "recorded_at": datetime.fromtimestamp(cassette["t0"]).isoformat(timespec="seconds"),
              "model": cassette["calls"][0]["model"] if cassette["calls"] else None,
              "paused": first["paused"], "regen_id": regen_id}
    with tempfile.TemporaryDirectory() as tmp:
        for t in tails:
            # The bench recording: the same run, with the recording's reviewer as the approver.
            rec = first if t is None else run_pass(cassette, index, t, REVIEWER, fast)
            name = scenario if t is None else f"{scenario}-{t}"
            rows = bench_record(rec["otlp"], recordings, name, cassette, regen_id,
                                cassette["title"] + (DENIED if t == "denied" else ""))
            written.append(name)
            # The app page's run (with no approval, the same run): its bench events, for the static page.
            # Both endings under the scenario's own name: the page plays one run, the pause then one ending.
            shown[t]["bench"] = rows[1:] if t is None else \
                bench_record(shown[t]["otlp"], Path(tmp), scenario, cassette, regen_id, None)[1:]

    if not first["paused"]:
        replay.update(pre=first["events"], final=first["final"], bench={"events": first["bench"]})
    else:
        pre = first["events"][:first["split"]]
        for t in tails:  # the two runs are the same up to the pause, or the page couldn't share one
            other = shown[t]["events"][:shown[t]["split"]]
            if [(e["node"], e["event_type"], e["data"]) for e in other] != [(e["node"], e["event_type"], e["data"]) for e in pre]:
                raise SystemExit(f"{scenario}: the approved and denied runs differ before the pause")
        cut = next(i for i, e in enumerate(first["bench"]) if e["event_type"] == "gate_waiting") + 1
        replay.update(
            pre=pre, action_digest=first["action_digest"],
            tails={t: {"events": shown[t]["events"][shown[t]["split"]:], "final": shown[t]["final"],
                       "tickets": shown[t]["tickets"] if t == "approved" else []} for t in tails},
            bench={"pre": first["bench"][:cut], "tails": {t: shown[t]["bench"][cut:] for t in tails}},
        )
    (replays / f"{scenario}.json").write_text(json.dumps(replay, indent=1, default=str, ensure_ascii=False) + "\n")
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fast", action="store_true", help="skip the recorded model-call waits")
    ap.add_argument("--only", nargs="*", help="scenario ids (default: every cassette)")
    ap.add_argument("--out", default=str(ROOT / "demo"), help="writes <out>/replays and <out>/bench-recordings")
    a = ap.parse_args(argv)
    if not (BENCH / "bench" / "record.py").exists():
        raise SystemExit(f"the bench checkout isn't at {BENCH} (set AGENT_LAB_BENCH_DIR)")
    index = HandbookIndex()
    out = Path(a.out)
    for path in sorted(CASSETTES.glob("*.json")):
        cassette = json.loads(path.read_text())
        if a.only and cassette["scenario"] not in a.only:
            continue
        try:
            names = regen(cassette, index, out, a.fast)
        except StalePrompt as e:
            print(e, file=sys.stderr)
            return 1
        print(f"{cassette['scenario']}: {', '.join(names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
