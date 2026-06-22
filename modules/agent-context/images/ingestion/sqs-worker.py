#!/usr/bin/env python3
"""SQS Worker — receives one message from the ingestion queue and processes it.

Designed to run as a KEDA ScaledJob: one pod per message, exits after processing.
Routes to the correct ingestion script based on content_type in the SQS message.

Usage:
  python sqs-worker.py  # Processes one message then exits
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("sqs-worker")

# ---------------------------------------------------------------------------
# Configuration (centralized via config.py)
# ---------------------------------------------------------------------------

from config import settings
from github_auth import mint_github_token

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
    """Run a subprocess with timeout, raising on failure."""
    log.info("Running: %s", " ".join(cmd[:6]) + "...")
    result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    stdout = result.stdout.decode()[:2000] if result.stdout else ""
    stderr = result.stderr.decode()[:2000] if result.stderr else ""
    if result.returncode != 0:
        raise RuntimeError(f"Exit code {result.returncode}: {stderr or stdout}")
    if stdout:
        log.info("Output: %s", stdout[:500])


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

    log.info("Processing: %s (%s) triggered_by=%s", source, content_type, triggered_by)

    # Update DynamoDB status to "processing"
    update_dynamo_status(source, content_type, "processing", tags=tags)

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
        delete_sqs_message(receipt_handle)
        log.info("Completed %s (%s) in %.1fs", source, content_type, duration)

    except Exception as e:
        duration = time.monotonic() - start_time
        error_msg = str(e)[:1000]
        log.error("Failed %s (%s) after %.1fs: %s", source, content_type, duration, error_msg)

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
        sys.exit(1)


if __name__ == "__main__":
    main()
