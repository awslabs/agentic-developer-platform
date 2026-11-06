"""Exercise binding operations through the HTTP handler and persisted state."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator
from l5_services import runtime
from l5_services.adapters.security.binding import SecurityBindingAdapter
from l5_services.api.bindings import BindingStore
from l5_services.api.router import create_router
from l5_services.models import (
    Binding,
    BindingOperation,
    Configuration,
    SetupIntent,
    SetupWork,
)
from l5_services.models.base import Base
from l5_services.models.configuration import ConfigurationVersion
from l5_services.security_registration import (
    current_registration,
    observed_registration,
)
from l5_services.workers.execution import AuthorityRevoked, ExecutionService
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_configuration import configuration_scenario as _configuration_scenario

gateway_configuration_scenario = _configuration_scenario


@pytest.mark.asyncio
async def test_mounted_security_setup_uses_installed_factory_and_leased_sql_work(uncertain_setup_scenario, monkeypatch):
    scenario = uncertain_setup_scenario
    monkeypatch.setenv("L5_SERVICE_ID", "security")
    scenario["native"].lost_response = False
    selected = scenario["verified"]
    async with scenario["sessions"]() as session:
        session.add(ConfigurationVersion(
            tenant_id="tenant-one", configuration_id=selected["configuration_id"], revision=1,
            settings={"connection_ref": selected["connection_ref"]}, inherited_fields=[], gaps=[],
            state="needs_input", hash="a" * 64))
        await session.commit()

    gateway = AsyncMock()

    async def permit(path, *, json):
        assert path == "/internal/v1/l5/credential-session"
        assert json["connection_ref"] == {**selected["connection_ref"], "record_type": "credential"}
        async with scenario["sessions"]() as session:
            work = await session.get(SetupWork, ("tenant-one", json["attempt_id"]))
            assert work is not None and work.lease_owner == json["lease_owner"]
            assert work.fence == json["lease_fence"] and work.completed_at is None
        identity = {field: selected[field] for field in ("account_id", "role_arn", "region")}
        identity["verified_at"] = datetime.now(UTC).isoformat()
        issued = {"operation_id": json["operation_id"], "attempt_id": json["attempt_id"],
                  "delegation_revision": json["delegation_revision"], "setup_verified": selected,
                  "provider_identity": identity, "expires_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat()}
        return SimpleNamespace(status_code=200, json=lambda: issued, raise_for_status=lambda: None)

    gateway.post.side_effect = permit
    monkeypatch.setattr(runtime, "_gateway_transport", lambda: gateway)
    monkeypatch.setattr(runtime, "_sessions", lambda: scenario["sessions"])
    monkeypatch.setattr(runtime, "_archive_store", lambda: AsyncMock())
    monkeypatch.setattr(runtime, "_queue_publisher", lambda: AsyncMock())
    monkeypatch.setattr(runtime, "_provider_session", lambda *_: SimpleNamespace(
        client=lambda service, region_name: scenario["native"]))

    started = await scenario["client"].post(
        scenario["path"] + "/binding-operations", json={"action": "create", "configuration_revision": 1},
        headers={"Idempotency-Key": "installed_setup_00001"})
    assert started.status_code == 202, started.text
    async with scenario["sessions"]() as session:
        work = await session.scalar(select(SetupWork).where(SetupWork.operation_id == started.json()["operation_id"]))
        envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}
    worker = runtime.factory()
    assert await worker.execute(envelope)
    assert (await scenario["client"].get(scenario["path"] + "/binding-operations/" + started.json()["operation_id"])).json()["state"] == "succeeded"
    assert len(scenario["native"].create_attempts) == 1
    assert gateway.post.await_count >= 2
    assert not await worker.execute(envelope)
    assert len(scenario["native"].create_attempts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ("revoked", "wrong_revision", "expired"))
async def test_mounted_security_setup_denied_broker_receipt_never_replays(uncertain_setup_scenario, monkeypatch, denial):
    scenario = uncertain_setup_scenario
    monkeypatch.setenv("L5_SERVICE_ID", "security")
    selected = scenario["verified"]
    async with scenario["sessions"]() as session:
        session.add(ConfigurationVersion(
            tenant_id="tenant-one", configuration_id=selected["configuration_id"], revision=1,
            settings={"connection_ref": selected["connection_ref"]}, inherited_fields=[], gaps=[],
            state="needs_input", hash="a" * 64))
        await session.commit()

    gateway = AsyncMock()

    async def denied(path, *, json):
        assert path == "/internal/v1/l5/credential-session"
        async with scenario["sessions"]() as session:
            work = await session.get(SetupWork, ("tenant-one", json["attempt_id"]))
            assert work.lease_owner == json["lease_owner"] and work.fence == json["lease_fence"]
        if denial == "revoked":
            return SimpleNamespace(status_code=403)
        identity = {field: selected[field] for field in ("account_id", "role_arn", "region")}
        verified = {**selected, "configuration_revision": 2} if denial == "wrong_revision" else selected
        issued = {"operation_id": json["operation_id"], "attempt_id": json["attempt_id"],
                  "delegation_revision": json["delegation_revision"], "setup_verified": verified,
                  "provider_identity": identity,
                  "expires_at": (datetime.now(UTC) + timedelta(minutes=-1 if denial == "expired" else 2)).isoformat()}
        return SimpleNamespace(status_code=200, json=lambda: issued, raise_for_status=lambda: None)

    gateway.post.side_effect = denied
    provider_factory = AsyncMock()
    monkeypatch.setattr(runtime, "_gateway_transport", lambda: gateway)
    monkeypatch.setattr(runtime, "_sessions", lambda: scenario["sessions"])
    monkeypatch.setattr(runtime, "_archive_store", lambda: AsyncMock())
    monkeypatch.setattr(runtime, "_queue_publisher", lambda: AsyncMock())
    monkeypatch.setattr(runtime, "_provider_session", provider_factory)

    started = await scenario["client"].post(
        scenario["path"] + "/binding-operations", json={"action": "create", "configuration_revision": 1},
        headers={"Idempotency-Key": f"installed_denial_{denial}_0001"})
    assert started.status_code == 202, started.text
    async with scenario["sessions"]() as session:
        work = await session.scalar(select(SetupWork).where(SetupWork.operation_id == started.json()["operation_id"]))
        envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}
    worker = runtime.factory()
    assert not await worker.execute(envelope)
    assert not await worker.execute(envelope)
    async with scenario["sessions"]() as session:
        work = await session.get(SetupWork, ("tenant-one", envelope["work_id"]))


@pytest.mark.asyncio
async def test_uncertain_setup_revoked_on_redelivery_keeps_native_history(uncertain_setup_scenario):
    scenario = uncertain_setup_scenario
    started = await scenario["client"].post(
        scenario["path"] + "/binding-operations", json={"action": "create", "configuration_revision": 1},
        headers={"Idempotency-Key": "uncertain_revoked_redelivery_0001"})
    assert started.status_code == 202
    operation_id = started.json()["operation_id"]
    async with scenario["sessions"]() as session:
        work = await session.scalar(select(SetupWork).where(SetupWork.operation_id == operation_id))
        envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}

    now = datetime.now(UTC) + timedelta(minutes=1)
    worker = ExecutionService(scenario["sessions"], None, None, None, None,
                              binding_authorize=scenario["authorize"], clock=lambda: now)
    assert await worker.execute(envelope)
    assert len(scenario["native"].create_attempts) == 1
    async with scenario["sessions"]() as session:
        operation = await session.get(BindingOperation, ("tenant-one", operation_id))
        assert operation.state == "outcome_unknown" and operation.attempts == 1

    async def revoked(intent, claim):
        raise AuthorityRevoked("current setup work unavailable")

    later = now + timedelta(minutes=6)
    denied = ExecutionService(scenario["sessions"], None, None, None, None,
                              binding_authorize=revoked, clock=lambda: later)
    assert not await denied.execute(envelope)
    assert not await denied.execute(envelope)
    restored = ExecutionService(scenario["sessions"], None, None, None, None,
                                binding_authorize=scenario["authorize"], clock=lambda: later + timedelta(minutes=6))
    assert not await restored.execute(envelope)
    async with scenario["sessions"]() as session:
        operation = await session.get(BindingOperation, ("tenant-one", operation_id))
        work = await session.get(SetupWork, ("tenant-one", envelope["work_id"]))
        assert operation.state == "outcome_unknown" and operation.attempts == 1
@pytest.mark.parametrize("native_bucket", [
    "", "https://example-source-staging", "s3://example-source-staging", "example-source-staging/path",
    "arn:aws:s3:us-east-1:123456789012:accesspoint/example", "bad..bucket", "arn:aws:s3:::bad..bucket", None,
])
def test_security_binding_rejects_invalid_bucket_locations(native_bucket):
    assert observed_registration({"iamRoles": ["arn:aws:iam::123456789012:role/SourceReader"],
                                  "s3Buckets": [native_bucket]}, "123456789012") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("native_bucket", ["example-source-staging", "arn:aws:s3:::example-source-staging"])
async def test_security_binding_requires_selected_native_registration(native_bucket):
    identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/Selected",
                "region": "us-east-1", "verified_at": "2026-10-26T00:00:00Z"}
    reference = {"record_id": "space-one", "record_revision": "2026-10-26T00:00:00+00:00"}
    space = {"agentSpaceId": "space-one", "createdAt": datetime(2026, 10, 26, tzinfo=UTC),
             "awsResources": {"iamRoles": ["arn:aws:iam::123456789012:role/SourceReader"],
                              "s3Buckets": [native_bucket]}}

    class Native:
        def batch_get_agent_spaces(self, *, agentSpaceIds):
            return {"agentSpaces": [space] if agentSpaceIds == ["space-one"] else []}

    adapter = SecurityBindingAdapter(Native(), identity)
    readback = await adapter.read(reference, identity)
    assert readback["state"] == "ready" and readback["gaps"] == []
    binding = type("Binding", (), {"state": "ready", "gaps": [], "readiness": readback["readiness"],
                                 "provider_identity": identity})()
    assert current_registration(binding) == {"service_role": "arn:aws:iam::123456789012:role/SourceReader",
                                             "bucket": "example-source-staging"}
    binding.readiness[0]["observed_at"] = "2026-10-26T00:00:00+00:00"
    assert current_registration(binding) is None
    space["awsResources"]["iamRoles"] = ["arn:aws:iam::210987654321:role/Foreign"]
    assert (await adapter.read(reference, identity))["state"] == "blocked"
    space["awsResources"]["iamRoles"] = ["arn:aws:iam::123456789012:role/SourceReader"]
    space["awsResources"]["s3Buckets"] = []
    assert (await adapter.read(reference, identity))["state"] == "blocked"
    space["awsResources"]["s3Buckets"] = ["arn:aws:s3:::example-source-staging", "arn:aws:s3:::other-bucket"]
    assert (await adapter.read(reference, identity))["state"] == "blocked"


@pytest.fixture
async def uncertain_setup_scenario(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'uncertain.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        for tenant_id in ("tenant-one", "tenant-two"):
            for product_id in ("product-one", "product-two"):
                configuration_id = "config-one" if (tenant_id, product_id) == ("tenant-one", "product-one") else f"{tenant_id}-{product_id}"
                session.add(Configuration(tenant_id=tenant_id, configuration_id=configuration_id,
                                          product_id=product_id, project_id="project-one", service_id="security",
                                          relationship_revision="1", owner_id="owner-one" if tenant_id == "tenant-one" else "owner-two",
                                          current_revision=1, handoff_state="linked"))
        await session.commit()

    connection_ref = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "connection-one",
                      "record_type": "connection", "record_id": "connection-one", "record_revision": "1"}
    verified = {"configuration_id": "config-one", "configuration_revision": 1,
                "scope": {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one",
                          "relationship_revision": "1", "owner_id": "owner-one", "visibility": "only_me"},
                "connection_ref": connection_ref, "account_id": "123456789012", "region": "us-east-1",
                "role_arn": "arn:aws:iam::123456789012:role/FictionalRole",
                "authorization": {"actor_id": "owner-one", "delegation_id": "delegation-one",
                                  "delegation_revision": 1, "authorized_at": "2026-10-26T00:00:00Z"}}
    identity = {"account_id": verified["account_id"], "region": verified["region"],
                "role_arn": verified["role_arn"], "verified_at": "2026-10-26T00:00:00Z"}
    profiles = {("tenant-one", "product-one"): verified}
    for tenant_id in ("tenant-one", "tenant-two"):
        for product_id in ("product-one", "product-two"):
            if (tenant_id, product_id) == ("tenant-one", "product-one"):
                continue
            profiles[(tenant_id, product_id)] = {
                **verified, "configuration_id": f"{tenant_id}-{product_id}",
                "scope": {**verified["scope"], "tenant_id": tenant_id, "product_id": product_id,
                          "owner_id": "owner-one" if tenant_id == "tenant-one" else "owner-two"},
                "connection_ref": {**connection_ref, "tenant_id": tenant_id,
                                   "source_connection_id": "connection-one" if tenant_id == "tenant-one" else "connection-two",
                                   "record_id": "connection-one" if tenant_id == "tenant-one" else "connection-two"},
                "account_id": verified["account_id"] if tenant_id == "tenant-one" else "210987654321",
                "role_arn": verified["role_arn"] if tenant_id == "tenant-one" else
                            "arn:aws:iam::210987654321:role/OtherRole",
                "authorization": {**verified["authorization"],
                                  "actor_id": "owner-one" if tenant_id == "tenant-one" else "owner-two",
                                  "delegation_id": "delegation-one" if tenant_id == "tenant-one" else "delegation-two"},
            }
    active_tenant = {"id": "tenant-one"}

    class NativeClient:
        def __init__(self):
            self.spaces = {}
            self.create_attempts = []
            self.read_attempts = []
            self.visible = True
            self.lost_response = True

        def create_agent_space(self, *, name, description, tags):
            self.create_attempts.append((name, description, tags))
            space_id = "as-first" if len(self.create_attempts) == 1 else f"as-{len(self.create_attempts)}"
            self.spaces[name] = {"agentSpaceId": space_id, "name": name, "description": description,
                                 "createdAt": datetime(2026, 10, 26, 0, 0, tzinfo=UTC)}
            if self.lost_response:
                raise TimeoutError("provider committed, response lost")
            return {"agentSpaceId": space_id}

        def list_agent_spaces(self, **parameters):
            self.read_attempts.append(("list", parameters))
            return {"agentSpaceSummaries": [
                {"name": name, "agentSpaceId": space["agentSpaceId"]}
                for name, space in self.spaces.items()]} if self.visible else {"agentSpaceSummaries": []}

        def batch_get_agent_spaces(self, *, agentSpaceIds):
            self.read_attempts.append(("batch", agentSpaceIds))
            matches = [space for space in self.spaces.values()
                       if self.visible and space["agentSpaceId"] in agentSpaceIds]
            return {"agentSpaces": matches, "notFound": [space_id for space_id in agentSpaceIds
                                                     if space_id not in {space["agentSpaceId"] for space in matches}]}

    natives = {tenant_id: NativeClient() for tenant_id in ("tenant-one", "tenant-two")}
    adapters = {"tenant-one": SecurityBindingAdapter(natives["tenant-one"], identity),
                "tenant-two": SecurityBindingAdapter(natives["tenant-two"], {
                    "account_id": profiles[("tenant-two", "product-one")]["account_id"],
                    "role_arn": profiles[("tenant-two", "product-one")]["role_arn"],
                    "region": verified["region"], "verified_at": identity["verified_at"]})}
    native = natives["tenant-one"]
    adapter = adapters["tenant-one"]
    revoked = set()

    class Backend:
        def __init__(self):
            self.session = None

        def store(self, product_id, project_id, service_id):
            if ((active_tenant["id"], product_id) not in profiles or project_id != "project-one"
                    or service_id != "security"):
                raise HTTPException(404, detail={"code": "not_found"})
            return BindingStore(self.session, active_tenant["id"], product_id, project_id, service_id)

        def authority(self, product_id, project_id, service_id):
            self.store(product_id, project_id, service_id)
            if (active_tenant["id"], product_id) in revoked:
                raise HTTPException(403, detail={"code": "delegation_denied"})
            return profiles[(active_tenant["id"], product_id)]

        async def start_binding(self, product_id, project_id, service_id, request, key, if_match=None):
            store = self.store(product_id, project_id, service_id)
            selected = self.authority(product_id, project_id, service_id)
            if request.get("action") in {"check", "update", "suspend", "resume", "unlink", "cleanup"}:
                return await store.control(request, key, if_match, selected)
            return await store.start(request, key, selected)

        async def reconcile_binding(self, product_id, project_id, service_id, operation_id, reason, key):
            return await self.store(product_id, project_id, service_id).request_reconcile(
                operation_id, reason, key, self.authority(product_id, project_id, service_id))

        async def get_binding_operation(self, product_id, project_id, service_id, operation_id):
            return await self.store(product_id, project_id, service_id).operation(operation_id)

        async def get_binding(self, product_id, project_id, service_id, binding_id):
            return await self.store(product_id, project_id, service_id).binding(binding_id)

        async def list_bindings(self, product_id, project_id, service_id):
            return await self.store(product_id, project_id, service_id).list_bindings()

    backend = Backend()
    app = FastAPI()
    app.include_router(create_router(lambda: backend))

    async def authorize(intent, claim):
        assert intent.authorization["delegation_revision"] == 1
        selected = profiles[(intent.tenant_id, intent.product_id)]
        assert intent.scope == selected["scope"] and intent.configuration_id == selected["configuration_id"]
        if (intent.tenant_id, intent.product_id) in revoked:
            raise HTTPException(403, detail={"code": "delegation_denied"})
        return selected, adapters[intent.tenant_id]

    async with sessions() as session, AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        backend.session = session
        yield {"client": client, "native": native, "adapter": adapter, "natives": natives,
               "profiles": profiles, "active_tenant": active_tenant, "revoked": revoked, "sessions": sessions,
               "verified": verified, "authorize": authorize, "path":
               "/v1/products/product-one/projects/project-one/services/security"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_uncertain_setup(uncertain_setup_scenario, monkeypatch):
    from l5_services.api import bindings as binding_api

    fixed_time = datetime(2026, 10, 26, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_time if tz is not None else fixed_time.replace(tzinfo=None)

    monkeypatch.setattr(binding_api, "datetime", FrozenDateTime)
    scenario = uncertain_setup_scenario
    client, path = scenario["client"], scenario["path"]
    body = {"action": "create", "configuration_revision": 1}
    headers = {"Idempotency-Key": "one_create_setup_000001"}
    started = await client.post(path + "/binding-operations", json=body, headers=headers)
    assert started.status_code == 202 and started.json()["state"] == "accepted"
    operation_id = started.json()["operation_id"]
    async with scenario["sessions"]() as reader:
        work = await reader.scalar(select(SetupWork).where(SetupWork.operation_id == operation_id))
        intent = await reader.get(SetupIntent, ("tenant-one", operation_id))
        assert work is not None and intent.request == body and intent.binding_generation == 1
        envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}
    worker = ExecutionService(scenario["sessions"], None, None, None, None,
                              binding_authorize=scenario["authorize"], clock=lambda: fixed_time)
    assert await worker.execute(envelope)
    unknown = await client.get(path + "/binding-operations/" + operation_id)
    assert unknown.status_code == 200 and unknown.json()["state"] == "outcome_unknown"
    assert (await client.post(path + "/binding-operations", json=body, headers=headers)).json() == unknown.json()
    assert len(scenario["native"].create_attempts) == 1
    restarted = ExecutionService(scenario["sessions"], None, None, None, None,
                                 binding_authorize=scenario["authorize"],
                                 clock=lambda: fixed_time + timedelta(minutes=6))
    assert await restarted.execute(envelope)
    completed = await client.get(path + "/binding-operations/" + operation_id)
    assert completed.status_code == 200 and completed.json()["state"] == "succeeded"
    binding = await client.get(path + "/bindings/" + completed.json()["binding_id"])
    first_binding_id = binding.json()["binding_id"]
    assert binding.json()["ownership"] == "adp_created" and binding.json()["state"] == "blocked"
    assert binding.json()["native_resource_ref"]["record_id"] == "as-first"
    assert len(scenario["native"].create_attempts) == 1
    assert scenario["native"].read_attempts
    async with scenario["sessions"]() as reader:
        operation = await reader.get(BindingOperation, ("tenant-one", operation_id))
        owned = await reader.get(Binding, ("tenant-one", completed.json()["binding_id"]))
        assert operation.attempts == 1 and operation.reconcile_checks == 1
        assert operation.creation_receipt == owned.creation_receipt
        assert operation.creation_receipt["intent_hash"] == intent.intent_hash

    profiles = scenario["profiles"]
    native_clients = scenario["natives"]
    for tenant_id, product_id in (("tenant-one", "product-two"),
                                  ("tenant-two", "product-one"), ("tenant-two", "product-two")):
        scenario["active_tenant"]["id"] = tenant_id
        scoped_path = f"/v1/products/{product_id}/projects/project-one/services/security"
        selected = profiles[(tenant_id, product_id)]
        native = native_clients[tenant_id]
        before_creates = len(native.create_attempts)
        before_reads = len(native.read_attempts)
        assert (await client.get(scoped_path + "/bindings")).json()["items"] == []
        accepted = await client.post(scoped_path + "/binding-operations", json=body, headers=headers)
        assert accepted.status_code == 202 and accepted.json()["state"] == "accepted"
        operation_id = accepted.json()["operation_id"]
        async with scenario["sessions"]() as reader:
            persisted_intent = await reader.get(SetupIntent, (tenant_id, operation_id))
            work = await reader.scalar(select(SetupWork).where(
                SetupWork.tenant_id == tenant_id, SetupWork.operation_id == operation_id))
            assert persisted_intent.request == body and persisted_intent.scope == selected["scope"]
            assert persisted_intent.configuration_id == selected["configuration_id"]
            assert persisted_intent.authorization["delegation_id"] == selected["authorization"]["delegation_id"]
            assert persisted_intent.binding_generation == 1 and work.due_at == fixed_time.replace(tzinfo=None)
            envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}
        assert await worker.execute(envelope)
        unknown = await client.get(scoped_path + "/binding-operations/" + operation_id)
        assert unknown.status_code == 200 and unknown.json()["state"] == "outcome_unknown"
        assert (await client.post(scoped_path + "/binding-operations", json=body,
                                  headers=headers)).json() == unknown.json()
        assert len(native.create_attempts) == before_creates + 1 and len(native.spaces) == before_creates + 1
        assert len(native.read_attempts) == before_reads
        async with scenario["sessions"]() as reader:
            assert (await reader.scalars(select(Binding).where(
                Binding.tenant_id == tenant_id, Binding.configuration_id == selected["configuration_id"]))).all() == []
        assert not await worker.execute(envelope)
        assert await restarted.execute(envelope)
        completed = await client.get(scoped_path + "/binding-operations/" + operation_id)
        assert completed.status_code == 200 and completed.json()["state"] == "succeeded"
        binding_id = completed.json()["binding_id"]
        own_space = native.spaces["adp-" + operation_id]
        readback = await client.get(scoped_path + "/bindings/" + binding_id)
        assert readback.json()["ownership"] == "adp_created"
        assert readback.json()["native_resource_ref"]["record_id"] == own_space["agentSpaceId"]
        assert readback.json()["state"] == "blocked" and readback.json()["gaps"] == ["source_readiness_unverified"]
        async with scenario["sessions"]() as reader:
            outcome = await reader.get(BindingOperation, (tenant_id, operation_id))
            binding = await reader.get(Binding, (tenant_id, binding_id))
            saved_work = await reader.get(SetupWork, (tenant_id, work.work_id))
            assert outcome.attempts == 1 and outcome.reconcile_checks == 1
            assert outcome.creation_receipt == binding.creation_receipt
            assert binding.creation_receipt["intent_hash"] == persisted_intent.intent_hash
            assert binding.configuration_id == selected["configuration_id"] and binding.scope == selected["scope"]
            assert binding.provider_identity["account_id"] == selected["account_id"]
            assert saved_work.completed_at is not None and saved_work.generation == 1
        assert len(native.create_attempts) == before_creates + 1 and len(native.read_attempts) > before_reads
        other_product = "product-two" if product_id == "product-one" else "product-one"
        wrong_product_path = f"/v1/products/{other_product}/projects/project-one/services/security"
        assert (await client.get(wrong_product_path + "/bindings/" + binding_id)).status_code == 404
        assert (await client.get(wrong_product_path + "/binding-operations/" + operation_id)).status_code == 404

    scenario["active_tenant"]["id"] = "tenant-two"
    assert (await client.get(path + "/bindings/" + first_binding_id)).status_code == 404
    assert (await client.get(path + "/binding-operations/" + started.json()["operation_id"])).status_code == 404
    assert len(native_clients["tenant-one"].create_attempts) == 2
    assert len(native_clients["tenant-two"].create_attempts) == 2
    async with scenario["sessions"]() as reader:
        assert len((await reader.scalars(select(Binding))).all()) == 4
        assert len((await reader.scalars(select(SetupIntent))).all()) == 4


@pytest.mark.asyncio
async def test_scope_and_cleanup(uncertain_setup_scenario, monkeypatch):
    from l5_services.api import bindings as binding_api

    fixed_time = datetime(2026, 10, 26, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_time if tz is not None else fixed_time.replace(tzinfo=None)

    monkeypatch.setattr(binding_api, "datetime", FrozenDateTime)
    scenario = uncertain_setup_scenario
    client, path, native = scenario["client"], scenario["path"], scenario["native"]
    native.lost_response = False
    worker = ExecutionService(scenario["sessions"], None, None, None, None,
                              binding_authorize=scenario["authorize"], clock=lambda: fixed_time)

    async def run(operation_id, *, tenant_id="tenant-one", scoped_path=path):
        async with scenario["sessions"]() as reader:
            work = await reader.scalar(select(SetupWork).where(
                SetupWork.tenant_id == tenant_id, SetupWork.operation_id == operation_id))
            envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}
        assert await worker.execute(envelope)
        return (await client.get(scoped_path + "/binding-operations/" + operation_id)).json()

    created = await client.post(path + "/binding-operations", json={"action": "create", "configuration_revision": 1},
                                headers={"Idempotency-Key": "owned_fixture_0000000001"})
    owned = await run(created.json()["operation_id"])
    owned_id = owned["binding_id"]
    first_owned_id = owned_id
    assert owned["state"] == "succeeded"
    created_at = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
    native.spaces["foreign"] = {"agentSpaceId": "foreign-space", "name": "customer-owned",
                                "description": "not an ADP setup", "createdAt": created_at}
    reference = {**scenario["verified"]["connection_ref"], "record_type": "security_space",
                 "record_id": "foreign-space", "record_revision": created_at.isoformat()}
    linked = await client.post(path + "/binding-operations", json={"action": "link", "configuration_revision": 1,
                               "native_resource_ref": reference}, headers={"Idempotency-Key": "linked_fixture_000000001"})
    linked_id = (await run(linked.json()["operation_id"]))["binding_id"]
    assert linked_id != owned_id

    async def control(action, binding_id, key, *, scoped_path=path, **extra):
        current = await client.get(scoped_path + "/bindings/" + binding_id)
        body = {"action": action, "binding_id": binding_id, "configuration_revision": 1, **extra}
        return await client.post(scoped_path + "/binding-operations", json=body,
                                 headers={"Idempotency-Key": key, "If-Match": current.headers["ETag"]})

    for number, action in enumerate(("check", "update", "suspend", "resume", "unlink")):
        admitted = await control(action, linked_id, f"linked_control_{number}_00000001")
        assert admitted.status_code == 202, admitted.text
        assert (await run(admitted.json()["operation_id"]))["state"] == "succeeded"
        current = (await client.get(path + "/bindings/" + linked_id)).json()
        assert current["generation"] == number + 2
        if action == "suspend":
            assert current["state"] == "suspended"
        if action == "unlink":
            assert current["state"] == "unlinked" and "foreign" in native.spaces

    denied = await control("cleanup", linked_id, "linked_cleanup_000000001", owned_cleanup_confirmed=True)
    assert denied.status_code == 403 and "foreign" in native.spaces

    def unchanged_delete(*, agentSpaceId):
        assert agentSpaceId == "as-first"
        return {}

    native.delete_agent_space = unchanged_delete
    failed = await control("cleanup", owned_id, "owned_cleanup_fail_000001", owned_cleanup_confirmed=True)
    assert failed.status_code == 202
    assert (await run(failed.json()["operation_id"]))["state"] == "failed"
    async with scenario["sessions"]() as reader:
        work = await reader.scalar(select(SetupWork).where(SetupWork.operation_id == failed.json()["operation_id"]))
        intent = await reader.get(SetupIntent, ("tenant-one", failed.json()["operation_id"]))
        assert work.blocked_at is not None and work.last_error == "native_absence_unverified"
        assert intent.owned_resource_receipt["operation_id"] == created.json()["operation_id"]
    assert (await client.get(path + "/bindings/" + owned_id)).json()["state"] == "cleanup_failed"

    def remove_owned(*, agentSpaceId):
        assert agentSpaceId == "as-first"
        native.spaces.pop("adp-" + created.json()["operation_id"])
        return {}

    native.delete_agent_space = remove_owned
    scenario["adapter"].provider_identity = {**scenario["adapter"].provider_identity,
                                             "verified_at": "2026-10-26T01:00:00Z"}
    cleaned = await control("cleanup", owned_id, "owned_cleanup_retry_00001", owned_cleanup_confirmed=True)
    assert cleaned.status_code == 202
    assert (await run(cleaned.json()["operation_id"]))["state"] == "succeeded"
    assert "foreign" in native.spaces and (await client.get(path + "/bindings/" + owned_id)).json()["state"] == "cleaned"

    for tenant_id, product_id in (("tenant-one", "product-two"),
                                  ("tenant-two", "product-one"), ("tenant-two", "product-two")):
        scenario["active_tenant"]["id"] = tenant_id
        scoped_path = f"/v1/products/{product_id}/projects/project-one/services/security"
        selected = scenario["profiles"][(tenant_id, product_id)]
        native_client = scenario["natives"][tenant_id]
        native_client.lost_response = False
        customer_name = f"customer-{product_id}"
        customer_id = f"as-customer-{product_id}"
        native_client.spaces[customer_name] = {"agentSpaceId": customer_id, "name": customer_name,
                                               "description": "Customer-managed", "createdAt": fixed_time}
        customer_ref = {**selected["connection_ref"], "record_type": "security_space",
                        "record_id": customer_id, "record_revision": fixed_time.isoformat()}
        linked_operation = await client.post(scoped_path + "/binding-operations", json={
            "action": "link", "configuration_revision": 1, "native_resource_ref": customer_ref},
            headers={"Idempotency-Key": f"link_{tenant_id}_{product_id}_scope_01"})
        assert linked_operation.status_code == 202
        linked_id = (await run(linked_operation.json()["operation_id"], tenant_id=tenant_id,
                               scoped_path=scoped_path))["binding_id"]
        before_creates = len(native_client.create_attempts)
        owned_operation = await client.post(scoped_path + "/binding-operations", json={
            "action": "create", "configuration_revision": 1},
            headers={"Idempotency-Key": f"create_{tenant_id}_{product_id}_scope_01"})
        assert owned_operation.status_code == 202
        created_operation_id = owned_operation.json()["operation_id"]
        owned_id = (await run(created_operation_id, tenant_id=tenant_id, scoped_path=scoped_path))["binding_id"]
        space_name = "adp-" + created_operation_id
        native_space = native_client.spaces[space_name]
        assert len(native_client.create_attempts) == before_creates + 1
        assert linked_id != owned_id and native_space["name"] == space_name
        async with scenario["sessions"]() as reader:
            owned_binding = await reader.get(Binding, (tenant_id, owned_id))
            source_intent = await reader.get(SetupIntent, (tenant_id, created_operation_id))
            assert owned_binding.configuration_id == selected["configuration_id"]
            assert owned_binding.scope == source_intent.scope == selected["scope"]
            assert owned_binding.creation_receipt["intent_hash"] == source_intent.intent_hash
            assert owned_binding.native_resource_ref["record_id"] == native_space["agentSpaceId"]

        other_product = "product-two" if product_id == "product-one" else "product-one"
        wrong_path = f"/v1/products/{other_product}/projects/project-one/services/security"
        assert (await client.get(wrong_path + "/bindings/" + owned_id)).status_code == 404
        assert (await client.get(wrong_path + "/binding-operations/" + created_operation_id)).status_code == 404
        foreign_cleanup = await client.post(wrong_path + "/binding-operations", json={
            "action": "cleanup", "configuration_revision": 1, "binding_id": owned_id,
            "owned_cleanup_confirmed": True}, headers={"If-Match": '"1"',
                                                      "Idempotency-Key": f"foreign_{tenant_id}_{product_id}_cleanup_01"})
        assert foreign_cleanup.status_code == 404 and space_name in native_client.spaces
        linked_cleanup = await control("cleanup", linked_id, f"linked_{tenant_id}_{product_id}_denied_01",
                                       scoped_path=scoped_path, owned_cleanup_confirmed=True)
        assert linked_cleanup.status_code == 403 and customer_name in native_client.spaces

        if (tenant_id, product_id) == ("tenant-two", "product-one"):
            checking = await control("check", owned_id, "revoked_scope_check_00001", scoped_path=scoped_path)
            assert checking.status_code == 202
            async with scenario["sessions"]() as reader:
                revoked_work = await reader.scalar(select(SetupWork).where(
                    SetupWork.tenant_id == tenant_id,
                    SetupWork.operation_id == checking.json()["operation_id"]))
                revoked_envelope = {"schema_version": "1.0", "work_id": revoked_work.work_id,
                                    "generation": revoked_work.generation}
            reads_before = len(native_client.read_attempts)
            scenario["revoked"].add((tenant_id, product_id))
            assert (await control("unlink", linked_id, "revoked_scope_unlink_00001",
                                  scoped_path=scoped_path)).status_code == 403
            assert not await worker.execute(revoked_envelope)
            async with scenario["sessions"]() as reader:
                blocked = await reader.get(SetupWork, (tenant_id, revoked_work.work_id))
                outcome = await reader.get(BindingOperation, (tenant_id, checking.json()["operation_id"]))
                assert blocked.blocked_at is not None and blocked.last_error == "setup_authority_denied"
                assert outcome.state == "accepted" and outcome.attempts == 0
            assert len(native_client.read_attempts) == reads_before
            scenario["revoked"].remove((tenant_id, product_id))

        deletion_attempts = []
        inject_failure = (tenant_id, product_id) == ("tenant-two", "product-two")

        def delete_owned(*, agentSpaceId):
            deletion_attempts.append(agentSpaceId)
            assert agentSpaceId == native_space["agentSpaceId"]
            if not inject_failure or len(deletion_attempts) > 1:
                native_client.spaces.pop(space_name)
            return {"agentSpaceId": agentSpaceId}

        native_client.delete_agent_space = delete_owned
        cleanup = await control("cleanup", owned_id, f"cleanup_{tenant_id}_{product_id}_scope_01",
                                scoped_path=scoped_path, owned_cleanup_confirmed=True)
        assert cleanup.status_code == 202
        cleanup_result = await run(cleanup.json()["operation_id"], tenant_id=tenant_id, scoped_path=scoped_path)
        if inject_failure:
            assert cleanup_result["state"] == "failed" and cleanup_result["gaps"] == ["native_absence_unverified"]
            async with scenario["sessions"]() as reader:
                failed_work = await reader.scalar(select(SetupWork).where(
                    SetupWork.tenant_id == tenant_id, SetupWork.operation_id == cleanup.json()["operation_id"]))
                assert failed_work.blocked_at is not None and failed_work.last_error == "native_absence_unverified"
            assert space_name in native_client.spaces
            cleanup = await control("cleanup", owned_id, f"retry_{tenant_id}_{product_id}_cleanup_01",
                                    scoped_path=scoped_path, owned_cleanup_confirmed=True)
            assert cleanup.status_code == 202
            cleanup_result = await run(cleanup.json()["operation_id"], tenant_id=tenant_id,
                                       scoped_path=scoped_path)
        assert cleanup_result["state"] == "succeeded"
        assert deletion_attempts == [native_space["agentSpaceId"]] * (2 if inject_failure else 1)
        assert space_name not in native_client.spaces and native_client.spaces[customer_name]["agentSpaceId"] == customer_id
        final = await client.get(scoped_path + "/bindings/" + owned_id)
        assert final.json()["state"] == "cleaned"
        async with scenario["sessions"]() as reader:
            final_binding = await reader.get(Binding, (tenant_id, owned_id))
            final_operation = await reader.get(BindingOperation, (tenant_id, cleanup.json()["operation_id"]))
            final_work = await reader.scalar(select(SetupWork).where(
                SetupWork.tenant_id == tenant_id, SetupWork.operation_id == cleanup.json()["operation_id"]))
            final_intent = await reader.get(SetupIntent, (tenant_id, cleanup.json()["operation_id"]))
            assert final_operation.attempts == 1 and final_work.completed_at is not None
            assert final_intent.binding_generation == final_binding.generation
            assert final_intent.owned_resource_receipt == final_binding.creation_receipt
            assert final_binding.configuration_id == selected["configuration_id"]
        listing = await client.get(scoped_path + "/bindings")
        assert {item["binding_id"] for item in listing.json()["items"]} == {linked_id, owned_id}

    scenario["active_tenant"]["id"] = "tenant-two"
    assert (await client.get(path + "/bindings/" + first_owned_id)).status_code == 404
    assert (await client.get(path + "/binding-operations/" + created.json()["operation_id"])).status_code == 404
    assert "foreign" in scenario["natives"]["tenant-one"].spaces


@pytest.mark.parametrize("readback_issue", ("missing_not_found", "stale_list"))
@pytest.mark.asyncio
async def test_cleanup_requires_proof_and_confirmed_absence(uncertain_setup_scenario, readback_issue):
    scenario = uncertain_setup_scenario
    client, path, native = scenario["client"], scenario["path"], scenario["native"]
    native.lost_response = False
    worker = ExecutionService(scenario["sessions"], None, None, None, None,
                              binding_authorize=scenario["authorize"])

    async def finish(operation_id):
        async with scenario["sessions"]() as reader:
            work = await reader.scalar(select(SetupWork).where(SetupWork.operation_id == operation_id))
            envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}
        assert await worker.execute(envelope)
        return (await client.get(path + "/binding-operations/" + operation_id)).json()

    created = await client.post(path + "/binding-operations", json={"action": "create", "configuration_revision": 1},
                                headers={"Idempotency-Key": "proof_fixture_create_0001"})
    assert created.status_code == 202
    owned_id = (await finish(created.json()["operation_id"]))["binding_id"]
    created_at = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
    native.spaces["customer"] = {"agentSpaceId": "as-customer", "name": "customer-space",
                                 "description": "Customer setup", "createdAt": created_at}
    reference = {**scenario["verified"]["connection_ref"], "record_type": "security_space",
                 "record_id": "as-customer", "record_revision": created_at.isoformat()}
    linked = await client.post(path + "/binding-operations", json={"action": "link", "configuration_revision": 1,
                               "native_resource_ref": reference}, headers={"Idempotency-Key": "proof_fixture_link_00001"})
    assert linked.status_code == 202
    linked_id = (await finish(linked.json()["operation_id"]))["binding_id"]
    assert linked_id != owned_id and len(native.create_attempts) == 1

    deletion_attempts = []

    def delete_space(*, agentSpaceId):
        deletion_attempts.append(agentSpaceId)
        native.spaces.pop("adp-" + created.json()["operation_id"])
        return {}

    native.delete_agent_space = delete_space

    async def request_cleanup(binding_id, key, *, confirmed=True, etag=True):
        binding = await client.get(path + "/bindings/" + binding_id)
        headers = {"Idempotency-Key": key}
        if etag:
            headers["If-Match"] = binding.headers["ETag"]
        body = {"action": "cleanup", "configuration_revision": 1, "binding_id": binding_id}
        if confirmed:
            body["owned_cleanup_confirmed"] = True
        return await client.post(path + "/binding-operations", json=body, headers=headers)

    assert (await request_cleanup(owned_id, "missing_confirmation_0001", confirmed=False)).status_code == 422
    assert (await request_cleanup(owned_id, "missing_etag_0000000001", etag=False)).status_code == 428
    assert (await request_cleanup(linked_id, "linked_not_owned_0000001")).status_code == 403
    assert "customer" in native.spaces and deletion_attempts == []

    async with scenario["sessions"]() as reader:
        owned = await reader.get(Binding, ("tenant-one", owned_id))
        receipt = owned.creation_receipt
        owned.creation_receipt = None
        await reader.commit()
    assert (await request_cleanup(owned_id, "missing_receipt_00000001")).status_code == 403
    async with scenario["sessions"]() as reader:
        owned = await reader.get(Binding, ("tenant-one", owned_id))
        owned.creation_receipt = {**receipt, "operation_id": "not-the-original-operation"}
        await reader.commit()
    assert (await request_cleanup(owned_id, "forged_receipt_00000001")).status_code == 403
    async with scenario["sessions"]() as reader:
        owned = await reader.get(Binding, ("tenant-one", owned_id))
        owned.creation_receipt = receipt
        await reader.commit()
    assert deletion_attempts == []

    original_batch_get = native.batch_get_agent_spaces

    def incomplete_readback(*, agentSpaceIds):
        response = original_batch_get(agentSpaceIds=agentSpaceIds)
        if readback_issue == "missing_not_found" and agentSpaceIds == ["as-first"] and deletion_attempts:
            response["notFound"] = []
        return response

    native.batch_get_agent_spaces = incomplete_readback
    original_list = native.list_agent_spaces

    def stale_list(**parameters):
        if readback_issue == "stale_list" and deletion_attempts:
            return {"agentSpaceSummaries": [{"agentSpaceId": "as-first",
                                              "name": "adp-" + created.json()["operation_id"]}]}
        return original_list(**parameters)

    native.list_agent_spaces = stale_list
    requested = await request_cleanup(owned_id, "absence_unknown_00000001")
    assert requested.status_code == 202
    operation_id = requested.json()["operation_id"]
    failed = await finish(operation_id)
    assert failed["state"] == "failed" and failed["gaps"] == ["native_absence_unverified"]
    assert deletion_attempts == ["as-first"] and "customer" in native.spaces
    assert "adp-" + created.json()["operation_id"] not in native.spaces
    async with scenario["sessions"]() as reader:
        work = await reader.scalar(select(SetupWork).where(SetupWork.operation_id == operation_id))
        intent = await reader.get(SetupIntent, ("tenant-one", operation_id))
        operation = await reader.get(BindingOperation, ("tenant-one", operation_id))
        binding = await reader.get(Binding, ("tenant-one", owned_id))
        assert work.blocked_at is not None and work.last_error == "native_absence_unverified"
        assert operation.attempts == 1 and intent.owned_resource_receipt == receipt
        assert binding.state == "cleanup_failed" and binding.generation == 2
    replay = await client.post(path + "/binding-operations", json={"action": "cleanup", "configuration_revision": 1,
                                "binding_id": owned_id, "owned_cleanup_confirmed": True},
                               headers={"Idempotency-Key": "absence_unknown_00000001", "If-Match": '"1"'})
    assert replay.status_code == 202 and replay.json() == failed and deletion_attempts == ["as-first"]
    assert (await client.get(path + "/bindings/" + linked_id)).json()["state"] == "blocked"


@pytest.mark.asyncio
async def test_uncertain_create_never_retries(uncertain_setup_scenario):
    scenario = uncertain_setup_scenario
    scenario["native"].visible = False
    client, path = scenario["client"], scenario["path"]
    body = {"action": "create", "configuration_revision": 1}
    headers = {"Idempotency-Key": "uncertain_create_000001"}
    started = await client.post(path + "/binding-operations", json=body, headers=headers)
    operation_id = started.json()["operation_id"]
    async with scenario["sessions"]() as reader:
        work = await reader.scalar(select(SetupWork).where(SetupWork.operation_id == operation_id))
        envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": 1}
    worker = ExecutionService(scenario["sessions"], None, None, None, None,
                              binding_authorize=scenario["authorize"])
    assert await worker.execute(envelope)
    restarted = ExecutionService(scenario["sessions"], None, None, None, None,
                                 binding_authorize=scenario["authorize"],
                                 clock=lambda: datetime.now(UTC) + timedelta(minutes=6))
    assert await restarted.execute(envelope)
    unknown = await client.get(path + "/binding-operations/" + operation_id)
    assert unknown.json()["state"] == "outcome_unknown" and "binding_id" not in unknown.json()
    assert (await client.post(path + "/binding-operations", json=body, headers=headers)).json() == unknown.json()
    assert len(scenario["native"].create_attempts) == 1
    scenario["native"].visible = True
    for space in scenario["native"].spaces.values():
        space["description"] = "not the original creation receipt"
    async with scenario["sessions"]() as reader:
        await reader.execute(update(SetupWork).where(SetupWork.operation_id == operation_id).values(
            lease_until=datetime.now(UTC) - timedelta(seconds=1)))
        await reader.commit()
    reconcile_path = path + "/binding-operations/" + operation_id + "/reconcile"
    reconciling = await client.post(reconcile_path, json={"reason": "Check original setup"},
                                    headers={"Idempotency-Key": "explicit_reconcile_000001"})
    assert reconciling.status_code == 202 and reconciling.json()["state"] == "waiting"
    replay = await client.post(reconcile_path, json={"reason": "Check original setup"},
                               headers={"Idempotency-Key": "explicit_reconcile_000001"})
    assert replay.status_code == 202 and replay.json() == reconciling.json()
    async with scenario["sessions"]() as reader:
        next_work = await reader.scalar(select(SetupWork).where(
            SetupWork.operation_id == operation_id, SetupWork.generation == 2))
        assert next_work is not None
    assert await worker.execute({"schema_version": "1.0", "work_id": next_work.work_id, "generation": 2})
    async with scenario["sessions"]() as reader:
        operation = await reader.get(BindingOperation, ("tenant-one", operation_id))
        assert operation.attempts == 1 and operation.reconcile_checks == 2
        assert (await reader.scalars(select(Binding))).all() == []


@pytest.mark.asyncio
async def test_create_readback_loss_retains_native_candidate(uncertain_setup_scenario):
    scenario = uncertain_setup_scenario
    scenario["native"].lost_response = False
    scenario["native"].visible = False
    client, path = scenario["client"], scenario["path"]
    started = await client.post(path + "/binding-operations", json={
        "action": "create", "configuration_revision": 1},
        headers={"Idempotency-Key": "readback_lost_create_00001"})
    assert started.status_code == 202
    async with scenario["sessions"]() as reader:
        work = await reader.scalar(select(SetupWork).where(
            SetupWork.operation_id == started.json()["operation_id"]))
        envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": 1}
    worker = ExecutionService(scenario["sessions"], None, None, None, None,
                              binding_authorize=scenario["authorize"])
    assert await worker.execute(envelope)
    async with scenario["sessions"]() as reader:
        operation = await reader.get(BindingOperation, ("tenant-one", started.json()["operation_id"]))
        assert operation.state == "outcome_unknown" and operation.native_candidate_id == "as-first"
    scenario["native"].visible = True
    restarted = ExecutionService(scenario["sessions"], None, None, None, None,
                                 binding_authorize=scenario["authorize"],
                                 clock=lambda: datetime.now(UTC) + timedelta(minutes=6))
    assert await restarted.execute(envelope)
    assert (await client.get(path + "/binding-operations/" + started.json()["operation_id"])).json()["state"] == "succeeded"
    assert len(scenario["native"].create_attempts) == 1
    assert scenario["native"].read_attempts[0][0] == "batch"


@pytest.mark.asyncio
async def test_ambiguous_native_match_never_retries(uncertain_setup_scenario):
    scenario = uncertain_setup_scenario
    client, path = scenario["client"], scenario["path"]
    body = {"action": "create", "configuration_revision": 1}
    headers = {"Idempotency-Key": "ambiguous_native_00000001"}
    started = await client.post(path + "/binding-operations", json=body, headers=headers)
    assert started.status_code == 202
    async with scenario["sessions"]() as reader:
        work = await reader.scalar(select(SetupWork).where(
            SetupWork.operation_id == started.json()["operation_id"]))
        envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": 1}
    worker = ExecutionService(scenario["sessions"], None, None, None, None,
                              binding_authorize=scenario["authorize"])
    assert await worker.execute(envelope)

    def ambiguous_list(**parameters):
        space = next(iter(scenario["native"].spaces.values()))
        return {"agentSpaceSummaries": [
            {"name": space["name"], "agentSpaceId": space["agentSpaceId"]},
            {"name": space["name"], "agentSpaceId": "as-another"},
        ]}

    scenario["native"].list_agent_spaces = ambiguous_list
    restarted = ExecutionService(scenario["sessions"], None, None, None, None,
                                 binding_authorize=scenario["authorize"],
                                 clock=lambda: datetime.now(UTC) + timedelta(minutes=6))
    assert await restarted.execute(envelope)
    unknown = await client.get(path + "/binding-operations/" + started.json()["operation_id"])
    assert unknown.json()["state"] == "outcome_unknown"
    assert len(scenario["native"].create_attempts) == 1
    async with scenario["sessions"]() as reader:
        assert (await reader.scalars(select(Binding))).all() == []


@pytest.mark.asyncio
async def test_link_and_replay(tmp_path, monkeypatch):
    from l5_services.api import bindings as binding_api

    fixed_time = datetime(2026, 1, 1, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_time if tz is not None else fixed_time.replace(tzinfo=None)

    monkeypatch.setattr(binding_api, "datetime", FrozenDateTime)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bindings.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        for tenant_id in ("tenant-one", "tenant-two"):
            for product_id in ("product-one", "product-two"):
                configuration_id = "config-one" if (tenant_id, product_id) == ("tenant-one", "product-one") else f"{tenant_id}-{product_id}"
                session.add(Configuration(tenant_id=tenant_id, configuration_id=configuration_id,
                                          product_id=product_id, project_id="project-one", service_id="security",
                                          relationship_revision="1", owner_id="owner-one" if tenant_id == "tenant-one" else "owner-two",
                                          current_revision=1, handoff_state="linked"))
        await session.commit()

    connection_ref = {"tenant_id": "tenant-one", "project_id": "project-one",
                      "source_connection_id": "connection-one", "record_type": "connection",
                      "record_id": "connection-one", "record_revision": "1"}
    verified = {"configuration_id": "config-one", "configuration_revision": 1,
                "scope": {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one",
                          "relationship_revision": "1", "owner_id": "owner-one", "visibility": "only_me"},
                "connection_ref": connection_ref, "account_id": "123456789012", "region": "us-east-1",
                "role_arn": "arn:aws:iam::123456789012:role/example",
                "authorization": {"actor_id": "owner-one", "delegation_id": "delegation-one",
                                  "delegation_revision": 1, "authorized_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat()}}
    profiles = {("tenant-one", "product-one"): verified}
    for tenant_id in ("tenant-one", "tenant-two"):
        for product_id in ("product-one", "product-two"):
            if (tenant_id, product_id) == ("tenant-one", "product-one"):
                continue
            profiles[(tenant_id, product_id)] = {
                **verified, "configuration_id": f"{tenant_id}-{product_id}",
                "scope": {**verified["scope"], "tenant_id": tenant_id, "product_id": product_id,
                          "owner_id": "owner-one" if tenant_id == "tenant-one" else "owner-two"},
                "connection_ref": {**connection_ref, "tenant_id": tenant_id,
                                   "source_connection_id": "connection-one" if tenant_id == "tenant-one" else "connection-two",
                                   "record_id": "connection-one" if tenant_id == "tenant-one" else "connection-two"},
                "account_id": verified["account_id"] if tenant_id == "tenant-one" else "210987654321",
                "role_arn": verified["role_arn"] if tenant_id == "tenant-one" else
                            "arn:aws:iam::210987654321:role/other-example",
                "authorization": {**verified["authorization"],
                                  "actor_id": "owner-one" if tenant_id == "tenant-one" else "owner-two",
                                  "delegation_id": "delegation-one" if tenant_id == "tenant-one" else "delegation-two"},
            }
    active_tenant = {"id": "tenant-one"}

    class Provider:
        def __init__(self):
            self.reads = []
            self.creates = []
            self.spaces = {}
            self.read_fault = ("tenant-one", "product-two", "space-one")

        async def read(self, reference, authority):
            self.reads.append((reference, authority["scope"]))
            if self.read_fault == (reference["tenant_id"], authority["scope"]["product_id"],
                                   reference["record_id"]):
                self.read_fault = None
                raise TimeoutError("native read response lost")
            native = self.spaces.get((reference["tenant_id"], reference["source_connection_id"],
                                      reference["record_id"]))
            if native is None or native["provider_identity"]["account_id"] != authority["account_id"]:
                raise ValueError("native_scope_mismatch")
            return native

    provider = Provider()
    for (tenant_id, _product_id), profile in profiles.items():
        for space in ("space-one", "space-two"):
            native_ref = {**profile["connection_ref"], "record_type": "security_space",
                          "record_id": space, "record_revision": "1"}
            provider.spaces[(tenant_id, native_ref["source_connection_id"], space)] = {
                "native_resource_ref": native_ref,
                "provider_identity": {"account_id": profile["account_id"], "region": profile["region"],
                                      "role_arn": profile["role_arn"],
                                      "verified_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat()},
                "state": "blocked", "gaps": ["source_unavailable"], "readiness": []}

    class Backend:
        def __init__(self):
            self.session = None

        def store(self, product_id, project_id, service_id):
            if ((active_tenant["id"], product_id) not in profiles or project_id != "project-one"
                    or service_id != "security"):
                raise HTTPException(404, detail={"code": "not_found"})
            return BindingStore(self.session, active_tenant["id"], product_id, project_id, service_id)

        def authority(self, product_id, project_id, service_id):
            self.store(product_id, project_id, service_id)
            return profiles[(active_tenant["id"], product_id)]

        async def start_binding(self, product_id, project_id, service_id, request, key, if_match=None):
            store = self.store(product_id, project_id, service_id)
            selected = self.authority(product_id, project_id, service_id)
            if request.get("action") in {"check", "update", "suspend", "resume", "unlink", "cleanup"}:
                return await store.control(request, key, if_match, selected)
            return await store.start(request, key, selected)

        async def list_bindings(self, product_id, project_id, service_id):
            return await self.store(product_id, project_id, service_id).list_bindings()

        async def get_binding(self, product_id, project_id, service_id, binding_id):
            return await self.store(product_id, project_id, service_id).binding(binding_id)

        async def get_binding_operation(self, product_id, project_id, service_id, operation_id):
            return await self.store(product_id, project_id, service_id).operation(operation_id)

    backend = Backend()
    app = FastAPI()
    app.include_router(create_router(lambda: backend))
    path = "/v1/products/product-one/projects/project-one/services/security"

    async def authorize(intent, claim):
        selected = profiles[(intent.tenant_id, intent.product_id)]
        assert intent.scope == selected["scope"] and intent.request["action"] == "link"
        assert intent.request["native_resource_ref"]["tenant_id"] == intent.tenant_id
        return selected, provider

    worker = ExecutionService(sessions, None, None, None, None,
                              binding_authorize=authorize, clock=lambda: fixed_time)

    async with sessions() as session, AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        backend.session = session
        for space in ("space-one", "space-two"):
            reference = {"tenant_id": "tenant-one", "project_id": "project-one",
                         "source_connection_id": "connection-one", "record_type": "security_space",
                         "record_id": space, "record_revision": "1"}
            body = {"action": "link", "configuration_revision": 1, "native_resource_ref": reference}
            headers = {"Idempotency-Key": f"link_{space}_0000000001"}
            before_reads = len(provider.reads)
            original = await client.post(path + "/binding-operations", json=body, headers=headers)
            assert original.status_code == 202, original.text
            assert original.json()["state"] == "accepted" and len(provider.reads) == before_reads
            replay = await client.post(path + "/binding-operations", json=body, headers=headers)
            assert replay.status_code == 202 and replay.json() == original.json()
            work = await session.scalar(select(SetupWork).where(
                SetupWork.operation_id == original.json()["operation_id"]))
            assert await worker.execute({"schema_version": "1.0", "work_id": work.work_id,
                                         "generation": work.generation})
            session.expire_all()
            completed = await client.get(path + "/binding-operations/" + original.json()["operation_id"])
            assert completed.status_code == 200 and completed.json()["state"] == "succeeded"
            original = completed
            binding = await client.get(path + "/bindings/" + original.json()["binding_id"])
            assert binding.status_code == 200
            assert binding.json()["native_resource_ref"] == reference
            assert binding.json()["state"] == "blocked" and binding.json()["gaps"] == ["source_unavailable"]
            assert binding.json()["ownership"] == "customer_linked"
        listed = await client.get(path + "/bindings")
        assert listed.status_code == 200 and len(listed.json()["items"]) == 2
        assert listed.headers["ETag"]
        assert len(provider.reads) == 2 and provider.creates == []
        intents = (await session.scalars(select(SetupIntent))).all()
        bindings = (await session.scalars(select(Binding))).all()
        operations = (await session.scalars(select(BindingOperation))).all()
        work = (await session.scalars(select(SetupWork))).all()
        assert len(intents) == len(bindings) == len(operations) == 2
        assert len(work) == 2 and all(item.completed_at is not None for item in work)
        assert {intent.request["native_resource_ref"]["record_id"] for intent in intents} == {"space-one", "space-two"}
        assert all(intent.binding_generation == 1 and intent.authorization["delegation_revision"] == 1 for intent in intents)
        assert all(binding.revision == binding.generation == 1 for binding in bindings)
        assert all(operation.attempts == 1 for operation in operations)
        replay = await client.post(path + "/binding-operations", json={
            "action": "link", "configuration_revision": 1,
            "native_resource_ref": {"tenant_id": "tenant-one", "project_id": "project-one",
                                    "source_connection_id": "connection-one", "record_type": "security_space",
                                    "record_id": "space-one", "record_revision": "2"}},
            headers={"Idempotency-Key": "link_space-one_0000000001"})
        assert replay.status_code == 409
        assert len(provider.reads) == 2
        foreign = await client.post(path + "/binding-operations", json={
            "action": "link", "configuration_revision": 1,
            "native_resource_ref": {"tenant_id": "tenant-other", "project_id": "project-one",
                                    "source_connection_id": "connection-one", "record_type": "security_space",
                                    "record_id": "space-one", "record_revision": "1"}},
            headers={"Idempotency-Key": "link_foreign_space_00000001"})
        assert foreign.status_code == 404 and len(provider.reads) == 2
        await session.execute(update(SetupIntent).where(
            SetupIntent.operation_id == intents[0].operation_id).values(request={"action": "create"}))
        await session.commit()
        changed = await client.get(path + "/binding-operations/" + intents[0].operation_id)
        assert changed.status_code == 409 and changed.json()["detail"]["code"] == "setup_intent_changed"

        isolated_bindings = {}
        for tenant_id, product_id in (("tenant-one", "product-two"),
                                      ("tenant-two", "product-one"), ("tenant-two", "product-two")):
            active_tenant["id"] = tenant_id
            selected = profiles[(tenant_id, product_id)]
            scoped_path = f"/v1/products/{product_id}/projects/project-one/services/security"
            empty = await client.get(scoped_path + "/bindings")
            assert empty.status_code == 200 and empty.json()["items"] == []
            for space in ("space-one", "space-two"):
                native = provider.spaces[(tenant_id, selected["connection_ref"]["source_connection_id"], space)]
                request = {"action": "link", "configuration_revision": 1,
                           "native_resource_ref": native["native_resource_ref"]}
                headers = {"Idempotency-Key": f"link_{space}_0000000001"}
                reads_before = len(provider.reads)
                admitted = await client.post(scoped_path + "/binding-operations", json=request, headers=headers)
                assert admitted.status_code == 202 and admitted.json()["state"] == "accepted", admitted.text
                assert (await client.post(scoped_path + "/binding-operations", json=request,
                                          headers=headers)).json() == admitted.json()
                assert len(provider.reads) == reads_before
                operation_id = admitted.json()["operation_id"]
                async with sessions() as reader:
                    pending = await reader.get(SetupIntent, (tenant_id, operation_id))
                    work = await reader.scalar(select(SetupWork).where(
                        SetupWork.tenant_id == tenant_id, SetupWork.operation_id == operation_id))
                    assert pending.request == request and pending.binding_generation == 1
                    assert pending.authorization["delegation_id"] == selected["authorization"]["delegation_id"]
                    assert work.due_at == fixed_time.replace(tzinfo=None)
                    envelope = {"schema_version": "1.0", "work_id": work.work_id,
                                "generation": work.generation}
                assert await worker.execute(envelope)
                faulted = (tenant_id, product_id, space) == ("tenant-one", "product-two", "space-one")
                if faulted:
                    waiting = await client.get(scoped_path + "/binding-operations/" + operation_id)
                    assert waiting.json()["state"] == "waiting"
                    assert waiting.json()["gaps"] == ["native_readback_unavailable"]
                    async with sessions() as reader:
                        assert (await reader.scalars(select(Binding).where(
                            Binding.tenant_id == tenant_id, Binding.configuration_id == selected["configuration_id"]))).all() == []
                    assert not await worker.execute(envelope)
                    restarted = ExecutionService(sessions, None, None, None, None,
                                                 binding_authorize=authorize,
                                                 clock=lambda: fixed_time + timedelta(minutes=6))
                    assert await restarted.execute(envelope)
                completed = await client.get(scoped_path + "/binding-operations/" + operation_id)
                assert completed.status_code == 200 and completed.json()["state"] == "succeeded"
                assert completed.json()["gaps"] == []
                binding_id = completed.json()["binding_id"]
                isolated_bindings[(tenant_id, product_id, space)] = binding_id
                scoped_binding = await client.get(scoped_path + "/bindings/" + binding_id)
                assert scoped_binding.json()["native_resource_ref"] == native["native_resource_ref"]
                assert scoped_binding.json()["gaps"] == native["gaps"] and scoped_binding.json()["readiness"] == []
                async with sessions() as reader:
                    persisted = await reader.get(Binding, (tenant_id, binding_id))
                    outcome = await reader.get(BindingOperation, (tenant_id, operation_id))
                    saved_work = await reader.get(SetupWork, (tenant_id, work.work_id))
                    assert persisted.configuration_id == selected["configuration_id"]
                    assert persisted.provider_identity == native["provider_identity"]
                    assert persisted.scope == selected["scope"] and persisted.generation == 1
                    assert persisted.ownership == "customer_linked" and persisted.creation_receipt is None
                    assert outcome.attempts == (2 if faulted else 1) and saved_work.completed_at is not None
                assert len(provider.reads) == reads_before + (2 if faulted else 1)
                assert provider.reads[-1] == (native["native_resource_ref"], selected["scope"])
            listing = await client.get(scoped_path + "/bindings")
            assert {item["binding_id"] for item in listing.json()["items"]} == {
                isolated_bindings[(tenant_id, product_id, space)] for space in ("space-one", "space-two")}

        active_tenant["id"] = "tenant-one"
        wrong_product = await client.get(path + "/bindings/" + isolated_bindings[("tenant-one", "product-two", "space-one")])
        assert wrong_product.status_code == 404
        active_tenant["id"] = "tenant-two"
        assert (await client.get(path + "/bindings/" + bindings[0].binding_id)).status_code == 404
        assert (await client.get(path + "/binding-operations/" + operations[1].operation_id)).status_code == 404
        cross_tenant = await client.post(path + "/binding-operations", json={
            "action": "link", "configuration_revision": 1,
            "native_resource_ref": provider.spaces[("tenant-one", "connection-one", "space-one")]["native_resource_ref"]},
            headers={"Idempotency-Key": "foreign_tenant_link_00001"})
        assert cross_tenant.status_code == 404 and len(provider.reads) == 9
        assert len((await session.scalars(select(BindingOperation))).all()) == 8
        assert provider.creates == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_gateway_binding_dispatch(gateway_configuration_scenario, app, monkeypatch, request):
    from datetime import timedelta

    from l5_services.dispatcher import repair_owed_work
    from l5_services.models.configuration import ConfigurationVersion
    from src import l5_integration
    from src.auth.aws_connection_authority import connection_binding
    from src.products.models import ProductProjectBinding
    from src.shared.models.vault import CredentialServiceDelegation, UserCredential

    scenario = gateway_configuration_scenario
    actor = scenario["actors"][0]
    product = await scenario["prepare"](actor, "binding-dispatch", "internal", ["code_review"])
    scenario["switch"](actor)
    delivered = []
    fault = {"send": True}

    async def publish(envelope, delay):
        if fault["send"]:
            raise ConnectionError("queue unavailable")
        delivered.append((envelope, delay))

    async with scenario["sessions"]() as session:
        credential = UserCredential(
            org_id=actor.org_id, user_id=actor.user_id, agent_consent=True,
            service="aws", credential_type="aws_role", label="binding-test",
            secret_arn="fictional-secret", aws_external_id="fictional-external",
            aws_verified_at=datetime.now(UTC), aws_verification_attempt="verification-one",
            aws_verified_version_id="version-one",
            scopes={"account_id": "123456789012",
                    "role_arn": "arn:aws:iam::123456789012:role/FictionalRole",
                    "source": "imported_role", "status": "verified"},
        )
        session.add(credential)
        await session.flush()
        credential.aws_verified_binding = connection_binding(credential)
        reference = {"tenant_id": actor.org_id, "project_id": product["project_id"],
                     "source_connection_id": credential.id, "record_type": "connection",
                     "record_id": credential.id, "record_revision": "version-one"}
        relation = await session.scalar(select(ProductProjectBinding).where(
            ProductProjectBinding.product_id == product["product_id"],
            ProductProjectBinding.project_id == product["project_id"]))
        session.add(Configuration(tenant_id=actor.org_id, configuration_id="config-binding-test",
                                  product_id=product["product_id"], project_id=product["project_id"],
                                  service_id="security", relationship_revision=str(relation.relationship_revision),
                                  owner_id=actor.user_id, current_revision=1, handoff_state="linked"))
        session.add(ConfigurationVersion(tenant_id=actor.org_id, configuration_id="config-binding-test",
                                         revision=1, settings={"visibility": "only_me", "source_refs": [],
                                                               "connection_ref": reference, "region": "us-east-1"},
                                         inherited_fields=[], gaps=[], state="disabled", hash="f" * 64))
        grant = CredentialServiceDelegation(tenant_id=actor.org_id, credential_id=credential.id,
                                            service_id="l5-worker", product_id=product["product_id"],
                                            project_id=product["project_id"], created_by=actor.user_id,
                                            operations=["product:read", "credential:session"],
                                            sources=[{**reference, "record_type": "credential"}],
                                            capabilities=[], visibility="restricted",
                                            expires_at=datetime.now(UTC) + timedelta(hours=1))
        session.add(grant)
        await session.commit()
        scenario["permissions"][(actor.org_id, product["project_id"])].add((credential.id, credential.id))

        async def selected_connections(db, current_actor):
            return [({"connection_ref": {"kind": "credential", "id": credential.id},
                      "record_ref": reference}, None)]

        monkeypatch.setattr(l5_integration, "authorized_connections", selected_connections)
        gateway = l5_integration.GatewayConfigurationBackend(session, actor, publish=publish)
        app.dependency_overrides[l5_integration.backend] = lambda: gateway
        path = product["path"].removesuffix("/configuration")
        body = {"action": "link", "configuration_revision": 1,
                "native_resource_ref": {**reference, "record_type": "security_space",
                                        "record_id": "space-one"}}
        try:
            route = next(route for route in app.routes if route.path.endswith("/services/{service_id}/binding-operations"))
            assert any(dependency.call is l5_integration.backend for dependency in route.dependant.dependencies)
            assert (await gateway.current(product["product_id"], product["project_id"], "security"))[0].configuration_id == "config-binding-test"
            response = await scenario["client"].post(path + "/binding-operations", json=body,
                headers={"Idempotency-Key": "gateway_link_test_00000001"})
            assert response.status_code == 202, response.text
            assert response.json()["state"] == "accepted" and delivered == []
            async with scenario["sessions"]() as reader:
                work = await reader.scalar(select(SetupWork).where(
                    SetupWork.operation_id == response.json()["operation_id"]))
                assert work is not None and work.attempts == 1 and work.last_error == "publication_failed", (
                    work.attempts, work.last_error, work.dispatch_owner, work.due_at)
                intent = await reader.get(SetupIntent, (actor.org_id, response.json()["operation_id"]))
                assert intent.request == body and intent.authorization["delegation_id"] == grant.id
            fault["send"] = False
            assert await repair_owed_work(scenario["sessions"], publish, work_model=SetupWork) == 1
            assert delivered == [({"schema_version": "1.0", "work_id": work.work_id, "generation": 1}, 0)]
            replay = await scenario["client"].post(path + "/binding-operations", json=body,
                headers={"Idempotency-Key": "gateway_link_test_00000001"})
            assert replay.status_code == 202 and replay.json() == response.json()
            created = await scenario["client"].post(path + "/binding-operations", json={
                "action": "create", "configuration_revision": 1},
                headers={"Idempotency-Key": "gateway_create_test_00001"})
            assert created.status_code == 202 and created.json()["state"] == "accepted"
            async with scenario["sessions"]() as reader:
                create_intent = await reader.get(SetupIntent, (actor.org_id, created.json()["operation_id"]))
                assert create_intent.request == {"action": "create", "configuration_revision": 1}
            verified = (await gateway.binding_authority(product["product_id"], product["project_id"], "security"))[1]
            binding = Binding(tenant_id=actor.org_id, binding_id="scoped-control-binding",
                              configuration_id="config-binding-test", product_id=product["product_id"],
                              project_id=product["project_id"], service_id="security", scope=verified["scope"],
                              revision=1, generation=1, state="blocked", ownership="customer_linked",
                              connection_ref=reference, connection_key="a" * 64,
                              native_resource_ref=body["native_resource_ref"], native_key="b" * 64,
                              provider_identity={"account_id": verified["account_id"], "role_arn": verified["role_arn"],
                                                 "region": verified["region"], "verified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")},
                              readiness=[], gaps=["source_readiness_unverified"])
            session.add(binding)
            await session.commit()
            binding_path = path + "/bindings/" + binding.binding_id
            etag = (await scenario["client"].get(binding_path)).headers["ETag"]
            control = {"action": "check", "configuration_revision": 1, "binding_id": binding.binding_id}
            headers = {"If-Match": etag, "Idempotency-Key": "gateway_control_check_0001"}

            foreign_product_path = path.replace(f'/products/{product["product_id"]}/',
                                                "/products/different-product/")
            foreign_read = await scenario["client"].get(foreign_product_path + "/bindings/" + binding.binding_id)
            foreign_write = await scenario["client"].post(foreign_product_path + "/binding-operations",
                                                           json=control, headers=headers)
            assert foreign_read.status_code == foreign_write.status_code == 404
            other_actor = scenario["actors"][1]
            scenario["switch"](other_actor)
            app.dependency_overrides[l5_integration.backend] = lambda: l5_integration.GatewayConfigurationBackend(
                session, other_actor, publish=publish)
            tenant_read = await scenario["client"].get(binding_path)
            tenant_write = await scenario["client"].post(path + "/binding-operations", json=control, headers=headers)
            assert tenant_read.status_code == tenant_write.status_code == 404
            scenario["switch"](actor)
            app.dependency_overrides[l5_integration.backend] = lambda: gateway

            from src.internal import tools_l5

            monkeypatch.setattr(tools_l5, "files", lambda package: Path(__file__).resolve().parents[4] / "docs/l5-services")
            tools_l5.l5_tool_catalogue.cache_clear()
            tools_l5.l5_operations.cache_clear()
            request.addfinalizer(tools_l5.l5_tool_catalogue.cache_clear)
            request.addfinalizer(tools_l5.l5_operations.cache_clear)
            operations = {item["name"]: item["outputSchema"] for item in tools_l5.available_l5_catalogue()["tools"]}
            scope = {"product_id": product["product_id"], "project_id": product["project_id"],
                     "service_id": "security"}
            read = await tools_l5.dispatch_l5("l5___get_binding", {**scope, "binding_id": binding.binding_id}, gateway)
            assert read["structuredContent"]["etag"] == etag
            assert json.loads(read["content"][0]["text"]) == read["structuredContent"]
            Draft202012Validator(operations["l5.get_binding"]).validate(read["structuredContent"])
            required = await tools_l5.dispatch_l5("l5___start_binding_operation", {
                **scope, "input": control, "idempotency_key": "binding_missing_etag_00001",
            }, gateway)
            assert required["structuredContent"]["http_status"] == 428
            assert required["structuredContent"]["error"]["code"] == "precondition_required"
            tool_admitted = await tools_l5.dispatch_l5("l5___start_binding_operation", {
                **scope, "input": control, "idempotency_key": "gateway_control_check_0001", "if_match": etag,
            }, gateway)
            assert json.loads(tool_admitted["content"][0]["text"]) == tool_admitted["structuredContent"]
            Draft202012Validator(operations["l5.start_binding_operation"]).validate(tool_admitted["structuredContent"])
            admitted = await scenario["client"].post(path + "/binding-operations", json=control, headers=headers)
            assert admitted.json() == tool_admitted["structuredContent"]["data"]
            stale = await tools_l5.dispatch_l5("l5___start_binding_operation", {
                **scope, "input": control, "idempotency_key": "gateway_stale_check_0001", "if_match": etag,
            }, gateway)
            direct_stale = await scenario["client"].post(path + "/binding-operations", json=control, headers={
                "If-Match": etag, "Idempotency-Key": "gateway_direct_stale_00001",
            })
            assert direct_stale.status_code == 412
            assert stale["structuredContent"]["http_status"] == 412
            assert stale["structuredContent"]["error"]["code"] == direct_stale.json()["detail"]["code"]
            async with scenario["sessions"]() as independent:
                stored = await independent.get(Binding, (actor.org_id, binding.binding_id))
                assert stored.revision == 2 and stored.generation == 2
            assert admitted.status_code == 202, admitted.text
            assert admitted.json()["state"] == "accepted"
            async with scenario["sessions"]() as reader:
                control_work = await reader.scalar(select(SetupWork).where(
                    SetupWork.operation_id == admitted.json()["operation_id"]))
                envelope = {"schema_version": "1.0", "work_id": control_work.work_id,
                            "generation": control_work.generation}
            before_revoke = await scenario["client"].get(binding_path)
            assert before_revoke.json()["generation"] == 2
            credential.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await session.commit()
            expired = await scenario["client"].post(path + "/binding-operations", json=control,
                headers={"If-Match": before_revoke.headers["ETag"],
                         "Idempotency-Key": "expired_connection_00001"})
            assert expired.status_code == 503 and expired.json()["detail"]["code"] == "selected_connection_unavailable"
            credential.expires_at = None
            await session.commit()
            grant.revoked_at = datetime.now(UTC)
            await session.commit()
            denied = await scenario["client"].post(path + "/binding-operations", json=body,
                headers={"Idempotency-Key": "gateway_link_other_00000001"})
            assert denied.status_code == 403
            for index, action in enumerate(("check", "update", "suspend", "resume", "unlink", "cleanup")):
                attempt = await scenario["client"].post(path + "/binding-operations", json={
                    "action": action, "configuration_revision": 1, "binding_id": binding.binding_id,
                    **({"owned_cleanup_confirmed": True} if action == "cleanup" else {}),
                }, headers={"If-Match": before_revoke.headers["ETag"],
                            "Idempotency-Key": f"revoked_control_{index}_000001"})
                # Owned cleanup now needs a current JWT owner and separate
                # cleanup consent; this legacy fixture has no such session.
                assert attempt.status_code == (404 if action == "cleanup" else 403), (action, attempt.text)

            class ForbiddenProvider:
                async def read(self, *_args):
                    raise AssertionError("revoked worker reached native provider")

            async def authorize(intent, claim):
                async with scenario["sessions"]() as worker_session:
                    worker_gateway = l5_integration.GatewayConfigurationBackend(worker_session, actor)
                    _, authority = await worker_gateway.binding_authority(
                        intent.product_id, intent.project_id, intent.service_id)
                return authority, ForbiddenProvider()

            worker = ExecutionService(scenario["sessions"], None, None, None, None,
                                      binding_authorize=authorize)
            assert not await worker.execute(envelope)
            assert not await worker.execute(envelope)
            async with scenario["sessions"]() as reader:
                blocked = await reader.get(SetupWork, (actor.org_id, control_work.work_id))
                operation = await reader.get(BindingOperation, (actor.org_id, admitted.json()["operation_id"]))
                unchanged = await reader.get(Binding, (actor.org_id, binding.binding_id))
                assert blocked.blocked_at is not None and blocked.last_error == "setup_authority_denied"
                assert operation.state == "accepted" and operation.attempts == 0
                assert unchanged.generation == 2 and unchanged.state == "checking"
                assert len((await reader.scalars(select(BindingOperation).where(
                    BindingOperation.binding_id == binding.binding_id))).all()) == 1
        finally:
            app.dependency_overrides.pop(l5_integration.backend, None)


@pytest.mark.asyncio
async def test_finops_scoped_adoption_requires_current_authority(uncertain_setup_scenario):
    from l5_services.workers.bindings import execute_setup_work

    scenario = uncertain_setup_scenario
    async with scenario["sessions"]() as session:
        session.add(Configuration(tenant_id="tenant-one", configuration_id="config-finops-one",
                                  product_id="product-one", project_id="project-one",
                                  service_id="finops", relationship_revision="1",
                                  owner_id="owner-one", current_revision=1, handoff_state="linked"))
        await session.commit()
        store = BindingStore(session, "tenant-one", "product-one", "project-one", "finops")
        selected = {**scenario["verified"], "configuration_id": "config-finops-one",
                    "finops": {"billing_scope": {"account_ids": ["123456789012"],
                                                 "view_ref": "view-example"},
                               "data_role_ref": "role-example", "agent_mode": "adopt",
                               "native_agent_ref": "agent-example"}}
        adopted_ref = {**selected["connection_ref"], "record_type": "finops_agent",
                       "record_id": "agent-example", "record_revision": "revision-one"}
        request = {"action": "link", "configuration_revision": 1,
                   "native_resource_ref": adopted_ref}
        operation = await store.start(request, "finops_adopt_intent_0001", selected)
        replay = await store.start(request, "finops_adopt_intent_0001", selected)
        assert operation.operation_id == replay.operation_id
        assert operation.state == "accepted" and operation.gaps == []
        intent = await session.get(SetupIntent, ("tenant-one", operation.operation_id))
        assert intent.scope == selected["scope"]
        assert intent.request == request
        assert intent.authorization == selected["authorization"]
        work_id = await session.scalar(select(SetupWork.work_id).where(
            SetupWork.operation_id == operation.operation_id))
        assert work_id is not None
        create_selected = {**selected, "finops": {
            **selected["finops"], "agent_mode": "create"}}
        create_selected["finops"].pop("native_agent_ref")
        created = await store.start({"action": "create", "configuration_revision": 1},
                                    "finops_create_intent_0001", create_selected)
        assert created.action == "create" and created.state == "blocked" and created.gaps == ["native_unqualified"]
        create_work_id = await session.scalar(select(SetupWork.work_id).where(
            SetupWork.operation_id == created.operation_id))
        for invalid_request in (
            {"action": "link", "configuration_revision": 1},
            {"action": "link", "configuration_revision": 1,
             "native_resource_ref": {**adopted_ref, "record_id": "foreign-agent"}},
            {"action": "create", "configuration_revision": 1},
        ):
            with pytest.raises(HTTPException) as exc:
                await store.start(invalid_request, "finops_invalid_intent_0001", selected)
            assert exc.value.status_code in {404, 409, 422}
        with pytest.raises(HTTPException) as exc:
            await store.start(request, "finops_foreign_scope_0001", {
                **selected, "scope": {**selected["scope"], "project_id": "other-project"}})
        assert exc.value.status_code == 404
    authority_calls = []

    async def denied_authorize(intent, claim):
        authority_calls.append(intent.operation_id)
        raise HTTPException(403, detail={"code": "delegation_denied"})

    assert await execute_setup_work(scenario["sessions"], {
        "schema_version": "1.0", "work_id": work_id, "generation": 1,
    }, denied_authorize) is False
    assert await execute_setup_work(scenario["sessions"], {
        "schema_version": "1.0", "work_id": work_id, "generation": 1,
    }, denied_authorize) is False
    assert await execute_setup_work(scenario["sessions"], {
        "schema_version": "1.0", "work_id": create_work_id, "generation": 1,
    }, denied_authorize) is False
    async with scenario["sessions"]() as reader:
        work = await reader.get(SetupWork, ("tenant-one", work_id))
        assert work.blocked_at is not None and work.last_error == "setup_authority_denied"
        assert work.completed_at is None
        assert not (await reader.scalars(select(Binding))).all()
    assert authority_calls == [operation.operation_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("service_id", ("security", "devops", "finops", "transform"))
@pytest.mark.parametrize(("method", "suffix", "body", "backend_method", "arguments"), [
    ("POST", "/binding-operations", {"action": "create", "configuration_revision": 1}, "start_binding",
     ({"action": "create", "configuration_revision": 1}, "binding_scope_key_0001", "opaque-etag")),
    ("GET", "/binding-operations/operation-one", None, "get_binding_operation", ("operation-one",)),
    ("POST", "/binding-operations/operation-one/reconcile", {"reason": "recover"}, "reconcile_binding",
     ("operation-one", "recover", "binding_scope_key_0001")),
    ("GET", "/bindings", None, "list_bindings", ()),
    ("GET", "/bindings/binding-one", None, "get_binding", ("binding-one",)),
])
async def test_binding_routes_preserve_supported_service_scope(service_id, method, suffix, body, backend_method, arguments):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    operation = AsyncMock(side_effect=HTTPException(403, detail={"code": "forbidden"}))
    backend = SimpleNamespace(**{backend_method: operation})
    app = FastAPI()
    app.include_router(create_router(lambda: backend))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(
            method, f"/v1/products/product-one/projects/project-one/services/{service_id}{suffix}",
            json=body, headers={"Idempotency-Key": "binding_scope_key_0001", "If-Match": "opaque-etag"},
        )
    assert response.status_code == 403
    assert response.json() == {"detail": {"code": "forbidden"}}
    operation.assert_awaited_once_with("product-one", "project-one", service_id, *arguments)


@pytest.mark.asyncio
async def test_finops_binding_route_exposes_intent_without_security_controls():
    from types import SimpleNamespace

    calls = []

    class Backend:
        async def start_binding(self, product_id, project_id, service_id, body, key, etag):
            calls.append((product_id, project_id, service_id, body, key, etag))
            return SimpleNamespace(operation_id="operation-example", action=body["action"],
                                   state="blocked", revision=1, gaps=["native_unqualified"],
                                   binding_id=None, readback_ref=None, cleanup_receipt=None)

    app = FastAPI()
    app.include_router(create_router(lambda: Backend()))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = "/v1/products/product-one/projects/project-one/services/finops/binding-operations"
        headers = {"Idempotency-Key": "finops_setup_intent_0001"}
        adopted = await client.post(path, json={
            "action": "link", "configuration_revision": 1,
            "native_resource_ref": {"tenant_id": "tenant-one", "project_id": "project-one",
                                    "source_connection_id": "connection-one",
                                    "record_type": "finops_agent", "record_id": "agent-example",
                                    "record_revision": "revision-one"}},
                                    headers=headers)
        assert adopted.status_code == 202 and adopted.json()["gaps"] == ["native_unqualified"]
        assert (await client.post(path, json={"action": "cleanup", "binding_id": "foreign"},
                                  headers=headers)).status_code == 422
        assert len(calls) == 1
        assert calls[0][0:3] == ("product-one", "project-one", "finops")

@pytest.mark.asyncio
async def test_finops_binding_api_masks_foreign_scopes_and_view_overrides(uncertain_setup_scenario):
    import json

    scenario = uncertain_setup_scenario
    session_factory = scenario["sessions"]
    actor = {"tenant_id": "tenant-one"}
    selected = {**scenario["verified"], "configuration_id": "config-finops-denials",
                "finops": {"billing_scope": {"account_ids": ["123456789012"],
                                             "view_ref": "selected-view"},
                           "data_role_ref": "selected-role", "agent_mode": "adopt",
                           "native_agent_ref": "selected-agent"}}

    class Backend:
        def __init__(self, session):
            self.session = session

        def store(self, product_id, project_id, service_id):
            if service_id != "finops":
                raise HTTPException(404, detail={"code": "not_found"})
            return BindingStore(self.session, actor["tenant_id"], product_id, project_id, service_id)

        async def start_binding(self, product_id, project_id, service_id, body, key, etag):
            return await self.store(product_id, project_id, service_id).start(body, key, selected)

        async def get_binding_operation(self, product_id, project_id, service_id, operation_id):
            return await self.store(product_id, project_id, service_id).operation(operation_id)

    async with session_factory() as session:
        session.add(Configuration(tenant_id="tenant-one", configuration_id="config-finops-denials",
                                  product_id="product-one", project_id="project-one",
                                  service_id="finops", relationship_revision="1",
                                  owner_id="owner-one", current_revision=1, handoff_state="linked"))
        await session.commit()
        app = FastAPI()
        app.include_router(create_router(lambda: Backend(session)))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            base = "/v1/products/product-one/projects/project-one/services/finops/binding-operations"
            body = {"action": "link", "configuration_revision": 1,
                    "native_resource_ref": {**selected["connection_ref"],
                                            "record_type": "finops_agent",
                                            "record_id": "selected-agent",
                                            "record_revision": "revision-one"}}
            headers = {"Idempotency-Key": "finops_denial_operation_0001"}
            own = await client.post(base, json=body, headers=headers)
            assert own.status_code == 202, own.text
            own_id = own.json()["operation_id"]
            assert (await client.get(f"{base}/{own_id}")).status_code == 200
            for wrong in (
                {**body, "billing_scope": {"account_ids": ["987654321098"], "view_ref": "foreign-view"}},
                {**body, "data_role_ref": "foreign-role"},
                {**body, "native_resource_ref": {
                    **body["native_resource_ref"], "record_id": "foreign-agent"}},
            ):
                response = await client.post(base, json=wrong, headers={
                    "Idempotency-Key": "finops_denial_operation_0002"})
                assert response.status_code in {404, 422}
                assert "foreign" not in json.dumps(response.json())
            other_project = base.replace("/projects/project-one/", "/projects/project-two/")
            assert (await client.get(f"{other_project}/{own_id}")).status_code == 404
            assert (await client.post(other_project, json=body, headers=headers)).status_code == 404
            actor["tenant_id"] = "tenant-two"
            assert (await client.get(f"{base}/{own_id}")).status_code == 404
            assert (await client.post(base, json=body, headers=headers)).status_code == 404
            assert scenario["native"].create_attempts == []

@pytest.mark.asyncio
async def test_installed_finops_binding_intents_remain_project_scoped(gateway_configuration_scenario, monkeypatch):
    from datetime import timedelta

    from l5_services.models.configuration import ConfigurationVersion
    from src import l5_integration
    from src.auth.aws_connection_authority import connection_binding
    from src.l5_service_identity import worker_id
    from src.products.models import ProductSourceBinding
    from src.shared.models.vault import CredentialServiceDelegation, UserCredential

    scenario = gateway_configuration_scenario
    client = scenario["client"]
    actor, foreign_actor = scenario["actors"]
    products = [await scenario["prepare"](actor, label, "internal", ["finops"])
                for label in ("binding-finops-first", "binding-finops-second")]
    async with scenario["sessions"]() as session:
        credential = UserCredential(
            org_id=actor.org_id, user_id=actor.user_id, agent_consent=True,
            service="aws", credential_type="aws_role", label="finops-binding-test",
            secret_arn="fictional-secret", aws_external_id="fictional-external",
            aws_verified_at=datetime.now(UTC), aws_verification_attempt="verification-one",
            aws_verified_version_id="version-one",
            scopes={"account_id": "123456789012",
                    "role_arn": "arn:aws:iam::123456789012:role/FictionalRole",
                    "source": "imported_role", "status": "verified"},
        )
        session.add(credential)
        await session.flush()
        credential.aws_verified_binding = connection_binding(credential)
        for product in products:
            reference = {"tenant_id": actor.org_id, "project_id": product["project_id"],
                         "source_connection_id": credential.id, "record_type": "connection",
                         "record_id": credential.id, "record_revision": "version-one"}
            session.add(CredentialServiceDelegation(
                tenant_id=actor.org_id, credential_id=credential.id, service_id=worker_id("finops"),
                product_id=product["product_id"], project_id=product["project_id"],
                created_by=actor.user_id, operations=["product:read", "credential:session"],
                sources=[{**reference, "record_type": "credential"}], capabilities=[],
                visibility="restricted", expires_at=datetime.now(UTC) + timedelta(hours=1),
            ))
            session.add(ProductSourceBinding(
                tenant_id=actor.org_id, project_id=product["project_id"], credential_id=credential.id,
                kind="environment", provider="aws", namespace="selected-account", native_id="us-east-1",
                desired={"region": "us-east-1"}, observed={"source_revision": "version-one"}, status="healthy",
            ))
            scenario["permissions"][(actor.org_id, product["project_id"])].add(
                (credential.id, "us-east-1"))
        await session.commit()
    selected_project = {"id": products[0]["project_id"]}

    async def authorized_connections(db, current_actor):
        reference = {"tenant_id": actor.org_id, "project_id": selected_project["id"],
                     "source_connection_id": credential.id, "record_type": "connection",
                     "record_id": credential.id, "record_revision": "version-one"}
        return [({"provider": "aws", "connection_ref": {"kind": "credential", "id": credential.id},
                  "record_ref": reference}, None)]

    monkeypatch.setattr(l5_integration, "authorized_connections", authorized_connections)
    receipts = []
    for index, product in enumerate(products):
        scenario["switch"](actor)
        selected_project["id"] = product["project_id"]
        path = product["path"].replace("/security/", "/finops/")
        options = await client.get(path + "/options")
        assert options.status_code == 200, options.text
        connection = options.json()["connections"][0]
        body = {"service_id": "finops", "capabilities": ["cost_inquiry"], "enabled": False,
                "connection_ref": connection, "region": "us-east-1",
                "overrides": [{"field": "connection_ref", "value": connection, "reason": "Selected"},
                              {"field": "region", "value": "us-east-1", "reason": "Selected"}],
                "finops": {"billing_scope": {"account_ids": ["123456789012"],
                                             "view_ref": f"project-view-{index}"},
                           "data_role_ref": f"project-role-{index}", "agent_mode": "adopt",
                           "native_agent_ref": "shared-agent-example"}}
        saved = await client.put(path, json=body, headers={
            "Idempotency-Key": "finops_binding_config_0001", "If-None-Match": "*"})
        assert saved.status_code == 201, saved.text
        assert saved.json()["settings"]["finops"] == body["finops"]
        started = await client.post(path.removesuffix("/configuration") + "/binding-operations",
                                    json={"action": "link", "configuration_revision": 1,
                                          "native_resource_ref": {**connection,
                                                                  "record_type": "finops_agent",
                                                                  "record_id": "shared-agent-example",
                                                                  "record_revision": "revision-one"}},
                                    headers={"Idempotency-Key": "finops_binding_intent_0001"})
        assert started.status_code == 202, started.text
        assert started.json()["state"] == "accepted"
        assert started.json()["gaps"] == []
        receipts.append((product, started.json()["operation_id"]))
        async with scenario["sessions"]() as reader:
            intent = await reader.get(SetupIntent, (actor.org_id, started.json()["operation_id"]))
            assert intent.project_id == product["project_id"]
            assert intent.configuration_id == saved.json()["configuration_id"]
            assert intent.authorization["delegation_id"]
            version = await reader.get(ConfigurationVersion, (
                actor.org_id, saved.json()["configuration_id"], 1))
            assert version.settings["finops"]["billing_scope"]["view_ref"] == f"project-view-{index}"
    assert receipts[0][1] != receipts[1][1]
    first, second = receipts
    first_path = first[0]["path"].replace("/security/configuration", "/finops/binding-operations")
    second_path = second[0]["path"].replace("/security/configuration", "/finops/binding-operations")
    selected_project["id"] = second[0]["project_id"]
    assert (await client.get(f"{second_path}/{first[1]}")).status_code == 404
    scenario["switch"](foreign_actor)
    assert (await client.get(f"{first_path}/{first[1]}")).status_code == 404
    assert (await client.get(f"{second_path}/{second[1]}")).status_code == 404
    assert scenario["provider_attempts"] == []
    scenario["switch"](actor)
    selected_project["id"] = first[0]["project_id"]
    assert (await client.get(first_path + "/" + first[1])).status_code == 200
    replay_path = f"{first_path}/{first[1]}/reconcile"
    replay_headers = {"Idempotency-Key": "finops_blocked_replay_0001"}
    replay = await client.post(replay_path, json={"reason": "Review interrupted setup"}, headers=replay_headers)
    assert replay.status_code == 409 and replay.json()["detail"]["code"] == "reconcile_not_available"

    configuration_path = first[0]["path"].replace("/security/configuration", "/finops/configuration")
    current = await client.get(configuration_path)
    assert current.status_code == 200
    settings = current.json()["settings"]
    updated_settings = {field: settings[field] for field in (
        "service_id", "capabilities", "enabled", "connection_ref", "region", "overrides")}
    updated_settings["finops"] = {**settings["finops"], "agent_mode": "create"}
    updated_settings["finops"].pop("native_agent_ref")
    changed = await client.put(configuration_path, json=updated_settings, headers={
        "If-Match": current.headers["etag"], "Idempotency-Key": "finops_retirement_config_0001"})
    assert changed.status_code == 200, changed.text
    created = await client.post(first_path, json={"action": "create", "configuration_revision": 2},
                                headers={"Idempotency-Key": "finops_retirement_intent_0001"})
    assert created.status_code == 202 and created.json()["gaps"] == ["native_unqualified"]
    replay_path = f"{first_path}/{created.json()['operation_id']}/reconcile"
    replay = await client.post(replay_path, json={"reason": "Review interrupted setup"}, headers=replay_headers)
    assert replay.status_code == 202 and replay.json()["state"] == "blocked"
    assert replay.json()["gaps"] == ["native_unqualified"] and replay.json()["revision"] == 2
    repeated = await client.post(replay_path, json={"reason": "Review interrupted setup"}, headers=replay_headers)
    assert repeated.status_code == 202 and repeated.json() == replay.json()
    assert (await client.post(replay_path, json={"reason": "Different replay"},
                              headers=replay_headers)).status_code == 409

    async with scenario["sessions"]() as session:
        source = await session.get(SetupIntent, (actor.org_id, created.json()["operation_id"]))
        native_ref = {**settings["connection_ref"], "record_type": "finops_agent",
                      "record_id": "synthetic-owned-agent", "record_revision": "revision-one"}
        provider_identity = {"account_id": credential.scopes["account_id"],
                             "role_arn": credential.scopes["role_arn"], "region": "us-east-1"}
        receipt = {"operation_id": source.operation_id, "intent_hash": source.intent_hash,
                   "native_resource_ref": native_ref, "provider_identity": provider_identity}
        session.add(Binding(tenant_id=actor.org_id, binding_id="synthetic-owned-binding",
                            configuration_id=source.configuration_id, product_id=first[0]["product_id"],
                            project_id=first[0]["project_id"], service_id="finops", scope=source.scope,
                            revision=1, generation=1, state="ready", ownership="adp_created",
                            connection_ref=settings["connection_ref"], connection_key="synthetic-connection",
                            native_resource_ref=native_ref, native_key="synthetic-agent",
                            provider_identity=provider_identity, creation_receipt=receipt,
                            readiness=[], gaps=[]))
        await session.commit()

    cleanup_body = {"action": "cleanup", "configuration_revision": 2,
                    "binding_id": "synthetic-owned-binding", "owned_cleanup_confirmed": True}
    cleanup_headers = {"Idempotency-Key": "finops_retirement_cleanup_0001", "If-Match": '"1"'}
    assert (await client.post(first_path, json=cleanup_body,
                              headers={**cleanup_headers, "If-Match": '"0"'})).status_code == 412
    cleanup = await client.post(first_path, json=cleanup_body, headers=cleanup_headers)
    assert cleanup.status_code == 202, cleanup.text
    assert cleanup.json()["state"] == "blocked" and cleanup.json()["gaps"] == ["native_unqualified"]
    assert cleanup.json()["binding_id"] == "synthetic-owned-binding"
    assert (await client.post(first_path, json=cleanup_body, headers=cleanup_headers)).json() == cleanup.json()
    assert (await client.get(first_path + "/" + cleanup.json()["operation_id"])).json() == cleanup.json()
    cleanup_replay = await client.post(
        f'{first_path}/{cleanup.json()["operation_id"]}/reconcile',
        json={"reason": "Review pending owned cleanup"},
        headers={"Idempotency-Key": "finops_cleanup_replay_0001"})
    assert cleanup_replay.status_code == 202 and cleanup_replay.json()["state"] == "blocked"
    assert cleanup_replay.json()["revision"] == 2

    from l5_services.workers.bindings import execute_setup_work

    async with scenario["sessions"]() as reader:
        stored = await reader.get(Binding, (actor.org_id, "synthetic-owned-binding"))
        assert stored.state == "cleanup_pending" and stored.creation_receipt == receipt
        cleanup_intent = await reader.get(SetupIntent, (actor.org_id, cleanup.json()["operation_id"]))
        assert cleanup_intent.owned_resource_receipt == receipt
        work = await reader.scalar(select(SetupWork).where(
            SetupWork.tenant_id == actor.org_id, SetupWork.operation_id == cleanup_intent.operation_id))
        envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}

    async def forbidden_native_authorize(intent, claim):
        raise AssertionError("unqualified provider must not be called")

    assert await execute_setup_work(scenario["sessions"], envelope, forbidden_native_authorize) is False
    async with scenario["sessions"]() as reader:
        work = await reader.get(SetupWork, (actor.org_id, envelope["work_id"]))
        assert work.blocked_at is not None and work.last_error == "native_unqualified"
        assert (await reader.get(Binding, (actor.org_id, "synthetic-owned-binding"))).state == "cleanup_pending"
    selected_project["id"] = second[0]["project_id"]
    assert (await client.post(second_path, json=cleanup_body, headers=cleanup_headers)).status_code == 404
    async with scenario["sessions"]() as session:
        linked_intent = await session.get(SetupIntent, (actor.org_id, second[1]))
        linked_version = await session.get(ConfigurationVersion, (
            actor.org_id, linked_intent.configuration_id, 1))
        session.add(Binding(tenant_id=actor.org_id, binding_id="synthetic-linked-binding",
                            configuration_id=linked_intent.configuration_id, product_id=second[0]["product_id"],
                            project_id=second[0]["project_id"], service_id="finops", scope=linked_intent.scope,
                            revision=1, generation=1, state="ready", ownership="customer_linked",
                            connection_ref=linked_version.settings["connection_ref"],
                            connection_key="synthetic-link-connection",
                            native_resource_ref=linked_intent.request["native_resource_ref"],
                            native_key="synthetic-linked-agent", provider_identity=provider_identity,
                            readiness=[], gaps=[]))
        await session.commit()
    linked_cleanup = {**cleanup_body, "configuration_revision": 1,
                      "binding_id": "synthetic-linked-binding"}
    assert (await client.post(second_path, json=linked_cleanup,
                              headers={**cleanup_headers, "Idempotency-Key": "finops_linked_cleanup_0001"})).status_code == 403
    unlink = await client.post(second_path, json={"action": "unlink", "configuration_revision": 1,
                                                  "binding_id": "synthetic-linked-binding"},
                               headers={"Idempotency-Key": "finops_linked_unlink_0001", "If-Match": '"1"'})
    assert unlink.status_code == 202 and unlink.json()["state"] == "blocked"
    async with scenario["sessions"]() as reader:
        retained = await reader.get(Binding, (actor.org_id, "synthetic-linked-binding"))
        assert retained.ownership == "customer_linked" and retained.state == "unlinking"
        assert retained.native_resource_ref == linked_intent.request["native_resource_ref"]
    assert scenario["provider_attempts"] == []

@pytest.mark.asyncio
async def test_finops_setup_resource_receipts_survive_replay_without_duplicate_step(uncertain_setup_scenario):
    from l5_services.models.setup import SetupResourceReceipt

    scenario = uncertain_setup_scenario
    verified = {**scenario["verified"], "configuration_id": "config-finops-receipts",
                "finops": {"agent_mode": "create", "billing_scope": {
                    "account_ids": ["123456789012"], "view_ref": "view-example"},
                    "data_role_ref": "role-example"}}
    request = {"action": "create", "configuration_revision": 1}
    async with scenario["sessions"]() as session:
        session.add(Configuration(tenant_id="tenant-one", configuration_id="config-finops-receipts",
                                  product_id="product-one", project_id="project-one",
                                  service_id="finops", relationship_revision="1",
                                  owner_id="owner-one", current_revision=1, handoff_state="linked"))
        await session.commit()
        store = BindingStore(session, "tenant-one", "product-one", "project-one", "finops")
        operation = await store.start(request, "finops_owned_setup_0001", verified)
        assert operation.state == "blocked" and operation.gaps == ["native_unqualified"]

    for resource_kind, record_type in (("billing_view", "billing_view"),
                                       ("data_role", "data_role"),
                                       ("agent", "finops_agent")):
        async with scenario["sessions"]() as session:
            store = BindingStore(session, "tenant-one", "product-one", "project-one", "finops")
            assert (await store.start(request, "finops_owned_setup_0001", verified)).operation_id == operation.operation_id
            planned = await store.record_setup_resource(
                operation.operation_id, verified, resource_kind, "owned", "planned")
            assert planned.state == "planned" and planned.revision == 1
            assert (await store.record_setup_resource(
                operation.operation_id, verified, resource_kind, "owned", "planned")).revision == 1
            assert (await store.record_setup_resource(
                operation.operation_id, verified, resource_kind, "owned", "unknown")).state == "unknown"
        async with scenario["sessions"]() as session:
            store = BindingStore(session, "tenant-one", "product-one", "project-one", "finops")
            with pytest.raises(HTTPException) as failure:
                await store.record_setup_resource(
                    operation.operation_id, verified, resource_kind, "owned", "planned")
            assert failure.value.detail == {"code": "setup_step_reconcile_required"}
            reference = {**verified["connection_ref"], "record_type": record_type,
                         "record_id": f"{resource_kind}-example", "record_revision": "revision-one"}
            reconciled = await store.record_setup_resource(
                operation.operation_id, verified, resource_kind, "owned", "observed",
                native_resource_ref=reference, evidence_digest="a" * 64)
            assert reconciled.state == "observed" and reconciled.revision == 3
            assert reconciled.native_resource_ref == reference
            assert (await store.record_setup_resource(
                operation.operation_id, verified, resource_kind, "owned", "observed",
                native_resource_ref=reference, evidence_digest="a" * 64)).revision == 3
            foreign = {**reference, "project_id": "foreign-project"}
            with pytest.raises(HTTPException) as failure:
                await store.record_setup_resource(
                    operation.operation_id, verified, resource_kind, "owned", "observed",
                    native_resource_ref=foreign, evidence_digest="a" * 64)
            assert failure.value.status_code == 404

    async with scenario["sessions"]() as reader:
        rows = (await reader.scalars(select(SetupResourceReceipt).where(
            SetupResourceReceipt.operation_id == operation.operation_id))).all()
        assert {row.resource_kind for row in rows} == {"billing_view", "data_role", "agent"}
        assert all(row.ownership == "owned" and row.state == "observed" for row in rows)
        assert (await reader.get(BindingOperation, ("tenant-one", operation.operation_id))).state == "blocked"
    assert scenario["native"].create_attempts == []


@pytest.mark.asyncio
async def test_finops_retirement_keeps_adopted_and_referenced_resources(uncertain_setup_scenario):
    from l5_services.models import ConfigurationVersion, SetupResourceReceipt

    scenario = uncertain_setup_scenario
    verified = {**scenario["verified"], "configuration_id": "finops-retirement-first",
                "finops": {"agent_mode": "create", "billing_scope": {
                    "account_ids": ["123456789012"], "view_ref": "view-shared"},
                    "data_role_ref": "role-exclusive"}}
    other = {**verified, "configuration_id": "finops-retirement-second",
             "scope": {**verified["scope"], "product_id": "product-two"},
             "finops": {"agent_mode": "adopt", "native_agent_ref": "agent-adopted",
                        "billing_scope": verified["finops"]["billing_scope"],
                        "data_role_ref": "role-other"}}
    async with scenario["sessions"]() as session:
        for config_id, product_id in (("finops-retirement-first", "product-one"),
                                      ("finops-retirement-second", "product-two")):
            session.add(Configuration(tenant_id="tenant-one", configuration_id=config_id,
                                      product_id=product_id, project_id="project-one",
                                      service_id="finops", relationship_revision="1", owner_id="owner-one",
                                      current_revision=1, handoff_state="linked"))
        session.add(ConfigurationVersion(tenant_id="tenant-one", configuration_id="finops-retirement-second",
                                         revision=1, settings={"finops": other["finops"]},
                                         inherited_fields=[], gaps=[], state="ready", hash="a" * 64))
        await session.commit()
        first = BindingStore(session, "tenant-one", "product-one", "project-one", "finops")
        created = await first.start({"action": "create", "configuration_revision": 1},
                                    "finops_retirement_create_0001", verified)
        second = BindingStore(session, "tenant-one", "product-two", "project-one", "finops")
        linked = await second.start({"action": "link", "configuration_revision": 1,
                                     "native_resource_ref": {**verified["connection_ref"],
                                                             "record_type": "finops_agent",
                                                             "record_id": "agent-adopted",
                                                             "record_revision": "rev-one"}},
                                    "finops_retirement_link_00001", other)

    references = {
        kind: {**verified["connection_ref"], "record_type": record_type,
               "record_id": resource_id, "record_revision": "rev-one"}
        for kind, record_type, resource_id in (
            ("agent", "finops_agent", "agent-exclusive"),
            ("data_role", "data_role", "role-exclusive"),
            ("billing_view", "billing_view", "view-shared"))
    }
    async with scenario["sessions"]() as session:
        first = BindingStore(session, "tenant-one", "product-one", "project-one", "finops")
        second = BindingStore(session, "tenant-one", "product-two", "project-one", "finops")
        for kind, reference in references.items():
            await first.record_setup_resource(created.operation_id, verified, kind, "owned", "planned")
            await first.record_setup_resource(created.operation_id, verified, kind, "owned", "observed",
                                              native_resource_ref=reference, evidence_digest="a" * 64)
        await second.record_setup_resource(linked.operation_id, other, "billing_view", "adopted", "planned")
        await second.record_setup_resource(linked.operation_id, other, "billing_view", "adopted", "observed",
                                           native_resource_ref=references["billing_view"], evidence_digest="b" * 64)

        async def deny(store, operation_id, selected, kind, ownership, expected):
            with pytest.raises(HTTPException) as failure:
                await store.record_setup_resource(operation_id, selected, kind, ownership, "absent",
                                                  native_resource_ref=references[kind], evidence_digest="c" * 64)
            assert failure.value.detail == {"code": expected}

        await deny(second, linked.operation_id, other, "billing_view", "adopted",
                   "setup_step_reconcile_required")
        await deny(first, created.operation_id, verified, "billing_view", "owned", "resource_still_referenced")
        await deny(first, created.operation_id, verified, "data_role", "owned", "agent_retirement_required")
        await first.record_setup_resource(created.operation_id, verified, "agent", "owned", "unknown",
                                          native_resource_ref=references["agent"])
        assert (await session.get(SetupResourceReceipt, (
            "tenant-one", created.operation_id, "agent"))).state == "unknown"
        active = Binding(tenant_id="tenant-one", binding_id="finops-active-binding",
                         configuration_id="finops-retirement-first", product_id="product-one",
                         project_id="project-one", service_id="finops", scope=verified["scope"],
                         revision=1, generation=1, state="ready", ownership="adp_created",
                         connection_ref=verified["connection_ref"], connection_key="finops-connection-key",
                         native_resource_ref=references["agent"], native_key="finops-native-key",
                         provider_identity={"account_id": verified["account_id"]},
                         creation_receipt={"operation_id": created.operation_id}, readiness=[], gaps=[])
        session.add(active)
        await session.commit()
        await deny(first, created.operation_id, verified, "agent", "owned", "resource_still_referenced")
        active.state = "cleanup_pending"
        await session.commit()
        for kind in ("agent", "data_role"):
            if kind == "data_role":
                shared = await session.get(ConfigurationVersion, (
                    "tenant-one", "finops-retirement-second", 1))
                shared.settings = {"finops": {**other["finops"], "data_role_ref": "role-exclusive"}}
                await session.commit()
                await deny(first, created.operation_id, verified, kind, "owned", "resource_still_referenced")
                shared.settings = {"finops": other["finops"]}
                await session.commit()
            assert (await first.record_setup_resource(created.operation_id, verified, kind, "owned", "absent",
                                                      native_resource_ref=references[kind],
                                                      evidence_digest="c" * 64)).state == "absent"
        await deny(first, created.operation_id, verified, "billing_view", "owned", "resource_still_referenced")
        assert (await session.get(SetupResourceReceipt, (
            "tenant-one", linked.operation_id, "billing_view"))).state == "observed"
    assert scenario["native"].create_attempts == []


@pytest.mark.asyncio
async def test_setup_work_claim_fences_early_duplicate_and_renewed_deliveries(uncertain_setup_scenario):
    from l5_services.workers.bindings import execute_setup_work

    scenario = uncertain_setup_scenario
    now = datetime.now(UTC) + timedelta(minutes=1)
    async with scenario["sessions"]() as session:
        store = BindingStore(session, "tenant-one", "product-one", "project-one", "security")
        operation = await store.start({"action": "create", "configuration_revision": 1},
                                      "setup_claim_fence_0001", scenario["verified"])
        work = await session.scalar(select(SetupWork).where(
            SetupWork.operation_id == operation.operation_id))
        work_id = work.work_id
        work.due_at = now + timedelta(minutes=2)
        await session.commit()

    envelope = {"schema_version": "1.0", "work_id": work_id, "generation": 1}
    attempts = []

    async def unavailable(intent, claim):
        attempts.append(intent.operation_id)
        assert claim.operation_id == intent.operation_id and claim.owner and claim.fence >= 1
        assert claim.lease_until > now
        raise RuntimeError("provider unavailable")

    assert not await execute_setup_work(scenario["sessions"], envelope, unavailable, now=now)
    assert attempts == []
    async with scenario["sessions"]() as session:
        work = await session.get(SetupWork, ("tenant-one", work_id))
        assert work.lease_owner is None and work.fence == 0
        work.due_at = now
        await session.commit()

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await execute_setup_work(scenario["sessions"], envelope, unavailable, now=now)
    async with scenario["sessions"]() as session:
        first = await session.get(SetupWork, ("tenant-one", work_id))
        assert first.lease_owner and first.fence == 1
        original_owner = first.lease_owner
    assert not await execute_setup_work(scenario["sessions"], envelope, unavailable, now=now)
    assert not await execute_setup_work(scenario["sessions"], {
        **envelope, "generation": 2}, unavailable, now=now + timedelta(minutes=6))
    assert len(attempts) == 1
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await execute_setup_work(scenario["sessions"], envelope, unavailable, now=now + timedelta(minutes=6))
    async with scenario["sessions"]() as session:
        renewed = await session.get(SetupWork, ("tenant-one", work_id))
        assert renewed.fence == 2 and renewed.lease_owner != original_owner
    assert len(attempts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ("expired", "replaced"))
async def test_setup_authorization_rechecks_fenced_lease_before_provider(uncertain_setup_scenario, stale):
    scenario = uncertain_setup_scenario
    now = datetime.now(UTC) + timedelta(minutes=1)
    async with scenario["sessions"]() as session:
        store = BindingStore(session, "tenant-one", "product-one", "project-one", "security")
        operation = await store.start({"action": "create", "configuration_revision": 1},
                                      "setup_lease_stale_0001", scenario["verified"])
        work_id = await session.scalar(select(SetupWork.work_id).where(
            SetupWork.operation_id == operation.operation_id))
    time = {"now": now}
    claims = []

    async def authorize(intent, claim):
        claims.append(claim)
        if stale == "expired":
            time["now"] = claim.lease_until + timedelta(seconds=1)
        else:
            async with scenario["sessions"]() as session:
                work = await session.get(SetupWork, (claim.tenant_id, claim.work_id))
                work.fence += 1
                work.lease_owner = "successor"
                await session.commit()
        return scenario["verified"], scenario["adapter"]

    service = ExecutionService(scenario["sessions"], None, None, None, None,
                               binding_authorize=authorize, clock=lambda: time["now"])
    assert not await service.execute({"schema_version": "1.0", "work_id": work_id, "generation": 1})
    assert len(claims) == 1
    assert scenario["native"].create_attempts == []
    async with scenario["sessions"]() as session:
        work = await session.get(SetupWork, ("tenant-one", work_id))
        outcome = await session.get(BindingOperation, ("tenant-one", operation.operation_id))
        assert outcome.state == "accepted" and work.completed_at is None
        if stale == "replaced":
            assert work.fence > claims[0].fence and work.lease_owner == "successor"
        else:
            assert work.fence == claims[0].fence and work.lease_owner == claims[0].owner


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ("revoked", "foreign", "replaced", "expired", "invalid_session"))
async def test_setup_denial_keeps_successor_recovery_fenced(uncertain_setup_scenario, denial):
    scenario = uncertain_setup_scenario
    now = datetime.now(UTC) + timedelta(minutes=1)
    clock = {"now": now}
    async with scenario["sessions"]() as session:
        store = BindingStore(session, "tenant-one", "product-one", "project-one", "security")
        operation = await store.start({"action": "create", "configuration_revision": 1},
                                      f"setup_denial_{denial}_0001", scenario["verified"])
        work = await session.scalar(select(SetupWork).where(SetupWork.operation_id == operation.operation_id))
        envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}

    async def denied(intent, claim):
        assert intent.operation_id == operation.operation_id
        if denial == "replaced":
            async with scenario["sessions"]() as session:
                successor = await session.get(SetupWork, (claim.tenant_id, claim.work_id))
                successor.fence += 1
                successor.lease_owner = "successor"
                successor.lease_until = now + timedelta(minutes=10)
                await session.commit()
        elif denial == "expired":
            clock["now"] = claim.lease_until + timedelta(seconds=1)
        if denial == "invalid_session":
            raise AuthorityRevoked("setup broker returned another scope")
        raise HTTPException(404 if denial == "foreign" else 403,
                            detail={"code": "not_found" if denial == "foreign" else "delegation_denied"})

    worker = ExecutionService(scenario["sessions"], None, None, None, None,
                              binding_authorize=denied, clock=lambda: clock["now"])
    assert not await worker.execute(envelope)
    assert not await worker.execute(envelope)
    assert scenario["native"].create_attempts == []
    async with scenario["sessions"]() as session:
        current = await session.get(SetupWork, ("tenant-one", envelope["work_id"]))
        persisted = await session.get(BindingOperation, ("tenant-one", operation.operation_id))
        assert persisted.state == "accepted" and persisted.attempts == 0
        if denial in {"revoked", "foreign", "invalid_session"}:
            assert current.blocked_at is not None and current.last_error == "setup_authority_denied"
            assert current.fence == 1
        else:
            assert current.blocked_at is None and current.last_error is None
            assert current.fence == 2
            if denial == "replaced":
                assert current.lease_owner == "successor"

    if denial in {"revoked", "foreign", "invalid_session"}:
        clock["now"] = now + timedelta(minutes=11)
        restored = ExecutionService(scenario["sessions"], None, None, None, None,
                                    binding_authorize=scenario["authorize"], clock=lambda: clock["now"])
        assert not await restored.execute(envelope)
        assert scenario["native"].create_attempts == []

    if denial in {"replaced", "expired"}:
        clock["now"] = now + timedelta(minutes=11)
        scenario["native"].lost_response = False
        recovered = ExecutionService(scenario["sessions"], None, None, None, None,
                                     binding_authorize=scenario["authorize"], clock=lambda: clock["now"])
        assert await recovered.execute(envelope)
        assert not await recovered.execute(envelope)
        assert len(scenario["native"].create_attempts) == 1
        async with scenario["sessions"]() as session:
            current = await session.get(SetupWork, ("tenant-one", envelope["work_id"]))
            persisted = await session.get(BindingOperation, ("tenant-one", operation.operation_id))
            assert current.completed_at is not None and current.fence > 1
            assert persisted.state == "succeeded" and persisted.attempts == 1
