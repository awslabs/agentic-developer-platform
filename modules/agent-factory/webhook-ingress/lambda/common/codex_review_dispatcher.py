"""Build and publish the independent Codex SDK reviewer envelope.

This module intentionally knows nothing about personas, Claude Agent SDK, or
the hosted agent envelope. Its only input is an already-authenticated GitHub PR
webhook and its only output is the dedicated codex-review FIFO queue.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime

import boto3

_AGENT_BRANCH = re.compile(r"^agent/issue-(\d+)(?:-[A-Za-z0-9._-]+)?$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_sqs = None


def enabled() -> bool:
    return os.environ.get("CODEX_REVIEWER_ENABLED", "false").lower() == "true"


def eligible(event_type: str, payload: dict) -> bool:
    if not enabled() or event_type != "pull_request":
        return False
    if payload.get("action") not in {"opened", "synchronize"}:
        return False
    repo = (payload.get("repository") or {}).get("full_name", "")
    pr = payload.get("pull_request") or {}
    head = pr.get("head") or {}
    head_repo = (head.get("repo") or {}).get("full_name", "")
    return bool(
        repo
        and head_repo == repo
        and _AGENT_BRANCH.fullmatch(head.get("ref", ""))
        and _SHA.fullmatch(head.get("sha", ""))
    )


def build_envelope(
    payload: dict,
    *,
    tenant_id: str,
    message_id: str | None = None,
    correlation_ctx: dict | None = None,
) -> dict:
    pr = payload["pull_request"]
    head_ref = pr["head"]["ref"]
    match = _AGENT_BRANCH.fullmatch(head_ref)
    if not match:
        raise ValueError("PR head is not an agent issue branch")
    correlation = correlation_ctx or {}
    return {
        "version": "1.0",
        "kind": "codex_pr_review",
        "message_id": message_id or str(uuid.uuid4()),
        "arrived_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tenant_id": tenant_id,
        "installation_id": int(payload["installation"]["id"]),
        "repository": payload["repository"]["full_name"],
        "pull_request": {
            "number": int(pr["number"]),
            "issue_number": int(match.group(1)),
            "head_ref": head_ref,
            "base_ref": pr["base"]["ref"],
            "expected_head_sha": pr["head"]["sha"],
            "html_url": pr["html_url"],
        },
        "correlation": {
            "correlation_id": correlation.get("correlation_id", ""),
            "root_human_id": correlation.get("root_human_id", ""),
            "parent_invocation_id": correlation.get("parent_invocation_id"),
        },
    }


def _client():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client(
            "sqs", region_name=os.environ.get("AWS_REGION", "us-east-1")
        )
    return _sqs


def publish(envelope: dict) -> str | None:
    queue_url = os.environ.get("CODEX_REVIEW_QUEUE_URL", "")
    if not queue_url:
        return None
    try:
        response = _client().send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(envelope, separators=(",", ":")),
            MessageGroupId=(
                f"{envelope['tenant_id']}#{envelope['repository']}#"
                f"pr-{envelope['pull_request']['number']}"
            )[:128],
            MessageDeduplicationId=envelope["message_id"][:128],
        )
        return response.get("MessageId")
    except Exception:  # noqa: BLE001 - caller converts a failed publish into 500
        return None
