"""Consume credential authority established by authenticated broker middleware.

Request selectors assert the exact authenticated run's origin; flags never allow
body-user fallback. Legacy lookup helpers cannot authorize request routes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError
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
    invocation_id: str | None = None
    tenant_id: str | None = None


def _get_dynamodb_table(table_name: str, aws_region: str):
    """Get a DynamoDB Table resource. Separated for testability."""
    dynamodb = boto3.resource("dynamodb", region_name=aws_region)
    return dynamodb.Table(table_name)


def resolve_credential_binding(
    *,
    invocation_id: str | None,
    body_user_id: str,
    settings: Settings,
    verified_binding: BindingResult | None = None,
) -> BindingResult:
    """Consume authenticated broker state; body values are assertions only.

    Neither a body-selected event query nor the former shadow-mode fallback
    establishes ownership. The broker reads the exact current run's origin row.
    """
    if (
        verified_binding is None
        or not verified_binding.from_registry
        or not verified_binding.tenant_id
        or not verified_binding.resolved_user_id
        or not invocation_id
        or verified_binding.invocation_id != invocation_id
    ):
        observe_binding(from_registry=False, drift=False)
        raise HTTPException(403, {"error": "credential_binding_failed", "message": "Authenticated run credential binding for invocation_id required"})
    if verified_binding.resolved_user_id != body_user_id or verified_binding.body_user_id != body_user_id or verified_binding.drift_detected:
        observe_binding(from_registry=True, drift=True)
        raise HTTPException(403, {"error": "credential_authorization_drift", "message": "User assertion differs from authenticated run"})

    observe_binding(from_registry=True, drift=False)
    return verified_binding


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
    """The tenant + installation + repository a run is provably bound to.

    Resolved from the run's webhook-events row, never from the request body.

    Issue #5663 (A09): ``repo`` carries the repository recorded on the run's
    originating event, so the token route can refuse a mint for a repository the
    run was never assigned. It is ``None`` when the originating row carries no
    ``repo`` attribute at all — the EventBridge/scheduled-dispatch case, where
    ``target.repo`` is optional (``eventbridge/handler.py``: ``repo =
    target.get("repo", "")``) and ``log_event`` writes the attribute only ``if
    repo``. Those runs are unbound rather than bound-to-nothing, and the route
    decides what to do with that; see ``repo_binding_state``.
    """

    tenant_id: str
    installation_id: int
    repo: str | None = None


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

    This legacy lookup does not authenticate a run. Production routes consume
    middleware state established from the authenticated execution and exact event.

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

    # Issue #5663 (A09): carry the row's repository forward. Normalised to None
    # when the attribute is absent or blank so the caller can distinguish "this run
    # is bound to repo X" from "this run's originating event recorded no repo",
    # which are different authorization situations.
    bound_repo = str(row.get("repo") or "").strip() or None

    return InstallationBinding(
        tenant_id=tenant_id,
        installation_id=bound_installation_id,
        repo=bound_repo,
    )


@dataclass(frozen=True)
class ChainBinding:
    """The tenant and human root a correlation chain was provably started with.

    Issue #5663 (A09). Resolved from the ``correlation-index`` GSI on
    ``webhook-events`` — rows written by webhook-ingress at dispatch time — so a
    caller that names a chain cannot also decide what that chain's tenant or
    originating human is.

    ``root_human_id`` is the human recorded on the chain's EARLIEST row. The chain's
    origin is the only row whose human attribution is not itself derived from a
    later, possibly worker-influenced step, which is what makes it the anchor: a
    mid-chain row can be created by a run whose instructions an outsider shaped, so
    reading "the latest root_human_id" would let a chain re-root itself.

    ``is_human_rooted`` is ``None`` when the origin row carries no flag. Absent is
    NOT human — it resolves as service, mirroring ``run_binding.RunBinding`` and
    ``correlation_store.py``'s deliberate no-True-default. Treating absent as human
    would let a row that simply predates the lineage plane claim a person's
    authority.
    """

    correlation_id: str
    tenant_id: str
    root_human_id: str
    is_human_rooted: bool | None = None


def resolve_chain_binding(
    *,
    correlation_id: str,
    settings: Settings,
) -> ChainBinding | None:
    """Resolve a correlation chain's server-written origin, or ``None`` if unknown.

    Issue #5663 (A09). Used by the provenance endpoint to check a caller's asserted
    ``org_id`` / ``root_human_id`` / ``is_human_rooted`` against what webhook-ingress
    actually recorded when the chain began.

    Returns ``None`` — not an exception — for "no chain rows" and for a lookup fault,
    because the two are indistinguishable to this function and the caller must be
    free to choose its own policy for an unresolvable chain. The provenance route
    treats ``None`` as "cannot verify" and falls back to its existing membership
    check rather than denying, since a provenance write records an action that has
    ALREADY happened: refusing here destroys an audit row without preventing
    anything. See the route for that reasoning in full.

    IAM: the gateway role holds ungated ``dynamodb:Query`` on the table AND
    ``/index/*`` plus ``kms:Decrypt`` on its CMK, via the
    ``adp-<env>-policy-gateway-activity-read`` inline policy
    (``webhook-ingress/infra/iam.tf``, the ``WebhookEventsRead`` statement, which
    carries no ``count``/feature-flag gate). So this read needs no new grant and no
    IAM application — deliberately, since applying IAM is out of scope for #5663.
    The same policy is what ``activity/service.py`` already queries
    ``correlation-index`` with.
    """
    if not correlation_id:
        return None

    try:
        # Inside the try: constructing the resource can itself raise (e.g. a region
        # that cannot be resolved), and an exception escaping this function would
        # 500 a fail-soft audit endpoint.
        table = _get_dynamodb_table(settings.webhook_events_table, settings.aws_region)
        response = table.query(
            IndexName="correlation-index",
            KeyConditionExpression=Key("correlation_id").eq(correlation_id),
            ProjectionExpression="tenant_id, root_human_id, is_human_rooted, arrived_at",
            # Ascending arrived_at: the chain's ORIGIN row comes first. See the
            # dataclass docstring for why the origin, not the latest row, anchors
            # human attribution.
            ScanIndexForward=True,
            Limit=1,
        )
    except (ClientError, BotoCoreError) as exc:
        # BotoCoreError as well as ClientError, deliberately. A missing/expired
        # credential chain raises NoCredentialsError, and an unreachable endpoint
        # raises EndpointConnectionError — both BotoCoreError subclasses, neither a
        # ClientError. Catching only ClientError would turn an infrastructure
        # condition into an unhandled 500 on an endpoint whose whole contract is
        # fail-soft, which would take out provenance writes entirely rather than
        # degrading the new check.
        logger.warning(
            "chain_binding: DDB Query failed — correlation_id=%s error=%s table=%s",
            correlation_id,
            type(exc).__name__,
            settings.webhook_events_table,
        )
        return None

    items = response.get("Items", [])
    if not items:
        return None

    origin = items[0]
    tenant_id = str(origin.get("tenant_id") or "")
    if not tenant_id:
        # A chain with no tenant on its origin row carries no authority to compare
        # against, which is the same situation as no row at all.
        return None

    raw_flag = origin.get("is_human_rooted")
    if isinstance(raw_flag, bool):
        is_human_rooted: bool | None = raw_flag
    elif isinstance(raw_flag, str):
        is_human_rooted = raw_flag.strip().lower() == "true"
    else:
        is_human_rooted = None

    return ChainBinding(
        correlation_id=correlation_id,
        tenant_id=tenant_id,
        root_human_id=str(origin.get("root_human_id") or ""),
        is_human_rooted=is_human_rooted,
    )


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
            # Issue #5663 (A09): `repo` is projected so the token route can bind the
            # mint to the repository the run was actually assigned. One extra
            # projected attribute on a read this function already performs — no
            # additional round trip.
            ProjectionExpression="installation_id, tenant_id, repo, arrived_at",
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
