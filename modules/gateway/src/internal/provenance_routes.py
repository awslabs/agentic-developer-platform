"""Write provenance only for an authenticated run's exact server-owned origin.

IAM transport, run credentials and workload proof are mandatory. The endpoint
compares tenant, actor, correlation and human lineage to that origin before any
membership policy or persistence. Legacy shared-secret producers must migrate.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.credential_binding_metrics import observe_identity_binding
from src.shared.database import get_db
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.provenance import ActionProvenance

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/v1", tags=["internal-provenance"])

# ---------------------------------------------------------------------------
# Cross-tenant trigger policy vocabulary — MUST match the webhook-ingress
# resolver (issue #4029)
# ---------------------------------------------------------------------------
# These mirror identity_resolver.py's constants of the same names. They are
# duplicated rather than imported because package-lambdas.sh roots the Lambda zip
# at lambda/, so no gateway module is importable at Lambda runtime.
#
# The drift guard is tests/internal/test_provenance_policy_lockstep.py, which
# imports both sides and asserts these three names are identical. Change either
# side and that test fails until the other follows — which is the whole point:
# a mismatched DEFAULT is what made every cross-tenant provenance write fail.
TRIGGER_POLICY_ANY_ADP_USER = "any_adp_user"
TRIGGER_POLICY_HOME_TENANT_ONLY = "home_tenant_only"
DEFAULT_TRIGGER_POLICY = TRIGGER_POLICY_ANY_ADP_USER

# Issue #5663 (A09): route label for the identity-binding counters.
_PROVENANCE_ROUTE = "provenance"


async def _org_permits_any_adp_user(db: AsyncSession, org_id: str) -> bool:
    """Whether ``org_id``'s trigger policy lets any known ADP user act in it.

    Issue #4029: mirrors the webhook-ingress resolver's policy read
    (``identity_resolver.py``) **including its default** — an absent
    ``trigger_policy`` means ``any_adp_user``, exactly as the Lambda treats it.

    Why the default must match rather than be stricter: the ingress resolver is what
    decides whether a cross-tenant run may *execute*. By the time this endpoint is
    reached the run already happened; all this write does is *record* it. A stricter
    default here does not prevent the action — it only drops the audit row, and drops
    it precisely for cross-tenant activity, the case most worth auditing. An earlier
    revision of this fix required an *explicit* policy value and would have converted
    the #4029 silent 422 into a silent 403 for every org that never configured the
    setting (``Organization.settings`` defaults to ``{}``, i.e. nearly all of them).

    What still fails closed: an explicit ``home_tenant_only``, and an unknown org (no
    policy can authorize attribution to an org that does not exist).

    The value is read from the gateway's own Postgres (``organizations.settings``,
    the source the Lambda's DDB rows are synced from by ``admin/service.py``), never
    from anything the caller supplied.

    The two modules cannot import a shared constant (the Lambda zip is rooted at
    ``lambda/``), so ``tests/internal/test_provenance_policy_lockstep.py`` imports
    both sides and fails if this default and the resolver's ever diverge again.
    Reconciling the broader policy story is tracked in #4048.
    """
    settings = (await db.execute(select(Organization.settings).where(Organization.id == org_id))).scalar_one_or_none()
    if settings is None:
        # Unknown org — no policy can authorize attribution to it.
        return False
    return (settings or {}).get("trigger_policy", DEFAULT_TRIGGER_POLICY) == TRIGGER_POLICY_ANY_ADP_USER


def _emit_provenance_authority_metric(metric_name: str, org_id: str) -> None:
    """Count cross-tenant provenance authority decisions. Never raises.

    Issue #4029 (amendment 6), gateway half. Two metrics matter here:

    ``CrossTenantProvenanceAllowed`` — a row attributed to an org the actor has no
    Postgres relationship with, authorized by that org's ``trigger_policy``. This is
    the flow the ingress resolver permits, so it is expected rather than alarming,
    but it is the signal to watch if cross-tenant attribution ever looks wrong.

    ``ProvenanceAuthorityDenied`` — the residual 403 (explicit ``home_tenant_only``
    or an unknown org). Every caller of this endpoint is fail-soft, so without this
    metric a denial is invisible on both sides: the exact failure mode that let #4029
    hide for months. If this climbs, orgs are losing provenance rows and someone can
    now see it.
    """
    try:
        import boto3

        boto3.client("cloudwatch").put_metric_data(
            Namespace="ADP/Provenance",
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Dimensions": [{"Name": "OrgId", "Value": org_id}],
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never fail the write
        logger.debug("Failed to emit %s metric: %s", metric_name, exc)


async def _actor_is_member_of(db: AsyncSession, actor_user_id: str, org_id: str) -> bool:
    """Whether ``actor_user_id`` may have provenance attributed to ``org_id``.

    Issue #3985 (A2): the caller asserts ``body.org_id``, so FK-existence of the
    actor is not enough — without this compare any internal-plane caller can
    attribute an action to an org the actor has nothing to do with, poisoning
    another tenant's audit trail.

    ``tenant_memberships`` (migration 021) is the authority for org membership.
    Note that ``TenantMembership.user_id`` FKs ``users.id``, which is exactly what
    ``actor_user_id`` is, so this needs no ``cognito_sub`` indirection (unlike
    ``admin/access_control.py``, which starts from a token's Cognito sub).

    Fallback: ``POST /resolve-user`` auto-provisions shadow users with
    ``users.org_id`` set but no membership row (only three code paths create
    memberships, and migration 021 backfilled pre-existing users once). A
    strict membership-only check would therefore 403 legitimate provenance for
    shadow-user actors, so when an actor has no membership rows at all we fall
    back to their ``users.org_id``. The compare is still enforced either way —
    the fallback narrows the authority source, it does not skip the check.

    Issue #4029 — cross-tenant clause. Membership alone is too narrow to be the
    whole gate. ``ResolvedIdentity`` sets a run's tenant to the *repo/installation*
    org, and under the ingress default ``trigger_policy`` the resolver deliberately
    permits a user whose home org is A to trigger on a repo in org B. Every row for
    such a run (DDB invocation events, Activity) keys on B, so provenance must too —
    but the actor's memberships and ``users.org_id`` both say A, so a membership-only
    check 403s exactly the flow the platform is designed to allow. Because every
    caller of this endpoint is fail-soft, that 403 is as invisible as the #4029 422
    was: fixing only the payload would have swapped one silent failure for another.

    So when membership does not authorize, we consult the *target org's own* trigger
    policy, read with the same default as the ingress resolver
    (``_org_permits_any_adp_user``). This endpoint only records a run the resolver
    already permitted to execute, so a stricter default here would not block
    anything — it would just lose the audit row for cross-tenant activity.

    What still fails closed, preserving #3985: an explicit ``home_tenant_only``, and
    an unknown ``org_id``. Note the deliberate scope of the residual gate — for an
    org on the default policy, this no longer constrains which ``org_id`` a caller
    may attribute an action to. That authority rests on the internal-plane
    credential, whose compromise is already declared as inherited risk in this
    module's header. #4048 tracks reconciling the policy story.

    Either outcome is measured, so neither can hide.
    """
    rows = (await db.execute(select(TenantMembership.tenant_id).where(TenantMembership.user_id == actor_user_id))).scalars().all()

    if rows:
        if org_id in set(rows):
            return True
    else:
        actor_org_id = (await db.execute(select(User.org_id).where(User.id == actor_user_id))).scalar_one_or_none()
        if actor_org_id is None:
            return False
        if actor_org_id == org_id:
            logger.info(
                "provenance_membership_fallback actor=%s org=%s reason=no_membership_rows",
                actor_user_id,
                org_id,
            )
            return True

    # Not authorized by the actor's own org record — fall back to the target org's
    # trigger policy (the cross-tenant case the resolver permits), same default.
    if await _org_permits_any_adp_user(db, org_id):
        logger.info(
            "provenance_cross_tenant_allowed actor=%s org=%s reason=trigger_policy_any_adp_user",
            actor_user_id,
            org_id,
        )
        _emit_provenance_authority_metric("CrossTenantProvenanceAllowed", org_id)
        return True

    _emit_provenance_authority_metric("ProvenanceAuthorityDenied", org_id)
    return False


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateProvenanceRequest(BaseModel):
    """Body for POST /internal/v1/provenance."""

    actor_user_id: str
    triggered_by: str | None = None
    root_human_id: str
    is_human_rooted: bool
    action_kind: str
    source_event: dict
    correlation_id: str
    org_id: str
    parent_invocation_id: str | None = None


class CreateProvenanceResponse(BaseModel):
    id: str
    created_at: str


# ---------------------------------------------------------------------------
# Endpoint: POST /internal/v1/provenance
# ---------------------------------------------------------------------------


@router.post(
    "/provenance",
    response_model=CreateProvenanceResponse,
    status_code=201,
    summary="Record an action provenance row (Lambda/internal call)",
    description=("Called by the webhook-ingress Lambda to record provenance for agent/human actions. Validates FK references to users table."),
)
async def create_provenance(
    body: CreateProvenanceRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_internal_or_irsa),
) -> CreateProvenanceResponse:
    from src.internal.run_identity import verified_run_identity

    identity = await verified_run_identity(request)
    if (
        body.org_id != identity.tenant_id
        or body.actor_user_id != identity.actor_user_id
        or body.correlation_id != identity.correlation_id
        or body.root_human_id != identity.root_human_id
        or body.is_human_rooted != identity.is_human_rooted
        or body.parent_invocation_id != identity.parent_invocation_id
        or body.triggered_by != identity.triggered_by
    ):
        observe_identity_binding(route=_PROVENANCE_ROUTE, outcome="denied", enforced=True)
        raise HTTPException(403, "provenance does not match authenticated run")

    # Validate FK: actor_user_id must exist
    actor = await db.execute(select(User.id).where(User.id == body.actor_user_id))
    if actor.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_actor", "message": f"actor_user_id '{body.actor_user_id}' not found"},
        )

    # Issue #3985 (A2): the actor must actually belong to the org the caller is
    # attributing this action to. Checked before any further validation so a
    # cross-tenant attempt cannot probe which user IDs exist in other orgs.
    if not await _actor_is_member_of(db, body.actor_user_id, body.org_id):
        logger.warning(
            "Rejecting provenance write: actor=%s is not a member of org=%s",
            body.actor_user_id,
            body.org_id,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "actor_org_mismatch",
                "message": "actor_user_id is not a member of the specified org_id",
            },
        )

    # Validate FK: triggered_by must exist if provided
    if body.triggered_by is not None:
        triggered = await db.execute(select(User.id).where(User.id == body.triggered_by))
        if triggered.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_triggered_by", "message": f"triggered_by '{body.triggered_by}' not found"},
            )

    # Validate FK: root_human_id must exist
    root = await db.execute(select(User.id).where(User.id == body.root_human_id))
    if root.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_root_human", "message": f"root_human_id '{body.root_human_id}' not found"},
        )

    # Insert provenance row
    now = utcnow()
    row_id = new_uuid()

    # Guard against self-referential parent (prevents cycles in lineage tree)
    parent_inv_id = body.parent_invocation_id
    if parent_inv_id and parent_inv_id == row_id:
        raise HTTPException(
            status_code=400,
            detail={"error": "self_referential_parent", "message": "parent_invocation_id must not equal the row's own id"},
        )

    row = ActionProvenance(
        id=row_id,
        org_id=body.org_id,
        actor_user_id=body.actor_user_id,
        triggered_by=body.triggered_by,
        root_human_id=body.root_human_id,
        is_human_rooted=body.is_human_rooted,
        action_kind=body.action_kind,
        source_event=body.source_event,
        correlation_id=body.correlation_id,
        parent_invocation_id=parent_inv_id,
        created_at=now,
    )
    db.add(row)
    await db.commit()

    logger.info(
        "Provenance recorded id=%s actor=%s correlation=%s kind=%s",
        row.id,
        body.actor_user_id,
        body.correlation_id,
        body.action_kind,
    )

    return CreateProvenanceResponse(
        id=row.id,
        created_at=now.isoformat(),
    )
