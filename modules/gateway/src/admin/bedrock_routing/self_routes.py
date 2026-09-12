"""The self-service Bedrock account selector — Issue #4746 (#4692 · R5).

Design note ``docs/design-notes/4692-per-principal-bedrock-account-routing.md`` §6.4
(the selector), §1.4 (admin wins, and the display it obliges), §4.4 (verified-only),
§2.5 (fail closed), ruling 2 ("no new screen for the self path").

R4 (#4745) shipped the platform-admin authoring API beside this file. This module is
the other half: the surface on which a person points their **own** Bedrock traffic at
one of their **own** connected AWS accounts. Both write the same user-rung row in
``bedrock_account_mappings``; what differs is who is allowed to, and that difference is
the whole of this module.

## Why a separate router rather than three more routes on ``routes.py``

``routes.py`` is platform-admin-only on every route, asserted against its own source by
``test_authz.py`` (``require_platform_admin`` must be the first statement in every
handler). These routes are deliberately callable by an ordinary member. Adding them
there would either break that assertion or — far worse — invite a future edit to
"restore" it and silently take the feature away. Its module docstring already disclaims
this surface by name: *"Different authz (the caller IS the target), different storage
path."*

## The authz is the SHAPE of the route, not a check inside it

There is no target parameter at any position on any route here. No ``user_id``, no
``scope``, no ``destination_id``. The anchor is
the selected workspace's org-local user, derived from the validated token, so a
request naming somebody else cannot be *formed* — nothing has to notice and refuse it.
This is the argument ``person_cap_routes`` makes for ``/me/budget/person-cap`` and the
reason ``personCap.ts`` carries the same note on the client.

The one id a caller does supply, ``credential_id``, is looked up **scoped to the
caller** (``user_id`` and ``org_id`` from the token's resolution, the same predicate
``connect_verify`` uses). Another person's connection id therefore does not resolve at
all; it is not a permission failure to check for. The UI's list of selectable
connections is an affordance, and the server re-derives every gate on write — §4.2's
rule that *"a UI that only lists in-scope options is a usability feature; an API that
only accepts them is the control"* applies to the self surface as much as to the admin
one.

## "Admin wins" has to be enforced HERE, and this is the non-obvious part

``uq_bedrock_account_mapping_scope`` allows exactly ONE row per scope, so the user rung
is a single row that both surfaces upsert. There is no precedence to evaluate between
two rows at read time, because there are never two. So §1.4's "admin wins" can only
live in the writes: this module refuses to overwrite or delete a row a platform admin
authored (``service.require_not_admin_pinned``). Without that refusal, a person could
undo their own pin by re-selecting — reversing the exact decision the override exists to
take out of their hands — and no test of the *display* would notice, because the display
would then be telling the truth about a row that should not have changed.

## The two ways the person's own choice can fail to be in force

The read reports the **effective** destination (R4's ladder walk) and states
``own_selection_active`` explicitly, because a stored row is not evidence that it
governs:

1. A platform admin has taken the user rung (above).
2. The person's own destination has stopped being usable — role deleted, probe now
   failing — so the resolver skips it and walks to the team/org/platform rung (§4.4).

Both render as "your selection is not what is serving you", and a screen that showed
the stored pick as active in either case would be the #4511 inert-config defect.

## Fail closed, and why this surface in particular says so

Ruling 1: if the chosen account cannot serve a call, the call **fails** with an
explanatory error; there is no fallback to the platform account. §6.4 requires the
selector to disclose that, because this is the one surface where the person making the
choice is the person who gets paged by it. The copy lives in the component; the
server-side counterpart is that a destination which cannot be proven is refused at save
time (``validate_mapping_target``) rather than stored to fail later.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user
from src.auth.vault_routes import get_secrets_manager
from src.shared.database import get_db
from src.shared.models.base import new_uuid
from src.shared.models.bedrock_routing import BedrockAccountMapping, BedrockDestinationRegistry
from src.shared.models.vault import UserCredential
from src.shared.schemas.auth import TokenContext
from src.shared.services.routing_probe import ROUTING_REASON_USER_PINNED
from src.shared.services.secrets_manager import SecretsManagerHelper

from . import service
from .schemas import EffectiveMappingResponse, MySelectionRequest, MySelectionResponse, SelectableConnection

logger = logging.getLogger("bedrockgateway.bedrock_routing.self")

# `/me/...`, no `/admin` prefix, and no `/api` prefix — CloudFront strips the first
# `/api` before the origin (#4330, guarded by tests/test_route_prefix_convention.py).
# The `/me` namespace is the one the rest of the platform uses for "derived from your
# token, takes no target" (`/me/budget/person-cap`, `/me/activity`), and using it here
# makes the absence of a target parameter a visible property of the path rather than
# something a reader has to check the signature for.
router = APIRouter(prefix="/me/bedrock-routing", tags=["bedrock-routing"])


def _rejected(exc: service.MappingRejectedError) -> HTTPException:
    """A refusal as a 422 carrying the shared reason code — same shape as R4's.

    Identical to ``routes._rejected`` on purpose: the client branches on ``reason``, and
    one surface answering ``{reason, message}`` while the other answered a bare string
    would mean two error parsers for one vocabulary (§6.7 item 2).
    """
    return HTTPException(status_code=422, detail={"reason": exc.reason, "message": exc.message})


async def _caller_id(db: AsyncSession, current_user: TokenContext) -> str:
    """The caller's canonical ``users.id``. The anchor for everything in this module.

    Canonical id, never the Cognito sub: ``scope_id_user`` and ``authored_by_user_id``
    are both canonical (#4647), and a mapping written with a sub resolves for nobody —
    it would read as a configured selection and govern no call.

    ``resolve_canonical_user_id`` falls back to the raw sub when there is no ``users``
    row. That fallback is safe here and is *not* a way to author a phantom mapping: the
    connection lookup below is scoped by ``user_credentials.user_id``, which is a real
    FK, so an unprovisioned caller has no connections to select and every write refuses
    before it can store anything.
    """
    from src.shared.identity.workspaces import workspace_user

    user = await workspace_user(db, current_user.user_id, current_user.org_id, username=current_user.cognito_username)
    return user.id if user and user.org_id == current_user.org_id else current_user.user_id


async def _own_connections(db: AsyncSession, user_id: str) -> list[UserCredential]:
    """The caller's own AWS connections. Scoped by ``user_id``, which is the boundary.

    Personal rows only (``user_id ==`` the caller): an org-scoped connection has all
    three owner columns NULL by the ``vault.py`` convention and belongs to the tenant,
    not to a person, so it is not something an individual may point their traffic at
    from here. A platform admin can register one as a shared destination — that is the
    admin surface's job (§4.3, ruling 6).
    """
    rows = await db.scalars(
        select(UserCredential).where(
            UserCredential.user_id == user_id,
            UserCredential.service == "aws",
            UserCredential.credential_type == "aws_role",
        )
    )
    return list(rows)


def _describe_connection(credential: UserCredential) -> SelectableConnection:
    """One connection as the selector renders it, with its selectability and reason.

    The selectable predicate is ``verified`` **and** routing-capable, and both halves
    matter for different reasons:

    * ``status == "verified"`` — §4.4. A ``pending`` row's role does not exist in the
      destination account yet, so selecting it would reroute the person's traffic onto
      an account that fails every call.
    * ``routing_capable`` — §5.0b. R1 (#4742) writes this into ``scopes`` at
      connect-verify time: a v1-template role is pinned to the person who created it and
      cannot be assumed *for* them by the platform, so it assumes fine in the connect
      flow and fails every routed call.

    ``routing_capable`` absent (None) is treated as **not** selectable rather than as
    unknown-so-allow. Rows predating #4742 have no value, and the fail-closed reading is
    the only safe one: an unproven destination fails 100% of the person's calls (§2.5),
    so an optimistic default would hand them an outage. The write path re-probes
    regardless, so a genuinely capable old row is one save away from being stamped —
    which is why this is a stale label, not a dead end.
    """
    scopes = credential.scopes or {}
    status = str(scopes.get("status") or "pending")
    routing_capable = scopes.get("routing_capable")

    if status != "verified":
        reason: str | None = "connection_not_verified"
    elif routing_capable is True:
        reason = None
    else:
        # R1's stored reason when it has one; otherwise the v1-pin code, which is what
        # an unprobed or never-classified row overwhelmingly is (§5.0b) and whose
        # remediation — re-run the routing template — is the actionable one.
        reason = scopes.get("routing_reason") or ROUTING_REASON_USER_PINNED

    return SelectableConnection(
        credential_id=credential.id,
        label=credential.label,
        account_id=scopes.get("account_id"),
        status=status,
        selectable=reason is None,
        reason=reason,
    )


async def _compose_selection(db: AsyncSession, user_id: str) -> MySelectionResponse:
    """The whole §6.4 payload: what serves the caller, what they chose, what they may choose.

    Assembled in one place because the three facts are only meaningful together — see
    :class:`~.schemas.MySelectionResponse`. In particular ``own_selection_active`` is
    computed here, from the effective walk and the stored row, rather than left to the
    client: a client comparing two fields would have to reimplement §4.4's skip rule to
    get it right, and a client that got it wrong would show a stale pick as active.
    """
    effective = await service.resolve_effective(db, user_id)
    mapping = await service.load_self_selection(db, user_id)

    pinned = mapping is not None and service.mapping_source(mapping) == "platform_admin"

    own_destination: BedrockDestinationRegistry | None = None
    if mapping is not None and not pinned:
        own_destination = await db.scalar(select(BedrockDestinationRegistry).where(BedrockDestinationRegistry.id == mapping.destination_id))

    connections = [_describe_connection(credential) for credential in await _own_connections(db, user_id)]

    return MySelectionResponse(
        effective=EffectiveMappingResponse(**effective),
        own_selection_destination_id=own_destination.id if own_destination else None,
        own_selection_account_id=own_destination.account_id if own_destination else None,
        own_selection_label=own_destination.label if own_destination else None,
        own_selection_credential_id=own_destination.credential_id if own_destination else None,
        # Active only if the ladder actually landed on this row's destination. A stored
        # row whose destination is no longer usable is skipped by the walk (§4.4) and
        # must not be reported as in force.
        own_selection_active=own_destination is not None and effective["destination_id"] == own_destination.id,
        pinned_by_platform_admin=pinned,
        connections=sorted(connections, key=lambda c: c.label),
    )


@router.get("/selection", response_model=MySelectionResponse)
async def get_my_selection(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MySelectionResponse:
    """Which AWS account serves the caller's Bedrock calls, and what they may pick (§6.4).

    **Takes no target parameter.** The caller always gets their own answer; there is
    nothing to name.

    Reports the destination that is actually **in force**, not merely the row the caller
    stored. §6.4: *"Show the effective destination, including when it is overridden by a
    platform mapping (§1.4). 'Your calls currently go to …1234 (set by a platform
    admin)' is honest; showing the user's own stale pick as active is the inert-config
    defect."* ``own_selection_active`` is the field that keeps that promise, and it is
    False both when an admin has pinned the caller and when the caller's own choice has
    silently stopped being usable.

    A ``platform`` rung with no account is a real answer — today's ambient-IRSA
    behaviour — and the screen should render it as "the platform's account", never as
    "unconfigured".

    Raises:
        HTTPException:
            ``401`` when unauthenticated (from ``get_current_user``);
            ``422`` when the caller has no ``users`` row, so nothing can be resolved for
            them — rather than a confident "platform" answer, which would read as a
            resolution rather than as an unprovisioned account.
    """
    user_id = await _caller_id(db, current_user)
    try:
        return await _compose_selection(db, user_id)
    except service.MappingRejectedError as exc:
        raise _rejected(exc) from exc


@router.put("/selection", response_model=MySelectionResponse)
async def put_my_selection(
    request: MySelectionRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    secrets: Annotated[SecretsManagerHelper, Depends(get_secrets_manager)],
) -> MySelectionResponse:
    """Point the caller's own Bedrock traffic at one of their own AWS accounts (§6.4).

    **Names a connection, never a person.** The body has no ``user_id``, and the
    ``credential_id`` it does carry is looked up scoped to the caller — so another
    person's connection id does not resolve, and another person's *traffic* cannot be
    addressed at all.

    Every gate refuses with a ``422`` and **writes nothing** (ruling 4a, "reject, never
    store inert"), cheapest first:

    ==================================== =========================================
    Refusal (``reason``)                 Why
    ==================================== =========================================
    ``connection_not_found``             Not the caller's connection, or gone. The
                                        ownership boundary, expressed as a scoped
                                        lookup rather than a comparison.
    ``pinned_by_platform_admin``         §1.4 "admin wins". One row per scope, so
                                        overwriting the admin's row IS reversing
                                        their decision.
    ``connection_not_verified``          §4.4. A pending role does not exist yet
                                        and would fail every call.
    probe codes (``role_user_pinned_``   §6.7. A destination that cannot serve a
    ``needs_v2_template`` etc.)          call must not become a rule that fails
                                        100% of them under fail-closed.
    ==================================== =========================================

    The admin-pin check runs **before** the probe deliberately: a pinned caller's write
    is refused for a reason that has nothing to do with their account's health, and
    probing first would spend a network round trip to produce a *second*, misleading
    reason for a request that was never going to be stored.

    Idempotent. Re-selecting the same account replaces the destination on the existing
    row, keeping its ``id`` and ``created_at``.

    Returns the full selection payload rather than a bare acknowledgement, so the screen
    re-renders from the server's own answer — including the case where the save
    succeeded and something else still governs (§4.4), which a client-side optimistic
    update would get wrong.

    Raises:
        HTTPException:
            ``401`` when unauthenticated;
            ``422`` for any gate above — nothing is written in any of those cases, and
            the detail carries a stable ``reason``.
    """
    user_id = await _caller_id(db, current_user)

    try:
        credential = await db.scalar(
            select(UserCredential).where(
                UserCredential.id == request.credential_id,
                # The ownership boundary. Not a comparison after the fact: a connection
                # belonging to anybody else does not match, so there is no state to leak
                # and no check to forget.
                UserCredential.user_id == user_id,
                UserCredential.service == "aws",
                UserCredential.credential_type == "aws_role",
            )
        )
        if credential is None:
            raise service.MappingRejectedError(
                "connection_not_found",
                "No AWS connection of yours has that id. Connect the account first, or refresh the page.",
            )

        mapping = await service.load_self_selection(db, user_id)
        # Before the probe: see the docstring. A pinned caller is refused for a reason
        # unrelated to their account's health.
        service.require_not_admin_pinned(mapping)

        destination = await service.find_or_create_destination_for_credential(db, credential, actor_id=user_id)
        await service.validate_mapping_target(
            db,
            destination=destination,
            # The user rung. `service._reject_personal_credential` returns early for it,
            # which is the point: a person's own credential serving their own traffic is
            # exactly what ruling 2 allows, and what §4.3 forbids only for team/org rules.
            scope_type="user",
            # The credential's own tenant. A person's connection and the mapping for that
            # person are the same tenant by construction, so this check is trivially
            # satisfied here — passed anyway rather than bypassed, so the gate stays
            # total and no destination reaches the resolver through an unchecked path.
            scope_org_id=credential.org_id,
            secrets=secrets,
            # CloudTrail attribution in the destination account, not an authorization
            # input: the probe assumes without session tags. The person whose account it
            # is, is the right party to attribute it to.
            probe_user_id=user_id,
        )
    except service.MappingRejectedError as exc:
        await service.write_refusal_audit(
            db,
            event_type="bedrock_routing_self_selection_rejected",
            # A user-rung event names no org, the same reason R4's does not — see
            # `service.PLATFORM_AUDIT_ORG`.
            org_id=service.PLATFORM_AUDIT_ORG,
            actor_id=user_id,
            details={"credential_id": request.credential_id, "reason": exc.reason},
        )
        raise _rejected(exc) from exc

    if mapping is None:
        mapping = BedrockAccountMapping(
            id=new_uuid(),
            scope_type="user",
            scope_id_user=user_id,
            destination_id=destination.id,
            # The caller, which is what makes `service.mapping_source` read this row back
            # as `self` — and what makes a later admin PUT, which re-stamps this column,
            # read as `platform_admin`. That is the §1.4 transition, stored as a fact
            # about who wrote the row rather than as a flag that could disagree with it.
            authored_by_user_id=user_id,
        )
        db.add(mapping)
    else:
        mapping.destination_id = destination.id
        mapping.authored_by_user_id = user_id

    await service.write_audit(
        db,
        event_type="bedrock_routing_self_selection_authored",
        org_id=service.PLATFORM_AUDIT_ORG,
        actor_id=user_id,
        details={
            "credential_id": credential.id,
            "destination_id": destination.id,
            "destination_account_id": destination.account_id,
            # Server-side only — the §2.6 split. Never on a response or in an error.
            "destination_role_arn": destination.role_arn,
        },
    )
    await db.commit()

    # So this pod stops answering "no mappings exist" from its 60s cache. On an install
    # whose mapping table was empty — which is every install before its first selection —
    # the person would otherwise watch their traffic keep landing on the platform account
    # for up to a minute and conclude the selector did nothing.
    service.invalidate_resolver_existence_cache()

    logger.info("bedrock_routing_self_selection_authored account=%s", destination.account_id)
    return await _compose_selection(db, user_id)


@router.delete("/selection", response_model=MySelectionResponse)
async def delete_my_selection(
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MySelectionResponse:
    """Stop routing the caller's Bedrock calls to their own account (§6.4).

    **This does not make the caller unroutable.** Removing their row un-shadows the
    ladder beneath it: their traffic falls to their team's rule, then their org's, then
    the platform account. The response says where it landed, which is why this returns
    the selection payload rather than a ``204`` — "you are back on your team's account"
    and "you are back on the platform's" are different outcomes, and the person should
    not have to guess which.

    **Refuses when a platform admin has pinned the caller** (``pinned_by_platform_admin``).
    The pinned row is the admin's, not theirs; deleting it would reverse the override
    §1.4 settled in the admin's favour. This is the same guard the ``PUT`` applies, and
    it is needed on both — an unguarded ``DELETE`` would be a one-request way around a
    guarded ``PUT``.

    Idempotent when nothing is stored: there is no row to remove and the outcome the
    caller asked for already holds, so it is not an error.

    Raises:
        HTTPException:
            ``401`` when unauthenticated;
            ``422`` when a platform admin has pinned the caller.
    """
    user_id = await _caller_id(db, current_user)

    mapping = await service.load_self_selection(db, user_id)
    try:
        service.require_not_admin_pinned(mapping)
    except service.MappingRejectedError as exc:
        await service.write_refusal_audit(
            db,
            event_type="bedrock_routing_self_selection_rejected",
            org_id=service.PLATFORM_AUDIT_ORG,
            actor_id=user_id,
            details={"action": "delete", "reason": exc.reason},
        )
        raise _rejected(exc) from exc

    if mapping is not None:
        destination_id = mapping.destination_id
        await db.delete(mapping)
        await service.write_audit(
            db,
            event_type="bedrock_routing_self_selection_cleared",
            org_id=service.PLATFORM_AUDIT_ORG,
            actor_id=user_id,
            # Read before the delete: the flush the audit write triggers expires
            # `mapping`, and touching an attribute afterwards re-loads a deleted row.
            details={"destination_id": destination_id},
        )
        await db.commit()
        service.invalidate_resolver_existence_cache()
        logger.info("bedrock_routing_self_selection_cleared")

    try:
        return await _compose_selection(db, user_id)
    except service.MappingRejectedError as exc:
        raise _rejected(exc) from exc
