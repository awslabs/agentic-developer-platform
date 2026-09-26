"""Harness for the Bedrock routing admin surface — Issue #4745 (#4692 · R4).

Three decisions here are the reason these tests prove anything:

**``AccessControl`` is never mocked.** The route constructs it against the request's
real session, so ``require_platform_admin`` runs for real against a real
``tenant_memberships`` row. A mocked authority check asserts a guarantee it never
exercised (the #4046 trap), and authority is the property under test.

**Roles come from the database, never from a claim.** The org-admin denials would be
meaningless if "org admin" were something the caller set on their own token.
``context_for`` sets ``is_admin`` only for the platform admin, which mirrors
``auth/dependencies.py`` — it deliberately excludes ``org_admin`` from that flag, and
that exclusion is exactly what the 403s prove.

**The probe is patched, never the gate.** ``routes``/``service`` keep every check;
what the fixtures replace is the network round trip to somebody else's AWS account.
Patching ``probe_routing_destination`` (the one probe) rather than the validator means
a test that "passes" cannot have skipped the validator.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.admin.bedrock_routing.routes import router as routing_router
from src.auth.dependencies import get_current_user
from src.auth.vault_routes import get_secrets_manager
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.base import Base
from src.shared.models.bedrock_routing import BedrockAccountMapping, BedrockDestinationRegistry
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserCredential
from src.shared.schemas.auth import TokenContext

ORG_ID = "org-4745-acme"
OTHER_ORG_ID = "org-4745-globex"
TEAM_ID = "team-4745-ml"
OTHER_TEAM_ID = "team-4745-ops"

# Canonical `users.id` values, deliberately unlike the Cognito subs beside them:
# `scope_id_user` is the canonical id (#4647), and a mapping written with a sub
# resolves for nobody. A test that accidentally passed a sub must fail, not coincide.
MEMBER_SUB = "sub-4745-member"
MEMBER_ID = "47450000-0000-4000-8000-000000000001"

ORG_ADMIN_SUB = "sub-4745-orgadmin"
ORG_ADMIN_ID = "47450000-0000-4000-8000-000000000002"

PLATFORM_ADMIN_SUB = "sub-4745-platformadmin"
PLATFORM_ADMIN_ID = "47450000-0000-4000-8000-000000000003"

FOREIGN_MEMBER_SUB = "sub-4745-foreign"
FOREIGN_MEMBER_ID = "47450000-0000-4000-8000-000000000004"

# Destination account ids. Distinct digits per row so an assertion on the wrong
# destination cannot pass by coincidence.
ACME_ACCOUNT = "111111114821"
GLOBEX_ACCOUNT = "222222227733"
PERSONAL_ACCOUNT = "333333332210"
PLATFORM_ACCOUNT = "444444449034"

ACME_DEST = "dest-acme-org"
GLOBEX_DEST = "dest-globex-org"
PERSONAL_DEST = "dest-personal"
PLATFORM_DEST = "dest-platform-registered"
UNVERIFIED_DEST = "dest-unverified"


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.fixture
async def seeded(session: AsyncSession) -> None:
    """Two tenants, four people, and five destinations covering every gate.

    The destination set is chosen so each save-time refusal has a target that trips
    exactly one gate — otherwise a test cannot tell which gate refused it.
    """
    session.add(Organization(id=ORG_ID, name="Acme Corp"))
    session.add(Organization(id=OTHER_ORG_ID, name="Globex"))

    cast = [
        (MEMBER_ID, MEMBER_SUB, ORG_ID, TEAM_ID, "member"),
        (ORG_ADMIN_ID, ORG_ADMIN_SUB, ORG_ID, TEAM_ID, "org_admin"),
        (PLATFORM_ADMIN_ID, PLATFORM_ADMIN_SUB, ORG_ID, TEAM_ID, "platform_admin"),
        (FOREIGN_MEMBER_ID, FOREIGN_MEMBER_SUB, OTHER_ORG_ID, OTHER_TEAM_ID, "member"),
    ]
    for canonical, sub, org_id, team_id, role in cast:
        session.add(User(id=canonical, cognito_sub=sub, email=f"{sub}@example.com", org_id=org_id, team_id=team_id))
        session.add(TenantMembership(user_id=canonical, tenant_id=org_id, role=role, is_active=True))

    # An org-scoped AWS connection (all three owner columns NULL — the `vault.py`
    # convention), so it passes the §4.3 ruling-6 check for team and org rungs.
    session.add(
        UserCredential(
            id="cred-acme-org",
            org_id=ORG_ID,
            service="aws",
            credential_type="aws_role",
            label="acme-prod",
            secret_arn="arn:aws:secretsmanager:us-east-1:999:secret:adp/orgs/acme/aws-abc",
            scopes={"account_id": ACME_ACCOUNT, "role_arn": f"arn:aws:iam::{ACME_ACCOUNT}:role/ADP-Agent-acme-prod", "status": "verified"},
        )
    )
    # A PERSONAL connection: user_id set. This is the row §4.3 forbids a team or org
    # rule from pointing at.
    session.add(
        UserCredential(
            id="cred-personal",
            org_id=ORG_ID,
            user_id=MEMBER_ID,
            service="aws",
            credential_type="aws_role",
            label="jdoe-dev",
            secret_arn="arn:aws:secretsmanager:us-east-1:999:secret:adp/users/jdoe/aws-def",
            scopes={"account_id": PERSONAL_ACCOUNT, "role_arn": f"arn:aws:iam::{PERSONAL_ACCOUNT}:role/ADP-Agent-jdoe-dev", "status": "verified"},
        )
    )

    verified = datetime(2026, 9, 1, tzinfo=UTC)
    destinations = [
        # Usable, linked to ORG_ID — the happy-path target.
        (ACME_DEST, ACME_ACCOUNT, ORG_ID, False, "cred-acme-org", "acme-prod", True, verified),
        # Usable, linked to the OTHER org — the §4.2 requirement-1 target.
        (GLOBEX_DEST, GLOBEX_ACCOUNT, OTHER_ORG_ID, False, None, "globex-prod", True, verified),
        # Usable, but backed by one person's credential — the §4.3 target.
        (PERSONAL_DEST, PERSONAL_ACCOUNT, ORG_ID, False, "cred-personal", "jdoe-dev", True, verified),
        # Platform-registered: no owning tenant, usable by any scope (§4.2 req 2).
        (PLATFORM_DEST, PLATFORM_ACCOUNT, None, True, None, "ml-research", True, verified),
        # Linked to ORG_ID but never verified — the §4.4 skip target.
        (UNVERIFIED_DEST, ACME_ACCOUNT, ORG_ID, False, None, "acme-sandbox", False, None),
    ]
    for dest_id, account, owner_org, platform_registered, credential_id, label, capable, verified_at in destinations:
        session.add(
            BedrockDestinationRegistry(
                id=dest_id,
                account_id=account,
                role_arn=f"arn:aws:iam::{account}:role/ADP-Agent-{label}",
                credential_id=credential_id,
                owner_org_id=owner_org,
                is_platform_registered=platform_registered,
                routing_capable=capable,
                verified_at=verified_at,
                region="us-east-1",
                label=label,
                registered_by_user_id=PLATFORM_ADMIN_ID,
            )
        )

    await session.commit()


def context_for(sub: str, *, org_id: str = ORG_ID, is_admin: bool = False, **overrides) -> TokenContext:
    """A token context.

    ``is_admin`` means **platform** admin and nothing else. ``auth/dependencies.py``
    excludes ``org_admin`` from it, so the org admin's context below carries
    ``is_admin=False`` — which is the whole reason ``require_platform_admin`` denies
    them and why the denial tests are not testing a coincidence.
    """
    defaults = {
        "user_id": sub,
        "org_id": org_id,
        "team_id": TEAM_ID,
        "department_id": "",
        "account_type": "human",
        "is_admin": is_admin,
        "expires_at": date(2099, 1, 1),
    }
    return TokenContext(**{**defaults, **overrides})


def org_admin_context() -> TokenContext:
    """The synthetic org_admin the dev environment cannot provide.

    ``platform/scripts/bedrock-routing-validate.sh`` documents the limitation: dev has
    no real org_admin identity, so *"the org_admin case is provable cheaply only in the
    backend test suite with a synthetic org_admin context, and that test is required at
    PR time."* This is that context. The caller is a real ``org_admin`` by
    ``tenant_memberships`` and legitimately holds their own org's permissions — which
    is precisely why the gate must be a claim about the caller and not a partition
    check that their own org id would satisfy.
    """
    return context_for(ORG_ADMIN_SUB)


def platform_admin_context() -> TokenContext:
    return context_for(PLATFORM_ADMIN_SUB, is_admin=True)


def member_context() -> TokenContext:
    return context_for(MEMBER_SUB)


def build_app(session: AsyncSession, context: TokenContext | None, secrets: object | None = None) -> FastAPI:
    """Mount the routing router alone, with auth, db and Secrets Manager overridden.

    ``AccessControl`` is deliberately NOT overridden, and neither is the service
    module's validation: the only thing replaced is the network egress.
    """
    app = FastAPI()
    app.include_router(routing_router)

    # The same handler src/app.py registers. Without it, a raised AccessDeniedError
    # surfaces as an unhandled 500 and every 403 assertion below would be measuring
    # the test app's gap instead of the route's behaviour.
    @app.exception_handler(BedrockGatewayError)
    async def gateway_error_handler(request: Request, exc: BedrockGatewayError):
        content = {"error": exc.error, "message": exc.message}
        if exc.details:
            content["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=content)

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_secrets_manager] = lambda: secrets or fake_secrets()
    if context is not None:
        app.dependency_overrides[get_current_user] = lambda: context
    return app


def fake_secrets(external_id: str = "ext-4745") -> MagicMock:
    """A Secrets Manager stand-in whose payload matches ``connect_start``'s shape."""
    helper = MagicMock()
    helper.get_secret.return_value = f'{{"role_arn": "arn:aws:iam::x:role/y", "external_id": "{external_id}", "default_region": "us-east-1"}}'
    helper.create_secret.return_value = "arn:aws:secretsmanager:us-east-1:999:secret:adp/orgs/new-xyz"
    return helper


def client_for(session: AsyncSession, context: TokenContext | None, secrets: object | None = None) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=build_app(session, context, secrets)), base_url="http://test")


@pytest.fixture
def probe_ok(monkeypatch):
    """Patch the ONE probe to report capable. Every gate still runs for real."""
    probe = AsyncMock(return_value=(True, None))
    monkeypatch.setattr("src.admin.bedrock_routing.service.probe_routing_destination", probe)
    return probe


@pytest.fixture
def probe_denied(monkeypatch):
    """Patch the ONE probe to report the §5.0 case: assumes, cannot invoke Bedrock."""
    probe = AsyncMock(return_value=(False, "role_missing_bedrock_permission"))
    monkeypatch.setattr("src.admin.bedrock_routing.service.probe_routing_destination", probe)
    return probe


async def stored_mappings(session: AsyncSession) -> list[BedrockAccountMapping]:
    """Every mapping row, so a test can assert what was — or was not — written."""
    import sqlalchemy as sa

    session.expire_all()
    result = await session.scalars(sa.select(BedrockAccountMapping).order_by(BedrockAccountMapping.scope_type))
    return list(result)


async def seed_mapping(
    session: AsyncSession,
    *,
    scope_type: str,
    destination_id: str,
    org_id: str | None = None,
    team_id: str | None = None,
    user_id: str | None = None,
    authored_by: str = PLATFORM_ADMIN_ID,
) -> BedrockAccountMapping:
    mapping = BedrockAccountMapping(
        scope_type=scope_type,
        scope_id_org=org_id,
        scope_id_team=team_id,
        scope_id_user=user_id,
        destination_id=destination_id,
        authored_by_user_id=authored_by,
    )
    session.add(mapping)
    await session.commit()
    return mapping


@pytest.fixture(autouse=True)
def routing_transaction_lock(monkeypatch):
    from src.admin.bedrock_routing import revisions
    lock = AsyncMock()
    monkeypatch.setattr(revisions, "serialize_writes", lock)
    return lock
