"""Product flow filtering covers every Project, pagination, chips and live access."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete

from src.auth.dependencies import get_current_user
from src.products.models import Product
from src.shared.models.organization import TeamMembership, User
from tests.orchestration import test_list_flows as fixtures
from tests.products.membership import add_team_member

session = fixtures.session
app_with_router = fixtures.app_with_router


async def seed_product(session, product_id="product-a", *, org_id=fixtures.ORG_A, team_id="team-a"):
    product = Product(
        tenant_id=org_id,
        id=product_id,
        name=product_id,
        purpose="Delivery",
        intended_users=["Teams"],
        owner_user_id="another-owner",
        team_id=team_id,
        starting_point="idea",
        created_by="another-owner",
    )
    session.add(product)
    await session.flush()
    return product


async def seed_member(session):
    actor = fixtures._token_context(fixtures.ORG_A)
    session.add(User(id=actor.user_id, org_id=actor.org_id, team_id="stale-team", email="member@example.test"))
    await add_team_member(session, actor, team_id="team-a")
    await session.commit()
    return actor


async def test_product_filter_pages_all_projects_and_scopes_status_counts(session, app_with_router):
    await seed_product(session)
    await seed_product(session, "product-b")
    await seed_member(session)
    expected = set()
    for index in range(28):
        flow = await fixtures.seed_flow(session, slug=f"target-{index}", created_offset=index)
        flow.product_id = "product-a"
        flow.project_id = "base-project" if index % 2 else "release-project"
        await fixtures.seed_node(session, flow, node_ref="work", state="passed" if index % 2 else "running")
        expected.add(flow.id)
    other = await fixtures.seed_flow(session, slug="other-product")
    other.product_id = "product-b"
    unbound = await fixtures.seed_flow(session, slug="unbound")
    foreign = await fixtures.seed_flow(session, slug="foreign-tenant", org_id=fixtures.ORG_B)
    foreign.product_id = "product-a"
    archived = await fixtures.seed_flow(session, slug="target-archived")
    archived.product_id = "product-a"
    archived.archived_at = datetime.now(UTC)
    await session.commit()
    client = fixtures.client_for(app_with_router)

    pages = [client.get(fixtures.ROUTE, params={"product_id": "product-a", "offset": offset}) for offset in (0, 25)]
    assert all(page.status_code == 200 for page in pages), [page.text for page in pages]
    assert {row["id"] for page in pages for row in page.json()["flows"]} == expected
    assert {row["project_id"] for page in pages for row in page.json()["flows"]} == {"base-project", "release-project"}
    assert all(page.json()["total"] == 28 for page in pages)
    assert all(page.json()["product_id"] == "product-a" for page in pages)
    chips = pages[0].json()["status_counts"]
    assert chips["running"] == chips["complete"] == 14 and sum(chips.values()) == 28
    filtered = client.get(fixtures.ROUTE, params={"product_id": "product-a", "status": "running", "q": "target-2"})
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 5
    assert filtered.json()["status_counts"] == chips
    history = client.get(fixtures.ROUTE, params={"product_id": "product-a", "status": "archived"})
    assert [row["id"] for row in history.json()["flows"]] == [archived.id]
    assert history.json()["status_counts"]["archived"] == 1
    global_list = client.get(fixtures.ROUTE, params={"limit": 100}).json()
    assert global_list["product_id"] is None
    assert {row["id"] for row in global_list["flows"]} == expected | {other.id, unbound.id}


async def test_product_membership_rechecked_before_cached_list(session, app_with_router, monkeypatch):
    from src.orchestration import read_cache

    await seed_product(session)
    await seed_product(session, "product-b")
    actor = await seed_member(session)
    flow = await fixtures.seed_flow(session, slug="member-flow")
    flow.product_id = "product-a"
    await session.commit()
    # Exercise a warm shared projection without relying on Redis or its TTL.
    cache = {}

    async def cached(_db, *, org_id, operation, parameters, load):
        key = (org_id, operation, *parameters)
        if key not in cache:
            cache[key] = await load()
        return cache[key]

    monkeypatch.setattr(read_cache, "cached_projection", cached)
    client = fixtures.client_for(app_with_router)
    first = client.get(fixtures.ROUTE, params={"product_id": "product-a"})
    assert first.status_code == 200, first.text
    assert [row["id"] for row in first.json()["flows"]] == [flow.id]
    assert client.get(fixtures.ROUTE, params={"product_id": "product-b"}).json()["flows"] == []
    assert len(cache) == 2
    await session.execute(delete(TeamMembership).where(TeamMembership.user_id == actor.user_id))
    await session.commit()
    denied = client.get(fixtures.ROUTE, params={"product_id": "product-a"})
    assert denied.status_code == 404 and flow.title not in denied.text


@pytest.mark.parametrize("product_id", ["missing", "foreign", "other-team"])
async def test_product_filter_does_not_disclose_unavailable_products(session, app_with_router, product_id):
    await seed_member(session)
    await seed_product(session, "foreign", org_id=fixtures.ORG_B)
    await seed_product(session, "other-team", team_id="team-b")
    await session.commit()
    client = fixtures.client_for(app_with_router)
    assert client.get(fixtures.ROUTE, params={"product_id": product_id}).status_code == 404


async def test_platform_admin_can_read_product_without_team_membership_but_not_another_tenant(session, app_with_router):
    await seed_product(session, team_id=None)
    await seed_product(session, "foreign", org_id=fixtures.ORG_B)
    actor = fixtures._token_context(fixtures.ORG_A).model_copy(update={"is_admin": True})
    session.add(User(id=actor.user_id, org_id=actor.org_id, team_id="", email="admin@example.test"))
    await session.commit()
    client = fixtures.client_for(app_with_router)
    # get_current_user supplies verified platform authority in production.
    app_with_router.dependency_overrides[get_current_user] = lambda: actor
    own = client.get(fixtures.ROUTE, params={"product_id": "product-a"})
    assert own.status_code == 200 and own.json()["flows"] == []
    assert client.get(fixtures.ROUTE, params={"product_id": "foreign"}).status_code == 404


async def test_project_filter_scopes_rows_counts_create_context_and_current_binding(session, app_with_router):
    from src.products.models import ProductProjectBinding, Project

    await seed_product(session)
    await seed_product(session, "product-b")
    actor = await seed_member(session)
    for name in ("base-project", "release-project"):
        session.add(Project(id=name, tenant_id=actor.org_id, name=name, owner_user_id=actor.user_id))
        await session.flush()
        session.add(
            ProductProjectBinding(tenant_id=actor.org_id, project_id=name, product_id="product-a", is_base=name == "base-project", provenance={})
        )
    expected = set()
    for index in range(8):
        flow = await fixtures.seed_flow(session, slug=f"project-{index}", created_offset=index)
        flow.product_id = "product-a"
        flow.project_id = "base-project" if index % 2 else "release-project"
        await fixtures.seed_node(session, flow, node_ref="work", state="passed" if index % 2 else "running")
        if not index % 2:
            expected.add(flow.id)
    await session.commit()
    client = fixtures.client_for(app_with_router)
    params = {"product_id": "product-a", "project_id": "release-project", "limit": 2}
    pages = [client.get(fixtures.ROUTE, params={**params, "offset": offset}) for offset in (0, 2)]
    assert all(page.status_code == 200 for page in pages), [page.text for page in pages]
    assert {row["id"] for page in pages for row in page.json()["flows"]} == expected
    for page in pages:
        assert page.json()["total"] == 4
        assert page.json()["project_id"] == "release-project"
        assert page.json()["status_counts"]["complete"] == 0
        assert page.json()["status_counts"]["running"] == 4
    assert client.get(fixtures.ROUTE, params={**params, "product_id": "product-b"}).status_code == 404
    assert client.get(fixtures.ROUTE, params={"project_id": "release-project"}).json()["product_id"] == "product-a"
    assert client.get(fixtures.ROUTE, params={"project_id": "missing"}).status_code == 404
    await session.execute(delete(TeamMembership).where(TeamMembership.user_id == actor.user_id))
    await session.commit()
    assert client.get(fixtures.ROUTE, params=params).status_code == 404
