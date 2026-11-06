"""Scoped, immutable cost intents; consumer activation is independently qualified."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.models import AssessmentResult, Publication, ResultDetail, Run, Work
from l5_services.models.base import Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.event import EventReceipt
from l5_services.events.schedules import receive_schedule_tick
from l5_services.events.sources import admit_source_receipt
from l5_services.events.recovery import repair_pending_events
from l5_services.api.router import create_router
from l5_services.findings import ResultStore
from l5_services.publication.client import ContextClient, FinopsConsumerClient
from l5_services.publication.contributions import create_finops_publications
from l5_services.publication.invalidation import receive_verified_revocation
from l5_services.publication.recovery import claim_publication
from l5_services.workers.relay import repair_once
from test_fo04 import cost_case


def test_cost_intents_are_independent_and_fenced():
    from l5_services.finops_reconciliation import normalize_cost_result

    run, native, billing = cost_case("stale")
    run.service_id = "finops"
    run.tenant_id = "tenant-example"
    run.product_id = "product-example"
    run.project_id = "project-example"
    cost = normalize_cost_result(run, native, billing)
    detail = SimpleNamespace(tenant_id=run.tenant_id, result_id="result-example",
                             payload={"result_id": "result-example", "revision": 1,
                                      "scope": run.admission["scope"], **cost})
    result = SimpleNamespace(tenant_id=run.tenant_id, result_id="result-example",
                             run_id=run.run_id, version=1)
    now = datetime(2026, 2, 3, tzinfo=UTC)
    intents = create_finops_publications(run, result, detail, now=now)
    assert len(intents) == 2
    assert {intent.destination for intent in intents} == {"product-context", "engineering-insights"}
    assert len({intent.publication_id for intent in intents}) == 2
    for intent in intents:
        assert intent.state == "pending" and intent.receipt is None and intent.attempts == 0
        assert intent.payload["cost"]["query"] == run.input["query"]
        assert intent.payload["cost"]["oracle"]["measure"]["freshness_reason"] == "stale"
        assert intent.payload["cost"]["native"]["measure"]["estimated"] is False
        assert intent.payload["result_ref"]["record_revision"] == "1"
        assert not ContextClient(None, "delegation-example", private_read_fencing=True).can_publish(intent)
    replay = create_finops_publications(run, result, detail, now=now)
    assert [(intent.publication_id, intent.contribution_hash, intent.payload)
            for intent in intents] == [(intent.publication_id, intent.contribution_hash, intent.payload)
                                  for intent in replay]
    detail.payload["scope"] = {**detail.payload["scope"], "project_id": "foreign-project"}
    with pytest.raises(ValueError, match="scope"):
        create_finops_publications(run, result, detail, now=now)


@pytest.mark.parametrize("case", ["unknown", "stale", "zero", "partial", "missing"])
def test_cost_citations_preserve_original_evidence(case):
    from l5_services.finops_reconciliation import normalize_cost_result

    run, native, billing = cost_case(case)
    run.service_id = "finops"
    run.tenant_id = "tenant-example"
    run.product_id = "product-example"
    run.project_id = "project-example"
    cost = normalize_cost_result(run, native, billing)
    result = SimpleNamespace(tenant_id=run.tenant_id, result_id="result-example",
                             run_id=run.run_id, version=1)
    detail = SimpleNamespace(tenant_id=run.tenant_id, result_id=result.result_id,
                             payload={"result_id": result.result_id, "revision": 1,
                                      "scope": run.admission["scope"], **cost})
    intents = create_finops_publications(run, result, detail, now=datetime(2026, 2, 3, tzinfo=UTC))
    for intent in intents:
        assert intent.payload["scope"] == run.admission["scope"]
        assert intent.payload["cost"] == cost
        assert intent.payload["result_ref"]["record_id"] == result.result_id
        assert intent.payload["evidence_refs"] == [
            {"kind": "native_answer", "artifact": run.provider_readback_ref["answer_artifact"]},
            {"kind": "scope_snapshot", "artifact": run.admission["billing_scope"]["snapshot_artifact"]},
            *({"kind": "billing_page", "artifact": page} for page in cost["oracle"]["pages"]),
        ]
        assert intent.payload["cost"]["oracle"]["measure"]["estimated"] is billing["estimated"]
        assert intent.payload["cost"]["query"]["start"] == run.input["query"]["start"]
        assert intent.payload["cost"]["query"]["end"] == run.input["query"]["end"]
    original = intents[0].contribution_hash
    original_cost = deepcopy(cost)
    detail.payload["oracle"]["measure"]["freshness_reason"] = "later-edit"
    detail.payload["scope"]["owner_id"] = "new-owner"
    assert intents[0].contribution_hash == original
    assert intents[0].payload["cost"] == original_cost
    assert intents[0].payload["scope"]["owner_id"] == "owner-example"
    detail.payload["query"] = {**detail.payload["query"], "start": "2025-12-01"}
    with pytest.raises(ValueError, match="pinned result"):
        create_finops_publications(run, result, detail, now=datetime(2026, 2, 3, tzinfo=UTC))


async def test_monthly_tick_freezes_closed_interval_and_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("L5_FINOPS_ADMISSION_ENABLED", "true")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ticks.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tick = datetime(2026, 3, 1, tzinfo=UTC)
    trigger = {"trigger_id": "monthly-cost", "kind": "schedule", "enabled": True,
               "schedule": "0 0 1 * *", "period_template": "previous_closed_month",
               "template_revision": 3, "capability": "cost_inquiry", "binding_id": "binding-example",
               "question": "Report cost for the previous closed month",
               "query_template": {"metric": "UnblendedCost", "granularity": "MONTHLY",
                                  "group_by": ["SERVICE"], "currency": "USD"}}
    scope = {"tenant_id": "tenant-example", "product_id": "product-example",
             "project_id": "project-example", "owner_id": "owner-example"}
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add_all([
                Configuration(tenant_id="tenant-example", configuration_id="config-example",
                              product_id="product-example", project_id="project-example", service_id="finops",
                              relationship_revision="1", owner_id="owner-example", current_revision=1,
                              handoff_state="linked"),
                ConfigurationVersion(tenant_id="tenant-example", configuration_id="config-example", revision=1,
                                     settings={"enabled": True, "triggers": [trigger]}, inherited_fields=[],
                                     gaps=[], state="ready", hash="a" * 64),
                Binding(tenant_id="tenant-example", binding_id="binding-example",
                        configuration_id="config-example", product_id="product-example",
                        project_id="project-example", service_id="finops", scope=scope,
                        revision=1, generation=2, state="ready", ownership="customer_linked",
                        connection_ref={"record_id": "connection-example"}, connection_key="a" * 64,
                        readiness=[], gaps=[]),
            ])
            await session.commit()
            options = {"tenant_id": "tenant-example", "product_id": "product-example",
                       "project_id": "project-example", "trigger_id": "monthly-cost",
                       "service_id": "finops"}
            first = await receive_schedule_tick(session, **options, scheduled_at=tick, now=tick)
            assert first.state == "pending" and first.reason is None
            assert first.run_input == {
                "service_id": "finops", "capability": "cost_inquiry", "configuration_revision": 1,
                "binding_id": "binding-example", "question": trigger["question"], "sources": [],
                "query": {**trigger["query_template"], "start": "2026-02-01", "end": "2026-03-01"},
            }
            assert first.target["template_revision"] == 3
            assert first.target["binding_generation"] == 2
            assert first.target["scheduled_at"] == "2026-03-01T00:00:00Z"
            assert first.target["configuration_revision"] == 1
            assert first.target["query"] == first.run_input["query"]
            assert first.idempotency_key
            stale = await receive_schedule_tick(session, **options,
                                                scheduled_at=datetime(2026, 4, 1, tzinfo=UTC),
                                                now=datetime(2026, 4, 3, tzinfo=UTC))
            assert (stale.state, stale.reason, stale.run_input) == ("rejected", "missed_tick", None)
            wrong_instant = datetime(2026, 3, 15, tzinfo=UTC)
            off_boundary = await receive_schedule_tick(session, **options,
                                                       scheduled_at=wrong_instant, now=wrong_instant)
            assert (off_boundary.state, off_boundary.reason) == ("rejected", "schedule_mismatch")
            configuration = await session.get(Configuration, ("tenant-example", "config-example"))
            configuration.current_revision = 2
            session.add(ConfigurationVersion(
                tenant_id="tenant-example", configuration_id="config-example", revision=2,
                settings={"enabled": False, "triggers": []}, inherited_fields=[], gaps=[],
                state="disabled", hash="b" * 64))
            await session.commit()
            replay = await receive_schedule_tick(session, **options,
                                                 scheduled_at=tick.astimezone(timezone(timedelta(hours=2))),
                                                 now=tick + timedelta(days=40))
            assert replay is first
            assert replay.idempotency_key == first.idempotency_key
            assert replay.run_input["query"]["start"] == "2026-02-01"
            blocked = await receive_schedule_tick(session, **options, scheduled_at=datetime(2026, 5, 1, tzinfo=UTC),
                                                  now=datetime(2026, 5, 1, tzinfo=UTC))
            assert (blocked.state, blocked.reason, blocked.run_input) == ("rejected", "trigger_disabled", None)
            assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 4
            assert await session.scalar(select(func.count()).select_from(Run)) == 0
    finally:
        await engine.dispose()


async def test_periodic_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("L5_FINOPS_ADMISSION_ENABLED", "true")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admission.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tick = datetime(2026, 3, 1, tzinfo=UTC)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example",
             "project_id": "project-example", "owner_id": "owner-example",
             "relationship_revision": "1", "visibility": "only_me"}
    connection = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                  "source_connection_id": "connection-example", "record_type": "connection",
                  "record_id": "connection-example", "record_revision": "version-one"}
    trigger = {"trigger_id": "monthly-cost", "kind": "schedule", "enabled": True,
               "schedule": "0 0 1 * *", "period_template": "previous_closed_month",
               "template_revision": 1, "capability": "cost_inquiry", "binding_id": "binding-example",
               "question": "Report the previous month's costs",
               "query_template": {"metric": "UnblendedCost", "currency": "USD",
                                  "granularity": "MONTHLY", "group_by": ["SERVICE"]}}
    published = []
    authorized = {"active": True}
    budget_allowed = {"active": True}
    budget_calls = []

    async def publish(envelope, delay):
        published.append(envelope)

    async def verify_target(receipt):
        if not authorized["active"]:
            raise HTTPException(409, detail={"code": "binding_or_delegation_unavailable"})
        return ({"scope": authorized.get("scope", scope), "service_id": "finops",
                 "actor_id": authorized.get("actor_id", "service-example"),
                 "actor_type": "service", "configuration_id": "config-example",
                 "configuration_revision": 1, "configuration_hash": "a" * 64,
                 "configuration_enabled": True, "binding_id": "binding-example",
                 "binding_generation": 2, "binding_state": "ready",
                 "connection_ref": connection, "binding_connection_ref": connection,
                 "delegation_id": "delegation-example", "delegation_version": 1,
                 "delegation_active": True, "native_qualified": True,
                 "provider_identity": {"account_id": "000000000000", "region": "us-east-1",
                                       "role_arn": "arn:aws:iam::000000000000:role/Example"},
                 "native_agent_ref": {"tenant_id": scope["tenant_id"],
                                      "project_id": scope["project_id"],
                                      "record_type": "finops_agent", "record_id": "agent-example"},
                 "billing_scope": {"scope_generation": 2},
                 "profile_ref": {**connection, "record_type": "product",
                                 "record_id": "product-example"},
                 "trigger": authorized.get("trigger", trigger), "adapter_revision": "fixture-only",
                 "visibility": "only_me",
                 "classification": "internal", "causal_origin": "schedule",
                 "exclusions": [], "max_bytes": 0, "max_files": 0,
                 "monitoring_policy": {"poll_interval_seconds": 600, "poll_grace_seconds": 300},
                 "limits": {"deadline_seconds": 3600, "max_provider_attempts": 1,
                            "max_polls": 4, "concurrency": 1, "max_cost": "1.00",
                            "currency": "USD", "policy_ref": {"record_type": "policy",
                            "tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                            "record_id": "budget-example"}}, "event_id": receipt.event_id}, None)

    async def budget_guard(session, limits, selected_scope, now, replay):
        assert limits["max_provider_attempts"] == 1 and limits["max_cost"] == "1.00"
        assert selected_scope == scope
        budget_calls.append(now)
        if not budget_allowed["active"]:
            raise HTTPException(409, detail={"code": "budget_exhausted"})
        return await replay()

    try:
        async with engine.begin() as connection_session:
            await connection_session.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add_all([
                Configuration(tenant_id="tenant-example", configuration_id="config-example",
                              product_id="product-example", project_id="project-example", service_id="finops",
                              relationship_revision="1", owner_id="owner-example", current_revision=1,
                              handoff_state="linked"),
                ConfigurationVersion(tenant_id="tenant-example", configuration_id="config-example",
                                     revision=1, settings={"enabled": True, "triggers": [trigger]},
                                     inherited_fields=[], gaps=[], state="ready", hash="a" * 64),
                Binding(tenant_id="tenant-example", binding_id="binding-example",
                        configuration_id="config-example", product_id="product-example",
                        project_id="project-example", service_id="finops", scope=scope,
                        revision=1, generation=2, state="ready", ownership="customer_linked",
                        connection_ref=connection, connection_key="a" * 64, readiness=[], gaps=[]),
            ])
            await session.commit()
            options = {"tenant_id": "tenant-example", "product_id": "product-example",
                       "project_id": "project-example", "trigger_id": "monthly-cost",
                       "service_id": "finops"}
            receipt = await receive_schedule_tick(session, **options, scheduled_at=tick, now=tick)
            run = await admit_source_receipt(session, sessions, receipt, verify_target, publish,
                                             now=tick, budget_guard=budget_guard)
            assert run.input == receipt.run_input
            assert run.input["query"]["start"] == "2026-02-01"
            assert run.admission["event_id"] == receipt.event_id
            assert len(published) == 1 and len(budget_calls) == 1
            assert await admit_source_receipt(session, sessions, receipt, verify_target, publish,
                                              now=tick + timedelta(hours=1), budget_guard=budget_guard) == run
            assert len(published) == 1 and len(budget_calls) == 1
            authorized["actor_id"] = "foreign-service"
            with pytest.raises(HTTPException) as denied_actor:
                await admit_source_receipt(session, sessions, receipt, verify_target, publish,
                                           now=tick + timedelta(hours=1), budget_guard=budget_guard)
            assert denied_actor.value.detail["code"] == "idempotency_conflict"
            authorized.pop("actor_id")
            authorized["scope"] = {**scope, "product_id": "foreign-product"}
            with pytest.raises(HTTPException) as denied_scope:
                await admit_source_receipt(session, sessions, receipt, verify_target, publish,
                                           now=tick + timedelta(hours=1), budget_guard=budget_guard)
            assert denied_scope.value.status_code == 404
            authorized.pop("scope")
            authorized["trigger"] = {**trigger, "template_revision": 2}
            with pytest.raises(HTTPException) as changed_template:
                await admit_source_receipt(session, sessions, receipt, verify_target, publish,
                                           now=tick + timedelta(hours=1), budget_guard=budget_guard)
            assert changed_template.value.detail["code"] == "binding_or_delegation_unavailable"
            authorized.pop("trigger")
            authorized["active"] = False
            with pytest.raises(HTTPException) as denied:
                await admit_source_receipt(session, sessions, receipt, verify_target, publish,
                                           now=tick + timedelta(hours=2), budget_guard=budget_guard)
            assert denied.value.detail["code"] == "binding_or_delegation_unavailable"
            await session.rollback()
            assert await session.scalar(select(func.count()).select_from(Run)) == 1
            assert await session.scalar(select(func.count()).select_from(Work)) == 1
            assert len(published) == 1
            next_tick = tick.replace(month=4)
            pending = await receive_schedule_tick(session, **options, scheduled_at=next_tick, now=next_tick)
            assert pending.state == "pending"
        service = SimpleNamespace(sessions=sessions, verify_event_target=verify_target, publish=publish,
                                  budget_guard=budget_guard)
        assert await repair_pending_events(service, now=next_tick) == 0
        async with sessions() as session:
            rejected = await session.get(EventReceipt, ("tenant-example", "product-example",
                                                        "project-example", "finops", "monthly-cost", pending.event_id))
            assert (rejected.state, rejected.reason, rejected.run_id) == (
                "rejected", "binding_or_delegation_unavailable", None)
            assert await session.scalar(select(func.count()).select_from(Run)) == 1
            assert len(published) == 1
            authorized["active"] = True
            budget_allowed["active"] = False
            budget_tick = datetime(2026, 5, 1, tzinfo=UTC)
            exhausted = await receive_schedule_tick(session, **options, scheduled_at=budget_tick, now=budget_tick)
            assert exhausted.state == "pending"
            with pytest.raises(HTTPException) as unavailable:
                await admit_source_receipt(session, sessions, exhausted, verify_target, publish, now=budget_tick)
            assert unavailable.value.status_code == 503
            assert unavailable.value.detail["code"] == "budget_authority_unavailable"
            with pytest.raises(HTTPException) as denied_budget:
                await admit_source_receipt(session, sessions, exhausted, verify_target, publish,
                                           now=budget_tick, budget_guard=budget_guard)
            assert denied_budget.value.status_code == 409
            assert denied_budget.value.detail["code"] == "budget_exhausted"
            assert len(budget_calls) == 2
            exhausted_event_id = exhausted.event_id
            await session.rollback()
            assert await session.scalar(select(func.count()).select_from(Run)) == 1
            assert await session.scalar(select(func.count()).select_from(Work)) == 1
            assert len(published) == 1

            configuration = await session.get(Configuration, ("tenant-example", "config-example"))
            configuration.current_revision = 2
            session.add(ConfigurationVersion(
                tenant_id="tenant-example", configuration_id="config-example", revision=2,
                settings={"enabled": False, "triggers": []}, inherited_fields=[], gaps=[],
                state="disabled", hash="b" * 64))
            await session.commit()
            disabled_tick = datetime(2026, 6, 1, tzinfo=UTC)
            disabled = await receive_schedule_tick(session, **options, scheduled_at=disabled_tick, now=disabled_tick)
            assert (disabled.state, disabled.reason, disabled.run_input) == ("rejected", "trigger_disabled", None)
            with pytest.raises(HTTPException) as denied_disabled:
                await admit_source_receipt(session, sessions, disabled, verify_target, publish,
                                           now=disabled_tick, budget_guard=budget_guard)
            assert denied_disabled.value.detail["code"] == "trigger_disabled"
            exhausted = await session.get(EventReceipt, ("tenant-example", "product-example",
                                                         "project-example", "finops", "monthly-cost",
                                                         exhausted_event_id))
            with pytest.raises(HTTPException) as denied_old_tick:
                await admit_source_receipt(session, sessions, exhausted, verify_target, publish,
                                           now=disabled_tick, budget_guard=budget_guard)
            assert denied_old_tick.value.detail["code"] == "stale_revision"
            assert len(budget_calls) == 2
            assert await session.scalar(select(func.count()).select_from(Run)) == 1
            assert await session.scalar(select(func.count()).select_from(Work)) == 1
            assert len(published) == 1
    finally:
        await engine.dispose()


async def test_periodic_snapshot_repair(tmp_path, monkeypatch):
    monkeypatch.setenv("L5_FINOPS_ADMISSION_ENABLED", "true")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'monthly-repair.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tick = datetime(2026, 3, 1, tzinfo=UTC)
    delayed = datetime(2026, 4, 2, tzinfo=UTC)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example",
             "project_id": "project-example", "owner_id": "owner-example",
             "relationship_revision": "1", "visibility": "only_me"}
    connection = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                  "source_connection_id": "connection-example", "record_type": "connection",
                  "record_id": "connection-example", "record_revision": "version-one"}
    trigger = {"trigger_id": "monthly-cost", "kind": "schedule", "enabled": True,
               "schedule": "0 0 1 * *", "period_template": "previous_closed_month",
               "template_revision": 1, "capability": "cost_inquiry", "binding_id": "binding-example",
               "question": "Report the previous month's costs",
               "query_template": {"metric": "UnblendedCost", "currency": "USD",
                                  "granularity": "MONTHLY", "group_by": ["SERVICE"]}}
    options = {"tenant_id": scope["tenant_id"], "product_id": scope["product_id"],
               "project_id": scope["project_id"], "trigger_id": trigger["trigger_id"],
               "service_id": "finops"}
    published = []
    budget_calls = []

    async def publish(envelope, delay):
        published.append(envelope)

    async def verify_target(receipt):
        return ({"scope": scope, "service_id": "finops", "actor_id": "service-example",
                 "actor_type": "service", "configuration_id": "config-example",
                 "configuration_revision": 1, "configuration_hash": "a" * 64,
                 "configuration_enabled": True, "binding_id": "binding-example",
                 "binding_generation": 2, "binding_state": "ready",
                 "connection_ref": connection, "binding_connection_ref": connection,
                 "delegation_id": "delegation-example", "delegation_version": 1,
                 "delegation_active": True, "native_qualified": True,
                 "provider_identity": {"account_id": "000000000000", "region": "us-east-1",
                                       "role_arn": "arn:aws:iam::000000000000:role/Example"},
                 "native_agent_ref": {"tenant_id": scope["tenant_id"],
                                      "project_id": scope["project_id"],
                                      "record_type": "finops_agent", "record_id": "agent-example"},
                 "billing_scope": {"scope_generation": 2},
                 "profile_ref": {**connection, "record_type": "product", "record_id": "product-example"},
                 "trigger": trigger, "adapter_revision": "fixture-only", "visibility": "only_me",
                 "classification": "internal", "causal_origin": "schedule",
                 "exclusions": [], "max_bytes": 0, "max_files": 0,
                 "monitoring_policy": {"poll_interval_seconds": 600, "poll_grace_seconds": 300},
                 "limits": {"deadline_seconds": 3600, "max_provider_attempts": 1,
                            "max_polls": 4, "concurrency": 1, "max_cost": "1.00",
                            "currency": "USD", "policy_ref": {"record_type": "policy",
                            "tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                            "record_id": "budget-example"}}, "event_id": receipt.event_id}, None)

    async def budget_guard(session, limits, selected_scope, now, replay):
        assert selected_scope == scope and limits["max_cost"] == "1.00"
        budget_calls.append(now)
        return await replay()

    try:
        async with engine.begin() as connection_session:
            await connection_session.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add_all([
                Configuration(tenant_id=scope["tenant_id"], configuration_id="config-example",
                              product_id=scope["product_id"], project_id=scope["project_id"],
                              service_id="finops", relationship_revision="1", owner_id="owner-example",
                              current_revision=1, handoff_state="linked"),
                ConfigurationVersion(tenant_id=scope["tenant_id"], configuration_id="config-example",
                                     revision=1, settings={"enabled": True, "triggers": [trigger]},
                                     inherited_fields=[], gaps=[], state="ready", hash="a" * 64),
                Binding(tenant_id=scope["tenant_id"], binding_id="binding-example",
                        configuration_id="config-example", product_id=scope["product_id"],
                        project_id=scope["project_id"], service_id="finops", scope=scope,
                        revision=1, generation=2, state="ready", ownership="customer_linked",
                        connection_ref=connection, connection_key="a" * 64, readiness=[], gaps=[]),
            ])
            await session.commit()
            first = await receive_schedule_tick(session, **options, scheduled_at=tick, now=tick)
            snapshot = deepcopy(first.run_input)
            assert snapshot["query"] == {**trigger["query_template"], "start": "2026-02-01",
                                         "end": "2026-03-01"}
            assert (await receive_schedule_tick(session, **options,
                                                scheduled_at=tick.astimezone(timezone(timedelta(hours=3))),
                                                now=delayed)).event_id == first.event_id
        service = SimpleNamespace(sessions=sessions, verify_event_target=verify_target, publish=publish,
                                  budget_guard=budget_guard)
        monkeypatch.setenv("L5_FINOPS_ADMISSION_ENABLED", "false")
        assert await repair_pending_events(service, now=delayed) == 0
        async with sessions() as session:
            receipt_key = (scope["tenant_id"], scope["product_id"], scope["project_id"],
                           "finops", trigger["trigger_id"], first.event_id)
            stored = await session.get(EventReceipt, receipt_key)
            assert (stored.state, stored.run_input, stored.recovery_attempts) == ("pending", snapshot, 0)
            with pytest.raises(HTTPException) as disabled_admission:
                await admit_source_receipt(session, sessions, stored, verify_target, publish,
                                           now=delayed, budget_guard=budget_guard)
            assert disabled_admission.value.detail == {"code": "finops_admissions_disabled"}
            assert await session.scalar(select(func.count()).select_from(Run)) == 0
            blocked_tick = datetime(2026, 4, 1, tzinfo=UTC)
            blocked = await receive_schedule_tick(session, **options, scheduled_at=blocked_tick,
                                                  now=blocked_tick)
            assert (blocked.state, blocked.reason, blocked.run_input) == (
                "rejected", "finops_admissions_disabled", None)
        assert not published and not budget_calls
        monkeypatch.setenv("L5_FINOPS_ADMISSION_ENABLED", "true")
        assert await repair_pending_events(service, now=delayed) == 1
        async with sessions() as session:
            receipt_key = (scope["tenant_id"], scope["product_id"], scope["project_id"],
                           "finops", trigger["trigger_id"], first.event_id)
            stored = await session.get(EventReceipt, receipt_key)
            assert stored.state == "admitted" and stored.run_input == snapshot
            run = await session.get(Run, (scope["tenant_id"], stored.run_id))
            assert run.input == snapshot and run.admission["event_id"] == first.event_id
            assert await admit_source_receipt(session, sessions, stored, verify_target, publish,
                                              now=delayed, budget_guard=budget_guard) == run
            monkeypatch.setenv("L5_FINOPS_ADMISSION_ENABLED", "false")
            assert await admit_source_receipt(session, sessions, stored, verify_target, publish,
                                              now=delayed, budget_guard=budget_guard) == run
            monkeypatch.setenv("L5_FINOPS_ADMISSION_ENABLED", "true")
            assert len(published) == 1 and len(budget_calls) == 1
            assert await session.scalar(select(func.count()).select_from(Run)) == 1
            assert await session.scalar(select(func.count()).select_from(Work)) == 1
            assert (await receive_schedule_tick(session, **options, scheduled_at=tick,
                                                now=delayed)).run_input == snapshot

            future = datetime(2026, 5, 1, tzinfo=UTC)
            mismatched = await receive_schedule_tick(session, **options, scheduled_at=future, now=future)
            changed = {**mismatched.run_input["query"], "start": "2026-05-01", "end": "2026-06-01"}
            mismatched.run_input = {**mismatched.run_input, "query": changed}
            mismatched.target = {**mismatched.target, "query": changed}
            await session.commit()
        assert await repair_pending_events(service, now=future) == 0
        async with sessions() as session:
            future_id = f"schedule:{future.isoformat().replace('+00:00', 'Z')}"
            mismatched = await session.get(EventReceipt, (*receipt_key[:-1], future_id))
            assert (mismatched.state, mismatched.reason, mismatched.run_id) == (
                "rejected", "binding_or_delegation_unavailable", None)
            configuration = await session.get(Configuration, (scope["tenant_id"], "config-example"))
            configuration.current_revision = 2
            session.add(ConfigurationVersion(tenant_id=scope["tenant_id"], configuration_id="config-example",
                                             revision=2, settings={"enabled": False, "triggers": []},
                                             inherited_fields=[], gaps=[], state="disabled", hash="b" * 64))
            await session.commit()
            disabled = await receive_schedule_tick(session, **options,
                                                   scheduled_at=datetime(2026, 6, 1, tzinfo=UTC),
                                                   now=datetime(2026, 6, 1, tzinfo=UTC))
            assert (disabled.state, disabled.reason, disabled.run_input) == ("rejected", "trigger_disabled", None)
            assert await session.scalar(select(func.count()).select_from(Run)) == 1
            assert await session.scalar(select(func.count()).select_from(Work)) == 1
            assert len(published) == len(budget_calls) == 1
    finally:
        await engine.dispose()


@pytest.mark.parametrize("state", ["pending", "published", "publishing"])
@pytest.mark.parametrize("record_type", ["document", "repository"])
async def test_cost_source_revocation(tmp_path, state, record_type):
    from l5_services.finops_reconciliation import normalize_cost_result
    from l5_services.publication.contributions import acknowledge

    now = datetime(2026, 2, 3, tzinfo=UTC)
    run, native, billing = cost_case("stale")
    scope = run.admission["scope"]
    source = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
              "source_connection_id": "source-example", "record_type": record_type,
              "record_id": "record-example", "record_revision": "1"}
    record = Run(tenant_id=scope["tenant_id"], run_id=run.run_id,
                 product_id=scope["product_id"], project_id=scope["project_id"], service_id="finops",
                 configuration_id="config-example", configuration_revision=1, binding_id="binding-example",
                 trigger_id="manual-example", binding_generation=1, deadline_at=now + timedelta(days=1),
                 state="succeeded", provider_state="completed",
                 input={**run.input, "sources": [source, {**source, "record_id": "unrelated"}]},
                 admission=run.admission, native_job_ref=run.native_job_ref,
                 provider_readback_ref=run.provider_readback_ref)
    result = AssessmentResult(tenant_id=record.tenant_id, result_id="result-example",
                              run_id=record.run_id, version=1, payload={"native_artifact": native["answer_artifact"]})
    detail = ResultDetail(tenant_id=record.tenant_id, result_id=result.result_id,
                          payload={"result_id": result.result_id, "revision": 1, "scope": scope,
                                   **normalize_cost_result(record, native, billing)})
    publications = create_finops_publications(record, result, detail, now=now)
    originals = {item.publication_id: (deepcopy(item.payload), item.contribution_hash) for item in publications}
    for publication in publications:
        assert "source_refs" not in publication.payload
        if state == "published":
            acknowledge(publication, {"receipt_id": f"receipt-{publication.destination}",
                        "contribution_id": publication.publication_id, "admitted_schema": "1.0",
                        "consumer_revision": "1", "record_refs": [publication.payload["result_ref"]]})
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'revocation.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    invalidations = {}

    def peer(request):
        assert request.method == "POST" and request.url.path.endswith("/invalidate")
        publication = next(item for item in publications if item.publication_id == request.url.path.split("/")[-2])
        assert request.headers["x-l5-delegation-id"] == f"grant:{publication.destination}"
        payload = json.loads(request.content)
        assert payload == {"source_refs": [source], "reason": "source_access_revoked", "invalidation_watermark": 1}
        invalidations[publication.destination] = payload
        return httpx.Response(200, json={"receipt_id": f"invalidated-{publication.destination}",
                                         "applied_watermark": 1,
                                         "record_refs": [publication.payload["result_ref"]]})

    async def verify(event):
        return {**scope, **event, "revoked": True}

    async def forbidden_provider(*args):
        raise AssertionError("revocation must not create provider work")

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add_all([record, result, detail, *publications])
            await session.commit()
        if state == "publishing":
            for publication in publications:
                assert await claim_publication(sessions, record.tenant_id, publication.publication_id, now=now)
        async with sessions() as session:
            before = {item.publication_id: (item.revision, item.attempts, item.receipt)
                      for item in await session.scalars(select(Publication))}

        async with httpx.AsyncClient(transport=httpx.MockTransport(peer), base_url="http://consumer.test") as transport:
            clients = {destination: FinopsConsumerClient(transport, f"grant:{destination}", scope=scope,
                           destination=destination, private_read_fencing=True)
                       for destination in ("product-context", "engineering-insights")}
            service = SimpleNamespace(sessions=sessions, clock=lambda: now, publish=forbidden_provider,
                                      verify_source_revocation=verify, context_client=None,
                                      finops_context_client=clients["product-context"],
                                      finops_insights_client=clients["engineering-insights"])
            event = {"source_ref": {**source, "record_revision": "2"}, "invalidation_watermark": 1}
            for field in ("tenant_id", "product_id", "project_id"):
                foreign_scope = {**scope, field: "foreign"}
                foreign_source = {**source, **{key: foreign_scope[key] for key in ("tenant_id", "project_id")}}
                assert await receive_verified_revocation(service, {**event, **foreign_scope,
                    "source_ref": foreign_source}, now=now) == 0
            for field in ("source_connection_id", "record_type", "record_id"):
                assert await receive_verified_revocation(service, {**event,
                    "source_ref": {**source, field: "not-pinned"}}, now=now) == 0
            assert await receive_verified_revocation(service, event, now=now) == 2
            assert await receive_verified_revocation(service, event, now=now) == 0
            async with sessions() as session:
                for publication in await session.scalars(select(Publication)):
                    revision, attempts, receipt = before[publication.publication_id]
                    assert publication.state == "invalidation_pending"
                    assert publication.invalidation_watermark == 1 and publication.applied_watermark == 0
                    assert publication.invalidation == {"source_refs": [source], "reason": "source_access_revoked"}
                    assert publication.invalidation_receipt is None
                    assert publication.lease_owner is None and publication.lease_until is None
                    assert (publication.revision, publication.attempts, publication.receipt) == (revision + 1, attempts, receipt)
                    assert publication.next_due_at.replace(tzinfo=UTC) == now
                    assert (publication.payload, publication.contribution_hash) == originals[publication.publication_id]
                    with pytest.raises(ValueError, match="not eligible"):
                        await clients[publication.destination].publish(publication)
            assert await repair_once(service, now=now) == 2
            assert await repair_once(service, now=now) == 0
            assert set(invalidations) == {"product-context", "engineering-insights"}
        async with sessions() as session:
            for publication in await session.scalars(select(Publication)):
                assert (publication.state, publication.invalidation_watermark, publication.applied_watermark) == (
                    "invalidated", 1, 1)
                assert publication.invalidation_receipt["receipt_id"] == f"invalidated-{publication.destination}"
                assert (publication.payload, publication.contribution_hash) == originals[publication.publication_id]
            assert await session.scalar(select(func.count()).select_from(Work)) == 0
            assert await session.scalar(select(func.count()).select_from(Run)) == 1
            stored = await session.get(Run, (record.tenant_id, record.run_id))
            assert (stored.state, stored.revision, stored.input, stored.native_job_ref, stored.provider_readback_ref) == (
                "succeeded", record.revision, record.input, record.native_job_ref, record.provider_readback_ref)
            assert (await session.get(AssessmentResult, (record.tenant_id, result.result_id))).payload == result.payload
            assert (await session.get(ResultDetail, (record.tenant_id, result.result_id))).payload == detail.payload
    finally:
        await engine.dispose()


async def test_publication(tmp_path):
    from l5_services.finops_reconciliation import normalize_cost_result

    now = datetime(2026, 2, 3, tzinfo=UTC)
    run, native, billing = cost_case("stale")
    record = Run(tenant_id="tenant-example", run_id=run.run_id,
                 product_id="product-example", project_id="project-example", service_id="finops",
                 configuration_id="config-example", configuration_revision=1, binding_id="binding-example",
                 trigger_id="manual-example", binding_generation=1,
                 deadline_at=datetime(2026, 2, 4, tzinfo=UTC),
                 state="succeeded", provider_state="completed", input=run.input, admission=run.admission,
                 native_job_ref=run.native_job_ref, provider_readback_ref=run.provider_readback_ref)
    cost = normalize_cost_result(record, native, billing)
    result = AssessmentResult(tenant_id=record.tenant_id, result_id="result-example",
                              run_id=record.run_id, version=1, payload={"native_artifact": native["answer_artifact"]})
    detail = ResultDetail(tenant_id=record.tenant_id, result_id=result.result_id,
                          payload={"result_id": result.result_id, "revision": 1,
                                   "scope": record.admission["scope"], **cost})
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'publication.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    admitted = {}
    requests = []
    lost_context_ack = True

    def peer(request):
        nonlocal lost_context_ack
        requests.append(request)
        destination = "product-context" if "/context/" in request.url.path else "engineering-insights"
        if request.headers["x-l5-delegation-id"] != f"grant:{destination}":
            return httpx.Response(404)
        if request.method == "POST":
            contribution = json.loads(request.content)
            assert contribution["kind"] == "finops_cost_result"
            key = (destination, contribution["contribution_id"])
            assert request.headers["idempotency-key"] == (
                f"tenant-example/result-example/1/{destination}/1.0")
            if key in admitted:
                assert admitted[key][0] == contribution
            receipt = {"receipt_id": f"receipt-{destination}", "contribution_id": contribution["contribution_id"],
                       "admitted_schema": "1.0", "consumer_revision": "1",
                       "record_refs": [contribution["result_ref"]]}
            admitted[key] = (contribution, receipt)
            if destination == "product-context" and lost_context_ack:
                lost_context_ack = False
                raise httpx.ReadTimeout("lost Context acknowledgment", request=request)
            return httpx.Response(201, json=receipt)
        key = (destination, request.url.path.split("/")[-2 if request.url.path.endswith("/projection") else -1])
        if key not in admitted:
            return httpx.Response(404)
        contribution, receipt = admitted[key]
        if request.url.path.endswith("/projection"):
            return httpx.Response(200, json={**contribution,
                                             "contribution_hash": next(item.contribution_hash
                                                 for item in publications if item.publication_id == key[1])})
        return httpx.Response(200, json=receipt)

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add_all([record, result, detail])
            publications = create_finops_publications(record, result, detail, now=now)
            session.add_all(publications)
            await session.commit()

        async with httpx.AsyncClient(transport=httpx.MockTransport(peer), base_url="http://consumer.test") as transport:
            clients = {
                destination: FinopsConsumerClient(transport, f"grant:{destination}",
                                                    destination=destination,
                                                    scope=record.admission["scope"], private_read_fencing=True)
                for destination in ("product-context", "engineering-insights")}

            async def forbidden_publish(*args):
                raise AssertionError("publication repair cannot enqueue provider work")

            service = SimpleNamespace(sessions=sessions, clock=lambda: now, publish=forbidden_publish,
                                      context_client=None, finops_context_client=clients["product-context"],
                                      finops_insights_client=clients["engineering-insights"])
            assert await repair_once(service, now=now) == 2
            async with sessions() as session:
                rows = (await session.scalars(select(Publication))).all()
                assert {row.destination: row.state for row in rows} == {
                    "product-context": "retry_wait", "engineering-insights": "published"}
                assert (await session.scalar(select(Run))).state == "succeeded"
                first_status = await ResultStore(session, [record]).finops_publication_status(result.result_id)
                assert first_status["result_id"] == result.result_id
                assert first_status["revision"] == 1
                first_consumers = {item["destination"]: item for item in first_status["consumers"]}
                assert first_consumers["product-context"]["state"] == "retry_wait"
                assert first_consumers["product-context"]["last_error"] == "transport_unavailable"
                assert first_consumers["product-context"]["projection_verified_on_ack"] is False
                assert "receipt_id" not in first_consumers["product-context"]
                assert first_consumers["engineering-insights"]["state"] == "published"
                assert first_consumers["engineering-insights"]["projection_verified_on_ack"] is True
                assert first_consumers["engineering-insights"]["receipt_id"] == "receipt-engineering-insights"
                assert first_consumers["product-context"]["publication_id"] != (
                    first_consumers["engineering-insights"]["publication_id"])
            assert await repair_once(service, now=datetime(2026, 2, 3, 0, 2, tzinfo=UTC)) == 1
            assert await repair_once(service, now=datetime(2026, 2, 3, 0, 3, tzinfo=UTC)) == 0
            async with sessions() as session:
                rows = (await session.scalars(select(Publication))).all()
                assert {row.destination: row.state for row in rows} == {
                    "product-context": "published", "engineering-insights": "published"}
                assert {row.destination: row.receipt["receipt_id"] for row in rows} == {
                    "product-context": "receipt-product-context",
                    "engineering-insights": "receipt-engineering-insights"}
                assert (await session.scalar(select(AssessmentResult))).payload == result.payload
            assert len([request for request in requests if request.method == "POST"]) == 2
            assert len(admitted) == 2
            async with sessions() as session:
                assert (await session.scalars(select(Work))).all() == []
                assert (await session.scalar(select(Run))).native_job_ref == record.native_job_ref

            current = {"allowed": True, "task_id": "original-task",
                       "entitled_tasks": {"original-task", "later-task"}}

            class Backend:
                def __init__(self, session):
                    self.session = session

                async def result_store(self, product_id, project_id, service_id):
                    if (product_id, project_id, service_id) != (
                            "product-example", "project-example", "finops"):
                        raise HTTPException(404, detail={"code": "not_found"})
                    entitled = current["allowed"] and current["task_id"] in current["entitled_tasks"]
                    async def contribution_authority(run, publication):
                        return current["allowed"] and current["task_id"] in current["entitled_tasks"]

                    return ResultStore(self.session, [record] if entitled else [], actor_id="owner-example",
                                       contribution_authority=contribution_authority)

            async def backend():
                async with sessions() as session:
                    yield Backend(session)

            app = FastAPI()
            app.include_router(create_router(backend))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://local.test") as client:
                path = ("/v1/products/product-example/projects/project-example/services/finops/"
                        "results/result-example/cost-contributions/product-context")
                status_path = path.replace("/cost-contributions/product-context", "/publication-status")
                published_status = await client.get(status_path)
                assert published_status.status_code == 200
                assert published_status.headers["cache-control"] == "no-store"
                consumers = {item["destination"]: item for item in published_status.json()["consumers"]}
                assert set(consumers) == {"product-context", "engineering-insights"}
                assert all(item["state"] == "published" and item["projection_verified_on_ack"]
                           for item in consumers.values())
                assert {item["receipt_id"] for item in consumers.values()} == {
                    "receipt-product-context", "receipt-engineering-insights"}
                assert len([request for request in requests if request.method == "POST"]) == 2
                assert (await client.get(status_path.replace("product-example", "foreign-product"))).status_code == 404
                retrieved = await client.get(path)
                assert retrieved.status_code == 200
                assert retrieved.headers["cache-control"] == "no-store"
                assert retrieved.json()["cost"]["query"] == record.input["query"]
                assert retrieved.json()["evidence_refs"][0]["artifact"] == native["answer_artifact"]
                assert retrieved.json()["citation"] == {
                    "result_ref": retrieved.json()["result_ref"],
                    "evidence_refs": retrieved.json()["evidence_refs"]}
                current["task_id"] = "later-task"
                reused = await client.get(path)
                assert reused.status_code == 200 and reused.json() == retrieved.json()
                later_use = {"task_id": current["task_id"], "scope": reused.json()["scope"],
                             "period": {key: reused.json()["cost"]["query"][key]
                                        for key in ("start", "end")},
                             "estimated": reused.json()["cost"]["oracle"]["measure"]["estimated"],
                             "freshness": reused.json()["cost"]["oracle"]["measure"]["freshness_reason"],
                             "citation": reused.json()["citation"]}
                assert later_use == {
                    "task_id": "later-task", "scope": record.admission["scope"],
                    "period": {key: record.input["query"][key] for key in ("start", "end")},
                    "estimated": billing["estimated"], "freshness": "stale",
                    "citation": {"result_ref": publications[0].payload["result_ref"],
                                 "evidence_refs": publications[0].payload["evidence_refs"]}}
                insights_path = path.replace("product-context", "engineering-insights")
                assert (await client.get(insights_path)).json()["citation"] == later_use["citation"]
                async with sessions() as session:
                    original = (await session.scalars(select(Publication).where(
                        Publication.destination == "product-context"))).one()
                    original_payload = deepcopy(original.payload)
                    original.payload = {
                        **original_payload, "cost": {
                            **original_payload["cost"], "query": {
                                **original_payload["cost"]["query"], "start": "2025-01-01"}}}
                    await session.commit()
                assert (await client.get(path)).status_code == 404
                async with sessions() as session:
                    original = (await session.scalars(select(Publication).where(
                        Publication.destination == "product-context"))).one()
                    original.payload = original_payload
                    await session.commit()
                assert (await client.get(path)).json()["citation"] == later_use["citation"]
                current["task_id"] = "foreign-task"
                assert (await client.get(path)).status_code == 404
                assert (await client.get(status_path)).status_code == 404
                current["task_id"] = "later-task"
                assert (await client.get(path.replace("product-example", "foreign-product"))).status_code == 404
                current["allowed"] = False
                assert (await client.get(path)).status_code == 404
                assert (await client.get(status_path)).status_code == 404
                current["allowed"] = True
                async with sessions() as session:
                    publication = (await session.scalars(select(Publication).where(
                        Publication.destination == "product-context"))).one()
                    publication.state = "invalidation_pending"
                    await session.commit()
                assert (await client.get(path)).status_code == 404
                invalidated_status = await client.get(status_path)
                assert invalidated_status.status_code == 200
                context = next(item for item in invalidated_status.json()["consumers"]
                               if item["destination"] == "product-context")
                assert context["state"] == "invalidation_pending"
                assert context["projection_verified_on_ack"] is False
                assert "receipt_id" not in context
    finally:
        await engine.dispose()
