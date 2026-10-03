# The Slack Helpdesk Agent on Agent Lab Bench

This folder is the app's bench kit. The bench (a separate repo) knows nothing about this app; everything app-specific is here and is sent to the bench at startup.

- `adapter.py`: the event bus → bench events (mapping below).
- `topology.json`: the map. Every step and possible branch, with Agent Spec-style `from_branch` labels.
- `story.js`: the story. Handwritten panels (retrieval, classification, permission gate, approval) ported from the old peek view.
- `client.py`: registers the map and story (`PUT /apps/slack-helpdesk`) and sends events (`POST /ingest`), fire-and-forget.
- `bridge.py`: follows a running server's `/api/stream` and forwards it, for the operator server.
- `recordings.py`: turns `demo/replays/*.json` into self-contained bench recordings.

## Mapping

Source: the request-queue repo's `{run_id, node, event_type, data, ts}` events (JSONL run logs and `demo/replays/*.json`).

| this app | bench |
|---|---|
| `_meta` `phase: started` | `run_started` |
| `_meta` `phase: done` / `error` | `run_finished` (`ok` / `error`), plus `error` |
| `_meta` `phase: awaiting_approval` | dropped (the `gate_waiting` from `approval_requested` carries it) |
| `node_enter` | `step_started` |
| first event on a new node | `step_finished` for the previous node at its own last event, and `step_started` for this node at that same moment (latency inferred; see section 2) |
| `retrieval_hits` | `retrieval` (`hits[].chunk_id` → `id`, `section` → `title`; `bm25` kept) |
| `decision` | `decision`, with `branch` = `category` (classify) or `grounded` / `not_grounded` (grounding_check) |
| `llm_call` | `llm_call`, `cost_source: "estimated"`, `cost_basis: "request-queue price table"`. Its `input_tokens` is already the total (langchain's convention), so no conversion. |
| `approval_requested` | `gate_waiting` (`proposed` = `proposed_action`, `digest` = `action_digest`) |
| `approval_result` | `gate_resolved` (`approved_by` → `by`, `approver_via` → `via`) |
| `permission_verdict`, `respond`, `handoff`, `tool_call`, `model_output_error` | passed through under the same `event_type` (`model_output_error` also as `error`) |

A recording (`demo/replays/*.json`) has no `_meta` phases; its `final` reply becomes `run_finished.data.output`. `respond` is not emitted on every path (escalations and denials end without it), so the run's end, not `respond`, is the signal that the reply exists.

The adapter numbers `seq`, sets `session_id` from its caller (the demo sandbox id, or `"operator"`), and sets `content_mode` from `TRACE_CONTENT` (`redacted` unless it was `full`).
