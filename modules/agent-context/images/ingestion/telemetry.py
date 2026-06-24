"""Knowledge Layer Telemetry — structured logging + correlation context.

Provides the observability foundation for the ingestion pipeline:
- JSON structured logging (CloudWatch-friendly) with correlation dimensions
- Correlation context via contextvars (asset_id, owner_sub, tenant_id, etc.)
- Fail-open discipline: telemetry errors never block ingestion
- Kill switch via KNOWLEDGE_LAYER_TELEMETRY_ENABLED env var

The correlation spine enables pivoting metric -> trace -> log for any document:
  {asset_id, owner_sub, tenant_id, project_id, run_id, stage, asset_type}

Usage:
    from telemetry import configure_telemetry, set_correlation_context, get_logger

    configure_telemetry()
    set_correlation_context(asset_id="org/repo", owner_sub="user-123", ...)
    log = get_logger("sqs-worker")
    log.info("Processing started")  # JSON with all correlation fields

References:
    - Design: docs/agent-context/design-1746-observability.md
    - Gateway pattern: modules/gateway/src/shared/logging.py
    - Identity enrichment: modules/agent-factory/agent-worker-image/entrypoint.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Feature flag — kill switch for all telemetry emission
# ---------------------------------------------------------------------------

TELEMETRY_ENABLED = os.environ.get(
    "KNOWLEDGE_LAYER_TELEMETRY_ENABLED", "true"
).lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# Correlation context (contextvars for request-scoped dimensions)
# ---------------------------------------------------------------------------

asset_id_var: ContextVar[str | None] = ContextVar("asset_id", default=None)
owner_sub_var: ContextVar[str | None] = ContextVar("owner_sub", default=None)
tenant_id_var: ContextVar[str | None] = ContextVar("tenant_id", default=None)
project_id_var: ContextVar[str | None] = ContextVar("project_id", default=None)
run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)
stage_var: ContextVar[str | None] = ContextVar("stage", default=None)
asset_type_var: ContextVar[str | None] = ContextVar("asset_type", default=None)


@dataclass
class CorrelationContext:
    """The correlation spine — dimensions shared across logs, traces, metrics.

    Every log line, span, and (where cardinality-safe) metric carries these fields
    so an operator can pivot from any signal to the others for one document.
    """

    asset_id: str | None = None
    owner_sub: str | None = None
    tenant_id: str | None = None
    project_id: str | None = None
    run_id: str | None = None
    stage: str | None = None
    asset_type: str | None = None

    def as_dict(self) -> dict[str, str]:
        """Return non-None fields as a flat dict (for log enrichment)."""
        return {k: v for k, v in asdict(self).items() if v is not None}


def set_correlation_context(
    asset_id: str | None = None,
    owner_sub: str | None = None,
    tenant_id: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    stage: str | None = None,
    asset_type: str | None = None,
) -> None:
    """Set correlation context for the current execution context.

    Call this at message receipt (sqs-worker) to stamp all subsequent logs.
    Safe to call multiple times — later calls override earlier values.
    """
    if asset_id is not None:
        asset_id_var.set(asset_id)
    if owner_sub is not None:
        owner_sub_var.set(owner_sub)
    if tenant_id is not None:
        tenant_id_var.set(tenant_id)
    if project_id is not None:
        project_id_var.set(project_id)
    if run_id is not None:
        run_id_var.set(run_id)
    if stage is not None:
        stage_var.set(stage)
    if asset_type is not None:
        asset_type_var.set(asset_type)


def get_correlation_context() -> CorrelationContext:
    """Read the current correlation context from contextvars."""
    return CorrelationContext(
        asset_id=asset_id_var.get(),
        owner_sub=owner_sub_var.get(),
        tenant_id=tenant_id_var.get(),
        project_id=project_id_var.get(),
        run_id=run_id_var.get(),
        stage=stage_var.get(),
        asset_type=asset_type_var.get(),
    )


def clear_correlation_context() -> None:
    """Reset all correlation context vars. Useful between messages in tests."""
    asset_id_var.set(None)
    owner_sub_var.set(None)
    tenant_id_var.set(None)
    project_id_var.set(None)
    run_id_var.set(None)
    stage_var.set(None)
    asset_type_var.set(None)


# ---------------------------------------------------------------------------
# JSON Structured Formatter
# ---------------------------------------------------------------------------


class KnowledgeLayerJsonFormatter(logging.Formatter):
    """JSON log formatter that injects correlation context into every log line.

    Output format (one JSON object per line):
        {"timestamp": "...", "level": "INFO", "module": "sqs-worker",
         "message": "...", "asset_id": "...", "tenant_id": "...", ...}

    Uses stdlib logging only — no external dependency required for basic operation.
    python-json-logger is used when available for robustness, but we fall back
    to manual JSON serialization if it's missing.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record as a single-line JSON object."""
        log_entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }

        # Inject correlation context (fail-open: if contextvars somehow fail, skip)
        try:
            ctx = get_correlation_context()
            log_entry.update(ctx.as_dict())
        except Exception:
            pass  # fail-open: never crash on context read

        # Include exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Include extra fields passed via log.info("msg", extra={...})
        for key in ("extra_data",):
            if hasattr(record, key):
                log_entry[key] = getattr(record, key)

        return json.dumps(log_entry, default=str)


# ---------------------------------------------------------------------------
# Plain text formatter (development fallback)
# ---------------------------------------------------------------------------


class KnowledgeLayerTextFormatter(logging.Formatter):
    """Human-readable formatter for local development.

    Still includes correlation fields but in a readable format.
    """

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        ctx = get_correlation_context()
        ctx_str = " ".join(f"{k}={v}" for k, v in ctx.as_dict().items())
        prefix = f"{timestamp} [{record.levelname}] [{record.name}]"
        if ctx_str:
            prefix += f" [{ctx_str}]"
        msg = f"{prefix} {record.getMessage()}"
        if record.exc_info and record.exc_info[0] is not None:
            msg += "\n" + self.formatException(record.exc_info)
        return msg


# ---------------------------------------------------------------------------
# Configure logging (entry point)
# ---------------------------------------------------------------------------

_configured = False


def configure_telemetry(
    level: str = "INFO",
    json_output: bool | None = None,
    service_name: str = "knowledge-layer-ingestion",
) -> None:
    """Configure structured logging for the Knowledge Layer.

    Must be called once at process startup (before any log calls).
    Idempotent — subsequent calls are no-ops.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR).
        json_output: Force JSON (True) or text (False). Default: auto-detect
                     (JSON unless LOG_FORMAT=text is set).
        service_name: Identifies this service in log output.
    """
    global _configured
    if _configured:
        return
    _configured = True

    # Determine output format
    if json_output is None:
        json_output = os.environ.get("LOG_FORMAT", "json").lower() != "text"

    # When telemetry is disabled, use minimal text logging
    if not TELEMETRY_ENABLED:
        json_output = False

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove any existing handlers (prevents duplicate output)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(getattr(logging, level.upper(), logging.INFO))

    if json_output:
        handler.setFormatter(KnowledgeLayerJsonFormatter())
    else:
        handler.setFormatter(KnowledgeLayerTextFormatter())

    root_logger.addHandler(handler)

    # Reduce noise from third-party libraries
    for noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a named logger. Always call configure_telemetry() first.

    Args:
        name: Logger name (typically the script/module name).

    Returns:
        A stdlib Logger that respects the configured formatter.
    """
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Fail-open utility
# ---------------------------------------------------------------------------


def safe_emit(fn, *args, **kwargs) -> None:
    """Call fn(*args, **kwargs), swallowing any exception.

    Used to wrap telemetry emission so it never blocks ingestion.
    Example:
        safe_emit(set_correlation_context, run_id=tracker.run_id)
    """
    try:
        fn(*args, **kwargs)
    except Exception:
        pass  # fail-open: telemetry must never block ingestion
