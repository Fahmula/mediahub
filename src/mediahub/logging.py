"""Structured logging with secret redaction.

Every log record passes through :func:`redact_secrets`, which masks values whose key looks
sensitive, so an accidental ``log.info("x", headers=...)`` never leaks credentials.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping
from typing import Any

import structlog

from mediahub.config import LogFormat

REDACTED = "***"
_SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|apikey|token|secret|password|passwd|authorization|cookie|x-emby-token|"
    r"x-api-key|internal[_-]?key|endpoint|p256dh)",
    re.IGNORECASE,
)


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return value
    if isinstance(value, Mapping):
        return {
            k: REDACTED
            if isinstance(k, str) and _SENSITIVE_KEY.search(k)
            else _redact(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(_redact(v, depth + 1) for v in value)
    return value


def redact_secrets(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return _redact(event_dict)


def configure_logging(level: str = "INFO", fmt: LogFormat = LogFormat.JSON) -> None:
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_secrets,
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if fmt == LogFormat.JSON
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # httpx logs full request URLs at INFO; keep it quiet. Uvicorn access logs are replaced
    # by our own request logging middleware.
    for name in ("httpx", "httpcore", "websockets", "uvicorn.access", "alembic"):
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
