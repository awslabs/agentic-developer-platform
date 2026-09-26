"""Bedrock routing authoring API — Issue #4745 (#4692 · R4).

Design note ``docs/design-notes/4692-per-principal-bedrock-account-routing.md``
§1.4, §4.2, §4.3, §6.3, §6.6, §6.7.

R2 (#4743) shipped the tables and read the ladder in shadow mode; R3 (#4744) made the
routing real by signing with the destination account. Neither shipped a way to author a
mapping — the rows had to be written by hand. This router is that surface.

## Every route is platform-admin-only, and that is ruling 4, not caution

``AccessControl.require_platform_admin`` gates every handler. Ruling 4 removed the
org-admin rung *"for now (may be delegated later)"*, and §6.5 makes it a deliberate
non-goal: **no endpoint may accept an org-admin caller.**

The reason is the same authority inversion #4620 ruled on for person budgets, one
question over: a mapping decides **whose AWS account is billed** for a principal's
model calls. A user-rung mapping names a person who may work in several tenants, and
an org admin authoring one would be reaching into tenants they have no membership in.
So the gate is a claim about the *caller* (``require_platform_admin``), not about a
partition — ``check_permission(..., target_org_id=...)`` would be the wrong gate rather
than a looser one, since an org admin holds their own org's permissions and passing
their own org id would make it trivially true.

Note what ``is_admin`` deliberately excludes: ``org_admin`` is not in the
platform-admin predicate (``auth/dependencies.py``), which is what makes the 403 real
rather than incidental. Dev has no org_admin identity to prove it with, so
``tests/admin/bedrock_routing/`` proves it with a synthetic org_admin context — the only
place that assertion can actually be made.

**Authority is the first statement in every handler**, before the scope string is
parsed. A non-admin must not be able to use the 422-vs-403 difference to learn which
orgs, teams, users or destinations exist on this platform — the same ordering rule
``person_cap_routes`` documents, and it is asserted against this module's source.

## Two things a reader should not have to discover by experiment

**Saving runs a real assume-role, and a rule that fails it is refused, not stored**
(ruling 4a, §6.7). ``PUT`` returns 422 with a shared reason code and writes nothing.
This is the #4511 gate: a mapping stored inert reads as configured routing and fails
100% of the principal's calls under fail-closed (§2.5). A failing probe is the
mechanism working — there is no override parameter, deliberately.

**A write takes effect within the resolver's existence-cache TTL, not instantly.**
On an install whose mapping table was empty, the resolver answers "no mappings exist"
from a 60s process-local cache (§2.2) — the decision that keeps day-one installs at
zero queries per model call. This module clears that flag on the pod that took the
write; other pods wait out their own TTL. Bounded, documented, and the reason a saved
rule can look like it did nothing for up to a minute.

## What this router does NOT own

* **The self-service selector** (§6.4) — a user picking their own destination on the
  credentials page. Different authz (the caller IS the target), different storage path.
* **Deleting a destination.** Under fail-closed, removing a destination a mapping still
  references is an outage (§8.3), not a cleanup — it needs its own confirmation design.
  ``used_by`` is reported here so the panel can show what would break, and
  ``invalidate_signer_cache`` is wired for the re-verify path that does exist.
* **Any fallback to the platform account.** Ruling 1 forbids one; this surface cannot
  author one, because rung 4 is the absence of a mapping and there is no platform scope.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.auth.cfn_template import ROUTING_TEMPLATE_VERSION, build_launch_url, compute_role_arn, read_routing_template
from src.auth.dependencies import get_current_user
from src.auth.routing_setup import routing_setup_download
from src.auth.vault_routes import get_secrets_manager
from src.shared.database import get_db
from src.shared.identity import resolve_canonical_user_id
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.bedrock_routing import BedrockAccountMapping, BedrockConnectionGrant, BedrockDestinationRegistry
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserCredential
from src.shared.schemas.auth import TokenContext
from src.shared.services.secrets_manager import SecretsManagerHelper

from . import revisions, service
from .schemas import (
    DestinationSetupResponse,
    DestinationSummary,
    EffectiveMappingResponse,
    ExistingAwsConnection,
    MappingPage,
    MappingSummary,
    MappingUpsertRequest,
    RegisterConnectionDestination,
    RegisterDestinationResponse,
    RegisterNewDestination,
    RegisterSharedConnectionDestination,
    VerifyDestinationResponse,
)

logger = logging.getLogger("bedrockgateway.admin.bedrock_routing")

# No `/api` prefix: CloudFront strips the first `/api` segment before the origin, so a
# router mounting under `/api/...` is unreachable through the dashboard (#4330, guarded
# by tests/test_route_prefix_convention.py). The browser calls
# `/api/admin/bedrock-routing/mappings`; this router serves
# `/admin/bedrock-routing/mappings`.
router = APIRouter(prefix="/admin/bedrock-routing", tags=["bedrock-routing"])


def _rejected(exc: service.MappingRejectedError) -> HTTPException:
    """Map a save-time refusal to a 422 carrying the shared reason code.

    422, not 400: the request was well-formed and the *state* it names is
    unacceptable. The ``reason`` travels in the detail so the panel can branch on a
    stable code rather than parse prose, and so it matches what a runtime 502 would
    say for the same condition (§6.7 item 2).
    """
    return HTTPException(status_code=422, detail={"reason": exc.reason, "message": exc.message})


def _compose_destination(
    destination: BedrockDestinationRegistry, used_by: int, reason: str | None = None, *, connection_id: str | None = None
) -> DestinationSummary:
    """Render a registry row for the destinations table. No ``role_arn`` — §2.6."""
    return DestinationSummary(
        id=destination.id,
        revision=revisions.destination_revision(destination),
        connection_id=connection_id,
        source_connection_id=destination.credential_id,
        account_id=destination.account_id,
        label=destination.label,
        region=destination.region,
        source="admin-registered" if destination.is_platform_registered else "org-linked",
        owner_org_id=destination.owner_org_id,
        routing_capable=destination.routing_capable,
        verified_at=destination.verified_at,
        usable_for_routing=destination.is_usable_for_routing,
        reason=reason,
        used_by=used_by,
    )


def _compose_mapping(mapping: BedrockAccountMapping, destination: BedrockDestinationRegistry) -> MappingSummary:
    """Render a mapping row for the rules table."""
    return MappingSummary(
        id=mapping.id,
        revision=revisions.mapping_revision(mapping),
        scope_type=mapping.scope_type,  # type: ignore[arg-type]
        scope_id_org=mapping.scope_id_org,
        scope_id_team=mapping.scope_id_team,
        scope_id_user=mapping.scope_id_user,
        scope=service.format_scope(mapping),
        destination_id=destination.id,
        destination_account_id=destination.account_id,
        destination_label=destination.label,
        destination_usable=destination.is_usable_for_routing,
        source=service.mapping_source(mapping),  # type: ignore[arg-type]
        updated_at=mapping.updated_at,
    )


async def _scope_org_id(db: AsyncSession, scope_type: str, org_id: str | None, user_id: str | None) -> str:
    """The tenant a scope belongs to, for the §4.2 ownership check.

    For the org and team rungs it is the scope's own org. For the **user** rung the
    mapping row carries no org, so it is the target user's own ``users.org_id`` — read
    from the database, never from the caller's token. The caller is a platform admin
    whose token names *their* tenant; comparing a destination against that would
    compare the wrong two values and let every cross-tenant mapping through.
    """
    if scope_type != "user":
        return org_id or ""
    target_org = await db.scalar(select(User.org_id).where(User.id == user_id))
    if not target_org:
        raise service.MappingRejectedError(
            "scope_not_found",
            f"No user with id '{user_id}' exists on this platform, so the rule would govern nobody.",
        )
    return target_org


# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------


@router.get("/mappings", response_model=list[MappingSummary] | MappingPage)
async def list_mappings(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    scope: Annotated[str | None, Query()] = None,
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[MappingSummary] | MappingPage:
    """Every routing rule on the platform, narrowest rung first. **Platform admin only.**

    Ordered ``user`` → ``team`` → ``org`` to match the ladder, so the table reads in
    precedence order and a reader does not have to reconstruct which row wins.

    The platform rung is deliberately absent from the result: it is the absence of a
    rule (§1.2), so the panel renders it as a static footer row rather than as data
    this endpoint could ever return.

    Raises:
        HTTPException: ``403`` for any caller who is not a platform admin.
    """
    AccessControl(db).require_platform_admin(current_user)

    query = select(BedrockAccountMapping, BedrockDestinationRegistry).join(
        BedrockDestinationRegistry,
        BedrockDestinationRegistry.id == BedrockAccountMapping.destination_id,
    )
    if scope is not None:
        try:
            kind, org, team, user = service.parse_scope(scope)
        except service.MappingRejectedError as exc:
            raise _rejected(exc) from exc
        query = query.where(
            BedrockAccountMapping.scope_type == kind,
            BedrockAccountMapping.scope_id_org == org,
            BedrockAccountMapping.scope_id_team == team,
            BedrockAccountMapping.scope_id_user == user,
        )
    if page is not None:
        query = query.order_by(BedrockAccountMapping.scope_type, BedrockAccountMapping.id).offset((page - 1) * page_size).limit(page_size + 1)
    rows = (await db.execute(query)).tuples().all()
    if page is not None:
        return MappingPage(
            items=[_compose_mapping(m, d) for m, d in rows[:page_size]], page=page, page_size=page_size, has_more=len(rows) > page_size
        )
    ordered = sorted(rows, key=lambda row: (service.RUNG_ORDER.index(row[0].scope_type), row[0].updated_at))
    return [_compose_mapping(mapping, destination) for mapping, destination in ordered]


@router.put("/mappings/{scope}", response_model=MappingSummary)
async def put_mapping(
    scope: str,
    request: MappingUpsertRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    secrets: Annotated[SecretsManagerHelper, Depends(get_secrets_manager)],
    expected_revision: Annotated[str | None, Query(pattern=r"^(absent|[a-f0-9]{64})$")] = None,
) -> MappingSummary:
    """Point one scope's Bedrock traffic at a destination. **Platform admin only.**

    **Every gate below refuses with a 422 and writes nothing** (ruling 4a — "reject,
    never store inert"):

    ===================================== =============================================
    Refusal                               Why it is a refusal and not a stored row
    ===================================== =============================================
    Scope does not exist                   A rule on a mistyped/foreign/deleted id
                                           reads as configured and governs nobody
                                           (#4511, the #4696 pattern).
    Destination not linked to the scope's   §4.2 req 1. This predicate IS cross-tenant
    org                                     isolation for admin-authored mappings.
    Personal credential for a team/org rung §4.3 ruling 6. One person's role carrying a
                                           team's traffic is an authority/audit
                                           problem, and IAM would refuse it anyway —
                                           but not as a *diagnosable* message.
    Test assume or Bedrock invoke fails    §6.7. A destination that cannot serve a call
                                           must not become a rule that fails 100% of
                                           them under fail-closed.
    ===================================== =============================================

    A successful probe also **stamps the destination verified**, because the probe just
    proved exactly what ``is_usable_for_routing`` claims. That is what makes a
    first-time registration usable without a separate verify call.

    Idempotent: re-pointing a scope replaces the destination in place, keeping the
    row's ``id`` and ``created_at`` (when the scope was first routed) while re-stamping
    ``authored_by_user_id`` — which is also what flips a user's self-selected row to
    admin-authored, the §1.4 "admin wins" transition.

    Args:
        scope: ``org:<org_id>``, ``team:<org_id>:<team_id>``, or ``user:<users.id>``.
            There is no ``platform`` scope — see the module docstring.

    Raises:
        HTTPException:
            ``403`` for any caller who is not a platform admin;
            ``422`` for a malformed scope or any failed gate — nothing is written in
            any of those cases, and the detail carries a stable ``reason``.
    """
    AccessControl(db).require_platform_admin(current_user)
    await revisions.serialize_writes(db)

    actor_id = await resolve_canonical_user_id(db, current_user.user_id)

    # Bound before the try, so the refusal audit below has a tenant to file under even
    # when it is `parse_scope` itself that raised and nothing was parsed.
    org_id: str | None = None
    try:
        scope_type, org_id, team_id, user_id = service.parse_scope(scope)
        current = await service.load_mapping_for_scope(db, scope_type, org_id, team_id, user_id)
        revisions.require_revision(expected_revision, revisions.mapping_revision(current))
        await service.require_scope_exists(db, scope_type, org_id, team_id, user_id)
        destination = await service.load_destination(db, request.destination_id)
        revisions.require_revision(request.expected_destination_revision, revisions.destination_revision(destination))
        scope_org = await _scope_org_id(db, scope_type, org_id, user_id)
        await service.validate_mapping_target(
            db,
            destination=destination,
            scope_type=scope_type,
            scope_org_id=scope_org,
            secrets=secrets,
            # The probe assumes without session tags, so this id is CloudTrail
            # attribution in the destination account, not an authorization input. The
            # admin who authored the rule is the right party to attribute it to.
            probe_user_id=actor_id,
        )
    except service.MappingRejectedError as exc:
        # Audited on its own transaction: a refusal rolls back, and §4.2 requires
        # "every save-time assume failure" to leave a record anyway.
        await service.write_refusal_audit(
            db,
            event_type="bedrock_routing_mapping_rejected",
            org_id=org_id or service.PLATFORM_AUDIT_ORG,
            actor_id=actor_id,
            details={"scope": scope, "destination_id": request.destination_id, "reason": exc.reason},
        )
        raise _rejected(exc) from exc

    mapping = await service.load_mapping_for_scope(db, scope_type, org_id, team_id, user_id)
    if mapping is None:
        mapping = BedrockAccountMapping(
            id=new_uuid(),
            scope_type=scope_type,
            scope_id_org=org_id,
            scope_id_team=team_id,
            scope_id_user=user_id,
            destination_id=destination.id,
            authored_by_user_id=actor_id,
        )
        db.add(mapping)
    else:
        mapping.destination_id = destination.id
        mapping.authored_by_user_id = actor_id

    await service.write_audit(
        db,
        event_type="bedrock_routing_mapping_authored",
        org_id=org_id or service.PLATFORM_AUDIT_ORG,
        actor_id=actor_id,
        details={
            "scope": scope,
            "destination_id": destination.id,
            "destination_account_id": destination.account_id,
            # Server-side only. This is the §2.6 split: the ARN is what an incident
            # responder needs and is never in a response or an error.
            "destination_role_arn": destination.role_arn,
        },
    )
    await db.commit()
    await db.refresh(mapping)
    await db.refresh(destination)

    # So this pod stops answering "no mappings exist" from its 60s cache. See the
    # module docstring: without it, the first rule on an empty install appears to do
    # nothing for up to a minute.
    service.invalidate_resolver_existence_cache()

    logger.info("bedrock_routing_mapping_authored scope_type=%s", scope_type)
    return _compose_mapping(mapping, destination)


@router.delete("/mappings/{scope}", status_code=204)
async def delete_mapping(
    scope: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    expected_revision: Annotated[str | None, Query(pattern=r"^(absent|[a-f0-9]{64})$")] = None,
) -> None:
    """Remove one scope's routing rule. **Platform admin only.**

    Removing a rule does **not** make the scope unroutable: it falls through to the
    next rung, and to the platform account if none matches. That fall-through is why
    deleting a *mapping* is safe while deleting a *destination* is an outage (§8.3) —
    the ladder has somewhere to go.

    **204 whether or not a rule existed.** The outcome the caller asked for ("no rule
    for this scope") holds either way, and a 404 on the already-absent case would make
    a retried delete look like a failure.

    Raises:
        HTTPException:
            ``403`` for any caller who is not a platform admin;
            ``422`` for a malformed scope.
    """
    AccessControl(db).require_platform_admin(current_user)
    await revisions.serialize_writes(db)

    try:
        scope_type, org_id, team_id, user_id = service.parse_scope(scope)
        current = await service.load_mapping_for_scope(db, scope_type, org_id, team_id, user_id)
        revisions.require_revision(expected_revision, revisions.mapping_revision(current))
    except service.MappingRejectedError as exc:
        raise _rejected(exc) from exc

    mapping = await service.load_mapping_for_scope(db, scope_type, org_id, team_id, user_id)
    if mapping is None:
        return

    destination_id = mapping.destination_id
    await db.delete(mapping)
    await service.write_audit(
        db,
        event_type="bedrock_routing_mapping_deleted",
        org_id=org_id or service.PLATFORM_AUDIT_ORG,
        actor_id=await resolve_canonical_user_id(db, current_user.user_id),
        # Read before the delete: `mapping` is expired after the flush the audit
        # write triggers, and touching an attribute then re-loads a deleted row.
        details={"scope": scope, "destination_id": destination_id},
    )
    await db.commit()
    service.invalidate_resolver_existence_cache()
    logger.info("bedrock_routing_mapping_deleted scope_type=%s", scope_type)


@router.get("/effective/{user_id}", response_model=EffectiveMappingResponse)
async def get_effective_mapping(
    user_id: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EffectiveMappingResponse:
    """ "Who serves this person, and from which rule?" **Platform admin only.**

    §6.3 element 1, and the §1.4 display requirement. Reports the winning rung, who
    authored it, whether it overrides the person's own selection, and what it shadows.

    Reporting the rung is the point, not decoration: a destination shown *without* the
    rule that chose it invites an admin to "fix" the wrong row — the #4511 discipline
    applied to a UI, and the labelled-source requirement #4691 reached for limits.

    A ``platform`` answer means no usable rule matched. That is an answer (today's
    ambient-IRSA behaviour), not an absence — the same distinction R2's resolver makes
    by returning an explicit target rather than None.

    Args:
        user_id: Canonical ``users.id``. Not a Cognito sub and not a GitHub login: the
            mapping rows store canonical ids (#4647), so anything else would resolve
            to a confident "platform" that is simply wrong.

    Raises:
        HTTPException:
            ``403`` for any caller who is not a platform admin;
            ``422`` when no such user exists — rather than a "platform" answer, which
            would read as a resolution rather than as a bad id.
    """
    AccessControl(db).require_platform_admin(current_user)

    try:
        return EffectiveMappingResponse(**await service.resolve_effective(db, user_id))
    except service.MappingRejectedError as exc:
        raise _rejected(exc) from exc


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------


@router.get("/destinations", response_model=list[DestinationSummary])
async def list_destinations(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: str | None = None,
) -> list[DestinationSummary]:
    """The destinations an admin may pick from. **Platform admin only.**

    ``org_id`` filters to what a rule for that tenant may actually name: destinations
    linked to it, plus platform-registered ones. That is the mockup's *"Only verified
    destinations linked to acme-corp are listed"*, and it is **only a usability
    feature** — §4.2 requirement 1 is enforced in ``PUT``, because a UI that lists the
    right options is not a control while an API that accepts anything is a hole.

    Unfiltered (the destinations *table*) returns everything, including unusable rows:
    the mockup renders a failed destination in red with its reason, and hiding one
    would hide the thing the admin needs to re-verify.

    Raises:
        HTTPException: ``403`` for any caller who is not a platform admin.
    """
    AccessControl(db).require_platform_admin(current_user)

    query = select(BedrockDestinationRegistry)
    if org_id is not None:
        query = query.where((BedrockDestinationRegistry.owner_org_id == org_id) | (BedrockDestinationRegistry.is_platform_registered.is_(True)))
    destinations = list((await db.execute(query)).scalars().all())
    counts = await service.destination_usage_counts(db, [destination.id for destination in destinations])
    links = dict((await db.execute(select(BedrockConnectionGrant.destination_id, BedrockConnectionGrant.credential_id))).all())
    return [
        _compose_destination(destination, counts.get(destination.id, 0), connection_id=links.get(destination.id))
        for destination in sorted(destinations, key=lambda d: d.label)
    ]


@router.get("/connections", response_model=list[ExistingAwsConnection])
async def list_existing_connections(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ExistingAwsConnection]:
    """Platform-wide AWS connection metadata; no secret values or AWS calls."""
    AccessControl(db).require_platform_admin(current_user)
    rows = await db.execute(
        select(UserCredential, Organization.name, User.name, User.email)
        .outerjoin(Organization, Organization.id == UserCredential.org_id)
        .outerjoin(User, User.id == UserCredential.user_id)
        .where(UserCredential.service == "aws", UserCredential.credential_type == "aws_role")
        .order_by(UserCredential.label, UserCredential.id)
    )
    result = []
    for credential, org_name, user_name, email in rows:
        scopes = credential.scopes or {}
        reason = None
        try:
            service.require_routable_connection(credential)
        except service.MappingRejectedError as exc:
            reason = exc.reason
        result.append(
            ExistingAwsConnection(
                credential_id=credential.id,
                label=credential.label,
                account_id=scopes.get("account_id"),
                org_id=credential.org_id,
                org_name=org_name or credential.org_id,
                owner_scope=credential.owner_scope,
                owner_name=user_name or email,
                status=scopes.get("status") or "pending",
                selectable=reason is None,
                reason=reason,
            )
        )
    return result


@router.post("/connection-links", response_model=RegisterDestinationResponse, status_code=201)
async def link_existing_connection(
    request: RegisterSharedConnectionDestination,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    secrets: Annotated[SecretsManagerHelper, Depends(get_secrets_manager)],
    expected_revision: Annotated[str | None, Query(pattern=r"^(absent|[a-f0-9]{64})$")] = None,
) -> RegisterDestinationResponse:
    """Grant one org Bedrock use of an existing connection after a fresh AWS probe.

    No IAM mutation, new role, secret copy, or vault ownership change. The account
    owner can update their existing role when the capability probe refuses it.
    """
    AccessControl(db).require_platform_admin(current_user)
    await revisions.serialize_writes(db)
    if await db.get(Organization, request.link_to_org_id) is None:
        raise _rejected(service.MappingRejectedError("scope_not_found", "The organization no longer exists."))
    # Serialize retries for a connection, including different target orgs. The
    # grant's unique constraint is the durable one-link-per-connection/org guard.
    credential = await db.scalar(
        select(UserCredential)
        .where(UserCredential.id == request.credential_id, UserCredential.service == "aws", UserCredential.credential_type == "aws_role")
        .with_for_update()
    )
    if credential is None:
        raise _rejected(service.MappingRejectedError("connection_not_found", "That AWS connection no longer exists."))
    try:
        account_id, role_arn = service.require_routable_connection(credential)
    except service.MappingRejectedError as exc:
        raise _rejected(exc) from exc
    actor_id = await resolve_canonical_user_id(db, current_user.user_id)
    grant = await db.scalar(
        select(BedrockConnectionGrant).where(
            BedrockConnectionGrant.credential_id == credential.id, BedrockConnectionGrant.org_id == request.link_to_org_id
        )
    )
    selected = None
    if request.destination_id is not None:
        selected = await service.load_destination(db, request.destination_id)
        revisions.require_revision(expected_revision, revisions.destination_revision(selected))
        if (selected.credential_id, selected.owner_org_id, selected.account_id, selected.role_arn) != (
            credential.id,
            request.link_to_org_id,
            account_id,
            role_arn,
        ) or selected.is_platform_registered:
            raise HTTPException(409, detail={"reason": "connection_destination_mismatch"})
        if grant is not None and grant.destination_id != selected.id:
            raise HTTPException(409, detail={"reason": "connection_link_exists_elsewhere"})
    elif expected_revision is not None:
        raise HTTPException(422, detail={"reason": "destination_required_for_revision"})
    if grant is not None:
        destination = await service.load_destination(db, grant.destination_id)
        if (destination.account_id, destination.role_arn) != (account_id, role_arn):
            raise HTTPException(
                status_code=409, detail="The connection's AWS role has changed. Remove its routing rules and unlink it before linking again."
            )
    else:
        destination = selected or service.build_destination_from_credential(credential, account_id=account_id, role_arn=role_arn, actor_id=actor_id)
        destination.owner_org_id = request.link_to_org_id
    capable, reason = await service.test_assume_destination(db, destination, secrets=secrets, probe_user_id=actor_id)
    if not capable:
        await service.write_refusal_audit(
            db,
            event_type="bedrock_connection_link_refused",
            org_id=request.link_to_org_id,
            actor_id=actor_id,
            details={"credential_id": credential.id, "reason": reason or "routing_probe_inconclusive"},
        )
        raise _rejected(
            service.MappingRejectedError(
                reason or "routing_probe_inconclusive",
                "The existing connection could not be verified for shared Bedrock use. Ask its AWS account administrator to check "
                "the role's trust policy and Bedrock permissions, then retry. No link was saved and the original connection was not changed.",
            )
        )
    destination.routing_capable = True
    destination.verified_at = utcnow()
    if grant is None:
        db.add(destination)
        await db.flush()
        db.add(
            BedrockConnectionGrant(
                destination_id=destination.id, credential_id=credential.id, org_id=request.link_to_org_id, created_by_user_id=actor_id
            )
        )
    await service.write_audit(
        db,
        event_type="bedrock_connection_linked",
        org_id=request.link_to_org_id,
        actor_id=actor_id,
        details={"destination_id": destination.id, "credential_id": credential.id, "connection_org_id": credential.org_id, "account_id": account_id},
    )
    await db.commit()
    await db.refresh(destination)
    service.invalidate_signer_cache(destination.role_arn)
    counts = await service.destination_usage_counts(db, [destination.id])
    return RegisterDestinationResponse(destination=_compose_destination(destination, counts.get(destination.id, 0), connection_id=credential.id))


@router.delete("/connection-links/{destination_id}", status_code=204)
async def unlink_existing_connection(
    destination_id: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    expected_revision: Annotated[str | None, Query(pattern=r"^(absent|[a-f0-9]{64})$")] = None,
) -> None:
    """Remove an unused Bedrock grant, preserving the source connection and AWS role."""
    AccessControl(db).require_platform_admin(current_user)
    await revisions.serialize_writes(db)
    destination = await db.scalar(select(BedrockDestinationRegistry).where(BedrockDestinationRegistry.id == destination_id).with_for_update())
    grant = await db.get(BedrockConnectionGrant, destination_id)
    if destination is None or grant is None:
        raise HTTPException(status_code=404, detail="That existing-connection link no longer exists.")
    revisions.require_revision(expected_revision, revisions.destination_revision(destination))
    if (await service.destination_usage_counts(db, [destination_id])).get(destination_id):
        raise HTTPException(status_code=409, detail="Remove the routing rules that use this destination before unlinking it.")
    actor_id = await resolve_canonical_user_id(db, current_user.user_id)
    await service.write_audit(
        db,
        event_type="bedrock_connection_unlinked",
        org_id=grant.org_id,
        actor_id=actor_id,
        details={"destination_id": destination_id, "credential_id": grant.credential_id},
    )
    await db.execute(delete(BedrockConnectionGrant).where(BedrockConnectionGrant.destination_id == destination_id))
    await db.delete(destination)
    await db.commit()
    service.invalidate_resolver_existence_cache()
    service.invalidate_signer_cache(destination.role_arn)


@router.post("/destinations", response_model=RegisterDestinationResponse, status_code=201)
async def register_destination(
    request: Annotated[RegisterConnectionDestination | RegisterNewDestination, Body(discriminator="source")],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    secrets: Annotated[SecretsManagerHelper, Depends(get_secrets_manager)],
) -> RegisterDestinationResponse:
    """Add a destination to the registry. **Platform admin only.**

    Two sources, discriminated on ``source``:

    * ``connection`` — promote a tenant's existing verified AWS connection. Nothing to
      launch; the role already exists and has been assumed at least once.
    * ``new_account`` — §6.6's "Register new destination". Returns a CloudFormation
      quick-create URL for the **v2 routing template** (§5.0b: v1 pins the role to one
      user id, so a v1 destination cannot serve the team/org rules it exists for).

    **A newly registered destination is not usable until it verifies.**
    ``routing_capable`` stays False and ``verified_at`` NULL, and the resolver refuses
    those (§4.4). This is deliberate and is the fail-closed discipline applied to
    registration itself: merely *starting* a registration must not be able to reroute
    traffic onto an account where the role does not exist yet. ``PUT /mappings/...``
    and ``POST /destinations/{id}/verify`` are the two paths that flip it, and both do
    it by running the real probe.

    Raises:
        HTTPException:
            ``403`` for any caller who is not a platform admin;
            ``422`` when the named org or connection does not exist, or the connection
            is not a verified AWS role.
    """
    AccessControl(db).require_platform_admin(current_user)
    await revisions.serialize_writes(db)

    actor_id = await resolve_canonical_user_id(db, current_user.user_id)

    if isinstance(request, RegisterConnectionDestination):
        credential = await db.scalar(
            select(UserCredential).where(
                UserCredential.id == request.credential_id,
                UserCredential.credential_type == "aws_role",
            )
        )
        if credential is None:
            raise _rejected(service.MappingRejectedError("connection_not_found", "No AWS connection with that id exists."))
        # §4.4: `pending` rows must be EXCLUDED, not deprioritised — a connection whose
        # role has not been created yet would fail every call. Checked in the service so
        # the self-service selector (§6.4), which needs the identical refusal, cannot
        # drift from this one.
        try:
            account_id, role_arn = service.require_routable_connection(credential)
        except service.MappingRejectedError as exc:
            raise _rejected(exc) from exc
        # The credential's OWN tenant, never a parameter — see the builder. There is no
        # field with which an admin could mislabel a connection as belonging to a tenant
        # it does not, which is what makes the §4.2 check meaningful later.
        destination = service.build_destination_from_credential(
            credential,
            account_id=account_id,
            role_arn=role_arn,
            actor_id=actor_id,
            label=request.label,
        )
        db.add(destination)
        launch_url = None
    else:
        org_exists = await db.scalar(select(Organization.id).where(Organization.id == request.link_to_org_id).limit(1))
        if org_exists is None:
            raise _rejected(
                service.MappingRejectedError(
                    "scope_not_found",
                    f"No organization with id '{request.link_to_org_id}' exists on this platform.",
                )
            )

        # Per-destination ExternalId for confused-deputy protection, generated here and
        # stored only in Secrets Manager — the same discipline `connect_start` follows,
        # and the reason the registry row deliberately has no column for it.
        external_id = str(uuid.uuid4())
        role_arn = compute_role_arn(request.account_id, request.label)
        secret_arn = await _store_destination_secret(
            secrets=secrets,
            account_id=request.account_id,
            label=request.label,
            role_arn=role_arn,
            external_id=external_id,
            region=request.region,
            org_id=request.link_to_org_id,
        )
        credential = UserCredential(
            id=new_uuid(),
            org_id=request.link_to_org_id,
            # All three owner columns NULL = org-scoped (the `vault.py` convention).
            # That is what makes this destination pass §4.3's ruling-6 check for team
            # and org rungs, which a user-owned row cannot.
            service="aws",
            credential_type="aws_role",
            label=request.label,
            secret_arn=secret_arn,
            scopes={"account_id": request.account_id, "role_arn": role_arn, "status": "pending"},
        )
        db.add(credential)
        destination = BedrockDestinationRegistry(
            id=new_uuid(),
            account_id=request.account_id,
            role_arn=role_arn,
            credential_id=credential.id,
            owner_org_id=request.link_to_org_id,
            is_platform_registered=False,
            label=request.label,
            region=request.region,
            registered_by_user_id=actor_id,
        )
        db.add(destination)
        launch_url = build_launch_url(
            credential_id=credential.id,
            nickname=request.label,
            external_id=external_id,
            account_id=request.account_id,
            # Unused by v2 (it declares no UserSessionTag parameter), passed because
            # the signature requires it. That omission is the whole point of v2.
            user_id=actor_id,
            region=request.region,
            template_version=ROUTING_TEMPLATE_VERSION,
        )

    await service.write_audit(
        db,
        event_type="bedrock_routing_destination_registered",
        org_id=destination.owner_org_id or service.PLATFORM_AUDIT_ORG,
        actor_id=actor_id,
        details={
            "destination_id": destination.id,
            "account_id": destination.account_id,
            "source": request.source,
            "destination_role_arn": destination.role_arn,
        },
    )
    await db.commit()
    await db.refresh(destination)

    logger.info("bedrock_routing_destination_registered source=%s account=%s", request.source, destination.account_id)
    return RegisterDestinationResponse(destination=_compose_destination(destination, 0), launch_url=launch_url)


@router.get("/destinations/{destination_id}/setup", response_model=DestinationSetupResponse)
async def destination_setup(
    destination_id: str,
    response: Response,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    secrets: Annotated[SecretsManagerHelper, Depends(get_secrets_manager)],
) -> DestinationSetupResponse:
    """Resume setup without creating another destination or rotating its ExternalId."""
    AccessControl(db).require_platform_admin(current_user)
    response.headers["Cache-Control"] = "no-store"
    destination = await db.get(BedrockDestinationRegistry, destination_id)
    if destination is None:
        raise HTTPException(status_code=404, detail="No such destination.")
    credential = await db.get(UserCredential, destination.credential_id) if destination.credential_id else None
    grant = await db.get(BedrockConnectionGrant, destination_id)
    if (
        credential is None
        or grant is not None
        or credential.credential_type != "aws_role"
        or credential.owner_scope != "org"
        or credential.org_id != destination.owner_org_id
        or destination.role_arn != compute_role_arn(destination.account_id, destination.label)
    ):
        raise HTTPException(
            status_code=409,
            detail="This destination uses an existing AWS connection. Ask its owner to update the role, then re-verify it here.",
        )
    external_id = await service._destination_external_id(db, destination, secrets)
    if not external_id:
        raise HTTPException(
            status_code=409, detail="The destination's setup details could not be read. Retry or contact your platform administrator."
        )
    launch_url = build_launch_url(
        credential_id=credential.id,
        nickname=destination.label,
        external_id=external_id,
        account_id=destination.account_id,
        user_id=destination.registered_by_user_id,
        region=destination.region,
        template_version=ROUTING_TEMPLATE_VERSION,
    )
    template = await asyncio.to_thread(read_routing_template)
    return DestinationSetupResponse(
        account_id=destination.account_id,
        role_arn=destination.role_arn,
        region=destination.region,
        launch_url=launch_url,
        download_filename=f"adp-bedrock-{destination.account_id}.zip",
        download_base64=routing_setup_download(
            launch_url=launch_url, account_id=destination.account_id, role_arn=destination.role_arn, region=destination.region, template=template
        ),
    )


@router.post("/destinations/{destination_id}/verify", response_model=VerifyDestinationResponse)
async def verify_destination(
    destination_id: str,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    secrets: Annotated[SecretsManagerHelper, Depends(get_secrets_manager)],
) -> VerifyDestinationResponse:
    """Re-run the real probe against a destination. **Platform admin only.**

    §6.7 item 5: on-demand re-validation exists so an admin diagnosing a fail-closed
    outage can distinguish "the mapping is wrong" from "Bedrock is down" without
    waiting for a user to retry. It is also how a freshly registered destination
    becomes usable after its CloudFormation stack finishes.

    **Always re-probes**, never replays a stored verdict — a cached answer is the one
    thing this endpoint must not give, since the caller is asking precisely because
    they doubt the stored one. §6.7 item 4: a pass is a statement about now, not a
    permanent guarantee.

    A failure **clears** ``routing_capable`` and ``verified_at``, so a destination that
    has stopped working stops being selectable rather than staying green on a stale
    pass. Existing mappings pointing at it are left in place: the resolver skips an
    unusable destination and falls through (§4.4), and silently deleting an admin's
    rules on a transient probe failure would be a far larger action than they asked
    for.

    Raises:
        HTTPException:
            ``403`` for any caller who is not a platform admin;
            ``404`` when no such destination exists.
    """
    AccessControl(db).require_platform_admin(current_user)
    await revisions.serialize_writes(db)

    destination = await db.scalar(select(BedrockDestinationRegistry).where(BedrockDestinationRegistry.id == destination_id).with_for_update())
    if destination is None:
        raise HTTPException(status_code=404, detail="No such destination.")

    actor_id = await resolve_canonical_user_id(db, current_user.user_id)
    capable, reason = await service.test_assume_destination(db, destination, secrets=secrets, probe_user_id=actor_id)

    destination.routing_capable = capable
    destination.verified_at = utcnow() if capable else None

    await service.write_audit(
        db,
        event_type="bedrock_routing_destination_verified" if capable else "bedrock_routing_destination_verify_failed",
        org_id=destination.owner_org_id or service.PLATFORM_AUDIT_ORG,
        actor_id=actor_id,
        details={
            "destination_id": destination.id,
            "account_id": destination.account_id,
            "routing_capable": capable,
            "reason": reason,
            "destination_role_arn": destination.role_arn,
        },
    )
    await db.commit()
    await db.refresh(destination)

    # Drop any credentials cached under this role. R3 left the hook for exactly this
    # (`bedrock_signing.py:241`) — a re-verify is where an operator most wants the old
    # session gone rather than merely unreachable.
    service.invalidate_signer_cache(destination.role_arn)

    counts = await service.destination_usage_counts(db, [destination.id])
    connection_id = await db.scalar(select(BedrockConnectionGrant.credential_id).where(BedrockConnectionGrant.destination_id == destination.id))
    return VerifyDestinationResponse(
        destination=_compose_destination(destination, counts.get(destination.id, 0), reason, connection_id=connection_id),
        verified=capable,
        reason=reason,
    )


async def _store_destination_secret(
    *,
    secrets: SecretsManagerHelper,
    account_id: str,
    label: str,
    role_arn: str,
    external_id: str,
    region: str,
    org_id: str,
) -> str:
    """Write the destination's routing payload to Secrets Manager, org-scoped.

    Same payload shape ``connect_start`` writes, so the signer's
    ``_resolve_external_id`` and this module's probe read one format rather than two —
    the "verified here, broken there" failure §6.7 warns about applies to the *payload*
    as much as to the probe.

    Stored under the org namespace (``adp/orgs/<org_id>/...``) because the destination
    is org-linked and owned by nobody in particular; a user namespace would tie a
    shared routing destination to whichever admin happened to register it.

    The ExternalId reaches Secrets Manager and the quick-create URL and **nowhere
    else** — no registry column, no audit detail, no log line.
    """
    payload = json.dumps(
        {
            "role_arn": role_arn,
            "external_id": external_id,
            "account_id": account_id,
            "default_region": region,
        }
    )
    # Blocking boto3 call, same `to_thread` wrap every other caller uses.
    return await asyncio.to_thread(secrets.create_secret, "aws", label, payload, org_id=org_id)
