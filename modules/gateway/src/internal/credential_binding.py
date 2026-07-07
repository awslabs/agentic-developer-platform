"""Credential-authorization binding — registry-based user resolution.

Issue #3175: S2 of credential-authorization binding. Resolves the
`authorized_user_id` from the webhook-events DDB table (written by S1)
and enforces or shadows based on the ENFORCE_CREDENTIAL_BINDING flag.

Flow:
  1. Client sends `invocation_id` (= event_id PK in webhook-events).
  2. This module does a DDB GetItem to retrieve `authorized_user_id`.
  3. If flag is ENFORCE: missing invocation_id or empty authorized_user_id -> 403.
  4. If flag is SHADOW (default): resolve from registry when present, compare
     to body user_id, emit drift/fallback metrics, never block.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException

from src.shared.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BindingResult:
    """Result of credential-authorization binding resolution."""

    # The user_id to use for credential resolution.
    resolved_user_id: str
    # Whether the resolution came from the registry (True) or body fallback (False).
    from_registry: bool
    # Whether drift was detected (body user_id != registry user_id).
    drift_detected: bool
    # The body-supplied user_id (for audit purposes).
    body_user_id: str


def _get_dynamodb_table(table_name: str, aws_region: str):
    """Get a DynamoDB Table resource. Separated for testability."""
    dynamodb = boto3.resource("dynamodb", region_name=aws_region)
    return dynamodb.Table(table_name)


def resolve_credential_binding(
    *,
    invocation_id: str | None,
    body_user_id: str,
    settings: Settings,
) -> BindingResult:
    """Resolve credential authorization binding from the webhook-events registry.

    Parameters
    ----------
    invocation_id : str | None
        The invocation_id from the request body. Maps to `event_id` PK
        in the webhook-events DDB table.
    body_user_id : str
        The user_id from the request body (legacy path).
    settings : Settings
        Application settings (for flag + table name + region).

    Returns
    -------
    BindingResult
        Contains the resolved user_id and metadata about the resolution.

    Raises
    ------
    HTTPException(403)
        In enforce mode when invocation_id is missing or authorized_user_id
        is empty in the registry row.
    """
    enforce = settings.enforce_credential_binding

    # --- Case 1: No invocation_id provided ---
    if not invocation_id:
        if enforce:
            logger.warning(
                "credential_binding: REJECTED — missing invocation_id (enforce mode) body_user_id=%s",
                body_user_id,
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "credential_binding_failed",
                    "message": "invocation_id is required for credential access.",
                },
            )
        # Shadow mode: fallback to body, emit metric.
        logger.info(
            "credential_binding: fallback — no invocation_id, using body_user_id=%s",
            body_user_id,
        )
        return BindingResult(
            resolved_user_id=body_user_id,
            from_registry=False,
            drift_detected=False,
            body_user_id=body_user_id,
        )

    # --- Case 2: invocation_id provided — look up registry ---
    authorized_user_id = _lookup_authorized_user(
        invocation_id=invocation_id,
        table_name=settings.webhook_events_table,
        aws_region=settings.aws_region,
    )

    # --- Case 3: Registry row found but authorized_user_id is empty ---
    if not authorized_user_id:
        if enforce:
            logger.warning(
                "credential_binding: REJECTED — empty authorized_user_id invocation_id=%s body_user_id=%s (enforce mode)",
                invocation_id,
                body_user_id,
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "credential_binding_failed",
                    "message": "No authorized user for this invocation. Credential access denied.",
                },
            )
        # Shadow mode: fallback to body.
        logger.info(
            "credential_binding: fallback — empty authorized_user_id invocation_id=%s, using body_user_id=%s",
            invocation_id,
            body_user_id,
        )
        return BindingResult(
            resolved_user_id=body_user_id,
            from_registry=False,
            drift_detected=False,
            body_user_id=body_user_id,
        )

    # --- Case 4: Registry user resolved — check for drift ---
    drift_detected = authorized_user_id != body_user_id
    if drift_detected:
        logger.warning(
            "credential_binding: DRIFT detected — registry_user=%s body_user=%s invocation_id=%s enforce=%s",
            authorized_user_id,
            body_user_id,
            invocation_id,
            enforce,
        )
        if enforce:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "credential_authorization_drift",
                    "message": "Body user_id does not match authorized user from registry.",
                },
            )

    return BindingResult(
        resolved_user_id=authorized_user_id,
        from_registry=True,
        drift_detected=drift_detected,
        body_user_id=body_user_id,
    )


def _lookup_authorized_user(
    *,
    invocation_id: str,
    table_name: str,
    aws_region: str,
) -> str:
    """Look up authorized_user_id from webhook-events DDB table.

    Uses GetItem with event_id as the partition key.

    Returns the authorized_user_id string, or "" if not found / error.
    """
    try:
        table = _get_dynamodb_table(table_name, aws_region)
        response = table.get_item(
            Key={"event_id": invocation_id},
            ProjectionExpression="authorized_user_id",
            ConsistentRead=False,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        logger.warning(
            "credential_binding: DDB GetItem failed — invocation_id=%s error_code=%s table=%s",
            invocation_id,
            error_code,
            table_name,
        )
        # Fail-soft: DDB errors don't block in either mode.
        # In enforce mode, an empty result will trigger the 403 above.
        return ""

    item = response.get("Item")
    if item is None:
        logger.info(
            "credential_binding: no registry row for invocation_id=%s",
            invocation_id,
        )
        return ""

    return item.get("authorized_user_id", "")
