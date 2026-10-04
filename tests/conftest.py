import os
import sys
from pathlib import Path

# Tests never send to a bench someone happens to be running on this machine; the ones about
# Agent Lab capture spans in memory (agentlab.testing.capture).
os.environ.setdefault("AGENT_LAB_URL", "off")

import pytest
from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.board_adapter import BoardAdapter  # noqa: E402
from app.events import EventBus  # noqa: E402
from app.retrieval import HandbookIndex  # noqa: E402


class _Structured:
    def __init__(self, parsed):
        self.parsed = parsed

    def invoke(self, prompt, config=None):
        raw = AIMessage(content="", usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
        return {"parsed": self.parsed, "raw": raw, "parsing_error": None}


class FakeLLM:
    """Returns a fixed decision per output schema. Asking for a schema it
    wasn't given raises, so a test also proves which LLM nodes never ran."""

    def __init__(self, by_schema: dict):
        self.by_schema = by_schema

    def with_structured_output(self, schema, include_raw=False, **kwargs):
        if schema not in self.by_schema:
            raise AssertionError(f"graph asked the model for {schema.__name__}, which this path must never reach")
        return _Structured(self.by_schema[schema])


@pytest.fixture(scope="session")
def index():
    return HandbookIndex()


@pytest.fixture
def board(tmp_path):
    return BoardAdapter(tmp_path / "board.db")


@pytest.fixture
def bus():
    return EventBus(run_id="test")
