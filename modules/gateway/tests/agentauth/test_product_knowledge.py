"""Live Product membership is required before a run can obtain a Door grant."""

from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, text, update

from src.admin.platform_authority import set_platform_admin_grant
from src.agentauth import knowledge_service as service
from src.agentauth.product_knowledge import product_from_body, requested_product
from src.products.models import Product
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Team, TeamMembership, User
from src.shared.models.platform_admin import PlatformAdminGrant
from src.shared.models.vault import UserIdentity
from tests.agentauth.test_knowledge_service import HEADERS, PUBLIC_KEYS, URL, bridge, verify_identity  # noqa: F401
from tests.agentauth.test_knowledge_service_postgres import OWNER, sessions  # noqa: F401
from tests.agentauth.test_run_services import GRANT, RECORD
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401

PRODUCT = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
REAL_IDENTITY = service.locked_door_identity


@pytest.fixture
def current_platform_admin(monkeypatch):
    lookup = Mock(return_value=False)
    monkeypatch.setattr("src.admin.platform_authority.current_cognito_admin", lookup)
    return lookup


@pytest.fixture
async def product_db(sessions, current_platform_admin):  # noqa: F811 - imported fixture
    async with sessions() as db:
        conn = await db.connection()
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[Team.__table__, TeamMembership.__table__, Product.__table__, PlatformAdminGrant.__table__])
        )
        db.add(Team(id="product-team", name="product-team", org_id="tenant-one", department_id="department"))
        db.add(
            Product(
                tenant_id="tenant-one",
                id=PRODUCT,
                name="Product",
                purpose="Knowledge",
                intended_users=[],
                owner_user_id="other-owner",
                team_id="product-team",
                starting_point="new",
                created_by="other-owner",
            )
        )
        db.add(
            Product(
                tenant_id="tenant-two",
                id=OTHER,
                name="Other",
                purpose="Knowledge",
                intended_users=[],
                owner_user_id="human-one",
                starting_point="new",
                created_by="human-two",
            )
        )
        await db.flush()
        db.add(TeamMembership(user_id="human-one", team_id="product-team", org_id="tenant-one"))
        await db.commit()
    return sessions


async def test_run_member_without_github_identity_gets_only_requested_product(product_db, bridge, current_platform_admin, monkeypatch):  # noqa: F811
    monkeypatch.setattr(service, "locked_door_identity", REAL_IDENTITY)
    async with product_db() as db:
        await db.execute(delete(UserIdentity).where(UserIdentity.provider == "github"))
        await db.commit()
    response = await bridge.client.post(
        URL, headers={**HEADERS, "x-product-id": OTHER}, json={"name": "search", "arguments": {"query": "info", "product_id": PRODUCT}}
    )
    assert response.status_code == 200
    request = bridge.upstream[0]
    claims = verify_identity(request.headers["x-adp-door-identity"], PUBLIC_KEYS, method=request.method, path=request.url.path, body=request.content)
    assert claims["product_id"] == PRODUCT
    assert claims["github_login"] == ""
    assert "x-product-id" not in request.headers
    current_platform_admin.assert_not_called()


async def test_revoked_member_cannot_reuse_run_or_product_selection(product_db, bridge, monkeypatch):  # noqa: F811
    monkeypatch.setattr(service, "locked_door_identity", REAL_IDENTITY)
    async with product_db() as db:
        await db.execute(delete(TeamMembership).where(TeamMembership.user_id == "human-one"))
        await db.commit()
    response = await bridge.client.post(URL, headers=HEADERS, json={"name": "search", "arguments": {"query": "info", "product_id": PRODUCT}})
    assert response.status_code == 404
    assert not bridge.upstream


async def test_run_does_not_release_result_when_final_product_check_refuses(product_db, bridge, monkeypatch):  # noqa: F811
    from unittest.mock import AsyncMock

    monkeypatch.setattr(service, "locked_door_identity", REAL_IDENTITY)
    check = AsyncMock(side_effect=[None, HTTPException(404, "Product not found")])
    monkeypatch.setattr(service, "authorize_product", check)
    response = await bridge.client.post(URL, headers=HEADERS, json={"name": "search", "arguments": {"query": "info", "product_id": PRODUCT}})
    assert response.status_code == 404
    assert len(bridge.upstream) == 1
    assert check.await_count == 2
    assert "own-private-data" not in response.text


async def test_product_in_another_tenant_is_not_a_grant_even_for_same_owner(product_db):
    with pytest.raises(HTTPException) as error:
        async with REAL_IDENTITY(RECORD, GRANT, product_id=OTHER):
            pytest.fail("cross-tenant grant")
    assert error.value.status_code == 404


@pytest.mark.parametrize("role", [None, "admin", "platform_admin"])
async def test_owner_cached_team_and_display_role_do_not_grant_product_access(product_db, bridge, monkeypatch, role):  # noqa: F811
    monkeypatch.setattr(service, "locked_door_identity", REAL_IDENTITY)
    async with product_db() as db:
        await db.execute(delete(TeamMembership).where(TeamMembership.user_id == "human-one"))
        await db.execute(update(Product).where(Product.id == PRODUCT).values(owner_user_id="human-one"))
        await db.execute(update(User).where(User.id == "human-one").values(team_id="product-team", role=role))
        await db.commit()
    response = await bridge.client.post(
        URL,
        headers={**HEADERS, "x-is-admin": "true", "x-platform-admin": "true"},
        json={"name": "search", "arguments": {"query": "info", "product_id": PRODUCT, "is_admin": True}},
    )
    assert response.status_code == 404
    assert not bridge.upstream


async def test_current_platform_admin_without_product_team_uses_canonical_login(product_db, bridge, current_platform_admin, monkeypatch):  # noqa: F811
    monkeypatch.setattr(service, "locked_door_identity", REAL_IDENTITY)
    current_platform_admin.return_value = True
    async with product_db() as db:
        await db.execute(delete(TeamMembership).where(TeamMembership.user_id == "human-one"))
        await db.execute(update(User).where(User.id == "human-one").values(role="member", cognito_username="canonical-login"))
        await db.commit()
    response = await bridge.client.post(URL, headers=HEADERS, json={"name": "search", "arguments": {"query": "info", "product_id": PRODUCT}})
    assert response.status_code == 200
    assert len(bridge.upstream) == 1
    # Authority is freshly resolved before forwarding and before release.
    assert current_platform_admin.call_count == 2
    assert all(call.args == (OWNER, "canonical-login") for call in current_platform_admin.call_args_list)


async def test_admin_lookup_uses_canonical_login_for_placed_workspace_account(product_db, bridge, current_platform_admin, monkeypatch):  # noqa: F811
    monkeypatch.setattr(service, "locked_door_identity", REAL_IDENTITY)
    current_platform_admin.return_value = True
    async with product_db() as db:
        await db.execute(delete(TeamMembership).where(TeamMembership.user_id == "human-one"))
        await db.execute(update(User).where(User.id == "human-one").values(cognito_sub=None, cognito_username="other-login-alias"))
        db.add(
            User(
                id="login-account", org_id="tenant-two", team_id="", email="login@example.test", cognito_sub=OWNER, cognito_username="canonical-login"
            )
        )
        db.add(
            UserIdentity(
                user_id="human-one", org_id="tenant-one", team_id="", provider="cognito", provider_user_id=OWNER, verification_method="org_placement"
            )
        )
        await db.commit()
    response = await bridge.client.post(URL, headers=HEADERS, json={"name": "search", "arguments": {"query": "info", "product_id": PRODUCT}})
    assert response.status_code == 200
    assert current_platform_admin.call_count == 2
    assert all(call.args == (OWNER, "canonical-login") for call in current_platform_admin.call_args_list)


async def test_admin_still_requires_live_run_workspace_membership(product_db, bridge, current_platform_admin, monkeypatch):  # noqa: F811
    monkeypatch.setattr(service, "locked_door_identity", REAL_IDENTITY)
    current_platform_admin.return_value = True
    async with product_db() as db:
        await db.execute(update(TenantMembership).where(TenantMembership.user_id == "human-one").values(is_active=False))
        await db.commit()
    response = await bridge.client.post(URL, headers=HEADERS, json={"name": "search", "arguments": {"query": "info", "product_id": PRODUCT}})
    assert response.status_code == 404
    assert not bridge.upstream
    current_platform_admin.assert_not_called()


@pytest.mark.parametrize("demotion", ["durable", "idp"])
async def test_admin_demotion_removes_access_without_product_team(product_db, bridge, current_platform_admin, monkeypatch, demotion):  # noqa: F811
    monkeypatch.setattr(service, "locked_door_identity", REAL_IDENTITY)
    current_platform_admin.return_value = True
    async with product_db() as db:
        await db.execute(delete(TeamMembership).where(TeamMembership.user_id == "human-one"))
        await db.execute(update(User).where(User.id == "human-one").values(role="platform_admin"))
        await set_platform_admin_grant(db, OWNER, enabled=True)
        await db.commit()
    body = {"name": "search", "arguments": {"query": "info", "product_id": PRODUCT}}
    assert (await bridge.client.post(URL, headers=HEADERS, json=body)).status_code == 200
    if demotion == "durable":
        async with product_db() as db:
            await set_platform_admin_grant(db, OWNER, enabled=False)
            await db.commit()
    else:
        current_platform_admin.return_value = False
    response = await bridge.client.post(URL, headers=HEADERS, json=body)
    assert response.status_code == 404
    assert len(bridge.upstream) == 1


async def test_admin_demotion_during_query_prevents_result_release(product_db, bridge, current_platform_admin, monkeypatch):  # noqa: F811
    monkeypatch.setattr(service, "locked_door_identity", REAL_IDENTITY)
    current_platform_admin.return_value = True
    async with product_db() as db:
        await db.execute(delete(TeamMembership).where(TeamMembership.user_id == "human-one"))
        await db.commit()
    original_forward = service.forward_door

    async def demote_after_query(*args):
        response = await original_forward(*args)
        async with product_db() as db:
            await set_platform_admin_grant(db, OWNER, enabled=False)
            await db.commit()
        return response

    monkeypatch.setattr(service, "forward_door", demote_after_query)
    response = await bridge.client.post(URL, headers=HEADERS, json={"name": "search", "arguments": {"query": "info", "product_id": PRODUCT}})
    assert response.status_code == 404
    assert len(bridge.upstream) == 1
    assert "own-private-data" not in response.text


@pytest.mark.parametrize("table", ["product", "membership"])
async def test_product_grant_holds_membership_and_product_assignment_through_use(product_db, table):
    from sqlalchemy.exc import DBAPIError

    async with REAL_IDENTITY(RECORD, GRANT, product_id=PRODUCT) as identity:
        assert identity["x-product-id"] == PRODUCT
        async with product_db() as db:
            await db.execute(text("SET LOCAL lock_timeout = '150ms'"))
            statement = (
                update(Product).where(Product.id == PRODUCT).values(team_id="other-team")
                if table == "product"
                else delete(TeamMembership).where(TeamMembership.user_id == "human-one")
            )
            with pytest.raises(DBAPIError) as error:
                await db.execute(statement)
            assert error.value.orig.sqlstate == "55P03"
            await db.rollback()


@pytest.mark.parametrize(
    "operation,args",
    [
        ("search", {"product_id": "bad"}),
        ("browse", {"product_id": PRODUCT}),
        ("search", {"product_id": PRODUCT, "scope": "memory"}),
        ("search", {"product_id": PRODUCT, "project": "legacy-project"}),
    ],
)
def test_product_selector_is_strict_and_not_a_legacy_project_alias(operation, args):
    with pytest.raises(HTTPException):
        requested_product(operation, args)


def test_product_selector_supports_both_request_envelopes():
    import json

    params = {"name": "search", "arguments": {"product_id": PRODUCT}}
    assert product_from_body(json.dumps(params).encode(), "/call") == PRODUCT
    assert product_from_body(json.dumps({"method": "tools/call", "params": params}).encode(), "/mcp/") == PRODUCT
