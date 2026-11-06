"""Independent database readback of immutable PR receipt decisions."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.events.handler import receive_document_event, receive_pr_event
from l5_services.events.recovery import repair_pending_events
from l5_services.events.readback import read_event_lineage
from l5_services.events import sources as source_admission
from l5_services.models.base import Base
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.event import EventReceipt
from l5_services.models.run import Run
from l5_services.models.receipt import RunReceipt
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.work import Work
from datetime import timedelta
from uuid import uuid4


def source(tenant, revision):
    return {"tenant_id": tenant, "project_id": "project-a", "source_connection_id": "connection-a",
            "record_type": "repository_commit", "record_id": "repository-a", "record_revision": revision}


@pytest.mark.asyncio
async def test_pr_receipt_freezes_selected_binding_and_revision_after_policy_edit(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        triggers = [{"trigger_id": f"trigger-{index}", "kind": "pull_request", "enabled": True,
                     "capability": "code_review", "binding_id": f"binding-{index}",
                     "source_refs": [source("tenant-a", "base-1")]} for index in (1, 2)]
        async with sessions() as session:
            session.add_all([
                Configuration(tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                              service_id="security", configuration_id="config-a", relationship_revision="1",
                              owner_id="owner-a", current_revision=1, handoff_state="linked"),
                ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a", revision=1,
                                     settings={"enabled": True, "triggers": triggers}, inherited_fields=[],
                                     gaps=[], state="ready", hash="a" * 64),
            ])
            await session.commit()

        moment = datetime(2026, 1, 1, tzinfo=UTC)
        async with sessions() as session:
            first = await receive_pr_event(session, tenant_id="tenant-a", product_id="product-a",
                                           project_id="project-a", trigger_id="trigger-2", event_id="delivery-a",
                                           source_ref=source("tenant-a", "head-2"), now=moment)
            assert (first.state, first.configuration_revision, first.run_input["binding_id"]) == (
                "pending", 1, "binding-2")
            assert first.run_input["sources"] == [source("tenant-a", "head-2")]

        async with sessions() as session:
            configuration = await session.get(Configuration, ("tenant-a", "config-a"))
            configuration.current_revision = 2
            changed = deepcopy(triggers)
            changed[1]["binding_id"] = "binding-edited"
            session.add(ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a", revision=2,
                                             settings={"enabled": False, "triggers": changed}, inherited_fields=[],
                                             gaps=[], state="disabled", hash="b" * 64))
            await session.commit()
        async with sessions() as session:
            replay = await receive_pr_event(session, tenant_id="tenant-a", product_id="product-a",
                                            project_id="project-a", trigger_id="trigger-2", event_id="delivery-a",
                                            source_ref=source("tenant-a", "head-2"), now=moment)
            assert replay.run_input == first.run_input
            assert replay.target["binding_id"] == "binding-2"
            with pytest.raises(HTTPException) as conflict:
                await receive_pr_event(session, tenant_id="tenant-a", product_id="product-a",
                                       project_id="project-a", trigger_id="trigger-2", event_id="delivery-a",
                                       source_ref=source("tenant-a", "different-head"), now=moment)
            assert conflict.value.detail["code"] == "idempotency_conflict"
            rejected = await receive_pr_event(session, tenant_id="tenant-a", product_id="product-a",
                                              project_id="project-a", trigger_id="trigger-1", event_id="delivery-b",
                                              source_ref=source("tenant-a", "head-3"), now=moment)
            assert (rejected.state, rejected.reason, rejected.configuration_revision) == (
                "rejected", "trigger_disabled", 2)

        async with sessions() as session:
            stored = await session.get(EventReceipt, ("tenant-a", "product-a", "project-a", "security",
                                                      "trigger-2", "delivery-a"))
            assert stored.run_input["sources"] == [source("tenant-a", "head-2")]
            assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 2
            assert await session.scalar(select(func.count()).select_from(Run)) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_document_event_requires_enabled_matching_policy_after_refresh(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'documents.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        document = {**source("tenant-a", "document-1"), "record_type": "document", "record_id": "doc-a"}
        other = {**document, "record_id": "doc-b", "record_revision": "document-2"}
        trigger = {"trigger_id": "document-a", "kind": "document_revision", "enabled": False,
                   "capability": "threat_model", "binding_id": "binding-document", "source_refs": [document, other]}
        async with sessions() as session:
            session.add_all([
                Configuration(tenant_id="tenant-a", configuration_id="config-doc", product_id="product-a",
                              project_id="project-a", service_id="security", relationship_revision="1",
                              owner_id="owner-a", current_revision=1, handoff_state="linked"),
                ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-doc", revision=1,
                                     settings={"enabled": True, "exclusions": ["generated/**"],
                                               "triggers": [trigger]}, inherited_fields=["source_refs"],
                                     gaps=[], state="ready", hash="a" * 64),
            ])
            await session.commit()
            new_document = {**document, "record_revision": "document-3"}
            disabled = await receive_document_event(
                session, tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                trigger_id="document-a", event_id="document-event-1", source_ref=new_document,
            )
            assert (disabled.state, disabled.reason) == ("rejected", "trigger_disabled")
            configuration = await session.get(Configuration, ("tenant-a", "config-doc"))
            configuration.current_revision = 2
            enabled = {**trigger, "enabled": True}
            session.add(ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-doc", revision=2,
                                             settings={"enabled": True, "exclusions": ["generated/**"],
                                                       "triggers": [enabled]}, inherited_fields=["source_refs"],
                                             gaps=[], state="ready", hash="b" * 64))
            await session.commit()
            receipt = await receive_document_event(
                session, tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                trigger_id="document-a", event_id="document-event-2", source_ref=new_document,
            )
            assert (receipt.state, receipt.event_kind, receipt.configuration_revision) == (
                "pending", "document_revision", 2)
            assert receipt.run_input == {"configuration_revision": 2, "binding_id": "binding-document",
                                         "capability": "threat_model", "sources": [new_document, other]}
            assert (await receive_document_event(
                session, tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                trigger_id="document-a", event_id="document-event-1", source_ref=new_document,
            )).state == "rejected"
            configuration.current_revision = 3
            session.add(ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-doc", revision=3,
                                             settings={"enabled": True, "source_refs": [other],
                                                       "exclusions": ["generated/**"], "triggers": [enabled]},
                                             inherited_fields=["source_refs"], gaps=[], state="ready",
                                             hash="c" * 64))
            await session.commit()
            excluded = await receive_document_event(
                session, tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                trigger_id="document-a", event_id="document-event-3", source_ref=new_document,
            )
            assert (excluded.state, excluded.reason) == ("rejected", "source_scope_conflict")
        async with sessions() as session:
            stored_version = await session.get(ConfigurationVersion, ("tenant-a", "config-doc", 2))
            assert stored_version.settings["exclusions"] == ["generated/**"]
            assert await session.scalar(select(func.count()).select_from(Run)) == 0
            assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_document_identity_and_cursor_survive_policy_edits_across_tenants(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'document-replay.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        now = datetime(2026, 1, 1, tzinfo=UTC)

        def document(tenant, revision):
            return {**source(tenant, revision), "record_type": "document", "record_id": "doc-a"}

        async with sessions() as session:
            for tenant in ("tenant-a", "tenant-b"):
                pinned = document(tenant, "pin-1")
                session.add_all([
                    Configuration(tenant_id=tenant, configuration_id=f"config-{tenant}",
                                  product_id=f"product-{tenant}", project_id="project-a",
                                  service_id="security", relationship_revision="1", owner_id="owner-a",
                                  current_revision=1, handoff_state="linked"),
                    ConfigurationVersion(tenant_id=tenant, configuration_id=f"config-{tenant}", revision=1,
                                         settings={"enabled": True, "source_refs": [pinned], "triggers": [{
                                             "trigger_id": "document-a", "kind": "document_revision", "enabled": True,
                                             "capability": "threat_model", "binding_id": f"binding-{tenant}",
                                             "source_refs": [pinned],
                                         }]}, inherited_fields=[], gaps=[], state="ready", hash="a" * 64),
                ])
            await session.commit()
            accepted = await receive_document_event(
                session, tenant_id="tenant-a", product_id="product-tenant-a", project_id="project-a",
                trigger_id="document-a", event_id="native-event-1", source_ref=document("tenant-a", "pin-2"),
                source_cursor="signed-page-1", now=now,
            )
            other = await receive_document_event(
                session, tenant_id="tenant-b", product_id="product-tenant-b", project_id="project-a",
                trigger_id="document-a", event_id="native-event-1", source_ref=document("tenant-b", "pin-3"),
                source_cursor="signed-page-2", now=now,
            )
            assert accepted.run_input["binding_id"] == "binding-tenant-a"
            assert other.run_input["binding_id"] == "binding-tenant-b"

        async with sessions() as session:
            configuration = await session.get(Configuration, ("tenant-a", "config-tenant-a"))
            configuration.current_revision = 2
            session.add(ConfigurationVersion(
                tenant_id="tenant-a", configuration_id="config-tenant-a", revision=2,
                settings={"enabled": False, "source_refs": []}, inherited_fields=["source_refs"],
                gaps=[], state="disabled", hash="b" * 64,
            ))
            await session.commit()

        async with sessions() as session:
            replayed = await receive_document_event(
                session, tenant_id="tenant-a", product_id="product-tenant-a", project_id="project-a",
                trigger_id="document-a", event_id="native-event-1", source_ref=document("tenant-a", "pin-2"),
                source_cursor="different-page", now=now + timedelta(days=1),
            )
            assert (replayed.configuration_revision, replayed.source_cursor, replayed.received_at) == (
                1, "signed-page-1", now.replace(tzinfo=None),
            )
            assert replayed.run_input == accepted.run_input
            with pytest.raises(HTTPException) as conflict:
                await receive_document_event(
                    session, tenant_id="tenant-a", product_id="product-tenant-a", project_id="project-a",
                    trigger_id="document-a", event_id="native-event-1", source_ref=document("tenant-a", "pin-4"),
                    now=now,
                )
            assert conflict.value.detail["code"] == "idempotency_conflict"
            before = await read_event_lineage(session, tenant_id="tenant-a", product_id="product-tenant-a",
                                              project_id="project-a", trigger_id="document-a",
                                              event_id="native-event-1")
            assert (before["source_cursor"], before["receipt_state"], before["run"]) == (
                "signed-page-1", "pending", None)
            with pytest.raises(HTTPException) as foreign:
                await read_event_lineage(session, tenant_id="tenant-b", product_id="product-tenant-a",
                                         project_id="project-a", trigger_id="document-a", event_id="native-event-1")
            assert foreign.value.status_code == 404
            assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 2
            assert await session.scalar(select(func.count()).select_from(Run)) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pr_receipt_rejects_foreign_source_before_storage(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    sessions = async_sessionmaker(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with sessions() as session:
            with pytest.raises(HTTPException) as denied:
                await receive_pr_event(session, tenant_id="tenant-a", product_id="product-a",
                                       project_id="project-a", trigger_id="trigger-1", event_id="delivery-c",
                                       source_ref=source("tenant-b", "head-1"))
            assert denied.value.status_code == 422
            assert await session.scalar(select(func.count()).select_from(EventReceipt)) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_source_receipt_requires_matching_current_work_authority(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        trigger = {"trigger_id": "trigger-1", "kind": "pull_request", "enabled": True,
                   "capability": "code_review", "binding_id": "binding-1",
                   "source_refs": [source("tenant-a", "base-1")]}
        async with sessions() as session:
            session.add_all([
                Configuration(tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                              service_id="security", configuration_id="config-a", relationship_revision="1",
                              owner_id="owner-a", current_revision=1, handoff_state="linked"),
                ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a", revision=1,
                                     settings={"enabled": True, "triggers": [trigger]}, inherited_fields=[],
                                     gaps=[], state="ready", hash="a" * 64),
            ])
            await session.commit()
            receipt = await receive_pr_event(session, tenant_id="tenant-a", product_id="product-a",
                                             project_id="project-a", trigger_id="trigger-1", event_id="delivery-1",
                                             source_ref=source("tenant-a", "head-1"))

        verified = {"scope": {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a"},
                    "service_id": "security", "actor_type": "service", "configuration_id": "config-a",
                    "configuration_revision": 1, "event_id": "delivery-1", "trigger": trigger}
        forwarded = []

        async def fake_submit(session, sessions, source_client, publish, run_input, current, key, *, now):
            forwarded.append((deepcopy(run_input), deepcopy(current), key, publish))
            run_id = str(uuid4())
            work_id = str(uuid4())
            run = Run(tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                      service_id="security", configuration_id="config-a", configuration_revision=1,
                      binding_id="binding-1", binding_generation=1, trigger_id="trigger-1", run_id=run_id,
                      input=deepcopy(run_input), admission={"event_id": "delivery-1"},
                      deadline_at=datetime.now(UTC) + timedelta(hours=1))
            session.add_all((run, RunReceipt(tenant_id="tenant-a", product_id="product-a",
                                             project_id="project-a", service_id="security", actor_id="service-a",
                                             operation="submit", idempotency_key=key, request_hash="a" * 64,
                                             run_id=run_id, expires_at=datetime.now(UTC) + timedelta(hours=1)),
                             Work(tenant_id="tenant-a", work_id=work_id, run_id=run_id, kind="execute",
                                  generation=1, step=0, due_at=datetime.now(UTC), attempts=1),
                             WorkerAttempt(tenant_id="tenant-a", attempt_id=str(uuid4()), run_id=run_id,
                                           operation="submit", first_work_id=work_id, first_fence=1,
                                           idempotency_token="a" * 64, input_hash="b" * 64,
                                           started_at=datetime.now(UTC), status="started")))
            await session.commit()
            return run

        async def authorize(receipt):
            return verified, object()

        async def publish(envelope, delay):
            return None

        monkeypatch.setattr(source_admission, "submit_and_dispatch", fake_submit)
        async with sessions() as session:
            pending = await read_event_lineage(session, tenant_id="tenant-a", product_id="product-a",
                                               project_id="project-a", trigger_id="trigger-1",
                                               event_id="delivery-1")
            assert (pending["receipt_state"], pending["run"], pending["work"], pending["attempts"]) == (
                "pending", None, [], [])
        async with sessions() as session:
            with pytest.raises(HTTPException) as denied:
                async def wrong_binding(receipt):
                    return {**verified, "trigger": {**trigger, "binding_id": "other"}}, object()

                await source_admission.admit_source_receipt(session, sessions, receipt, wrong_binding, publish)
            assert denied.value.status_code == 404
            assert forwarded == []
            await session.rollback()
            service = SimpleNamespace(sessions=sessions, verify_event_target=authorize, publish=publish)
            assert await repair_pending_events(service, now=datetime.now(UTC)) == 1
            run = await session.scalar(select(Run))
            assert forwarded[0][0]["sources"] == [source("tenant-a", "head-1")]
            assert forwarded[0][1]["trigger"]["source_refs"] == [source("tenant-a", "head-1")]
            assert forwarded[0][3] is publish

        async with sessions() as session:
            stored = await session.get(EventReceipt, ("tenant-a", "product-a", "project-a", "security",
                                                      "trigger-1", "delivery-1"))
            assert (stored.run_id, stored.state) == (run.run_id, "admitted")
            lineage = await read_event_lineage(session, tenant_id="tenant-a", product_id="product-a",
                                               project_id="project-a", trigger_id="trigger-1",
                                               event_id="delivery-1")
            assert (lineage["configuration_revision"], lineage["target"]["binding_id"]) == (1, "binding-1")
            assert lineage["run_input"]["sources"] == [source("tenant-a", "head-1")]
            assert lineage["run"] == {"run_id": run.run_id, "revision": 1, "state": "admitted",
                                      "input": run.input, "event_id": "delivery-1"}
            assert (len(lineage["work"]), lineage["work"][0]["dispatch_attempts"]) == (1, 1)
            assert [attempt["status"] for attempt in lineage["attempts"]] == ["started"]
            with pytest.raises(HTTPException) as scoped:
                await read_event_lineage(session, tenant_id="tenant-b", product_id="product-a",
                                         project_id="project-a", trigger_id="trigger-1", event_id="delivery-1")
            assert scoped.value.status_code == 404
            with pytest.raises(HTTPException) as different_product:
                await read_event_lineage(session, tenant_id="tenant-a", product_id="product-b",
                                         project_id="project-a", trigger_id="trigger-1", event_id="delivery-1")
            assert different_product.value.status_code == 404

            stored.state, stored.run_id = "pending", None
            await session.commit()
        async with sessions() as session:
            interrupted = await read_event_lineage(session, tenant_id="tenant-a", product_id="product-a",
                                                    project_id="project-a", trigger_id="trigger-1",
                                                    event_id="delivery-1")
            assert interrupted["receipt_state"] == "pending"
            assert interrupted["run"]["run_id"] == run.run_id

            async def unavailable(receipt):
                raise AssertionError("already admitted event must not reverify")

            repeated = await source_admission.admit_source_receipt(session, sessions, stored, unavailable, publish)
            assert repeated.run_id == run.run_id
            assert len(forwarded) == 1
        async with sessions() as session:
            repaired = await read_event_lineage(session, tenant_id="tenant-a", product_id="product-a",
                                                project_id="project-a", trigger_id="trigger-1",
                                                event_id="delivery-1")
            assert repaired["receipt_state"] == "admitted"
    finally:
        await engine.dispose()
