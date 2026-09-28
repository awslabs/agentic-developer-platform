"""Tests for POST /internal/v1/provenance.

Issue #785: Phase 2-b — action provenance write endpoint.

Business-rule tests use explicit synthetic verified run identities. Transport
negative tests retain the real dependency; canonical lookup and attribution
negative cases are in test_provenance_chain_attribution.py.

Coverage:
  - Valid request -> 201 + row inserted
  - Missing actor_user_id FK -> 400
  - Missing triggered_by FK -> 400
  - Missing root_human_id FK -> 400
  - Missing required fields -> 422
  - Missing/invalid auth -> 403
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.provenance_routes import router
from src.internal.run_identity import ServerRunIdentity
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.provenance import ActionProvenance

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_VALID_KEY = "test-internal-api-key"


def _make_engine():
    return create_async_engine(
        TEST_DB_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def engine():
    eng = _make_engine()
    async with eng.begin() as conn:
        import src.shared.models.audit  # noqa: F401
        import src.shared.models.provenance  # noqa: F401
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        org = Organization(
            id="org-test",
            name="Test Org",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
            cognito_client_ids=[],
        )
        dept = Department(id="dept-eng", org_id="org-test", name="Engineering")
        team = Team(id="team-eng", org_id="org-test", department_id="dept-eng", name="Eng")
        alice = User(
            id="user-alice",
            org_id="org-test",
            team_id="team-eng",
            email="alice@test.com",
        )
        bot = User(
            id="user-bot",
            org_id="org-test",
            team_id="team-eng",
            email="bot@test.com",
        )
        session.add_all([org, dept, team, alice, bot])
        await session.commit()
        yield session


def _make_app(db_session: AsyncSession, *, identity_body=None, authenticated=True) -> TestClient:
    """Build a minimal FastAPI test app with the provenance router."""
    app = FastAPI()
    app.include_router(router)
    app.state.verified_identity = _identity_for_body(identity_body or _valid_body())
    if authenticated:
        app.dependency_overrides[verify_internal_or_irsa] = lambda: None

    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app, raise_server_exceptions=False)


def _settings_mock() -> MagicMock:
    s = MagicMock()
    s.internal_api_key = _VALID_KEY
    return s


def _identity_for_body(body):
    """Explicit synthetic server fixture for downstream FK/policy tests only.

    The separate chain-attribution suite exercises the real canonical resolver.
    This value is prepared before requests, never inferred by production auth.
    """
    return ServerRunIdentity(
        "fixture-run",
        body["org_id"],
        body["actor_user_id"],
        body["correlation_id"],
        body["root_human_id"],
        body["is_human_rooted"],
        body.get("parent_invocation_id"),
        triggered_by=body.get("triggered_by"),
    )


@pytest.fixture(autouse=True)
def verified_business_identity(monkeypatch):
    async def identity(request):
        return request.app.state.verified_identity

    monkeypatch.setattr("src.internal.run_identity.verified_run_identity", identity)
    with mock_aws():  # CloudWatch policy metrics stay offline too.
        yield


@contextmanager
def _agreeing_chain(client, body: dict, *, monkeypatch):
    """Provide an explicit verified fixture to reach the downstream policy gate."""
    previous = client.app.state.verified_identity
    client.app.state.verified_identity = _identity_for_body(body)
    try:
        yield
    finally:
        client.app.state.verified_identity = previous


def _post_with_agreeing_chain(client, body: dict, *, monkeypatch):
    """`_agreeing_chain` + the plain authenticated POST, for the tests that need no
    extra patches."""
    with _agreeing_chain(client, body, monkeypatch=monkeypatch):
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            return client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )


def _valid_body() -> dict:
    return {
        "actor_user_id": "user-bot",
        "triggered_by": "user-alice",
        "root_human_id": "user-alice",
        "is_human_rooted": True,
        "action_kind": "issue_comment",
        "source_event": {"issue": 783, "comment_id": 12345},
        "correlation_id": "corr-abc123",
        "org_id": "org-test",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateProvenance:
    @pytest.mark.asyncio
    async def test_valid_request_returns_201(self, db):
        """Happy path: valid body -> 201 with id and created_at."""
        client = _make_app(db)
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=_valid_body(),
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert "created_at" in data

        # Verify row exists in DB
        result = await db.execute(select(ActionProvenance).where(ActionProvenance.id == data["id"]))
        row = result.scalar_one_or_none()
        assert row is not None
        assert row.actor_user_id == "user-bot"
        assert row.correlation_id == "corr-abc123"
        assert row.is_human_rooted is True

    @pytest.mark.asyncio
    async def test_invalid_actor_returns_400(self, db):
        """actor_user_id not found -> 400."""
        client = _make_app(db)
        body = _valid_body()
        body["actor_user_id"] = "nonexistent-user"
        client.app.state.verified_identity = _identity_for_body(body)
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 400
        assert "invalid_actor" in resp.json()["detail"]["error"]

    @pytest.mark.asyncio
    async def test_invalid_triggered_by_returns_400(self, db):
        """triggered_by references nonexistent user -> 400."""
        client = _make_app(db)
        body = _valid_body()
        body["triggered_by"] = "ghost-user"
        client.app.state.verified_identity = _identity_for_body(body)
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 400
        assert "invalid_triggered_by" in resp.json()["detail"]["error"]

    @pytest.mark.asyncio
    async def test_invalid_root_human_returns_400(self, db):
        """root_human_id not found -> 400."""
        client = _make_app(db)
        body = _valid_body()
        body["root_human_id"] = "no-such-user"
        client.app.state.verified_identity = _identity_for_body(body)
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 400
        assert "invalid_root_human" in resp.json()["detail"]["error"]

    @pytest.mark.asyncio
    async def test_null_triggered_by_is_valid(self, db):
        """triggered_by=null is accepted (initial human action)."""
        client = _make_app(db)
        body = _valid_body()
        body["triggered_by"] = None
        client.app.state.verified_identity = _identity_for_body(body)
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_missing_fields_returns_422(self, db):
        """Missing required fields -> 422 validation error."""
        client = _make_app(db)
        body = {"actor_user_id": "user-alice"}  # missing most fields
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_auth_returns_403(self, db):
        """No auth header -> 403."""
        client = _make_app(db, authenticated=False)
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=_valid_body(),
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_invalid_auth_returns_403(self, db):
        """Wrong API key -> 403."""
        client = _make_app(db, authenticated=False)
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=_valid_body(),
                headers={"X-Internal-Api-Key": "wrong-key"},
            )
        assert resp.status_code == 403


class TestActorOrgCompare:
    """Issue #3985 (A2): the actor must belong to the org the caller asserts.

    Without this compare, FK-existence of actor_user_id was the only gate, so any
    internal-plane caller could attribute an action to an arbitrary org_id and
    poison another tenant's audit trail.

    Issue #4029 narrowed the scope of that compare, deliberately. The gate now falls
    through to the target org's ``trigger_policy``, read with the SAME default as the
    webhook-ingress resolver (absent ⇒ ``any_adp_user``). So for an org on the
    implicit default the compare no longer constrains which ``org_id`` a caller may
    attribute to; it binds only when the org explicitly sets ``home_tenant_only``,
    or when the org does not exist.

    Why: this endpoint only *records* a run the resolver already permitted to
    execute. Requiring an explicit policy here did not prevent the cross-tenant
    action — it only dropped the audit row, exactly for the cross-tenant activity
    most worth auditing. The two tests below therefore pin the gate against an org
    with an EXPLICIT policy; ``test_unset_policy_permits_and_is_measured`` pins the
    permissive default so the posture change is visible rather than implicit.

    These are downstream policy tests. Canonical authenticated-run binding is
    independently exercised in test_provenance_chain_attribution.py.
    """

    @pytest.mark.asyncio
    async def test_actor_in_other_org_rejected(self, db, monkeypatch):
        """Actor in a different org, target org restricts triggering -> 403, nothing written."""
        other_org = Organization(
            id="org-other",
            name="Other Org",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
            cognito_client_ids=[],
        )
        # Explicit home_tenant_only: the target org has opted out of letting any ADP
        # user act in it, which is what makes the actor/org compare binding (#4029).
        closed_org = Organization(
            id="org-closed",
            name="Closed Org",
            aws_accounts=[],
            role_mappings={},
            settings={"trigger_policy": "home_tenant_only"},
            github_installation_ids=[],
            cognito_client_ids=[],
        )
        outsider = User(
            id="user-outsider",
            org_id="org-other",
            team_id="team-eng",
            email="outsider@other.com",
        )
        db.add_all([other_org, closed_org, outsider])
        await db.commit()

        body = _valid_body()
        body["actor_user_id"] = "user-outsider"  # actor is in org-other
        body["org_id"] = "org-closed"  # ...but caller claims org-closed

        # Supply a synthetic verified identity to isolate this downstream policy.
        resp = _post_with_agreeing_chain(_make_app(db), body, monkeypatch=monkeypatch)
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "actor_org_mismatch"

        rows = (await db.execute(select(ActionProvenance).where(ActionProvenance.actor_user_id == "user-outsider"))).scalars().all()
        assert rows == []

    @pytest.mark.asyncio
    async def test_membership_row_authorizes_actor(self, db):
        """An explicit tenant_memberships row is the authority when one exists."""
        member_org = Organization(
            id="org-member",
            name="Member Org",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
            cognito_client_ids=[],
        )
        db.add(member_org)
        db.add(
            TenantMembership(
                id="tm-bot-member",
                user_id="user-bot",
                tenant_id="org-member",
                role="member",
                is_active=True,
            )
        )
        await db.commit()

        body = _valid_body()
        body["org_id"] = "org-member"

        client = _make_app(db)
        client.app.state.verified_identity = _identity_for_body(body)
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_membership_rows_override_users_org_id(self, db, monkeypatch):
        """Once memberships exist they are authoritative; users.org_id is not consulted.

        Guards the fallback from becoming a bypass: an actor with a membership in
        org-member must NOT be able to write provenance for their legacy
        users.org_id value.

        #4029: asserted against a target org with an explicit ``home_tenant_only``,
        since an org on the permissive default is authorized by the policy fallback
        regardless of the membership/users.org_id precedence being tested here. The
        precedence rule itself is unchanged — this only isolates it from the fallback.
        """
        member_org = Organization(
            id="org-member2",
            name="Member Org 2",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
            cognito_client_ids=[],
        )
        legacy_org = Organization(
            id="org-legacy",
            name="Legacy Org",
            aws_accounts=[],
            role_mappings={},
            settings={"trigger_policy": "home_tenant_only"},
            github_installation_ids=[],
            cognito_client_ids=[],
        )
        legacy_bot = User(
            id="user-bot-legacy",
            org_id="org-legacy",
            team_id="team-eng",
            email="bot-legacy@test.com",
        )
        db.add_all([member_org, legacy_org, legacy_bot])
        db.add(
            TenantMembership(
                id="tm-bot-member2",
                user_id="user-bot-legacy",
                tenant_id="org-member2",
                role="member",
                is_active=True,
            )
        )
        await db.commit()

        body = _valid_body()
        body["actor_user_id"] = "user-bot-legacy"
        body["org_id"] = "org-legacy"  # the actor's users.org_id, but not a membership

        # Supply a synthetic verified identity to isolate this downstream policy.
        resp = _post_with_agreeing_chain(_make_app(db), body, monkeypatch=monkeypatch)
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "actor_org_mismatch"

    @pytest.mark.asyncio
    async def test_shadow_user_without_membership_falls_back_to_users_org_id(self, db):
        """Shadow users (POST /resolve-user) have no membership row and must still work."""
        body = _valid_body()  # user-bot has users.org_id == org-test, no memberships
        body["org_id"] = "org-test"

        client = _make_app(db)
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 201


class TestCrossTenantTriggerPolicy:
    """Issue #4029: the authority gate must not turn the fixed 422 into a silent 403.

    A run's tenant is the *repo/installation* org, and the webhook resolver permits a
    user whose home org is A to trigger on a repo in org B. Provenance for that run is
    attributed to B, which no membership row of the actor's covers — so the #3985 gate
    denies it. The target org's trigger_policy is what authorizes it, read with the
    same default as the resolver (absent ⇒ any_adp_user).
    """

    @staticmethod
    async def _seed_cross_tenant(db, *, org_id: str, settings: dict) -> None:
        """A repo-org the actor has NO Postgres relationship with."""
        db.add(
            Organization(
                id=org_id,
                name=f"Repo Org {org_id}",
                aws_accounts=[],
                role_mappings={},
                settings=settings,
                github_installation_ids=[],
                cognito_client_ids=[],
            )
        )
        await db.commit()

    @pytest.mark.asyncio
    async def test_explicit_any_adp_user_authorizes_cross_tenant_write(self, db, monkeypatch):
        """The flow the resolver permits must produce a row, not a 403."""
        await self._seed_cross_tenant(db, org_id="org-repo-open", settings={"trigger_policy": "any_adp_user"})

        body = _valid_body()
        body["org_id"] = "org-repo-open"  # actor user-bot's org is org-test

        # Supply a synthetic verified identity to isolate this downstream policy.
        client = _make_app(db)
        with _agreeing_chain(client, body, monkeypatch=monkeypatch):
            with (
                patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
                patch("src.internal.provenance_routes._emit_provenance_authority_metric") as metric,
            ):
                resp = client.post(
                    "/internal/v1/provenance",
                    json=body,
                    headers={"X-Internal-Api-Key": _VALID_KEY},
                )
        assert resp.status_code == 201

        # The row is attributed to the repo org, consistent with DDB/Activity.
        row = (await db.execute(select(ActionProvenance).where(ActionProvenance.id == resp.json()["id"]))).scalar_one()
        assert row.org_id == "org-repo-open"

        # And the rare cross-tenant attribution is measured, not just logged.
        metric.assert_called_once_with("CrossTenantProvenanceAllowed", "org-repo-open")

    @pytest.mark.asyncio
    async def test_home_tenant_only_still_fails_closed(self, db, monkeypatch):
        """#3985 must survive: an org that restricts triggering still 403s."""
        await self._seed_cross_tenant(db, org_id="org-repo-closed", settings={"trigger_policy": "home_tenant_only"})

        body = _valid_body()
        body["org_id"] = "org-repo-closed"

        # Supply a synthetic verified identity to isolate this downstream policy.
        resp = _post_with_agreeing_chain(_make_app(db), body, monkeypatch=monkeypatch)
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "actor_org_mismatch"

    @pytest.mark.asyncio
    async def test_unset_policy_permits_and_is_measured(self, db, monkeypatch):
        """An org with no explicit policy PERMITS — mirroring the ingress default.

        This is the case that matters most in practice: ``Organization.settings``
        defaults to ``{}``, so nearly every org reaches this branch. The ingress
        resolver treats an absent ``trigger_policy`` as ``any_adp_user`` and lets the
        run execute; if this endpoint denied the same configuration, the audit trail
        would get a hole exactly where cross-tenant activity happens.

        An earlier revision of #4029 required an *explicit* policy here and would have
        swapped the silent 422 for a silent 403 across the fleet. The lockstep test
        (test_provenance_policy_lockstep.py) now keeps the two defaults from drifting
        apart again.
        """
        await self._seed_cross_tenant(db, org_id="org-repo-default", settings={})

        body = _valid_body()
        body["org_id"] = "org-repo-default"

        client = _make_app(db)
        with _agreeing_chain(client, body, monkeypatch=monkeypatch):
            with (
                patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
                patch("src.internal.provenance_routes._emit_provenance_authority_metric") as metric,
            ):
                resp = client.post(
                    "/internal/v1/provenance",
                    json=body,
                    headers={"X-Internal-Api-Key": _VALID_KEY},
                )
        assert resp.status_code == 201

        # The row is attributed to the repo org, consistent with DDB/Activity.
        row = (await db.execute(select(ActionProvenance).where(ActionProvenance.id == resp.json()["id"]))).scalar_one()
        assert row.org_id == "org-repo-default"

        # Still measured — the cross-tenant grant is visible, not silent.
        metric.assert_called_once_with("CrossTenantProvenanceAllowed", "org-repo-default")

    @pytest.mark.asyncio
    async def test_unknown_org_denial_is_measured(self, db, monkeypatch):
        """The residual 403 must stay visible, or it hides the way the 422 did.

        With unset ⇒ permitted, unknown-org and explicit ``home_tenant_only`` are the
        only denial paths left, so this is where ProvenanceAuthorityDenied has to fire.
        """
        body = _valid_body()
        body["org_id"] = "org-nonexistent-measured"

        # Supply a synthetic verified identity to isolate this downstream policy.
        client = _make_app(db)
        with _agreeing_chain(client, body, monkeypatch=monkeypatch):
            with (
                patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
                patch("src.internal.provenance_routes._emit_provenance_authority_metric") as metric,
            ):
                resp = client.post(
                    "/internal/v1/provenance",
                    json=body,
                    headers={"X-Internal-Api-Key": _VALID_KEY},
                )
        assert resp.status_code == 403
        metric.assert_called_once_with("ProvenanceAuthorityDenied", "org-nonexistent-measured")

    @pytest.mark.asyncio
    async def test_unknown_org_is_rejected(self, db):
        """A policy lookup on a nonexistent org must not authorize anything."""
        body = _valid_body()
        body["org_id"] = "org-does-not-exist"

        client = _make_app(db)
        client.app.state.verified_identity = _identity_for_body(body)
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_membership_still_wins_without_consulting_policy(self, db):
        """A member is authorized on membership alone — policy is only the fallback."""
        await self._seed_cross_tenant(db, org_id="org-repo-member", settings={"trigger_policy": "home_tenant_only"})
        db.add(
            TenantMembership(
                id="tm-bot-repo-member",
                user_id="user-bot",
                tenant_id="org-repo-member",
                role="member",
                is_active=True,
            )
        )
        await db.commit()

        body = _valid_body()
        body["org_id"] = "org-repo-member"

        client = _make_app(db)
        client.app.state.verified_identity = _identity_for_body(body)
        with patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()):
            resp = client.post(
                "/internal/v1/provenance",
                json=body,
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_metric_failure_does_not_break_the_write(self, db, monkeypatch):
        """Telemetry must never fail a provenance write."""
        await self._seed_cross_tenant(db, org_id="org-repo-metricfail", settings={"trigger_policy": "any_adp_user"})

        body = _valid_body()
        body["org_id"] = "org-repo-metricfail"

        # Supply a synthetic verified identity to isolate this downstream policy.
        client = _make_app(db)
        with _agreeing_chain(client, body, monkeypatch=monkeypatch):
            with (
                patch("src.internal.auth_deps.get_settings", return_value=_settings_mock()),
                patch("boto3.client", side_effect=RuntimeError("no credentials")),
            ):
                resp = client.post(
                    "/internal/v1/provenance",
                    json=body,
                    headers={"X-Internal-Api-Key": _VALID_KEY},
                )
        assert resp.status_code == 201
