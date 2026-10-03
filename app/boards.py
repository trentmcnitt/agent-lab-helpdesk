from __future__ import annotations

from pathlib import Path

from . import config
from .board_adapter import BoardAdapter


def make_board(db_path: Path | None = None):
    if config.BOARD_BACKEND == "mcp":
        from .mcp_board_client import MCPBoardClient

        return MCPBoardClient(db_path)
    return BoardAdapter(db_path)
