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
import tempfile
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
from github_auth import InstallationRevokedError, mint_github_token, mint_installation_token
from scope import asset_storage_prefix, validate_ingestion_attempt, ScopeValidationError, parse_scope
from source_admission import SourceAdmissionError, validate_source
from status_callback import emit_status_callback
from ingestion_result import RESULT_ENV, read as read_ingestion_result
import job_client

AWS_REGION = settings.aws_region
SQS_QUEUE_URL = settings.sqs_queue_url
DYNAMO_TABLE = settings.dynamo_table

# Timeouts per content type (seconds)
TIMEOUTS = {
    "repo": 3600,  # 60 min — large Python repos need ~10-20 min for Neptune load (#3160)
    "url": 600,  # 10 min
    "doc": 300,  # 5 min
    "infra": 300,  # 5 min
}

# AWS clients
sqs = None if job_client.enabled() else boto3.client("sqs", region_name=AWS_REGION)
dynamodb = None if job_client.enabled() else boto3.resource("dynamodb", region_name=AWS_REGION)


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
    steps: dict[str, str] | None = None,
):
    """Update the STATE record in DynamoDB for a source."""
    if job_client.enabled():
        job_client.call("state", {"status": status, "error": error, "steps": steps or {}})
        return
    table = dynamodb.Table(DYNAMO_TABLE)
    pk = f"{content_type}#{source}"
    now = datetime.now(timezone.utc).isoformat()

    update_expr = "SET content_type = :ct, updated_at = :now"
    expr_values: dict[str, Any] = {
        ":ct": content_type,
        ":now": now,
    }

    update_expr += ", ingestion_status = :ingestion_status"
    expr_values[":ingestion_status"] = status
    # Project only reported stages. A clean process exit does not prove every
    # producer ran, and skipped optional backends must stay skipped.
    for field, stage in {
        "s3_status": "s3_upload", "deepwiki_status": "deepwiki",
        "graphrag_status": "graphrag", "code_index_status": "cgc_structural",
        "sbom_source_status": "sbom_source",
    }.items():
        value = (steps or {}).get(stage)
        if value is not None:
            update_expr += f", {field} = :{field}"
            expr_values[f":{field}"] = "complete" if value == "verified" else value

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
    if job_client.enabled():
        job_client.call("history", {"status": status, "duration_sec": duration_sec,
                                    "steps": steps or {}, "error": error})
        return
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


def ingest_repo(
    source: str,
    tags: dict[str, str],
    steps: list[str] | None = None,
    scope_env: dict[str, str] | None = None,
) -> dict:
    """Run ingest-repo.py for a repository."""
    cmd = [
        sys.executable,
        "/app/ingest-repo.py",
        "--repo",
        source,
    ]
    if tags:
        cmd.extend(["--tags", json.dumps(tags)])
    return _run_subprocess(cmd, timeout=TIMEOUTS["repo"], extra_env=scope_env)


def ingest_url(
    source: str,
    tags: dict[str, str],
    scope_env: dict[str, str] | None = None,
    registry_asset_id: str | None = None,
) -> dict:
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
    if registry_asset_id:
        cmd.extend(["--registry-asset-id", registry_asset_id])
    return _run_subprocess(cmd, timeout=TIMEOUTS["url"], extra_env=scope_env)


def ingest_doc(
    source: str,
    tags: dict[str, str],
    title: str | None = None,
    scope_env: dict[str, str] | None = None,
    registry_asset_id: str | None = None,
) -> dict:
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
    if registry_asset_id:
        cmd.extend(["--registry-asset-id", registry_asset_id])
    return _run_subprocess(cmd, timeout=TIMEOUTS["doc"], extra_env=scope_env)


def discover_infra(
    source: str, tags: dict[str, str], scope_env: dict[str, str] | None = None
) -> dict:
    """Run discover-infra.py for an AWS account."""
    cmd = [
        sys.executable,
        "/app/discover-infra.py",
        "--account",
        source,
    ]
    return _run_subprocess(cmd, timeout=TIMEOUTS["infra"], extra_env=scope_env)


class IngestionFailed(RuntimeError):
    def __init__(self, result: dict):
        self.result = result
        failed = ", ".join(k for k, v in result["steps"].items() if v == "failed")
        super().__init__(f"Ingestion failed: {failed or 'producer reported failure'}")


def _run_subprocess(cmd: list[str], timeout: int, extra_env: dict[str, str] | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="ingestion-result-") as directory:
        path = os.path.join(directory, "result.json")
        process_error = None
        try:
            _stream_subprocess(cmd, timeout, {**(extra_env or {}), RESULT_ENV: path})
        except RuntimeError as exc:
            process_error = exc
        try:
            result = read_ingestion_result(path)
        except (OSError, ValueError, TypeError) as exc:
            if process_error:
                raise process_error from exc
            raise RuntimeError("Ingestion produced no valid result") from exc
        if process_error and result["status"] == "complete":
            raise process_error
        return result


def _stream_subprocess(cmd: list[str], timeout: int, extra_env: dict[str, str] | None = None) -> int:
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

    # Propagate scope and any other extra env vars to child process
    if extra_env:
        child_env.update(extra_env)

    cmd_summary = " ".join(cmd[:6])
    log.info("subprocess.start: %s (timeout=%ds)", cmd_summary, timeout)
    start = _time.monotonic()

    process = subprocess.Popen(  # nosemgrep: dangerous-subprocess-use-audit
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=child_env,
    )

    # Stream output line-by-line in a reader thread (so timeout works even if
    # the child produces no output — e.g. a long-running computation or sleep)
    last_lines: list[str] = []  # Bounded failure diagnostics

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
        log.error("subprocess.timeout: %s (killed after %.1fs)", cmd_summary, duration)
        raise subprocess.TimeoutExpired(cmd, timeout)  # nosemgrep: dangerous-subprocess-use-audit

    # Wait for reader thread to finish draining output
    reader.join(timeout=10)
    duration = _time.monotonic() - start
    returncode = process.returncode

    if returncode != 0:
        log.error(
            "subprocess.failed: %s exit_code=%d duration=%.1fs",
            cmd_summary,
            returncode,
            duration,
        )
        tail = "\n".join(last_lines[-10:]) if last_lines else "(no output)"
        raise RuntimeError(f"Exit code {returncode}: {tail[:2000]}")

    log.info("subprocess.complete: %s exit_code=0 duration=%.1fs", cmd_summary, duration)
    return 0


# ---------------------------------------------------------------------------
# Main worker loop (single message)
# ---------------------------------------------------------------------------


def receive_sqs_message() -> tuple[dict[str, Any], str] | None:
    """Receive one message from SQS. Returns (message_body, receipt_handle) or None."""
    if job_client.enabled():
        from pathlib import Path
        raw = json.loads(Path(os.environ["INGESTION_JOB_FILE"]).read_text())
        if raw.get("empty") is True:
            return None
        admitted = job_client.job()
        return admitted, admitted["receipt_ticket"]
    if os.environ.get("INGESTION_ISOLATION_REQUIRED") == "true":
        raise RuntimeError("Queue access belongs to the admission init container")
    try:
        resp = sqs.receive_message(
            QueueUrl=SQS_QUEUE_URL,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            VisibilityTimeout=3600,
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
    if job_client.enabled():
        job_client.call("ack", {"receipt_ticket": receipt_handle})
        return
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
    if job_client.enabled():
        return job_client.install_github_token()
    return mint_github_token()


def _mint_per_installation_token(installation_id: int) -> bool:
    """Mint a token for a specific GitHub App installation (issue #2088).

    Per-pod installation auth: the asset carries its installation_id from
    register time. We mint a token scoped to exactly that installation.

    Returns True on success.

    Raises:
        InstallationRevokedError: installation no longer exists (fail-closed).
        RuntimeError: transient/config error.
    """
    if job_client.enabled():
        if installation_id != job_client.job().get("installation_id"):
            raise RuntimeError("Ingestion installation differs from admission")
        return job_client.install_github_token()
    return mint_installation_token(installation_id)


def main():
    if not SQS_QUEUE_URL and not job_client.enabled():
        log.error("SQS_QUEUE_URL not set")
        sys.exit(1)

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
    registry_asset_id = message.get("registry_asset_id")
    installation_id = message.get("installation_id")  # Per-pod auth (#2088)
    # Issue #5663 (A09): the gateway's server-minted authority for THIS asset.
    # Opaque here by design — the worker forwards it verbatim on every status
    # callback and never constructs or modifies it. Absent on messages published
    # before the gateway change; the gateway counts those as unbound rather than
    # trusting the body.
    callback_grant = message.get("callback_grant")

    # Require an explicit ownership envelope; missing or invalid scope is fatal
    # (#5658): the old behaviour downgraded it to shared, which published
    # tenant- or user-scoped content to the prefix every tenant can read. The
    # message is abandoned without deleting the receipt, so it retries and then
    # lands in the DLQ for inspection rather than being silently mis-ingested.
    try:
        scope = parse_scope(message.get("scope"))
        validate_source(
            content_type, source, scope, default_bucket=settings.s3_bucket_name,
            allowlist=settings.s3_source_allowlist, allow_infra=True,
        )
    except (ScopeValidationError, SourceAdmissionError) as e:
        log.error("Refusing message with unsatisfiable scope: %s", e)
        sys.exit(1)

    # Export scope as env vars for child processes (S3 prefix routing in #1773)
    if scope.is_product or registry_asset_id:
        asset_storage_prefix(scope, "content", registry_asset_id)
    scope_env = scope.to_env()
    scope_env["INGESTION_REGISTRY_ASSET_ID"] = registry_asset_id or ""
    scope_env["INGESTION_ATTEMPT_ID"] = message.get("ingestion_attempt_id") or ""
    if scope.is_product or (scope.is_personal and registry_asset_id):
        validate_ingestion_attempt(message.get("ingestion_attempt_id"))
    state_source = registry_asset_id if scope.is_product or (scope.is_personal and registry_asset_id) else source

    # Set correlation context from SQS envelope (fail-open)
    safe_emit(
        set_correlation_context,
        asset_id=source,
        asset_type=content_type,
        owner_sub=scope.owner_sub,
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
    )

    # --- Per-pod installation auth (issue #2088) ---
    # If the asset carries an installation_id (private repo registered via
    # story #4), mint a token for THAT specific installation. If absent
    # (public/shared asset), fall back to anonymous clone (no mint).
    # Fail-closed: if the installation is revoked, abort immediately.
    if installation_id and content_type == "repo":
        try:
            _mint_per_installation_token(int(installation_id))
        except InstallationRevokedError as e:
            error_msg = f"access_revoked: {e}"
            log.error(
                "Installation revoked for %s (installation_id=%s): %s",
                source,
                installation_id,
                e,
            )
            update_dynamo_status(state_source, content_type, "failed", error=error_msg, tags=tags)
            write_dynamo_run_record(
                state_source,
                content_type,
                "failed",
                0.0,
                trigger=triggered_by,
                error=error_msg,
            )
            safe_emit(
                emit_status_callback,
                registry_asset_id,
                "failed",
                status_detail={"reason": "access_revoked"},
                error=error_msg,
                tenant_id=scope.tenant_id,
                callback_grant=callback_grant,
            )
            # Fail-closed: delete message (no retry — installation won't come back)
            # and exit. The asset status surfaces the revocation to the user.
            delete_sqs_message(receipt_handle)
            sys.exit(1)
        except RuntimeError as e:
            # Transient/config error — let the message return to queue for retry
            log.error(
                "Token mint failed for %s (installation_id=%s): %s",
                source,
                installation_id,
                e,
            )
            sys.exit(1)
    elif not installation_id and content_type == "repo":
        # Public/shared asset — use legacy global mint (non-fatal if it fails)
        _mint_github_token()
    # Non-repo content types don't need GitHub auth

    log.info("Processing: %s (%s) triggered_by=%s", source, content_type, triggered_by)

    # Update DynamoDB status to "processing"
    update_dynamo_status(state_source, content_type, "processing", tags=tags)

    # Emit status callback: worker "processing" → gateway "indexing" (#2049)
    safe_emit(emit_status_callback, registry_asset_id, "indexing", tenant_id=scope.tenant_id, callback_grant=callback_grant)

    # Root span wrapping the entire ingestion run — child spans per stage
    # are created by StageTracker and become children via trace context propagation
    root_span_attrs = {
        "asset_id": source,
        "content_type": content_type,
        "triggered_by": triggered_by,
        "owner_sub": scope.owner_sub or "",
        "tenant_id": scope.tenant_id or "",
        "project_id": scope.project_id or "",
        "product_id": scope.product_id or "",
        "visibility": scope.visibility,
    }
    root_span_cm = _tracer.start_as_current_span("ingestion_run", attributes=root_span_attrs)
    root_span = root_span_cm.__enter__()

    start_time = time.monotonic()
    try:
        if content_type == "repo":
            outcome = ingest_repo(source, tags=tags, steps=steps, scope_env=scope_env)
        elif content_type == "url":
            outcome = ingest_url(source, tags=tags, scope_env=scope_env, registry_asset_id=registry_asset_id)
        elif content_type == "doc":
            outcome = ingest_doc(source, tags=tags, title=title, scope_env=scope_env, registry_asset_id=registry_asset_id)
        elif content_type == "infra":
            outcome = discover_infra(source, tags=tags, scope_env=scope_env)
        else:
            raise ValueError(f"Unknown content_type: {content_type}")

        if outcome["status"] == "failed":
            raise IngestionFailed(outcome)
        duration = time.monotonic() - start_time

        # Reflect partial ingestion in telemetry as well as the registry.
        try:
            from opentelemetry.trace import StatusCode

            root_span.set_attribute("duration_sec", round(duration, 1))
            if outcome["status"] == "complete":
                root_span.set_status(StatusCode.OK)
            else:
                root_span.set_status(StatusCode.ERROR, "partial ingestion")
        except Exception:
            pass

        # Persist the producer result without inventing stage outcomes.
        update_dynamo_status(state_source, content_type, outcome["status"], tags=tags, steps=outcome["steps"])
        write_dynamo_run_record(
            state_source,
            content_type,
            "success" if outcome["status"] == "complete" else "partial",
            duration,
            trigger=triggered_by,
            steps=outcome["steps"],
        )

        # Emit the actual terminal status and its compact stage projection.
        safe_emit(
            emit_status_callback,
            registry_asset_id,
            outcome["status"],
            status_detail={
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "duration_sec": round(duration, 1),
                "steps": outcome["steps"],
                "run_id": outcome.get("run_id"),
            },
            tenant_id=scope.tenant_id,
            callback_grant=callback_grant,
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
        failed_steps = e.result["steps"] if isinstance(e, IngestionFailed) else {}
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
        update_dynamo_status(state_source, content_type, "failed", error=error_msg, tags=tags, steps=failed_steps)
        write_dynamo_run_record(
            state_source,
            content_type,
            "failed",
            duration,
            trigger=triggered_by,
            error=error_msg,
            steps=failed_steps,
        )

        # Emit status callback: failed with error in status_detail (#2049)
        safe_emit(
            emit_status_callback,
            registry_asset_id,
            "failed",
            status_detail={
                "steps": failed_steps,
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "duration_sec": round(duration, 1),
            },
            error=error_msg,
            tenant_id=scope.tenant_id,
            callback_grant=callback_grant,
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
