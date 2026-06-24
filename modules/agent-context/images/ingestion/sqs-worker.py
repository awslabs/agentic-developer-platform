#!/usr/bin/env python3
"""SQS Worker — receives one message from the ingestion queue and processes it.

Designed to run as a KEDA ScaledJob: one pod per message, exits after processing.
Routes to the correct ingestion script based on content_type in the SQS message.

Usage:
  python sqs-worker.py  # Processes one message then exits
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3

from metrics import record_ingestion_duration, setup_metrics, shutdown_metrics
from telemetry import configure_telemetry, get_logger, safe_emit, set_correlation_context
from tracing import get_tracer, setup_tracing, shutdown_tracing

configure_telemetry(service_name="knowledge-layer-ingestion")
setup_tracing(service_name="knowledge-layer-ingestion")
setup_metrics(service_name="knowledge-layer")
log = get_logger("sqs-worker")
_tracer = get_tracer("knowledge-layer.sqs-worker")

# ---------------------------------------------------------------------------
# Configuration (centralized via config.py)
# ---------------------------------------------------------------------------

from config import settings
from github_auth import mint_github_token
from scope import parse_scope

AWS_REGION = settings.aws_region
SQS_QUEUE_URL = settings.sqs_queue_url
DYNAMO_TABLE = settings.dynamo_table

# Timeouts per content type (seconds)
TIMEOUTS = {
    "repo": 900,  # 15 min
    "url": 600,  # 10 min
    "doc": 300,  # 5 min
    "infra": 300,  # 5 min
}

# AWS clients
sqs = boto3.client("sqs", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)


# ---------------------------------------------------------------------------
# DynamoDB state helpers
# ---------------------------------------------------------------------------


def update_dynamo_status(
    source: str,
    content_type: str,
    status: str,
    error: str | None = None,
    tags: dict[str, str] | None = None,
    extra_attrs: dict[str, Any] | None = None,
):
    """Update the STATE record in DynamoDB for a source."""
    table = dynamodb.Table(DYNAMO_TABLE)
    pk = f"{content_type}#{source}"
    now = datetime.now(timezone.utc).isoformat()

    update_expr = "SET content_type = :ct, updated_at = :now"
    expr_values: dict[str, Any] = {
        ":ct": content_type,
        ":now": now,
    }

    # Set per-step status fields based on the overall status
    for step in [
        "s3_status",
        "deepwiki_status",
        "graphrag_status",
        "code_index_status",
        "sbom_source_status",
    ]:
        if status == "processing":
            update_expr += f", {step} = :pending"
            expr_values[":pending"] = "pending"
        elif status == "complete":
            update_expr += f", {step} = :complete"
            expr_values[":complete"] = "complete"
        elif status == "failed":
            update_expr += f", {step} = :failed"
            expr_values[":failed"] = "failed"

    if error:
        update_expr += ", last_error = :err"
        expr_values[":err"] = error
    else:
        update_expr += ", last_error = :null"
        expr_values[":null"] = None

    if tags:
        update_expr += ", user_tags = :tags"
        expr_values[":tags"] = tags

    if extra_attrs:
        import re as _re

        for k, v in extra_attrs.items():
            # Sanitize attribute name to prevent DynamoDB expression injection
            safe_k = _re.sub(r"[^a-zA-Z0-9_]", "_", k)
            update_expr += f", {safe_k} = :{safe_k}"
            expr_values[f":{safe_k}"] = v

    try:
        table.update_item(
            Key={"source": pk, "record_type": "STATE"},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
        )
        log.info("DynamoDB state updated: %s -> %s", pk, status)
    except Exception as e:
        log.error("DynamoDB update failed for %s: %s", pk, e)


def write_dynamo_run_record(
    source: str,
    content_type: str,
    status: str,
    duration_sec: float,
    trigger: str = "unknown",
    steps: dict[str, str] | None = None,
    error: str | None = None,
):
    """Write an append-only RUN record to DynamoDB (TTL: 30 days)."""
    table = dynamodb.Table(DYNAMO_TABLE)
    pk = f"{content_type}#{source}"
    now = datetime.now(timezone.utc)
    sk = f"RUN#{now.isoformat()}"
    ttl_epoch = int(now.timestamp()) + (30 * 86400)  # 30 days

    item = {
        "source": pk,
        "record_type": sk,
        "trigger": trigger,
        "status": status,
        "duration_sec": Decimal(str(round(duration_sec, 1))),
        "ttl": ttl_epoch,
        "created_at": now.isoformat(),
    }
    if steps:
        item["steps"] = steps
    if error:
        item["error"] = error[:1000]  # Truncate long errors

    try:
        table.put_item(Item=item)
        log.info("DynamoDB run record written: %s %s", pk, status)
    except Exception as e:
        log.error("DynamoDB run record failed for %s: %s", pk, e)


# ---------------------------------------------------------------------------
# Ingestion routing
# ---------------------------------------------------------------------------


def ingest_repo(source: str, tags: dict[str, str], steps: list[str] | None = None) -> None:
    """Run ingest-repo.py for a repository."""
    cmd = [
        sys.executable,
        "/app/ingest-repo.py",
        "--repo",
        source,
    ]
    if tags:
        cmd.extend(["--tags", json.dumps(tags)])
    _run_subprocess(cmd, timeout=TIMEOUTS["repo"])


def ingest_url(source: str, tags: dict[str, str]) -> None:
    """Run ingest-url.py for a URL."""
    cmd = [
        sys.executable,
        "/app/ingest-url.py",
        "--url",
        source,
        "--max-pages",
        "100",
    ]
    if tags:
        cmd.extend(["--tags", json.dumps(tags)])
    _run_subprocess(cmd, timeout=TIMEOUTS["url"])


def ingest_doc(source: str, tags: dict[str, str], title: str | None = None) -> None:
    """Run ingest-doc.py for a document."""
    cmd = [
        sys.executable,
        "/app/ingest-doc.py",
        "--source",
        source,
    ]
    if title:
        cmd.extend(["--title", title])
    if tags:
        cmd.extend(["--tags", json.dumps(tags)])
    _run_subprocess(cmd, timeout=TIMEOUTS["doc"])


def discover_infra(source: str, tags: dict[str, str]) -> None:
    """Run discover-infra.py for an AWS account."""
    cmd = [
        sys.executable,
        "/app/discover-infra.py",
        "--account",
        source,
    ]
    if tags:
        cmd.extend(["--tags", json.dumps(tags)])
    _run_subprocess(cmd, timeout=TIMEOUTS["infra"])


def _run_subprocess(cmd: list[str], timeout: int) -> None:
    """Run a subprocess with timeout, streaming output through structured logging.

    Streams child stdout/stderr line-by-line through the parent's logger (which
    carries correlation context). Emits bookend events (start/complete/fail) with
    duration and exit code.

    This fixes the lost-subprocess-logs gap: previously output was captured via
    capture_output=True and truncated to 500 chars, losing SCIP/cgc/syft diagnostics.
    """
    import threading
    import time as _time

    # Propagate telemetry config to child so it emits structured JSON
    child_env = os.environ.copy()
    child_env.setdefault("KNOWLEDGE_LAYER_TELEMETRY_ENABLED", "true")
    child_env.setdefault("LOG_FORMAT", os.environ.get("LOG_FORMAT", "json"))
    child_env.setdefault(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "http://adot-collector.adp-agents.svc.cluster.local:4317",
        ),
    )

    cmd_summary = " ".join(cmd[:6])
    log.info("subprocess.start: %s (timeout=%ds)", cmd_summary, timeout)
    start = _time.monotonic()

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=child_env,
    )

    # Stream output line-by-line in a reader thread (so timeout works even if
    # the child produces no output — e.g. a long-running computation or sleep)
    last_lines: list[str] = []  # Keep tail for error context

    def _read_output():
        try:
            assert process.stdout is not None
            for line in process.stdout:
                stripped = line.rstrip("\n")
                if stripped:
                    log.info("subprocess: %s", stripped)
                    last_lines.append(stripped)
                    if len(last_lines) > 50:
                        last_lines.pop(0)
        except Exception as e:
            log.warning("subprocess output read error: %s", e)

    reader = threading.Thread(target=_read_output, daemon=True)
    reader.start()

    # Wait for process to complete (with timeout)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
        reader.join(timeout=5)
        duration = _time.monotonic() - start
        log.error(
            "subprocess.timeout: %s (killed after %.1fs)", cmd_summary, duration
        )
        raise subprocess.TimeoutExpired(cmd, timeout)

    # Wait for reader thread to finish draining output
    reader.join(timeout=10)
    duration = _time.monotonic() - start
    returncode = process.returncode

    if returncode != 0:
        tail = "\n".join(last_lines[-10:]) if last_lines else "(no output)"
        log.error(
            "subprocess.failed: %s exit_code=%d duration=%.1fs",
            cmd_summary,
            returncode,
            duration,
        )
        raise RuntimeError(
            f"Exit code {returncode}: {tail[:2000]}"
        )

    log.info("subprocess.complete: %s exit_code=0 duration=%.1fs", cmd_summary, duration)


# ---------------------------------------------------------------------------
# Main worker loop (single message)
# ---------------------------------------------------------------------------


def receive_sqs_message() -> tuple[dict[str, Any], str] | None:
    """Receive one message from SQS. Returns (message_body, receipt_handle) or None."""
    try:
        resp = sqs.receive_message(
            QueueUrl=SQS_QUEUE_URL,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            VisibilityTimeout=900,
            MessageAttributeNames=["All"],
        )
        messages = resp.get("Messages", [])
        if not messages:
            log.info("No messages in queue")
            return None

        msg = messages[0]
        body = json.loads(msg["Body"])
        receipt_handle = msg["ReceiptHandle"]
        return body, receipt_handle
    except Exception as e:
        log.error("Failed to receive SQS message: %s", e)
        return None


def delete_sqs_message(receipt_handle: str) -> None:
    """Delete a message from SQS (acknowledge successful processing)."""
    try:
        sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=receipt_handle)
        log.info("SQS message deleted")
    except Exception as e:
        log.error("Failed to delete SQS message: %s", e)


def _mint_github_token() -> bool:
    """Mint a GitHub App token and write to /tmp/github-token.

    Delegates to the shared github_auth module so sqs-worker and refresh-repos
    use one implementation (see #1682).

    Returns True if token was successfully obtained, False otherwise.
    Failure is non-fatal — anonymous clones still work for public repos.
    """
    return mint_github_token()


def main():
    if not SQS_QUEUE_URL:
        log.error("SQS_QUEUE_URL not set")
        sys.exit(1)

    # Mint GitHub App token before processing (enables private repo clones)
    _mint_github_token()

    result = receive_sqs_message()
    if result is None:
        log.info("No work to do — exiting")
        sys.exit(0)

    message, receipt_handle = result
    content_type = message.get("content_type", "unknown")
    source = message.get("source", "unknown")
    tags = message.get("tags", {})
    title = message.get("title")
    triggered_by = message.get("triggered_by", "unknown")
    steps = message.get("steps", [])

    # Parse scope envelope (backward-compatible: defaults to shared if absent)
    scope = parse_scope(message.get("scope"))

    # Set correlation context from SQS envelope (fail-open)
    safe_emit(
        set_correlation_context,
        asset_id=source,
        asset_type=content_type,
        owner_sub=scope.owner_sub,
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
    )

    log.info("Processing: %s (%s) triggered_by=%s", source, content_type, triggered_by)

    # Update DynamoDB status to "processing"
    update_dynamo_status(source, content_type, "processing", tags=tags)

    # Root span wrapping the entire ingestion run — child spans per stage
    # are created by StageTracker and become children via trace context propagation
    root_span_attrs = {
        "asset_id": source,
        "content_type": content_type,
        "triggered_by": triggered_by,
        "owner_sub": scope.owner_sub or "",
        "tenant_id": scope.tenant_id or "",
        "project_id": scope.project_id or "",
        "visibility": scope.visibility,
    }
    root_span_cm = _tracer.start_as_current_span(
        "ingestion_run", attributes=root_span_attrs
    )
    root_span = root_span_cm.__enter__()

    start_time = time.monotonic()
    try:
        if content_type == "repo":
            ingest_repo(source, tags=tags, steps=steps)
        elif content_type == "url":
            ingest_url(source, tags=tags)
        elif content_type == "doc":
            ingest_doc(source, tags=tags, title=title)
        elif content_type == "infra":
            discover_infra(source, tags=tags)
        else:
            raise ValueError(f"Unknown content_type: {content_type}")

        duration = time.monotonic() - start_time

        # Mark root span as successful (fail-open)
        try:
            from opentelemetry.trace import StatusCode

            root_span.set_attribute("duration_sec", round(duration, 1))
            root_span.set_status(StatusCode.OK)
        except Exception:
            pass

        # Success — update state and write run record
        update_dynamo_status(source, content_type, "complete", tags=tags)
        write_dynamo_run_record(
            source,
            content_type,
            "success",
            duration,
            trigger=triggered_by,
            steps={s: "ok" for s in steps},
        )
        # Emit ingestion duration metric (fail-open)
        safe_emit(
            record_ingestion_duration,
            tenant_id=scope.tenant_id or "",
            asset_type=content_type,
            duration_ms=duration * 1000,
        )

        delete_sqs_message(receipt_handle)
        log.info("Completed %s (%s) in %.1fs", source, content_type, duration)

    except Exception as e:
        duration = time.monotonic() - start_time
        error_msg = str(e)[:1000]
        log.error("Failed %s (%s) after %.1fs: %s", source, content_type, duration, error_msg)

        # Mark root span as failed (fail-open)
        try:
            from opentelemetry.trace import StatusCode

            root_span.record_exception(e)
            root_span.set_attribute("duration_sec", round(duration, 1))
            root_span.set_status(StatusCode.ERROR, error_msg[:256])
        except Exception:
            pass

        # Failure — update state but don't delete message (goes back to queue for retry, then DLQ)
        update_dynamo_status(source, content_type, "failed", error=error_msg, tags=tags)
        write_dynamo_run_record(
            source,
            content_type,
            "failed",
            duration,
            trigger=triggered_by,
            error=error_msg,
        )
        # End root span + flush before exit (fail-open)
        try:
            root_span_cm.__exit__(None, None, None)
        except Exception:
            pass
        safe_emit(shutdown_tracing)
        safe_emit(shutdown_metrics)
        sys.exit(1)

    # End root span on success (fail-open)
    try:
        root_span_cm.__exit__(None, None, None)
    except Exception:
        pass
    safe_emit(shutdown_tracing)
    safe_emit(shutdown_metrics)


if __name__ == "__main__":
    main()
