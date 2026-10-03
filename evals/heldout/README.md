# Held-out requests (written blind)

`requests.json`: 24 requests with expected category, action, board state and handbook section, plus the writer's reasoning for each label.

## Who wrote them, and what they could see

A separate Claude session (Opus), started fresh on 09-27. It was given a copy of the handbook, version 1 (`data/handbook_v1.md`), in an otherwise empty folder, told to read nothing else, and given the one-paragraph job description below. It did not see the agent's prompts, its code, or the 25 requests in `data/seed_requests.json`. It made three tool calls: read the handbook, write the file, check the file parses. Like every subagent in the environment it was built in, it also received that environment's standing instructions, which say nothing about this project.

So the labels are independent of the builder, but they are still AI-written. That is a step toward the independent-labels gap, not a closing of it. A person labeling from the handbook is still the missing piece.

The job description it was given, verbatim:

> Employees of Northwire Technologies post requests in a Slack channel, #helpdesk-requests. The agent reads each one and does exactly one of three things: (1) **answerable** — replies with an answer drawn from the handbook, changing nothing; (2) **needs_write** — proposes one system change that a human approver then approves before it runs; the changes it can make are `create_ticket` (open a ticket in Waypoint, the ticket system — optionally with a target system, an access tier of viewer/operator/admin, and an assignee) and `assign_ticket` (open a ticket already assigned to a named person); (3) **escalate** — hands the request to a human on the IT/Ops team without acting, because the handbook says a human must handle it, the request is ambiguous or out of scope, or it is a security or identity matter. Each requester has a role on their core account: viewer, operator, or admin.

It was asked to label by what the handbook says should happen, to cover hard cases (routine-looking requests that hit an always-a-human rule, scary-looking ones that are routine, wording that invites the wrong action, and a few adversarial ones), and to mark anything it couldn't decide from the handbook as ambiguous.

## Ambiguous cases

Two are marked `"ambiguous": true` and left out of the score: ho-014 (operator access on Atlas, where §2 and §8 disagree) and ho-016 (the only `assign_ticket` case; the handbook never says when a ticket should carry an assignee).

## Where the writer found the handbook unclear

Its notes, condensed. These are findings about the handbook I wrote, and several explain misses before any model runs:

1. **§2 vs §8 on elevated access.** §2 says anything touching "security, credentials, or elevated access follows the Escalation Policy in Section 11 regardless of how it is phrased"; §8 gives elevated access an ordinary ticket path. A calm request for elevated access could be labeled either way. Account unlock has the same problem (§7 calls it routine; it arguably "touches credentials").
2. **§8 vs §11.5 on verbal approval.** §8 lets a manager's verbal approval open and route a ticket; §11.5 escalates requests "citing urgency, seniority, or a verbal/unlogged approval." They only fit if pressure is the deciding factor, which the handbook never says. ho-013 and ho-020 are a matched pair that differ only in pressure.
3. **§11.3 offers a clarifying question** the agent has no way to ask; the writer labeled that case escalate.
4. **§1 vs §11.4: redirect or escalate** for out-of-scope requests (ho-017, payroll). A "go to HR" answer could reasonably be graded answerable.
5. **No rule for when to assign** a ticket (why ho-016 is ambiguous).
6. **A viewer requesting access for someone else**: §3 forbids it but doesn't say whether to explain or escalate. Left out.
7. **Travel notice (§9)** has no stated channel. Left out.
8. **A legitimate request mixed with an injected instruction** (ho-022): §11.6 says escalate, but not whether the legitimate part may still be filed. Labeled escalate as a whole.

## Running it

```bash
uv run evals/score.py --requests evals/heldout/requests.json --k 3
```

## Re-checked against handbook v2

`data/handbook_v2.md` fixes the notes above (see `data/handbook_changelog.md`). A second fresh session was given only handbook v2 and these 24 requests with their v1 labels (not the changelog), and asked what each label should be under v2. Its answers are in `recheck_v2.json`, each with the deciding handbook text quoted verbatim.

- **One label changes:** ho-017 (a payroll question), escalate → answerable. v2 says out-of-scope requests get a reply pointing to the right team.
- **The two ambiguous cases are settled:** ho-014 is needs_write (a calm request for more access is a §8 ticket), and ho-016 is assign_ticket, provided the named person is in IT Operations, which the message implies but doesn't say.
- **Unclear under v2:** none.
- **Labels whose reasoning is stale** (same label, but it cites v1 wording or v1's §11 numbering): ho-010, 013, 014, 016, 017, 020, 022, 023.

The labels in `requests.json` stay as written against v1, and the eval runs on v1.

The re-checker also found four places where v2 is still unclear. They're recorded in the changelog as open.
