"""Offline WA02 configuration and binding contract tests."""

import asyncio
import hashlib
import json
from copy import deepcopy

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.api.configuration import ConfigurationStore
from l5_services.configuration_readiness import assess_readiness
from l5_services.models.base import Base


def reference(kind, identifier, revision="1"):
    return {"tenant_id": "tenant-example", "project_id": "project-example",
            "source_connection_id": "connection-example", "record_type": kind,
            "record_id": identifier, "record_revision": revision}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["edit", "refresh"])
@pytest.mark.parametrize("explicit", [False, True])
async def test_existing_repository_pin_normalizes_without_scope_change(operation, explicit):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repository = reference("repository", "repository-example", "a" * 40)
    commit = {**repository, "record_type": "repository_commit"}
    profile_ref = reference("product", "product-example")
    profile = {"tenant_id": "tenant-example", "project_id": "project-example", "product_id": "product-example", "revision": 1,
               "configuration": {"basic": {"repositories": [repository]}}}
    available = {"coverage": "complete", "next_cursor": None,
                 "items": [{"connection_ref": {"id": "connection-example"}, "resources": [repository]}]}
    choices = {"service_id": "well_architected", "capabilities": ["architecture_review"], "enabled": False,
               "well_architected": {"review_mode": "source_only", "pillars": ["SECURITY"]},
               "triggers": [{"kind": "manual", "enabled": False, "source_refs": [repository]}]}
    if explicit:
        choices.update(source_refs=[repository], overrides=[{"field": "source_refs", "value": [repository], "reason": "Keep exact commit"}])
    try:
        async with sessions() as session:
            store = ConfigurationStore(session, "tenant-example", "user-example")
            configuration, previous = await store.create("product-example", "project-example", "well_architected",
                                                         1, profile, profile_ref, available, choices)
            # Model a pre-fix persisted version. Its immutable history remains unchanged.
            legacy = deepcopy(previous.settings)
            legacy["source_refs"] = [repository]
            legacy["triggers"][0]["source_refs"] = [repository]
            if explicit:
                legacy["overrides"][0]["value"] = [repository]
            previous.settings = legacy
            previous.hash = hashlib.sha256(json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            await session.commit()
            if operation == "edit":
                _, current = await store.edit(configuration, previous, profile, available, {}, f'"{previous.hash}"')
            else:
                _, current = await store.refresh(configuration, previous, {**profile, "revision": 2},
                                                  {**profile_ref, "record_revision": "2"}, available, f'"{previous.hash}"')
            assert current.revision == 2
            assert current.settings["source_refs"] == [commit]
            assert current.settings["triggers"][0]["source_refs"] == [commit]
            assert "source_scope_changed" not in current.gaps
            assert previous.settings == legacy
            assert profile["configuration"]["basic"]["repositories"] == [repository]
            if explicit:
                assert current.settings["overrides"][0]["value"] == [commit]
            new_repository = {**repository, "record_revision": "b" * 40}
            newer = {**profile, "revision": 3, "configuration": {"basic": {"repositories": [new_repository]}}}
            new_available = {**available, "items": [{"connection_ref": {"id": "connection-example"}, "resources": [new_repository]}]}
            if explicit:
                with pytest.raises(HTTPException) as conflict:
                    await store.refresh(configuration, current, newer, {**profile_ref, "record_revision": "3"},
                                        new_available, f'"{current.hash}"')
                assert conflict.value.detail["code"] == "source_scope_conflict"
            else:
                _, changed = await store.refresh(configuration, current, newer, {**profile_ref, "record_revision": "3"},
                                                  new_available, f'"{current.hash}"')
                assert changed.settings["source_refs"] == []
                assert "source_scope_changed" in changed.gaps
                assert current.settings["source_refs"] == [commit]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_configuration_choices_and_readiness(monkeypatch):
    import boto3

    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: pytest.fail("saving called AWS"))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    document = reference("document", "architecture-example")
    repository = reference("repository_commit", "repository-example")
    connection_ref = reference("connection", "connection-example")
    profile_ref = reference("product", "product-example")
    profile = {"tenant_id": "tenant-example", "project_id": "project-example",
               "product_id": "product-example", "revision": 1,
               "configuration": {"basic": {"documents": [document]}}}
    available = {"coverage": "complete", "next_cursor": None,
                 "items": [{"connection_ref": {"id": "connection-example"},
                            "record_ref": connection_ref, "resources": [document, repository]}]}
    choices = {"service_id": "well_architected", "capabilities": ["architecture_review"],
               "source_refs": [document], "connection_ref": connection_ref,
               "overrides": [{"field": "source_refs", "value": [document], "reason": "Review selected design"},
                             {"field": "connection_ref", "value": connection_ref, "reason": "Selected account"}],
               "well_architected": {"review_mode": "source_only", "pillars": ["SECURITY"]},
               "triggers": [{"kind": "manual", "enabled": False}], "enabled": False}
    try:
        async with sessions() as session:
            store = ConfigurationStore(session, "tenant-example", "user-example")
            configuration, old = await store.create("product-example", "project-example", "well_architected",
                                                    1, profile, profile_ref, available, choices)
            assert old.settings["source_refs"] == [document]
            assert old.settings["enabled"] is False
            assert old.settings["triggers"] == choices["triggers"]
            assert assess_readiness(old.settings | {"enabled": True}, configuration.configuration_id, [],
                                    delegation_active=True, service_id="well_architected")[1] == [
                "catalog_mapping_unresolved", "provider_unqualified", "region", "classification", "limits",
                "well_architected.execution_role_ref", "well_architected.source_store_ref",
            ]
            newer = {**profile, "revision": 2, "configuration": {
                "basic": {"documents": [document], "repositories": [repository]}}}
            _, refreshed = await store.refresh(configuration, old, newer, reference("product", "product-example", "2"),
                                               available, f'"{old.hash}"')
            assert refreshed.settings["source_refs"] == [document]
            assert refreshed.settings["overrides"] == choices["overrides"]
            assert refreshed.settings["well_architected"] == choices["well_architected"]
            assert refreshed.settings["triggers"] == choices["triggers"]
            assert refreshed.settings["profile_ref"]["record_revision"] == "2"
            with pytest.raises(HTTPException) as stale:
                await store.refresh(configuration, old, newer, reference("product", "product-example", "2"),
                                    available, f'"{old.hash}"')
            assert stale.value.status_code == 412
            await session.refresh(configuration)
            await session.refresh(refreshed)

            from datetime import UTC, datetime, timedelta

            from sqlalchemy import func, select

            from l5_services.models.configuration import ConfigurationVersion
            from l5_services.models.run import Run

            run = Run(tenant_id="tenant-example", run_id="pinned-generation", product_id="product-example",
                      project_id="project-example", service_id="well_architected",
                      configuration_id=configuration.configuration_id, configuration_revision=refreshed.revision,
                      binding_id="binding-example", binding_generation=1, trigger_id="manual",
                      input={}, admission={}, scope={}, state="running", provider_state="unknown",
                      deadline_at=datetime.now(UTC) + timedelta(hours=1))
            session.add(run)
            await session.commit()
            for action in ("refresh", "edit", "sources"):
                with pytest.raises(HTTPException) as fenced:
                    if action == "refresh":
                        await store.refresh(configuration, refreshed,
                                            {**newer, "revision": 3}, reference("product", "product-example", "3"),
                                            available, f'"{refreshed.hash}"')
                    else:
                        changes = ({"well_architected": {"review_mode": "source_only", "pillars": ["RESILIENCE"]}}
                                   if action == "edit" else {"source_refs": [repository],
                                       "overrides": [{"field": "source_refs", "value": [repository], "reason": "New source"},
                                                     choices["overrides"][1]]})
                        await store.edit(configuration, refreshed, newer, available, changes, f'"{refreshed.hash}"')
                assert fenced.value.status_code == 409
                assert fenced.value.detail == {"code": "native_generation_active"}
                await session.refresh(configuration)
                await session.refresh(refreshed)
                assert configuration.current_revision == refreshed.revision == 2
                assert await session.scalar(select(func.count()).select_from(ConfigurationVersion)) == 2
            run = await session.get(Run, ("tenant-example", "pinned-generation"))
            run.state = "succeeded"
            run.provider_state = "completed"
            run.native_job_ref = {"record_id": "native-example"}
            run.provider_readback_ref = {"record_id": "terminal-example"}
            await session.commit()
            _, after = await store.edit(configuration, refreshed, newer, available,
                                        {"well_architected": {"review_mode": "source_only", "pillars": ["RESILIENCE"]}},
                                        f'"{refreshed.hash}"')
            assert after.revision == 3 and after.settings["well_architected"]["pillars"] == ["RESILIENCE"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_scoped_configuration_api_retries_only_profile_handoff():
    import httpx
    from fastapi import FastAPI

    from l5_services.api.router import create_router
    from l5_services.profile_handoff import link_configuration

    profile = {"tenant_id": "tenant-example", "project_id": "project-example", "revision": 1,
               "configuration": {"selected_capabilities": [{"capability": "architecture_review"}]}}
    profile_ref = reference("product", "product-example")
    configuration_ref = reference("l5_configuration", "configuration-example")
    save_calls = []
    patch_calls = []
    version = {"hash": "version-example", "settings": {"profile_ref": profile_ref}, "handoff_state": "pending"}

    class Backend:
        async def save(self, product_id, project_id, service_id, settings, idempotency_key, if_none_match, if_match):
            save_calls.append(settings)
            version["settings"] = settings | {"profile_ref": profile_ref}
            return await self._handoff()

        async def get(self, product_id, project_id, service_id):
            return version

        async def refresh(self, product_id, project_id, service_id, incoming, idempotency_key, if_match):
            if if_match != '"version-example"':
                raise HTTPException(412, detail={"code": "stale_revision"})
            return version

        async def retry_handoff(self, product_id, project_id, service_id, idempotency_key, if_match):
            if if_match != '"version-example"':
                raise HTTPException(412, detail={"code": "stale_revision"})
            return await self._handoff()

        async def _handoff(self):
            async def patch_profile(patch, revision, key):
                patch_calls.append((patch, revision))
                if len(patch_calls) == 1:
                    raise HTTPException(412, detail={"code": "stale_profile_revision"})
                profile["configuration"] = patch["configuration"]
                profile["revision"] += 1

            version["handoff_state"] = await link_configuration(
                lambda: read_profile(), patch_profile, configuration_ref, ["architecture_review"], "key-example",
            )
            return version

    async def read_profile():
        return profile

    app = FastAPI()
    app.include_router(create_router(lambda: Backend()))
    path = "/v1/products/product-example/projects/project-example/services/well_architected/configuration"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        created = await client.put(path, json={"service_id": "well_architected", "well_architected": {
            "review_mode": "source_only", "pillars": ["SECURITY"]}},
            headers={"Idempotency-Key": "create-example-0001", "If-None-Match": "*"})
        assert created.status_code == 201
        assert created.json()["handoff_state"] == "conflict"
        assert created.headers["etag"] == '"version-example"'
        assert (await client.get(path)).json()["settings"]["well_architected"]["pillars"] == ["SECURITY"]
        stale = await client.post(path + "/refresh", json={"profile_ref": profile_ref},
            headers={"Idempotency-Key": "refresh-example-01", "If-Match": '"old-version"'})
        assert stale.status_code == 412
        retried = await client.post(path + "/handoff", headers={
            "Idempotency-Key": "handoff-example-01", "If-Match": '"version-example"'})
        assert retried.status_code == 200
        assert retried.json()["handoff_state"] == "linked"
    assert len(save_calls) == 1
    assert len(patch_calls) == 2
    assert profile["configuration"]["selected_capabilities"][0]["configuration_ref"] == configuration_ref


@pytest.mark.asyncio
async def test_refresh_edit_requires_explicit_source_expansion():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    document = reference("document", "architecture-example")
    repository = reference("repository_commit", "repository-example")
    profile_ref = reference("product", "product-example")
    profile = {"tenant_id": "tenant-example", "project_id": "project-example",
               "product_id": "product-example", "revision": 1,
               "configuration": {"basic": {"documents": [document]}}}
    available = {"coverage": "complete", "next_cursor": None, "items": [{
        "resources": [document, repository]}]}
    choices = {"service_id": "well_architected", "capabilities": ["architecture_review"],
               "well_architected": {"review_mode": "source_only", "pillars": ["SECURITY"]},
               "enabled": False}
    try:
        async with sessions() as session:
            store = ConfigurationStore(session, "tenant-example", "user-example")
            configuration, first = await store.create(
                "product-example", "project-example", "well_architected", 1,
                profile, profile_ref, available, choices,
            )
            newer = {**profile, "revision": 2, "configuration": {"basic": {
                "documents": [document], "repositories": [repository]}}}
            new_ref = reference("product", "product-example", "2")
            _, refreshed = await store.refresh(configuration, first, newer, new_ref, available, f'"{first.hash}"')
            assert refreshed.settings["source_refs"] == [document]
            assert "source_scope_changed" in refreshed.gaps
            refreshed_hash = refreshed.hash
            _, edited = await store.edit(configuration, refreshed, newer, available,
                                         {"enabled": True}, f'"{refreshed.hash}"')
            assert edited.settings["source_refs"] == [document]
            assert "source_scope_changed" in edited.gaps
            revoked = {**available, "items": [{"resources": [repository]}]}
            with pytest.raises(HTTPException) as revoked_source:
                await store.edit(configuration, edited, newer, revoked,
                                 {"enabled": False}, f'"{edited.hash}"')
            assert revoked_source.value.status_code == 409
            with pytest.raises(HTTPException) as unjustified:
                await store.edit(configuration, edited, newer, available,
                    {"source_refs": [document, repository]}, f'"{edited.hash}"')
            assert unjustified.value.status_code == 422
            sources = [document, repository]
            _, widened = await store.edit(configuration, edited, newer, available, {
                "source_refs": sources,
                "overrides": [{"field": "source_refs", "value": sources, "reason": "Approve both sources"}],
            }, f'"{edited.hash}"')
            assert widened.settings["source_refs"] == sources
            assert "source_scope_changed" not in widened.gaps
            assert first.settings["profile_ref"] == profile_ref
            assert first.settings["source_refs"] == [document]
            with pytest.raises(HTTPException) as stale:
                await store.edit(configuration, refreshed, newer, available,
                                 {"enabled": True}, f'"{refreshed_hash}"')
            assert stale.value.status_code == 412
    finally:
        await engine.dispose()


def test_source_only_profile_adapter_reads_qualified_owned_and_adopted_profiles():
    from unittest.mock import Mock

    from l5_services.adapters.well_architected.binding import ProfileAdapter, ProfileIdentityError, ProfileSelection

    account = "000000000000"
    region = "us-east-1"
    broker_role = f"arn:aws:iam::{account}:role/broker-example"
    execution_role = f"arn:aws:iam::{account}:role/source-only-example"
    profile_arn = f"arn:aws:wellarchitected:{region}:{account}:agent-profile/profile-example"
    qualification = {"account_id": account, "region": region, "broker_role_arn": broker_role,
                     "execution_role_arn": execution_role, "trust": "qualified",
                     "source_permissions": "qualified", "support": "eligible"}
    selected = ProfileSelection("tenant-example", "product-example", "project-example", account, region,
                                broker_role, execution_role, ("SECURITY", "RESILIENCE"), qualification)
    fingerprint = selected.fingerprint()
    native = {"arn": profile_arn, "name": ProfileAdapter._created_name(fingerprint, "durable-operation-example"),
              "tags": [{"key": "adp:scope", "value": fingerprint}], "pillars": ["RESILIENCE", "SECURITY"],
              "executionRoleArn": execution_role, "aggregationConfiguration": [],
              "eligibleForArchitectureGeneration": True, "eligibleForScheduledGeneration": True,
              "fieldErrors": []}
    client = Mock()
    client.create_agent_profile.return_value = {"arn": profile_arn}
    client.get_agent_profile.return_value = native
    client.list_agent_contexts.return_value = {"items": []}
    adapter = ProfileAdapter(client, account_id=account, region=region)
    owned = adapter.create(selected, "durable-operation-example")
    assert owned.state == "ready" and owned.gaps == ()
    assert owned.eligibility is True and owned.scheduled_eligibility is True
    assert client.create_agent_profile.call_args.kwargs == {
        "name": native["name"], "pillars": ["SECURITY", "RESILIENCE"],
        "executionRoleArn": execution_role, "aggregationConfiguration": [],
        "clientToken": hashlib.sha256(f"{fingerprint}:durable-operation-example".encode()).hexdigest(),
        "tags": [{"key": "adp:scope", "value": fingerprint}],
    }
    from botocore.session import get_session
    from botocore.validate import ParamValidator

    model = get_session().get_service_model("wellarchitected").operation_model("CreateAgentProfile")
    assert not ParamValidator().validate(client.create_agent_profile.call_args.kwargs,
                                         model.input_shape).has_errors()
    assert adapter.adopt(selected, profile_arn).snapshot_hash == owned.snapshot_hash
    assert client.create_agent_profile.call_count == 1

    for changed in ({"executionRoleArn": broker_role}, {"aggregationConfiguration": [{}]},
                    {"pillars": ["SECURITY"]}, {"eligibleForArchitectureGeneration": None},
                    {"fieldErrors": [{"fieldName": "executionRoleArn"}]}):
        client.get_agent_profile.return_value = native | changed
        assert adapter.adopt(selected, profile_arn).state == "blocked"
    client.get_agent_profile.return_value = native
    client.list_agent_contexts.side_effect = [{"items": [], "nextToken": "second"},
                                             {"items": [{"profileArn": profile_arn, "id": "foreign-context"}]}]
    assert adapter.adopt(selected, profile_arn).gaps == ("unrequested_native_contexts",)
    client.list_agent_contexts.side_effect = None
    client.list_agent_contexts.return_value = {"items": []}
    with pytest.raises(ProfileIdentityError):
        adapter.adopt(selected, profile_arn.replace(account, "111111111111"))
    with pytest.raises(ProfileIdentityError):
        ProfileAdapter(client, account_id="111111111111", region=region).create(selected, "operation")
    for unqualified in (qualification | {"trust": "unknown"}, qualification | {"support": "unknown"},
                        qualification | {"execution_role_arn": broker_role}):
        with pytest.raises(ProfileIdentityError):
            adapter.create(ProfileSelection("tenant-example", "product-example", "project-example", account,
                                            region, broker_role, execution_role,
                                            ("SECURITY", "RESILIENCE"), unqualified), "operation")
    assert client.create_agent_profile.call_count == 1


@pytest.mark.asyncio
async def test_native_profile_claim_survives_unlink_and_connection_alias():
    from unittest.mock import Mock

    from sqlalchemy import func, select

    from l5_services.adapters.well_architected.binding import ProfileBindingProvider, ProfileSelection
    from l5_services.api.bindings import BindingStore
    from l5_services.models.binding import Binding
    from l5_services.models.configuration import Configuration
    from l5_services.models.profile_claim import ProfileClaim

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    account = "000000000000"
    region = "us-east-1"
    profile_arn = f"arn:aws:wellarchitected:{region}:{account}:agent-profile/profile-example"
    broker_role = f"arn:aws:iam::{account}:role/broker-example"
    execution_role = f"arn:aws:iam::{account}:role/source-only-example"
    qualification = {"account_id": account, "region": region, "broker_role_arn": broker_role,
                     "execution_role_arn": execution_role, "trust": "qualified",
                     "source_permissions": "qualified", "support": "eligible"}
    native = {"arn": profile_arn, "name": "customer-owned", "pillars": ["SECURITY"],
              "executionRoleArn": execution_role, "aggregationConfiguration": [],
              "eligibleForArchitectureGeneration": True, "fieldErrors": []}
    client = Mock()
    client.get_agent_profile.return_value = native
    client.list_agent_contexts.return_value = {"items": []}

    async def scope(session, project_id, connection_id):
        tenant_id = "tenant-example"
        configuration_id = f"configuration-{project_id}"
        session.add(Configuration(tenant_id=tenant_id, configuration_id=configuration_id,
            product_id="product-example", project_id=project_id, service_id="well_architected",
            relationship_revision="1", owner_id="owner-example", current_revision=1,
            handoff_state="linked"))
        await session.commit()
        selected_connection = {"tenant_id": tenant_id, "project_id": project_id,
            "source_connection_id": connection_id, "record_type": "connection",
            "record_id": connection_id, "record_revision": "1"}
        verified = {"configuration_id": configuration_id, "configuration_revision": 1,
            "scope": {"tenant_id": tenant_id, "product_id": "product-example", "project_id": project_id,
                      "relationship_revision": "1", "owner_id": "owner-example", "visibility": "only_me"},
            "connection_ref": selected_connection, "account_id": account, "region": region,
            "role_arn": broker_role, "source_role_qualification": qualification,
            "authorization": {"actor_id": "owner-example", "delegation_id": "delegation-example",
                              "delegation_revision": 1}}
        selected = ProfileSelection(tenant_id, "product-example", project_id, account, region,
                                    broker_role, execution_role, ("SECURITY",), qualification)
        provider = ProfileBindingProvider(client, selected, selected_connection)
        revision = provider.adapter.adopt(selected, profile_arn).snapshot_hash
        native_ref = {"tenant_id": tenant_id, "project_id": project_id,
                      "source_connection_id": connection_id, "record_type": "agent_profile",
                      "record_id": profile_arn, "record_revision": revision}
        return BindingStore(session, tenant_id, "product-example", project_id, "well_architected"), verified, provider, native_ref

    try:
        async with sessions() as session:
            original, verified, provider, native_ref = await scope(session, "project-example", "connection-example")
            with pytest.raises(HTTPException) as unqualified:
                await original.start({"action": "link", "configuration_revision": 1,
                                      "native_resource_ref": native_ref}, "no-qualification-0001",
                                     {**verified, "source_role_qualification": None})
            assert unqualified.value.status_code == 409
            operation = await original.start({"action": "link", "configuration_revision": 1,
                                              "native_resource_ref": native_ref}, "first-link-example", verified)
            attached = await original.finish_link(operation.operation_id, verified, provider)
            assert attached.state == "succeeded"
            binding = await original.binding(attached.binding_id)
            assert binding.state == "ready" and binding.ownership == "customer_linked"
            unlink = await original.control({"action": "unlink", "configuration_revision": 1,
                                             "binding_id": binding.binding_id}, "unlink-example", '"1"', verified)
            assert (await original.finish_control(unlink.operation_id, verified, provider)).state == "succeeded"
            assert (await original.binding(binding.binding_id)).state == "unlinked"
            other, other_verified, other_provider, other_ref = await scope(session, "project-other", "alias-other")
            conflict = await other.start({"action": "link", "configuration_revision": 1,
                                          "native_resource_ref": other_ref}, "second-link-example", other_verified)
            with pytest.raises(HTTPException) as denied:
                await other.finish_link(conflict.operation_id, other_verified, other_provider)
            assert denied.value.status_code == 404
            assert await session.scalar(select(func.count()).select_from(ProfileClaim)) == 1
            assert await session.scalar(select(func.count()).select_from(Binding)) == 1
            assert client.create_agent_profile.call_count == 0
            from l5_services.models.setup import BindingOperation, SetupWork
            from l5_services.workers.bindings import execute_setup_work

            owned_arn = profile_arn.replace("profile-example", "owned-example")
            fingerprint = provider.selection.fingerprint()
            created = await original.start({"action": "create", "configuration_revision": 1},
                                           "owned-create-example", verified)
            created_id = created.operation_id
            client.get_agent_profile.return_value = native | {
                "arn": owned_arn, "name": provider.adapter._created_name(fingerprint, created_id),
                "tags": [{"key": "adp:scope", "value": fingerprint}],
            }
            client.create_agent_profile.return_value = {"arn": owned_arn}
            work = await session.scalar(select(SetupWork).where(SetupWork.operation_id == created.operation_id))
            work_id = work.work_id
            await session.commit()

            async def authorize(intent, claim):
                assert intent.operation_id == created.operation_id
                assert claim.operation_id == intent.operation_id and claim.work_id == work_id
                return verified, provider

            assert await execute_setup_work(sessions, {"schema_version": "1.0",
                "work_id": work_id, "generation": 1}, authorize)
            session.expire_all()
            owned_operation = await session.get(BindingOperation, ("tenant-example", created_id))
            assert owned_operation.state == "succeeded"
            owned_binding = await original.binding(owned_operation.binding_id)
            assert owned_binding.ownership == "adp_created" and owned_binding.state == "ready"
            assert owned_binding.creation_receipt["operation_id"] == created_id
            assert await session.scalar(select(func.count()).select_from(ProfileClaim)) == 2
            check = await original.control({"action": "check", "configuration_revision": 1,
                "binding_id": owned_binding.binding_id}, "owned-check-example", '"1"', verified)
            assert (await original.finish_control(check.operation_id, verified, provider)).state == "succeeded"
            assert client.create_agent_profile.call_count == 1

            from datetime import UTC, datetime, timedelta

            from botocore.exceptions import ClientError

            from l5_services.models.evidence import AssessmentResult
            from l5_services.models.run import Run

            late_link = await original.start({"action": "link", "configuration_revision": 1,
                                              "native_resource_ref": owned_binding.native_resource_ref},
                                             "link-before-generation", verified)
            late_create = await original.start({"action": "create", "configuration_revision": 1},
                                               "create-before-generation", verified)
            active = Run(tenant_id="tenant-example", run_id="active-example", product_id="product-example",
                         project_id="project-example", service_id="well_architected",
                         configuration_id=owned_binding.configuration_id, configuration_revision=1,
                         binding_id=owned_binding.binding_id, binding_generation=owned_binding.generation,
                         trigger_id="manual", revision=1, input={}, admission={}, scope=verified["scope"],
                         state="running", provider_state="running", deadline_at=datetime.now(UTC) + timedelta(hours=1))
            session.add(active)
            await session.commit()
            late_result = await original.finish_link(late_link.operation_id, verified, provider)
            assert late_result.state == "blocked" and late_result.gaps == ["native_scope_change_fenced"]
            late_work = await session.scalar(select(SetupWork).where(SetupWork.operation_id == late_link.operation_id))
            assert late_work.blocked_at is not None and late_work.last_error == "native_scope_change_fenced"
            create_result = await original.finish_create(late_create.operation_id, verified, provider)
            assert create_result.state == "blocked" and create_result.gaps == ["native_scope_change_fenced"]
            create_work = await session.scalar(select(SetupWork).where(SetupWork.operation_id == late_create.operation_id))
            assert create_work.blocked_at is not None and create_work.last_error == "native_scope_change_fenced"
            assert client.create_agent_profile.call_count == 1
            latest = await original.binding(owned_binding.binding_id)
            for action in ("check", "update", "suspend", "resume", "unlink"):
                with pytest.raises(HTTPException) as fenced:
                    await original.control({"action": action, "configuration_revision": 1,
                                            "binding_id": latest.binding_id},
                                           f"fenced-{action}", f'"{latest.revision}"', verified)
                assert fenced.value.detail == {"code": "native_generation_active"}
            with pytest.raises(HTTPException) as fenced:
                await original.start({"action": "link", "configuration_revision": 1,
                                      "native_resource_ref": owned_binding.native_resource_ref},
                                     "fenced-link-example", verified)
            assert fenced.value.detail == {"code": "native_generation_active"}
            assert client.create_agent_profile.call_count == 1
            assert client.delete_agent_profile.call_count == 0
            request = {"action": "cleanup", "configuration_revision": 1,
                       "binding_id": latest.binding_id, "owned_cleanup_confirmed": True}
            alias = Binding(tenant_id="tenant-example", binding_id="adopted-alias-example",
                            product_id="product-example", project_id="project-example",
                            service_id="well_architected", configuration_id=latest.configuration_id,
                            scope=latest.scope, revision=1, generation=1, state="unlinked",
                            ownership="customer_linked", connection_ref=latest.connection_ref,
                            connection_key="adopted-alias-example", native_resource_ref=latest.native_resource_ref,
                            native_key="adopted-alias-example", provider_identity=latest.provider_identity,
                            readiness=[], gaps=[])
            session.add(alias)
            await session.commit()
            with pytest.raises(HTTPException) as referenced:
                await original.control(request, "referenced-cleanup", f'"{latest.revision}"', verified)
            assert referenced.value.detail == {"code": "native_profile_still_referenced"}
            assert client.delete_agent_profile.call_count == 0
            await session.delete(alias)
            await session.commit()
            with pytest.raises(HTTPException) as unsafe:
                await original.control(request, "active-run-cleanup", f'"{latest.revision}"', verified)
            assert unsafe.value.status_code == 409
            assert unsafe.value.detail == {"code": "native_run_not_terminal"}
            assert client.delete_agent_profile.call_count == 0

            active.state = "succeeded"
            active.provider_state = "completed"
            active.native_job_ref = {"record_id": "completed-native-job"}
            active.provider_readback_ref = {"record_id": "terminal-status"}
            session.add(AssessmentResult(tenant_id="tenant-example", result_id="result-example",
                                         run_id=active.run_id, version=1, payload={}))
            await session.commit()
            from copy import deepcopy

            native_owned = client.get_agent_profile.return_value | {"deletionProtection": False}
            native_adopted = deepcopy(native)
            profiles = {profile_arn: native_adopted, owned_arn: native_owned}
            adopted_before = deepcopy(profiles[profile_arn])
            list_after_delete = 0
            deny_delete = True

            def native_get(*, profileArn):
                if profileArn not in profiles:
                    raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
                                      "GetAgentProfile")
                return deepcopy(profiles[profileArn])

            def native_list(**parameters):
                nonlocal list_after_delete
                assert parameters == {}
                items = [{"arn": profile_arn, "name": "customer-owned"}]
                if owned_arn in profiles:
                    items.append({"arn": owned_arn, "name": native_owned["name"]})
                elif list_after_delete == 0:
                    items.append({"arn": owned_arn, "name": native_owned["name"]})
                    list_after_delete += 1
                return {"items": items}

            def native_delete(*, profileArn):
                assert profileArn == owned_arn and profile_arn in profiles
                if deny_delete:
                    raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
                                      "DeleteAgentProfile")
                del profiles[profileArn]
                return {}

            client.get_agent_profile.side_effect = native_get
            client.list_agent_profiles.side_effect = native_list
            client.delete_agent_profile.side_effect = native_delete
            native_owned["deletionProtection"] = True
            protected = await original.control(request, "owned-cleanup-protected", f'"{latest.revision}"', verified)
            refused = await original.finish_control(protected.operation_id, verified, provider)
            assert refused.state == "failed" and refused.gaps == ["native_deletion_protected"]
            assert client.delete_agent_profile.call_count == 0
            native_owned["deletionProtection"] = False
            latest = await original.binding(owned_binding.binding_id)
            denied = await original.control(request, "owned-cleanup-denied", f'"{latest.revision}"', verified)
            blocked = await original.finish_control(denied.operation_id, verified, provider)
            assert blocked.state == "failed" and blocked.gaps == ["native_cleanup_denied"]
            denied_debt = await session.scalar(select(SetupWork).where(SetupWork.operation_id == denied.operation_id))
            assert denied_debt.blocked_at is not None and denied_debt.last_error == "native_cleanup_denied"
            assert profiles[profile_arn] == adopted_before and owned_arn in profiles
            assert (await original.binding(latest.binding_id)).state == "cleanup_failed"
            assert client.delete_agent_profile.call_count == 1
            deny_delete = False
            latest = await original.binding(owned_binding.binding_id)
            cleanup = await original.control(request, "owned-cleanup-first", f'"{latest.revision}"', verified)
            with pytest.raises(HTTPException) as fenced:
                await original.start({"action": "create", "configuration_revision": 1},
                                     "create-during-cleanup", verified)
            assert fenced.value.detail == {"code": "binding_cleanup_in_progress"}
            failed = await original.finish_control(cleanup.operation_id, verified, provider)
            assert failed.state == "failed" and failed.gaps == ["native_absence_unverified"]
            debt = await session.scalar(select(SetupWork).where(SetupWork.operation_id == cleanup.operation_id))
            assert debt.blocked_at is not None and debt.last_error == "native_absence_unverified"
            assert (await original.binding(latest.binding_id)).state == "cleanup_failed"
            assert client.delete_agent_profile.call_count == 2
            assert profiles[profile_arn] == adopted_before and owned_arn not in profiles
            assert native_get(profileArn=profile_arn) == adopted_before
            assert any(item["arn"] == profile_arn for item in native_list()["items"])
            assert (await original.binding(binding.binding_id)).state == "unlinked"
            assert client.create_agent_profile.call_count == 1
            retry_version = await original.binding(latest.binding_id)
            retry = await original.control(request, "owned-cleanup-retry", f'"{retry_version.revision}"', verified)
            finished = await original.finish_control(retry.operation_id, verified, provider)
            assert finished.state == "succeeded"
            assert finished.cleanup_receipt["absent"] is True
            assert finished.cleanup_receipt["operation_id"] == retry.operation_id
            assert finished.cleanup_receipt["native_resource_ref"]["record_id"] == owned_arn
            assert (await original.binding(latest.binding_id)).state == "cleaned"
            assert client.delete_agent_profile.call_count == 2
            assert [call.kwargs["profileArn"] for call in client.delete_agent_profile.call_args_list] == [owned_arn, owned_arn]
            assert native_get(profileArn=profile_arn) == adopted_before
            assert (await original.binding(binding.binding_id)).ownership == "customer_linked"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_native_claims_reject_other_projects_tenants_and_foreign_sources(tmp_path):
    from unittest.mock import Mock

    from sqlalchemy import func, select

    from l5_services.adapters.well_architected.binding import ProfileBindingProvider, ProfileSelection
    from l5_services.api.bindings import BindingStore
    from l5_services.models.binding import Binding
    from l5_services.models.configuration import Configuration
    from l5_services.models.profile_claim import ProfileClaim

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wa-claims.sqlite'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    account = "000000000000"
    region = "us-east-1"
    profile_arn = f"arn:aws:wellarchitected:{region}:{account}:agent-profile/shared-example"
    broker_role = f"arn:aws:iam::{account}:role/broker-example"
    execution_role = f"arn:aws:iam::{account}:role/source-only-example"
    qualification = {"account_id": account, "region": region, "broker_role_arn": broker_role,
                     "execution_role_arn": execution_role, "trust": "qualified",
                     "source_permissions": "qualified", "support": "eligible"}
    native = {"arn": profile_arn, "pillars": ["SECURITY"], "executionRoleArn": execution_role,
              "aggregationConfiguration": [], "eligibleForArchitectureGeneration": True, "fieldErrors": []}
    client = Mock()
    client.get_agent_profile.return_value = native
    client.list_agent_contexts.return_value = {"items": []}

    async def prepare(tenant_id, project_id):
        connection_id = f"connection-{tenant_id}-{project_id}"
        configuration_id = f"configuration-{tenant_id}-{project_id}"
        async with sessions() as session:
            session.add(Configuration(tenant_id=tenant_id, configuration_id=configuration_id,
                product_id="product-example", project_id=project_id, service_id="well_architected",
                relationship_revision="1", owner_id="owner-example", current_revision=1,
                handoff_state="linked"))
            await session.commit()
            connection_ref = {"tenant_id": tenant_id, "project_id": project_id,
                "source_connection_id": connection_id, "record_type": "connection",
                "record_id": connection_id, "record_revision": "1"}
            verified = {"configuration_id": configuration_id, "configuration_revision": 1,
                "scope": {"tenant_id": tenant_id, "product_id": "product-example", "project_id": project_id,
                          "relationship_revision": "1", "owner_id": "owner-example", "visibility": "only_me"},
                "connection_ref": connection_ref, "account_id": account, "region": region,
                "role_arn": broker_role, "source_role_qualification": qualification,
                "authorization": {"actor_id": "owner-example", "delegation_id": "delegation-example",
                                  "delegation_revision": 1}}
            selected = ProfileSelection(tenant_id, "product-example", project_id, account, region,
                                        broker_role, execution_role, ("SECURITY",), qualification)
            provider = ProfileBindingProvider(client, selected, connection_ref)
            native_ref = {"tenant_id": tenant_id, "project_id": project_id,
                          "source_connection_id": connection_id, "record_type": "agent_profile",
                          "record_id": profile_arn, "record_revision": provider.adapter.adopt(selected, profile_arn).snapshot_hash}
            store = BindingStore(session, tenant_id, "product-example", project_id, "well_architected")
            operation = await store.start({"action": "link", "configuration_revision": 1,
                                           "native_resource_ref": native_ref}, f"link-{tenant_id}-{project_id}", verified)
        return operation.operation_id, verified, provider, native_ref

    try:
        winner = await prepare("tenant-example", "project-example")
        other_project = await prepare("tenant-example", "project-other")
        other_tenant = await prepare("tenant-other", "project-example")
        async with sessions() as session:
            store = BindingStore(session, "tenant-other", "product-example", "project-example", "well_architected")
            _, verified, _, native_ref = other_tenant
            for foreign_ref in (winner[3], {**native_ref, "source_connection_id": "connection-foreign"},
                                {**native_ref, "record_id": profile_arn.replace(account, "111111111111")}):
                with pytest.raises(HTTPException) as denied:
                    await store.start({"action": "link", "configuration_revision": 1,
                                       "native_resource_ref": foreign_ref}, "foreign-source-example", verified)
                assert denied.value.status_code == 404

        arrived = set()
        all_arrived = asyncio.Event()
        release = {name: asyncio.Event() for name in ("winner", "other_project", "other_tenant")}

        async def finish(name, candidate):
            operation_id, verified, provider, _ = candidate
            read = provider.read

            async def gated_read(reference, authority):
                arrived.add(name)
                if len(arrived) == len(release):
                    all_arrived.set()
                await release[name].wait()
                return await read(reference, authority)

            provider.read = gated_read
            async with sessions() as session:
                store = BindingStore(session, verified["scope"]["tenant_id"], "product-example",
                                     verified["scope"]["project_id"], "well_architected")
                return await store.finish_link(operation_id, verified, provider)

        attempts = {"winner": asyncio.create_task(finish("winner", winner)),
                    "other_project": asyncio.create_task(finish("other_project", other_project)),
                    "other_tenant": asyncio.create_task(finish("other_tenant", other_tenant))}
        await asyncio.wait_for(all_arrived.wait(), 10)
        release["winner"].set()
        assert (await asyncio.wait_for(attempts["winner"], 10)).state == "succeeded"
        release["other_project"].set()
        release["other_tenant"].set()
        for name in ("other_project", "other_tenant"):
            with pytest.raises(HTTPException) as denied:
                await asyncio.wait_for(attempts[name], 10)
            assert denied.value.status_code == 404

        async with sessions() as session:
            store = BindingStore(session, "tenant-other", "product-example", "project-example", "well_architected")
            _, verified, provider, native_ref = other_tenant
            wrong_broker = {**verified, "role_arn": f"arn:aws:iam::{account}:role/wrong-example"}
            wrong_role = await store.start({"action": "link", "configuration_revision": 1,
                                            "native_resource_ref": native_ref}, "wrong-role-example", wrong_broker)
            assert (await store.finish_link(wrong_role.operation_id, wrong_broker, provider)).gaps == ["native_scope_mismatch"]
            other_arn = profile_arn.replace("shared-example", "unclaimed-example")
            client.get_agent_profile.return_value = native | {"arn": other_arn,
                "executionRoleArn": f"arn:aws:iam::{account}:role/wrong-example"}
            unclaimed_ref = {**native_ref, "record_id": other_arn}
            unqualified = await store.start({"action": "link", "configuration_revision": 1,
                                              "native_resource_ref": unclaimed_ref}, "wrong-execution-example", verified)
            assert "execution_role_mismatch" in (await store.finish_link(unqualified.operation_id, verified, provider)).gaps
            assert await session.scalar(select(func.count()).select_from(ProfileClaim)) == 1
            assert await session.scalar(select(func.count()).select_from(Binding)) == 1
        assert client.create_agent_profile.call_count == 0
        assert client.update_agent_profile.call_count == 0
        assert client.delete_agent_profile.call_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_owned_create_recovers_after_lost_response_and_worker_restart(tmp_path):
    from datetime import UTC, timedelta
    from unittest.mock import Mock

    from sqlalchemy import func, select

    from l5_services.adapters.well_architected.binding import ProfileBindingProvider, ProfileSelection
    from l5_services.api.bindings import BindingStore
    from l5_services.models.binding import Binding
    from l5_services.models.configuration import Configuration
    from l5_services.models.profile_claim import ProfileClaim
    from l5_services.models.setup import BindingOperation, SetupWork
    from l5_services.workers.bindings import execute_setup_work

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wa-recovery.sqlite'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    account = "000000000000"
    region = "us-east-1"
    profile_arn = f"arn:aws:wellarchitected:{region}:{account}:agent-profile/owned-example"
    broker_role = f"arn:aws:iam::{account}:role/broker-example"
    execution_role = f"arn:aws:iam::{account}:role/source-only-example"
    qualification = {"account_id": account, "region": region, "broker_role_arn": broker_role,
                     "execution_role_arn": execution_role, "trust": "qualified",
                     "source_permissions": "qualified", "support": "eligible"}
    selected = ProfileSelection("tenant-example", "product-example", "project-example", account, region,
                                broker_role, execution_role, ("SECURITY",), qualification)
    fingerprint = selected.fingerprint()
    native = {"arn": profile_arn, "name": "pending-native-create",
              "tags": [{"key": "adp:scope", "value": fingerprint}], "pillars": ["SECURITY"],
              "executionRoleArn": execution_role, "aggregationConfiguration": [],
              "eligibleForArchitectureGeneration": True, "fieldErrors": []}
    client = Mock()
    created = []
    listings = []

    def create(**request):
        created.append(request)
        raise TimeoutError("native created, response lost")

    def get(profileArn):
        assert created
        return native | {"arn": profileArn}

    def list_profiles(**request):
        if request.get("nextToken") == "second":
            return {"items": [{"name": native["name"], "arn": profile_arn}]}
        assert request == {}
        listings.append(len(created))
        if len(listings) == 1:
            return {"items": []}
        if len(listings) == 2:
            return {"items": [{"name": native["name"], "arn": profile_arn},
                              {"name": native["name"], "arn": profile_arn.replace("owned", "duplicate")}]}
        return {"items": [
            {"name": provider.adapter._created_name(fingerprint, "prior-operation"), "arn": profile_arn},
            {"name": "unrelated", "arn": profile_arn}], "nextToken": "second"}

    client.create_agent_profile.side_effect = create
    client.get_agent_profile.side_effect = get
    client.list_agent_profiles.side_effect = list_profiles
    client.list_agent_contexts.return_value = {"items": []}
    connection_ref = {"tenant_id": "tenant-example", "project_id": "project-example",
        "source_connection_id": "connection-example", "record_type": "connection",
        "record_id": "connection-example", "record_revision": "1"}
    verified = {"configuration_id": "configuration-example", "configuration_revision": 1,
        "scope": {"tenant_id": "tenant-example", "product_id": "product-example",
                  "project_id": "project-example", "relationship_revision": "1",
                  "owner_id": "owner-example", "visibility": "only_me"},
        "connection_ref": connection_ref, "account_id": account, "region": region,
        "role_arn": broker_role, "source_role_qualification": qualification,
        "authorization": {"actor_id": "owner-example", "delegation_id": "delegation-example",
                          "delegation_revision": 1}}
    provider = ProfileBindingProvider(client, selected, connection_ref)

    async def authorize(intent, claim):
        assert intent.configuration_id == verified["configuration_id"]
        assert claim.operation_id == intent.operation_id
        return verified, provider

    try:
        async with sessions() as session:
            session.add(Configuration(tenant_id="tenant-example", configuration_id="configuration-example",
                product_id="product-example", project_id="project-example", service_id="well_architected",
                relationship_revision="1", owner_id="owner-example", current_revision=1, handoff_state="linked"))
            await session.commit()
            store = BindingStore(session, "tenant-example", "product-example", "project-example", "well_architected")
            operation = await store.start({"action": "create", "configuration_revision": 1},
                                          "owned-create-example", verified)
            operation_id = operation.operation_id
            native["name"] = provider.adapter._created_name(fingerprint, operation_id)
            work = await session.scalar(select(SetupWork).where(SetupWork.operation_id == operation_id))
            work_id = work.work_id
            start = work.due_at.replace(tzinfo=UTC)
        envelope = {"schema_version": "1.0", "work_id": work_id, "generation": 1}
        for attempt in range(4):
            assert await execute_setup_work(sessions, envelope, authorize,
                                            now=start + timedelta(minutes=6 * attempt))
            async with sessions() as session:
                result = await session.get(BindingOperation, ("tenant-example", operation_id))
                assert result.state == ("succeeded" if attempt == 3 else "outcome_unknown")
                assert result.attempts == 1
                assert result.reconcile_checks == attempt
                assert len(created) == 1
                if attempt < 3:
                    assert await session.scalar(select(func.count()).select_from(Binding)) == 0
                    assert await session.scalar(select(func.count()).select_from(ProfileClaim)) == 0
        async with sessions() as session:
            result = await session.get(BindingOperation, ("tenant-example", operation_id))
            binding = await session.get(Binding, ("tenant-example", result.binding_id))
            assert binding.state == "ready" and binding.ownership == "adp_created"
            assert binding.native_resource_ref["record_id"] == profile_arn
            assert binding.creation_receipt["operation_id"] == operation_id
            assert await session.scalar(select(func.count()).select_from(ProfileClaim)) == 1
        assert created[0]["clientToken"] == hashlib.sha256(f"{fingerprint}:{operation_id}".encode()).hexdigest()
        assert created[0]["aggregationConfiguration"] == []
        assert created[0]["tags"] == native["tags"]
        assert client.list_agent_profiles.call_count == 4
        assert provider.adapter.reconcile(selected, operation_id, profile_arn.replace("owned", "other")) is None
        client.get_agent_profile.side_effect = lambda profileArn: native | {
            "arn": profileArn, "tags": [{"key": "adp:scope", "value": "foreign-scope"}],
        }
        assert provider.adapter.reconcile(selected, operation_id, None) is None
        assert len(created) == 1
        assert client.delete_agent_profile.call_count == 0
        assert not await execute_setup_work(sessions, envelope, authorize, now=start + timedelta(minutes=24))
        assert len(created) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ac_01(monkeypatch):
    import boto3
    from copy import deepcopy
    import httpx
    from fastapi import FastAPI
    from sqlalchemy import func, select

    from l5_services.api.configuration import view
    from l5_services.api.router import create_router
    from l5_services.models.configuration import ConfigurationVersion
    from l5_services.profile_handoff import link_configuration

    provider_calls = []
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: provider_calls.append((args, kwargs)))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    document = reference("document", "architecture-example")
    repository = reference("repository_commit", "repository-example")
    connection_ref = reference("connection", "connection-example")
    profile = {"tenant_id": "tenant-example", "project_id": "project-example",
               "product_id": "product-example", "revision": 1,
               "configuration": {"basic": {"documents": [document]},
                                 "selected_capabilities": [{"capability": "architecture_review"}]}}
    available = {"coverage": "complete", "next_cursor": None, "items": [
        {"connection_ref": {"id": "connection-example"}, "record_ref": connection_ref,
         "resources": [document, repository]}]}
    choices = {"service_id": "well_architected", "capabilities": ["architecture_review"],
               "source_refs": [document], "connection_ref": connection_ref,
               "overrides": [{"field": "source_refs", "value": [document], "reason": "Review selected design"},
                             {"field": "connection_ref", "value": connection_ref, "reason": "Selected account"}],
               "well_architected": {"review_mode": "source_only", "pillars": ["SECURITY"]},
               "triggers": [{"kind": "manual", "enabled": False}], "enabled": False}
    patch_calls = []
    profiles_by_revision = {1: deepcopy(profile)}

    try:
        async with sessions() as session:
            store = ConfigurationStore(session, "tenant-example", "user-example")

            class Backend:
                async def _handoff(self, configuration, version, key):
                    configuration_ref = reference("l5_configuration", configuration.configuration_id,
                                                  str(version.revision))

                    async def read_profile():
                        return profile

                    async def patch_profile(patch, revision, operation_key):
                        patch_calls.append((revision, operation_key))
                        if len(patch_calls) == 1:
                            raise HTTPException(412, detail={"code": "stale_profile_revision"})
                        assert revision == profile["revision"]
                        profile["configuration"] = patch["configuration"]
                        profile["revision"] += 1

                    configuration.handoff_state = await link_configuration(
                        read_profile, patch_profile, configuration_ref,
                        ["architecture_review"], key)
                    await session.commit()
                    return view(configuration, version)

                async def save(self, product_id, project_id, service_id, settings, key, if_none_match, if_match):
                    if if_none_match == "*":
                        configuration, version = await store.create(
                            product_id, project_id, service_id, 1, profile,
                            reference("product", product_id, str(profile["revision"])), available,
                            settings, idempotency_key=key)
                    else:
                        configuration, previous = await store.current(product_id, project_id, service_id)
                        pinned_revision = int(previous.settings["profile_ref"]["record_revision"])
                        configuration, version = await store.edit(
                            configuration, previous, profiles_by_revision[pinned_revision],
                            available, settings, if_match)
                    return await self._handoff(configuration, version, key)

                async def get(self, product_id, project_id, service_id):
                    return view(*await store.current(product_id, project_id, service_id))

                async def version(self, product_id, project_id, service_id, revision):
                    configuration, _ = await store.current(product_id, project_id, service_id)
                    historical = await session.get(ConfigurationVersion, (
                        configuration.tenant_id, configuration.configuration_id, revision))
                    if historical is None:
                        raise HTTPException(404, detail={"code": "not_found"})
                    return view(configuration, historical)

                async def refresh(self, product_id, project_id, service_id, incoming, key, if_match):
                    configuration, previous = await store.current(product_id, project_id, service_id)
                    if incoming != reference("product", product_id, str(profile["revision"])):
                        raise HTTPException(409, detail={"code": "source_scope_conflict"})
                    configuration, version = await store.refresh(
                        configuration, previous, profile, incoming, available, if_match)
                    return await self._handoff(configuration, version, key)

                async def retry_handoff(self, product_id, project_id, service_id, key, if_match):
                    configuration, version = await store.current(product_id, project_id, service_id)
                    if if_match != f'"{version.hash}"':
                        raise HTTPException(412, detail={"code": "stale_revision"})
                    return await self._handoff(configuration, version, key)

                async def check(self, product_id, project_id, service_id):
                    configuration, version = await store.current(product_id, project_id, service_id)
                    return {"ready": version.state == "ready" and configuration.handoff_state == "linked",
                            "gaps": version.gaps, "provider_contacted": False}

            app = FastAPI()
            app.include_router(create_router(lambda: Backend()))
            path = "/v1/products/product-example/projects/project-example/services/well_architected/configuration"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                created = await client.put(path, json=choices, headers={
                    "Idempotency-Key": "create-wa02-scn01-0001", "If-None-Match": "*"})
                assert created.status_code == 201 and created.json()["handoff_state"] == "conflict"
                assert created.json()["settings"]["overrides"] == choices["overrides"]
                assert created.json()["settings"]["well_architected"]["pillars"] == ["SECURITY"]
                first_etag = created.headers["etag"]
                assert first_etag == f'"{created.json()["hash"]}"'
                first_check = await client.post(f"{path}/check")
                assert first_check.json() == {"ready": False,
                                              "gaps": ["catalog_mapping_unresolved", "provider_unqualified"],
                                              "provider_contacted": False}
                retried = await client.post(f"{path}/handoff", headers={
                    "Idempotency-Key": "handoff-wa02-scn01-01", "If-Match": first_etag})
                assert retried.status_code == 200 and retried.json()["handoff_state"] == "linked"
                assert retried.headers["etag"] == first_etag
                assert await session.scalar(select(func.count()).select_from(ConfigurationVersion)) == 1
                profile["revision"] += 1
                profile["configuration"]["basic"]["repositories"] = [repository]
                next_ref = reference("product", "product-example", str(profile["revision"]))
                profiles_by_revision[profile["revision"]] = deepcopy(profile)
                stale = await client.post(f"{path}/refresh", json={"profile_ref": next_ref}, headers={
                    "Idempotency-Key": "refresh-wa02-scn01-01", "If-Match": '"stale-etag"'})
                assert stale.status_code == 412 and stale.json()["detail"]["code"] == "stale_revision"
                refreshed = await client.post(f"{path}/refresh", json={"profile_ref": next_ref}, headers={
                    "Idempotency-Key": "refresh-wa02-scn01-02", "If-Match": first_etag})
                assert refreshed.status_code == 200, refreshed.text
                current = refreshed.json()
                assert current["revision"] == 2 and current["settings"]["profile_ref"] == next_ref
                assert current["settings"]["source_refs"] == [document]
                assert current["settings"]["overrides"] == choices["overrides"]
                assert current["settings"]["well_architected"] == choices["well_architected"]
                assert current["settings"]["triggers"] == choices["triggers"]
                assert (await client.get(f"{path}/versions/1")).json()["settings"]["profile_ref"] == (
                    reference("product", "product-example"))
                assert (await client.get(path)).headers["etag"] == refreshed.headers["etag"]
                enabled = await client.put(path, json={"enabled": True}, headers={
                    "Idempotency-Key": "edit-wa02-scn01-0001", "If-Match": refreshed.headers["etag"]})
                assert enabled.status_code == 200, enabled.text
                assert enabled.json()["settings"]["triggers"] == choices["triggers"]
                assert enabled.json()["settings"]["source_refs"] == [document]
                assert (await client.post(f"{path}/check")).json() == {
                    "ready": False, "provider_contacted": False,
                    "gaps": ["catalog_mapping_unresolved", "provider_unqualified", "region", "classification", "limits",
                             "well_architected.execution_role_ref", "well_architected.source_store_ref",
                             "execution_delegation"]}
                assert await session.scalar(select(func.count()).select_from(ConfigurationVersion)) == 3
                assert len(patch_calls) == 4 and len({key for _, key in patch_calls}) == 4
                assert provider_calls == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ac_02(tmp_path, monkeypatch):
    from unittest.mock import Mock

    import httpx
    from fastapi import FastAPI
    from sqlalchemy import func, select

    from l5_services.adapters.well_architected.binding import ProfileBindingProvider, ProfileSelection
    from l5_services.api.bindings import BindingStore
    from l5_services.api.router import create_router
    from l5_services.models.binding import Binding
    from l5_services.models.configuration import Configuration
    from l5_services.models.profile_claim import ProfileClaim
    from l5_services.models.setup import SetupWork
    from l5_services.workers.bindings import execute_setup_work

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wa02-scn02.sqlite'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    account, region = "000000000000", "us-east-1"
    broker_role = f"arn:aws:iam::{account}:role/broker-example"
    execution_role = f"arn:aws:iam::{account}:role/source-only-example"
    adopted_arn = f"arn:aws:wellarchitected:{region}:{account}:agent-profile/adopted-example"
    owned_arn = f"arn:aws:wellarchitected:{region}:{account}:agent-profile/owned-example"
    wrong_role_arn = f"arn:aws:wellarchitected:{region}:{account}:agent-profile/wrong-role-example"
    profiles = {
        adopted_arn: {"arn": adopted_arn, "name": "customer-owned", "pillars": ["SECURITY"],
                      "executionRoleArn": execution_role, "aggregationConfiguration": [],
                      "eligibleForArchitectureGeneration": True, "fieldErrors": []},
        wrong_role_arn: {"arn": wrong_role_arn, "name": "other-role", "pillars": ["SECURITY"],
                         "executionRoleArn": broker_role, "aggregationConfiguration": [],
                         "eligibleForArchitectureGeneration": True, "fieldErrors": []},
    }
    client = Mock()
    client.get_agent_profile.side_effect = lambda *, profileArn: profiles[profileArn]
    client.list_agent_contexts.return_value = {"items": []}

    def native_create(**kwargs):
        assert kwargs["aggregationConfiguration"] == []
        assert kwargs["executionRoleArn"] == execution_role
        assert kwargs["pillars"] == ["SECURITY"]
        assert kwargs["clientToken"] and kwargs["tags"]
        profiles[owned_arn] = {"arn": owned_arn, "name": kwargs["name"], "tags": kwargs["tags"],
                               "pillars": kwargs["pillars"], "executionRoleArn": execution_role,
                               "aggregationConfiguration": [], "eligibleForArchitectureGeneration": True,
                               "fieldErrors": []}
        return {"arn": owned_arn}

    client.create_agent_profile.side_effect = native_create
    qualification = {"account_id": account, "region": region, "broker_role_arn": broker_role,
                     "execution_role_arn": execution_role, "trust": "qualified",
                     "source_permissions": "qualified", "support": "eligible"}
    base_path = "/v1/products/product-example/projects/{project_id}/services/well_architected"
    race_keys = {name: f"race-{name}-wa02-scn02-01" for name in ("winner", "project", "tenant")}
    race_release = {key: asyncio.Event() for key in race_keys.values()}
    race_arrivals = set()
    race_ready = asyncio.Event()

    async def scoped_client(tenant_id, project_id):
        configuration_id = f"configuration-{tenant_id}-{project_id}"
        connection_id = f"connection-{tenant_id}-{project_id}"
        session = sessions()
        session.add(Configuration(tenant_id=tenant_id, configuration_id=configuration_id,
                                  product_id="product-example", project_id=project_id,
                                  service_id="well_architected", relationship_revision="1",
                                  owner_id="owner-example", current_revision=1, handoff_state="linked"))
        await session.commit()
        connection_ref = {"tenant_id": tenant_id, "project_id": project_id,
                          "source_connection_id": connection_id, "record_type": "connection",
                          "record_id": connection_id, "record_revision": "1"}
        verified = {"configuration_id": configuration_id, "configuration_revision": 1,
                    "scope": {"tenant_id": tenant_id, "product_id": "product-example", "project_id": project_id,
                              "relationship_revision": "1", "owner_id": "owner-example", "visibility": "only_me"},
                    "connection_ref": connection_ref, "account_id": account, "region": region,
                    "role_arn": broker_role, "source_role_qualification": qualification,
                    "authorization": {"actor_id": "owner-example", "delegation_id": "delegation-example",
                                      "delegation_revision": 1}}
        selection = ProfileSelection(tenant_id, "product-example", project_id, account, region,
                                     broker_role, execution_role, ("SECURITY",), qualification)
        provider = ProfileBindingProvider(client, selection, connection_ref)
        store = BindingStore(session, tenant_id, "product-example", project_id, "well_architected")

        def native_ref(profile_arn):
            readback = provider.adapter.adopt(selection, profile_arn)
            return {"tenant_id": tenant_id, "project_id": project_id,
                    "source_connection_id": connection_id, "record_type": "agent_profile",
                    "record_id": profile_arn, "record_revision": readback.snapshot_hash}

        class Backend:
            async def start_binding(self, product_id, requested_project, service_id, body, key, etag):
                if (product_id, requested_project, service_id) != (
                        "product-example", project_id, "well_architected"):
                    raise HTTPException(404, detail={"code": "not_found"})
                if body.get("action") in {"check", "unlink"}:
                    operation = await store.control(body, key, etag, verified)
                else:
                    operation = await store.start(body, key, verified)
                operation_id = operation.operation_id
                work = await session.scalar(select(SetupWork).where(SetupWork.operation_id == operation_id))
                envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}
                await session.rollback()
                if key in race_release:
                    race_arrivals.add(key)
                    if len(race_arrivals) == len(race_release):
                        race_ready.set()
                    await race_release[key].wait()

                async def authorize(intent, claim):
                    assert intent.tenant_id == tenant_id and intent.configuration_id == configuration_id
                    assert claim.operation_id == intent.operation_id
                    return verified, provider

                await execute_setup_work(sessions, envelope, authorize)
                async with sessions() as observed:
                    return await BindingStore(observed, tenant_id, "product-example", project_id,
                                              "well_architected").operation(operation_id)

            async def get_binding_operation(self, product_id, requested_project, service_id, operation_id):
                if requested_project != project_id:
                    raise HTTPException(404, detail={"code": "not_found"})
                return await store.operation(operation_id)

            async def get_binding(self, product_id, requested_project, service_id, binding_id):
                if requested_project != project_id:
                    raise HTTPException(404, detail={"code": "not_found"})
                return await store.binding(binding_id)

            async def list_bindings(self, product_id, requested_project, service_id):
                if requested_project != project_id:
                    raise HTTPException(404, detail={"code": "not_found"})
                return await store.list_bindings()

        app = FastAPI()
        app.include_router(create_router(lambda: Backend()))
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        return http, session, native_ref, base_path.format(project_id=project_id)

    resources = []
    try:
        winner = await scoped_client("tenant-example", "project-example")
        competitor = await scoped_client("tenant-example", "project-other")
        foreign_tenant = await scoped_client("tenant-other", "project-example")
        resources.extend((winner, competitor, foreign_tenant))
        http, session, native_ref, path = winner

        async def start(target, body, key, if_match=None):
            transport, _, _, scoped_path = target
            headers = {"Idempotency-Key": key}
            if if_match is not None:
                headers["If-Match"] = if_match
            return await transport.post(scoped_path + "/binding-operations", json=body, headers=headers)

        adopted_ref = native_ref(adopted_arn)
        candidates = {"winner": (winner, adopted_ref),
                      "project": (competitor, competitor[2](adopted_arn)),
                      "tenant": (foreign_tenant, foreign_tenant[2](adopted_arn))}
        races = {name: asyncio.create_task(start(target, {
            "action": "link", "configuration_revision": 1, "native_resource_ref": candidate_ref,
        }, race_keys[name])) for name, (target, candidate_ref) in candidates.items()}
        await asyncio.wait_for(race_ready.wait(), 10)
        race_release[race_keys["winner"]].set()
        link = await asyncio.wait_for(races["winner"], 10)
        assert link.status_code == 202 and link.json()["state"] == "succeeded", link.text
        race_release[race_keys["project"]].set()
        race_release[race_keys["tenant"]].set()
        for name in ("project", "tenant"):
            denied = await asyncio.wait_for(races[name], 10)
            assert denied.status_code == 202 and denied.json()["state"] == "blocked"
            assert denied.json()["gaps"] == ["native_profile_claim_denied"]
            assert client.create_agent_profile.call_count == 0
        adopted_id = link.json()["binding_id"]
        adopted = await http.get(f"{path}/bindings/{adopted_id}")
        assert adopted.json()["state"] == "ready" and adopted.json()["ownership"] == "customer_linked"
        assert adopted.json()["native_resource_ref"] == adopted_ref
        assert adopted.json()["gaps"] == [] and adopted.json()["readiness"][0]["snapshot_hash"] == adopted_ref["record_revision"]
        assert (await http.get(f"{path}/binding-operations/{link.json()['operation_id']}")).json()["state"] == "succeeded"
        assert client.create_agent_profile.call_count == 0
        wrong_source = await start(competitor, {"action": "link", "configuration_revision": 1,
                                                "native_resource_ref": adopted_ref}, "foreign-wa02-scn02-0001")
        assert wrong_source.status_code == 404
        wrong_role = await start(competitor, {"action": "link", "configuration_revision": 1,
                                              "native_resource_ref": competitor[2](wrong_role_arn)},
                                 "wrong-role-wa02-scn02-01")
        assert wrong_role.status_code == 202 and wrong_role.json()["state"] == "blocked"
        assert "execution_role_mismatch" in wrong_role.json()["gaps"]
        assert client.create_agent_profile.call_count == 0
        original_read = ProfileBindingProvider.read

        async def mismatched_read(provider, reference, verified):
            native = await original_read(provider, reference, verified)
            return {**native, "provider_identity": {**native["provider_identity"], "region": "us-west-2"}}

        with monkeypatch.context() as patch:
            patch.setattr(ProfileBindingProvider, "read", mismatched_read)
            wrong_scope = await start(competitor, {"action": "link", "configuration_revision": 1,
                                                  "native_resource_ref": competitor[2](wrong_role_arn)},
                                      "wrong-scope-wa02-scn02-01")
        assert wrong_scope.status_code == 202
        assert wrong_scope.json()["gaps"] == ["native_scope_mismatch"]
        async with sessions() as readback:
            wrong_scope_work = await readback.scalar(select(SetupWork).where(
                SetupWork.operation_id == wrong_scope.json()["operation_id"]))
            assert wrong_scope_work.blocked_at is not None
            assert wrong_scope_work.last_error == "native_scope_mismatch"
        checked = await start(winner, {"action": "check", "configuration_revision": 1,
                                       "binding_id": adopted_id}, "check-wa02-scn02-0001", adopted.headers["etag"])
        assert checked.status_code == 202 and checked.json()["state"] == "succeeded"
        current = await http.get(f"{path}/bindings/{adopted_id}")
        unlink = await start(winner, {"action": "unlink", "configuration_revision": 1,
                                      "binding_id": adopted_id}, "unlink-wa02-scn02-01", current.headers["etag"])
        assert unlink.status_code == 202 and unlink.json()["state"] == "succeeded"
        assert (await http.get(f"{path}/bindings/{adopted_id}")).json()["state"] == "unlinked"
        after_unlink = await start(competitor, {"action": "link", "configuration_revision": 1,
                                                "native_resource_ref": competitor[2](adopted_arn)},
                                   "still-owned-wa02-scn02")
        assert after_unlink.status_code == 202 and after_unlink.json()["state"] == "blocked"
        assert after_unlink.json()["gaps"] == ["native_profile_claim_denied"]
        created = await start(winner, {"action": "create", "configuration_revision": 1},
                              "create-wa02-scn02-0001")
        assert created.status_code == 202 and created.json()["state"] == "succeeded", created.text
        owned = await http.get(f"{path}/bindings/{created.json()['binding_id']}")
        assert owned.json()["state"] == "ready" and owned.json()["ownership"] == "adp_created"
        assert owned.json()["native_resource_ref"]["record_id"] == owned_arn
        assert client.create_agent_profile.call_count == 1
        create_input = client.create_agent_profile.call_args.kwargs
        assert create_input["aggregationConfiguration"] == [] and create_input["executionRoleArn"] == execution_role
        assert create_input["tags"][0]["key"] == "adp:scope"
        assert await session.scalar(select(func.count()).select_from(ProfileClaim)) == 2
        assert await session.scalar(select(func.count()).select_from(Binding)) == 2
        async with sessions() as readback:
            denied_work = (await readback.scalars(select(SetupWork).where(
                SetupWork.last_error == "native_profile_claim_denied"))).all()
            assert len(denied_work) == 3 and all(work.blocked_at is not None for work in denied_work)
            wrong_role_work = await readback.scalar(select(SetupWork).where(
                SetupWork.operation_id == wrong_role.json()["operation_id"]))
            assert wrong_role_work.blocked_at is not None
            assert wrong_role_work.last_error == "execution_role_mismatch"

            async def unexpected_authority(intent, claim):
                pytest.fail("blocked setup work was replayed")

            before_replay = client.get_agent_profile.call_count
            for denied_item in (*denied_work, wrong_role_work, wrong_scope_work):
                assert not await execute_setup_work(sessions, {"schema_version": "1.0",
                    "work_id": denied_item.work_id, "generation": denied_item.generation}, unexpected_authority)
            assert client.get_agent_profile.call_count == before_replay
        assert client.update_agent_profile.call_count == client.delete_agent_profile.call_count == 0
        assert profiles[adopted_arn]["executionRoleArn"] == execution_role
        assert profiles[adopted_arn]["aggregationConfiguration"] == []
    finally:
        for transport, session, _, _ in resources:
            await transport.aclose()
            await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_ac_03(tmp_path):
    from copy import deepcopy
    from datetime import UTC, timedelta
    from unittest.mock import Mock

    import httpx
    from botocore.exceptions import ClientError
    from fastapi import FastAPI
    from sqlalchemy import func, select

    from l5_services.adapters.well_architected.binding import ProfileBindingProvider, ProfileSelection
    from l5_services.api.bindings import BindingStore
    from l5_services.api.router import create_router
    from l5_services.models.binding import Binding
    from l5_services.models.configuration import Configuration
    from l5_services.models.profile_claim import ProfileClaim
    from l5_services.models.setup import SetupWork
    from l5_services.workers.bindings import execute_setup_work

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wa02-scn03.sqlite'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    account, region = "000000000000", "us-east-1"
    broker_role = f"arn:aws:iam::{account}:role/broker-example"
    execution_role = f"arn:aws:iam::{account}:role/source-only-example"
    owned_arn = f"arn:aws:wellarchitected:{region}:{account}:agent-profile/owned-example"
    adopted_arn = f"arn:aws:wellarchitected:{region}:{account}:agent-profile/adopted-example"
    qualification = {"account_id": account, "region": region, "broker_role_arn": broker_role,
                     "execution_role_arn": execution_role, "trust": "qualified",
                     "source_permissions": "qualified", "support": "eligible"}
    selected = ProfileSelection("tenant-example", "product-example", "project-example", account, region,
                                broker_role, execution_role, ("SECURITY",), qualification)
    adopted_native = {"arn": adopted_arn, "name": "customer-owned", "pillars": ["SECURITY"],
                      "executionRoleArn": execution_role, "aggregationConfiguration": [],
                      "eligibleForArchitectureGeneration": True, "fieldErrors": []}
    profiles = {adopted_arn: deepcopy(adopted_native)}
    client = Mock()
    creates = []
    listing_round = 0
    deny_delete = True
    stale_listing = False

    def native_create(**request):
        assert request["executionRoleArn"] == execution_role and request["aggregationConfiguration"] == []
        creates.append(request)
        profiles[owned_arn] = {"arn": owned_arn, "name": request["name"], "tags": request["tags"],
                               "pillars": request["pillars"], "executionRoleArn": execution_role,
                               "aggregationConfiguration": [], "eligibleForArchitectureGeneration": True,
                               "fieldErrors": [], "deletionProtection": False}
        raise TimeoutError("native created, response lost")

    def native_get(*, profileArn):
        if profileArn not in profiles:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
                              "GetAgentProfile")
        return deepcopy(profiles[profileArn])

    def native_list(**request):
        nonlocal listing_round, stale_listing
        if request.get("nextToken") == "second":
            return {"items": [{"arn": owned_arn, "name": profiles[owned_arn]["name"]}]}
        assert request == {}
        listing_round += 1
        items = [{"arn": adopted_arn, "name": adopted_native["name"]}]
        if owned_arn in profiles:
            if listing_round == 1:
                return {"items": items}
            if listing_round == 2:
                return {"items": items, "nextToken": "second"}
            items.append({"arn": owned_arn, "name": profiles[owned_arn]["name"]})
        elif stale_listing:
            items.append({"arn": owned_arn, "name": creates[0]["name"]})
            stale_listing = False
        return {"items": items}

    def native_delete(*, profileArn):
        nonlocal deny_delete, stale_listing
        assert profileArn == owned_arn and adopted_arn in profiles
        if deny_delete:
            deny_delete = False
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
                              "DeleteAgentProfile")
        del profiles[owned_arn]
        stale_listing = True
        return {}

    client.create_agent_profile.side_effect = native_create
    client.get_agent_profile.side_effect = native_get
    client.list_agent_profiles.side_effect = native_list
    client.list_agent_contexts.return_value = {"items": []}
    client.delete_agent_profile.side_effect = native_delete
    connection_ref = {"tenant_id": "tenant-example", "project_id": "project-example",
                      "source_connection_id": "connection-example", "record_type": "connection",
                      "record_id": "connection-example", "record_revision": "1"}
    verified = {"configuration_id": "configuration-example", "configuration_revision": 1,
                "scope": {"tenant_id": "tenant-example", "product_id": "product-example",
                          "project_id": "project-example", "relationship_revision": "1",
                          "owner_id": "owner-example", "visibility": "only_me"},
                "connection_ref": connection_ref, "account_id": account, "region": region,
                "role_arn": broker_role, "source_role_qualification": qualification,
                "authorization": {"actor_id": "owner-example", "delegation_id": "delegation-example",
                                  "delegation_revision": 1}}
    provider = ProfileBindingProvider(client, selected, connection_ref)
    path = "/v1/products/product-example/projects/project-example/services/well_architected"

    async def authorize(intent, claim):
        assert intent.configuration_id == verified["configuration_id"] and intent.scope == verified["scope"]
        assert claim.operation_id == intent.operation_id
        return verified, provider

    class Backend:
        async def start_binding(self, product_id, project_id, service_id, body, key, etag):
            assert (product_id, project_id, service_id) == (
                "product-example", "project-example", "well_architected")
            async with sessions() as session:
                store = BindingStore(session, "tenant-example", product_id, project_id, service_id)
                if body["action"] in {"create", "link"}:
                    return await store.start(body, key, verified)
                return await store.control(body, key, etag, verified)

        async def get_binding_operation(self, product_id, project_id, service_id, operation_id):
            async with sessions() as session:
                return await BindingStore(session, "tenant-example", product_id, project_id,
                                          service_id).operation(operation_id)

        async def get_binding(self, product_id, project_id, service_id, binding_id):
            async with sessions() as session:
                return await BindingStore(session, "tenant-example", product_id, project_id,
                                          service_id).binding(binding_id)

        async def list_bindings(self, product_id, project_id, service_id):
            async with sessions() as session:
                return await BindingStore(session, "tenant-example", product_id, project_id,
                                          service_id).list_bindings()

    app = FastAPI()
    app.include_router(create_router(lambda: Backend()))
    try:
        async with sessions() as session:
            session.add(Configuration(tenant_id="tenant-example", configuration_id="configuration-example",
                                      product_id="product-example", project_id="project-example",
                                      service_id="well_architected", relationship_revision="1",
                                      owner_id="owner-example", current_revision=1, handoff_state="linked"))
            await session.commit()

        async def run_work(operation_id, attempt):
            async with sessions() as session:
                work = await session.scalar(select(SetupWork).where(SetupWork.operation_id == operation_id))
                envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}
            return await execute_setup_work(sessions, envelope, authorize,
                                            now=work.due_at.replace(tzinfo=UTC) + timedelta(minutes=6 * attempt))

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            async def start(body, key, etag=None):
                headers = {"Idempotency-Key": key}
                if etag is not None:
                    headers["If-Match"] = etag
                return await http.post(f"{path}/binding-operations", json=body, headers=headers)

            adopted_ref = {"tenant_id": "tenant-example", "project_id": "project-example",
                           "source_connection_id": "connection-example", "record_type": "agent_profile",
                           "record_id": adopted_arn,
                           "record_revision": provider.adapter.adopt(selected, adopted_arn).snapshot_hash}
            linked = await start({"action": "link", "configuration_revision": 1,
                                  "native_resource_ref": adopted_ref}, "adopt-scn03-0001")
            assert linked.status_code == 202 and await run_work(linked.json()["operation_id"], 0)
            linked_result = await http.get(f"{path}/binding-operations/{linked.json()['operation_id']}")
            adopted = await http.get(f"{path}/bindings/{linked_result.json()['binding_id']}")
            assert adopted.json()["ownership"] == "customer_linked" and adopted.json()["state"] == "ready"

            started = await start({"action": "create", "configuration_revision": 1}, "create-scn03-0001")
            assert started.status_code == 202 and started.json()["state"] == "accepted"
            operation_id = started.json()["operation_id"]
            assert await run_work(operation_id, 0)
            assert (await http.get(f"{path}/binding-operations/{operation_id}")).json()["state"] == "outcome_unknown"
            assert await run_work(operation_id, 1)
            assert (await http.get(f"{path}/binding-operations/{operation_id}")).json()["state"] == "outcome_unknown"
            assert await run_work(operation_id, 2)
            recovered = await http.get(f"{path}/binding-operations/{operation_id}")
            assert recovered.json()["state"] == "succeeded"
            owned_id = recovered.json()["binding_id"]
            owned = await http.get(f"{path}/bindings/{owned_id}")
            assert owned.json()["state"] == "ready" and owned.json()["ownership"] == "adp_created"
            async with sessions() as session:
                assert (await session.get(Binding, ("tenant-example", owned_id))).creation_receipt["operation_id"] == operation_id
                assert await session.scalar(select(func.count()).select_from(ProfileClaim)) == 2
                assert await session.scalar(select(func.count()).select_from(Binding)) == 2
            assert len(creates) == client.create_agent_profile.call_count == 1
            assert not await run_work(operation_id, 3)

            cleanup_body = {"action": "cleanup", "configuration_revision": 1,
                            "binding_id": owned_id, "owned_cleanup_confirmed": True}
            refused_adopted = await start({**cleanup_body, "binding_id": adopted.json()["binding_id"]},
                                          "adopt-cleanup-scn03", adopted.headers["etag"])
            assert refused_adopted.status_code == 403
            for attempt, expected in enumerate(("native_cleanup_denied", "native_absence_unverified", None), 3):
                owned = await http.get(f"{path}/bindings/{owned_id}")
                cleanup = await start(cleanup_body, f"cleanup-scn03-{attempt:04d}", owned.headers["etag"])
                assert cleanup.status_code == 202 and await run_work(cleanup.json()["operation_id"], attempt)
                result = await http.get(f"{path}/binding-operations/{cleanup.json()['operation_id']}")
                assert result.json()["state"] == ("succeeded" if expected is None else "failed")
                if expected is not None:
                    assert result.json()["gaps"] == [expected]
                    async with sessions() as session:
                        debt = await session.scalar(select(SetupWork).where(
                            SetupWork.operation_id == cleanup.json()["operation_id"]))
                        assert debt.blocked_at is not None and debt.last_error == expected
                assert native_get(profileArn=adopted_arn) == adopted_native
            assert (await http.get(f"{path}/bindings/{owned_id}")).json()["state"] == "cleaned"
            assert owned_arn not in profiles and adopted_arn in profiles
            assert client.delete_agent_profile.call_count == 2
            assert all(call.kwargs["profileArn"] == owned_arn for call in client.delete_agent_profile.call_args_list)
            assert client.update_agent_profile.call_count == 0
            assert len(creates) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ac_04(tmp_path):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import Mock

    import httpx
    from fastapi import FastAPI
    from sqlalchemy import event, func, select

    from l5_services.adapters.well_architected.binding import ProfileAdapter, ProfileSelection
    from l5_services.admission import persist_admission
    from l5_services.api.bindings import BindingStore
    from l5_services.api.configuration import ConfigurationStore
    from l5_services.api.router import create_router
    from l5_services.models.binding import Binding
    from l5_services.models.configuration import Configuration, ConfigurationVersion
    from l5_services.models.receipt import RunReceipt
    from l5_services.models.run import Run
    from l5_services.models.work import Work
    from src.l5_integration import GatewayConfigurationBackend

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wa02-scn04.sqlite'}",
                                 connect_args={"timeout": 10})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    tenant, product, project = "tenant-example", "product-example", "project-example"
    account, region = "000000000000", "us-east-1"
    broker_role = f"arn:aws:iam::{account}:role/broker-example"
    execution_role = f"arn:aws:iam::{account}:role/source-only-example"
    profile_arn = f"arn:aws:wellarchitected:{region}:{account}:agent-profile/example"
    source = {"tenant_id": tenant, "project_id": project, "source_connection_id": "connection-example",
              "record_type": "document", "record_id": "architecture-example", "record_revision": "rev-1"}
    connection_ref = {**source, "record_type": "connection", "record_id": "connection-example"}
    scope = {"tenant_id": tenant, "product_id": product, "project_id": project}
    qualification = {"account_id": account, "region": region, "broker_role_arn": broker_role,
                     "execution_role_arn": execution_role, "trust": "qualified",
                     "source_permissions": "qualified", "support": "eligible"}
    selection = ProfileSelection(tenant, product, project, account, region, broker_role, execution_role,
                                 ("SECURITY",), qualification)
    native = {"arn": profile_arn, "pillars": ["SECURITY"], "executionRoleArn": execution_role,
              "aggregationConfiguration": [], "eligibleForArchitectureGeneration": True, "fieldErrors": []}
    client = Mock()
    client.get_agent_profile.side_effect = lambda *, profileArn: {**native, "arn": profileArn}
    client.list_agent_contexts.return_value = {"items": []}
    adapter = ProfileAdapter(client, account_id=account, region=region)
    pinned_hash = adapter.adopt(selection, profile_arn).snapshot_hash
    reference = {**source, "record_type": "agent_profile", "record_id": profile_arn,
                 "record_revision": pinned_hash}
    settings = {"enabled": True, "source_refs": [source], "connection_ref": connection_ref,
                "classification": "internal",
                "limits": {"deadline_seconds": 3600, "max_provider_attempts": 1, "max_polls": 30, "concurrency": 1,
                           "max_cost": "1.00", "currency": "USD", "policy_ref": {
                               **source, "record_type": "policy", "record_id": "example-budget"}},
                "triggers": [{"kind": "manual", "enabled": True, "capability": "architecture_review",
                              "binding_id": "binding-example"}],
                "well_architected": {"review_mode": "source_only", "pillars": ["SECURITY"],
                                     "execution_role_ref": {"record_id": execution_role}}}
    changed = {**settings, "source_refs": [{**source, "record_revision": "rev-2"}],
               "well_architected": {**settings["well_architected"], "execution_role_ref": {
                   "record_id": f"arn:aws:iam::{account}:role/other-example"}}}
    verified = {"configuration_id": "configuration-example", "configuration_revision": 1,
                "scope": scope, "connection_ref": connection_ref, "account_id": account,
                "region": region, "role_arn": broker_role, "source_role_qualification": qualification}
    request = {"service_id": "well_architected", "configuration_revision": 1,
               "binding_id": "binding-example", "capability": "architecture_review", "sources": [source]}
    configuration = Configuration(tenant_id=tenant, configuration_id="configuration-example",
                                  product_id=product, project_id=project, service_id="well_architected",
                                  relationship_revision="1", owner_id="owner-example",
                                  current_revision=1, handoff_state="linked")
    version = ConfigurationVersion(tenant_id=tenant, configuration_id="configuration-example",
                                   revision=1, settings=settings, inherited_fields=[], gaps=[],
                                   state="ready", hash="a" * 64)
    binding = Binding(tenant_id=tenant, binding_id="binding-example", configuration_id="configuration-example",
                      product_id=product, project_id=project, service_id="well_architected", scope=scope,
                      revision=1, generation=1, state="ready", ownership="customer_linked",
                      connection_ref=connection_ref, connection_key="connection-example",
                      native_resource_ref=reference, native_key="profile-example",
                      provider_identity={"account_id": account, "region": region, "role_arn": broker_role},
                      readiness=[{"snapshot_hash": pinned_hash}], gaps=[])

    def admission(label):
        run = Run(tenant_id=tenant, run_id=f"run-{label}", product_id=product, project_id=project,
                  service_id="well_architected", configuration_id=configuration.configuration_id,
                  configuration_revision=1, binding_id=binding.binding_id, binding_generation=1,
                  trigger_id="manual", input={"sources": [source]},
                  admission={"native_profile_ref": reference, "profile_hash": pinned_hash,
                             "source_refs": [source], "execution_role_arn": execution_role},
                  deadline_at=datetime.now(UTC) + timedelta(hours=1))
        receipt = RunReceipt(tenant_id=tenant, product_id=product, project_id=project,
                             service_id="well_architected", actor_id="owner-example", operation="submit",
                             idempotency_key=f"receipt-{label}", request_hash="a" * 64,
                             run_id=run.run_id, expires_at=datetime.now(UTC) + timedelta(hours=2))
        work = Work(tenant_id=tenant, work_id=f"work-{label}", run_id=run.run_id,
                    kind="execute", generation=1, step=0, due_at=datetime.now(UTC))
        return run, receipt, work

    try:
        async with sessions() as session:
            session.add_all((configuration, version, binding))
            await session.commit()
        async with sessions() as admission_session, sessions() as edit_session:
            held, release, edit_attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
            original_commit = admission_session.commit

            async def pause_commit():
                held.set()
                await release.wait()
                await original_commit()

            admission_session.commit = pause_commit
            admitted = asyncio.create_task(persist_admission(admission_session, *admission("first")))
            await asyncio.wait_for(held.wait(), 5)
            saved_configuration = await edit_session.get(Configuration, (tenant, configuration.configuration_id))
            saved_version = await edit_session.get(ConfigurationVersion, (tenant, configuration.configuration_id, 1))

            def observe_edit(connection, cursor, statement, parameters, context, executemany):
                if statement.startswith("UPDATE l5_service_configurations"):
                    edit_attempted.set()

            event.listen(engine.sync_engine, "before_cursor_execute", observe_edit)
            try:
                edit = asyncio.create_task(ConfigurationStore(edit_session, tenant, "owner-example").append(
                    saved_configuration, saved_version, changed, [], [], "ready", f'"{saved_version.hash}"'))
                await asyncio.wait_for(edit_attempted.wait(), 5)
                assert not edit.done()
                release.set()
                await asyncio.wait_for(admitted, 5)
                with pytest.raises(HTTPException) as fenced:
                    await asyncio.wait_for(edit, 5)
                assert fenced.value.detail == {"code": "native_generation_active"}
            finally:
                release.set()
                event.remove(engine.sync_engine, "before_cursor_execute", observe_edit)

        async with sessions() as session:
            original = await session.get(Run, (tenant, "run-first"))
            assert original.admission["native_profile_ref"] == reference
            assert original.admission["profile_hash"] == pinned_hash
            assert original.admission["source_refs"] == [source]
            assert (await session.get(Configuration, (tenant, "configuration-example"))).current_revision == 1
            with pytest.raises(HTTPException) as second:
                await persist_admission(session, *admission("second"))
            assert second.value.detail == {"code": "native_generation_active"}
            assert [await session.scalar(select(func.count()).select_from(model))
                    for model in (Run, RunReceipt, Work)] == [1, 1, 1]

        async with sessions() as session:
            actor = SimpleNamespace(org_id=tenant, user_id="owner-example", account_type="human")
            backend = GatewayConfigurationBackend(session, actor)

            async def current(*args):
                return configuration, version

            async def authority(*args):
                return BindingStore(session, tenant, product, project, "well_architected"), verified

            async def rights(*args):
                return {(source["source_connection_id"], source["record_id"])}

            async def native_client(*args):
                return client

            backend.current = current
            backend.binding_authority = authority
            backend.source_rights = rights
            backend.wa_profile_client = native_client
            app = FastAPI()
            app.include_router(create_router(lambda: backend))
            url = f"/v1/products/{product}/projects/{project}/services/well_architected/runs"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
                headers = {"Idempotency-Key": "admission-scn04-0001"}
                intact = await http.post(url, json=request, headers=headers)
                assert intact.status_code == 503 and intact.json()["detail"]["code"] == "work_authority_unavailable"
                native["pillars"] = ["RESILIENCE"]
                drifted = await http.post(url, json=request, headers=headers)
                assert drifted.status_code == 409 and drifted.json()["detail"]["code"] == "native_profile_drift"
                observed = adapter.adopt(selection, profile_arn)
                assert observed.snapshot_hash != pinned_hash and observed.state == "blocked"
                current_binding = await session.get(Binding, (tenant, "binding-example"))
                await session.refresh(current_binding)
                assert current_binding.state == "blocked"
                assert [item["snapshot_hash"] for item in current_binding.readiness] == [
                    pinned_hash, observed.snapshot_hash]
                assert current_binding.native_resource_ref == reference
                calls = client.get_agent_profile.call_count
                blocked = await http.post(url, json=request, headers=headers)
                assert blocked.status_code == 409 and client.get_agent_profile.call_count == calls
                with pytest.raises(HTTPException) as further:
                    await persist_admission(session, *admission("third"))
                assert further.value.detail == {"code": "stale_binding_generation"}
            original = await session.get(Run, (tenant, "run-first"))
            assert original.admission["profile_hash"] == pinned_hash
            assert [await session.scalar(select(func.count()).select_from(model))
                    for model in (Run, RunReceipt, Work)] == [1, 1, 1]
            assert client.create_agent_profile.call_count == client.delete_agent_profile.call_count == 0
    finally:
        await engine.dispose()
