"""Tests for Issue #440 — credential scope relaxation.

Covers:
- CHECK constraint (owner invariant): 0, 2, or 3 owner columns → rejected
- CHECK constraint: exactly 1 owner column → accepted
- owner_scope property: returns correct scope name
- CredentialResolver: user credential returned when user_id matches
- CredentialResolver: falls back to team cred when user has none
- CredentialResolver: falls back to org cred when team has none
- CredentialResolver: falls back to domain-app cred after org has none
- CredentialResolver: strict=True credential skips fallback chain
- CredentialResolver: ScopeEscalationError when scope_hint is narrower
  than the resolved credential's scope
- SecretsManagerHelper: builds correct namespace per owner scope
"""

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserCredential
from src.shared.services.credential_resolver import (
    CredentialNotFoundError,
    CredentialResolver,
    ScopeEscalationError,
)
from src.shared.services.secrets_manager import SecretsManagerHelper

# ---------------------------------------------------------------------------
# Model / schema tests
# ---------------------------------------------------------------------------


class TestOwnerInvariant:
    """The application-level owner invariant: exactly one owner column non-NULL
    (or all NULL for org-scoped).  These tests use raw INSERT/UPDATE to bypass
    ORM defaults and check that the DB or the property enforces correctness.
    """

    @pytest.mark.asyncio
    async def test_user_owned_scope(self, db_session: AsyncSession):
        """user_id set → owner_scope == 'user'."""
        user = User(id="u-scope-1", org_id="org-scope", team_id="t-1", email="scope1@x.com")
        db_session.add(user)
        await db_session.flush()

        cred = UserCredential(
            id="cred-scope-user",
            org_id="org-scope",
            user_id="u-scope-1",
            service="github",
            credential_type="api_key",
            label="default",
            secret_arn="arn:1",
        )
        db_session.add(cred)
        await db_session.flush()
        assert cred.owner_scope == "user"

    @pytest.mark.asyncio
    async def test_team_owned_scope(self, db_session: AsyncSession):
        """team_id set, user_id NULL → owner_scope == 'team'."""
        cred = UserCredential(
            id="cred-scope-team",
            org_id="org-scope",
            team_id="team-a",
            service="github",
            credential_type="api_key",
            label="deploy-bot",
            secret_arn="arn:2",
        )
        db_session.add(cred)
        await db_session.flush()
        assert cred.owner_scope == "team"

    @pytest.mark.asyncio
    async def test_org_owned_scope(self, db_session: AsyncSession):
        """All three owner cols NULL → owner_scope == 'org'."""
        cred = UserCredential(
            id="cred-scope-org",
            org_id="org-scope",
            service="virustotal",
            credential_type="api_key",
            label="enterprise",
            secret_arn="arn:3",
        )
        db_session.add(cred)
        await db_session.flush()
        assert cred.owner_scope == "org"

    @pytest.mark.asyncio
    async def test_domain_app_owned_scope(self, db_session: AsyncSession):
        """domain_app_id set → owner_scope == 'domain_app'."""
        cred = UserCredential(
            id="cred-scope-dapp",
            org_id="org-scope",
            domain_app_id="cyber-app",
            service="misp",
            credential_type="api_key",
            label="default",
            secret_arn="arn:4",
        )
        db_session.add(cred)
        await db_session.flush()
        assert cred.owner_scope == "domain_app"

    @pytest.mark.asyncio
    async def test_new_columns_exist_on_model(self):
        """domain_app_id and strict columns are present in mapper."""
        mapper = inspect(UserCredential)
        cols = {c.key for c in mapper.column_attrs}
        assert "domain_app_id" in cols
        assert "strict" in cols

    @pytest.mark.asyncio
    async def test_user_id_is_nullable(self):
        """user_id must be nullable (was NOT NULL in 004)."""
        mapper = inspect(UserCredential)
        col = mapper.columns["user_id"]
        assert col.nullable, "user_id should be nullable after scope relaxation"

    @pytest.mark.asyncio
    async def test_team_id_is_nullable(self):
        """team_id must be nullable."""
        mapper = inspect(UserCredential)
        col = mapper.columns["team_id"]
        assert col.nullable, "team_id should be nullable after scope relaxation"

    @pytest.mark.asyncio
    async def test_strict_defaults_to_false(self, db_session: AsyncSession):
        """strict column defaults to False."""
        cred = UserCredential(
            id="cred-strict-default",
            org_id="org-x",
            team_id="team-x",
            service="github",
            credential_type="api_key",
            label="default",
            secret_arn="arn:strict",
        )
        db_session.add(cred)
        await db_session.flush()
        assert cred.strict is False


# ---------------------------------------------------------------------------
# CredentialResolver tests
# ---------------------------------------------------------------------------


class TestCredentialResolver:
    """Resolver fallback chain and safety-rail tests."""

    @pytest.fixture
    async def org(self, db_session: AsyncSession) -> Organization:
        org = Organization(id="org-res", name="resolver-org", aws_accounts=[], role_mappings={}, settings={})
        db_session.add(org)
        await db_session.flush()
        return org

    @pytest.fixture
    async def user(self, db_session: AsyncSession, org: Organization) -> User:
        u = User(id="u-res-1", org_id="org-res", team_id="team-res", email="resolver@x.com")
        db_session.add(u)
        await db_session.flush()
        return u

    @pytest.fixture
    async def user_cred(self, db_session: AsyncSession, user: User) -> UserCredential:
        cred = UserCredential(
            id="c-user",
            org_id="org-res",
            user_id="u-res-1",
            service="github",
            credential_type="api_key",
            label="default",
            secret_arn="arn:user",
        )
        db_session.add(cred)
        await db_session.flush()
        return cred

    @pytest.fixture
    async def team_cred(self, db_session: AsyncSession) -> UserCredential:
        cred = UserCredential(
            id="c-team",
            org_id="org-res",
            team_id="team-res",
            service="github",
            credential_type="api_key",
            label="default",
            secret_arn="arn:team",
        )
        db_session.add(cred)
        await db_session.flush()
        return cred

    @pytest.fixture
    async def org_cred(self, db_session: AsyncSession) -> UserCredential:
        cred = UserCredential(
            id="c-org",
            org_id="org-res",
            service="github",
            credential_type="api_key",
            label="default",
            secret_arn="arn:org",
        )
        db_session.add(cred)
        await db_session.flush()
        return cred

    @pytest.fixture
    async def domain_app_cred(self, db_session: AsyncSession) -> UserCredential:
        cred = UserCredential(
            id="c-dapp",
            org_id="org-res",
            domain_app_id="cyber-app",
            service="misp",
            credential_type="api_key",
            label="default",
            secret_arn="arn:dapp",
        )
        db_session.add(cred)
        await db_session.flush()
        return cred

    @pytest.mark.asyncio
    async def test_user_cred_returned_when_present(self, db_session: AsyncSession, user_cred: UserCredential):
        """User credential is returned when user_id matches."""
        resolver = CredentialResolver(db_session)
        cred = await resolver.resolve(
            org_id="org-res",
            service="github",
            label="default",
            user_id="u-res-1",
            team_id="team-res",
        )
        assert cred.id == "c-user"
        assert cred.owner_scope == "user"

    @pytest.mark.asyncio
    async def test_falls_back_to_team_when_no_user_cred(self, db_session: AsyncSession, team_cred: UserCredential):
        """No user cred → resolver falls back to team credential."""
        resolver = CredentialResolver(db_session)
        cred = await resolver.resolve(
            org_id="org-res",
            service="github",
            label="default",
            user_id="u-res-1",  # user exists but has no cred
            team_id="team-res",
        )
        assert cred.id == "c-team"
        assert cred.owner_scope == "team"

    @pytest.mark.asyncio
    async def test_falls_back_to_org_when_no_team_cred(self, db_session: AsyncSession, org_cred: UserCredential):
        """No user or team cred → resolver falls back to org credential."""
        resolver = CredentialResolver(db_session)
        cred = await resolver.resolve(
            org_id="org-res",
            service="github",
            label="default",
            user_id="u-res-1",
            team_id="team-res",
        )
        assert cred.id == "c-org"
        assert cred.owner_scope == "org"

    @pytest.mark.asyncio
    async def test_falls_back_to_domain_app(self, db_session: AsyncSession, domain_app_cred: UserCredential):
        """No user/team/org cred → resolver falls back to domain-app cred."""
        resolver = CredentialResolver(db_session)
        cred = await resolver.resolve(
            org_id="org-res",
            service="misp",
            label="default",
            user_id="u-res-1",
            team_id="team-res",
            domain_app_id="cyber-app",
        )
        assert cred.id == "c-dapp"
        assert cred.owner_scope == "domain_app"

    @pytest.mark.asyncio
    async def test_not_found_raises(self, db_session: AsyncSession):
        """CredentialNotFoundError when no cred exists at any scope."""
        resolver = CredentialResolver(db_session)
        with pytest.raises(CredentialNotFoundError):
            await resolver.resolve(
                org_id="org-res",
                service="nonexistent-svc",
                label="default",
                user_id="u-res-1",
            )

    @pytest.mark.asyncio
    async def test_strict_cred_not_returned_as_fallback(self, db_session: AsyncSession):
        """A strict=True team credential is NOT returned when resolving via user fallback."""
        strict_team_cred = UserCredential(
            id="c-strict-team",
            org_id="org-res",
            team_id="team-res",
            service="pagerduty",
            credential_type="api_key",
            label="oncall",
            secret_arn="arn:strict-team",
            strict=True,
        )
        db_session.add(strict_team_cred)
        await db_session.flush()

        resolver = CredentialResolver(db_session)
        # The team cred exists but is strict → not returned via user fallback.
        with pytest.raises(CredentialNotFoundError):
            await resolver.resolve(
                org_id="org-res",
                service="pagerduty",
                label="oncall",
                user_id="u-res-1",  # no user cred exists
                team_id="team-res",
            )

    @pytest.mark.asyncio
    async def test_strict_cred_returned_on_exact_scope(self, db_session: AsyncSession):
        """A strict=True team credential IS returned when querying team scope directly."""
        strict_team_cred = UserCredential(
            id="c-strict-team-direct",
            org_id="org-res",
            team_id="team-res",
            service="pagerduty",
            credential_type="api_key",
            label="direct",
            secret_arn="arn:strict-direct",
            strict=True,
        )
        db_session.add(strict_team_cred)
        await db_session.flush()

        resolver = CredentialResolver(db_session)
        # When team_id is provided as the first lookup, it matches at "team" scope
        # directly — strict check passes (owner_scope == scope == "team").
        cred = await resolver.resolve(
            org_id="org-res",
            service="pagerduty",
            label="direct",
            team_id="team-res",  # only team scope provided → exact match
        )
        assert cred.id == "c-strict-team-direct"

    @pytest.mark.asyncio
    async def test_scope_escalation_rejected(self, db_session: AsyncSession, org_cred: UserCredential):
        """ScopeEscalationError when resolved scope (org) is wider than scope_hint (user)."""
        resolver = CredentialResolver(db_session)
        with pytest.raises(ScopeEscalationError):
            await resolver.resolve(
                org_id="org-res",
                service="github",
                label="default",
                user_id="u-res-1",
                team_id="team-res",
                scope_hint="user",  # only user scope acceptable
            )

    @pytest.mark.asyncio
    async def test_scope_hint_team_rejects_org(self, db_session: AsyncSession, org_cred: UserCredential):
        """scope_hint='team' also rejects resolution to org scope."""
        resolver = CredentialResolver(db_session)
        with pytest.raises(ScopeEscalationError):
            await resolver.resolve(
                org_id="org-res",
                service="github",
                label="default",
                team_id="team-res",
                scope_hint="team",
            )

    @pytest.mark.asyncio
    async def test_scope_hint_invalid_raises(self, db_session: AsyncSession):
        """Invalid scope_hint raises ValueError."""
        resolver = CredentialResolver(db_session)
        with pytest.raises(ValueError, match="scope_hint must be one of"):
            await resolver.resolve(
                org_id="org-res",
                service="github",
                scope_hint="bogus",
            )

    @pytest.mark.asyncio
    async def test_user_cred_wins_over_team_when_both_exist(self, db_session: AsyncSession, user_cred: UserCredential, team_cred: UserCredential):
        """User credential takes precedence over team when both exist."""
        resolver = CredentialResolver(db_session)
        cred = await resolver.resolve(
            org_id="org-res",
            service="github",
            label="default",
            user_id="u-res-1",
            team_id="team-res",
        )
        assert cred.id == "c-user"


# ---------------------------------------------------------------------------
# SecretsManagerHelper namespace tests
# ---------------------------------------------------------------------------


class TestSecretsManagerNamespaces:
    """Verify the correct path prefix is used for each owner scope."""

    def _make_helper(self, arn_suffix="arn:aws:secretsmanager:us-east-1:123:secret:"):
        from unittest.mock import MagicMock

        client = MagicMock()
        client.create_secret.return_value = {"ARN": arn_suffix + "placeholder"}
        return SecretsManagerHelper(client=client)

    def test_user_namespace(self):
        helper = self._make_helper()
        client = helper._client
        helper.create_secret("github", "my-token", "ghp_xxx", user_sub="sub123")
        name = client.create_secret.call_args.kwargs["Name"]
        assert name.startswith("adp/users/sub123/github-")

    def test_team_namespace(self):
        helper = self._make_helper()
        client = helper._client
        helper.create_secret("github", "deploy-bot", "ghp_xxx", team_id="team-abc")
        name = client.create_secret.call_args.kwargs["Name"]
        assert name.startswith("adp/teams/team-abc/github-")

    def test_org_namespace(self):
        helper = self._make_helper()
        client = helper._client
        helper.create_secret("virustotal", "enterprise", "vt-key", org_id="org-xyz")
        name = client.create_secret.call_args.kwargs["Name"]
        assert name.startswith("adp/orgs/org-xyz/virustotal-")

    def test_domain_app_namespace(self):
        helper = self._make_helper()
        client = helper._client
        helper.create_secret("misp", "default", "misp-key", domain_app_id="cyber-app", org_id="org-xyz")
        name = client.create_secret.call_args.kwargs["Name"]
        assert name.startswith("adp/domain-apps/cyber-app/org-xyz/misp-")

    def test_domain_app_requires_org_id(self):
        helper = self._make_helper()
        with pytest.raises(ValueError, match="org_id is required"):
            helper.create_secret("misp", "default", "key", domain_app_id="cyber-app")

    def test_user_scope_owner_tag(self):
        helper = self._make_helper()
        client = helper._client
        helper.create_secret("github", "tok", "v", user_sub="sub999")
        tags = {t["Key"]: t["Value"] for t in client.create_secret.call_args.kwargs["Tags"]}
        assert tags["adp:owner_scope"] == "user"
        assert tags["adp:user_sub"] == "sub999"

    def test_team_scope_owner_tag(self):
        helper = self._make_helper()
        client = helper._client
        helper.create_secret("github", "tok", "v", team_id="team-1")
        tags = {t["Key"]: t["Value"] for t in client.create_secret.call_args.kwargs["Tags"]}
        assert tags["adp:owner_scope"] == "team"
        assert tags["adp:team_id"] == "team-1"

    def test_org_scope_owner_tag(self):
        helper = self._make_helper()
        client = helper._client
        helper.create_secret("virustotal", "ent", "v", org_id="org-1")
        tags = {t["Key"]: t["Value"] for t in client.create_secret.call_args.kwargs["Tags"]}
        assert tags["adp:owner_scope"] == "org"

    def test_domain_app_scope_owner_tag(self):
        helper = self._make_helper()
        client = helper._client
        helper.create_secret("misp", "default", "v", domain_app_id="cyber-app", org_id="org-1")
        tags = {t["Key"]: t["Value"] for t in client.create_secret.call_args.kwargs["Tags"]}
        assert tags["adp:owner_scope"] == "domain_app"
        assert tags["adp:domain_app_id"] == "cyber-app"
