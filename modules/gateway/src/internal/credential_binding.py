"""Credential-authorization binding — registry-based user resolution.

Issue #3175: S2 of credential-authorization binding. Resolves the
`authorized_user_id` from the webhook-events DDB table (written by S1)
and enforces or shadows based on the ENFORCE_CREDENTIAL_BINDING flag.

Flow:
  1. Client sends `invocation_id` (= event_id PK in webhook-events).
  2. This module does a DDB Query on event_id to retrieve `authorized_user_id`.
     (The table has a composite key: event_id HASH + arrived_at RANGE —
     Issue #3376 fixed this from GetItem which required both keys.)
  3. If flag is ENFORCE: missing invocation_id or empty authorized_user_id -> 403.
  4. If flag is SHADOW (default): resolve from registry when present, compare
     to body user_id, emit drift/fallback metrics, never block.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import HTTPException

from src.shared.config import Settings

from .credential_binding_metrics import observe_binding

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
        observe_binding(from_registry=False, drift=False)
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
        observe_binding(from_registry=False, drift=False)
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
    observe_binding(from_registry=True, drift=drift_detected)
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

    Uses a Query on event_id (partition key). The table has a composite
    primary key (event_id HASH + arrived_at RANGE), so GetItem would
    require both keys. Query by PK returns all items for that event_id;
    we pick the one with the latest arrived_at (handles re-delivery).

    Returns the authorized_user_id string, or "" if not found / error.
    """
    try:
        table = _get_dynamodb_table(table_name, aws_region)
        response = table.query(
            KeyConditionExpression=Key("event_id").eq(invocation_id),
            ProjectionExpression="authorized_user_id, arrived_at",
            ScanIndexForward=False,  # descending arrived_at → latest first
            Limit=1,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        logger.warning(
            "credential_binding: DDB Query failed — invocation_id=%s error_code=%s table=%s",
            invocation_id,
            error_code,
            table_name,
        )
        # Fail-soft: DDB errors don't block in either mode.
        # In enforce mode, an empty result will trigger the 403 above.
        return ""

    items = response.get("Items", [])
    if not items:
        logger.info(
            "credential_binding: no registry row for invocation_id=%s",
            invocation_id,
        )
        return ""

    return items[0].get("authorized_user_id", "")


# ---------------------------------------------------------------------------
# Installation binding (issue #4272)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallationBinding:
    """The tenant + installation a run is provably bound to.

    Resolved from the run's webhook-events row, never from the request body.
    """

    tenant_id: str
    installation_id: int


def resolve_installation_binding(
    *,
    invocation_id: str | None,
    requested_installation_id: int,
    settings: Settings,
) -> InstallationBinding:
    """Bind a run to the installation its originating webhook actually carried.

    Issue #4272. The GitHub-token gatekeeper mints an org-scoped credential
    server-side, so it must never accept the caller's word for *which* org. This
    resolves the authoritative pair from the webhook-events row written at
    ingress and rejects anything that disagrees.

    Deliberately distinct from :func:`resolve_credential_binding`, which resolves
    a *user* (``authorized_user_id``) and has no notion of an installation or a
    tenant — it cannot perform this check.

    Two properties this must have, both learned the hard way:

    * **Fail-closed on absence.** ``installation_id`` is written conditionally
      into the row (``webhook_events.write_event``: ``if installation_id``), so a
      row legitimately may not carry one. That is a *reject*, not a pass: an
      unbound mint request is exactly the confused-deputy primitive this guard
      exists to deny.
    * **Independent of ``ENFORCE_CREDENTIAL_BINDING``.** That flag is ``false``
      on at least one live environment, so a control gated on it silently
      shadows instead of enforcing. This function never reads it. Its caller must
      not gate it either.

    A DDB error is also a reject. ``resolve_credential_binding`` is fail-soft
    there by design (a lookup failure must not break credential reads); the
    opposite is correct here, because failing soft would hand out an
    unverifiable org-scoped GitHub token.

    Args:
        invocation_id: The run's invocation id (= ``event_id`` PK in
            webhook-events). Typically ``ADP_MESSAGE_ID`` in the worker.
        requested_installation_id: The installation the caller wants a token for.
        settings: Application settings (table name + region).

    Returns:
        The bound ``InstallationBinding``. ``tenant_id`` comes from the row and
        is what callers must use for the ownership check — never a body value.

    Raises:
        HTTPException(403): missing invocation_id, no row, row missing
            ``installation_id`` or ``tenant_id``, lookup failure, or a mismatch
            between the row and the request.
    """
    if not invocation_id:
        logger.warning(
            "installation_binding: REJECTED — missing invocation_id (requested_installation_id=%s)",
            requested_installation_id,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "installation_binding_failed",
                "message": "invocation_id is required to mint an installation token.",
            },
        )

    row = _lookup_installation_row(
        invocation_id=invocation_id,
        table_name=settings.webhook_events_table,
        aws_region=settings.aws_region,
    )
    if row is None:
        logger.warning(
            "installation_binding: REJECTED — no registry row for invocation_id=%s",
            invocation_id,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "installation_binding_failed",
                "message": "No authorization record for this invocation. Token minting denied.",
            },
        )

    bound_raw = row.get("installation_id")
    tenant_id = str(row.get("tenant_id") or "")

    if not bound_raw:
        # Fail-closed on absence. See the docstring: the attribute is optional in
        # the table, so this is a reachable state and must not be a pass.
        logger.warning(
            "installation_binding: REJECTED — row carries no installation_id invocation_id=%s",
            invocation_id,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "installation_binding_failed",
                "message": "This invocation is not bound to a GitHub App installation.",
            },
        )

    if not tenant_id:
        logger.warning(
            "installation_binding: REJECTED — row carries no tenant_id invocation_id=%s",
            invocation_id,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "installation_binding_failed",
                "message": "This invocation is not bound to a tenant.",
            },
        )

    try:
        bound_installation_id = int(bound_raw)
    except (TypeError, ValueError):
        logger.warning(
            "installation_binding: REJECTED — unparseable installation_id=%r invocation_id=%s",
            bound_raw,
            invocation_id,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "installation_binding_failed",
                "message": "This invocation's bound installation could not be read.",
            },
        ) from None

    if bound_installation_id != requested_installation_id:
        logger.warning(
            "installation_binding: REJECTED — mismatch invocation_id=%s bound=%s requested=%s tenant_id=%s",
            invocation_id,
            bound_installation_id,
            requested_installation_id,
            tenant_id,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "installation_binding_mismatch",
                "message": "Requested installation does not match this invocation's installation.",
            },
        )

    return InstallationBinding(tenant_id=tenant_id, installation_id=bound_installation_id)


def _lookup_installation_row(
    *,
    invocation_id: str,
    table_name: str,
    aws_region: str,
) -> dict | None:
    """Fetch the latest webhook-events row for ``invocation_id``.

    Same Query shape as :func:`_lookup_authorized_user` (composite key
    ``event_id`` HASH + ``arrived_at`` RANGE, newest first), projecting the
    attributes the installation binding needs.

    Returns the row, or ``None`` when it is absent OR the lookup failed. The
    caller treats both as a reject — unlike the user-binding lookup, this one
    must not fail soft.
    """
    try:
        table = _get_dynamodb_table(table_name, aws_region)
        response = table.query(
            KeyConditionExpression=Key("event_id").eq(invocation_id),
            ProjectionExpression="installation_id, tenant_id, arrived_at",
            ScanIndexForward=False,  # descending arrived_at -> latest first
            Limit=1,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        logger.warning(
            "installation_binding: DDB Query failed — invocation_id=%s error_code=%s table=%s (failing closed)",
            invocation_id,
            error_code,
            table_name,
        )
        return None

    items = response.get("Items", [])
    if not items:
        return None
    return items[0]
