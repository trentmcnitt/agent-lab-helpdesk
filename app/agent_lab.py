"""This app on Agent Lab: the app-level facts, the story, and `init()`.

Everything else Agent Lab shows comes from the code where it happens:
- the steps, branches and their names from the compiled graph (`instrument` in graph.py);
- each step's plain words from `@lab.step` on its node function, and its Engineering description
  from the function's docstring;
- the handbook's sections from `HandbookIndex` (`lab.corpus`), the never list from
  permissions.py (`lab.never`), the one action from board_adapter.py;
- the facts (what the search returned, the decision, each check, the approval) from one
  `lab.*` line where each is produced.

`lab.verify` in tests/test_agent_lab.py fails when any of the words stops matching the code.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import agentlab as lab

from . import config, permissions, redact
from .board_adapter import CREATE_TICKET

APP_ID = "slack-helpdesk"
STORY_FILE = Path(__file__).resolve().parent / "bench" / "story.js"
PRICE_BASIS = "request-queue price table (app/config.py)"

# The baseline, by kind of request: config.MANUAL_ESTIMATE says how long each takes by hand.
_BASELINE_KIND = {"answerable": "answer", "needs_write": "ticket", "escalate": "hand-off"}


def baseline() -> str:
    """'answer: ~3–5 min · ticket: ~10–15 min · hand-off: human judgment required', from
    config.MANUAL_ESTIMATE (each estimate up to its first comma; the web UI shows the rest)."""
    return " · ".join(f"{_BASELINE_KIND.get(k, k)}: {v.removeprefix('manual: ').split(',')[0]}"
                      for k, v in config.MANUAL_ESTIMATE.items())


def track_record(results: Path = config.REPO_ROOT / "evals" / "results") -> str | None:
    """The newest held-out eval run, in plain words, read from its results file."""
    runs = []
    for p in results.glob("*.json"):
        try:
            r = json.loads(p.read_text())
        except ValueError:
            continue
        if "heldout" in str(r.get("requests_file", "")) and r.get("cases") and r.get("k"):
            runs.append((str(r["date"]), r))
    if not runs:
        return None
    _, r = max(runs, key=lambda x: x[0])
    total = r["cases"] * r["k"]
    right = round(r["accuracy"] * total)
    when = datetime.fromisoformat(r["date"]).strftime("%m-%d-%y")
    every = "every time" if right == total else f"{right} times"
    return (f"The test requests were written by AI, not yet by people. On {r['cases']} of them it had never seen, "
            f"written separately, it chose the right path {every}, {r['k']} tries each ({right} of {total}; "
            f"evals run {when}).")


APP = lab.App(
    name="Slack Helpdesk Agent",
    id=APP_ID,
    description=("An IT helpdesk assistant in Slack for a fictional company. It answers from the company handbook, "
                 "opens a ticket when something needs changing (a person approves first), and hands everything else "
                 "to a person."),
    privacy_note=("Passwords, keys, emails and phone numbers are hidden on this screen. "
                  "The AI itself saw the original message."),
    track_record=track_record(),
    baseline=baseline(),
    never=permissions.NEVER,
    actions=(CREATE_TICKET,),
    # The state fields a person reads (graph.RequestState); lab.verify checks they exist (R15).
    request="message",
    reply="final_response",
    requester=("requester_name", "requester_role"),
)

# The story's panels. The panel functions are in story.js; each draws Presentation and Engineering.
STORY = lab.Story(
    file=STORY_FILE,
    panels=(
        # Engineering only: Presentation's callout shows every source item, what was given and what
        # was relied on, the same for every app; this adds the fused and BM25 scores.
        lab.Panel("retrieval", "Documents the model was given · handbook sections, best match first",
                  ["retrieval"], audience="engineering"),
        lab.Panel("classify", "Classification", ["decision", "check_result", "triage"], nodes=["classify"],
                  plain_title="The path it picked", mode="append"),
        lab.Panel("grounding", "Grounding check", ["check_result"], nodes=["grounding_check"],
                  plain_title="Checking the answer against the handbook"),
        lab.Panel("permission", "Permission gate", ["permission_verdict"], nodes=["permission_check"],
                  plain_title="What the rules said"),
        # Engineering only: Presentation's callout already shows the exact proposal and who decided.
        lab.Panel("gate", "Approval (decided in the app)", ["gate_waiting", "gate_resolved"],
                  nodes=["approval_gate"], mode="append", audience="engineering"),
    ),
)


def price(model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> float:
    """Agent Lab passes the total input; config.cost_for takes the uncached part."""
    return round(config.cost_for(model, input_tokens - cache_read - cache_write, output_tokens,
                                 cache_read=cache_read, cache_write=cache_write), 6)


def init() -> None:
    """Once at startup. Sends to the local bench (http://127.0.0.1:8790) unless AGENT_LAB_URL says
    otherwise; a silent no-op when nothing is listening. Content is masked like the app's own
    traces unless TRACE_CONTENT=full."""
    lab.init(service_name=APP_ID, price=price, price_basis=PRICE_BASIS,
             redact=redact.redact if redact.enabled() else None)
