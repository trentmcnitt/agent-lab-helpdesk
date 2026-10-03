"""Mechanical first pass of the failure taxonomy: classify how every recorded
run ended (runs/*.jsonl), and pull misroutes from the latest k-run eval.
The codes are assigned by pattern, not by a human; the judgment calls live in
evals/failure_taxonomy.md.

    uv run evals/taxonomy.py
"""
from __future__ import annotations

import collections
import glob
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HANDOFF_CODES = [
    (r"Low confidence", "low_confidence_override"),
    (r"couldn't be read", "unparseable_model_output"),
    (r"never permitted", "forbidden_action_blocked"),
    (r"Unrecognized action type", "unrecognized_action_blocked"),
    (r"didn't match the exact action|changed after it was approved", "approval_digest_mismatch"),
    (r"Cited chunk|No citations|Low lexical overlap", "grounding_check_failed"),
    (r"^Escalated for human review\.$", "classifier_chose_escalate"),
]


def ending(events: list[dict]) -> str:
    if not events:
        return "empty_trace"
    kinds = [e["event_type"] for e in events]
    for e in reversed(events):
        if e["event_type"] == "handoff":
            reason = e["data"].get("reason", "")
            for pattern, code in HANDOFF_CODES:
                if re.search(pattern, reason):
                    return f"escalated:{code}"
            if any(e2["event_type"] == "approval_result" and not e2["data"].get("approved") for e2 in events):
                return "escalated:human_denied"
            return "escalated:other"
        if e["event_type"] == "respond":
            return f"completed:{e['data'].get('outcome')}"
    if kinds[-1] == "approval_requested":
        return "incomplete:paused_never_resumed"
    return f"incomplete:stopped_after_{events[-1]['node']}.{kinds[-1]}"


def main() -> None:
    endings = collections.Counter()
    examples: dict[str, list[str]] = collections.defaultdict(list)
    parse_errors = []
    for path in sorted(glob.glob(str(ROOT / "runs" / "*.jsonl"))):
        events = [json.loads(line) for line in open(path) if line.strip()]
        code = ending(events)
        endings[code] += 1
        examples[code].append(Path(path).stem)
        parse_errors += [(Path(path).stem, e["node"], e["data"]) for e in events if e["event_type"] == "model_output_error"]

    print(f"## Run endings ({sum(endings.values())} traces)\n")
    for code, n in endings.most_common():
        print(f"- `{code}`: {n}  (e.g. {', '.join(examples[code][:2])})")

    print(f"\n## Unparseable model output ({len(parse_errors)})\n")
    for run, node, data in parse_errors:
        print(f"- {run} · {node} · stop_reason={data.get('stop_reason')} · {data.get('parsing_error', '')[:160]}")

    results = sorted(glob.glob(str(ROOT / "evals" / "results" / "*.json")))
    k_runs = [r for r in results if "k" in json.loads(Path(r).read_text())]
    if not k_runs:
        sys.exit(0)
    latest = json.loads(Path(k_runs[-1]).read_text())
    print(f"\n## Misroutes in {Path(k_runs[-1]).name} (k={latest['k']})\n")
    for case in latest["results"]:
        for run in case["runs"]:
            if not run["correct"]:
                print(f"- {case['id']} expected {case['expected_outcome']}, got {run['actual_outcome']} "
                      f"(classify: {run['original_category']} @ {run['confidence']}) -- {(run['rationale'] or '')[:180]}")


if __name__ == "__main__":
    main()
