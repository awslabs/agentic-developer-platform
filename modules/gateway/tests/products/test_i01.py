"""HTTP Product onboarding with independent database readback."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, inspect, select
from sqlalchemy.exc import IntegrityError

from src.auth.dependencies import get_current_user
from src.products.models import Product, ProductChange, ProductOperation, ProductProjectBinding, Project
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from tests.products.membership import add_team_member

REQUEST = {
    "name": "Example Planner",
    "purpose": "Coordinate small releases",
    "intended_users": ["Maintainers"],
    "starting_point": "idea",
    "project": {"mode": "create"},
}
HEADERS = {"Idempotency-Key": "run_owned_key_123456"}


@pytest.fixture
async def member(db_session, app, valid_token_context):
    db_session.add(Organization(id=valid_token_context.org_id, name="Test workspace"))
    db_session.add(
        User(
            id=valid_token_context.user_id,
            org_id=valid_token_context.org_id,
            team_id=valid_token_context.team_id,
            email="member@example.test",
            user_kind="human",
        )
    )
    await add_team_member(db_session, valid_token_context)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: valid_token_context
    yield valid_token_context


async def counts(db_session):
    return [
        await db_session.scalar(select(func.count()).select_from(model))
        for model in (Product, Project, ProductProjectBinding, ProductOperation, ProductChange)
    ]


@pytest.mark.asyncio
async def test_persistence_schema(test_engine):
    async with test_engine.connect() as connection:
        assert {"products", "projects", "product_project_bindings", "product_operations", "product_changes"}.issubset(
            set(await connection.run_sync(lambda conn: inspect(conn).get_table_names()))
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_keys", [False, True])
async def minimal_create_replay(test_client, db_session, test_engine, member, monkeypatch, foreign_keys):
    if foreign_keys:
        async with test_engine.connect() as connection:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            assert (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one() == 1
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: observed_at)
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: pytest.fail("Product creation must not call a provider"))
    first = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert first.status_code == 201, first.text
    replay = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()
    assert replay.headers["Profile-Revision"] == first.headers["Profile-Revision"] == "1"
    assert first.headers["Location"].endswith(f"/{first.json()['product_id']}/projects/{first.json()['project_id']}")
    assert first.json()["product"]["purpose"] == REQUEST["purpose"]
    assert await counts(db_session) == [1, 1, 1, 1, 1]
    binding = await db_session.scalar(select(ProductProjectBinding))
    change = await db_session.scalar(select(ProductChange))
    assert binding.valid_from.replace(tzinfo=UTC) == observed_at
    assert binding.mapping_version == "profile-registry-1"
    assert change.relationship_versions[0]["relationship_id"] == binding.id
    assert (await test_client.get("/v1/products")).json()["items"][0]["project_ids"] == [first.json()["project_id"]]

    conflict = await test_client.post("/v1/products", json={**REQUEST, "name": "Changed"}, headers=HEADERS)
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"
    assert await counts(db_session) == [1, 1, 1, 1, 1]


@pytest.mark.asyncio
async def test_create_history_failure_rolls_back_and_allows_retry(test_client, db_session, test_engine, member):
    async with test_engine.connect() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        assert (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one() == 1

    def reject_history(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO product_changes"):
            raise IntegrityError(statement, parameters, RuntimeError("injected history write failure"))

    event.listen(test_engine.sync_engine, "before_cursor_execute", reject_history)
    try:
        with pytest.raises(IntegrityError, match="injected history write failure"):
            await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", reject_history)
    assert await counts(db_session) == [0, 0, 0, 0, 0]

    retry = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert retry.status_code == 201, retry.text
    assert await counts(db_session) == [1, 1, 1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        {"purpose": None},
        {"intended_users": [f"user-{index}" for index in range(101)]},
        {"unexpected_field": "not allowed"},
    ],
)
async def invalid_create_atomic(test_client, db_session, member, invalid):
    request = REQUEST.copy()
    request.update(invalid)
    response = await test_client.post("/v1/products", json=request, headers=HEADERS)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "validation_error"
    assert all("input" not in field for field in response.json()["detail"]["fields"])
    assert await counts(db_session) == [0, 0, 0, 0, 0]


@pytest.mark.asyncio
async def test_product_list_cursor(test_client, member):
    first = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    second = await test_client.post("/v1/products", json={**REQUEST, "name": "Second Product"}, headers={"Idempotency-Key": "run_owned_key_second"})
    assert first.status_code == second.status_code == 201
    first_page = (await test_client.get("/v1/products?limit=1")).json()
    assert len(first_page["items"]) == 1 and first_page["next_cursor"]
    second_page = (await test_client.get("/v1/products", params={"limit": 1, "cursor": first_page["next_cursor"]})).json()
    assert len(second_page["items"]) == 1 and second_page["next_cursor"] is None
    assert {first_page["items"][0]["id"], second_page["items"][0]["id"]} == {first.json()["product_id"], second.json()["product_id"]}
    assert (await test_client.get("/v1/products?cursor=garbage")).status_code == 400


@pytest.mark.asyncio
async def read_scope_denial(test_client, db_session, app, member, valid_token_context):
    first = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert first.status_code == 201, first.text
    ids = first.json()
    path = f"/v1/products/{ids['product_id']}/projects/{ids['project_id']}"
    assert (await test_client.get(path)).status_code == 200
    listing = await test_client.get("/v1/products")
    assert [row["id"] for row in listing.json()["items"]] == [ids["product_id"]]
    assert listing.json()["next_cursor"] is None

    foreign = valid_token_context.model_copy(update={"org_id": "other-workspace", "user_id": "foreign-user"})
    db_session.add(Organization(id=foreign.org_id, name="Other workspace"))
    db_session.add(User(id=foreign.user_id, org_id=foreign.org_id, team_id="other-team", email="other@example.test", user_kind="human"))
    await add_team_member(db_session, foreign, team_id="other-team")
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: foreign
    assert (await test_client.get(path)).status_code == 404
    assert (await test_client.get("/v1/products")).json()["items"] == []
    foreign_created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert foreign_created.status_code == 201
    assert [entry["id"] for entry in (await test_client.get("/v1/products")).json()["items"]] == [foreign_created.json()["product_id"]]

    app.dependency_overrides[get_current_user] = lambda: member
    assert [entry["id"] for entry in (await test_client.get("/v1/products")).json()["items"]] == [ids["product_id"]]
    db_session.add(
        TenantMembership(id="member-revoked", user_id=member.user_id, tenant_id=member.org_id, role="member", revoked_at=datetime.now(UTC))
    )
    await db_session.commit()
    assert (await test_client.get(path)).status_code == 404
    denied_list = await test_client.get("/v1/products")
    assert denied_list.status_code == 403
    assert ids["product_id"] not in denied_list.text
    denied_replay = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert denied_replay.status_code == 403
    assert ids["product_id"] not in denied_replay.text


@pytest.mark.asyncio
async def test_disabled_product_admission_preserves_reads(test_client, monkeypatch, db_session, member):
    monkeypatch.delenv("BG_PRODUCT_PROFILE_ENABLED")
    response = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "product_profile_disabled"
    assert (await test_client.get("/v1/products")).status_code == 200
    assert await counts(db_session) == [0, 0, 0, 0, 0]


@pytest.mark.asyncio
async def test_recovery_retains_product_history_and_replay_receipt(test_client, db_session, member, monkeypatch):
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    original = await counts(db_session)
    monkeypatch.setenv("BG_PRODUCT_PROFILE_ENABLED", "false")
    assert (await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)).status_code == 503
    assert (await test_client.get(f"/v1/products/{product_id}/projects/{project_id}")).status_code == 200
    assert (await test_client.get(f"/v1/products/{product_id}/projects/{project_id}/changes")).status_code == 200
    assert (await test_client.get("/v1/products")).json()["items"][0]["id"] == product_id
    assert await counts(db_session) == original
    monkeypatch.setenv("BG_PRODUCT_PROFILE_ENABLED", "true")
    replay = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert replay.status_code == 201 and replay.json() == created.json()
    assert await counts(db_session) == original
