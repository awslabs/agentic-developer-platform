"""Opt-in review ticks retain scoped receipts and use current admission authority."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from l5_services.adapters.well_architected.binding import (
    ProfileAdapter,
    ProfileSelection,
)
from l5_services.adapters.well_architected.generation import GenerationAdapter
from l5_services.events.recovery import repair_pending_events
from l5_services.events.schedules import receive_schedule_tick
from l5_services.events.sources import admit_source_receipt
from l5_services.models import (
    Binding,
    Configuration,
    ConfigurationVersion,
    EventReceipt,
    Run,
    Work,
)
from l5_services.models.base import Base
from l5_services.models.receipt import RunReceipt
from l5_services.workers.execution import ExecutionService
from l5_services.workers.relay import receive_schedule_event
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from src.shared.models.budget import BudgetConfig, BudgetUsage

TICK = datetime(2026, 10, 29, 12, tzinfo=UTC)
SCOPE = {"tenant_id": "tenant-example", "product_id": "product-example",
         "project_id": "project-example"}
SOURCE = {"tenant_id": "tenant-example", "project_id": "project-example",
          "source_connection_id": "connection-example", "record_type": "document",
          "record_id": "design-example", "record_revision": "revision-example"}
TRIGGER = {"trigger_id": "review-weekly", "kind": "schedule", "enabled": True,
           "schedule": "0 12 * * 4", "capability": "architecture_review",
           "binding_id": "binding-example", "source_refs": [SOURCE]}


@pytest.fixture
async def review_store(tmp_path, monkeypatch):
    # The worker runs at TICK; admission must use the same simulated instant.
    # Otherwise the fixed retention window expires against the host's clock.
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return TICK.astimezone(tz) if tz is not None else TICK.replace(tzinfo=None)

    monkeypatch.setattr("l5_services.admission.datetime", FixedClock)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wa-schedule.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(BudgetConfig.__table__.create)
        await connection.run_sync(BudgetUsage.__table__.create)
    try:
        async with sessions() as session:
            session.add_all([
                Configuration(tenant_id=SCOPE["tenant_id"], configuration_id="config-example",
                              product_id=SCOPE["product_id"], project_id=SCOPE["project_id"],
                              service_id="well_architected", relationship_revision="1",
                              owner_id="owner-example", current_revision=1, handoff_state="linked"),
                ConfigurationVersion(tenant_id=SCOPE["tenant_id"], configuration_id="config-example",
                                     revision=1, settings={"enabled": False, "triggers": [TRIGGER],
                                                           "source_refs": [SOURCE]}, inherited_fields=[],
                                     gaps=[], state="ready", hash="a" * 64),
                Binding(tenant_id=SCOPE["tenant_id"], binding_id="binding-example",
                        configuration_id="config-example", product_id=SCOPE["product_id"],
                        project_id=SCOPE["project_id"], service_id="well_architected",
                        scope=SCOPE, revision=1, generation=3, state="ready", ownership="customer_linked",
                        connection_ref=SOURCE, connection_key="a" * 64, readiness=[], gaps=[]),
            ])
            await session.commit()
        yield sessions
    finally:
        await engine.dispose()


def authority(receipt):
    return {"scope": SCOPE, "service_id": "well_architected", "actor_id": "service-example",
            "actor_type": "service", "configuration_id": "config-example",
            "configuration_revision": receipt.configuration_revision, "configuration_enabled": True,
            "event_id": receipt.event_id, "trigger": deepcopy(TRIGGER),
            "binding_generation": 3, "binding_state": "ready", "source_refs": [SOURCE],
            "connection_ref": SOURCE, "binding_connection_ref": SOURCE,
            "delegation_active": True, "delegation_id": "delegation-example", "delegation_version": 1}


def qualified_authority(receipt):
    profile = {**SOURCE, "record_type": "agent_profile",
               "record_id": "arn:aws:wellarchitected:us-east-1:000000000000:agent-profile/profile-example",
               "record_revision": "c" * 64}
    return {**authority(receipt), "scope": {**SCOPE, "relationship_revision": "1"},
            "binding_id": "binding-example", "configuration_hash": "a" * 64,
            "profile_ref": SOURCE, "native_profile_ref": profile, "profile_hash": profile["record_revision"],
            "native_creator": "provider-example", "generation_model_hash": "d" * 64,
            "generation_scope": {"pillars": ["RESILIENCE"]},
            "generation_context": {"goal": "resilience"}, "architecture_coverage": {},
            "source_retention": {"required_until": (TICK + timedelta(days=1)).isoformat(),
                                 "policy_expires_at": (TICK + timedelta(days=4)).isoformat()},
            "adapter_revision": "wa-example", "limits": {"deadline_seconds": 300},
            "visibility": "only_me", "classification": "private",
            "causal_origin": "schedule", "exclusions": [],
            "max_files": 1, "max_bytes": 8192, "monitoring_policy": {}}


@pytest.mark.asyncio
async def test_enabled_review_tick_freezes_scoped_revision_and_replays_without_duplicate_receipt(review_store):
    async with review_store() as session:
        disabled = await receive_schedule_tick(session, **SCOPE, service_id="well_architected",
                                               trigger_id=TRIGGER["trigger_id"], scheduled_at=TICK, now=TICK)
        assert (disabled.state, disabled.reason, disabled.run_input) == ("rejected", "trigger_disabled", None)
        configuration = await session.get(Configuration, (SCOPE["tenant_id"], "config-example"))
        configuration.current_revision = 2
        session.add(ConfigurationVersion(tenant_id=SCOPE["tenant_id"], configuration_id="config-example",
                                         revision=2, settings={"enabled": True, "triggers": [TRIGGER],
                                                               "source_refs": [SOURCE]}, inherited_fields=[],
                                         gaps=[], state="ready", hash="b" * 64))
        await session.commit()
        first = await receive_schedule_tick(session, **SCOPE, service_id="well_architected",
                                            trigger_id=TRIGGER["trigger_id"], scheduled_at=TICK, now=TICK)
        replay = await receive_schedule_tick(session, **SCOPE, service_id="well_architected",
                                             trigger_id=TRIGGER["trigger_id"],
                                             scheduled_at=TICK.astimezone(timezone(timedelta(hours=2))), now=TICK)
        assert first is replay
        assert first.event_id == "schedule:2026-10-29T12:00:00Z:revision:2"
        assert first.state == "pending" and first.target["binding_generation"] == 3
        assert first.run_input == {"service_id": "well_architected", "capability": "architecture_review",
                                   "configuration_revision": 2, "binding_id": "binding-example", "sources": [SOURCE]}
        assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 2
        assert await session.scalar(select(func.count()).select_from(Run)) == 0


@pytest.mark.asyncio
async def test_review_tick_uses_configuration_projection_of_exact_product_repository(review_store):
    from l5_services.configuration_population import populate_settings

    repository = {**SOURCE, "record_type": "repository", "record_id": "repository-example", "record_revision": "a" * 40}
    profile_ref = {**SOURCE, "record_type": "product", "record_id": SCOPE["product_id"], "record_revision": "1"}
    profile = {**SCOPE, "revision": 1, "configuration": {"basic": {"repositories": [repository], "documents": [SOURCE]}}}
    available = {"coverage": "complete", "next_cursor": None,
                 "items": [{"resources": [repository, SOURCE]}]}
    trigger = {**TRIGGER, "source_refs": [repository, SOURCE]}
    settings, _ = populate_settings(profile, profile_ref, available, {
        "service_id": "well_architected", "capabilities": ["architecture_review"], "enabled": True,
        "well_architected": {"review_mode": "source_only", "pillars": ["SECURITY"]}, "triggers": [trigger],
    }, "well_architected")
    expected = [{**repository, "record_type": "repository_commit"}, SOURCE]
    async with review_store() as session:
        version = await session.get(ConfigurationVersion, (SCOPE["tenant_id"], "config-example", 1))
        version.settings = settings
        await session.commit()
        receipt = await receive_schedule_tick(session, **SCOPE, service_id="well_architected",
                                              trigger_id=TRIGGER["trigger_id"], scheduled_at=TICK, now=TICK)
        assert receipt.state == "pending"
        assert receipt.run_input["sources"] == settings["source_refs"] == settings["triggers"][0]["source_refs"] == expected
        assert profile["configuration"]["basic"]["repositories"] == [repository]
        assert trigger["source_refs"] == [repository, SOURCE]


@pytest.mark.asyncio
async def test_review_admission_rechecks_current_authority_and_forwards_budget(review_store, monkeypatch):
    async with review_store() as session:
        version = await session.get(ConfigurationVersion, (SCOPE["tenant_id"], "config-example", 1))
        version.settings = {"enabled": True, "triggers": [TRIGGER], "source_refs": [SOURCE]}
        await session.commit()
        receipt = await receive_schedule_tick(session, **SCOPE, service_id="well_architected",
                                              trigger_id=TRIGGER["trigger_id"], scheduled_at=TICK, now=TICK)

    observed = []
    budget_guard = object()

    async def publish(*args):
        pytest.fail("dispatch should be intercepted before publishing")

    async def dispatch(session, sessions, source_client, publish_work, run_input, verified, key, *, now, budget_guard):
        assert run_input == receipt.run_input and key == receipt.idempotency_key
        assert budget_guard is budget_guard_ref and verified["event_id"] == receipt.event_id
        observed.append(verified)
        return SimpleNamespace(run_id="run-example")

    budget_guard_ref = budget_guard
    monkeypatch.setattr("l5_services.events.sources.submit_and_dispatch", dispatch)

    async def verify_target(selected):
        return authority(selected), object()

    async with review_store() as session:
        with pytest.raises(HTTPException) as early:
            await admit_source_receipt(session, review_store, receipt, verify_target, publish,
                                       now=TICK - timedelta(seconds=1), budget_guard=budget_guard)
        assert early.value.detail["code"] == "schedule_not_due"
        for changed in ({"delegation_active": False}, {"binding_generation": 4},
                        {"trigger": {**TRIGGER, "enabled": False}},
                        {"scope": {**SCOPE, "project_id": "foreign-project"}},
                        {"connection_ref": {**SOURCE, "record_id": "foreign-connection"}}):
            async def altered(selected, difference=changed):
                return {**authority(selected), **difference}, object()

            with pytest.raises(HTTPException):
                await admit_source_receipt(session, review_store, receipt, altered, publish,
                                           now=TICK, budget_guard=budget_guard)
        assert not observed
        assert await session.scalar(select(func.count()).select_from(Run)) == 0
        run = await admit_source_receipt(session, review_store, receipt, verify_target, publish,
                                         now=TICK, budget_guard=budget_guard)
        assert run.run_id == "run-example" and len(observed) == 1
        stored = await session.get(EventReceipt, (SCOPE["tenant_id"], SCOPE["product_id"],
                                                  SCOPE["project_id"], "well_architected",
                                                  TRIGGER["trigger_id"], receipt.event_id))
        assert stored.state == "admitted" and stored.run_id == run.run_id


async def scheduled_review_journey(review_store):
    artifact = {"artifact_id": "source-content", "version": "one", "media_type": "application/json",
                "bytes": 4, "sha256": "b" * 64}
    manifest = {**artifact, "artifact_id": "source-manifest"}
    captures = []
    published = []
    budget_checks = []

    class SourceClient:
        async def pin_source(self, product_id, project_id, request, *, idempotency_key):
            captures.append((product_id, project_id, idempotency_key))
            assert request["source_ref"] == SOURCE
            return {"source_ref": SOURCE, "snapshot_id": "snapshot-example", "exclusions": [],
                    "expires_at": (TICK + timedelta(days=3)).isoformat(),
                    "content": artifact, "file_manifest": manifest, "file_count": 1}

    async def verify_target(receipt):
        return qualified_authority(receipt), SourceClient()

    async def budget_guard(session, limits, scope, now, replay):
        budget_checks.append((limits, scope, now))
        return await replay()

    async def publish(envelope, delay):
        published.append(envelope)

    worker = ExecutionService(review_store, SourceClient(), None, publish, None,
                              clock=lambda: TICK, verify_event_target=verify_target,
                              budget_guard=budget_guard)
    options = {**SCOPE, "service_id": "well_architected", "trigger_id": TRIGGER["trigger_id"],
               "scheduled_at": TICK}
    disabled = await receive_schedule_event(worker, **options)
    assert (disabled.state, disabled.reason, disabled.run_id) == ("rejected", "trigger_disabled", None)
    assert not captures and not budget_checks and not published
    async with review_store() as session:
        configuration = await session.get(Configuration, (SCOPE["tenant_id"], "config-example"))
        configuration.current_revision = 2
        session.add(ConfigurationVersion(tenant_id=SCOPE["tenant_id"], configuration_id="config-example",
                                         revision=2, settings={"enabled": True, "triggers": [TRIGGER],
                                                               "source_refs": [SOURCE]}, inherited_fields=[],
                                         gaps=[], state="ready", hash="b" * 64))
        await session.commit()
    first = await receive_schedule_event(worker, **options)
    assert first.configuration_revision == 2 and first.event_id != disabled.event_id
    assert first.state == "admitted" and first.run_id is not None
    repeated = await receive_schedule_event(worker, **{
        **options, "scheduled_at": TICK.astimezone(timezone(timedelta(hours=2)))})
    assert repeated.event_id == first.event_id and repeated.run_id == first.run_id
    assert len(captures) == len(budget_checks) == len(published) == 1
    assert budget_checks[0] == ({"deadline_seconds": 300}, {**SCOPE, "relationship_revision": "1"}, TICK)
    async with review_store() as session:
        run = await session.get(Run, (SCOPE["tenant_id"], first.run_id))
        assert run.admission["event_id"] == first.event_id
        assert run.configuration_revision == 2
        assert run.input["sources"] == [SOURCE]
        assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 2
        assert await session.scalar(select(func.count()).select_from(RunReceipt)) == 1
        assert await session.scalar(select(func.count()).select_from(Run)) == 1
        assert await session.scalar(select(func.count()).select_from(Work)) == 1


@pytest.mark.asyncio
async def test_worker_admits_enabled_review_tick_once_through_shared_run_and_work(review_store):
    await scheduled_review_journey(review_store)


@pytest.mark.asyncio
@pytest.mark.parametrize(("configuration_enabled", "trigger_enabled"), [
    (False, True),
    (True, False),
])
async def test_disabled_review_ticks_cannot_create_work_through_worker(
    review_store, configuration_enabled, trigger_enabled,
):
    async with review_store() as session:
        version = await session.get(ConfigurationVersion, (SCOPE["tenant_id"], "config-example", 1))
        version.settings = {"enabled": configuration_enabled,
                            "triggers": [{**TRIGGER, "enabled": trigger_enabled}],
                            "source_refs": [SOURCE]}
        await session.commit()

    async def forbidden(*args, **kwargs):
        pytest.fail("disabled review must not check authority, capture sources, or publish work")

    worker = ExecutionService(review_store, SimpleNamespace(pin_source=forbidden), None, forbidden,
                              None, clock=lambda: TICK, verify_event_target=forbidden,
                              budget_guard=forbidden)
    options = {**SCOPE, "service_id": "well_architected", "trigger_id": TRIGGER["trigger_id"]}
    first = await receive_schedule_event(worker, **options, scheduled_at=TICK)
    replay = await receive_schedule_event(worker, **options,
                                          scheduled_at=TICK.astimezone(timezone(timedelta(hours=2))))
    next_tick = await receive_schedule_event(worker, **options, scheduled_at=TICK + timedelta(days=7),
                                             now=TICK + timedelta(days=7))
    assert first.event_id == replay.event_id
    assert first.state == replay.state == next_tick.state == "rejected"
    assert first.reason == replay.reason == next_tick.reason == "trigger_disabled"
    assert first.run_id is None and first.run_input is None and next_tick.run_id is None
    assert await repair_pending_events(worker, now=TICK + timedelta(days=8)) == 0
    async with review_store() as session:
        assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 2
        assert await session.scalar(select(func.count()).select_from(RunReceipt)) == 0
        assert await session.scalar(select(func.count()).select_from(Run)) == 0
        assert await session.scalar(select(func.count()).select_from(Work)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["delegation", "budget"])
async def test_revoked_authority_or_exhausted_budget_cannot_start_native_review(
    review_store, monkeypatch, revocation,
):
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fictional")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fictional")
    monkeypatch.setenv("BG_TOKEN_SECRET_KEY", "fictional-test-key-0123456789abcdef")
    from src.l5_integration import GatewayConfigurationBackend

    async with review_store() as session:
        version = await session.get(ConfigurationVersion, (SCOPE["tenant_id"], "config-example", 1))
        version.settings = {"enabled": True, "triggers": [TRIGGER], "source_refs": [SOURCE]}
        session.add_all([
            BudgetConfig(id="budget-example", org_id=SCOPE["tenant_id"], entity_type="org",
                         entity_id=SCOPE["tenant_id"], period_type="daily",
                         budget_amount_usd=Decimal(2), enforcement_mode="hard"),
            BudgetUsage(org_id=SCOPE["tenant_id"], entity_type="org",
                        entity_id=SCOPE["tenant_id"], period_type="daily", period_start=TICK.date(),
                        total_cost_usd=Decimal(0)),
        ])
        await session.commit()
        receipt = await receive_schedule_tick(session, **SCOPE, service_id="well_architected",
                                              trigger_id=TRIGGER["trigger_id"], scheduled_at=TICK, now=TICK)
        assert receipt.state == "pending"

    delegation_active = revocation != "delegation"
    if revocation == "budget":
        async with review_store() as session:
            usage = await session.scalar(select(BudgetUsage).where(
                BudgetUsage.org_id == SCOPE["tenant_id"], BudgetUsage.period_start == TICK.date()))
            assert usage.total_cost_usd == Decimal(0)
            usage.total_cost_usd = Decimal(2)
            await session.commit()

    account_id = "000000000000"
    region = "us-east-1"
    broker_role = f"arn:aws:iam::{account_id}:role/broker-example"
    source_role = f"arn:aws:iam::{account_id}:role/source-example"
    qualification = {"account_id": account_id, "region": region, "broker_role_arn": broker_role,
                     "execution_role_arn": source_role, "trust": "qualified",
                     "source_permissions": "qualified", "support": "eligible"}
    selection = ProfileSelection(SCOPE["tenant_id"], SCOPE["product_id"], SCOPE["project_id"],
                                 account_id, region, broker_role, source_role, ("RESILIENCE",), qualification)
    provider = Mock()
    provider.get_agent_profile.return_value = {
        "arn": qualified_authority(receipt)["native_profile_ref"]["record_id"],
        "executionRoleArn": source_role, "pillars": ["RESILIENCE"],
        "aggregationConfiguration": [], "eligibleForArchitectureGeneration": True,
        "eligibleForScheduledGeneration": True, "fieldErrors": [],
    }
    provider.list_agent_contexts.return_value = {"items": []}
    adapter = ProfileAdapter(provider, account_id=account_id, region=region)
    profile_arn = provider.get_agent_profile.return_value["arn"]
    before = adapter.adopt(selection, profile_arn)
    assert before.state == "ready" and before.scheduled_eligibility is True

    async def forbidden(*args, **kwargs):
        pytest.fail("denied tick must not capture, publish or start native generation")

    budget_checks = []

    async def verify_target(selected):
        verified = qualified_authority(selected)
        verified["limits"] = {"deadline_seconds": 300, "max_cost": "1", "policy_ref": {
            "tenant_id": SCOPE["tenant_id"], "project_id": SCOPE["project_id"],
            "record_type": "policy", "record_id": "budget-example"}}
        verified["delegation_active"] = delegation_active
        return verified, SimpleNamespace(pin_source=forbidden)

    async def budget_guard(session, limits, scope, now, replay):
        budget_checks.append(now)
        backend = GatewayConfigurationBackend(session, SimpleNamespace(
            org_id=SCOPE["tenant_id"], user_id="owner-example"))
        return await backend._require_devops_budget(session, limits, scope, now, replay)

    worker = ExecutionService(review_store, SimpleNamespace(pin_source=forbidden), None,
                              forbidden, None, clock=lambda: TICK, verify_event_target=verify_target,
                              budget_guard=budget_guard, well_architected_adapter=GenerationAdapter(provider))
    with pytest.raises(HTTPException) as denied:
        await receive_schedule_event(worker, **SCOPE, service_id="well_architected",
                                     trigger_id=TRIGGER["trigger_id"], scheduled_at=TICK)
    assert denied.value.detail["code"] == (
        "binding_or_delegation_unavailable" if revocation == "delegation" else "budget_exhausted")
    assert budget_checks == ([] if revocation == "delegation" else [TICK])
    assert adapter.adopt(selection, profile_arn).snapshot_hash == before.snapshot_hash
    assert provider.get_agent_profile.return_value["aggregationConfiguration"] == []
    provider.create_agent_profile.assert_not_called()
    provider.update_agent_profile.assert_not_called()
    provider.start_agent_recommendation_generation.assert_not_called()
    async with review_store() as session:
        current = await session.get(EventReceipt, (SCOPE["tenant_id"], SCOPE["product_id"],
                                                   SCOPE["project_id"], "well_architected",
                                                   TRIGGER["trigger_id"], receipt.event_id))
        assert current.state == "pending" and current.run_id is None
        assert await session.scalar(select(func.count()).select_from(RunReceipt)) == 0
        assert await session.scalar(select(func.count()).select_from(Run)) == 0
        assert await session.scalar(select(func.count()).select_from(Work)) == 0
