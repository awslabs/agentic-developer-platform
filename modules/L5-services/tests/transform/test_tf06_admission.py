"""Scheduled Transform uses the same checked admission as manual submissions."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.admission import submit_and_dispatch
from l5_services.events.recovery import repair_pending_events
from l5_services.events.schedules import receive_schedule_tick
from l5_services.events.sources import admit_source_receipt
from l5_services.models import Binding, Configuration, ConfigurationVersion, EventReceipt, Run, Work
from l5_services.models.base import Base
from l5_services.models.receipt import RunReceipt
from test_tf03_attempts import QUALIFIED, SOURCE, TEST_POLICY


NOW = datetime(2026, 1, 1, tzinfo=UTC)
GOAL = {"kind": "refactor", "summary": "Simplify the example widget"}
SCOPE = {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a",
         "relationship_revision": "1", "owner_id": "owner-a", "visibility": "only_me"}
TRIGGER = {"trigger_id": "scheduled-transform", "kind": "schedule", "enabled": True,
           "schedule": "0 * * * *", "capability": "refactor", "binding_id": "binding-a",
           "goal": GOAL, "source_refs": [SOURCE]}
MANUAL_TRIGGER = {"trigger_id": "manual-transform", "kind": "manual", "enabled": True,
                  "capability": "refactor", "binding_id": "binding-a", "source_refs": [SOURCE]}
SETTINGS = {"connection_ref": SOURCE, "executor_binding_ref": SOURCE, "test_policy_ref": SOURCE,
            "region": "example-region", "read_paths": ["src"], "write_paths": ["src/app.py"],
            "excluded_paths": [".git"], "goal_defaults": GOAL}


@pytest.fixture
async def occurrence(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admission.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            session.add_all([
                Configuration(tenant_id="tenant-a", configuration_id="config-a", product_id="product-a",
                              project_id="project-a", service_id="transform", relationship_revision="1",
                              owner_id="owner-a", current_revision=1, handoff_state="linked"),
                ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a", revision=1,
                                     settings={"enabled": True, "triggers": [TRIGGER, MANUAL_TRIGGER], "source_refs": [SOURCE]},
                                     inherited_fields=[], gaps=[], state="ready", hash="a" * 64),
                Binding(tenant_id="tenant-a", binding_id="binding-a", configuration_id="config-a",
                        product_id="product-a", project_id="project-a", service_id="transform",
                        scope=SCOPE, revision=1, generation=3, state="ready", ownership="customer_linked",
                        connection_ref=SOURCE, connection_key="a" * 64, readiness=[], gaps=[]),
            ])
            await session.commit()
            receipt = await receive_schedule_tick(
                session, tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                service_id="transform", trigger_id="scheduled-transform", scheduled_at=NOW, now=NOW)
            assert receipt.state == "pending"
        yield sessions, receipt
    finally:
        await engine.dispose()


def authority(receipt):
    return {"scope": SCOPE, "service_id": "transform", "actor_id": "service-a",
            "actor_type": "service", "configuration_id": "config-a", "configuration_revision": 1,
            "configuration_hash": "a" * 64, "configuration_enabled": True,
            "binding_id": "binding-a", "binding_generation": 3, "binding_state": "ready",
            "connection_ref": SOURCE, "binding_connection_ref": SOURCE,
            "qualified_executor_ref": SOURCE, "delegation_id": "delegation-a", "delegation_version": 1,
            "delegation_active": True, "trigger": deepcopy(TRIGGER), "source_refs": [SOURCE],
            "capabilities": ["refactor"], "transform_settings": SETTINGS,
            "provider_identity": {"account_id": "123456789012",
                                  "role_arn": "arn:aws:iam::123456789012:role/example",
                                  "region": "example-region"},
            "transform_qualification": QUALIFIED,
            "transform_test_policy": {**TEST_POLICY, "test_policy_ref": SOURCE},
            "profile_ref": SOURCE, "adapter_revision": "1", "visibility": "only_me",
            "classification": "internal", "causal_origin": {}, "exclusions": [],
            "monitoring_policy": {}, "max_bytes": 10000, "max_files": 10,
            "limits": {"deadline_seconds": 3600}, "event_id": receipt.event_id}


class SourceClient:
    def __init__(self):
        self.captures = []

    async def pin_source(self, product_id, project_id, request, *, idempotency_key):
        self.captures.append((product_id, project_id, request, idempotency_key))
        artifact = {"artifact_id": "artifact-a", "version": "1", "media_type": "application/json",
                    "bytes": 4, "sha256": "b" * 64}
        return {"snapshot_id": "sealed-a", "source_ref": request["source_ref"],
                "exclusions": request["exclusions"], "expires_at": request["retain_until"],
                "content": artifact, "file_manifest": artifact, "file_count": 1}


@pytest.mark.asyncio
async def test_transform_schedule_uses_manual_admission_and_budget(occurrence):
    sessions, receipt = occurrence
    source_client = SourceClient()
    published = []
    budget_checks = []

    async def verify_target(selected):
        return authority(selected), source_client

    async def publish(envelope, delay):
        published.append((envelope, delay))

    async def budget_guard(session, limits, scope, now, replay):
        budget_checks.append((limits, scope, now))
        return await replay()

    async with sessions() as session:
        run = await admit_source_receipt(session, sessions, receipt, verify_target, publish,
                                         now=NOW, budget_guard=budget_guard)
        stored = await session.get(EventReceipt, ("tenant-a", "product-a", "project-a", "transform",
                                                  "scheduled-transform", receipt.event_id))
        assert stored.state == "admitted" and stored.run_id == run.run_id
        assert run.input == receipt.run_input and run.input["sources"] == [SOURCE]
        assert run.admission["event_id"] == receipt.event_id
        assert run.admission["source_snapshots"][0]["source_ref"] == SOURCE
        assert run.admission["transform_test_policy"] == {**TEST_POLICY, "test_policy_ref": SOURCE}
        assert budget_checks == [({"deadline_seconds": 3600}, SCOPE, NOW)]
        assert len(source_client.captures) == len(published) == 1
        assert published[0][0]["generation"] == 1
        assert await session.scalar(select(func.count()).select_from(RunReceipt)) == 1
        assert await session.scalar(select(func.count()).select_from(Run)) == 1
        assert await session.scalar(select(func.count()).select_from(Work)) == 1

    async with sessions() as session:
        replay = await admit_source_receipt(session, sessions, receipt, verify_target, publish,
                                            now=NOW + timedelta(minutes=1), budget_guard=budget_guard)
        assert replay.run_id == run.run_id
        assert await session.scalar(select(func.count()).select_from(Run)) == 1
        assert await session.scalar(select(func.count()).select_from(Work)) == 1
        assert len(source_client.captures) == len(published) == len(budget_checks) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"trigger": {**TRIGGER, "enabled": False}},
    {"delegation_active": False},
    {"configuration_enabled": False},
    {"trigger": {**TRIGGER, "goal": {**GOAL, "summary": "New goal"}}},
    {"source_refs": []},
    {"binding_generation": 4},
])
async def test_transform_schedule_denies_changed_authority_before_effects(occurrence, change):
    sessions, receipt = occurrence
    source_client = SourceClient()
    budget_checks = []

    async def verify_target(selected):
        return {**authority(selected), **change}, source_client

    async def publish(envelope, delay):
        pytest.fail("denied receipt must not dispatch work")

    async def budget_guard(session, limits, scope, now, replay):
        budget_checks.append(now)

    async with sessions() as session:
        with pytest.raises(HTTPException) as denied:
            await admit_source_receipt(session, sessions, receipt, verify_target, publish,
                                       now=NOW, budget_guard=budget_guard)
        assert denied.value.status_code in {404, 409}
        await session.rollback()
        assert (await session.get(EventReceipt, ("tenant-a", "product-a", "project-a", "transform",
                                                 "scheduled-transform", receipt.event_id))).state == "pending"
        assert not source_client.captures and not budget_checks
        assert await session.scalar(select(func.count()).select_from(Run)) == 0
        assert await session.scalar(select(func.count()).select_from(Work)) == 0


@pytest.mark.asyncio
async def test_unqualified_check_duration_denies_admission_before_capture(occurrence):
    sessions, receipt = occurrence
    source_client = SourceClient()
    policy = {**TEST_POLICY, "test_policy_ref": SOURCE,
              "commands": [{**TEST_POLICY["commands"][0], "timeout_seconds": 121}]}

    async def verify_target(selected):
        return {**authority(selected), "transform_test_policy": policy}, source_client

    async def publish(envelope, delay):
        pytest.fail("unqualified check cannot publish paid work")

    async with sessions() as session:
        with pytest.raises(HTTPException) as denied:
            await admit_source_receipt(session, sessions, receipt, verify_target, publish, now=NOW)
        assert denied.value.detail == {"code": "transform_test_policy_unavailable"}
        await session.rollback()
        assert not source_client.captures
        assert await session.scalar(select(func.count()).select_from(Run)) == 0
        assert await session.scalar(select(func.count()).select_from(Work)) == 0


@pytest.mark.asyncio
async def test_transform_schedule_requires_budget_authority_before_capture(occurrence):
    sessions, receipt = occurrence
    source_client = SourceClient()

    async def verify_target(selected):
        return authority(selected), source_client

    async def publish(envelope, delay):
        pytest.fail("missing budget authority must not publish")

    async with sessions() as session:
        with pytest.raises(HTTPException) as denied:
            await admit_source_receipt(session, sessions, receipt, verify_target, publish, now=NOW)
        assert denied.value.detail == {"code": "budget_authority_unavailable"}
        assert source_client.captures == []
        await session.rollback()
        assert await session.scalar(select(func.count()).select_from(Run)) == 0
        assert await session.scalar(select(func.count()).select_from(Work)) == 0


@pytest.mark.asyncio
async def test_transform_schedule_denies_exhausted_budget_before_capture(occurrence):
    sessions, receipt = occurrence
    source_client = SourceClient()
    checked = []

    async def verify_target(selected):
        return authority(selected), source_client

    async def publish(envelope, delay):
        pytest.fail("exhausted budget must not publish")

    async def budget_guard(session, limits, scope, now, replay):
        checked.append((limits, scope, now))
        raise HTTPException(409, detail={"code": "budget_exhausted"})

    async with sessions() as session:
        with pytest.raises(HTTPException) as denied:
            await admit_source_receipt(session, sessions, receipt, verify_target, publish,
                                       now=NOW, budget_guard=budget_guard)
        assert denied.value.detail == {"code": "budget_exhausted"}
        assert checked == [({"deadline_seconds": 3600}, SCOPE, NOW)]
        assert not source_client.captures
        await session.rollback()
        assert await session.scalar(select(func.count()).select_from(Run)) == 0
        assert await session.scalar(select(func.count()).select_from(Work)) == 0


@pytest.mark.asyncio
async def test_manual_and_scheduled_transform_share_admission_policy(occurrence):
    sessions, receipt = occurrence
    source_client = SourceClient()
    budget_checks = []
    dispatched = []

    async def verify_target(selected):
        return authority(selected), source_client

    async def publish(envelope, delay):
        dispatched.append((envelope, delay))

    async def budget_guard(session, limits, scope, now, replay):
        budget_checks.append((deepcopy(limits), deepcopy(scope), now))
        return await replay()

    manual_authority = {**authority(receipt), "actor_id": "owner-a", "actor_type": "human",
                        "event_id": None, "trigger": MANUAL_TRIGGER}
    async with sessions() as session:
        manual = await submit_and_dispatch(session, sessions, source_client, publish, receipt.run_input,
                                           manual_authority, "manual-transform-once", now=NOW,
                                           budget_guard=budget_guard)
    async with sessions() as session:
        scheduled = await admit_source_receipt(session, sessions, receipt, verify_target, publish,
                                               now=NOW, budget_guard=budget_guard)
        assert manual.run_id != scheduled.run_id
        assert manual.input == scheduled.input == receipt.run_input
        assert manual.input["sources"][0]["record_revision"] == SOURCE["record_revision"]
        assert receipt.target["pinned_source"] == SOURCE
        assert "event_id" not in manual.admission
        assert scheduled.admission["event_id"] == receipt.event_id
        for admitted in (manual, scheduled):
            assert admitted.admission["source_snapshots"][0]["source_ref"] == SOURCE
            assert admitted.admission["transform_test_policy"] == {**TEST_POLICY, "test_policy_ref": SOURCE}
            assert admitted.admission["limits"] == {"deadline_seconds": 3600}
        assert budget_checks == [({"deadline_seconds": 3600}, SCOPE, NOW)] * 2
        assert [capture[2]["source_ref"] for capture in source_client.captures] == [SOURCE, SOURCE]
        assert source_client.captures[0][3] != source_client.captures[1][3]
        assert len(dispatched) == 2
        assert await session.scalar(select(func.count()).select_from(Run)) == 2
        assert await session.scalar(select(func.count()).select_from(RunReceipt)) == 2
        assert await session.scalar(select(func.count()).select_from(Work)) == 2

    async with sessions() as session:
        repeated = await admit_source_receipt(session, sessions, receipt, verify_target, publish,
                                               now=NOW + timedelta(minutes=1), budget_guard=budget_guard)
        assert repeated.run_id == scheduled.run_id
        assert await session.scalar(select(func.count()).select_from(Run)) == 2
        assert await session.scalar(select(func.count()).select_from(Work)) == 2
        assert len(source_client.captures) == len(dispatched) == len(budget_checks) == 2


@pytest.mark.asyncio
async def test_disabled_transform_schedules_never_opt_in_or_admit(occurrence):
    sessions, pending = occurrence
    source_client = SourceClient()
    options = {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a",
               "service_id": "transform", "trigger_id": "scheduled-transform"}

    async def verify_target(selected):
        return authority(selected), source_client

    async def publish(envelope, delay):
        pytest.fail("disabled schedule must not publish work")

    async with sessions() as session:
        configuration = await session.get(Configuration, ("tenant-a", "config-a"))
        for revision, settings in (
            (2, {"triggers": [TRIGGER], "source_refs": [SOURCE]}),
            (3, {"enabled": True, "triggers": [{**TRIGGER, "enabled": False}],
                 "source_refs": [SOURCE]}),
        ):
            configuration.current_revision = revision
            session.add(ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a",
                                             revision=revision, settings=settings, inherited_fields=[],
                                             gaps=[], state="ready", hash=f"{revision}" * 64))
            await session.commit()
            tick = NOW + timedelta(hours=revision)
            denied = await receive_schedule_tick(session, **options, scheduled_at=tick, now=tick)
            assert (denied.state, denied.reason, denied.run_input) == ("rejected", "trigger_disabled", None)
            assert denied.event_id.endswith(f":revision:{revision}")
            with pytest.raises(HTTPException) as error:
                await admit_source_receipt(session, sessions, denied, verify_target, publish, now=tick)
            assert error.value.detail == {"code": "trigger_disabled"}
        assert pending.state == "pending"
        assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 3
        assert await session.scalar(select(func.count()).select_from(Run)) == 0
        assert await session.scalar(select(func.count()).select_from(Work)) == 0
        assert not source_client.captures


@pytest.mark.asyncio
async def test_revoked_transform_binding_rejects_pending_receipt_during_recovery(occurrence):
    sessions, pending = occurrence
    source_client = SourceClient()
    budget_checks = []

    async def verify_target(selected):
        async with sessions() as session:
            binding = await session.get(Binding, ("tenant-a", "binding-a"))
            return {**authority(selected), "binding_state": binding.state}, source_client

    async def publish(envelope, delay):
        pytest.fail("revoked receipt must not publish work")

    async def budget_guard(session, limits, scope, now, replay):
        budget_checks.append(now)

    async with sessions() as session:
        binding = await session.get(Binding, ("tenant-a", "binding-a"))
        binding.state = "revoked"
        await session.commit()
    service = SimpleNamespace(sessions=sessions, verify_event_target=verify_target,
                              publish=publish, budget_guard=budget_guard)
    assert await repair_pending_events(service, now=NOW) == 0
    async with sessions() as session:
        stored = await session.get(EventReceipt, ("tenant-a", "product-a", "project-a", "transform",
                                                  "scheduled-transform", pending.event_id))
        assert (stored.state, stored.reason, stored.recovery_attempts) == (
            "rejected", "binding_or_delegation_unavailable", 1)
        assert await session.scalar(select(func.count()).select_from(Run)) == 0
        assert await session.scalar(select(func.count()).select_from(RunReceipt)) == 0
        assert await session.scalar(select(func.count()).select_from(Work)) == 0
        assert not source_client.captures and not budget_checks


@pytest.mark.asyncio
async def test_duplicate_utc_transform_tick_admits_one_pinned_run(occurrence):
    sessions, pending = occurrence
    source_client = SourceClient()
    published = []
    budget_checks = []
    options = {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a",
               "service_id": "transform", "trigger_id": "scheduled-transform"}

    async def verify_target(selected):
        return authority(selected), source_client

    async def publish(envelope, delay):
        published.append((envelope, delay))

    async def budget_guard(session, limits, scope, now, replay):
        budget_checks.append((limits, scope, now))
        return await replay()

    async with sessions() as session:
        receipt = await receive_schedule_tick(
            session, **options, scheduled_at=NOW.astimezone(timezone(timedelta(hours=2))), now=NOW)
        assert receipt.event_id == pending.event_id
        assert receipt.run_input["sources"] == [SOURCE]
        admitted = await admit_source_receipt(session, sessions, receipt, verify_target, publish,
                                              now=NOW, budget_guard=budget_guard)
        repeated = await receive_schedule_tick(session, **options, scheduled_at=NOW, now=NOW)
        assert repeated.event_id == pending.event_id
        assert repeated.run_input == receipt.run_input and repeated.target == receipt.target
        replay = await admit_source_receipt(session, sessions, repeated, verify_target, publish,
                                            now=NOW, budget_guard=budget_guard)
        assert replay.run_id == admitted.run_id
        assert admitted.input["sources"] == [SOURCE]
        assert admitted.admission["event_id"] == pending.event_id
        assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 1
        assert await session.scalar(select(func.count()).select_from(Run)) == 1
        assert await session.scalar(select(func.count()).select_from(RunReceipt)) == 1
        assert await session.scalar(select(func.count()).select_from(Work)) == 1
        assert len(source_client.captures) == len(published) == len(budget_checks) == 1
