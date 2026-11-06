"""Schedule identity, current-delegation fencing and Context loop rejection."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.events.handler import receive_document_event
from l5_services.events.readback import read_event_lineage
from l5_services.events.schedules import receive_schedule_tick
from l5_services.events.sources import admit_source_receipt
from l5_services.models.base import Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.event import EventReceipt
from l5_services.models.run import Run
from l5_services.models.work import Work


def reference(kind, identifier, revision):
    return {"tenant_id": "tenant-a", "project_id": "project-a", "source_connection_id": "connection-a",
            "record_type": kind, "record_id": identifier, "record_revision": revision}


@pytest.mark.asyncio
async def test_schedule_ticks_freeze_distinct_targets_and_revocation_stops_admission(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ticks.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        document = reference("document", "doc-a", "pinned-document")
        repository = reference("repository_commit", "repo-a", "pinned-commit")
        triggers = [{"trigger_id": "trigger-document", "kind": "schedule", "enabled": True,
                     "schedule": "0 * * * *", "capability": "threat_model", "binding_id": "binding-document",
                     "source_refs": [document]},
                    {"trigger_id": "trigger-code", "kind": "schedule", "enabled": True,
                     "schedule": "0 * * * *", "capability": "code_review", "binding_id": "binding-code",
                     "source_refs": [repository]}]
        async with sessions() as session:
            session.add_all([
                Configuration(tenant_id="tenant-a", configuration_id="config-a", product_id="product-a",
                              project_id="project-a", service_id="security", relationship_revision="1",
                              owner_id="owner-a", current_revision=1, handoff_state="linked"),
                ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a", revision=1,
                                     settings={"enabled": True, "triggers": triggers,
                                               "source_refs": [document, repository]}, inherited_fields=[],
                                     gaps=[], state="ready", hash="a" * 64),
                *(Binding(tenant_id="tenant-a", binding_id=identifier, configuration_id="config-a",
                          product_id="product-a", project_id="project-a", service_id="security",
                          scope={"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a"},
                          revision=1, generation=3, state="ready", ownership="adp_created",
                          connection_ref=reference("connection", "connection-a", "1"),
                          connection_key=identifier, readiness=[], gaps=[])
                  for identifier in ("binding-document", "binding-code")),
            ])
            await session.commit()
            tick = datetime(2026, 1, 1, 10, tzinfo=UTC)
            first = await receive_schedule_tick(
                session, tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                trigger_id="trigger-document", scheduled_at=tick, now=tick + timedelta(minutes=2),
            )
            assert first.run_input == {"configuration_revision": 1, "binding_id": "binding-document",
                                       "capability": "threat_model", "sources": [document]}
            code = await receive_schedule_tick(
                session, tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                trigger_id="trigger-code", scheduled_at=tick, now=tick + timedelta(minutes=4),
            )
            assert code.run_input["sources"] == [repository]
            assert code.event_id == first.event_id == "schedule:2026-01-01T10:00:00Z"

        async with sessions() as session:
            async def revoked(receipt):
                raise HTTPException(409, detail={"code": "binding_or_delegation_unavailable"})

            async def publish(envelope, delay):
                raise AssertionError("revoked delegation must not enqueue work")

            with pytest.raises(HTTPException) as denied:
                await admit_source_receipt(session, sessions, first, revoked, publish, now=tick)
            assert denied.value.detail["code"] == "binding_or_delegation_unavailable"
            await session.rollback()
            configuration = await session.get(Configuration, ("tenant-a", "config-a"))
            configuration.current_revision = 2
            session.add(ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a", revision=2,
                                             settings={"enabled": False, "triggers": [], "source_refs": []},
                                             inherited_fields=[], gaps=[], state="disabled", hash="b" * 64))
            await session.commit()

        async with sessions() as session:
            duplicate = await receive_schedule_tick(
                session, tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                trigger_id="trigger-document", scheduled_at=tick.astimezone(timezone(timedelta(hours=2))),
                now=tick + timedelta(minutes=20),
            )
            assert duplicate.configuration_revision == 1 and duplicate.event_id == first.event_id
            assert duplicate.run_input["sources"] == [document]
            next_tick = await receive_schedule_tick(
                session, tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                trigger_id="trigger-document", scheduled_at=tick + timedelta(hours=1), now=tick + timedelta(hours=1),
            )
            assert (next_tick.state, next_tick.reason) == ("rejected", "trigger_disabled")
            assert next_tick.event_id != first.event_id
            lineage = await read_event_lineage(session, tenant_id="tenant-a", product_id="product-a",
                                               project_id="project-a", trigger_id="trigger-document",
                                               event_id=first.event_id)
            assert (lineage["receipt_state"], lineage["run"], lineage["work"]) == ("pending", None, [])
            assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 3
            assert await session.scalar(select(func.count()).select_from(Run)) == 0
            assert await session.scalar(select(func.count()).select_from(Work)) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_own_context_update_has_a_rejection_receipt_without_creating_work(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'context.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        pinned = reference("document", "doc-a", "pinned-revision")
        trigger = {"trigger_id": "doc-trigger", "kind": "document_revision", "enabled": True,
                   "capability": "threat_model", "binding_id": "binding-a", "source_refs": [pinned]}
        async with sessions() as session:
            session.add_all([
                Configuration(tenant_id="tenant-a", configuration_id="config-a", product_id="product-a",
                              project_id="project-a", service_id="security", relationship_revision="1",
                              owner_id="owner-a", current_revision=1, handoff_state="linked"),
                ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a", revision=1,
                                     settings={"enabled": True, "source_refs": [pinned], "triggers": [trigger]},
                                     inherited_fields=[], gaps=[], state="ready", hash="a" * 64),
            ])
            await session.commit()
            origin = {"source": "context", "service_id": "security", "run_id": "own-run-a"}
            ignored = await receive_document_event(
                session, tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                trigger_id="doc-trigger", event_id="context-event-a",
                source_ref={**pinned, "record_revision": "new-revision"}, causal_origin=origin,
            )
            assert (ignored.state, ignored.reason, ignored.causal_origin) == ("rejected", "self_caused", origin)
            await session.commit()
        async with sessions() as session:
            persisted = await read_event_lineage(session, tenant_id="tenant-a", product_id="product-a",
                                                 project_id="project-a", trigger_id="doc-trigger",
                                                 event_id="context-event-a")
            assert (persisted["causal_origin"], persisted["receipt_state"], persisted["reason"]) == (
                origin, "rejected", "self_caused")
            assert persisted["run"] is None and persisted["work"] == []
            assert await session.scalar(select(func.count()).select_from(Run)) == 0
            assert await session.scalar(select(func.count()).select_from(Work)) == 0
    finally:
        await engine.dispose()
