"""Product edit scenarios with independent database readback."""

import pytest
from sqlalchemy import func, select

from src.auth.dependencies import get_current_user
from src.products.models import Product, ProductChange, ProductOperation
from src.shared.models.organization import Organization, User
from tests.products.membership import add_team_member


@pytest.fixture
async def editors(app, db_session, valid_token_context):
    owner = valid_token_context
    colleague = owner.model_copy(update={"user_id": "colleague-test-123"})
    db_session.add(Organization(id=owner.org_id, name="Test workspace"))
    db_session.add_all(
        [
            User(id=owner.user_id, org_id=owner.org_id, team_id=owner.team_id, email="owner@example.test", user_kind="human"),
            User(id=colleague.user_id, org_id=owner.org_id, team_id=owner.team_id, email="colleague@example.test", user_kind="human"),
        ]
    )
    await add_team_member(db_session, owner)
    await add_team_member(db_session, colleague)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: owner
    return owner, colleague


@pytest.mark.asyncio
async def edit_race(test_client, db_session, test_engine, app, editors):
    import asyncio

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.app import create_app
    from src.shared.database import get_db

    owner, colleague = editors
    created = await test_client.post(
        "/v1/products",
        json={
            "name": "Example Planner",
            "purpose": "Coordinate releases",
            "intended_users": ["Maintainers"],
            "starting_point": "idea",
            "project": {"mode": "create"},
        },
        headers={"Idempotency-Key": "create_edit_race_12345"},
    )
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    product = await db_session.scalar(select(Product).where(Product.id == product_id))
    product.team_id = owner.team_id
    await db_session.commit()
    url = f"/v1/products/{product_id}/projects/{project_id}"
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    colleague_app = create_app()
    lock = asyncio.Lock()

    async def new_session():
        async with lock:
            async with session_factory() as session:
                yield session

    app.dependency_overrides[get_db] = new_session
    colleague_app.dependency_overrides[get_db] = new_session
    colleague_app.dependency_overrides[get_current_user] = lambda: colleague
    entered = 0
    ready = asyncio.Event()

    async def simultaneous_edit(client, name, key):
        nonlocal entered
        entered += 1
        if entered == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=5)
        return await client.patch(
            url,
            json={"name": name},
            headers={
                "Idempotency-Key": key,
                "If-Profile-Revision": "1",
            },
        )

    async with AsyncClient(transport=ASGITransport(app=colleague_app), base_url="http://test") as colleague_client:
        responses = await asyncio.gather(
            simultaneous_edit(test_client, "Owner name", "edit_race_owner_12345"),
            simultaneous_edit(colleague_client, "Colleague name", "edit_race_colleague_12345"),
        )
    assert entered == 2
    winners = [response for response in responses if response.status_code == 200]
    losers = [response for response in responses if response.status_code == 412]
    assert len(winners) == len(losers) == 1, [(response.status_code, response.text) for response in responses]
    assert losers[0].json()["detail"] == {"code": "stale_revision", "conflict_paths": ["/product/name"]}
    current = await test_client.get(url)
    assert current.status_code == 200
    assert current.json()["product"]["name"] == winners[0].json()["product"]["name"]
    assert current.headers["Profile-Revision"] == "2"
    changes = (await db_session.scalars(select(ProductChange).where(ProductChange.product_id == product_id).order_by(ProductChange.revision))).all()
    assert len(changes) == 2
    assert changes[0].revision == 1
    assert changes[1].actor_id in {owner.user_id, colleague.user_id}
    assert changes[1].changed_paths == ["/product/name"]
    assert changes[1].snapshot["product"]["name"] == current.json()["product"]["name"]
    assert await db_session.scalar(select(func.count()).select_from(ProductOperation)) == 2


async def new_product(test_client):
    created = await test_client.post(
        "/v1/products",
        json={
            "name": "Example Planner",
            "purpose": "Coordinate releases",
            "intended_users": ["Maintainers"],
            "starting_point": "idea",
            "project": {"mode": "create"},
        },
        headers={"Idempotency-Key": "create_retry_edit_12345"},
    )
    assert created.status_code == 201, created.text
    return f"/v1/products/{created.json()['product_id']}/projects/{created.json()['project_id']}"


@pytest.mark.asyncio
async def test_edit_receipt_from_new_app(test_client, db_session, test_engine, editors):
    from datetime import UTC, datetime, timedelta

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.app import create_app
    from src.shared.database import get_db

    owner, _ = editors
    url = await new_product(test_client)
    headers = {"Idempotency-Key": "lost_response_edit_12345", "If-Profile-Revision": "1"}
    first = await test_client.patch(url, json={"purpose": "New purpose"}, headers=headers)
    assert first.status_code == 200, first.text
    restarted = create_app()
    restarted.dependency_overrides[get_current_user] = lambda: owner
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def new_session():
        async with session_factory() as session:
            yield session

    restarted.dependency_overrides[get_db] = new_session
    async with AsyncClient(transport=ASGITransport(app=restarted), base_url="http://test") as fresh_client:
        replay = await fresh_client.patch(url, json={"purpose": "New purpose"}, headers=headers)
        assert replay.status_code == 200, replay.text
        assert replay.json() == first.json()
        assert replay.headers["Profile-Revision"] == first.headers["Profile-Revision"] == "2"
        assert (await fresh_client.get(url)).json()["product"]["purpose"] == "New purpose"
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 2
    assert await db_session.scalar(select(func.count()).select_from(ProductOperation)) == 2
    change = await db_session.scalar(select(ProductChange).where(ProductChange.revision == 2))
    assert change.actor_id == owner.user_id

    no_op = await test_client.patch(
        url,
        json={"purpose": "New purpose"},
        headers={
            "Idempotency-Key": "no_op_receipt_edit_12345",
            "If-Profile-Revision": "2",
        },
    )
    assert no_op.status_code == 200, no_op.text
    assert (
        await test_client.patch(
            url,
            json={"purpose": "New purpose"},
            headers={
                "Idempotency-Key": "no_op_receipt_edit_12345",
                "If-Profile-Revision": "2",
            },
        )
    ).json() == no_op.json()
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 2
    assert await db_session.scalar(select(func.count()).select_from(ProductOperation)) == 3

    receipt = await db_session.scalar(
        select(ProductOperation).where(ProductOperation.operation == "updateProduct").order_by(ProductOperation.created_at)
    )
    receipt.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    expired = await test_client.patch(url, json={"purpose": "New purpose"}, headers=headers)
    assert expired.status_code == 410 and expired.json()["detail"]["code"] == "operation_expired"
    original = await test_client.get(f"{url}/revisions/1")
    current = await test_client.get(url)
    updated = await test_client.get(f"{url}/revisions/2")
    assert original.status_code == current.status_code == updated.status_code == 200
    assert original.json()["product"]["purpose"] == "Coordinate releases"
    assert updated.json() == current.json()
    assert current.json()["product"]["purpose"] == "New purpose"
    assert current.headers["Profile-Revision"] == updated.headers["Profile-Revision"] == "2"
    history = (await db_session.scalars(select(ProductChange).order_by(ProductChange.revision))).all()
    assert [change.snapshot["product"]["purpose"] for change in history] == ["Coordinate releases", "New purpose"]


@pytest.mark.asyncio
async def test_edit_key_conflict_and_revoked_access(test_client, db_session, app, editors):
    from datetime import UTC, datetime

    from src.shared.models.onboarding import TenantMembership

    owner, _ = editors
    url = await new_product(test_client)
    headers = {"Idempotency-Key": "key_conflict_edit_12345", "If-Profile-Revision": "1"}
    assert (await test_client.patch(url, json={"name": "Accepted name"}, headers=headers)).status_code == 200
    conflict = await test_client.patch(url, json={"name": "Different name"}, headers=headers)
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 2
    db_session.add(TenantMembership(id="revoked-editor", user_id=owner.user_id, tenant_id=owner.org_id, role="member", revoked_at=datetime.now(UTC)))
    await db_session.commit()
    replay = await test_client.patch(url, json={"name": "Accepted name"}, headers=headers)
    assert replay.status_code == 404 and "Accepted name" not in replay.text


@pytest.mark.asyncio
async def archive_and_history(test_client, db_session, app, editors, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from src.products.models import Project

    owner, colleague = editors
    url = await new_product(test_client)
    project = await db_session.scalar(select(Project))
    product = await db_session.scalar(select(Product))
    product.team_id = owner.team_id
    asset = {"provider": "example", "native_id": "test-owned/external-asset"}
    project.configuration = {"assets": [asset]}
    await db_session.commit()
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: pytest.fail("archive must not call a provider"))
    lifecycle = f"{url}/lifecycle"
    headers = {"Idempotency-Key": "archive_product_12345", "If-Profile-Revision": "1"}
    archive = await test_client.post(lifecycle, json={"action": "archive", "reason": "No longer in use"}, headers=headers)
    assert archive.status_code == 200, archive.text
    assert archive.headers["Profile-Revision"] == "2"
    assert archive.json()["product"]["lifecycle_status"] == "draft"
    assert archive.json()["archived_at"]
    assert (await test_client.post(lifecycle, json={"action": "archive", "reason": "No longer in use"}, headers=headers)).json() == archive.json()
    assert (
        await test_client.patch(
            url,
            json={"name": "Forbidden after archive"},
            headers={
                "Idempotency-Key": "archived_edit_12345",
                "If-Profile-Revision": "2",
            },
        )
    ).status_code == 409
    await db_session.refresh(project)
    assert project.configuration == {"assets": [asset]}
    history = await test_client.get(f"{url}/changes", params={"limit": 1})
    assert history.status_code == 200, history.text
    assert history.json()["items"][0]["revision"] == 1
    assert history.json()["next_cursor"]
    second_page = await test_client.get(f"{url}/changes", params={"limit": 1, "cursor": history.json()["next_cursor"]})
    assert [row["revision"] for row in second_page.json()["items"]] == [2]
    assert second_page.json()["items"][0]["actor_ref"] == {"kind": "user", "id": owner.user_id}
    assert second_page.json()["next_cursor"] is None
    assert "test-owned/external-asset" not in history.text + second_page.text
    assert (await test_client.get(f"{url}/changes?cursor=invalid")).status_code == 400
    original = await test_client.get(f"{url}/revisions/1")
    assert original.status_code == 200, original.text
    assert original.json()["revision"] == 1
    assert original.json()["product"]["name"] == "Example Planner"
    assert "archived_at" not in original.json()
    snapshot = await test_client.get(f"{url}/revisions/2")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.headers["Profile-Revision"] == "2"
    assert snapshot.json()["configuration"]["assets"] == [asset]
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 2

    app.dependency_overrides[get_current_user] = lambda: colleague
    assert (await test_client.get(f"{url}/changes")).status_code == 200
    assert (await test_client.get(f"{url}/revisions/2")).status_code == 200
    assert (
        await test_client.post(
            lifecycle,
            json={"action": "restore", "reason": "Requested"},
            headers={
                "Idempotency-Key": "team_restore_12345",
                "If-Profile-Revision": "2",
            },
        )
    ).status_code == 403

    foreign = owner.model_copy(update={"org_id": "foreign-workspace", "user_id": "foreign-archive-editor"})
    db_session.add(Organization(id=foreign.org_id, name="Foreign workspace"))
    db_session.add(User(id=foreign.user_id, org_id=foreign.org_id, team_id="foreign-team", email="foreign@example.test", user_kind="human"))
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: foreign
    assert (await test_client.get(f"{url}/changes")).status_code == 404
    assert (await test_client.get(f"{url}/revisions/2")).status_code == 404

    app.dependency_overrides[get_current_user] = lambda: owner
    change = await db_session.scalar(select(ProductChange).where(ProductChange.revision == 1))
    change.recorded_at = datetime.now(UTC) - timedelta(days=91)
    await db_session.commit()
    expired = await test_client.get(f"{url}/revisions/1")
    assert expired.status_code == 410 and expired.json()["detail"]["code"] == "revision_expired"
    assert "Example Planner" not in expired.text
    assert (await test_client.get(f"{url}/revisions/2")).status_code == 200
    restore = await test_client.post(
        lifecycle,
        json={"action": "restore", "reason": "Requested"},
        headers={
            "Idempotency-Key": "owner_restore_12345",
            "If-Profile-Revision": "2",
        },
    )
    assert restore.status_code == 200, restore.text
    assert restore.headers["Profile-Revision"] == "3"
    assert "archived_at" not in restore.json()
    await db_session.refresh(project)
    assert project.configuration == {"assets": [asset]}
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 3


@pytest.mark.asyncio
async def lost_response_replay(tmp_path, valid_token_context):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.app import create_app
    from src.shared.database import get_db
    from src.shared.models.base import Base

    database_url = f"sqlite+aiosqlite:///{tmp_path / 'durable-product.db'}"
    initial_engine = create_async_engine(database_url)
    async with initial_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(initial_engine, expire_on_commit=False)() as session:
        session.add(Organization(id=valid_token_context.org_id, name="Persistent test workspace"))
        session.add(
            User(
                id=valid_token_context.user_id,
                org_id=valid_token_context.org_id,
                team_id=valid_token_context.team_id,
                email="restart@example.test",
                user_kind="human",
            )
        )
        await add_team_member(session, valid_token_context)
        await session.commit()

    def application(engine):
        instance = create_app()
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def database_session():
            async with sessions() as session:
                yield session

        instance.dependency_overrides[get_db] = database_session
        instance.dependency_overrides[get_current_user] = lambda: valid_token_context
        return instance

    initial_app = application(initial_engine)
    async with AsyncClient(transport=ASGITransport(app=initial_app), base_url="http://test") as client:
        url = await new_product(client)
        request = {"purpose": "Edited before connection loss"}
        headers = {"Idempotency-Key": "durable_response_lost_12345", "If-Profile-Revision": "1"}
        dropped_response = await client.patch(url, json=request, headers=headers)
        assert dropped_response.status_code == 200
        del dropped_response
    await initial_engine.dispose()

    reopened_engine = create_async_engine(database_url)
    async with async_sessionmaker(reopened_engine, expire_on_commit=False)() as session:
        original = await session.scalar(select(ProductOperation).where(ProductOperation.operation == "updateProduct"))
        assert original is not None
        receipt = original.receipt["profile"]
        assert receipt["product"]["purpose"] == request["purpose"]
        assert original.actor_id == valid_token_context.user_id
        assert await session.scalar(select(func.count()).select_from(ProductChange)) == 2
        assert await session.scalar(select(func.count()).select_from(ProductOperation)) == 2

    reopened_app = application(reopened_engine)
    async with AsyncClient(transport=ASGITransport(app=reopened_app), base_url="http://test") as client:
        replay = await client.patch(url, json=request, headers=headers)
        assert replay.status_code == 200, replay.text
        # Durable receipts hold saved settings; responses project the authorized catalogue.
        persisted_profile = dict(replay.json())
        shared = persisted_profile.pop("shared_profile")
        assert shared["product_id"] == receipt["product_id"] and shared["revision"] == 2
        assert shared["resources"] == []
        persisted_profile["profile_configuration"] = {}
        assert persisted_profile == receipt
        assert replay.headers["Profile-Revision"] == "2"
        current = await client.get(url)
        assert current.status_code == 200
        assert current.json()["revision"] == 2
        assert current.json()["product"]["purpose"] == request["purpose"]
    async with async_sessionmaker(reopened_engine, expire_on_commit=False)() as session:
        assert await session.scalar(select(func.count()).select_from(ProductChange)) == 2
        assert await session.scalar(select(func.count()).select_from(ProductOperation)) == 2
        journal = await session.scalar(select(ProductChange).where(ProductChange.revision == 2))
        assert journal.actor_id == valid_token_context.user_id
        assert journal.snapshot["product"]["purpose"] == request["purpose"]
    await reopened_engine.dispose()


@pytest.mark.asyncio
async def test_product_reassignment_requires_current_admin(test_client, db_session, app, editors):
    from src.shared.models.onboarding import TenantMembership
    from src.shared.models.organization import Team

    owner, colleague = editors
    url = await new_product(test_client)
    headers = {"Idempotency-Key": "reassign_product_12345", "If-Profile-Revision": "1"}
    for patch in ({"owner_user_id": colleague.user_id}, {"team_id": "new-team-test"}):
        denied = await test_client.patch(url, json=patch, headers=headers)
        assert denied.status_code == 403, denied.text
        assert denied.json()["detail"]["code"] == "permission_denied"
    assert await db_session.scalar(select(func.count()).select_from(ProductChange)) == 1
    db_session.add(TenantMembership(id="admin-member", user_id=owner.user_id, tenant_id=owner.org_id, role="org_admin"))
    db_session.add(Team(id="new-team-test", org_id=owner.org_id, department_id="test-department", name="Authorized team"))
    db_session.add(Organization(id="foreign-tenant", name="Foreign workspace"))
    db_session.add(
        User(id="other-tenant-user", org_id="foreign-tenant", team_id="other-tenant-team", email="other-tenant@example.test", user_kind="human")
    )
    db_session.add(Team(id="other-tenant-team", org_id="foreign-tenant", department_id="foreign-department", name="Foreign team"))
    await db_session.commit()
    foreign_owner = await test_client.patch(url, json={"owner_user_id": "other-tenant-user"}, headers=headers)
    assert foreign_owner.status_code == 404
    foreign_team = await test_client.patch(url, json={"team_id": "other-tenant-team"}, headers=headers)
    assert foreign_team.status_code == 404
    await add_team_member(db_session, owner, team_id="new-team-test", primary=False)
    await add_team_member(db_session, colleague, team_id="new-team-test", primary=False)
    await db_session.commit()
    changed_team = await test_client.patch(url, json={"team_id": "new-team-test"}, headers=headers)
    assert changed_team.status_code == 200, changed_team.text
    assert changed_team.json()["product"]["team_id"] == "new-team-test"
    assert changed_team.json()["revision"] == 2
    cleared = await test_client.patch(
        url,
        json={"team_id": None},
        headers={
            "Idempotency-Key": "remove_team_12345",
            "If-Profile-Revision": "2",
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["revision"] == 3
    assert (await test_client.get(url)).status_code == 404
    # Once teamless, only a verified platform administrator can assign it again.
    app.dependency_overrides[get_current_user] = lambda: owner.model_copy(update={"is_admin": True})
    assert (
        await test_client.patch(
            url,
            json={"team_id": "new-team-test"},
            headers={
                "Idempotency-Key": "return_team_12345",
                "If-Profile-Revision": "3",
            },
        )
    ).status_code == 200
    app.dependency_overrides[get_current_user] = lambda: owner
    changed_owner = await test_client.patch(
        url,
        json={"owner_user_id": colleague.user_id},
        headers={
            "Idempotency-Key": "reassign_owner_12345",
            "If-Profile-Revision": "4",
        },
    )
    assert changed_owner.status_code == 200, changed_owner.text
    assert changed_owner.json()["product"]["owner_ref"]["id"] == colleague.user_id
    assert changed_owner.json()["revision"] == 5
    product = await db_session.scalar(select(Product))
    await db_session.refresh(product)
    assert product.owner_user_id == colleague.user_id and product.team_id == "new-team-test"
    assert (await test_client.get(url)).status_code == 200
    app.dependency_overrides[get_current_user] = lambda: colleague
    assert (await test_client.get(url)).json()["product"]["owner_ref"]["id"] == colleague.user_id
    journal = (await db_session.scalars(select(ProductChange).order_by(ProductChange.revision))).all()
    assert [entry.changed_paths for entry in journal[1:]] == [
        ["/product/team_id"],
        ["/product/team_id"],
        ["/product/team_id"],
        ["/product/owner_user_id"],
    ]


@pytest.mark.asyncio
async def test_predecessor_revision_uses_immutable_create_input(test_client, db_session, editors):
    from datetime import UTC, datetime, timedelta

    from src.products.models import Project

    url = await new_product(test_client)
    change = await db_session.scalar(select(ProductChange).where(ProductChange.revision == 1))
    original = change.snapshot
    change.snapshot = {
        "name": original["product"]["name"],
        "purpose": original["product"]["purpose"],
        "intended_users": original["product"]["intended_users"],
        "starting_point": original["product"]["starting_point"],
        "project": {"mode": "create"},
    }
    await db_session.commit()
    edited = await test_client.patch(
        url,
        json={"name": "New Product name", "purpose": "Updated purpose"},
        headers={
            "Idempotency-Key": "legacy_profile_edit_12345",
            "If-Profile-Revision": "1",
        },
    )
    assert edited.status_code == 200, edited.text
    project = await db_session.scalar(select(Project))
    project.name = "Changed project name"
    await db_session.commit()
    old = await test_client.get(f"{url}/revisions/1")
    assert old.status_code == 200, old.text
    assert old.json()["revision"] == 1
    assert old.json()["product"]["name"] == "Example Planner"
    assert old.json()["product"]["purpose"] == "Coordinate releases"
    assert old.json()["project"]["name"] == "Example Planner"
    assert old.json()["product"]["owner_ref"]["id"] == editors[0].user_id
    assert old.json()["project"]["owner_user_id"] == editors[0].user_id
    assert (await test_client.get(f"{url}/revisions/2")).json()["product"]["name"] == "New Product name"
    change.recorded_at = datetime.now(UTC) - timedelta(days=91)
    await db_session.commit()
    expired = await test_client.get(f"{url}/revisions/1")
    assert expired.status_code == 410
    assert expired.json()["detail"]["code"] == "revision_expired"
    assert "Example Planner" not in expired.text
