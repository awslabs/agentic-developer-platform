"""Recover committed event admissions and owed work without inventing a run."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.events.readback import read_event_lineage
from l5_services.events.schedules import receive_schedule_tick
from l5_services.models.base import Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.event import EventReceipt
from l5_services.models.receipt import RunReceipt
from l5_services.models.run import Run
from l5_services.models.setup import SetupIntent, SetupWork
from l5_services.models.work import Work
from l5_services.workers.execution import ExecutionService
from l5_services.workers.relay import repair_once


@pytest.mark.asyncio
async def test_schedule_recovery_lineage_after_restart_transport_failure(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'restart.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        now = datetime(2026, 1, 1, 10, tzinfo=UTC)
        pinned = {"tenant_id": "tenant-a", "project_id": "project-a",
                  "source_connection_id": "connection-a", "record_type": "document",
                  "record_id": "doc-a", "record_revision": "fixed-revision"}
        trigger = {"trigger_id": "scheduled-a", "kind": "schedule", "enabled": True,
                   "schedule": "0 * * * *", "capability": "threat_model",
                   "binding_id": "binding-a", "source_refs": [pinned]}
        async with sessions() as session:
            session.add_all([
                Configuration(tenant_id="tenant-a", configuration_id="config-a", product_id="product-a",
                              project_id="project-a", service_id="security", relationship_revision="1",
                              owner_id="owner-a", current_revision=1, handoff_state="linked"),
                ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a", revision=1,
                                     settings={"enabled": True, "source_refs": [pinned], "triggers": [trigger]},
                                     inherited_fields=[], gaps=[], state="ready", hash="a" * 64),
                # Scheduled Security admission freezes the binding generation, so the
                # scoped ready binding the trigger names has to exist for recovery to
                # replay the same target after a restart.
                Binding(tenant_id="tenant-a", binding_id="binding-a", configuration_id="config-a",
                        product_id="product-a", project_id="project-a", service_id="security",
                        scope={"tenant_id": "tenant-a", "product_id": "product-a",
                               "project_id": "project-a"},
                        revision=1, generation=1, state="ready", ownership="adp_created",
                        connection_ref={**pinned, "record_type": "connection",
                                        "record_id": "connection-a", "record_revision": "1"},
                        connection_key="binding-a", readiness=[], gaps=[]),
            ])
            await session.commit()
            receipt = await receive_schedule_tick(
                session, tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                trigger_id="scheduled-a", scheduled_at=now, now=now,
            )
            pending = await receive_schedule_tick(
                session, tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                trigger_id="scheduled-a", scheduled_at=now + timedelta(hours=1), now=now,
            )
            run_id, work_id = str(uuid4()), str(uuid4())
            setup_id, setup_work_id = str(uuid4()), str(uuid4())
            session.add_all([
                Run(tenant_id="tenant-a", run_id=run_id, product_id="product-a", project_id="project-a",
                    service_id="security", configuration_id="config-a", configuration_revision=1,
                    binding_id="binding-a", trigger_id="scheduled-a", binding_generation=1,
                    input=receipt.run_input, admission={"event_id": receipt.event_id},
                    deadline_at=now + timedelta(hours=3)),
                RunReceipt(tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                           service_id="security", actor_id="service-a", operation="submit",
                           idempotency_key=receipt.idempotency_key, request_hash="a" * 64, run_id=run_id,
                           expires_at=now + timedelta(hours=3)),
                Work(tenant_id="tenant-a", work_id=work_id, run_id=run_id, kind="execute",
                     generation=1, step=0, due_at=now),
                SetupIntent(tenant_id="tenant-a", operation_id=setup_id, product_id="product-a",
                            project_id="project-a", service_id="security", actor_id="owner-a",
                            idempotency_key="setup-a", configuration_id="config-a", scope={},
                            request={"action": "link"}, authorization={}, intent_hash="b" * 64),
                SetupWork(tenant_id="tenant-a", work_id=setup_work_id, operation_id=setup_id,
                          generation=1, due_at=now),
            ])
            await session.commit()
            assert receipt.run_id is None and receipt.state == "pending"
            assert pending.run_id is None and pending.state == "pending"

        async with sessions() as session:
            waiting = await read_event_lineage(session, tenant_id="tenant-a", product_id="product-a",
                                               project_id="project-a", trigger_id="scheduled-a",
                                               event_id=pending.event_id)
            assert waiting["event_id"] == "schedule:2026-01-01T11:00:00Z"
            # The admitted target is the trigger frozen with the observed binding
            # generation, configuration revision and scheduled instant.
            assert waiting["target"] == {**trigger, "binding_generation": 1,
                                         "configuration_revision": 1,
                                         "scheduled_at": "2026-01-01T11:00:00Z"}
            assert waiting["run_input"]["sources"] == [pinned]
            assert waiting["received_at"].replace(tzinfo=UTC) == now
            assert waiting["retry_at"].replace(tzinfo=UTC) == now + timedelta(hours=1)
            assert (waiting["receipt_state"], waiting["recovery_attempts"], waiting["run"], waiting["work"]) == (
                "pending", 0, None, [])
            with pytest.raises(HTTPException) as wrong_scope:
                await read_event_lineage(session, tenant_id="tenant-b", product_id="product-a",
                                         project_id="project-a", trigger_id="scheduled-a",
                                         event_id=pending.event_id)
            assert wrong_scope.value.status_code == 404

        attempts = []

        async def unavailable(envelope, delay):
            attempts.append((envelope, delay))
            raise OSError("publication outage")

        first = ExecutionService(sessions, None, None, unavailable, None, clock=lambda: now)
        assert await repair_once(first, now=now) == 0
        async with sessions() as session:
            lineage = await read_event_lineage(session, tenant_id="tenant-a", product_id="product-a",
                                               project_id="project-a", trigger_id="scheduled-a",
                                               event_id=receipt.event_id)
            assert lineage["receipt_state"] == "admitted" and lineage["run"]["run_id"] == run_id
            assert lineage["event_id"] == receipt.event_id
            assert lineage["recovery_attempts"] == 0
            assert lineage["work"] == [{"work_id": work_id, "kind": "execute", "generation": 1,
                                        "dispatch_attempts": 1, "last_error": "publication_failed"}]
            untouched = await session.get(EventReceipt, ("tenant-a", "product-a", "project-a", "security",
                                                         "scheduled-a", pending.event_id))
            assert untouched.state == "pending" and untouched.run_id is None
            assert await session.scalar(select(func.count()).select_from(Run)) == 1
            setup_work = await session.get(SetupWork, ("tenant-a", setup_work_id))
            assert (setup_work.attempts, setup_work.last_error) == (1, "publication_failed")

        delivered = []

        async def available(envelope, delay):
            async with sessions() as session:
                stored = await session.get(EventReceipt, ("tenant-a", "product-a", "project-a", "security",
                                                          "scheduled-a", receipt.event_id))
                assert stored.state == "admitted" and stored.run_id == run_id
            delivered.append((envelope, delay))

        restarted = ExecutionService(sessions, None, None, available, None,
                                     clock=lambda: now + timedelta(minutes=1))
        assert await repair_once(restarted, now=now + timedelta(minutes=1)) == 2
        expected = [({"schema_version": "1.0", "work_id": identifier, "generation": 1}, 0)
                    for identifier in (work_id, setup_work_id)]
        assert delivered == expected
        assert attempts == expected
        async with sessions() as session:
            work = await session.get(Work, ("tenant-a", work_id))
            assert work.attempts == 2 and work.last_error is None
            setup_work = await session.get(SetupWork, ("tenant-a", setup_work_id))
            assert setup_work.attempts == 2 and setup_work.last_error is None
            lineage = await read_event_lineage(session, tenant_id="tenant-a", product_id="product-a",
                                               project_id="project-a", trigger_id="scheduled-a",
                                               event_id=receipt.event_id)
            assert lineage["work"][0]["work_id"] == work_id
            assert lineage["work"][0]["dispatch_attempts"] == 2
            work.completed_at = now + timedelta(minutes=2)
            setup_work.completed_at = now + timedelta(minutes=2)
            await session.commit()
            assert await session.scalar(select(func.count()).select_from(Run)) == 1
            assert await session.scalar(select(func.count()).select_from(Work)) == 1
            assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 2

        verifications = []

        async def transient(receipt):
            verifications.append(receipt.event_id)
            raise HTTPException(503, detail={"code": "work_authority_unavailable"})

        restoring = ExecutionService(sessions, None, None, available, None, clock=lambda: now,
                                     binding_authorize=unavailable, verify_event_target=transient)
        assert await repair_once(restoring, now=now + timedelta(minutes=2)) == 0
        assert verifications == []
        assert await repair_once(restoring, now=now + timedelta(hours=1)) == 0
        assert verifications == [pending.event_id]
        async with sessions() as session:
            delayed = await session.get(EventReceipt, ("tenant-a", "product-a", "project-a", "security",
                                                       "scheduled-a", pending.event_id))
            assert (delayed.state, delayed.reason, delayed.recovery_attempts) == (
                "pending", "retryable_dependency", 1)
            assert delayed.retry_at.replace(tzinfo=UTC) == now + timedelta(hours=1, minutes=1)
            retrying = await read_event_lineage(session, tenant_id="tenant-a", product_id="product-a",
                                                project_id="project-a", trigger_id="scheduled-a",
                                                event_id=pending.event_id)
            assert (retrying["reason"], retrying["recovery_attempts"], retrying["run"], retrying["attempts"]) == (
                "retryable_dependency", 1, None, [])
            assert retrying["retry_at"].replace(tzinfo=UTC) == now + timedelta(hours=1, minutes=1)
        assert await repair_once(restoring, now=now + timedelta(hours=1, seconds=30)) == 0
        assert len(verifications) == 1

        async def revoked(receipt):
            verifications.append(receipt.event_id)
            raise HTTPException(409, detail={"code": "binding_or_delegation_unavailable"})

        restoring.verify_event_target = revoked
        assert await repair_once(restoring, now=now + timedelta(hours=1, minutes=1)) == 0
        async with sessions() as session:
            rejected = await session.get(EventReceipt, ("tenant-a", "product-a", "project-a", "security",
                                                        "scheduled-a", pending.event_id))
            assert (rejected.state, rejected.reason, rejected.recovery_attempts) == (
                "rejected", "binding_or_delegation_unavailable", 2)
            denied = await read_event_lineage(session, tenant_id="tenant-a", product_id="product-a",
                                              project_id="project-a", trigger_id="scheduled-a",
                                              event_id=pending.event_id)
            assert (denied["receipt_state"], denied["reason"], denied["recovery_attempts"],
                    denied["run"], denied["work"], denied["attempts"]) == (
                "rejected", "binding_or_delegation_unavailable", 2, None, [], [])
            assert await session.scalar(select(func.count()).select_from(Run)) == 1
            assert await session.scalar(select(func.count()).select_from(Work)) == 1
        assert len(verifications) == 2
    finally:
        await engine.dispose()
