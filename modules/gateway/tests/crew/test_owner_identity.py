"""#8108: the Crew owner is the caller's tenant member row, like the Dev Box owner and the chat principal.

Dev evidence: a GitHub-attested member row (``users.org_id`` = tenant, no ``cognito_sub``)
plus a Cognito-indexed login row in another org. ``/cloud-desktops/*`` resolved the
member row through the signed username; ``/crew/runtime`` matched the sub against
``users.id`` and refused the same person with ``crew_scope_refused``.
"""

import inspect
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from src.agentauth import external_roots
from src.agentauth.external_roots import RootBinding
from src.auth.dependencies import get_current_user
from src.cloud_desktop import host_identity
from src.cloud_desktop import routes as cloud_desktop_routes
from src.crew import handoff, handoff_tickets, host_lease, model_token, proxy, targets
from src.crew.proxy import crew_dashboard_enabled, current_crew_owner, get_target_resolver, resolve_crew_owner, router
from src.crew.targets import CrewLease, CrewOwner, CrewTargetMissingError, ResolvedCrewTarget
from src.shared.database import get_db
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext

TENANT = "two"
PRODUCER = "arn:aws:iam::123456789012:role/chat-producer"


def context(subject, *, org=TENANT, username=""):
    return TokenContext(
        user_id=subject,
        org_id=org,
        team_id="",
        department_id="",
        account_type="human",
        cognito_username=username,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
async def people(db_session):
    db_session.add_all(
        [
            Organization(id="one", name="Personal"),
            Organization(id=TENANT, name="Tenant", settings={"crew": {"allowed_targets": ["eks-pod", "ec2-instance"]}}),
            # The dev shape: login row in a personal org; GitHub-attested member row in the tenant, no sub.
            User(id="github-login", org_id="one", team_id="team", email="owner@example.test", cognito_sub="broker-sub"),
            User(id="github-owner", org_id=TENANT, team_id="team", email="owner@example.test"),
            # A Cognito-native single-row member whose users.id is their sub (the previously working case).
            User(id="native-sub", org_id=TENANT, team_id="team", email="native@example.test", cognito_sub="native-sub"),
            # A login with no member row anywhere near the tenant.
            User(id="visitor", org_id="one", team_id="team", email="visitor@example.test", cognito_sub="visitor-sub"),
        ]
    )
    await db_session.commit()
    db_session.add_all(
        [
            TenantMembership(user_id="github-login", tenant_id="one", is_active=True, joined_via="org_membership"),
            TenantMembership(user_id="github-owner", tenant_id=TENANT, role="org_admin", joined_via="app_install"),
            TenantMembership(user_id="native-sub", tenant_id=TENANT),
            TenantMembership(user_id="visitor", tenant_id="one", is_active=True),
            UserIdentity(
                user_id="github-owner",
                org_id=TENANT,
                team_id="team",
                provider="github",
                provider_user_id="123",
                verification_method="oauth",
                verified_at=datetime.now(UTC),
            ),
        ]
    )
    await db_session.commit()
    return db_session


class ReadyResolver:
    """Resolves only the tenant member row's lease, as inventory keyed on that id would."""

    def __init__(self, owner_id):
        self.owner_id = owner_id

    async def resolve(self, owner, *, now):
        if owner.user_id != self.owner_id:
            raise CrewTargetMissingError("crew_target_missing")
        endpoint = "https://crew-host.example.test"
        lease = CrewLease(
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            target_kind="ec2-instance",
            target_ref="i-crew",
            generation=1,
            expires_at=now + 600,
            endpoint=endpoint,
        )
        return ResolvedCrewTarget(endpoint, lease, "ready")


def crew_app(db, token, resolver):
    app = FastAPI()
    app.include_router(router)

    async def database():
        yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: token
    app.dependency_overrides[crew_dashboard_enabled] = lambda: None
    app.dependency_overrides[get_target_resolver] = lambda: resolver
    return app


async def test_github_attested_owner_reaches_runtime_and_target(people):
    token = context("broker-sub", username="GitHub_123")
    owner = await current_crew_owner(token, people)
    assert owner == CrewOwner(TENANT, "github-owner")
    app = crew_app(people, token, ReadyResolver("github-owner"))
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
        runtime = await client.get("/crew/runtime")
        assert runtime.status_code == 200, runtime.text
        assert runtime.json() == {"allowed_targets": ["ec2-instance", "eks-pod"]}
        target = await client.get("/crew/target")
        assert target.status_code == 200, target.text
        assert target.json()["lease"]["user_id"] == "github-owner"


async def test_unlinked_person_is_refused_by_name(people):
    with pytest.raises(proxy.HTTPException) as refusal:
        await resolve_crew_owner(people, context("visitor-sub"))
    assert refusal.value.status_code == 404
    assert refusal.value.detail == {
        "error": "crew_scope_refused",
        "reason": "chat_principal_not_tenant_member",
        "detail": f"no member row in tenant {TENANT}",
    }
    assert refusal.value.headers == {"Cache-Control": "no-store"}
    # The GitHub-attested person without the signed username and without any proven
    # link on the login row is likewise refused, not silently bound to the login row.
    with pytest.raises(proxy.HTTPException) as unproven:
        await resolve_crew_owner(people, context("broker-sub"))
    assert unproven.value.detail["reason"] == "chat_principal_not_tenant_member"
    app = crew_app(people, context("visitor-sub"), ReadyResolver("github-owner"))
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
        response = await client.get("/crew/runtime")
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "crew_scope_refused"
    assert response.json()["detail"]["detail"] == f"no member row in tenant {TENANT}"


@pytest.mark.parametrize("subject", ["native-sub", "github-owner"])
async def test_existing_single_row_members_resolve_unchanged(people, subject):
    """A Cognito-native member (users.id == sub) and a handoff-ticket identity that
    already carries the users.id keep resolving to that same row."""
    expected = "native-sub" if subject == "native-sub" else "github-owner"
    assert (await resolve_crew_owner(people, context(subject))).user_id == expected


async def test_revoked_member_is_refused_after_the_fact(people):
    await people.execute(TenantMembership.__table__.update().where(TenantMembership.user_id == "github-owner").values(revoked_at=datetime.now(UTC)))
    await people.commit()
    with pytest.raises(proxy.HTTPException) as refusal:
        await resolve_crew_owner(people, context("broker-sub", username="GitHub_123"))
    assert refusal.value.detail["reason"] == "chat_principal_not_tenant_member"


# ---------------------------------------------------------------------------
# Parity: every owner check derives the owner from the same resolver, and every
# owner consumer takes the resolved ``CrewOwner`` instead of re-resolving identity.
# ---------------------------------------------------------------------------

OWNER_RESOLVERS = {
    "crew owner (runtime, target, handoff ticket, ensure, checkpoints, dashboard)": proxy.resolve_crew_owner,
    "dev box owner (/cloud-desktops/*)": cloud_desktop_routes.owner,
    "host enrollment / assistant host scope": host_identity.current_host,
    "chat admission principal": external_roots.canonical_human,
}

OWNER_CONSUMERS = [
    targets.OwnerTargetResolver.resolve,
    targets.OwnerTargetResolver.ensure,
    targets.OwnerTargetResolver.end,
    handoff_tickets.mint_handoff_ticket,
    model_token.mint_model_token,
    handoff.deliver_model_token,
    handoff.active_crew_run,
    handoff.mint_crew_link,
    host_lease.publish_host_lease,
]


def test_every_owner_check_uses_the_tenant_member_resolver():
    for name, function in OWNER_RESOLVERS.items():
        source = inspect.getsource(function)
        assert "tenant_member_principal(" in source, f"{name} does not resolve through tenant_member_principal"
        assert "workspace_user(" not in source, f"{name} still resolves through workspace_user"
        assert "current_chat_member(" not in source, f"{name} still matches the sub against users.id"


def test_every_owner_consumer_takes_the_resolved_owner():
    for function in OWNER_CONSUMERS:
        parameters = inspect.signature(function).parameters
        assert "owner" in parameters, f"{function.__qualname__} does not take the resolved owner"
        source = inspect.getsource(function)
        assert "tenant_member_principal(" not in source and "workspace_user(" not in source, f"{function.__qualname__} re-resolves identity"


async def test_crew_dev_box_and_chat_resolve_the_same_tenant_row(people):
    token = context("broker-sub", username="GitHub_123")
    crew = (await resolve_crew_owner(people, token)).user_id
    dev_box = await cloud_desktop_routes.owner(people, token)
    binding = RootBinding(source="chat", producer_role=PRODUCER, tenant_id=TENANT, personas=frozenset({"developer"}))
    # Chat admission has only the subject; the login row needs the proven GitHub link too.
    people.add(
        UserIdentity(
            user_id="github-login",
            org_id="one",
            team_id="team",
            provider="github",
            provider_user_id="123",
            verification_method="oauth",
            verified_at=datetime.now(UTC),
        )
    )
    await people.commit()
    chat = await external_roots.canonical_human(people, binding, "broker-sub")
    assert crew == dev_box == chat == "github-owner"
