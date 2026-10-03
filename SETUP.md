# Setup

## Prerequisites

- Python 3.12+ and [`uv`](https://docs.astral.sh/uv/)
- An Anthropic API key
- (Optional) A [Langfuse](https://langfuse.com) project for tracing. This build uses Langfuse Cloud; a self-hosted instance works the same way (point `LANGFUSE_BASE_URL` at it). Without one, the JSONL run log in `runs/` is the trace.
- (Optional) A Slack workspace you control, for the real Slack surface. Without Slack tokens the server falls back to a built-in Slack-like mock.

## Install

```bash
uv sync
```

## Credentials

Copy `.env.example` to `.env` and fill in what you have. `python-dotenv` loads it automatically. Or inject the variables at launch and skip the file:

```bash
ANTHROPIC_API_KEY=sk-... uv run cli.py run-all
```

| Variable | Needed for |
|---|---|
| `ANTHROPIC_API_KEY` | everything that calls the model |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` | tracing (optional) |
| `SLACK_BOT_TOKEN` (`xoxb-…`), `SLACK_APP_TOKEN` (`xapp-…`) | real Slack (optional; both or neither) |
| `BOARD_BACKEND` | `mcp` (default: the board is reached over MCP) or `local` (direct SQLite) |

(This build was assembled inside a larger personal-automation environment that resolves credentials through its own secrets manager. That tooling isn't part of this repo and isn't needed to run it.)

## Run it

```bash
# One seeded request through the CLI, auto-approved
uv run cli.py run req-011

# The interactive peek view (http://127.0.0.1:8731) -- real approve/deny via LangGraph interrupt/resume.
# With Slack tokens set, it also connects to Slack over Socket Mode.
uv run uvicorn server:app --host 127.0.0.1 --port 8731
# At startup it prints an operator link, http://127.0.0.1:8731/#token=...
# Without the token the page is read-only; seeding, custom requests and approving
# need it. Set OPERATOR_TOKEN to keep the same one across restarts.
# On another port, set HELPDESK_PORT to match --port so the printed link and
# slack/seed.py point at it: HELPDESK_PORT=8741 uv run uvicorn server:app --port 8741

# Post seeded requests into Slack through the running server
OPERATOR_TOKEN=... uv run slack/seed.py req-003 req-011 req-019   # the token the server printed

# Eval: every seeded request k times; tracked accuracy + pass^k, results/<timestamp>.json
uv run evals/score.py --k 3

# How every recorded run ended, model-output errors, and misroutes
uv run evals/taxonomy.py

# The CI gate: deterministic tests, plus the live trap invariants if ANTHROPIC_API_KEY is set
bash scripts/ci.sh
```

State on disk (all gitignored): `data/board.db` (the ticket board), `data/checkpoints.db` (paused runs survive a restart), `data/pending_approvals.json` (which Slack message carries each pending approval), `data/eval_board.db` (evals use their own board), `data/slack_roles.json` (who can approve), `runs/*.jsonl` (one event log per run).

## Real Slack (about 10 minutes)

1. At [api.slack.com/apps](https://api.slack.com/apps) choose **Create New App → From a manifest**, pick your workspace, and paste [`slack/manifest.yml`](slack/manifest.yml). It declares Socket Mode, interactivity (Approve/Deny buttons over the socket, no public URL) and six bot scopes: `chat:write`, `chat:write.customize` (seeded requests are labeled "(seeded)"), `channels:history`, `channels:read`, `channels:join` (the bot joins the channel itself), `users:read`.
2. **Basic Information → App-Level Tokens → Generate Token and Scopes**, scope `connections:write` only. That's `SLACK_APP_TOKEN`.
3. **Install App → Install to workspace**. The Bot User OAuth Token is `SLACK_BOT_TOKEN`.
4. Create a public channel named `#helpdesk-requests` (or set `SLACK_CHANNEL`).
5. Copy `data/slack_roles.example.json` to `data/slack_roles.json` and put your own Slack user ID in it as `admin` (profile → ⋮ → Copy member ID). Anyone not listed is a `viewer`. Only `admin` can press Approve, and nobody can approve their own request; a refused click gets a private reply and the run stays paused.
6. Start the server. It joins the channel, and any top-level message there becomes a request.

The adapter (`app/real_slack_adapter.py`) implements the same interface as the mock (`app/slack_adapter.py`), so nothing downstream changes between them.

## The board over MCP

`mcp_board/server.py` is a stdio MCP server (MCP Python SDK 2.x, `MCPServer`) exposing `create_ticket`, `assign_ticket`, `mark_executed`, `get_ticket` and `list_tickets`, with no delete. It carries no policy: the permission gate runs in the agent before any MCP call is made. The agent reaches it through `app/mcp_board_client.py`, which launches it as a subprocess. You can run it standalone for any MCP client:

```bash
BOARD_DB_PATH=data/board.db uv run python -m mcp_board.server
```
