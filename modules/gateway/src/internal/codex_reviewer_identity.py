"""Authorize the standalone Codex reviewer at the GitHub token boundary.

The reviewer is not an ``authority-worker`` and never receives the hosted
worker's run credential.  Its proof is the conjunction of two independent
facts already owned by the platform:

* API Gateway authenticated the dedicated ``agent-codex-reviewer`` IRSA role;
* webhook ingress durably recorded an eligible pull-request delivery before it
  published that delivery to the reviewer's private queue.

The queue envelope is therefore an assertion that is checked against the
immutable activity-row fields.  Reviewer status updates cannot rewrite those
fields (the workload IAM policy restricts UpdateItem to lifecycle attributes).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from src.internal.credential_binding import InstallationBinding
from src.shared.config import get_settings

logger = logging.getLogger(__name__)

CODEX_REVIEWER_AGENT = "agent-codex-reviewer"
_ELIGIBLE_ACTIONS = frozenset({"opened", "synchronize"})
_LIVE_STATUSES = frozenset({"webhook_received", "in_progress"})
_READ_PERMISSIONS = {
    "contents": "read",
    "pull_requests": "write",
    "issues": "write",
    "checks": "read",
    "metadata": "read",
}


class CodexReviewerAction(StrEnum):
    """Plain internal-plane action marker; intentionally not orchestration state."""

    REVIEW = "review"


@dataclass(frozen=True)
class CodexReviewerBinding:
    invocation_id: str
    arrived_at: str
    tenant_id: str
    installation_id: int
    repository: str


def _table(table_name: str, region: str):
    return boto3.resource("dynamodb", region_name=region).Table(table_name)


def load_codex_reviewer_binding(
    *,
    invocation_id: str,
    requested_installation_id: int,
    requested_repository: str,
    table_name: str,
    region: str,
) -> CodexReviewerBinding:
    """Resolve one reviewer assignment from the ingress-owned activity row."""

    if not invocation_id or not requested_repository:
        raise HTTPException(403, "Codex reviewer assignment is missing")
    try:
        response = _table(table_name, region).query(
            KeyConditionExpression=Key("event_id").eq(invocation_id),
            ProjectionExpression=("event_id, arrived_at, tenant_id, installation_id, repo, persona, event_type, #action, #status"),
            ExpressionAttributeNames={"#action": "action", "#status": "status"},
            ScanIndexForward=False,
            Limit=1,
            ConsistentRead=True,
        )
    except (ClientError, BotoCoreError):
        logger.warning("Codex reviewer assignment lookup unavailable", extra={"invocation_id": invocation_id})
        raise HTTPException(503, "Codex reviewer assignment is unavailable") from None

    rows = response.get("Items", [])
    row = rows[0] if rows else None
    try:
        installation_id = int((row or {}).get("installation_id", ""))
    except (TypeError, ValueError):
        installation_id = 0
    if (
        not row
        or row.get("event_id") != invocation_id
        or row.get("persona") != "codex-reviewer"
        or row.get("event_type") != "pull_request"
        or row.get("action") not in _ELIGIBLE_ACTIONS
        or row.get("status") not in _LIVE_STATUSES
        or installation_id != requested_installation_id
        or row.get("repo") != requested_repository
        or not row.get("tenant_id")
        or not row.get("arrived_at")
    ):
        logger.info("Codex reviewer assignment refused", extra={"invocation_id": invocation_id})
        raise HTTPException(403, "Codex reviewer assignment is invalid")
    return CodexReviewerBinding(
        invocation_id=invocation_id,
        arrived_at=str(row["arrived_at"]),
        tenant_id=str(row["tenant_id"]),
        installation_id=installation_id,
        repository=requested_repository,
    )


async def verify_codex_reviewer_broker(request: Request) -> None:
    """Bind a token request to the standalone reviewer's recorded assignment."""

    identity = getattr(request.state, "token_context", None)
    if identity is None or identity.user_id != CODEX_REVIEWER_AGENT or identity.scope != "internal":
        raise HTTPException(403, "Codex reviewer identity required")
    try:
        body = await request.json()
        installation_id = int(body.get("installation_id", 0))
        repository = f"{body.get('repo_owner', '')}/{body.get('repo_name', '')}"
        invocation_id = str(body.get("invocation_id") or "")
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(403, "Codex reviewer request is invalid") from None
    settings = get_settings()
    binding = await run_in_threadpool(
        load_codex_reviewer_binding,
        invocation_id=invocation_id,
        requested_installation_id=installation_id,
        requested_repository=repository,
        table_name=settings.webhook_events_table,
        region=settings.aws_region,
    )
    request.state.codex_reviewer_binding = binding
    request.state.agent_installation_binding = InstallationBinding(
        tenant_id=binding.tenant_id,
        installation_id=binding.installation_id,
    )
    request.state.agent_authorized_action = CodexReviewerAction.REVIEW
    permissions = dict(_READ_PERMISSIONS)
    scopes = set(getattr(identity, "credential_scopes", []) or [])
    if scopes & {"codex:branch-write", "codex:merge"}:
        permissions["contents"] = "write"
    request.state.agent_github_permissions = permissions


__all__ = [
    "CODEX_REVIEWER_AGENT",
    "CodexReviewerAction",
    "CodexReviewerBinding",
    "load_codex_reviewer_binding",
    "verify_codex_reviewer_broker",
]
