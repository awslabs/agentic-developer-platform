"""Product UI scenario API readbacks against the real in-process gateway."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.auth.dependencies import get_current_user
from src.products.models import Product, ProductOperation
from src.shared.models.organization import Organization, User
from tests.products.membership import add_team_member


@pytest.fixture
async def owner(db_session, app, valid_token_context):
    db_session.add(Organization(id=valid_token_context.org_id, name="Test workspace"))
    db_session.add(
        User(
            id=valid_token_context.user_id,
            org_id=valid_token_context.org_id,
            team_id=valid_token_context.team_id,
            email="owner@example.test",
            user_kind="human",
        )
    )
    await add_team_member(db_session, valid_token_context)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: valid_token_context
    return valid_token_context


@pytest.mark.asyncio
async def ui_minimal_save(test_client, db_session, owner, monkeypatch):
    monkeypatch.setattr("src.products.routes.utcnow", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    body = {
        "name": "Example Planner",
        "purpose": "Coordinate small releases",
        "intended_users": ["Maintainers"],
        "starting_point": "idea",
        "project": {"mode": "create"},
    }
    created = await test_client.post("/v1/products", json=body, headers={"Idempotency-Key": "ui_minimal_save_001"})
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    readback = await test_client.get(f"/v1/products/{product_id}/projects/{project_id}")
    assert readback.status_code == 200, readback.text
    assert readback.json()["product"]["name"] == body["name"]
    assert readback.json()["product"]["purpose"] == body["purpose"]
    assert readback.json()["product"]["intended_users"] == body["intended_users"]
    assert readback.json()["product"]["owner_ref"] == {"kind": "user", "id": owner.user_id}
    assert readback.json()["project_id"] == project_id
    assert readback.headers["Profile-Revision"] == "1"
    listing = await test_client.get("/v1/products")
    assert listing.status_code == 200
    assert [(item["id"], item["project_ids"]) for item in listing.json()["items"]] == [(product_id, [project_id])]
    product = await db_session.scalar(select(Product).where(Product.id == product_id))
    operation = await db_session.scalar(select(ProductOperation).where(ProductOperation.product_id == product_id))
    assert product is not None and product.owner_user_id == owner.user_id
    assert operation is not None and operation.receipt == {"product_id": product_id, "project_id": project_id, "revision": 1}


@pytest.mark.asyncio
async def ui_shared_edit(test_client, db_session, test_engine, owner, app, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.app import create_app
    from src.shared.database import get_db

    monkeypatch.setattr("src.products.routes.utcnow", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    created = await test_client.post(
        "/v1/products",
        json={
            "name": "Example Planner",
            "purpose": "Original purpose",
            "intended_users": ["Maintainers"],
            "starting_point": "idea",
            "project": {"mode": "create"},
        },
        headers={"Idempotency-Key": "ui_shared_edit_create_001"},
    )
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    url = f"/v1/products/{product_id}/projects/{project_id}"
    product = await db_session.scalar(select(Product).where(Product.id == product_id))
    product.team_id = owner.team_id
    teammate = owner.model_copy(update={"user_id": "teammate-test-123"})
    db_session.add(User(id=teammate.user_id, org_id=owner.org_id, team_id=owner.team_id, email="teammate@example.test", user_kind="human"))
    await add_team_member(db_session, teammate)
    await db_session.commit()
    teammate_app = create_app()
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def teammate_session():
        async with session_factory() as session:
            yield session

    teammate_app.dependency_overrides[get_db] = teammate_session
    teammate_app.dependency_overrides[get_current_user] = lambda: teammate
    async with AsyncClient(transport=ASGITransport(app=teammate_app), base_url="http://test") as second_client:
        assert (await second_client.get(url)).status_code == 200
        first_change = await second_client.patch(
            url,
            json={"purpose": "Teammate's purpose"},
            headers={"Idempotency-Key": "ui_shared_edit_remote_001", "If-Profile-Revision": "1"},
        )
        assert first_change.status_code == 200, first_change.text
        clean_refresh = await test_client.get(url)
        assert clean_refresh.status_code == 200
        assert clean_refresh.json()["product"]["purpose"] == "Teammate's purpose"
        assert clean_refresh.json()["revision"] == 2

        second_change = await second_client.patch(
            url,
            json={"purpose": "Teammate's newest purpose"},
            headers={"Idempotency-Key": "ui_shared_edit_remote_002", "If-Profile-Revision": "2"},
        )
        assert second_change.status_code == 200, second_change.text
        stale = await test_client.patch(
            url,
            json={"purpose": "My unsaved purpose"},
            headers={"Idempotency-Key": "ui_shared_edit_local_001", "If-Profile-Revision": "2"},
        )
        assert stale.status_code == 412
        assert stale.json()["detail"] == {"code": "stale_revision", "conflict_paths": ["/product/purpose"]}
        current = await test_client.get(url)
        assert current.json()["product"]["purpose"] == "Teammate's newest purpose"
        assert current.json()["revision"] == 3
        reviewed = await test_client.patch(
            url,
            json={"purpose": "My unsaved purpose"},
            headers={"Idempotency-Key": "ui_shared_edit_reviewed_001", "If-Profile-Revision": "3"},
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["revision"] == 4
        independent = await second_client.get(url)
        assert independent.status_code == 200
        assert independent.json()["product"]["purpose"] == "My unsaved purpose"
        assert independent.headers["Profile-Revision"] == "4"


@pytest.mark.asyncio
async def ui_input_recovery(test_client, db_session, owner, monkeypatch):
    from sqlalchemy import func

    monkeypatch.setattr("src.products.routes.utcnow", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    request = {
        "name": "Example Planner",
        "purpose": "Coordinate releases",
        "intended_users": ["Maintainers"],
        "starting_point": "idea",
        "project": {"mode": "create"},
    }
    for invalid in ({**request, "name": ""}, {**request, "intended_users": ["Maintainers", "Maintainers"]}):
        rejected = await test_client.post("/v1/products", json=invalid, headers={"Idempotency-Key": "ui_invalid_attempt_001"})
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["detail"]["code"] == "validation_error"
    assert await db_session.scalar(select(func.count()).select_from(Product)) == 0

    headers = {"Idempotency-Key": "ui_recovery_create_001"}
    discarded = await test_client.post("/v1/products", json=request, headers=headers)
    assert discarded.status_code == 201, discarded.text
    replay = await test_client.post("/v1/products", json=request, headers=headers)
    assert replay.status_code == 201
    assert replay.json() == discarded.json()
    product_id, project_id = replay.json()["product_id"], replay.json()["project_id"]
    url = f"/v1/products/{product_id}/projects/{project_id}"
    independent = await test_client.get(url)
    assert independent.status_code == 200
    assert independent.json()["product"]["name"] == request["name"]
    assert independent.json()["product"]["purpose"] == request["purpose"]
    assert independent.json()["product"]["intended_users"] == request["intended_users"]
    assert independent.json()["product"]["owner_ref"]["id"] == owner.user_id
    assert await db_session.scalar(select(func.count()).select_from(Product)) == 1
    assert await db_session.scalar(select(func.count()).select_from(ProductOperation)) == 1

    edit_headers = {"Idempotency-Key": "ui_recovery_edit_001", "If-Profile-Revision": "1"}
    edited = await test_client.patch(url, json={"purpose": "Edited offline"}, headers=edit_headers)
    assert edited.status_code == 200, edited.text
    edit_replay = await test_client.patch(url, json={"purpose": "Edited offline"}, headers=edit_headers)
    assert edit_replay.status_code == 200
    assert edit_replay.json() == edited.json()
    latest = await test_client.get(url)
    assert latest.json()["product"]["purpose"] == "Edited offline"
    assert latest.json()["revision"] == 2
    assert await db_session.scalar(select(func.count()).select_from(ProductOperation)) == 2
