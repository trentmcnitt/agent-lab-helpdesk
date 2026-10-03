"""Langfuse is a sink, not a dependency: if it's unreachable or misconfigured,
the run continues and the JSONL event log stays the authoritative trace."""
from __future__ import annotations

import logging

from . import config, redact

logger = logging.getLogger(__name__)

_checked = False
_handler = None
_client = None


def get_langfuse_handler():
    """Returns a langfuse.langchain.CallbackHandler, or None if unavailable.
    Runs the connectivity check exactly once per process."""
    global _checked, _handler, _client
    if _checked:
        return _handler
    _checked = True

    if not (config.LANGFUSE_PUBLIC_KEY and config.LANGFUSE_SECRET_KEY):
        logger.warning("Langfuse credentials not set -- tracing sink disabled, JSONL log is authoritative.")
        return None

    try:
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler

        _client = Langfuse(
            public_key=config.LANGFUSE_PUBLIC_KEY,
            secret_key=config.LANGFUSE_SECRET_KEY,
            host=config.LANGFUSE_HOST,
            # Prompts carry the raw request; mask secrets before they leave the machine.
            mask=redact.langfuse_mask if redact.enabled() else None,
        )
        if not _client.auth_check():
            raise RuntimeError("auth_check() returned False")
        _handler = CallbackHandler()
        logger.info("Langfuse connected: %s", config.LANGFUSE_HOST)
    except Exception as e:
        logger.warning("Langfuse unavailable (%s) -- falling back to JSONL-only tracing.", e)
        _handler = None

    return _handler


def flush_langfuse() -> None:
    """Short-lived CLI processes must flush explicitly -- the SDK batches."""
    if _client is not None:
        try:
            _client.flush()
        except Exception as e:
            logger.warning("Langfuse flush failed (%s)", e)


def langfuse_status() -> str:
    handler = get_langfuse_handler()
    return "connected" if handler is not None else "unavailable (JSONL fallback)"
