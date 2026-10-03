#!/usr/bin/env bash
# The CI gate: invariants only. Accuracy / pass^k are tracked by
# evals/score.py, never gated here -- they move run to run by design.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run pytest -m "not live" -q
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  uv run pytest -m live -q
else
  echo "ANTHROPIC_API_KEY unset: skipped live invariants (four traps x INVARIANT_K runs)"
fi
