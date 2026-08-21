"""Authorization + denylist for privileged agent-registry writes (Issue #3989).

Child E of sub-EPIC #3984, finding f-2c3ccdce.

The DynamoDB agent registry is an *authentication database*: the Lambda
authorizer resolves any registered ``role_arn`` to a ``TokenContext`` with
``account_type="service"`` in that row's org (``src/auth/agent_registry.py``), and
``role_arn`` is unique table-wide. Two consequences drive these tests:

1. ``POST /admin/agents/onboard`` had NO authorization check at all while
   ``org_id`` is a required, caller-supplied field — any authenticated principal
   could mint an authenticated identity inside any org. It is now gated on
   ``Permission.AGENT_REGISTER`` scoped to the target org, and the other
   caller-supplied scope fields (``team_id``, ``owner``, ``level``) are validated
   against that org.
2. Nothing stopped a caller from registering a PLATFORM-owned role, which both
   binds the platform's own role into a tenant and squats the ARN so the
   legitimate row can never be created. A reserved-role-name denylist is now
   enforced on every registry write regardless of caller privilege.

The route tests use the REAL ``AccessControl`` against a real session with
``rbac_least_privilege_default=True`` (#3987 PR 2's default), because under the
currently shipped default a membership-less caller still resolves to ORG_ADMIN
and the 403s would be unreachable.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.agent_onboarding_schemas import AgentOnboardRequest
from src.admin.agent_onboarding_service import AgentOnboardingService
from src.admin.agent_registry_schemas import (
    AgentRegistryCreateRequest,
    AgentRegistryResponse,
    AgentRegistryUpdateRequest,
    is_reserved_role_arn,
    role_name_from_arn,
)
from src.admin.agent_registry_service import AgentRegistryService
from src.admin.config import AdminConfig, set_admin_config
from src.admin.exceptions import AccessDeniedError
from src.admin.routes import (
    _validate_onboard_target_scope,
    get_agent_onboarding_service,
    get_agent_registry_service,
    get_current_user,
    router,
)
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.exceptions import ValidationError as GatewayValidationError
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.schemas.auth import TokenContext

ORG_A = "org-a"
ORG_B = "org-b"

MEMBER_SUB = "sub-member"
ORG_ADMIN_SUB = "sub-orgadmin"
PLATFORM_SUB = "sub-platform"

VALID_ROLE_ARN = "arn:aws:iam::123456789012:role/tenant-agent-role"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def least_privilege_config():
    """Run with #3987 PR 2's least-privilege default so the gates are effective."""
    set_admin_config(AdminConfig(rbac_least_privilege_default=True, rbac_role_cache_ttl_seconds=30.0))
    yield
    set_admin_config(AdminConfig())


def _context(sub: str, org_id: str, *, is_admin: bool = False) -> TokenContext:
    return TokenContext(
        user_id=sub,
        org_id=org_id,
        team_id="team-a1",
        department_id="dept-a",
        account_type="human",
        is_admin=is_admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


async def _seed(db: AsyncSession) -> None:
    """Two orgs, each with a team; a member and an org admin in org-a."""
    db.add_all(
        [
            Organization(id=ORG_A, name="Org A"),
            Organization(id=ORG_B, name="Org B"),
        ]
    )
    await db.flush()
    db.add_all(
        [
            Department(id="dept-a", org_id=ORG_A, name="Dept A"),
            Department(id="dept-b", org_id=ORG_B, name="Dept B"),
        ]
    )
    await db.flush()
    db.add_all(
        [
            Team(id="team-a1", org_id=ORG_A, department_id="dept-a", name="Team A1"),
            Team(id="team-b1", org_id=ORG_B, department_id="dept-b", name="Team B1"),
        ]
    )
    db.add_all(
        [
            User(id="pg-member", org_id=ORG_A, team_id="team-a1", email="member@a.test", cognito_sub=MEMBER_SUB),
            User(id="pg-orgadmin", org_id=ORG_A, team_id="team-a1", email="orgadmin@a.test", cognito_sub=ORG_ADMIN_SUB),
            User(id="pg-platform", org_id=ORG_A, team_id="team-a1", email="platform@a.test", cognito_sub=PLATFORM_SUB),
            User(id="pg-outsider", org_id=ORG_B, team_id="team-b1", email="outsider@b.test", cognito_sub="sub-outsider"),
        ]
    )
    await db.flush()
    db.add_all(
        [
            TenantMembership(user_id="pg-member", tenant_id=ORG_A, role="member", is_active=True, joined_via="org_membership"),
            TenantMembership(user_id="pg-orgadmin", tenant_id=ORG_A, role="org_admin", is_active=True, joined_via="org_membership"),
        ]
    )
    await db.commit()


@pytest.fixture
def seeded_db(db_session: AsyncSession) -> AsyncSession:
    """tests/admin/conftest.py's session, seeded with the org/user fixtures.

    Sync so the TestClient route tests below stay sync; the loop is already
    running because ``db_session`` itself is an async fixture.
    """
    asyncio.get_event_loop().run_until_complete(_seed(db_session))
    return db_session


@pytest.fixture
def onboarding_service() -> MagicMock:
    service = MagicMock(spec=AgentOnboardingService)
    service.onboard_agent = AsyncMock(
        return_value={
            "agent_id": "agent-uuid",
            "service_account_name": "agent-x-sa",
            "runs_on_label": "arc-runner-x",
            "scale_set_name": "arc-runner-x",
            "team_namespace": "arc-runners-team-a1",
            "api_gateway_invoke_url": "https://api.example.test",
            "irsa_trust_policy_snippet": {},
            "budget_config_id": None,
            "message": "ok",
        }
    )
    return service


def _registry_response(*, org_id: str = ORG_A, role_arn: str = VALID_ROLE_ARN) -> AgentRegistryResponse:
    now = datetime.now(UTC)
    return AgentRegistryResponse(
        agent_id="agent-uuid",
        agent_name="agent-x",
        role_arn=role_arn,
        org_id=org_id,
        team_id="team-a1",
        owner="pg-member",
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def registry_service() -> MagicMock:
    service = MagicMock(spec=AgentRegistryService)
    service.create_agent = AsyncMock(return_value=_registry_response())
    service.update_agent = AsyncMock(return_value=_registry_response())
    service.get_agent = AsyncMock(return_value=_registry_response())
    return service


def _client(
    caller: TokenContext,
    db: AsyncSession,
    *,
    onboarding=None,
    registry=None,
) -> TestClient:
    """Test app wired with the REAL AccessControl (via the real get_db session)."""
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(BedrockGatewayError)
    async def gateway_error_handler(request: Request, exc: BedrockGatewayError):
        content = {"error": exc.error, "message": exc.message}
        if exc.details:
            content["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=content)

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = lambda: caller
    if onboarding is not None:
        app.dependency_overrides[get_agent_onboarding_service] = lambda: onboarding
    if registry is not None:
        app.dependency_overrides[get_agent_registry_service] = lambda: registry

    # NOTE: get_access_control is deliberately NOT overridden — it depends on
    # get_db, so it resolves to a real AccessControl over the seeded session.
    # Mocking it (as tests/admin/test_registry_org_scope.py does) would make
    # every permission assertion below vacuous.
    return TestClient(app, raise_server_exceptions=False)


def _onboard_body(**overrides) -> dict:
    body = {
        "agent_name": "my-agent",
        "role_arn": VALID_ROLE_ARN,
        "org_id": ORG_A,
        "team_id": "team-a1",
        "owner": MEMBER_SUB,
        "level": "team",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# POST /admin/agents/onboard
# ---------------------------------------------------------------------------


class TestOnboardPermissionGate:
    """The finding: the endpoint had no authorization check whatsoever."""

    def test_plain_member_cannot_onboard(self, seeded_db, onboarding_service):
        client = _client(_context(MEMBER_SUB, ORG_A), seeded_db, onboarding=onboarding_service)
        resp = client.post("/admin/agents/onboard", json=_onboard_body())
        assert resp.status_code == 403
        assert resp.json()["error"] == "access_denied"
        # The registry write must never be reached.
        onboarding_service.onboard_agent.assert_not_awaited()

    def test_gate_names_agent_register_not_org_update(self, seeded_db, onboarding_service):
        """Deliberately its own permission: ORG_UPDATE is held by every role that
        can edit any org attribute, and minting identity is strictly higher."""
        client = _client(_context(MEMBER_SUB, ORG_A), seeded_db, onboarding=onboarding_service)
        resp = client.post("/admin/agents/onboard", json=_onboard_body())
        assert resp.json()["details"]["required_permission"] == "agent:register"

    def test_org_admin_cannot_onboard_into_another_org(self, seeded_db, onboarding_service):
        """org_id is caller-supplied — the scope check must pin it to the caller's org."""
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db, onboarding=onboarding_service)
        resp = client.post("/admin/agents/onboard", json=_onboard_body(org_id=ORG_B, team_id="team-b1", owner="sub-outsider"))
        assert resp.status_code == 403
        onboarding_service.onboard_agent.assert_not_awaited()

    def test_org_admin_can_onboard_into_own_org(self, seeded_db, onboarding_service):
        """Regression guard: the gate must not lock out legitimate org admins."""
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db, onboarding=onboarding_service)
        resp = client.post("/admin/agents/onboard", json=_onboard_body())
        assert resp.status_code == 201, resp.text
        onboarding_service.onboard_agent.assert_awaited_once()

    def test_platform_admin_can_onboard_into_any_org(self, seeded_db, onboarding_service):
        client = _client(_context(PLATFORM_SUB, ORG_A, is_admin=True), seeded_db, onboarding=onboarding_service)
        resp = client.post("/admin/agents/onboard", json=_onboard_body(org_id=ORG_B, team_id="team-b1", owner="sub-outsider"))
        assert resp.status_code == 201, resp.text


class TestOnboardTargetScopeValidation:
    """team_id / owner / level are separate caller-controlled fields."""

    def test_team_from_another_org_is_rejected(self, seeded_db, onboarding_service):
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db, onboarding=onboarding_service)
        resp = client.post("/admin/agents/onboard", json=_onboard_body(team_id="team-b1"))
        assert resp.status_code == 403
        assert resp.json()["error"] == "invalid_scope"
        onboarding_service.onboard_agent.assert_not_awaited()

    def test_unknown_team_is_rejected(self, seeded_db, onboarding_service):
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db, onboarding=onboarding_service)
        resp = client.post("/admin/agents/onboard", json=_onboard_body(team_id="team-does-not-exist"))
        assert resp.status_code == 403

    def test_owner_outside_target_org_is_rejected(self, seeded_db, onboarding_service):
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db, onboarding=onboarding_service)
        resp = client.post("/admin/agents/onboard", json=_onboard_body(owner="sub-outsider"))
        assert resp.status_code == 403
        assert resp.json()["error"] == "invalid_scope"

    def test_unknown_owner_is_rejected(self, seeded_db, onboarding_service):
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db, onboarding=onboarding_service)
        resp = client.post("/admin/agents/onboard", json=_onboard_body(owner="nobody@nowhere"))
        assert resp.status_code == 403

    def test_owner_may_be_the_postgres_user_id(self, seeded_db, onboarding_service):
        """Callers pass either the Cognito sub or users.id; both must be accepted."""
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db, onboarding=onboarding_service)
        resp = client.post("/admin/agents/onboard", json=_onboard_body(owner="pg-member"))
        assert resp.status_code == 201, resp.text

    def test_no_team_id_is_allowed(self, seeded_db, onboarding_service):
        """team_id is optional; omitting it must not trip the team validation."""
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db, onboarding=onboarding_service)
        body = _onboard_body()
        del body["team_id"]
        resp = client.post("/admin/agents/onboard", json=body)
        assert resp.status_code == 201, resp.text

    def test_org_level_requires_org_admin(self, seeded_db):
        """level="org" grants org-wide reach.

        Exercised against the helper directly: no role currently holds
        AGENT_REGISTER without also being an org admin, so this is the
        defence-in-depth path that a future role grant must not bypass.
        """
        access = MagicMock()
        access.is_org_admin = AsyncMock(return_value=False)
        request = AgentOnboardRequest(**_onboard_body(level="org"))

        with pytest.raises(AccessDeniedError):
            asyncio.get_event_loop().run_until_complete(_validate_onboard_target_scope(request, _context(MEMBER_SUB, ORG_A), access, seeded_db))

    def test_team_level_does_not_require_org_admin(self, seeded_db):
        """The level check must only bite on the org level."""
        access = MagicMock()
        access.is_org_admin = AsyncMock(return_value=False)
        request = AgentOnboardRequest(**_onboard_body(level="team"))

        asyncio.get_event_loop().run_until_complete(_validate_onboard_target_scope(request, _context(MEMBER_SUB, ORG_A), access, seeded_db))
        access.is_org_admin.assert_not_awaited()


# ---------------------------------------------------------------------------
# /admin/registry/agents (create + update)
# ---------------------------------------------------------------------------


class TestRegistryWritePermissionGate:
    """Registry create/update moved from ORG_UPDATE to AGENT_REGISTER."""

    _CREATE_BODY = {
        "agent_name": "direct-agent",
        "role_arn": VALID_ROLE_ARN,
        "org_id": ORG_A,
        "owner": "pg-member",
    }

    def test_member_cannot_create_registry_agent(self, seeded_db, registry_service):
        client = _client(_context(MEMBER_SUB, ORG_A), seeded_db, registry=registry_service)
        resp = client.post("/admin/registry/agents", json=self._CREATE_BODY)
        assert resp.status_code == 403
        assert resp.json()["details"]["required_permission"] == "agent:register"
        registry_service.create_agent.assert_not_awaited()

    def test_org_admin_can_create_registry_agent_in_own_org(self, seeded_db, registry_service):
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db, registry=registry_service)
        resp = client.post("/admin/registry/agents", json=self._CREATE_BODY)
        assert resp.status_code == 201, resp.text
        registry_service.create_agent.assert_awaited_once()

    def test_org_admin_cannot_create_registry_agent_in_another_org(self, seeded_db, registry_service):
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db, registry=registry_service)
        resp = client.post("/admin/registry/agents", json={**self._CREATE_BODY, "org_id": ORG_B})
        assert resp.status_code == 403
        registry_service.create_agent.assert_not_awaited()

    def test_member_cannot_update_registry_agent(self, seeded_db, registry_service):
        """Repointing role_arn mints identity exactly as a create does."""
        client = _client(_context(MEMBER_SUB, ORG_A), seeded_db, registry=registry_service)
        resp = client.patch("/admin/registry/agents/agent-uuid", json={"role_arn": "arn:aws:iam::123456789012:role/attacker-role"})
        assert resp.status_code == 403
        assert resp.json()["details"]["required_permission"] == "agent:register"
        registry_service.update_agent.assert_not_awaited()

    def test_org_admin_can_update_registry_agent_in_own_org(self, seeded_db, registry_service):
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db, registry=registry_service)
        resp = client.patch("/admin/registry/agents/agent-uuid", json={"agent_name": "renamed"})
        assert resp.status_code == 200, resp.text
        registry_service.update_agent.assert_awaited_once()

    def test_org_admin_cannot_update_another_orgs_registry_agent(self, seeded_db, registry_service):
        registry_service.get_agent = AsyncMock(return_value=_registry_response(org_id=ORG_B))
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db, registry=registry_service)
        resp = client.patch("/admin/registry/agents/agent-uuid", json={"agent_name": "renamed"})
        assert resp.status_code == 403
        registry_service.update_agent.assert_not_awaited()


# ---------------------------------------------------------------------------
# Platform-reserved role-name denylist
# ---------------------------------------------------------------------------


class TestRoleNameFromArn:
    """The denylist matches on the role NAME, not the raw ARN."""

    def test_plain_role_arn(self):
        assert role_name_from_arn("arn:aws:iam::123456789012:role/my-role") == "my-role"

    def test_path_prefixed_role_arn(self):
        """A path must not let a reserved name hide behind it."""
        assert role_name_from_arn("arn:aws:iam::123456789012:role/some/path/adp-runner") == "adp-runner"

    def test_non_role_arn_returns_empty(self):
        assert role_name_from_arn("arn:aws:iam::123456789012:user/bob") == ""


class TestIsReservedRoleArn:
    """Default denylist is BG_RESERVED_ROLE_NAME_PREFIXES="adp-,bedrockgw-"."""

    @pytest.mark.parametrize(
        "role_arn",
        [
            "arn:aws:iam::123456789012:role/adp-agent-runtime",
            "arn:aws:iam::123456789012:role/bedrockgw-gateway-irsa",
            "arn:aws:iam::123456789012:role/ADP-Agent-Runtime",
            "arn:aws:iam::123456789012:role/service-role/adp-webhook-lambda",
        ],
    )
    def test_reserved_prefixes_match(self, role_arn):
        assert is_reserved_role_arn(role_arn) is True

    @pytest.mark.parametrize(
        "role_arn",
        [
            VALID_ROLE_ARN,
            "arn:aws:iam::123456789012:role/my-adp-role",  # prefix must be at the start
            "arn:aws:iam::123456789012:role/bedrock-gateway-thing",
            "arn:aws:iam::123456789012:user/adp-not-a-role",
        ],
    )
    def test_tenant_roles_do_not_match(self, role_arn):
        assert is_reserved_role_arn(role_arn) is False


class TestDenylistEnforcedOnRegistryWrites:
    """Enforced in the service — the single choke point for every registry write."""

    @pytest.fixture
    def service(self) -> AgentRegistryService:
        dynamodb = MagicMock()
        return AgentRegistryService(dynamodb_client=dynamodb, table_name="test-agent-registry")

    async def test_create_rejects_reserved_role_arn(self, service):
        request = AgentRegistryCreateRequest(
            agent_name="squatter",
            role_arn="arn:aws:iam::123456789012:role/adp-agent-runtime",
            org_id=ORG_A,
            owner="pg-member",
        )
        with pytest.raises(GatewayValidationError):
            await service.create_agent(request)
        # Rejected before any DynamoDB access, so the ARN is never squatted.
        service.dynamodb.query.assert_not_called()
        service.dynamodb.put_item.assert_not_called()

    async def test_update_rejects_reserved_role_arn(self, service):
        request = AgentRegistryUpdateRequest(role_arn="arn:aws:iam::123456789012:role/bedrockgw-gateway-irsa")
        with pytest.raises(GatewayValidationError):
            await service.update_agent("agent-uuid", request)
        service.dynamodb.get_item.assert_not_called()
        service.dynamodb.update_item.assert_not_called()

    async def test_update_without_role_arn_is_not_blocked(self, service):
        """A metadata-only update must not trip the denylist (role_arn is None)."""
        now = datetime.now(UTC).isoformat()
        service.get_agent = AsyncMock(return_value=_registry_response())
        service.dynamodb.update_item.return_value = {
            "Attributes": {
                "agent_id": {"S": "agent-uuid"},
                "agent_name": {"S": "agent-x"},
                "role_arn": {"S": VALID_ROLE_ARN},
                "org_id": {"S": ORG_A},
                "owner": {"S": "pg-member"},
                "description": {"S": "just a description"},
                "created_at": {"S": now},
                "updated_at": {"S": now},
            }
        }
        request = AgentRegistryUpdateRequest(description="just a description")

        result = await service.update_agent("agent-uuid", request)

        # Reached the DynamoDB write, i.e. it got past _reject_reserved_role_arn.
        assert result.description == "just a description"
        service.dynamodb.update_item.assert_called_once()


class TestDenylistAppliesToPlatformAdmins:
    """Item 4 of the approved design: the denylist binds regardless of privilege.

    A same-account ``iam:GetRole`` ownership check would NOT catch this — platform
    roles are precisely the same-account roles that pass it — which is why the
    approved design uses a name denylist instead.
    """

    def test_platform_admin_create_with_reserved_arn_returns_400(self, seeded_db):
        # Real service (mocked DynamoDB) so the service-layer denylist is in play.
        real_service = AgentRegistryService(dynamodb_client=MagicMock(), table_name="test-agent-registry")
        client = _client(_context(PLATFORM_SUB, ORG_A, is_admin=True), seeded_db, registry=real_service)

        resp = client.post(
            "/admin/registry/agents",
            json={
                "agent_name": "squatter",
                "role_arn": "arn:aws:iam::123456789012:role/adp-agent-runtime",
                "org_id": ORG_A,
                "owner": "pg-member",
            },
        )

        assert resp.status_code == 400
        assert resp.json()["error"] == "validation_error"
        real_service.dynamodb.put_item.assert_not_called()

    def test_platform_admin_onboard_with_reserved_arn_returns_400(self, seeded_db, monkeypatch):
        """The onboard path funnels into the same service method."""
        real_registry = AgentRegistryService(dynamodb_client=MagicMock(), table_name="test-agent-registry")
        onboarding = AgentOnboardingService(iam_client=MagicMock(), agent_registry_service=real_registry)
        # Skip the IAM/STS round trip; the denylist runs after it.
        monkeypatch.setattr(onboarding, "_validate_role", AsyncMock(return_value=None))

        app_client = _client(_context(PLATFORM_SUB, ORG_A, is_admin=True), seeded_db, onboarding=onboarding)
        resp = app_client.post("/admin/agents/onboard", json=_onboard_body(role_arn="arn:aws:iam::123456789012:role/adp-agent-runtime"))

        assert resp.status_code == 400
        assert resp.json()["error"] == "validation_error"
        real_registry.dynamodb.put_item.assert_not_called()
