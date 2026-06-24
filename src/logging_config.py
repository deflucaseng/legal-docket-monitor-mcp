from __future__ import annotations

import contextvars
import sys
import uuid

import structlog

# Per-request correlation ID — set once at the top of each tool call
request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "docket-intelligence"):
    return structlog.get_logger(name)


def new_request_id() -> str:
    rid = str(uuid.uuid4())
    request_id.set(rid)
    return rid
