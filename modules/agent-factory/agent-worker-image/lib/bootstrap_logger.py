"""Durable bootstrap logger — writes step-level logs to CloudWatch Logs.

Survives pod GC by pushing directly to CloudWatch via boto3 (no fluent-bit
dependency). Used exclusively during the entrypoint Setup sequence (Steps 1-6b)
before the Node agent SDK / OTEL plumbing starts.

Log group: /adp/{environment}/agent-factory/bootstrap
Log stream: keyed by correlation_id (or message_id fallback)

Design: fail-soft everywhere — CloudWatch failures must NEVER crash the
bootstrap. If the handler can't reach CloudWatch, logs still flow to stdout
via the standard Python logger.
"""

from __future__ import annotations

import logging
import time
import traceback
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class CloudWatchBootstrapHandler(logging.Handler):
    """Logging handler that ships records directly to CloudWatch Logs.

    Buffers log events and flushes on explicit flush() call or when the
    buffer reaches max_batch_size. Thread-safety is NOT required — the
    entrypoint is single-threaded.
    """

    MAX_BATCH_SIZE = 20  # CloudWatch PutLogEvents limit is 10,000; keep small for bootstrap

    def __init__(
        self,
        log_group: str,
        log_stream: str,
        region: str = "us-east-1",
    ):
        super().__init__()
        self._log_group = log_group
        self._log_stream = log_stream
        self._region = region
        self._buffer: list[dict[str, Any]] = []
        self._sequence_token: str | None = None
        self._client = None
        self._initialized = False
        self._failed = False  # Once True, stop attempting CW writes

        self._ensure_client()

    def _ensure_client(self) -> None:
        """Create boto3 client and ensure log group/stream exist."""
        if self._failed:
            return
        try:
            self._client = boto3.client("logs", region_name=self._region)
            # Create log group (idempotent)
            try:
                self._client.create_log_group(logGroupName=self._log_group)
            except ClientError as e:
                if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
                    raise
            # Set retention (7 days — bootstrap logs are transient diagnostics)
            try:
                self._client.put_retention_policy(logGroupName=self._log_group, retentionInDays=7)
            except ClientError:
                pass  # Non-fatal: retention is a nice-to-have

            # Create log stream (idempotent)
            try:
                self._client.create_log_stream(
                    logGroupName=self._log_group, logStreamName=self._log_stream
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
                    raise
            self._initialized = True
        except Exception as exc:
            # Fail-soft: log to stdout but don't crash
            logger.warning(
                "[bootstrap_logger] CloudWatch init failed (will log to stdout only): %s", exc
            )
            self._failed = True

    def emit(self, record: logging.LogRecord) -> None:
        """Buffer log event; flush when buffer is full."""
        if self._failed:
            return
        try:
            msg = self.format(record)
            self._buffer.append(
                {
                    "timestamp": int(record.created * 1000),
                    "message": msg,
                }
            )
            if len(self._buffer) >= self.MAX_BATCH_SIZE:
                self.flush()
        except Exception:
            # Never raise from emit — Python logging contract
            pass

    def flush(self) -> None:
        """Push buffered events to CloudWatch."""
        if self._failed or not self._buffer or not self._initialized:
            return
        try:
            # Sort events by timestamp (CloudWatch requires monotonic)
            events = sorted(self._buffer, key=lambda e: e["timestamp"])
            kwargs: dict[str, Any] = {
                "logGroupName": self._log_group,
                "logStreamName": self._log_stream,
                "logEvents": events,
            }
            if self._sequence_token:
                kwargs["sequenceToken"] = self._sequence_token
            try:
                resp = self._client.put_log_events(**kwargs)
                self._sequence_token = resp.get("nextSequenceToken")
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code in (
                    "InvalidSequenceTokenException",
                    "DataAlreadyAcceptedException",
                ):
                    # Recover: fetch the correct token and retry once
                    token = e.response["Error"].get("expectedSequenceToken")
                    if token:
                        kwargs["sequenceToken"] = token
                        resp = self._client.put_log_events(**kwargs)
                        self._sequence_token = resp.get("nextSequenceToken")
                    else:
                        raise
                else:
                    raise
            self._buffer.clear()
        except Exception as exc:
            # Fail-soft: don't crash, but stop retrying this batch
            logger.warning("[bootstrap_logger] flush failed: %s", exc)
            self._buffer.clear()
            # Don't set _failed=True — next batch might succeed


class BootstrapLogger:
    """High-level API for step-level bootstrap logging.

    Usage:
        bl = BootstrapLogger(environment="dev", correlation_id="abc-123", region="us-east-1")
        bl.step_start(1, "parse_envelope", correlation_id="abc", tenant_id="acme")
        bl.step_success(1, "parse_envelope")
        bl.step_error(2, "vault_fetch", exc)
        bl.close()
    """

    def __init__(
        self,
        environment: str,
        correlation_id: str,
        region: str = "us-east-1",
        message_id: str = "",
    ):
        self._environment = environment
        self._correlation_id = correlation_id
        self._message_id = message_id
        self._region = region
        self._current_step: int | None = None
        self._current_step_name: str | None = None

        log_group = f"/adp/{environment}/agent-factory/bootstrap"
        # Stream name: correlation_id preferred, fallback to message_id, then "unknown"
        stream_name = correlation_id or message_id or f"unknown-{int(time.time())}"
        # Sanitize stream name (CloudWatch allows: . / # - but NOT ':')
        stream_name = stream_name.replace(":", "-")

        self._handler = CloudWatchBootstrapHandler(
            log_group=log_group,
            log_stream=stream_name,
            region=region,
        )
        self._handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

        # Also attach to the module-level logger so these lines go to stdout AND CloudWatch
        self._logger = logging.getLogger("bootstrap")
        self._logger.setLevel(logging.INFO)
        self._logger.addHandler(self._handler)
        # Propagate to root so messages also appear on stdout
        self._logger.propagate = True

    @property
    def is_active(self) -> bool:
        """Whether CloudWatch handler initialized successfully."""
        return self._handler._initialized and not self._handler._failed

    def step_start(self, step: int, name: str, **context: Any) -> None:
        """Log the start of a bootstrap step."""
        self._current_step = step
        self._current_step_name = name
        ctx_str = " ".join(f"{k}={v}" for k, v in context.items()) if context else ""
        msg = f"[bootstrap step={step} name={name}] ENTER"
        if ctx_str:
            msg += f" {ctx_str}"
        if self._correlation_id:
            msg += f" correlation_id={self._correlation_id}"
        self._logger.info(msg)

    def step_success(self, step: int, name: str, **context: Any) -> None:
        """Log successful completion of a bootstrap step."""
        ctx_str = " ".join(f"{k}={v}" for k, v in context.items()) if context else ""
        msg = f"[bootstrap step={step} name={name}] OK"
        if ctx_str:
            msg += f" {ctx_str}"
        if self._correlation_id:
            msg += f" correlation_id={self._correlation_id}"
        self._logger.info(msg)

    def step_error(self, step: int, name: str, exc: BaseException) -> None:
        """Log a bootstrap step failure with exception details and traceback."""
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        tb_str = "".join(tb[-3:])  # Last 3 frames — keeps it concise
        msg = (
            f"[bootstrap step={step} name={name}] FAILED "
            f"exception={type(exc).__name__} message={exc}"
        )
        if self._correlation_id:
            msg += f" correlation_id={self._correlation_id}"
        msg += f"\n{tb_str}"
        self._logger.error(msg)

    def log_fatal(self, exc: BaseException) -> None:
        """Log a fatal bootstrap exception (wraps whatever step was in progress)."""
        step = self._current_step or 0
        name = self._current_step_name or "unknown"
        self.step_error(step, name, exc)
        self.close()

    def close(self) -> None:
        """Flush remaining buffered logs to CloudWatch."""
        try:
            self._handler.flush()
        except Exception:
            pass
