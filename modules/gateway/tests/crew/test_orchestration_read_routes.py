"""Host-run reads retain the real owner's current Product membership."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from src.admin.connections import service as connection_service
from src.admin.connections.schemas import ConnectionVerification
from src.crew import orchestration_read_routes
from src.crew.model_token import mint_model_token
from src.crew.targets import CrewOwner
from src.orchestration.models import OrchestrationDecision
from src.products.models import Product, ProductProjectBinding, Project
from src.proxy.routes import get_token_context, set_agent_run_id_from_header
from src.shared.database import get_db
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, TeamMembership, User
from src.shared.models.vault import ChannelTenantMap
from tests.crew.test_host_tools import active, host_context, registry  # noqa: F401
from tests.orchestration.test_registration import gateless_proposal


@pytest.fixture
def host_identity():
    return SimpleNamespace(context=host_context())


@pytest.fixture
def owner_session_signing_key(monkeypatch):
    key = "owner-session-test-signing-key"
    # Other collected modules set a different process-wide signing key.
    # Own this test's key explicitly while retaining the real JWT mint/verify.
    monkeypatch.setenv("BG_TOKEN_SECRET_KEY", key)
    return key


@pytest.fixture
async def client(active, db_session, host_identity):  # noqa: F811
    for tenant, owner in (("tenant-a", "owner-a"), ("tenant-b", "owner-b")):
        db_session.add(Organization(id=tenant, name=tenant))
        db_session.add(Department(id=f"{tenant}-department", org_id=tenant, name="Engineering"))
        db_session.add(Team(id=f"{tenant}-team", org_id=tenant, department_id=f"{tenant}-department", name="Engineering"))
        db_session.add(User(id=owner, org_id=tenant, team_id=f"{tenant}-team", email=f"{owner}@example.test"))
        db_session.add(TeamMembership(org_id=tenant, user_id=owner, team_id=f"{tenant}-team", is_primary=True))
        db_session.add(TenantMembership(tenant_id=tenant, user_id=owner, role="member", is_active=True))
        db_session.add(
            Product(
                tenant_id=tenant,
                id=f"{tenant}-product",
                name=f"{tenant} secret",
                purpose="Example",
                intended_users=["Owner"],
                owner_user_id=owner,
                team_id=f"{tenant}-team",
                starting_point="idea",
                created_by=owner,
            )
        )
        db_session.add(Project(tenant_id=tenant, id=f"{tenant}-project", name="Base", owner_user_id=owner))
        db_session.add(
            ProductProjectBinding(
                tenant_id=tenant,
                product_id=f"{tenant}-product",
                project_id=f"{tenant}-project",
                is_base=True,
                provenance={},
                valid_from=datetime.now(UTC),
            )
        )
    await db_session.commit()

    app = FastAPI()
    app.include_router(orchestration_read_routes.router)

    async def context(request: Request):
        request.state.token_context = host_identity.context
        return request.state.token_context

    async def run_id(request: Request):
        return request.headers.get("x-agent-runid")

    async def session():
        yield db_session

    app.dependency_overrides[get_token_context] = context
    app.dependency_overrides[set_agent_run_id_from_header] = run_id
    app.dependency_overrides[get_db] = session
    token = await mint_model_token(active.owner, active.target, now=active.now)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"X-Adp-Crew-Model-Token": token}) as http:
        yield http


@pytest.fixture
async def two_owner_runs(client, active, host_identity):  # noqa: F811
    other_instance = "i-0123456789abcdef1"
    other_owner = CrewOwner("tenant-b", "owner-b")
    other_run = active.store.mint(
        tenant_id="tenant-b",
        user_id="owner-b",
        session_id="box-b",
        lease_generation=3,
        lease_expires_at=active.now + 300,
        target_kind="ec2-instance",
        target_ref=other_instance,
        deployment_id="dep-a",
        now=active.now,
    )
    other_target = replace(
        active.target,
        lease=active.target.lease.model_copy(
            update={
                "tenant_id": "tenant-b",
                "user_id": "owner-b",
                "target_ref": "box-b",
            }
        ),
        host_run_id=other_run,
        host_instance_ref=other_instance,
    )
    original_resolve = active.resolver.resolve

    async def resolve(owner, *, now):
        if owner == other_owner:
            return other_target
        return await original_resolve(owner, now=now)

    active.resolver.resolve = resolve
    other_token = await mint_model_token(other_owner, other_target, now=active.now)
    return SimpleNamespace(instance=other_instance, run_id=other_run, token=other_token, identity=host_identity)


async def test_two_host_runs_can_only_read_their_respective_owner_products(client, active, two_owner_runs):  # noqa: F811
    owner_a = await client.get(
        "/crew/orchestration/products",
        headers={"X-Agent-RunId": active.run_id, "X-Agent-OrgId": "tenant-b"},
    )
    assert owner_a.status_code == 200, owner_a.text
    assert [product["id"] for product in owner_a.json()["items"]] == ["tenant-a-product"]
    assert "tenant-b secret" not in owner_a.text

    two_owner_runs.identity.context = host_context(two_owner_runs.instance)
    owner_b_headers = {
        "X-Agent-RunId": two_owner_runs.run_id,
        "X-Agent-OrgId": "tenant-a",
        "X-Adp-Crew-Model-Token": two_owner_runs.token,
    }
    owner_b = await client.get("/crew/orchestration/products", headers=owner_b_headers)
    assert owner_b.status_code == 200, owner_b.text
    assert [product["id"] for product in owner_b.json()["items"]] == ["tenant-b-product"]
    assert "tenant-a secret" not in owner_b.text

    for headers in (
        {**owner_b_headers, "X-Agent-RunId": active.run_id},
        {**owner_b_headers, "X-Adp-Crew-Model-Token": client.headers["X-Adp-Crew-Model-Token"]},
    ):
        denied = await client.get("/crew/orchestration/products", headers=headers)
        assert denied.status_code in (401, 403), denied.text
        assert "tenant-a secret" not in denied.text
        assert "tenant-b secret" not in denied.text


async def test_products_read_only_current_host_owner_even_with_forged_org_header(client, active):  # noqa: F811
    result = await client.get(
        "/crew/orchestration/products",
        headers={"X-Agent-RunId": active.run_id, "X-Agent-OrgId": "tenant-b"},
    )
    assert result.status_code == 200, result.text
    assert [item["id"] for item in result.json()["items"]] == ["tenant-a-product"]
    assert "tenant-b secret" not in result.text


async def test_different_run_id_cannot_read_the_owners_products(client):
    result = await client.get("/crew/orchestration/products", headers={"X-Agent-RunId": str(uuid4())})
    assert result.status_code == 403
    assert "tenant-a secret" not in result.text


async def test_host_github_status_lists_only_current_owners_repository_ids(
    client,
    active,  # noqa: F811
    two_owner_runs,
    host_identity,
    db_session,
    monkeypatch,
):
    for tenant, installation in (("tenant-a", 101), ("tenant-b", 202)):
        db_session.add(
            ChannelTenantMap(
                provider="github",
                provider_scope_id=str(installation),
                org_id=tenant,
                install_metadata={
                    "installation_id": installation,
                    "account_login": tenant,
                    "account_type": "Organization",
                    "repository_selection": "selected",
                },
            )
        )
        connection_service._repo_cache_invalidate(installation)
    await db_session.commit()

    calls = []

    class GitHubClient:
        async def list_installation_repository_names(self, installation_id):
            calls.append(installation_id)
            return [f"tenant-{'a' if installation_id == 101 else 'b'}/example"]

        async def list_installation_repositories_page(self, installation_id, page, limit):
            assert page == 1 and limit == 100
            return {
                "repositories": [
                    {"full_name": f"tenant-{'a' if installation_id == 101 else 'b'}/example", "id": 1001 if installation_id == 101 else 2002}
                ]
            }

    monkeypatch.setattr(connection_service, "_get_github_app_credentials", lambda: ("fixture", "fixture"))
    monkeypatch.setattr(connection_service, "GitHubAppClient", lambda **_kwargs: GitHubClient())
    monkeypatch.setattr(connection_service, "_find_orphaned_installations", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        connection_service,
        "_compute_connection_verification",
        AsyncMock(
            return_value=ConnectionVerification(record_present=True, repositories_live=True),
        ),
    )

    owner_a = await client.get(
        "/crew/orchestration/github/status",
        headers={"X-Agent-RunId": active.run_id, "X-Agent-OrgId": "tenant-b"},
    )
    assert owner_a.status_code == 200, owner_a.text
    assert [item["repository_details"] for item in owner_a.json()["connections"]] == [
        [{"full_name": "tenant-a/example", "repository_id": 1001}],
    ]
    assert calls == [101]
    assert "tenant-b/example" not in owner_a.text

    host_identity.context = host_context(two_owner_runs.instance)
    owner_b = await client.get(
        "/crew/orchestration/github/status",
        headers={
            "X-Agent-RunId": two_owner_runs.run_id,
            "X-Adp-Crew-Model-Token": two_owner_runs.token,
            "X-Agent-OrgId": "tenant-a",
        },
    )
    assert owner_b.status_code == 200, owner_b.text
    assert [item["repository_details"] for item in owner_b.json()["connections"]] == [
        [{"full_name": "tenant-b/example", "repository_id": 2002}],
    ]
    assert calls == [101, 202]
    assert "tenant-a/example" not in owner_b.text

    denied = await client.get(
        "/crew/orchestration/github/status",
        headers={
            "X-Agent-RunId": active.run_id,
            "X-Adp-Crew-Model-Token": two_owner_runs.token,
        },
    )
    assert denied.status_code == 403
    assert calls == [101, 202]


async def test_draft_wrappers_register_under_verified_host_owner(client, active, db_session):  # noqa: F811
    proposal = gateless_proposal(org_id="tenant-a").model_dump(mode="json")
    headers = {"X-Agent-RunId": active.run_id, "X-Agent-OrgId": "tenant-b"}
    dry_run = await client.post("/crew/orchestration/flows/drafts/preview", json=proposal, headers=headers)
    submitted = await client.post("/crew/orchestration/flows/drafts", json=proposal, headers=headers)
    assert dry_run.status_code == 200, dry_run.text
    assert submitted.status_code == 201, submitted.text
    assert dry_run.json()["wrote_nothing"] is True
    assert submitted.json()["flow_id"]
    decision = (await db_session.execute(select(OrchestrationDecision))).scalar_one()
    assert decision.org_id == "tenant-a" and decision.actor_id == "owner-a"
    assert decision.actor_kind == "service"


async def test_host_run_reads_draft_plan_versions_and_decisions(client, active):  # noqa: F811
    headers = {"X-Agent-RunId": active.run_id, "X-Agent-OrgId": "tenant-b"}
    proposal = gateless_proposal(org_id="tenant-a").model_dump(mode="json")
    registered = await client.post("/crew/orchestration/flows/drafts", json=proposal, headers=headers)
    assert registered.status_code == 201, registered.text
    flow_id = registered.json()["flow_id"]
    plans = await client.get(f"/crew/orchestration/flows/{flow_id}/plans", headers=headers)
    decisions = await client.get(f"/crew/orchestration/flows/{flow_id}/decisions", headers=headers)
    assert plans.status_code == 200, plans.text
    assert decisions.status_code == 200, decisions.text
    assert plans.json()[0]["plan_hash"] == registered.json()["plan_hash"]
    assert decisions.json()[0]["actor_kind"] == "service"
    for route in ("plans", "decisions"):
        denied = await client.get(f"/crew/orchestration/flows/{flow_id}/{route}", headers={"X-Agent-RunId": str(uuid4())})
        assert denied.status_code == 403
        assert flow_id not in denied.text


async def test_drafts_deny_unbound_run_and_missing_model_token(client, active):  # noqa: F811
    proposal = gateless_proposal(org_id="tenant-a").model_dump(mode="json")
    for headers in ({"X-Agent-RunId": str(uuid4())}, {"X-Agent-RunId": active.run_id, "X-Adp-Crew-Model-Token": ""}):
        response = await client.post("/crew/orchestration/flows/drafts", json=proposal, headers=headers)
        assert response.status_code in (401, 403), response.text


async def test_draft_registration_rejects_revoked_owner_and_foreign_tenant(client, active, db_session):  # noqa: F811
    headers = {"X-Agent-RunId": active.run_id, "X-Agent-OrgId": "tenant-b"}
    foreign = gateless_proposal(org_id="tenant-b").model_dump(mode="json")
    response = await client.post("/crew/orchestration/flows/drafts", json=foreign, headers=headers)
    assert response.status_code == 422, response.text
    await db_session.execute(update(TenantMembership).where(TenantMembership.user_id == "owner-a").values(revoked_at=datetime.now(UTC)))
    await db_session.commit()
    proposal = gateless_proposal(org_id="tenant-a").model_dump(mode="json")
    response = await client.post("/crew/orchestration/flows/drafts", json=proposal, headers=headers)
    assert response.status_code == 403, response.text
    assert (await db_session.execute(select(OrchestrationDecision))).all() == []


async def test_host_run_reads_owner_product_profile_and_project_budget(client, active):  # noqa: F811
    """#9522: the adp-flow preflight on a Dev Box needs the profile (delivery home) and the Project budget."""
    headers = {"X-Agent-RunId": active.run_id}
    profile = await client.get("/crew/orchestration/products/tenant-a-product/projects/tenant-a-project", headers=headers)
    assert profile.status_code == 200, profile.text
    body = profile.json()
    assert body["product_id"] == "tenant-a-product" and "delivery_home_implied" in body
    budget = await client.get("/crew/orchestration/projects/tenant-a-project/budget", headers=headers)
    assert budget.status_code in (200, 503), budget.text  # 503 only when the reservation store is absent in this harness
    if budget.status_code == 200:
        assert budget.json()["project_id"] == "tenant-a-project"
    for route in ("/crew/orchestration/products/tenant-b-product/projects/tenant-b-project", "/crew/orchestration/projects/tenant-b-project/budget"):
        denied = await client.get(route, headers=headers)
        assert denied.status_code in (403, 404), (route, denied.text)
    forged = await client.get("/crew/orchestration/products/tenant-a-product/projects/tenant-a-project", headers={"X-Agent-RunId": str(uuid4())})
    assert forged.status_code == 403


# --- #9640: owner session for a verified host run -----------------------------------------


async def test_verified_host_run_mints_the_owners_session_with_delegation(client, active, db_session, monkeypatch, owner_session_signing_key):  # noqa: F811
    from src.admin import platform_authority
    from src.auth.token_manager import TokenManager
    from src.shared.models.audit import AuditLog

    monkeypatch.setenv("ADP_PUBLIC_API_URL", "https://app.example.test/api")
    monkeypatch.setattr(platform_authority, "current_cognito_admin", lambda subject, username="": subject == "owner-a")
    response = await client.post("/crew/orchestration/owner-session", headers={"X-Agent-RunId": active.run_id})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["gateway_url"] == "https://app.example.test/api"
    assert body["user_id"] == "owner-a" and body["is_admin"] is True
    assert body["delegation"] == f"dev-box:{active.target.lease.target_ref}:{active.run_id}"
    assert response.headers["cache-control"] == "no-store"
    # The token IS the owner's login-equivalent context, and it names the delegate.
    context = await TokenManager(owner_session_signing_key).validate_token(body["access_token"], db_session)
    assert (context.user_id, context.org_id, context.team_id, context.account_type) == ("owner-a", "tenant-a", "tenant-a-team", "human")
    assert context.is_admin is True
    assert context.delegation == body["delegation"]
    assert (datetime.fromisoformat(body["expires_at"]) - datetime.now(UTC)).total_seconds() <= 60 * 60 + 5
    minted = (await db_session.execute(select(AuditLog).where(AuditLog.event_type == "dev_box_owner_session_minted"))).scalars().all()
    assert len(minted) == 1 and minted[0].actor_id == "owner-a" and minted[0].details["delegation"] == body["delegation"]


async def test_owner_session_is_not_an_admin_session_for_a_plain_member(client, active, db_session, monkeypatch, owner_session_signing_key):  # noqa: F811
    from src.admin import platform_authority
    from src.auth.token_manager import TokenManager

    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173, https://portal.example.test")
    monkeypatch.delenv("ADP_PUBLIC_API_URL", raising=False)
    monkeypatch.setattr(platform_authority, "current_cognito_admin", lambda subject, username="": False)
    response = await client.post("/crew/orchestration/owner-session", headers={"X-Agent-RunId": active.run_id})
    assert response.status_code == 201, response.text
    assert response.json()["gateway_url"] == "https://portal.example.test/api"
    context = await TokenManager(owner_session_signing_key).validate_token(response.json()["access_token"], db_session)
    assert context.is_admin is False and context.user_id == "owner-a"


async def test_owner_session_refuses_a_missing_model_token_and_a_foreign_run(client, active, host_identity, monkeypatch):  # noqa: F811
    from src.admin import platform_authority

    monkeypatch.setenv("ADP_PUBLIC_API_URL", "https://app.example.test/api")
    monkeypatch.setattr(platform_authority, "current_cognito_admin", lambda subject, username="": False)
    unbound = await client.post("/crew/orchestration/owner-session", headers={"X-Agent-RunId": str(uuid4())})
    assert unbound.status_code in (403, 404), unbound.text
    no_token = await client.post("/crew/orchestration/owner-session", headers={"X-Agent-RunId": active.run_id, "X-Adp-Crew-Model-Token": ""})
    assert no_token.status_code in (401, 403), no_token.text
