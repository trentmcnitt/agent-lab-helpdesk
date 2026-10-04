# The public demo (demo_server.py): mock Slack, per-visitor sandboxes, recorded
# replays by default. The operator server (server.py) is not what this runs.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" FASTEMBED_CACHE_PATH=/opt/fastembed PYTHONUNBUFFERED=1

WORKDIR /app
# Agent Lab's library is a path dependency (pyproject: ../agent-lab/sdk/python, i.e.
# /agent-lab/sdk/python from /app), from a clone of trentmcnitt/agent-lab beside this repo.
# Build with it as a named context:
#   docker build --build-context agentlab=../agent-lab/sdk/python .
COPY --from=agentlab . /agent-lab/sdk/python
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY mcp_board ./mcp_board
COPY ui ./ui
COPY data/handbook_v1.md data/handbook_v2.md data/seed_requests.json ./data/
COPY demo ./demo
COPY cli.py demo_server.py ./

# Bake the embedding model in, so a live run never downloads anything at request time.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

RUN useradd --create-home --uid 10001 demo && mkdir -p /data && chown demo /data
USER demo
ENV DEMO_DATA_DIR=/data DEMO_MODE=replay
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["uvicorn", "demo_server:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
