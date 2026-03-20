"""
Structured JSON logging module for BedrockGateway.

This module provides:
- JSON-formatted log output using python-json-logger
- Automatic context injection via contextvars
- Request correlation via request_id
- CloudWatch-friendly log format
"""

import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from pythonjsonlogger import jsonlogger

# Context variables for request-scoped data
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
org_id_var: ContextVar[str | None] = ContextVar("org_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)
team_id_var: ContextVar[str | None] = ContextVar("team_id", default=None)
department_id_var: ContextVar[str | None] = ContextVar("department_id", default=None)


class StructuredJsonFormatter(jsonlogger.JsonFormatter):
    """
    Custom JSON formatter that includes context fields and standardized format.

    Every log line includes:
    - timestamp: ISO 8601 format with timezone
    - level: Log level name
    - message: Log message
    - module: Logger name/module
    - request_id: Request correlation ID (from contextvar)
    - org_id, user_id, team_id, department_id: Entity context (from contextvars)
    """

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        """Add custom fields to log record."""
        super().add_fields(log_record, record, message_dict)

        # Add timestamp in ISO 8601 format
        log_record["timestamp"] = datetime.now(UTC).isoformat()

        # Add log level
        log_record["level"] = record.levelname

        # Add module name
        log_record["module"] = record.name

        # Add context from contextvars
        request_id = request_id_var.get()
        if request_id:
            log_record["request_id"] = request_id

        org_id = org_id_var.get()
        if org_id:
            log_record["org_id"] = org_id

        user_id = user_id_var.get()
        if user_id:
            log_record["user_id"] = user_id

        team_id = team_id_var.get()
        if team_id:
            log_record["team_id"] = team_id

        department_id = department_id_var.get()
        if department_id:
            log_record["department_id"] = department_id

        # Move message to standardized location
        if "message" not in log_record and record.msg:
            log_record["message"] = record.getMessage()


def get_json_formatter() -> StructuredJsonFormatter:
    """
    Create a JSON formatter instance.

    Returns:
        StructuredJsonFormatter: Configured JSON formatter
    """
    # Note: We handle field additions in add_fields() instead of using rename_fields
    # to maintain compatibility and have full control over the output format
    return StructuredJsonFormatter(
        fmt="%(message)s",
    )


def configure_logging(
    level: str = "INFO",
    json_output: bool = True,
    stream: Any = None,
) -> None:
    """
    Configure the root logger with structured JSON output.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        json_output: Whether to use JSON formatter (False for development)
        stream: Output stream (defaults to sys.stdout)
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper()))

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create handler
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setLevel(getattr(logging, level.upper()))

    # Set formatter
    if json_output:
        handler.setFormatter(get_json_formatter())
    else:
        # Development-friendly format
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    root_logger.addHandler(handler)

    # Reduce noise from third-party libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance with the given name.

    Args:
        name: Logger name (typically __name__)

    Returns:
        logging.Logger: Configured logger instance
    """
    return logging.getLogger(name)


def set_request_context(
    request_id: str | None = None,
    org_id: str | None = None,
    user_id: str | None = None,
    team_id: str | None = None,
    department_id: str | None = None,
) -> None:
    """
    Set request context variables for logging.

    Args:
        request_id: Unique request identifier
        org_id: Organization ID
        user_id: User ID
        team_id: Team ID
        department_id: Department ID
    """
    if request_id is not None:
        request_id_var.set(request_id)
    if org_id is not None:
        org_id_var.set(org_id)
    if user_id is not None:
        user_id_var.set(user_id)
    if team_id is not None:
        team_id_var.set(team_id)
    if department_id is not None:
        department_id_var.set(department_id)


def clear_request_context() -> None:
    """Clear all request context variables."""
    request_id_var.set(None)
    org_id_var.set(None)
    user_id_var.set(None)
    team_id_var.set(None)
    department_id_var.set(None)


def get_request_id() -> str | None:
    """Get the current request ID from context."""
    return request_id_var.get()


class LogContext:
    """
    Context manager for temporarily setting log context.

    Example:
        with LogContext(request_id="abc-123", org_id="org-456"):
            logger.info("Processing request")
    """

    def __init__(
        self,
        request_id: str | None = None,
        org_id: str | None = None,
        user_id: str | None = None,
        team_id: str | None = None,
        department_id: str | None = None,
    ):
        self.request_id = request_id
        self.org_id = org_id
        self.user_id = user_id
        self.team_id = team_id
        self.department_id = department_id
        self._tokens: list[Any] = []

    def __enter__(self) -> "LogContext":
        if self.request_id is not None:
            self._tokens.append(("request_id", request_id_var.set(self.request_id)))
        if self.org_id is not None:
            self._tokens.append(("org_id", org_id_var.set(self.org_id)))
        if self.user_id is not None:
            self._tokens.append(("user_id", user_id_var.set(self.user_id)))
        if self.team_id is not None:
            self._tokens.append(("team_id", team_id_var.set(self.team_id)))
        if self.department_id is not None:
            self._tokens.append(("department_id", department_id_var.set(self.department_id)))
        return self

    def __exit__(self, *args: Any) -> None:
        for name, token in self._tokens:
            if name == "request_id":
                request_id_var.reset(token)
            elif name == "org_id":
                org_id_var.reset(token)
            elif name == "user_id":
                user_id_var.reset(token)
            elif name == "team_id":
                team_id_var.reset(token)
            elif name == "department_id":
                department_id_var.reset(token)
