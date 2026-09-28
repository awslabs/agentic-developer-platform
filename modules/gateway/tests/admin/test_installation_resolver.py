"""Tests for the canonical installation -> tenant resolver.

Issue #4070 (sub-EPIC #4068 ·A0).

Gate discipline (sub-EPIC #4068): every regression test here is written to FAIL
on pre-fix code, and asserts the ACCESS DECISION — never the plumbing. In
particular NO test asserts on a SQL string. An earlier ownership test did
exactly that::

    assert "metadata->>'installation_id'" in query_text

which passes with the authorization check deleted, and is the #4046 failure mode
this sub-EPIC exists to reject. Asserting the SQL also forced the production
query to stay Postgres-only, which is why it could not be tested against the
SQLite suite in the first place.
"""

from unittest.mock import AsyncMock

import pytest

from src.admin.installations.resolver import (
    InstallationOwnershipError,
    OwnerState,
    assert_installation_owned_by,
    resolve_installation_owner,
)
from src.shared.models.organization import Organization
from src.shared.models.vault import ChannelTenantMap, InstallationOwnershipConflict

INSTALL_X = 5550001


async def _mk_org(db, org_id: str, *, github_org_id: str | None = None, installation_ids: list[str] | None = None) -> Organization:
    org = Organization(
        id=org_id,
        name=f"Org {org_id}",
        github_org_id=github_org_id,
        github_installation_ids=installation_ids or [],
    )
    db.add(org)
    await db.commit()
    return org


async def _mk_mapping(db, org_id: str, installation_id: int, *, scope_id: str | None = None) -> ChannelTenantMap:
    mapping = ChannelTenantMap(
        provider="github",
        provider_scope_id=scope_id or f"acct-{org_id}",
        installation_id=str(installation_id),
        org_id=org_id,
    )
    db.add(mapping)
    await db.commit()
    return mapping


class TestCrossTenantAmbiguity:
    """Regression: two tenants must never both hold one installation."""

    @pytest.mark.asyncio
    async def test_two_orgs_claiming_one_installation_is_ambiguous(self, db_session):
        """The exact pre-fix state: one claim per representation, disjoint keyspaces.

        Org A holds the installation via ``channel_tenant_map``; org B holds it
        via ``organizations.github_installation_ids``. Pre-fix these never
        collided (one column carried a GitHub ACCOUNT id, the other an
        INSTALLATION id) so both rows coexisted happily and the old ownership
        check returned True for whichever tenant happened to ask.
        """
        await _mk_org(db_session, "org-a", github_org_id="1111")
        await _mk_org(db_session, "org-b", github_org_id="2222", installation_ids=[str(INSTALL_X)])
        await _mk_mapping(db_session, "org-a", INSTALL_X)

        owner, state = await resolve_installation_owner(INSTALL_X, db=db_session)

        assert state is OwnerState.AMBIGUOUS
        assert owner is None, "a disputed installation must not resolve to an owner"

    @pytest.mark.asyncio
    async def test_ambiguous_installation_is_denied_to_both_claimants(self, db_session):
        """Fail CLOSED: neither claimant gets access while ownership is disputed.

        This is the security assertion. Pre-fix, the ownership check answered
        True for both tenants — the cross-tenant hole. Denying BOTH is correct:
        guessing a winner would silently re-home a customer.
        """
        await _mk_org(db_session, "org-a", github_org_id="1111")
        await _mk_org(db_session, "org-b", github_org_id="2222", installation_ids=[str(INSTALL_X)])
        await _mk_mapping(db_session, "org-a", INSTALL_X)

        for claimant in ("org-a", "org-b"):
            with pytest.raises(InstallationOwnershipError) as exc:
                await assert_installation_owned_by(claimant, INSTALL_X, db=db_session)
            assert exc.value.state is OwnerState.AMBIGUOUS

    @pytest.mark.asyncio
    async def test_quarantined_installation_is_ambiguous(self, db_session):
        """A conflict quarantined by migration 026 keeps failing closed.

        Ownership stays denied until an operator settles it with
        scripts/resolve_installation_conflicts.py — the migration deliberately
        did not pick a winner.
        """
        await _mk_org(db_session, "org-a", github_org_id="1111")
        await _mk_mapping(db_session, "org-a", INSTALL_X)
        db_session.add(InstallationOwnershipConflict(installation_id=str(INSTALL_X), org_id="org-a", source="channel_tenant_map"))
        db_session.add(InstallationOwnershipConflict(installation_id=str(INSTALL_X), org_id="org-b", source="organizations.github_installation_ids"))
        await db_session.commit()

        owner, state = await resolve_installation_owner(INSTALL_X, db=db_session)

        assert state is OwnerState.AMBIGUOUS
        assert owner is None


class TestReturnsOwningTenantNotCaller:
    """Regression: the resolver must return the OWNING tenant, not the caller's."""

    @pytest.mark.asyncio
    async def test_resolver_returns_owning_tenant_regardless_of_who_asks(self, db_session):
        """Installation owned by B resolves to B even though A is asking.

        The issue's own blast-radius table lists "resolver returns the caller's
        tenant" as a way this ships broken and a no-op ownership check. The
        signature makes it unreachable: there is no caller argument to return.
        """
        await _mk_org(db_session, "org-a", github_org_id="1111")
        await _mk_org(db_session, "org-b", github_org_id="2222")
        await _mk_mapping(db_session, "org-b", INSTALL_X)

        owner, state = await resolve_installation_owner(INSTALL_X, db=db_session)

        assert state is OwnerState.RESOLVED
        assert owner is not None
        assert owner.tenant_id == "org-b"

    @pytest.mark.asyncio
    async def test_resolver_signature_accepts_no_caller_identity(self):
        """Structural proof, not a behavioural one.

        A resolver that cannot be *told* who is asking cannot answer with who is
        asking. Asserting on the signature keeps a later refactor from
        reintroducing a caller/tenant parameter and quietly turning the resolver
        back into a check.
        """
        import inspect

        params = set(inspect.signature(resolve_installation_owner).parameters)

        assert "tenant_id" not in params
        assert "caller_org_id" not in params
        assert "caller_user_id" not in params

    @pytest.mark.asyncio
    async def test_non_owner_is_denied_even_though_installation_resolves(self, db_session):
        """Org A is refused an installation that cleanly belongs to org B.

        State stays RESOLVED (we know exactly who owns it — it is not disputed),
        which is what lets a handler render 409 rather than a generic 403.
        """
        await _mk_org(db_session, "org-a", github_org_id="1111")
        await _mk_org(db_session, "org-b", github_org_id="2222")
        await _mk_mapping(db_session, "org-b", INSTALL_X)

        with pytest.raises(InstallationOwnershipError) as exc:
            await assert_installation_owned_by("org-a", INSTALL_X, db=db_session)

        assert exc.value.state is OwnerState.RESOLVED

        # ...and the real owner is still allowed.
        await assert_installation_owned_by("org-b", INSTALL_X, db=db_session)


class TestResolverStates:
    """Table-driven over the four resolver states."""

    @pytest.mark.asyncio
    async def test_resolved_for_sole_owner(self, db_session):
        await _mk_org(db_session, "org-a", github_org_id="1111")
        await _mk_mapping(db_session, "org-a", INSTALL_X)

        owner, state = await resolve_installation_owner(INSTALL_X, db=db_session)

        assert state is OwnerState.RESOLVED
        assert owner is not None and owner.tenant_id == "org-a"
        assert owner.attested is False, "a map lookup must never claim GitHub attestation"

    @pytest.mark.asyncio
    async def test_not_found_for_unknown_installation(self, db_session):
        await _mk_org(db_session, "org-a", github_org_id="1111")

        owner, state = await resolve_installation_owner(9999999, db=db_session)

        assert state is OwnerState.NOT_FOUND
        assert owner is None

    @pytest.mark.asyncio
    async def test_org_json_claim_alone_does_not_grant_on_unattested_path(self, db_session):
        """A tenant's self-assertion is not proof of ownership.

        ``organizations.github_installation_ids`` is client-writable by an
        ``org_admin`` on their own tenant, so a claim backed by nothing else is
        just an assertion about itself. It must not RESOLVE on the no-network
        path — otherwise naming an installation is enough to own it. GitHub is
        the only authority that can upgrade such a claim (see the attest=True
        case in TestSelfAssertedClaimCannotGrantOwnership).
        """
        await _mk_org(db_session, "org-a", github_org_id="1111", installation_ids=[str(INSTALL_X)])

        owner, state = await resolve_installation_owner(INSTALL_X, db=db_session)

        assert state is OwnerState.UNATTESTABLE
        assert owner is None

    @pytest.mark.asyncio
    async def test_same_tenant_in_both_representations_is_not_a_conflict(self, db_session):
        """One tenant, both representations — redundant, NOT ambiguous.

        Guards against an over-eager conflict rule that would deny a perfectly
        valid install just because it is recorded twice.
        """
        await _mk_org(db_session, "org-a", github_org_id="1111", installation_ids=[str(INSTALL_X)])
        await _mk_mapping(db_session, "org-a", INSTALL_X)

        owner, state = await resolve_installation_owner(INSTALL_X, db=db_session)

        assert state is OwnerState.RESOLVED
        assert owner is not None and owner.tenant_id == "org-a"

    @pytest.mark.asyncio
    async def test_unattestable_when_owning_tenant_has_no_github_org_id(self, db_session):
        """github_org_id is nullable (migration 020) — those tenants cannot be attested."""
        await _mk_org(db_session, "org-legacy", github_org_id=None)
        await _mk_mapping(db_session, "org-legacy", INSTALL_X)

        owner, state = await resolve_installation_owner(INSTALL_X, db=db_session, attest=True, github_client=AsyncMock())

        assert state is OwnerState.UNATTESTABLE
        assert owner is None

    @pytest.mark.asyncio
    async def test_assert_fails_closed_on_unattestable(self, db_session):
        """An owner we cannot vouch for is denied — never downgraded to the map answer."""
        await _mk_org(db_session, "org-legacy", github_org_id=None)
        await _mk_mapping(db_session, "org-legacy", INSTALL_X)

        with pytest.raises(InstallationOwnershipError) as exc:
            await assert_installation_owned_by("org-legacy", INSTALL_X, db=db_session, attest=True, github_client=AsyncMock())

        assert exc.value.state is OwnerState.UNATTESTABLE

    @pytest.mark.asyncio
    async def test_assert_fails_closed_on_not_found(self, db_session):
        with pytest.raises(InstallationOwnershipError) as exc:
            await assert_installation_owned_by("org-a", 9999999, db=db_session)

        assert exc.value.state is OwnerState.NOT_FOUND


class TestSelfAssertedClaimCannotGrantOwnership:
    """A client-writable field must never be able to MINT ownership.

    ``organizations.github_installation_ids`` is a plain unvalidated list on
    ``OrganizationUpdateRequest``; ``ORG_ADMIN`` holds ``ORG_UPDATE`` and the
    scope check only rejects a *different* target org, so an org_admin may set it
    on their own tenant — and a user self-serves into org_admin merely by
    installing the App. These tests pin the asymmetry that makes that harmless:
    the field may cause a DENIAL, never an AUTHORIZATION.
    """

    @pytest.mark.asyncio
    async def test_attacker_cannot_claim_an_uncontested_installation(self, db_session):
        """The takeover path: claim an installation nobody else recorded.

        Victim's install is not (yet) in channel_tenant_map — e.g. an install
        recorded without a nonce, or one predating the backfill. The attacker
        names it in their own org's github_installation_ids. If the resolver
        believed that, the attacker's tenant would become the sole claimant and
        ownership checks downstream would pass, letting a private repo be cloned
        into the attacker's scope.
        """
        await _mk_org(db_session, "org-attacker", github_org_id="6666", installation_ids=[str(INSTALL_X)])

        owner, state = await resolve_installation_owner(INSTALL_X, db=db_session)

        assert state is not OwnerState.RESOLVED, "self-asserted claim must not grant ownership"
        assert owner is None

    @pytest.mark.asyncio
    async def test_attacker_self_assertion_is_denied_by_the_ownership_check(self, db_session):
        """Same scenario stated as the access decision callers actually make."""
        await _mk_org(db_session, "org-attacker", github_org_id="6666", installation_ids=[str(INSTALL_X)])

        with pytest.raises(InstallationOwnershipError):
            await assert_installation_owned_by("org-attacker", INSTALL_X, db=db_session)

    @pytest.mark.asyncio
    async def test_attacker_cannot_steal_a_corroborated_installation(self, db_session):
        """Claiming a properly-recorded install must not transfer it.

        The legitimate owner has a channel_tenant_map row. The attacker asserts
        the same installation. The attacker must be denied; ambiguity here is the
        correct outcome because a human must settle a genuine dispute.
        """
        await _mk_org(db_session, "org-victim", github_org_id="1111")
        await _mk_mapping(db_session, "org-victim", INSTALL_X)
        await _mk_org(db_session, "org-attacker", github_org_id="6666", installation_ids=[str(INSTALL_X)])

        with pytest.raises(InstallationOwnershipError):
            await assert_installation_owned_by("org-attacker", INSTALL_X, db=db_session)

    @pytest.mark.asyncio
    async def test_self_assertion_still_counts_for_ambiguity_detection(self, db_session):
        """Losing the power to grant must not cost it the power to deny.

        If org-JSON claims were simply ignored, a poisoned claim against a
        corroborated install would leave the install looking cleanly owned and
        the conflict would go unnoticed by the dedup/backfill. It must surface.
        """
        await _mk_org(db_session, "org-victim", github_org_id="1111")
        await _mk_mapping(db_session, "org-victim", INSTALL_X)
        await _mk_org(db_session, "org-attacker", github_org_id="6666", installation_ids=[str(INSTALL_X)])

        owner, state = await resolve_installation_owner(INSTALL_X, db=db_session)

        assert state is OwnerState.AMBIGUOUS
        assert owner is None

    @pytest.mark.asyncio
    async def test_github_can_upgrade_a_self_asserted_claim(self, db_session):
        """attest=True may resolve an org-JSON-only claim — GitHub is the authority.

        This is the legitimate backfill case: the map row is missing, but GitHub
        confirms the installation really does belong to this tenant's account.
        Here we believe GitHub, not the claimant.
        """
        await _mk_org(db_session, "org-a", github_org_id="1111", installation_ids=[str(INSTALL_X)])
        client = AsyncMock()
        client.get_installation.return_value = {"account": {"id": 1111}}

        owner, state = await resolve_installation_owner(INSTALL_X, db=db_session, attest=True, github_client=client)

        assert state is OwnerState.RESOLVED
        assert owner is not None and owner.tenant_id == "org-a"
        assert owner.attested is True

    @pytest.mark.asyncio
    async def test_github_refuses_to_upgrade_a_forged_claim(self, db_session):
        """The attacker's own account id does not match the installation's."""
        await _mk_org(db_session, "org-attacker", github_org_id="6666", installation_ids=[str(INSTALL_X)])
        client = AsyncMock()
        client.get_installation.return_value = {"account": {"id": 1111}}  # victim's account

        with pytest.raises(InstallationOwnershipError):
            await assert_installation_owned_by("org-attacker", INSTALL_X, db=db_session, attest=True, github_client=client)


class TestPersonalInstallIsClaimed:
    """Personal installs must be recorded in the canonical column.

    Left NULL they are invisible to the resolver — i.e. unclaimed, and therefore
    mintable by any tenant asserting them (see the class above).
    """

    @pytest.mark.asyncio
    async def test_real_personal_install_flow_leaves_install_claimed(self, db_session):
        """Drive the ACTUAL writer, then ask the resolver who owns the install.

        Deliberately calls attach_to_adp_default rather than hand-building a row,
        so this fails if that writer stops populating the canonical column.
        Pre-fix it wrote install_metadata only, leaving installation_id NULL, and
        the resolver reported NOT_FOUND — an unclaimed, therefore claimable install.
        """
        from src.admin.connections.adp_default import attach_to_adp_default, get_adp_default_org_id

        adp_default_id = get_adp_default_org_id()
        await _mk_org(db_session, adp_default_id, github_org_id="7777")

        await attach_to_adp_default(
            installation_id=INSTALL_X,
            account_login="octocat",
            github_account_id=7777,
            caller_user_id="user-1",
            db=db_session,
        )

        owner, state = await resolve_installation_owner(INSTALL_X, db=db_session)

        assert state is OwnerState.RESOLVED, "a personal install must be claimed, not left unclaimed"
        assert owner is not None and owner.tenant_id == adp_default_id


class TestGitHubAttestation:
    """The GitHub layer must be load-bearing, not decorative."""

    @pytest.mark.asyncio
    async def test_attestation_mismatch_is_denied(self, db_session):
        """GitHub says the installation belongs to a different account ⇒ deny.

        This is the check the pre-fix code could not make at all: the old
        ownership function was a Postgres SELECT against the very table the
        sibling attack corrupts, so it had no independent oracle.
        """
        await _mk_org(db_session, "org-a", github_org_id="1111")
        await _mk_mapping(db_session, "org-a", INSTALL_X)

        client = AsyncMock()
        client.get_installation.return_value = {"account": {"id": 9999, "login": "someone-else"}}

        owner, state = await resolve_installation_owner(INSTALL_X, db=db_session, attest=True, github_client=client)

        assert state is OwnerState.AMBIGUOUS
        assert owner is None

        with pytest.raises(InstallationOwnershipError):
            await assert_installation_owned_by("org-a", INSTALL_X, db=db_session, attest=True, github_client=client)

    @pytest.mark.asyncio
    async def test_attestation_match_is_allowed_and_marked_attested(self, db_session):
        await _mk_org(db_session, "org-a", github_org_id="1111")
        await _mk_mapping(db_session, "org-a", INSTALL_X)

        client = AsyncMock()
        client.get_installation.return_value = {"account": {"id": 1111, "login": "org-a"}}

        owner, state = await resolve_installation_owner(INSTALL_X, db=db_session, attest=True, github_client=client)

        assert state is OwnerState.RESOLVED
        assert owner is not None and owner.attested is True
        await assert_installation_owned_by("org-a", INSTALL_X, db=db_session, attest=True, github_client=client)

    @pytest.mark.asyncio
    async def test_github_failure_fails_closed(self, db_session):
        """A GitHub outage must not become an authorization bypass."""
        await _mk_org(db_session, "org-a", github_org_id="1111")
        await _mk_mapping(db_session, "org-a", INSTALL_X)

        client = AsyncMock()
        client.get_installation.side_effect = RuntimeError("502 Bad Gateway")

        owner, state = await resolve_installation_owner(INSTALL_X, db=db_session, attest=True, github_client=client)

        assert state is OwnerState.UNATTESTABLE
        assert owner is None

    @pytest.mark.asyncio
    async def test_attest_false_makes_zero_github_calls(self, db_session):
        """Hot-path latency guard: the default path must not touch the network."""
        await _mk_org(db_session, "org-a", github_org_id="1111")
        await _mk_mapping(db_session, "org-a", INSTALL_X)

        client = AsyncMock()
        await resolve_installation_owner(INSTALL_X, db=db_session, attest=False, github_client=client)

        client.get_installation.assert_not_called()


class TestVerifyInstallationOwnershipDelegate:
    """The Wave-0 delegate keeps existing callers working, with one improvement."""

    @pytest.mark.asyncio
    async def test_owner_still_allowed(self, db_session):
        from src.knowledge.github_app_service import verify_installation_ownership

        await _mk_org(db_session, "org-a", github_org_id="1111")
        await _mk_mapping(db_session, "org-a", INSTALL_X)

        assert await verify_installation_ownership("org-a", INSTALL_X, db=db_session) is True

    @pytest.mark.asyncio
    async def test_non_owner_still_denied(self, db_session):
        from src.knowledge.github_app_service import verify_installation_ownership

        await _mk_org(db_session, "org-a", github_org_id="1111")
        await _mk_org(db_session, "org-b", github_org_id="2222")
        await _mk_mapping(db_session, "org-b", INSTALL_X)

        assert await verify_installation_ownership("org-a", INSTALL_X, db=db_session) is False

    @pytest.mark.asyncio
    async def test_disputed_installation_denied_to_everyone(self, db_session):
        """The behaviour change the shared rule buys: fail closed for BOTH claimants.

        Pre-fix this returned True for whichever tenant asked — the cross-tenant
        hole, reached through the Knowledge Layer's own ownership check.
        """
        from src.knowledge.github_app_service import verify_installation_ownership

        await _mk_org(db_session, "org-a", github_org_id="1111")
        await _mk_org(db_session, "org-b", github_org_id="2222", installation_ids=[str(INSTALL_X)])
        await _mk_mapping(db_session, "org-a", INSTALL_X)

        assert await verify_installation_ownership("org-a", INSTALL_X, db=db_session) is False
        assert await verify_installation_ownership("org-b", INSTALL_X, db=db_session) is False


class TestWritersAgree:
    """Both writers must land on the same column with the same value."""

    @pytest.mark.asyncio
    async def test_both_writers_use_installation_id_column_identically(self, db_session):
        """Regression for the disjoint-keyspace root cause.

        Pre-fix ``organizations_service`` wrote the INSTALLATION id into
        ``provider_scope_id`` while ``_attach_org_installation`` wrote the GitHub
        ACCOUNT id there. Two number spaces in one column is why the unique
        constraint never fired. Both must now write ``installation_id``.
        """
        from src.admin.connections.service import _attach_org_installation

        await _mk_org(db_session, "org-a", github_org_id="1111")
        await _mk_org(db_session, "org-b", github_org_id="2222")

        await _attach_org_installation(
            installation_id=INSTALL_X,
            github_org_id=1111,
            github_org_login="org-a-login",
            caller_org_id="org-a",
            db=db_session,
        )

        # The organizations_service representation for a different installation.
        other_install = INSTALL_X + 1
        db_session.add(
            ChannelTenantMap(
                provider="github",
                provider_scope_id=str(other_install),
                installation_id=str(other_install),
                org_id="org-b",
            )
        )
        await db_session.commit()

        # Both are now discoverable through the one canonical column.
        owner_a, state_a = await resolve_installation_owner(INSTALL_X, db=db_session)
        owner_b, state_b = await resolve_installation_owner(other_install, db=db_session)

        assert state_a is OwnerState.RESOLVED and owner_a is not None and owner_a.tenant_id == "org-a"
        assert state_b is OwnerState.RESOLVED and owner_b is not None and owner_b.tenant_id == "org-b"

    @pytest.mark.asyncio
    async def test_attach_preserves_account_scope_semantics(self, db_session):
        """provider_scope_id must still carry the ACCOUNT id, not the installation id.

        Guards the four existing provider_scope_id readers (the ``personal:``
        prefix scheme, the ``endswith(":user")`` filter, and the two connections
        lookups) against a well-meaning "reconcile the column" refactor.
        """
        from sqlalchemy import select

        from src.admin.connections.service import _attach_org_installation

        await _mk_org(db_session, "org-a", github_org_id="1111")
        await _attach_org_installation(
            installation_id=INSTALL_X,
            github_org_id=1111,
            github_org_login="org-a-login",
            caller_org_id="org-a",
            db=db_session,
        )

        row = (await db_session.execute(select(ChannelTenantMap).where(ChannelTenantMap.installation_id == str(INSTALL_X)))).scalar_one()

        assert row.provider_scope_id == "1111", "provider_scope_id is the GitHub account id"
        assert row.installation_id == str(INSTALL_X), "installation_id is the installation id"

    @pytest.mark.asyncio
    async def test_reinstall_updates_installation_id(self, db_session):
        """A new installation id for the same account must be picked up.

        Uninstall + reinstall issues a fresh installation id against the same
        GitHub account, so the row is matched by account scope and the
        installation column must be reassigned — not left at the stale value.
        """
        from src.admin.connections.service import _attach_org_installation

        await _mk_org(db_session, "org-a", github_org_id="1111")
        for install_id in (INSTALL_X, INSTALL_X + 99):
            await _attach_org_installation(
                installation_id=install_id,
                github_org_id=1111,
                github_org_login="org-a-login",
                caller_org_id="org-a",
                db=db_session,
            )

        owner, state = await resolve_installation_owner(INSTALL_X + 99, db=db_session)
        assert state is OwnerState.RESOLVED
        assert owner is not None and owner.tenant_id == "org-a"

        # The superseded id no longer resolves — one row, one current installation.
        _, stale_state = await resolve_installation_owner(INSTALL_X, db=db_session)
        assert stale_state is OwnerState.NOT_FOUND
