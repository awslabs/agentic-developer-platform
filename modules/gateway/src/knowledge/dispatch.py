"""Phase-1 inline SQS dispatch for knowledge-asset ingestion.

Issue #2045: Relocated from agent_context.ingestion.dispatch into the gateway.
Original: Issue #1797 (Story H of E10 #1736).
Design reference: docs/agent-context/design-1736-knowledge-asset-registry.md S8.9.

Pattern: row-before-publish invariant.
  1. Row is INSERT'd at status='registered' (caller's responsibility)
  2. Commit an exclusive callback attempt, then publish to SQS.
  3. On success, update status to 'queued' if the attempt is still current.
  4. An uncertain publish retains the attempt and is never automatically replayed.
     An authorized /reindex resets the attempt and invalidates its old grant.

Phase 2 transition: when the sweeper CronJob deploys, these inline calls are removed.
The gateway then loses sqs:SendMessage permission (additive, not breaking).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.knowledge.ingestion_callback_grant import GrantError, mint_ingestion_grant
from src.knowledge.source_admission import SourceAdmissionError, admit_source
from src.knowledge.type_registry import ASSET_TYPE_REGISTRY

logger = logging.getLogger("bedrockgateway.knowledge.dispatch")


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class IngestionQueueUnavailableError(Exception):
    """Raised when INGESTION_QUEUE_URL is not configured.

    Callers (routes) should catch this and surface a 503 to the client,
    rather than letting it be swallowed by the generic dispatch try/except.
    """


# ---------------------------------------------------------------------------
# SQS client protocol — allows injection for testing
# ---------------------------------------------------------------------------


class SQSClient(Protocol):
    """Protocol for SQS message publishing (matches boto3 SQS client subset)."""

    def send_message(
        self,
        QueueUrl: str,  # noqa: N803
        MessageBody: str,  # noqa: N803
        MessageAttributes: dict[str, Any] | None = None,  # noqa: N803
    ) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Module-level SQS client (lazy-initialized)
# ---------------------------------------------------------------------------

_sqs_client: SQSClient | None = None


def get_sqs_client() -> SQSClient:
    """Get or create the module-level SQS client.

    Uses boto3 by default. Tests override via set_sqs_client().
    """
    global _sqs_client
    if _sqs_client is None:
        import boto3

        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
        _sqs_client = boto3.client("sqs", region_name=region)
    return _sqs_client


def set_sqs_client(client: SQSClient | None) -> None:
    """Override the module-level SQS client (for testing)."""
    global _sqs_client
    _sqs_client = client


def get_queue_url() -> str:
    """Read ingestion queue URL from environment.

    Env var: INGESTION_QUEUE_URL (same as used by sqs-worker.py and publish-ingestion.py).

    Raises:
        IngestionQueueUnavailableError: when INGESTION_QUEUE_URL is not set.
    """
    url = os.environ.get("INGESTION_QUEUE_URL", "")
    if not url:
        raise IngestionQueueUnavailableError(
            "INGESTION_QUEUE_URL not set — cannot dispatch ingestion. Set this env var on the gateway pod to enable knowledge-asset dispatch."
        )
    return url


# ---------------------------------------------------------------------------
# Source identifier extraction
# ---------------------------------------------------------------------------


def extract_source_identifier(source_ref: str, asset_type: str) -> str:
    """Extract the canonical source identifier from source_ref.

    For repos: strip the URL prefix to get org/repo (e.g. "acme/my-service").
    For URLs/docs: return source_ref as-is (the full URL/path is the identifier).
    """
    if asset_type == "repo":
        # Strip GitHub URL prefix to get org/repo
        for prefix in ("https://github.com/", "git@github.com:"):
            if source_ref.startswith(prefix):
                identifier = source_ref[len(prefix) :]
                # Strip trailing .git if present
                if identifier.endswith(".git"):
                    identifier = identifier[:-4]
                # Strip trailing slash
                return identifier.rstrip("/")
    return source_ref


# ---------------------------------------------------------------------------
# Scope envelope builder
# ---------------------------------------------------------------------------


def build_scope_envelope(
    tenant_id: str | None,
    owner_sub: str | None,
    project_id: str | None,
) -> dict[str, Any]:
    """Build the scope dict for the SQS message body.

    Visibility is derived from the presence of tenant_id/owner_sub per design S9.1:
      - owner_sub set -> "personal"
      - tenant_id set (no owner_sub) -> "tenant"
      - neither -> "shared"
    """
    if owner_sub:
        visibility = "personal"
    elif tenant_id:
        visibility = "tenant"
    else:
        visibility = "shared"

    return {
        "tenant_id": tenant_id,
        "owner_sub": owner_sub,
        "project_id": project_id,
        "visibility": visibility,
    }


# ---------------------------------------------------------------------------
# Core dispatch function
# ---------------------------------------------------------------------------


async def dispatch_ingestion(
    asset_id: str,
    asset_type: str,
    source_ref: str,
    tenant_id: str | None,
    owner_sub: str | None,
    project_id: str | None,
    db: AsyncSession,
    *,
    installation_id: int | None = None,
) -> bool:
    """Phase 1: inline SQS publish for a registered asset.

    Precondition: row MUST exist at status='registered' before calling.

    Args:
        asset_id: UUID of the knowledge_assets row.
        asset_type: Type key (must be in ASSET_TYPE_REGISTRY).
        source_ref: The source reference (URL, S3 path, etc.).
        tenant_id: Tenant isolation key (may be None).
        owner_sub: User isolation key (may be None).
        project_id: Project grouping key (may be None).
        db: Active database session (for status update on success).
        installation_id: GitHub App installation_id for per-pod auth (#2082).

    Returns:
        True if publish succeeded and status updated to 'queued'.
        False if signing, reservation, publication or status update fails.
        Publication errors retain the attempt; explicit reindex starts a new one.
    """
    # Look up type config
    type_config = ASSET_TYPE_REGISTRY.get(asset_type)
    if not type_config:
        logger.error(
            "dispatch_ingestion: unknown asset_type=%s for asset_id=%s",
            asset_type,
            asset_id,
        )
        return False

    try:
        await admit_source(asset_type, source_ref, tenant_id, owner_sub)
    except SourceAdmissionError:
        return False

    # Build the SQS message
    message: dict[str, Any] = {
        "source": extract_source_identifier(source_ref, asset_type),
        "content_type": asset_type,
        "registry_asset_id": asset_id,
        "scope": build_scope_envelope(tenant_id, owner_sub, project_id),
        "steps": type_config["steps"],
        "triggered_by": "self_serve",
        "enqueued_at": datetime.now(UTC).isoformat(),
    }

    # Include installation_id so the ingestion pod can mint a per-repo token (#2082).
    if installation_id is not None:
        message["installation_id"] = installation_id

    queue_url = get_queue_url()

    # Persist the exact generation and grant digest before making the message
    # visible. A retry/reindex supersedes older grants; caller fields are checked
    # against the already-authorized row before any publication.
    attempt_id = str(uuid.uuid4())
    try:
        grant = mint_ingestion_grant(asset_id=asset_id, tenant_id=tenant_id, attempt_id=attempt_id)
    except GrantError:
        logger.warning("Ingestion grant unavailable; source was not queued")
        return False
    binding = await db.execute(
        text("""
        UPDATE knowledge_assets
        SET ingestion_attempt_id = CAST(:attempt_id AS uuid), callback_grant_sha256 = :digest
        WHERE id = :id AND status = 'registered'
          AND ingestion_attempt_id IS NULL AND callback_grant_sha256 IS NULL
          AND tenant_id IS NOT DISTINCT FROM :tenant_id AND source_ref = :source_ref
        RETURNING id
    """),
        {
            "id": asset_id,
            "attempt_id": attempt_id,
            "digest": hashlib.sha256(grant.encode()).hexdigest(),
            "tenant_id": tenant_id,
            "source_ref": source_ref,
        },
    )
    if binding.fetchone() is None:
        return False
    await db.commit()
    message["callback_grant"] = grant

    # Publish to SQS
    try:
        client = get_sqs_client()
        client.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(message),
            MessageAttributes={
                "content_type": {
                    "DataType": "String",
                    "StringValue": asset_type,
                },
            },
        )
        logger.info(
            "dispatch_ingestion: published asset_id=%s type=%s to SQS",
            asset_id,
            asset_type,
        )
    except IngestionQueueUnavailableError:
        # Let queue-unavailable propagate — caller surfaces as 503.
        raise
    except Exception:
        logger.exception(
            "dispatch_ingestion: SQS publication uncertain for asset_id=%s; attempt retained, explicit reindex required",
            asset_id,
        )
        return False

    # Update status to 'queued' only after successful publish
    try:
        await db.execute(
            text("""
                UPDATE knowledge_assets
                SET status = 'queued', updated_at = NOW()
                WHERE id = :id AND status = 'registered'
                  AND ingestion_attempt_id = CAST(:attempt_id AS uuid)
            """),
            {"id": asset_id, "attempt_id": attempt_id},
        )
        await db.commit()
    except Exception:
        logger.exception(
            "dispatch_ingestion: status update to 'queued' failed for asset_id=%s — message is published but row stays at 'registered'",
            asset_id,
        )
        # Message is already published; status mismatch is recoverable
        # (sweeper or worker callback will reconcile)
        return False

    return True
