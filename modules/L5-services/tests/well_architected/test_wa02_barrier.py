"""The SQL admission boundary serializes pinned WA scope against concurrent edits."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from l5_services.adapters.well_architected.binding import ProfileAdapter, ProfileSelection
from l5_services.admission import persist_admission
from l5_services.api.bindings import BindingStore
from l5_services.api.configuration import ConfigurationStore
from l5_services.models.base import Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.receipt import RunReceipt
from l5_services.models.run import Run
from l5_services.models.work import Work
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.mark.asyncio
async def test_wa_configuration_and_admission_transactions_preserve_pinned_profile(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wa-barrier.db'}", connect_args={"timeout": 10})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    tenant, product, project = "tenant-example", "product-example", "project-example"
    account, region = "123456789012", "us-east-1"
    role = f"arn:aws:iam::{account}:role/execution-example"
    broker_role = f"arn:aws:iam::{account}:role/broker-example"
    profile_arn = f"arn:aws:wellarchitected:{region}:{account}:agent-profile/example"
    source = {"tenant_id": tenant, "project_id": project, "source_connection_id": "connection-example",
              "record_type": "document", "record_id": "design-example", "record_revision": "source-1"}
    connection_ref = {**source, "record_type": "connection", "record_id": "connection-example"}
    scope = {"tenant_id": tenant, "product_id": product, "project_id": project}
    qualification = {"account_id": account, "region": region, "broker_role_arn": broker_role,
                     "execution_role_arn": role, "trust": "qualified",
                     "source_permissions": "qualified", "support": "eligible"}
    selected = ProfileSelection(tenant, product, project, account, region, broker_role,
                                role, ("SECURITY",), qualification)
    native = {"arn": profile_arn, "executionRoleArn": role, "pillars": ["SECURITY"],
              "aggregationConfiguration": [], "eligibleForArchitectureGeneration": True, "fieldErrors": []}
    client = Mock()
    client.get_agent_profile.return_value = native
    client.list_agent_contexts.return_value = {"items": []}
    adapter = ProfileAdapter(client, account_id=account, region=region)
    pinned_hash = adapter.adopt(selected, profile_arn).snapshot_hash
    profile_ref = {**source, "record_type": "agent_profile", "record_id": profile_arn,
                   "record_revision": pinned_hash}
    settings = {"source_refs": [source], "connection_ref": connection_ref,
                "well_architected": {"review_mode": "source_only", "pillars": ["SECURITY"],
                                     "execution_role_ref": {"record_id": role}},
                "profile_ref": {**source, "record_type": "product", "record_id": product}}
    changed_settings = {**settings, "source_refs": [{**source, "record_revision": "source-2"}],
                        "well_architected": {**settings["well_architected"], "execution_role_ref": {
                            "record_id": f"arn:aws:iam::{account}:role/another-role"}}}
    configuration = Configuration(tenant_id=tenant, configuration_id="configuration-example", product_id=product,
                                  project_id=project, service_id="well_architected", relationship_revision="1",
                                  owner_id="owner-example", current_revision=1, handoff_state="linked")
    version = ConfigurationVersion(tenant_id=tenant, configuration_id=configuration.configuration_id,
                                   revision=1, settings=settings, inherited_fields=[], gaps=[], state="ready",
                                   hash="a" * 64)
    binding = Binding(tenant_id=tenant, binding_id="binding-example", configuration_id=configuration.configuration_id,
                      product_id=product, project_id=project, service_id="well_architected", scope=scope,
                      revision=1, generation=1, state="ready", ownership="customer_linked",
                      connection_ref=connection_ref, connection_key="connection-example", native_resource_ref=profile_ref,
                      native_key="profile-example", provider_identity={"account_id": account,
                                                                        "region": region, "role_arn": broker_role},
                      readiness=[{"snapshot_hash": pinned_hash}], gaps=[])

    def new_admission(revision, label):
        run = Run(tenant_id=tenant, run_id=f"run-{label}", product_id=product, project_id=project,
                  service_id="well_architected", configuration_id=configuration.configuration_id,
                  configuration_revision=revision, binding_id=binding.binding_id, trigger_id="manual",
                  binding_generation=1, input={"sources": [source], "binding_id": binding.binding_id},
                  admission={"native_profile_ref": profile_ref, "source_refs": [source],
                             "execution_role_arn": role},
                  deadline_at=datetime.now(UTC) + timedelta(hours=1))
        receipt = RunReceipt(tenant_id=tenant, product_id=product, project_id=project,
                             service_id="well_architected", actor_id="owner-example", operation="submit",
                             idempotency_key=f"receipt-{label}", request_hash="a" * 64, run_id=run.run_id,
                             expires_at=datetime.now(UTC) + timedelta(hours=2))
        work = Work(tenant_id=tenant, work_id=f"work-{label}", run_id=run.run_id,
                    kind="execute", generation=1, step=0, due_at=datetime.now(UTC))
        return run, receipt, work

    try:
        async with sessions() as session:
            session.add_all((configuration, version, binding))
            await session.commit()
        async with sessions() as edit_session, sessions() as admission_session:
            selected_configuration = await edit_session.get(Configuration, (tenant, configuration.configuration_id))
            selected_version = await edit_session.get(ConfigurationVersion, (tenant, configuration.configuration_id, 1))
            await edit_session.execute(update(Configuration).where(
                Configuration.tenant_id == tenant, Configuration.configuration_id == configuration.configuration_id,
            ).values(current_revision=Configuration.current_revision))
            attempted = asyncio.Event()

            def observe_admission(connection, cursor, statement, parameters, context, executemany):
                if statement.startswith("UPDATE l5_service_configurations"):
                    attempted.set()

            event.listen(engine.sync_engine, "before_cursor_execute", observe_admission)
            late_admission = asyncio.create_task(persist_admission(admission_session, *new_admission(1, "late")))
            await asyncio.wait_for(attempted.wait(), 5)
            assert not late_admission.done()
            _, updated = await ConfigurationStore(edit_session, tenant, "owner-example").append(
                selected_configuration, selected_version, changed_settings, [], [], "ready",
                f'"{selected_version.hash}"')
            with pytest.raises(HTTPException) as stale:
                await asyncio.wait_for(late_admission, 5)
            assert stale.value.detail == {"code": "stale_revision"}
            event.remove(engine.sync_engine, "before_cursor_execute", observe_admission)
        async with sessions() as admission_session, sessions() as edit_session:
            selected_configuration = await edit_session.get(Configuration, (tenant, configuration.configuration_id))
            selected_version = await edit_session.get(ConfigurationVersion, (tenant, configuration.configuration_id, 2))
            held = asyncio.Event()
            release = asyncio.Event()
            original_commit = admission_session.commit

            async def pause_commit():
                held.set()
                await release.wait()
                await original_commit()

            admission_session.commit = pause_commit
            admitted = asyncio.create_task(persist_admission(admission_session, *new_admission(2, "first")))
            await asyncio.wait_for(held.wait(), 5)
            competing_attempted = asyncio.Event()

            def observe_edit(connection, cursor, statement, parameters, context, executemany):
                if statement.startswith("UPDATE l5_service_configurations"):
                    competing_attempted.set()

            event.listen(engine.sync_engine, "before_cursor_execute", observe_edit)
            competing = asyncio.create_task(ConfigurationStore(edit_session, tenant, "owner-example").append(
                selected_configuration, selected_version, settings, [], [], "ready", f'"{selected_version.hash}"'))
            await asyncio.wait_for(competing_attempted.wait(), 5)
            assert not competing.done()
            release.set()
            await asyncio.wait_for(admitted, 5)
            with pytest.raises(HTTPException) as fenced:
                await asyncio.wait_for(competing, 5)
            assert fenced.value.detail == {"code": "native_generation_active"}
            event.remove(engine.sync_engine, "before_cursor_execute", observe_edit)
        async with sessions() as session:
            persisted = await session.get(Run, (tenant, "run-first"))
            saved_configuration = await session.get(Configuration, (tenant, configuration.configuration_id))
            saved_binding = await session.get(Binding, (tenant, binding.binding_id))
            assert persisted.state == "admitted"
            assert persisted.admission["native_profile_ref"] == profile_ref
            assert persisted.admission["source_refs"] == [source]
            assert persisted.admission["execution_role_arn"] == role
            assert adapter.adopt(selected, profile_arn).snapshot_hash == pinned_hash
            assert saved_configuration.current_revision == 2
            assert saved_binding.state == "ready" and saved_binding.generation == 1
            assert saved_binding.native_resource_ref == profile_ref
            assert await session.scalar(select(func.count()).select_from(Run)) == 1
            store = BindingStore(session, tenant, product, project, "well_architected")
            with pytest.raises(HTTPException) as no_profile_change:
                await store.control({"action": "check", "configuration_revision": 2,
                                     "binding_id": binding.binding_id}, "while-run-active-example", '"1"',
                                    {"configuration_id": configuration.configuration_id, "configuration_revision": 2,
                                     "scope": scope, "connection_ref": connection_ref, "account_id": account,
                                     "region": region, "role_arn": broker_role,
                                     "source_role_qualification": qualification,
                                     "authorization": {"actor_id": "owner-example"}})
            assert no_profile_change.value.detail == {"code": "native_generation_active"}
            assert client.create_agent_profile.call_count == client.delete_agent_profile.call_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_wa_second_and_drifted_admissions_leave_original_work_unchanged():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    tenant, product, project = "tenant-example", "product-example", "project-example"
    account, region = "123456789012", "us-east-1"
    execution_role = f"arn:aws:iam::{account}:role/execution-example"
    broker_role = f"arn:aws:iam::{account}:role/broker-example"
    profile_arn = f"arn:aws:wellarchitected:{region}:{account}:agent-profile/example"
    selection = ProfileSelection(tenant, product, project, account, region, broker_role, execution_role,
                                 ("SECURITY",), {"account_id": account, "region": region,
                                                 "broker_role_arn": broker_role,
                                                 "execution_role_arn": execution_role,
                                                 "trust": "qualified", "source_permissions": "qualified",
                                                 "support": "eligible"})
    native = {"arn": profile_arn, "executionRoleArn": execution_role, "pillars": ["SECURITY"],
              "aggregationConfiguration": [], "eligibleForArchitectureGeneration": True, "fieldErrors": []}
    client = Mock()
    client.get_agent_profile.return_value = native
    client.list_agent_contexts.return_value = {"items": []}
    adapter = ProfileAdapter(client, account_id=account, region=region)
    pinned_hash = adapter.adopt(selection, profile_arn).snapshot_hash
    profile_ref = {"tenant_id": tenant, "project_id": project, "source_connection_id": "connection-example",
                   "record_type": "agent_profile", "record_id": profile_arn, "record_revision": pinned_hash}
    scope = {"tenant_id": tenant, "product_id": product, "project_id": project}
    configuration = Configuration(tenant_id=tenant, configuration_id="configuration-example", product_id=product,
                                  project_id=project, service_id="well_architected", relationship_revision="1",
                                  owner_id="owner-example", current_revision=1, handoff_state="linked")
    version = ConfigurationVersion(tenant_id=tenant, configuration_id="configuration-example",
                                   revision=1, settings={}, inherited_fields=[], gaps=[], state="ready", hash=pinned_hash)
    binding = Binding(tenant_id=tenant, binding_id="binding-example", configuration_id="configuration-example",
                      product_id=product, project_id=project, service_id="well_architected", scope=scope,
                      revision=1, generation=1, state="ready", ownership="customer_linked",
                      connection_ref={"record_id": "connection-example"}, connection_key="connection-example",
                      native_resource_ref=profile_ref, native_key="profile-example",
                      provider_identity={"account_id": account, "region": region, "role_arn": broker_role},
                      readiness=[{"snapshot_hash": pinned_hash}], gaps=[])

    def admission(label):
        run = Run(tenant_id=tenant, run_id=f"run-{label}", product_id=product, project_id=project,
                  service_id="well_architected", configuration_id="configuration-example",
                  configuration_revision=1, binding_id="binding-example", binding_generation=1,
                  trigger_id="manual", input={"capability": "architecture_review"},
                  admission={"native_profile_ref": profile_ref, "profile_hash": pinned_hash},
                  deadline_at=datetime.now(UTC) + timedelta(hours=1))
        receipt = RunReceipt(tenant_id=tenant, product_id=product, project_id=project,
                             service_id="well_architected", actor_id="owner-example", operation="submit",
                             idempotency_key=f"receipt-{label}", request_hash="b" * 64,
                             run_id=run.run_id, expires_at=datetime.now(UTC) + timedelta(hours=2))
        work = Work(tenant_id=tenant, work_id=f"work-{label}", run_id=run.run_id,
                    kind="execute", generation=1, step=0, due_at=datetime.now(UTC))
        return run, receipt, work

    try:
        async with sessions() as session:
            session.add_all((configuration, version, binding))
            await session.commit()
            await persist_admission(session, *admission("first"))
            with pytest.raises(HTTPException) as second:
                await persist_admission(session, *admission("second"))
            assert second.value.status_code == 409
            assert second.value.detail == {"code": "native_generation_active"}
            assert [await session.scalar(select(func.count()).select_from(model))
                    for model in (Run, RunReceipt, Work)] == [1, 1, 1]
            native["pillars"] = ["RESILIENCE"]
            observed = adapter.adopt(selection, profile_arn)
            assert observed.state == "blocked" and observed.snapshot_hash != pinned_hash
            current_binding = await session.get(Binding, (tenant, "binding-example"))
            current_binding.state = "blocked"
            current_binding.revision += 1
            current_binding.gaps = [*observed.gaps, "native_profile_drift"]
            current_binding.readiness = [*current_binding.readiness, {"snapshot_hash": observed.snapshot_hash}]
            await session.commit()
            with pytest.raises(HTTPException) as drifted:
                await persist_admission(session, *admission("drifted"))
            assert drifted.value.status_code == 409
            assert drifted.value.detail == {"code": "stale_binding_generation"}
            original = await session.get(Run, (tenant, "run-first"))
            assert original.admission["profile_hash"] == pinned_hash
            assert original.admission["native_profile_ref"] == profile_ref
            current_binding = await session.get(Binding, (tenant, "binding-example"))
            assert current_binding.readiness == [{"snapshot_hash": pinned_hash},
                                                 {"snapshot_hash": observed.snapshot_hash}]
            assert current_binding.native_resource_ref == profile_ref
            assert [await session.scalar(select(func.count()).select_from(model))
                    for model in (Run, RunReceipt, Work)] == [1, 1, 1]
            assert client.create_agent_profile.call_count == client.delete_agent_profile.call_count == 0
    finally:
        await engine.dispose()
