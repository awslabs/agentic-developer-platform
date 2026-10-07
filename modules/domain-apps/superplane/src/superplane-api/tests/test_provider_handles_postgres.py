"""Real PostgreSQL row-lock regressions on an isolated disposable test database.

The shared fixture starts pgserver, or uses an explicit SUPERPLANE_TEST_POSTGRES_URL
override. Each test creates and removes its own random schema; no provider, cloud
or B service is contacted.
"""

import asyncio
import importlib.util
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from superplane_contracts import (
    CallOutcome,
    OperationKind,
    ProviderHandle,
    ProviderObservation,
    ProviderPresence,
    ReconcileResult,
    Submitter,
)

from app.database import Base
from app.models.organization import Organization
from app.models.provider_handle import (
    ProviderAllocation,
    ProviderAllocationResource,
    ProviderOperation,
)
from app.models.workspace import Workspace
from app.services import provider_authority, provider_handles, provider_inventory
from tests.test_installation_postgres import (
    installation_postgres_url as installation_postgres_url,
)
from tests.test_installation_postgres import pytestmark as postgres_available

# CI installs pgserver and must execute these races. A missing/broken disposable
# server must fail fixture setup there, rather than turn the lane green with skips.
pytestmark = [] if os.environ.get("CI") else postgres_available


@pytest.fixture
async def postgres(monkeypatch, installation_postgres_url):  # noqa: F811 - pytest fixture injection
    url = installation_postgres_url
    schema = "provider_test_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        url,
        connect_args={
            "server_settings": {"search_path": schema, "statement_timeout": "10000"}
        },
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace, org = uuid.uuid4(), uuid.uuid4()
    handle = ProviderHandle(
        operation=OperationKind.PROVISION,
        provider="aws",
        resource_name="node",
        idempotency_key="operation-1",
        allocation_id="allocation-1",
        workspace=str(workspace),
    )
    submitter = Submitter(
        submitter_id="executor", workspaces=frozenset({str(workspace)})
    )

    class Validator:
        async def resolve(self, authority, *, submitter, handle):
            return provider_authority.VerifiedProviderAuthority(
                operation_id=handle.idempotency_key,
                run_id="run",
                attempt_id="attempt",
                submitter_id=submitter.submitter_id,
                handle=handle,
                expires_at=datetime.now(UTC) + timedelta(minutes=1),
                active=True,
            )

    monkeypatch.setattr(provider_authority, "_validator", Validator())
    try:
        spec = importlib.util.spec_from_file_location(
            "provider_migration",
            Path(__file__).parents[1]
            / "alembic/versions/013_add_provider_operations.py",
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        def prepare(connection):
            provider_tables = {
                "provider_allocations",
                "provider_operations",
                "provider_reference_conflicts",
                "provider_allocation_resources",
            }
            Base.metadata.create_all(
                connection,
                tables=[
                    table
                    for table in Base.metadata.sorted_tables
                    if table.name not in provider_tables
                ],
            )
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()

        async with engine.begin() as connection:
            await connection.run_sync(prepare)
        async with sessions() as session:
            session.add(Organization(id=org, name="postgres-test"))
            await session.flush()
            session.add(
                Workspace(
                    id=workspace, org_id=org, name="dev", isolation_mode="dedicated"
                )
            )
            await session.commit()
            await provider_handles.record_handle(
                session,
                submitter=submitter,
                handle=handle,
                operation_authority="test-b-authority",
            )
        yield sessions, handle, submitter, str(org)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


def member(resource_id, kind="compute"):
    return provider_inventory.AllocationResourceIdentity(
        resource_id=resource_id,
        provider="aws",
        provider_reference=resource_id,
        kind=kind,
        operation_keys=frozenset({"operation-1"})
        if resource_id == "node"
        else frozenset(),
    )


def absent(resource_id):
    return ProviderObservation(presence=ProviderPresence.ABSENT, queried_by=resource_id)


def inventory(handle, submitter, org, digest, resources, expires_at):
    return provider_inventory.VerifiedAllocationInventory(
        workspace=handle.workspace,
        org_id=org,
        allocation_id=handle.allocation_id,
        executor_id=submitter.submitter_id,
        active=True,
        attested_report_digest=digest,
        revision="test-fence",
        resources=resources,
        complete=True,
        expires_at=expires_at,
    )


async def assess(session, handle, submitter, observations):
    return await provider_handles.assess_allocation_release(
        session,
        submitter=submitter,
        workspace=handle.workspace,
        allocation_id=handle.allocation_id,
        observations=observations,
        operation_authority="test-b-authority",
    )


async def wait_for_database_lock(sessions, pid):
    # Observe a real PostgreSQL wait, not just an asyncio task that has started.
    async with asyncio.timeout(5):
        while True:
            async with sessions() as observer:
                event = await observer.scalar(
                    text(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": pid},
                )
            if event == "Lock":
                return
            await asyncio.sleep(0.02)


async def test_authority_expiring_during_allocation_lock_wait_is_refused(
    postgres, monkeypatch
):
    sessions, handle, submitter, org = postgres
    called = asyncio.Event()
    expires = datetime.now(UTC) + timedelta(seconds=1)

    class Reader:
        async def read(self, **kwargs):
            called.set()
            return inventory(
                handle,
                submitter,
                org,
                kwargs["report_digest"],
                (member("node"),),
                expires,
            )

    monkeypatch.setattr(provider_inventory, "_reader", Reader())
    async with sessions() as holder, sessions() as waiter:
        await holder.get(
            ProviderAllocation,
            {"workspace": handle.workspace, "allocation_id": handle.allocation_id},
            with_for_update=True,
        )
        pid = await waiter.scalar(text("SELECT pg_backend_pid()"))

        async def assert_refused():
            with pytest.raises(
                provider_handles.HandleRefused, match="allocation authority"
            ):
                await assess(waiter, handle, submitter, {"node": absent("node")})

        async with asyncio.TaskGroup() as tasks:
            task = tasks.create_task(assert_refused())
            try:
                await wait_for_database_lock(sessions, pid)
                assert not called.is_set(), (
                    "B inventory must be read after acquiring the allocation lock"
                )
                await asyncio.sleep(
                    max(0, (expires - datetime.now(UTC)).total_seconds()) + 0.05
                )
            finally:
                await holder.rollback()
            await task
    assert called.is_set()


async def test_overlapping_snapshots_cannot_erase_volume_exposure(
    postgres, monkeypatch
):
    sessions, handle, submitter, org = postgres
    first_reader = asyncio.Event()
    release_reader = asyncio.Event()

    class Reader:
        calls = 0

        async def read(self, **kwargs):
            self.calls += 1
            resources = (member("node"),)
            if self.calls == 1:
                resources += (member("volume", "storage"),)
                first_reader.set()
                await asyncio.wait_for(release_reader.wait(), 5)
            return inventory(
                handle,
                submitter,
                org,
                kwargs["report_digest"],
                resources,
                datetime.now(UTC) + timedelta(minutes=1),
            )

    reader = Reader()
    monkeypatch.setattr(provider_inventory, "_reader", reader)
    async with sessions() as first, sessions() as second:
        # Prime the second session's ORM cache before the first writes membership.
        await second.get(
            ProviderOperation,
            {"workspace": handle.workspace, "idempotency_key": handle.idempotency_key},
        )
        pid = await second.scalar(text("SELECT pg_backend_pid()"))
        async with asyncio.TaskGroup() as tasks:
            one = tasks.create_task(
                assess(
                    first,
                    handle,
                    submitter,
                    {
                        "node": absent("node"),
                        "volume": ProviderObservation(
                            presence=ProviderPresence.PRESENT,
                            queried_by="volume",
                            provider_state="available",
                        ),
                    },
                )
            )
            await asyncio.wait_for(first_reader.wait(), 5)
            two = tasks.create_task(
                assess(second, handle, submitter, {"node": absent("node")})
            )
            try:
                await wait_for_database_lock(sessions, pid)
                assert reader.calls == 1
            finally:
                release_reader.set()
        assert not one.result().may_mark_released
        assert not two.result().may_mark_released
        assert "volume" in two.result().unresolved_resources
    async with sessions() as session:
        assert set(
            (
                await session.scalars(select(ProviderAllocationResource.resource_id))
            ).all()
        ) == {"node", "volume"}


async def test_authority_expiring_during_commit_cannot_return_release(
    postgres, monkeypatch
):
    sessions, handle, submitter, org = postgres
    expires = datetime.now(UTC) + timedelta(seconds=0.3)

    class Reader:
        async def read(self, **kwargs):
            return inventory(
                handle,
                submitter,
                org,
                kwargs["report_digest"],
                (member("node"),),
                expires,
            )

    monkeypatch.setattr(provider_inventory, "_reader", Reader())
    async with sessions() as session:
        commit = session.commit

        async def delayed_commit():
            await commit()
            await asyncio.sleep(
                max(0, (expires - datetime.now(UTC)).total_seconds()) + 0.05
            )

        monkeypatch.setattr(session, "commit", delayed_commit)
        with pytest.raises(provider_handles.HandleRefused, match="authority expired"):
            await assess(session, handle, submitter, {"node": absent("node")})


async def test_release_decision_precedes_waiting_inventory_write(postgres, monkeypatch):
    sessions, handle, submitter, org = postgres
    membership_written = asyncio.Event()
    continue_decision = asyncio.Event()
    decision_derived = asyncio.Event()
    second_finished = asyncio.Event()

    class Reader:
        calls = 0

        async def read(self, **kwargs):
            self.calls += 1
            resources = (member("node"),)
            if self.calls == 2:
                assert decision_derived.is_set(), "waiter overtook the release decision"
                resources += (member("volume", "storage"),)
            return inventory(
                handle,
                submitter,
                org,
                kwargs["report_digest"],
                resources,
                datetime.now(UTC) + timedelta(minutes=1),
            )

    reader = Reader()
    monkeypatch.setattr(provider_inventory, "_reader", reader)
    original_assess = provider_handles.assess_release

    def derive(*args, **kwargs):
        result = original_assess(*args, **kwargs)
        if reader.calls == 1:
            decision_derived.set()
        return result

    monkeypatch.setattr(provider_handles, "assess_release", derive)
    async with sessions() as first, sessions() as second:
        flush, commit = first.flush, first.commit

        async def paused_flush(*args, **kwargs):
            writes_membership = any(
                isinstance(row, ProviderAllocationResource) for row in first.new
            )
            await flush(*args, **kwargs)
            if writes_membership:
                membership_written.set()
                await asyncio.wait_for(continue_decision.wait(), 5)

        async def checked_commit():
            assert decision_derived.is_set(), "allocation lock dropped before decision"
            await commit()
            await asyncio.wait_for(second_finished.wait(), 5)

        monkeypatch.setattr(first, "flush", paused_flush)
        monkeypatch.setattr(first, "commit", checked_commit)
        pid = await second.scalar(text("SELECT pg_backend_pid()"))

        async def second_assessment():
            try:
                return await assess(second, handle, submitter, {"node": absent("node")})
            finally:
                second_finished.set()

        async with asyncio.TaskGroup() as tasks:
            one = tasks.create_task(
                assess(first, handle, submitter, {"node": absent("node")})
            )
            await asyncio.wait_for(membership_written.wait(), 5)
            two = tasks.create_task(second_assessment())
            try:
                await wait_for_database_lock(sessions, pid)
                assert reader.calls == 1
                assert not decision_derived.is_set()
            finally:
                continue_decision.set()
        assert one.result().may_mark_released
        assert not two.result().may_mark_released
        assert "volume" in two.result().unresolved_resources


async def test_conclusion_refreshes_reference_after_allocation_wait(postgres):
    sessions, handle, submitter, org = postgres
    async with sessions() as holder, sessions() as waiter:
        await holder.get(
            ProviderAllocation,
            {"workspace": handle.workspace, "allocation_id": handle.allocation_id},
            with_for_update=True,
        )
        row = await holder.get(
            ProviderOperation,
            {"workspace": handle.workspace, "idempotency_key": handle.idempotency_key},
        )
        row.provider_reference = "provider-returned-id"
        await holder.flush()
        pid = await waiter.scalar(text("SELECT pg_backend_pid()"))
        async with asyncio.TaskGroup() as tasks:
            task = tasks.create_task(
                provider_handles.conclude_operation(
                    waiter,
                    submitter=submitter,
                    workspace=handle.workspace,
                    idempotency_key=handle.idempotency_key,
                    outcome=CallOutcome.AMBIGUOUS,
                    operation_authority="test-b-authority",
                    observation=absent("provider-returned-id"),
                )
            )
            try:
                await wait_for_database_lock(sessions, pid)
            finally:
                await holder.commit()
        saved, applied, result = task.result()
        assert saved.provider_reference == "provider-returned-id"
        assert result == ReconcileResult.RETRY_PERMITTED


async def test_long_provider_state_and_conflict_survive_postgresql(postgres):
    from superplane_contracts import CallOutcome

    sessions, handle, submitter, _ = postgres
    provider_state = "provider phase detail " * 60
    async with sessions() as session:
        row, applied, _ = await provider_handles.conclude_operation(
            session,
            submitter=submitter,
            workspace=handle.workspace,
            idempotency_key=handle.idempotency_key,
            outcome=CallOutcome.SUCCEEDED,
            operation_authority="test-b-authority",
            provider_reference="instance-1",
            observation=ProviderObservation(
                presence=ProviderPresence.PRESENT,
                queried_by="instance-1",
                provider_state=provider_state,
            ),
        )
        assert applied
        assert row.provider_state == provider_state
    async with sessions() as session:
        with pytest.raises(provider_handles.HandleRefused) as conflict:
            await provider_handles.conclude_operation(
                session,
                submitter=submitter,
                workspace=handle.workspace,
                idempotency_key=handle.idempotency_key,
                outcome=CallOutcome.SUCCEEDED,
                operation_authority="test-b-authority",
                provider_reference="instance-2",
                observation=ProviderObservation(
                    presence=ProviderPresence.PRESENT,
                    queried_by="instance-2",
                    provider_state=provider_state,
                ),
            )
        assert conflict.value.status_code == 409
    async with sessions() as session:
        recovered = await provider_handles.load_operation(
            session,
            submitter=submitter,
            workspace=handle.workspace,
            idempotency_key=handle.idempotency_key,
        )
        assert recovered.provider_state == provider_state
        assert recovered.conflicts[0].provider_state == provider_state
        assert recovered.conflicts[0].provider_reference == "instance-2"
