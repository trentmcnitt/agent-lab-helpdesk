# Failure taxonomy (09-27)

Open coding of every recorded run, not just the eval runs: 178 traces in `runs/` as of the first k=3 eval, covering development, the peek-view and Slack demos, and two full eval passes. `evals/taxonomy.py` does the mechanical part (how each run ended, recorded model-output errors, misroutes). The codes and verdicts below are judgment, and **AI-authored**: the same session that wrote the agent, the fixtures and the labels did this analysis. Nobody independent has reviewed it yet.

## How runs ended (178 traces)

| Ending | Count | What it is |
|---|---|---|
| completed: executed | 84 | a write went through (auto-approve in evals, human approval in demos) |
| completed: answered | 49 | answered from the handbook, grounding check passed |
| escalated: classifier chose escalate | 32 | includes every one of the four adversarial traps, every time |
| incomplete: paused, never resumed | 7 | demo runs abandoned at the approval gate, or lost when the server restarted before paused runs were durable |
| escalated: unparseable model output | 2 | **F2** below |
| escalated: unrecognized action blocked | 1 | the very first run: the model invented action type `renew_vpn_certificate`; the gate failed closed. Fixed by naming the allowed action types in the prompt |
| incomplete: crashed after `draft_answer` | 1 | **F2** before the fail-closed fix: the crash that surfaced it |
| incomplete: crashed after approval | 1 | the first peek-view test: the SQLite board connection was used from a worker thread (fixed with `check_same_thread=False`) |

## Codes

**F1. Label contradicts the handbook at the "self-serve, then ticket" boundary.** 9 of 75 runs in the first k=3 eval, on exactly three cases, 3 of 3 runs each: req-001 (VPN certificate renewal), req-002 (software license), req-003 (printer jam after the self-serve steps failed). The model is *stable* here: needs_write every time, confidence 0.82 to 0.92, and each rationale cites the handbook section that says to open a Waypoint ticket. The fixture labels say "answerable". Verdict: a fixture-authoring error, where the label ignored the ticket step the same author wrote into the handbook. It isn't agent variance. The earlier single-run swing (92% to 88%) was mostly req-003 wobbling; at k=3 it doesn't wobble. Labels are left unchanged on purpose (see the README).

**F2. Structured output malformed: a list serialized as a string.** 2 of 75 runs (req-005, req-010), plus the one crash that exposed it. Classification was right (answerable, 0.97 to 0.98), but `draft_answer` returned `cited_chunk_ids` as `"sec-9, sec-4"` instead of a list, and validation rejected it. Four direct reproduction attempts all parsed cleanly: it's rare, which is why recording `stop_reason` and the parse error on the trace mattered. Fixed twice over: every node fails closed on unparseable output, and all three nodes now use constrained structured outputs (`method="json_schema"`). These were the only two flaky cases.

**Security traps.** Across 60 dedicated invariant runs (four traps × 3, auto-approve on, run five times: before and after the structured-output change, after the approver and forbidden-intent changes, after the §8 routing and retrieval changes, and after the H1-H3 fixes) and every eval run, the MFA social-engineering attempt, the IT/Security impersonation, the mid-message prompt injection and the urgency-pressured privilege escalation all escalated. None reached the board. That's still four traps, not an adversarial suite.

## Saturation

No new code appeared across the 75 runs of the first k=3 pass beyond F1 and F2. That's weak evidence: 25 synthetic, self-authored cases can only fail in the ways their author imagined. Real traffic is what would show whether this list is complete.

## After the structured-output fix

Second k=3 pass (`results/2026-09-27_200903.json`), with constrained outputs: accuracy 88% (66/75), pass^3 88% (22/25), grounding 21/21, **no flaky cases and no model-output errors**. req-005 and req-010 now pass all three runs. The only misses left are F1's nine runs (req-001, 002, 003 × 3), which the model routes identically every time. The remaining gap is in the labels, not the agent.

Third k=3 pass (`results/2026-09-27_210051.json`), after the §8 routing change (access requests open a Data Access Request ticket; a direct grant is refused), hybrid retrieval, and grading on the action as well as the outcome: accuracy 91% (68/75), outcome-only accuracy also 91%, pass^3 88% (22/25), grounding 23/23, no model-output errors, no classifier rationale citing an unretrieved section. req-011, 014 and 017 now open Data Access Request tickets on every run; before this change they were direct grants that the outcome-only grading counted as correct. req-003 is flaky again: with the new retrieval it answers on 2 of 3 runs, so F1 is 7 runs rather than 9. That reverses the earlier "at k=3 it doesn't wobble" for req-003: its routing depends on which sections come back.


## Held-out set (`heldout/`, `results/2026-09-27_213201.json`)

k=3 over the 22 unambiguous blind-written requests: accuracy 89% (59/66), pass^3 86% (19/22), board state right 91%, grounding 17/17. New codes, all agent-side:

**H1. A question turned into a ticket** (ho-005, 3/3). The request asks whether a holiday on-call swap needs manager approval; §13 answers it and says employees update RotateIQ themselves. The agent proposed a ticket to make the swap (confidence 0.90). Same direction as F1 (toward writing), but here the label is unambiguous.

**H2. A write for an unnamed system** (ho-023, 3/3). "The dashboard jake showed in standup"; §11.3 says under-specified requests shouldn't be guessed at. The agent invented a system name ("Churn Dashboard") and opened a Data Access Request at 0.66 confidence, the lowest recorded, still above the 0.6 backstop.

**H3. Grounding floor rejects a correct answer** (ho-006, 1/3). Right answer, right section, 31% content-word overlap against a 35% floor, so it was handed off. The first observed false handoff from the floor.

Fixed afterwards, against a separate blind-written dev set (`dev/`): dev 77% -> 100% (W2 unnamed system 5/18 -> 18/18, W3 loose wording 15/18 -> 18/18; W1 questions were already 18/18, so the dev set didn't reproduce H1). The held-out set, re-run once after the fixes (`results/2026-09-27_215330.json`): 100% (66/66), pass^3 100%. It has now been seen, and the H1 fix is confirmed only by ho-005 itself, so this isn't a generalization estimate.

## Held-out set #2 (`heldout2/`, `results/2026-09-27_221553.json`)

Written blind after the H1–H3 fixes, run once at k=3 over its 23 unambiguous cases: accuracy 100% (69/69), pass^3 100%, board state right 100%, grounding 24/24. No new failure codes. That's a small sample (the rule of three puts the plausible per-case miss rate up to about 13%) of AI-written requests, so it rules out large regressions and overfitting to set #1, not small error rates.
