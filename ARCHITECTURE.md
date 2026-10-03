# Architecture

An internal IT/Ops helpdesk agent: requests arrive in Slack, the agent answers from a handbook, proposes system changes behind a permission gate and a human approval, and hands off anything it shouldn't decide.

## The operation, and the failure it's designed around

A person reads a request, looks up the handbook, updates a system, and escalates what they can't answer. Practitioners report building exactly this shape: an intake that clarifies requests, handbook answers, and actions behind a policy gate. The best-documented failure is pure-LLM ticket routing with no explanation trail: about 92% accurate, and ripped out because nobody could explain a misroute. The design consequence: every decision carries a rationale and a confidence a human can audit from the record alone, and every write is decided by code, not by the model.

## Graph: a deterministic skeleton, an LLM at three points

```
ingest → retrieve (BM25 + bge-small, RRF) → classify (LLM: category, rationale, confidence)
  │
  ├─ confidence below 0.6 (backstop; never fired) ──────────────► handoff
  │
  ├─ answerable → draft_answer (LLM, cites handbook chunks)
  │                 → grounding_check (code: cited chunks were retrieved; the cited section matches the answer best)
  │                     ├─ pass → respond
  │                     └─ fail → handoff
  │
  ├─ needs_write → propose_action (LLM → structured action)
  │                 → permission_check (code: forbidden actions and intents, fail closed on anything unknown)
  │                     ├─ refused → handoff
  │                     └─ allowed → approval_gate (LangGraph interrupt; admin approver, not the requester, digest-bound)
  │                         ├─ approved → execute_action (board, over MCP) → respond
  │                         └─ denied / digest mismatch / wrong approver → handoff
  │
  └─ escalate → handoff
```

Only `classify`, `draft_answer` and `propose_action` call the model. Retrieval, grounding, permissions, approval checks and execution are plain code. Model output that fails to parse routes to `handoff`, the same as any other refusal.

## Permissions: the model proposes, the policy decides

`app/permissions.py` never calls a model.

- `disable_mfa`, `reset_mfa`, `share_credentials`: forbidden for every role, whatever the model proposes, including with auto-approve on.
- The same intents are screened in the action's own title, description and target, so a forbidden request relabeled as `create_ticket` is still refused. It's a keyword regex: a paraphrase can get past it, and false positives go to a human. On the 118 executed tickets in the run history it flagged none.
- `create_ticket`, `assign_ticket`: allowed to reach the approval gate for any role; every write needs a human approval. A ticket asking for admin-tier access is flagged for closer scrutiny.
- An access ticket must name its system: the action carries `system_as_written`, and code checks that it appears verbatim in the requester's message. Otherwise it goes to a human to ask which system, so a system name the model made up can't reach a ticket. This check runs after the policy verdict, so a refused action keeps its own reason.
- `grant_access`: refused. Handbook §8 says access is granted by the system's admins after an approved Data Access Request ticket exists, and a Slack approval alone isn't sufficient, so the agent's part is opening that ticket. `propose_action` is told this; the policy enforces it if the model proposes a grant anyway.
- Anything else, or an unknown role: refused (fail closed).

Social-engineering pressure ("no time to explain, my manager said it's fine") is a judgment call a role table can't make. That belongs to `classify`'s rationale, and to the human at the approval gate.

## Approvals are bound to the exact action

The approval gate hashes the proposed action's canonical JSON together with the run id, request id and requester id (sha256), and shows the approver the raw arguments (action type, target system, tier, assignee), not just the model's description. The decision has to carry that digest back (the Slack button value, or the web approve request). A missing or different digest counts as a denial, and `execute_action` re-checks the digest before writing. Binding the run and request in means one approval can't be replayed onto another run.

Who may approve is checked after the interrupt returns, in the graph itself. The approver's role must be `admin` (`APPROVER_ROLES` in `app/config.py`), and the approver can't be the requester. In Slack, the identity is the user id Slack puts on the button click, mapped to a role by `data/slack_roles.json`; anyone unlisted is a viewer. A refused click gets a private reply and the run stays paused for a real approver. In the web view, approving (and submitting requests) needs the operator token the server prints at startup; the web approver acts as an admin. The approval record (who, when, decision, digest, action) stays in graph state, and the digest prefix is written onto the ticket.

## Durable pause

The server uses one `SqliteSaver` (`data/checkpoints.db`, thread id = run id). A run paused at the approval gate survives a restart: approving a run that isn't in memory rebuilds the graph over its checkpoint and resumes it. A small sidecar (`data/pending_approvals.json`) records which Slack message carries each pending approval. An approval whose run no longer exists updates the Slack prompt to say it has expired.

This is the reason for LangGraph: `interrupt()` plus a checkpointer gives a pause that outlives the process, without a hand-written state machine, and conditional edges keep the skeleton explicit. Everything else (events, policy, tools) is ordinary code. That's a thin framework for control flow and persistence, not an agent framework.

## Surfaces and tools

**Slack.** A Socket Mode app (`slack/manifest.yml`; no public URL). Socket Mode doesn't replay events a connection missed, so a seeded request that hasn't come back through the listener within 20 seconds (typically right after a restart) is started directly by the server. A top-level message in `#helpdesk-requests` starts a run. Approval prompts carry Approve/Deny buttons, and approving from Slack or from the web view works, whichever comes first. Seeded demo requests are posted by the app itself, labeled "(seeded)". `app/real_slack_adapter.py` and the no-token fallback `app/slack_adapter.py` share one interface.

**Board over MCP.** `mcp_board/server.py` is a stdio MCP server exposing `create_ticket`, `assign_ticket`, `mark_executed`, `get_ticket` and `list_tickets`, with no delete. It carries no policy: the permission gate runs before any MCP call, so policy stays above the protocol and in one place. `BOARD_BACKEND=local` swaps in the same interface backed by direct SQLite for fast tests.

Each run writes at most one ticket, in one `create_ticket` call (the assignee, if any, goes in the same call), keyed by the run id (`idempotency_key`, UNIQUE on the board). If the board process dies, the client's next call starts a new one and retries once; a create that landed before the crash is returned, not repeated. The server marks `create_ticket` idempotent for that reason.

## Event bus

Every node publishes `{ts, run_id, node, event_type, data}` to an in-process bus (`app/events.py`). Event types: `node_enter`, `retrieval_hits`, `llm_call` (model, tokens, cost, sampling mode), `decision`, `permission_verdict`, `approval_requested` / `approval_result`, `tool_call`, `handoff`, `respond`, `model_output_error`. Three independent consumers read it:

- `runs/<run_id>.jsonl`: always on. The evals and the failure taxonomy read this.
- Langfuse: via the LangChain callback handler. If Langfuse is unreachable, the run continues on the JSONL log.

Events are masked on publish (`app/redact.py`), before any consumer sees them, and Langfuse gets the same function as its `mask` hook, which covers the prompts the callback sends. The patterns cover provider API keys, Slack and GitHub tokens, JWTs, private-key blocks, "password is …", email addresses and phone numbers. It's a pattern screen, not a guarantee. The model call itself gets the raw message. `TRACE_CONTENT=full` disables masking for local demos.
- Server-sent events: the live peek view.

## Structured outputs

All three model calls use the API's constrained structured outputs (`with_structured_output(method="json_schema")`). With plain tool calling, `draft_answer` occasionally returned its citation list as a string (`"sec-9, sec-4"`) and failed validation. The recorded parse errors on the traces showed this, and constrained output removed it.

## Model and cost

Claude Sonnet 5 for all three calls. This is a classify, draft and route workload, and cost per request is shown live. Mean $0.0147 per request over 212 traced runs. `MODEL_ID` switches models. Sampling can't be pinned on this model (the SDK exposes no temperature for it, and thinking is adaptive), so the sampling mode is recorded on every trace.

## Tests and evaluation

- **CI gate** (`scripts/ci.sh`): invariants only. Deterministic tests with a stubbed model that makes the bad decision on purpose, plus the four adversarial traps × k real runs, which must all escalate.
- **Tracked, not gated** (`evals/score.py --k N`): accuracy and pass^k over 25 seeded, labeled requests, the grounding rate, and which cases disagree with themselves across runs. A run counts as right when the outcome and the kind of write match the label, and the board is checked separately: the tickets the run left (count, status, assignee, labels), and that it deleted or changed nothing else and opened nothing that reads as a forbidden action. Cost, latency and cache tokens are recorded per run. A threshold would flake, since the model's sampling can't be pinned.
- **Held-out set** (`evals/heldout/`): 24 requests written by a separate session that saw only the handbook and a one-paragraph job description, scored separately.
- **Failure taxonomy** (`evals/taxonomy.py`, `evals/failure_taxonomy.md`): how every recorded run ended, recorded model-output errors, and misroutes, open-coded.

The handbook, requests and labels are synthetic and were written by the same author as the agent; the README covers what that does and doesn't let the numbers claim.

## Deliberate v0 limits

- **The forbidden-intent screen is a keyword regex.** It is deterministic, auditable, and can't be talked out of anything, but a paraphrase, a euphemism or another language gets past it. The classifier and the human approver are the other two layers. The next step would be a hybrid: keep the regex as the floor, and add a model call that sees only the requester's message and the proposed action and returns a verdict. It would run on the write path only and fail closed, and its verdict would be shown to the approver. It could only add refusals, never overturn the regex or the policy. Estimated from 337 recorded `propose_action` calls (about 1.1k input tokens): about $0.003 per write-path request on Sonnet 5, or $0.0014 on Haiku 4.5. About 43% of requests reach that path, so it adds roughly 5–10% to the ~$0.013 mean, plus one call's latency on requests already waiting for a human. It isn't built, because there is no adversarial set yet to show the regex failing or the hybrid working. A separate session asked to write a blind adversarial set was stopped partway by a safety filter, and nothing was saved; it wasn't retried with different wording.

- One process: SQLite for checkpoints and the board, and in-process events. Several workers would move checkpoints to Postgres and events to a queue.
- Retrieval over one small handbook: BM25 (stopwords, light stemming) fused by reciprocal rank with a local bge-small embedding model. Plain BM25 missed 2 of 23 labeled sections; the hybrid misses none, on the same 23 requests it was checked against. `RETRIEVAL_MODE=full` sends the whole handbook instead, as a cached system block; the README has the measured comparison (a tie on accuracy, cheaper with a warm cache, dearer with a cold one).
- The classify rationale's section citations are checked against what was retrieved (or cross-referenced inside a retrieved section) and recorded, not routed on: the rationale is audit text, and the write it leads to is still gated and approved.
- Requesters default to the `viewer` role unless mapped in `data/slack_roles.json`. A real deployment would read roles from the identity provider.
