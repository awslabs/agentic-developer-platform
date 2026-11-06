"""Scheduled Security assessments: frozen identity, window and scope refusals.

These are offline structural checks. They prove no native qualification and no
provider effect; installed scheduling evidence requires the live service.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import json
from hashlib import sha256

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.events.schedules import receive_schedule_tick, security_configured
from l5_services.events.sources import _verify_security_schedule
from l5_services.models.base import Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.event import EventReceipt
from l5_services.models.run import Run
from l5_services.models.work import Work
from l5_services.models.preparation import AdmissionPreparation


DOCUMENT = {"tenant_id": "tenant-a", "project_id": "project-a", "source_connection_id": "connection-a",
            "record_type": "document", "record_id": "doc-a", "record_revision": "pinned-document"}
REPOSITORY = {**DOCUMENT, "record_type": "repository_commit", "record_id": "repo-a",
              "record_revision": "pinned-commit"}
CONNECTION = {**DOCUMENT, "record_type": "connection", "record_id": "connection-a", "record_revision": "1"}
TRIGGERS = [{"trigger_id": "nightly-threat-model", "kind": "schedule", "enabled": True,
             "schedule": "30 2 * * *", "capability": "threat_model", "binding_id": "binding-threat",
             "source_refs": [DOCUMENT]},
            {"trigger_id": "nightly-code-review", "kind": "schedule", "enabled": True,
             "schedule": "30 2 * * *", "capability": "code_review", "binding_id": "binding-code",
             "source_refs": [REPOSITORY]}]
TICK = datetime(2026, 2, 1, 2, 30, tzinfo=UTC)


def _fixture(settings_state="needs_input", *, enabled=True, triggers=None, generation=4):
    configuration = Configuration(
        tenant_id="tenant-a", configuration_id="config-a", product_id="product-a",
        project_id="project-a", service_id="security", relationship_revision="1",
        owner_id="owner-a", current_revision=1, handoff_state="linked")
    version = ConfigurationVersion(
        tenant_id="tenant-a", configuration_id="config-a", revision=1,
        settings={"enabled": enabled, "capabilities": ["threat_model", "code_review"],
                  "triggers": TRIGGERS if triggers is None else triggers,
                  "source_refs": [DOCUMENT, REPOSITORY]},
        inherited_fields=[], gaps=["execution_delegation"], state=settings_state, hash="a" * 64)
    bindings = [Binding(tenant_id="tenant-a", binding_id=identifier, configuration_id="config-a",
                        product_id="product-a", project_id="project-a", service_id="security",
                        scope={"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a"},
                        revision=1, generation=generation, state="ready", ownership="adp_created",
                        connection_ref=CONNECTION, connection_key=identifier, readiness=[], gaps=[])
                for identifier in ("binding-threat", "binding-code")]
    return [configuration, version, *bindings]


async def _sessions(tmp_path, name, rows):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        session.add_all(rows)
        await session.commit()
    return engine, sessions


async def _tick(session, trigger_id, *, scheduled_at=TICK, now=None):
    return await receive_schedule_tick(
        session, tenant_id="tenant-a", product_id="product-a", project_id="project-a",
        trigger_id=trigger_id, scheduled_at=scheduled_at, now=now or scheduled_at + timedelta(minutes=1),
        service_id="security")


def test_a_saved_needs_input_security_configuration_still_schedules():
    """Security saves stay needs_input until execution verifies fresh consent."""
    assert security_configured(SimpleNamespace(state="needs_input"))
    assert security_configured(SimpleNamespace(state="ready"))
    assert not security_configured(SimpleNamespace(state="disabled"))
    assert not security_configured(SimpleNamespace(state="draft"))


@pytest.mark.asyncio
async def test_both_capabilities_freeze_distinct_targets_without_revision_identity(tmp_path):
    engine, sessions = await _sessions(tmp_path, "both.db", _fixture())
    try:
        async with sessions() as session:
            threat = await _tick(session, "nightly-threat-model")
            review = await _tick(session, "nightly-code-review")
            assert threat.event_id == review.event_id == "schedule:2026-02-01T02:30:00Z"
            assert (threat.state, review.state) == ("pending", "pending")
            assert threat.run_input == {"configuration_revision": 1, "binding_id": "binding-threat",
                                        "capability": "threat_model", "sources": [DOCUMENT]}
            assert review.run_input["sources"] == [REPOSITORY]
            for receipt in (threat, review):
                assert receipt.target["binding_generation"] == 4
                assert receipt.target["configuration_revision"] == 1
                assert receipt.target["scheduled_at"] == "2026-02-01T02:30:00Z"
                assert "revision:" not in receipt.event_id

            # A duplicate delivery of the same tick observes the original receipt.
            duplicate = await _tick(session, "nightly-threat-model", now=TICK + timedelta(minutes=3))
            assert duplicate.event_id == threat.event_id
            assert duplicate.run_input == threat.run_input
            assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 2
            assert await session.scalar(select(func.count()).select_from(Run)) == 0
            assert await session.scalar(select(func.count()).select_from(Work)) == 0

            # The next authorized tick is a separate frozen receipt.
            following = await _tick(session, "nightly-threat-model", scheduled_at=TICK + timedelta(days=1))
            assert following.event_id == "schedule:2026-02-02T02:30:00Z"
            assert following.state == "pending"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_wrong_time_and_missed_ticks_are_refused_without_catch_up(tmp_path):
    engine, sessions = await _sessions(tmp_path, "window.db", _fixture())
    try:
        async with sessions() as session:
            early = await _tick(session, "nightly-threat-model", scheduled_at=TICK - timedelta(minutes=5))
            assert (early.state, early.reason) == ("rejected", "schedule_mismatch")
            late = await _tick(session, "nightly-code-review", now=TICK + timedelta(minutes=6))
            assert (late.state, late.reason) == ("rejected", "missed_tick")
            assert late.run_input is None and late.target is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_disabled_configuration_or_trigger_refuses_the_tick(tmp_path):
    disabled = [{**TRIGGERS[0], "enabled": False}, TRIGGERS[1]]
    engine, sessions = await _sessions(tmp_path, "disabled.db", _fixture(triggers=disabled))
    try:
        async with sessions() as session:
            refused = await _tick(session, "nightly-threat-model")
            assert (refused.state, refused.reason) == ("rejected", "trigger_disabled")
    finally:
        await engine.dispose()

    engine, sessions = await _sessions(tmp_path, "off.db", _fixture(enabled=False))
    try:
        async with sessions() as session:
            refused = await _tick(session, "nightly-code-review")
            assert (refused.state, refused.reason) == ("rejected", "trigger_disabled")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unpinned_sources_and_binding_changes_are_refused(tmp_path):
    unpinned = [{**TRIGGERS[0], "source_refs": [{**DOCUMENT, "record_revision": "other-revision"}]},
                {**TRIGGERS[1], "binding_generation": 9}]
    engine, sessions = await _sessions(tmp_path, "scope.db", _fixture(triggers=unpinned))
    try:
        async with sessions() as session:
            scope = await _tick(session, "nightly-threat-model")
            assert (scope.state, scope.reason) == ("rejected", "source_scope_conflict")
            stale = await _tick(session, "nightly-code-review")
            assert (stale.state, stale.reason) == ("rejected", "binding_unavailable")
    finally:
        await engine.dispose()

    missing = [{**TRIGGERS[0], "binding_id": "binding-absent"}]
    engine, sessions = await _sessions(tmp_path, "absent.db", _fixture(triggers=missing))
    try:
        async with sessions() as session:
            absent = await _tick(session, "nightly-threat-model")
            assert (absent.state, absent.reason) == ("rejected", "binding_unavailable")
    finally:
        await engine.dispose()


def _verified(**changes):
    verified = {"trigger": {"trigger_id": "nightly-threat-model", "kind": "schedule", "enabled": True,
                            "capability": "threat_model", "binding_id": "binding-threat",
                            "schedule": "30 2 * * *", "source_refs": [DOCUMENT]},
                "binding_generation": 4, "causal_origin": "schedule", "configuration_enabled": True,
                "binding_state": "ready", "delegation_active": True, "connection_ref": CONNECTION,
                "binding_connection_ref": CONNECTION, "source_registration": {"account_id": "111122223333"},
                "native_model_ref": "model-a", "consent_expires_at": "2026-02-01T12:00:00Z"}
    verified.update(changes)
    return verified


def _receipt(**changes):
    target = {"trigger_id": "nightly-threat-model", "kind": "schedule", "enabled": True,
              "capability": "threat_model", "binding_id": "binding-threat", "schedule": "30 2 * * *",
              "source_refs": [DOCUMENT], "binding_generation": 4, "configuration_revision": 1,
              "scheduled_at": "2026-02-01T02:30:00Z"}
    receipt = {"service_id": "security", "event_kind": "schedule", "target": target,
               "configuration_revision": 1, "event_id": "schedule:2026-02-01T02:30:00Z",
               "run_input": {"configuration_revision": 1, "binding_id": "binding-threat",
                             "capability": "threat_model", "sources": [DOCUMENT]}}
    receipt.update(changes)
    return SimpleNamespace(**receipt)


def test_a_verified_security_schedule_keeps_its_trigger_and_instant_identity():
    _verify_security_schedule(_receipt(), _verified())


@pytest.mark.parametrize("receipt_changes, verified_changes", [
    # Security must not adopt the revision-suffixed Transform/WA event identity.
    ({"event_id": "schedule:2026-02-01T02:30:00Z:revision:1"}, {}),
    ({"event_kind": "document_revision"}, {}),
    # A frozen target may not drift from the currently authorized trigger.
    ({}, {"trigger": {"trigger_id": "other", "kind": "schedule", "enabled": True,
                      "capability": "threat_model", "binding_id": "binding-threat",
                      "schedule": "30 2 * * *", "source_refs": [DOCUMENT]}}),
    ({}, {"binding_generation": 5}),
    ({}, {"binding_state": "suspended"}),
    ({}, {"configuration_enabled": False}),
    ({}, {"delegation_active": False}),
    ({}, {"causal_origin": "manual"}),
    ({}, {"source_registration": None}),
    # A threat model needs the owner-selected native model and live consent.
    ({}, {"native_model_ref": None}),
    ({}, {"consent_expires_at": None}),
])
def test_unverified_security_schedules_are_refused(receipt_changes, verified_changes):
    with pytest.raises(HTTPException) as refused:
        _verify_security_schedule(_receipt(**receipt_changes), _verified(**verified_changes))
    assert refused.value.status_code == 409
    assert refused.value.detail["code"] == "binding_or_delegation_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "late_start", "expired", "changed_input", "other_occurrence", "human"])
async def test_only_timely_frozen_repository_preparation_can_finish_after_arrival_window(tmp_path, change):
    from l5_services.events.sources import _timely_security_preparation
    from l5_services.models.event import semantic_key

    engine, sessions = await _sessions(tmp_path, "capture-continuation.db", _fixture())
    try:
        async with sessions() as session:
            receipt = await _tick(session, "nightly-code-review")
            await session.commit()
            frozen = {
                "actor_id": "owner-a", "actor_type": "service", "event_id": receipt.event_id,
                "configuration_id": receipt.configuration_id, "configuration_revision": receipt.configuration_revision,
                "trigger": {"trigger_id": receipt.trigger_id}, "limits": {"deadline_seconds": 3600}, "exclusions": [],
            }
            request_hash = sha256(json.dumps({"input": receipt.run_input, "event_id": receipt.event_id, "exclusions": []},
                                             sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            deadline = TICK + timedelta(minutes=61)
            if change == "late_start":
                deadline = TICK + timedelta(minutes=66)
            elif change == "expired":
                deadline = TICK + timedelta(minutes=10)
            elif change == "other_occurrence":
                frozen["event_id"] = "schedule:another-instant"
            elif change == "human":
                frozen["actor_type"] = "human"
            elif change == "changed_input":
                request_hash = "0" * 64
            preparation = AdmissionPreparation(
                tenant_id=receipt.tenant_id, product_id=receipt.product_id, project_id=receipt.project_id,
                service_id="security", actor_id="owner-a", operation="submit", idempotency_key=semantic_key(receipt),
                verified=frozen, request_hash=request_hash, deadline_at=deadline,
            )
            session.add(preparation)
            await session.commit()
            result = await _timely_security_preparation(session, receipt, TICK, TICK + timedelta(minutes=11))
            assert result is (change is None)
            assert preparation.deadline_at == deadline
    finally:
        await engine.dispose()
