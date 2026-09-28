"""Write-side guard: may this tenant CLAIM this installation?

Issue #4072 (sub-EPIC #4068, child ·B).

Why this is separate from ``assert_installation_owned_by``
---------------------------------------------------------
·A0's ``assert_installation_owned_by`` answers "does this tenant *already*
provably own this installation?" and fails closed on ``NOT_FOUND``. That is the
right rule for a *read* authorization ("may I act on this installation I claim
to own"), and the wrong rule for a *binding write*: binding an installation for
the first time is exactly the case where nobody owns it yet. Using the read
check on the write paths would reject every legitimate first-time claim.

The rule a binding write needs is the one the pre-existing platform-admin
org-linking endpoint already implements (``admin/tenants/routes.py``):

===========================  ==================  ==========================
resolver outcome             decision            status
===========================  ==================  ==========================
owned by this tenant         idempotent success   200 (no error raised)
not claimed by anyone        proceed              200 (no error raised)
owned by a different tenant  refuse               409
ownership disputed           refuse               409
unattestable                 refuse               403
===========================  ==================  ==========================

"Unowned proceeds" is not a hole: the *only* record that can make an
installation unowned is the absence of any claim in either store, and creating
the claim is precisely the operation being authorized. The caller's authority
over the *target org* is a separate check that the route performs first — both
are required, and neither substitutes for the other (#4072 threat model: #5
fails the org-authority half, #11 fails the installation-ownership half).

This module contains no ownership logic of its own. It is a mapping from ·A0's
one resolver onto one HTTP contract, so the two #11 writers cannot drift apart —
which is the whole reason ·A0 exists.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select

from src.admin.installations.resolver import OwnerState, resolve_installation_owner
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.organization import Organization

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.admin.connections.github_client import GitHubAppClient

logger = logging.getLogger(__name__)


class InstallationClaimError(BedrockGatewayError):
    """Raised when ``org_id`` may not claim ``installation_id``.

    A ``BedrockGatewayError`` so ``app.py``'s registered handler renders the
    status directly and no route has to re-derive it — the same way
    ``ResourceConflictError`` reaches the client as a 409. ``state`` is retained
    for callers that want to log the resolver outcome.
    """

    def __init__(self, message: str, *, status_code: int, state: OwnerState, installation_id: int, org_id: str) -> None:
        super().__init__(
            error="installation_claim_denied",
            message=message,
            status_code=status_code,
            details={"installation_id": str(installation_id), "org_id": org_id, "ownership_state": state.value},
        )
        self.state = state
        self.installation_id = installation_id
        self.org_id = org_id


async def assert_installation_claimable_by(
    org_id: str,
    installation_id: int,
    *,
    db: AsyncSession,
    attest: bool = False,
    github_client: GitHubAppClient | None = None,
) -> None:
    """Raise unless ``org_id`` may bind ``installation_id`` to itself.

    Args:
        org_id: The tenant the installation would be bound to.
        installation_id: The GitHub App installation id being claimed.
        db: Gateway database session.
        attest: Forwarded to ·A0's resolver. When True, an existing claim is
            additionally confirmed against GitHub. Costs one HTTP call.
        github_client: Required when ``attest=True``.

    Raises:
        InstallationClaimError: with ``status_code`` set (409 for a cross-tenant
            or disputed claim, 403 for one that cannot be verified).
    """
    owner, state = await resolve_installation_owner(
        installation_id,
        db=db,
        attest=attest,
        github_client=github_client,
    )

    if state is OwnerState.REVOKED:
        raise InstallationClaimError(
            f"Installation {installation_id} was revoked. Explicit operator restoration is required.",
            status_code=409,
            state=state,
            installation_id=installation_id,
            org_id=org_id,
        )

    if state is OwnerState.NOT_FOUND:
        # Nobody claims it — this write is the first claim. Allowed.
        return

    if state is OwnerState.RESOLVED and owner is not None:
        if owner.tenant_id == org_id:
            # Already ours: idempotent re-confirmation.
            return
        logger.warning(
            "event=installation_claim_denied installation_id=%s attempted_org=%s owning_org=%s reason=owned_by_another_tenant",
            installation_id,
            org_id,
            owner.tenant_id,
        )
        raise InstallationClaimError(
            f"Installation {installation_id} is already connected to another workspace.",
            status_code=409,
            state=state,
            installation_id=installation_id,
            org_id=org_id,
        )

    if state is OwnerState.AMBIGUOUS:
        logger.warning(
            "event=installation_claim_denied installation_id=%s attempted_org=%s reason=ownership_disputed",
            installation_id,
            org_id,
        )
        raise InstallationClaimError(
            f"Ownership of installation {installation_id} is disputed and must be resolved by an operator.",
            status_code=409,
            state=state,
            installation_id=installation_id,
            org_id=org_id,
        )

    # UNATTESTABLE: a claim exists but could not be substantiated (self-asserted
    # with no server-written row, no github_org_id to attest against, or GitHub
    # was unreachable). Refusing is the fail-closed half of ·A0's contract — a
    # claim nobody can vouch for must not become a binding.
    logger.warning(
        "event=installation_claim_denied installation_id=%s attempted_org=%s reason=unattestable",
        installation_id,
        org_id,
    )
    raise InstallationClaimError(
        f"Ownership of installation {installation_id} could not be verified.",
        status_code=403,
        state=state,
        installation_id=installation_id,
        org_id=org_id,
    )


async def assert_new_installation_ids_claimable_by(
    org_id: str,
    *,
    new_ids: list[str],
    old_ids: list[str],
    db: AsyncSession,
) -> None:
    """Guard the admin org-update writers that accept a caller-supplied id list.

    Issue #4072 (#11): both ``PUT /admin/organizations/{org_id}`` and
    ``PATCH /api/admin/identity/organizations/{org_id}`` assign
    ``github_installation_ids`` verbatim from the request body. Neither checked
    that the ids belong to the target org, so an org-admin could claim a victim's
    installation and thereby receive the victim's webhook events, agent runs and
    credential context.

    Only ids that are **newly added** are checked. Re-submitting an id the org
    already holds must stay possible even when that pre-existing claim is in a
    state ·A0 fails closed on (e.g. a self-asserted id with no corroborating
    ``channel_tenant_map`` row) — otherwise one bad legacy entry would make every
    unrelated update to that org permanently fail, turning a remediation into an
    outage.

    Non-numeric entries are skipped: a GitHub installation id is always numeric,
    so a non-numeric string cannot name a real installation and cannot collide
    with a victim's id in either store or in webhook routing. Enforcing on them
    would only reject test/placeholder data while closing nothing.

    Raises:
        InstallationClaimError: on the first id the org may not claim.
    """
    added = [i for i in new_ids if i not in set(old_ids)]
    for raw in added:
        try:
            installation_id = int(str(raw).strip())
        except (TypeError, ValueError):
            logger.info(
                "event=installation_claim_skipped org=%s value=%r reason=not_a_numeric_installation_id",
                org_id,
                raw,
            )
            continue
        await assert_installation_claimable_by(org_id, installation_id, db=db)


async def lock_installation_organization(db: AsyncSession, org_id: str) -> Organization | None:
    """Serialize binding/list changes using the database's current organization.

    FOR UPDATE alone does not refresh an ORM object loaded by earlier authority
    resolution. Every lifecycle writer must derive its list after this refresh.
    Call before claim guards and retain the transaction until mutation commits.
    """
    return await db.scalar(select(Organization).where(Organization.id == org_id).with_for_update().execution_options(populate_existing=True))
