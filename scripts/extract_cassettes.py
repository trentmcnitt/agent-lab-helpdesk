"""Builds demo/cassettes/*.json from recorded demo runs: once from demo/replays/*.json as they were
recorded on 09-29-26, and from then on through scripts/record_cassettes.py, which reuses
`cassette()`. scripts/regen_demo.py then remakes everything else from the cassettes with no
model calls.

    uv run scripts/extract_cassettes.py [--replays demo/replays] [--out demo/cassettes]

A cassette keeps only what cost money: each model call's node, key, model, output, token usage
and latency. `latency_ms` is the gap before the call's llm_call event in the recording (the bus
publishes when the model returns), labelled as such in the cassette. The finish reason wasn't
recorded, so it is null. Don't run it over regenerated replays: they would only reproduce the
cassettes they came from."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.demo.replay_model import CASSETTE_V, call_key  # noqa: E402
from cli import load_seed_requests  # noqa: E402

# The plain run picker: each recording's request in plain words, and what kind of run it is.
# Groups in picker order; a ticket request's denied run sits beside its approved one.
GROUPS = ("It answers", "It opens a ticket (a person approves)", "It hands it to a person", "Trick requests")
TITLES = {
    "req-005": (GROUPS[0], "Can I work from a coffee shop on VPN?"),
    "req-009": (GROUPS[0], "My VPN keeps dropping, what should I check?"),
    "req-012": (GROUPS[1], "Can I get a JetBrains license?"),
    "req-011": (GROUPS[1], "I need read access to the Compass analytics dashboard"),
    "req-019": (GROUPS[2], "I lost my phone, can you turn off two-factor?"),
    "req-020": (GROUPS[3], "Someone claiming to be Security asks for a coworker's password"),
    "req-021": (GROUPS[3], "A normal request with 'ignore your instructions' hidden inside"),
    "req-024": (GROUPS[3], "Urgent admin access, 'my manager already said it's fine'"),
}


def cassette(rec: dict, req: dict) -> dict:
    scenario = rec["scenario"]
    run_id = f"rec-{scenario}"
    calls, prev = [], None
    for ev in rec["pre"]:
        if ev["event_type"] == "llm_call":
            d = ev["data"]
            schema = (d.get("params") or {}).get("json_schema")
            calls.append({
                "node": ev["node"],
                "key": call_key(ev["node"], d.get("system"), d["messages"], schema),
                "model": d.get("model") or rec.get("model"),
                "output": d["output"],
                "usage": {"input_tokens": d.get("input_tokens", 0), "output_tokens": d.get("output_tokens", 0),
                          "cache_read": d.get("cache_read_tokens", 0), "cache_write": d.get("cache_write_tokens", 0)},
                "latency_ms": round((ev["ts"] - prev) * 1000, 1) if prev is not None else None,
                "finish_reason": None,
            })
        prev = ev["ts"]
    group, title = TITLES.get(scenario, (None, req["message"][:80]))
    return {
        "v": CASSETTE_V,
        "scenario": scenario,
        "title": title,
        "group": group,
        "recorded": datetime.fromisoformat(rec["recorded_at"]).strftime("%m-%d-%y"),
        "t0": rec["pre"][0]["ts"],
        "latency_basis": "the gap before each call's llm_call event in the original recording",
        "input": {"run_id": run_id, "request_id": scenario, "requester_name": req["requester_name"],
                  "requester_role": req["requester_role"], "requester_id": None, "channel": req["channel"],
                  "thread_ts": "0", "message": req["message"], "auto_approve": False},
        "run_id": run_id,
        "resumes": {"approved": {"approved": True}, "denied": {"approved": False}} if rec.get("paused") else None,
        "calls": calls,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replays", default=str(ROOT / "demo/replays"))
    ap.add_argument("--out", default=str(ROOT / "demo/cassettes"))
    a = ap.parse_args()
    requests = {r["id"]: r for r in load_seed_requests()}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for p in sorted(Path(a.replays).glob("*.json")):
        rec = json.loads(p.read_text())
        c = cassette(rec, requests[rec["scenario"]])
        (out / f"{c['scenario']}.json").write_text(json.dumps(c, indent=1, ensure_ascii=False) + "\n")
        print(f"{c['scenario']}: {len(c['calls'])} model calls")


if __name__ == "__main__":
    main()
