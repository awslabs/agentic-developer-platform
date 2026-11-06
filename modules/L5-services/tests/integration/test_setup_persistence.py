"""The production schema enforces immediate parent/child foreign keys."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.api.bindings import BindingStore
from l5_services.models.base import Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration
from l5_services.models.setup import BindingOperation, SetupIntent, SetupWork


@pytest.fixture
async def selected():
    engine = create_async_engine("sqlite+aiosqlite://")

    @event.listens_for(engine.sync_engine, "connect")
    def enforce_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    scope = {"tenant_id": "tenant", "product_id": "product", "project_id": "project",
             "owner_id": "owner", "relationship_revision": "1", "visibility": "only_me"}
    reference = {"tenant_id": "tenant", "project_id": "project", "source_connection_id": "connection",
                 "record_type": "connection", "record_id": "connection", "record_revision": "1"}
    verified = {"scope": scope, "configuration_id": "config", "configuration_revision": 2,
                "connection_ref": reference, "account_id": "123456789012", "region": "us-east-1",
                "role_arn": "arn:aws:iam::123456789012:role/selected",
                "authorization": {"actor_id": "owner", "delegation_id": "delegation", "delegation_revision": 1}}
    async with sessions() as session:
        session.add(Configuration(tenant_id="tenant", configuration_id="config", product_id="product", project_id="project",
                                  service_id="devops", relationship_revision="1", owner_id="owner", current_revision=2,
                                  handoff_state="linked"))
        await session.commit()
    yield SimpleNamespace(engine=engine, sessions=sessions, verified=verified)
    await engine.dispose()


async def counts(session):
    return [await session.scalar(select(func.count()).select_from(model)) for model in (SetupIntent, BindingOperation, SetupWork)]


async def seed_binding(selected, service):
    async with selected.sessions() as session:
        session.add(Binding(tenant_id="tenant", binding_id="binding", configuration_id="config", product_id="product", project_id="project",
                            service_id=service, scope=selected.verified["scope"], revision=1, generation=1, state="ready", ownership="customer_linked",
                            connection_ref=selected.verified["connection_ref"], connection_key="connection", native_key="native",
                            native_resource_ref={**selected.verified["connection_ref"], "record_type": "agent_space", "record_id": "space"},
                            provider_identity={key: selected.verified[key] for key in ("account_id", "role_arn", "region")}, readiness=[], gaps=[]))
        await session.commit()


@pytest.mark.parametrize("service", ["devops", "security"])
@pytest.mark.parametrize("action", ["create", "link", "check"])
async def test_setup_and_control_are_atomic_and_replay_with_foreign_keys_enforced(selected, service, action):
    request = {"action": action, "configuration_revision": 2}
    if action == "link":
        request["native_resource_ref"] = {**selected.verified["connection_ref"], "record_type": "agent_space", "record_id": "space"}
    if action == "check":
        await seed_binding(selected, service)
        request["binding_id"] = "binding"
    async with selected.sessions() as session:
        assert (await session.execute(text("PRAGMA foreign_keys"))).scalar() == 1
        store = BindingStore(session, "tenant", "product", "project", service)
        async def submit():
            if action == "check":
                return await store.control(request, "same-key", '"1"', selected.verified)
            return await store.start(request, "same-key", selected.verified)
        first = await submit()
        assert first.state == "accepted" and await counts(session) == [1, 1, 1]
        replay = await submit()
        assert replay.operation_id == first.operation_id and await counts(session) == [1, 1, 1]
        intent = await session.get(SetupIntent, ("tenant", first.operation_id))
        work = await session.scalar(select(SetupWork))
        assert work.operation_id == intent.operation_id == first.operation_id
        assert work.generation == intent.binding_generation == (2 if action == "check" else 1)


@pytest.mark.parametrize("action", ["create", "check"])
async def test_child_constraint_failure_rolls_back_parent_work_and_binding_generation(selected, action, caplog):
    if action == "check":
        await seed_binding(selected, "devops")
    async with selected.engine.begin() as connection:
        await connection.execute(text("""CREATE TRIGGER reject_setup_work BEFORE INSERT ON l5_service_setup_work
                                         BEGIN SELECT RAISE(ABORT, 'offline child rejection'); END"""))
    async with selected.sessions() as session:
        store = BindingStore(session, "tenant", "product", "project", "devops")
        with pytest.raises(HTTPException) as failed:
            if action == "check":
                await store.control({"action": "check", "configuration_revision": 2, "binding_id": "binding"}, "retained-key", '"1"', selected.verified)
            else:
                await store.start({"action": "create", "configuration_revision": 2}, "retained-key", selected.verified)
        assert failed.value.status_code == 500 and failed.value.detail["code"] == "setup_persistence_failed"
        assert "original request key" in failed.value.detail["message"] and "platform support" in failed.value.detail["message"]
        assert await counts(session) == [0, 0, 0]
        if action == "check":
            binding = await session.get(Binding, ("tenant", "binding"))
            assert (binding.revision, binding.generation, binding.state) == (1, 1, "ready")
        record = next(record for record in caplog.records if getattr(record, "event", None) == "l5_setup_persistence_failed")
        assert record.service_id == "devops" and record.operation_id
        assert "offline child rejection" not in caplog.text  # No SQL/parameters or raw database error is logged.


async def test_changed_same_key_authority_remains_an_idempotency_conflict(selected):
    async with selected.sessions() as session:
        store = BindingStore(session, "tenant", "product", "project", "devops")
        request = {"action": "create", "configuration_revision": 2}
        await store.start(request, "same-key", selected.verified)
        changed = deepcopy(selected.verified)
        changed["authorization"]["delegation_revision"] = 2
        with pytest.raises(HTTPException) as conflict:
            await store.start(request, "same-key", changed)
        assert conflict.value.status_code == 409 and conflict.value.detail["code"] == "idempotency_conflict"
        assert "reuse its original request" in conflict.value.detail["message"]
        assert await counts(session) == [1, 1, 1]


async def test_duplicate_request_race_preserves_original_rows_and_reports_conflict(selected, monkeypatch):
    async with selected.sessions() as session:
        store = BindingStore(session, "tenant", "product", "project", "devops")
        request = {"action": "create", "configuration_revision": 2}
        original = await store.start(request, "same-key", selected.verified)
        operation_id = original.operation_id
        scalar = session.scalar
        miss_once = True

        async def concurrent_lookup(statement, *args, **kwargs):
            nonlocal miss_once
            if miss_once and statement.column_descriptions[0].get("entity") is SetupIntent:
                miss_once = False
                return None  # Another transaction inserts this key after the lookup.
            return await scalar(statement, *args, **kwargs)

        monkeypatch.setattr(session, "scalar", concurrent_lookup)
        with pytest.raises(HTTPException) as conflict:
            await store.start(request, "same-key", selected.verified)
        assert conflict.value.status_code == 409 and conflict.value.detail["code"] == "idempotency_conflict"
        assert conflict.value.detail["operation_id"] == operation_id
        assert await counts(session) == [1, 1, 1]
        assert (await session.scalar(select(BindingOperation))).operation_id == operation_id


@pytest.mark.parametrize("collision", ["intent_primary", "work_primary"])
async def test_unrelated_unique_constraint_is_not_reported_as_request_key_conflict(selected, monkeypatch, collision):
    async with selected.sessions() as session:
        store = BindingStore(session, "tenant", "product", "project", "devops")
        request = {"action": "create", "configuration_revision": 2}
        original_id = (await store.start(request, "original-key", selected.verified)).operation_id
        original_work_id = (await session.scalar(select(SetupWork))).work_id
        generated = iter([original_id if collision == "intent_primary" else str(uuid4()),
                          original_work_id if collision == "work_primary" else str(uuid4())])
        monkeypatch.setattr("l5_services.api.bindings.uuid4", lambda: next(generated))
        with pytest.raises(HTTPException) as failure:
            await store.start(request, "different-key", selected.verified)
        assert failure.value.status_code == 500 and failure.value.detail["code"] == "setup_persistence_failed"
        assert await counts(session) == [1, 1, 1]
        assert (await session.scalar(select(BindingOperation))).operation_id == original_id
