"""Runtime configuration. Reads plain env vars (see .env.example), so this
repo runs standalone for anyone who clones it.
Real credential values are injected at launch time by the operator (see
SETUP.md), never hardcoded or committed."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# The MCP board subprocess sets this: it needs no credentials, so it must not
# pick them up from a .env file either.
if not os.environ.get("HELPDESK_NO_DOTENV"):
    load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
RUNS_DIR = REPO_ROOT / "runs"
BOARD_DB_PATH = DATA_DIR / "board.db"
# Durable graph state: a run paused at the approval gate survives a server restart.
CHECKPOINT_DB_PATH = DATA_DIR / "checkpoints.db"
# The labels in data/seed_requests.json, evals/heldout and evals/dev are written against v1;
# v2 resolves v1's contradictions (data/handbook_changelog.md).
HANDBOOK_VERSION = os.environ.get("HANDBOOK_VERSION", "1")
HANDBOOK_PATH = DATA_DIR / f"handbook_v{HANDBOOK_VERSION}.md"
SEED_REQUESTS_PATH = DATA_DIR / "seed_requests.json"

# Cost-conscious default: this is a classify/draft/route workload, the shape
# practitioners report running on lighter models. See ARCHITECTURE.md.
MODEL_ID = os.environ.get("MODEL_ID", "claude-sonnet-5")

# Below this, classify's stated category is overridden to "escalate"
# regardless of what the model picked -- the documented rip-out fix.
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.6"))

RETRIEVAL_TOP_K = int(os.environ.get("RETRIEVAL_TOP_K", "4"))
RETRIEVAL_MODE = os.environ.get("RETRIEVAL_MODE", "hybrid")  # "hybrid" (BM25 + bge-small, RRF), "bm25", or "full" (whole handbook, cached)
TRACE_CONTENT = os.environ.get("TRACE_CONTENT", "redacted")  # "redacted" (mask secrets/PII in traces) or "full"

# "mcp": the board is reached over MCP (stdio) via mcp_board/server.py.
# "local": direct SQLite adapter, same interface (fast unit tests).
BOARD_BACKEND = os.environ.get("BOARD_BACKEND", "mcp")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# Real Slack (Socket Mode). Both unset -> the server falls back to the mock surface.
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")  # xoxb-...
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")  # xapp-..., connections:write
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "helpdesk-requests")
# Optional {slack_user_id: "viewer"|"operator"|"admin"}; anyone unlisted is a viewer.
SLACK_ROLES_PATH = DATA_DIR / "slack_roles.json"

LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY")
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY")
LANGFUSE_HOST = os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")

# Pricing snapshot (Anthropic API, cached 2026-06-24) -- $ per token.
PRICING_PER_TOKEN = {
    "claude-opus-5": {"input": 5.00 / 1_000_000, "output": 25.00 / 1_000_000},
    "claude-sonnet-5": {"input": 2.00 / 1_000_000, "output": 10.00 / 1_000_000},
    "claude-haiku-4-5": {"input": 1.00 / 1_000_000, "output": 5.00 / 1_000_000},
}


# Prompt caching: reads bill at 0.1x the input price, 5-minute writes at 1.25x.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


def cost_for(model_id: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    """input_tokens here is the uncached part only."""
    p = PRICING_PER_TOKEN.get(model_id, PRICING_PER_TOKEN["claude-sonnet-5"])
    return (input_tokens * p["input"] + cache_read * p["input"] * CACHE_READ_MULTIPLIER
            + cache_write * p["input"] * CACHE_WRITE_MULTIPLIER + output_tokens * p["output"])


# Roles, most to least privileged.
ROLES = ("viewer", "operator", "admin")

# Only these roles may approve a write, and never on their own request.
APPROVER_ROLES = frozenset({"admin"})

# Never permitted, regardless of requester role or model output.
# The Decawork lesson, encoded in code rather than left to the model.
FORBIDDEN_ACTION_TYPES = frozenset({"disable_mfa", "reset_mfa", "share_credentials"})
