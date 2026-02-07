"""Structured logging configuration for the Outlook AI Assistant.

Uses structlog for JSON-formatted logs to stdout. Supports correlation IDs
(triage_cycle_id) via contextvars for tracing requests through the system.

Usage:
    from assistant.core.logging import get_logger, set_correlation_id

    logger = get_logger(__name__)

    # In triage engine:
    set_correlation_id(str(uuid.uuid4()))

    # Log with automatic correlation ID inclusion:
    logger.info("email_classified", email_id="abc123", folder="Projects/Example")
"""

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

# Context variable for correlation ID (triage_cycle_id)
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def set_correlation_id(correlation_id: str | None) -> None:
    """Set the correlation ID for the current context.

    Call this at the start of each triage cycle with a new UUID.
    All subsequent log entries in this context will include the ID.

    Args:
        correlation_id: UUID string for this triage cycle, or None to clear
    """
    _correlation_id.set(correlation_id)


def get_correlation_id() -> str | None:
    """Get the current correlation ID, if set."""
    return _correlation_id.get()


def add_correlation_id(
    logger: structlog.types.WrappedLogger,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Structlog processor to add correlation ID to log entries."""
    correlation_id = _correlation_id.get()
    if correlation_id is not None:
        event_dict["triage_cycle_id"] = correlation_id
    return event_dict


def configure_logging(log_level: str = "INFO", json_output: bool = True) -> None:
    """Configure structlog for the application.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        json_output: If True, output JSON; if False, output human-readable format
    """
    # Set up standard library logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper()),
    )

    # Common processors for all configurations
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        add_correlation_id,
    ]

    if json_output:
        # JSON output for production (Docker captures this)
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Human-readable output for development
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structured logger instance.

    Args:
        name: Logger name (typically __name__ of the calling module)

    Returns:
        A structlog BoundLogger instance configured for this application

    Example:
        logger = get_logger(__name__)
        logger.info("operation_complete", duration_ms=42, records=100)
    """
    return structlog.get_logger(name)
