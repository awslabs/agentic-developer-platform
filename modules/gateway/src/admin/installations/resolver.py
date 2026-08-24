"""Canonical GitHub App installation -> owning ADP tenant resolver.

Issue #4070 (sub-EPIC #4068, child ·A0 — Wave 0, foundational).

Why this module exists
----------------------
Two parts of the codebase used to record installation ownership in two
different places with no shared rule and no uniqueness guarantee, so two
tenants could hold the same installation. Every downstream "who may touch this
installation" decision reads that record, and the disagreement is what made the
cross-tenant install-takeover and token-confusion defects possible. Children
·A (#4071) and ·B (#4072) both consume this module so that they are fixed
against ONE notion of ownership rather than two.

Ownership is TWO NAMED LAYERS — do not conflate them:

1. **Map lookup** (fast, no network) — installation -> owning tenant, read from
   Postgres. This is what ``attest=False`` does.
2. **GitHub attestation** (network) — the installation's GitHub ``account.id``
   must equal the claimed owner's ``organizations.github_org_id``. This is what
   ``attest=True`` adds.

Layer 2 is not optional decoration. The pre-existing ownership check
(``verify_installation_ownership``) consulted *only* ``channel_tenant_map`` —
i.e. the very table the #4072/#5 attack corrupts. An ownership oracle whose
only source is the table under attack cannot answer the ownership question, so
binding writes must attest against GitHub, which is the sole authority on who
actually installed the App.

Not all claims carry the same weight
------------------------------------
Two stores record ownership, and they are **not** equally trustworthy:

* ``channel_tenant_map.installation_id`` is written only by server-side install
  callbacks, from data GitHub gave us. Call these claims *corroborated*.
* ``organizations.github_installation_ids`` is a plain client-supplied list on
  ``OrganizationUpdateRequest`` with no validator and no length bound. An
  ``org_admin`` — a role a user self-serves into merely by installing the App —
  may set it on their own tenant via ``PUT /admin/organizations/{org_id}``.

So an org-JSON claim is an *assertion by a tenant about itself*. Unioning the
two sets into one undifferentiated claim set would let anyone mint ownership of
any installation by naming it, which is the exact defect this module exists to
close. Instead the two sets are kept apart:

* A claim may only **grant** ownership on the no-network path if it is
  corroborated by ``channel_tenant_map``.
* An org-JSON-only claim still **counts for AMBIGUOUS detection** — poisoning
  the field can therefore cause a denial, never an authorization — and it may
  still resolve on the ``attest=True`` path, because there GitHub, not the
  claimant, is the one being believed.

This asymmetry is deliberate: an attacker-writable field keeps its power to
*deny* (it must, or the backfill/dedup would be wrong) and loses its power to
*grant*.

Fail-closed contract
--------------------
``assert_installation_owned_by`` raises on ``NOT_FOUND``, ``AMBIGUOUS`` and
``UNATTESTABLE`` — never only on an explicit mismatch. An installation nobody
can vouch for is not an installation you may act on.

Structural guarantee
--------------------
``resolve_installation_owner`` takes **no caller/tenant argument**. A function
handed a ``tenant_id`` is a *check*, not a resolver — and "resolver returns the
caller's tenant instead of the owning tenant" was listed as a way this work
could ship broken. Not accepting the caller's identity makes that failure mode
unreachable by construction rather than by reviewer discipline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from src.shared.models.organization import Organization
from src.shared.models.vault import ChannelTenantMap, InstallationOwnershipConflict

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.admin.connections.github_client import GitHubAppClient

logger = logging.getLogger(__name__)


class OwnerState(StrEnum):
    """Outcome of an installation -> tenant resolution.

    Mirrors the ``resolved | not_found | error`` tri-state vocabulary #4046
    introduced on ``resolve_installation_by_id`` in the webhook-ingress identity
    resolver — deliberately reusing that convention rather than inventing a
    second one for the same concept.
    """

    RESOLVED = "resolved"
    NOT_FOUND = "not_found"
    #: More than one tenant claims this installation. Fail CLOSED — this is the
    #: pre-migration duplicate state and the quarantined state, and guessing a
    #: winner would silently re-home a customer.
    AMBIGUOUS = "ambiguous"
    #: The owning tenant has no ``github_org_id``, so GitHub attestation is
    #: impossible for it. Fail CLOSED on binding writes. Reachable because
    #: ``organizations.github_org_id`` is nullable (migration 020) — tenants
    #: predating #2952 have it NULL.
    UNATTESTABLE = "unattestable"


@dataclass(frozen=True)
class InstallationOwner:
    """The tenant that owns an installation, plus how strongly that is known."""

    tenant_id: str
    installation_id: int
    #: ``organizations.github_org_id`` of the owning tenant. None => unattestable.
    github_account_id: str | None
    #: True ONLY when confirmed against GitHub. Never set true by a map lookup —
    #: a caller that requires attestation must be able to trust this flag.
    attested: bool


class InstallationOwnershipError(Exception):
    """Raised when a tenant does not provably own an installation.

    Carries ``state`` so callers can map the outcome onto an HTTP status without
    re-deriving it: a genuine cross-tenant claim is a 409 (conflict), everything
    else a caller may not act on is a 403.
    """

    def __init__(self, message: str, *, state: OwnerState, installation_id: int, tenant_id: str) -> None:
        super().__init__(message)
        self.state = state
        self.installation_id = installation_id
        self.tenant_id = tenant_id


def _claims_from_org_json(installation_id: int, orgs: list[Organization]) -> set[str]:
    """Tenants claiming ``installation_id`` via ``organizations.github_installation_ids``.

    Matched in Python rather than with ``:iid = ANY(...)`` / ``->>`` so the one
    code path works against both the Postgres JSONB column (GIN-indexed,
    migration 005) and the SQLite JSON column the test suite uses. This is the
    documented in-repo pattern from ``internal/routes.py`` ``resolve_installation``;
    the org set per account is small. A raw ``->>`` here would make this module
    Postgres-only and therefore untestable by the suite, which is what pushed an
    earlier ownership test into asserting on a SQL string instead of a behaviour.
    """
    needle = str(installation_id)
    return {org.id for org in orgs if needle in [str(i) for i in (org.github_installation_ids or [])]}


async def resolve_installation_owner(
    installation_id: int,
    *,
    db: AsyncSession,
    attest: bool = False,
    github_client: GitHubAppClient | None = None,
) -> tuple[InstallationOwner | None, OwnerState]:
    """Canonical installation -> owning tenant. Takes NO caller identity.

    Unions both records of ownership — ``channel_tenant_map.installation_id`` and
    ``organizations.github_installation_ids`` — because until migration 026's
    backfill has run everywhere, either may be the only one populated, and a
    resolver that reads just one would report ``NOT_FOUND`` for a live install.

    Args:
        installation_id: The GitHub App installation id.
        db: Gateway database session.
        attest: When True, additionally confirm against GitHub that the
            installation's account really is the resolved tenant's. Costs one
            HTTP call. Use on writes that BIND an installation to a tenant; skip
            on hot read paths.
        github_client: Client used when ``attest=True``. Required in that case;
            passed in rather than constructed here so this module needs no
            knowledge of where App credentials live.

    Returns:
        ``(owner, state)``. ``owner`` is None for every state except
        ``RESOLVED``. On ``UNATTESTABLE`` the owner is None because the claim
        could not be substantiated — callers must not fall back to the map
        answer, which is exactly the trust this layer exists to withhold.
    """
    scope_id = str(installation_id)

    # A quarantined installation is ambiguous by definition: migration 026 found
    # more than one tenant claiming it and deliberately did not pick a winner.
    # Checked FIRST so an operator-visible conflict can never be resolved past.
    quarantined = (
        await db.execute(
            select(InstallationOwnershipConflict.org_id).where(
                InstallationOwnershipConflict.installation_id == scope_id,
                InstallationOwnershipConflict.resolved_at.is_(None),
            )
        )
    ).all()
    if quarantined:
        logger.warning(
            "installation %s is quarantined as cross-tenant ambiguous (%d claims) — failing closed",
            scope_id,
            len(quarantined),
        )
        return None, OwnerState.AMBIGUOUS

    # Corroborated claims: written server-side by the install callbacks from data
    # GitHub supplied. These are the only claims allowed to GRANT ownership
    # without a network attestation.
    corroborated: set[str] = set(
        (
            await db.execute(
                select(ChannelTenantMap.org_id).where(
                    ChannelTenantMap.provider == "github",
                    ChannelTenantMap.installation_id == scope_id,
                )
            )
        )
        .scalars()
        .all()
    )

    # Self-asserted claims: client-writable via PUT/PATCH on the org (see the
    # "Not all claims carry the same weight" note above). Counted for ambiguity,
    # never trusted on its own to grant.
    orgs = list((await db.execute(select(Organization))).scalars().all())
    self_asserted: set[str] = _claims_from_org_json(installation_id, orgs)

    claims = corroborated | self_asserted

    if not claims:
        return None, OwnerState.NOT_FOUND

    if len(claims) > 1:
        # The defect this whole child exists to close: two tenants holding one
        # installation. Never guess — the caller must be denied and an operator
        # must resolve it.
        logger.warning(
            "installation %s claimed by %d tenants (%s) — failing closed as AMBIGUOUS",
            scope_id,
            len(claims),
            sorted(claims),
        )
        return None, OwnerState.AMBIGUOUS

    tenant_id = next(iter(claims))
    owning_org = next((o for o in orgs if o.id == tenant_id), None)
    github_account_id = owning_org.github_org_id if owning_org is not None else None

    if not attest:
        if tenant_id not in corroborated:
            # The sole claim is the tenant's own assertion about itself, with no
            # server-written channel_tenant_map row behind it. Believing it here
            # would let an org_admin take ownership of any installation nobody
            # else has claimed simply by naming it in github_installation_ids —
            # they self-serve into that role by installing the App. Only GitHub
            # can upgrade a self-assertion into ownership, so require attest=True.
            logger.warning(
                "installation %s is claimed ONLY by tenant %s via self-asserted "
                "github_installation_ids with no channel_tenant_map row — refusing to "
                "grant on the unattested path; re-check with attest=True",
                scope_id,
                tenant_id,
            )
            return None, OwnerState.UNATTESTABLE

        return (
            InstallationOwner(
                tenant_id=tenant_id,
                installation_id=installation_id,
                github_account_id=github_account_id,
                attested=False,
            ),
            OwnerState.RESOLVED,
        )

    # --- Layer 2: GitHub attestation ---------------------------------------
    if not github_account_id:
        # Nullable since migration 020; tenants predating #2952 have it NULL.
        # We cannot attest them, so we refuse to vouch for them rather than
        # silently downgrading to the map answer.
        logger.warning(
            "installation %s resolves to tenant %s which has no github_org_id — UNATTESTABLE, failing closed",
            scope_id,
            tenant_id,
        )
        return None, OwnerState.UNATTESTABLE

    if github_client is None:
        raise ValueError("attest=True requires a github_client")

    try:
        installation: dict[str, Any] = await github_client.get_installation(installation_id)
    except Exception as exc:
        # A GitHub outage must not become an authorization bypass.
        logger.warning("attestation for installation %s failed (%s) — failing closed as UNATTESTABLE", scope_id, exc)
        return None, OwnerState.UNATTESTABLE

    actual_account_id = (installation.get("account") or {}).get("id")
    if actual_account_id is None or str(actual_account_id) != str(github_account_id):
        # Same comparison install_callback already makes when it resolves an org
        # by github_org_id — here it is load-bearing for authorization.
        logger.warning(
            "attestation MISMATCH for installation %s: GitHub account id=%s but tenant %s claims github_org_id=%s",
            scope_id,
            actual_account_id,
            tenant_id,
            github_account_id,
        )
        return None, OwnerState.AMBIGUOUS

    return (
        InstallationOwner(
            tenant_id=tenant_id,
            installation_id=installation_id,
            github_account_id=str(github_account_id),
            attested=True,
        ),
        OwnerState.RESOLVED,
    )


async def assert_installation_owned_by(
    tenant_id: str,
    installation_id: int,
    *,
    db: AsyncSession,
    attest: bool = False,
    github_client: GitHubAppClient | None = None,
) -> None:
    """Raise unless ``tenant_id`` provably owns ``installation_id``.

    The *check* built on the resolver. Fails closed on ``NOT_FOUND``,
    ``AMBIGUOUS`` and ``UNATTESTABLE`` — i.e. it raises unless a single tenant
    was resolved AND that tenant is the one asserted.

    Raises:
        InstallationOwnershipError: with ``.state`` set, so callers can render a
            409 for a genuine cross-tenant conflict and a 403 otherwise.
    """
    owner, state = await resolve_installation_owner(
        installation_id,
        db=db,
        attest=attest,
        github_client=github_client,
    )

    if state is OwnerState.RESOLVED and owner is not None and owner.tenant_id == tenant_id:
        return

    if state is OwnerState.RESOLVED:
        # Resolved cleanly, just not to the asserting tenant. The state stays
        # RESOLVED — it is NOT ambiguous, we know exactly who owns it — so a
        # caller seeing (raised, state=RESOLVED) knows this is the
        # someone-else-owns-it case and renders 409. Overloading AMBIGUOUS here
        # would erase the difference between "disputed" and "definitely theirs".
        message = f"Installation {installation_id} belongs to another tenant."
    elif state is OwnerState.NOT_FOUND:
        message = f"Installation {installation_id} is not registered to any tenant."
    elif state is OwnerState.AMBIGUOUS:
        message = f"Ownership of installation {installation_id} is disputed and must be resolved by an operator."
    else:
        message = f"Ownership of installation {installation_id} cannot be verified against GitHub."

    raise InstallationOwnershipError(
        message,
        state=state,
        installation_id=installation_id,
        tenant_id=tenant_id,
    )
