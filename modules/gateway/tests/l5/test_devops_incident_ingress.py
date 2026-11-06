"""Signed webhook routes by configured Product source, not event-supplied scope."""

import asyncio
import hmac
import json
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from l5_services.models.base import Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.event import EventReceipt
from l5_services.models.receipt import RunReceipt
from l5_services.models.run import Run
from l5_services.models.work import Work
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.l5_integration import GatewayConfigurationBackend, incident_work_backend, load_incident_secret, router
from src.shared.database import get_db
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.models.vault import CredentialServiceDelegation, UserCredential


@pytest.mark.asyncio
async def test_signed_webhook_selects_matching_project_without_user_identity(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'webhooks.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    secrets = {"secret-a": "fake-secret-a", "secret-b": "fake-secret-b"}

    def reference(suffix, record_type):
        return {
            "tenant_id": f"tenant-{suffix}",
            "project_id": f"project-{suffix}",
            "source_connection_id": f"connector-{suffix}",
            "record_type": record_type,
            "record_id": f"record-{suffix}",
            "record_revision": "revision-1",
        }

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(BudgetConfig.__table__.create)
        await connection.run_sync(BudgetUsage.__table__.create)
    async with sessions() as session:
        for suffix in ("a", "b"):
            resource = reference(suffix, "resource")
            trigger = {
                "trigger_id": "incident-trigger",
                "enabled": True,
                "kind": "native_event",
                "capability": "investigation",
                "binding_id": f"binding-{suffix}",
                "resource_refs": [resource],
                "telemetry_refs": [],
                "severities": ["high"],
                "environment_refs": [resource],
                "maintenance_windows": [],
            }
            session.add(
                Configuration(
                    tenant_id=f"tenant-{suffix}",
                    configuration_id=f"config-{suffix}",
                    product_id=f"product-{suffix}",
                    project_id=f"project-{suffix}",
                    service_id="devops",
                    relationship_revision="1",
                    owner_id=f"owner-{suffix}",
                    current_revision=1,
                    handoff_state="linked",
                )
            )
            connection_ref = {**resource, "record_type": "connection", "record_id": f"connector-{suffix}"}
            settings = {
                "enabled": True,
                "visibility": "only_me",
                "classification": "internal",
                "profile_ref": {**resource, "record_type": "product"},
                "limits": {
                    "deadline_seconds": 3600,
                    "max_provider_attempts": 1,
                    "max_polls": 10,
                    "concurrency": 1,
                    "max_cost": "1.00",
                    "currency": "USD",
                    "policy_ref": {**resource, "record_type": "policy"},
                },
                "devops": {
                    "region": "us-east-1",
                    "hosting_connection_ref": connection_ref,
                    "trigger_policies": [trigger],
                    "incident_sources": [
                        {
                            "source_id": "shared-source-name",
                            "secret_ref": f"secret-{suffix}",
                            "source_ref": reference(suffix, "incident"),
                            "environment_ref": resource,
                            "trigger_id": "incident-trigger",
                        }
                    ],
                },
            }
            session.add(
                ConfigurationVersion(
                    tenant_id=f"tenant-{suffix}",
                    configuration_id=f"config-{suffix}",
                    revision=1,
                    state="ready",
                    hash="a" * 64,
                    inherited_fields=[],
                    gaps=[],
                    settings=settings,
                )
            )
            if suffix == "b":
                session.add(
                    BudgetConfig(
                        id="record-b",
                        org_id="tenant-b",
                        entity_type="org",
                        entity_id="tenant-b",
                        period_type="daily",
                        budget_amount_usd=Decimal("5.00"),
                        enforcement_mode="hard",
                    )
                )
                account = "1" * 12
                role = f"arn:aws:iam::{account}:role/example"
                session.add(
                    Binding(
                        tenant_id="tenant-b",
                        binding_id="binding-b",
                        configuration_id="config-b",
                        product_id="product-b",
                        project_id="project-b",
                        service_id="devops",
                        scope={
                            "tenant_id": "tenant-b",
                            "product_id": "product-b",
                            "project_id": "project-b",
                            "relationship_revision": "1",
                            "owner_id": "owner-b",
                            "visibility": "only_me",
                        },
                        revision=1,
                        generation=3,
                        state="ready",
                        ownership="adopted",
                        connection_ref=connection_ref,
                        connection_key="a" * 64,
                        native_key="b" * 64,
                        native_resource_ref={**resource, "record_type": "agent_space"},
                        provider_identity={"account_id": account, "role_arn": role, "region": "us-east-1"},
                        readiness=[],
                        gaps=[],
                    )
                )
        await session.commit()

    async def database():
        async with sessions() as session:
            yield session

    async def load(secret_ref):
        return secrets[secret_ref]

    credential = SimpleNamespace(
        id="connector-b",
        org_id="tenant-b",
        user_id="owner-b",
        service="aws",
        credential_type="aws_role",
        aws_verified_version_id="revision-1",
        scopes={"account_id": account, "role_arn": role},
    )
    grant = SimpleNamespace(
        id="grant-b",
        execution_scope=None,
        revision=2,
        operations=["credential:session"],
        capabilities=["investigation"],
        binding_generation=3,
        sources=[{**reference("b", "connection"), "record_type": "credential", "record_id": "connector-b"}],
    )
    monkeypatch.setattr("src.l5_integration.verified_connection_evidence", lambda observed: observed is credential)

    # This fixture isolates routing/budget races with synthetic authority.
    # test_l5_owner_incident_delegation covers real owner and child issuance.
    async def derived(db, receipt, parent, selected_credential, binding, connection, deadline):
        assert parent is grant and selected_credential is credential
        return SimpleNamespace(id="grant-b-child", revision=1, created_by="owner-b")

    monkeypatch.setattr("src.l5_integration.issue_l5_incident_delegation", derived)
    queue = []

    async def publish(envelope, delay):
        queue.append((envelope, delay))

    class AuthoritySession:
        def __init__(self, session):
            self.session = session
            self.bind = engine

        def __getattr__(self, name):
            return getattr(self.session, name)

        async def get(self, model, identifier, **kwargs):
            if model is UserCredential:
                return credential if identifier == credential.id else None
            if model is CredentialServiceDelegation:
                return grant if identifier == grant.id else None
            return await self.session.get(model, identifier, **kwargs)

        async def scalar(self, statement):
            if statement.column_descriptions[0]["entity"] is CredentialServiceDelegation:
                return grant
            return await self.session.scalar(statement)

    selected = SimpleNamespace(publish=publish)
    racing = False
    arrivals = 0
    both_ready = asyncio.Event()

    class RacingBackend(GatewayConfigurationBackend):
        async def _require_devops_budget(self, database, limits, selected_scope, now, replay):
            nonlocal arrivals
            if racing:
                arrivals += 1
                if arrivals == 2:
                    both_ready.set()
                await asyncio.wait_for(both_ready.wait(), timeout=5)
            return await super()._require_devops_budget(database, limits, selected_scope, now, replay)

    def work_backend(session, receipt):
        actor = SimpleNamespace(org_id=receipt.tenant_id, user_id=None, account_type="service")
        return RacingBackend(AuthoritySession(session), actor, publish=selected.publish)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[load_incident_secret] = lambda: load
    app.dependency_overrides[incident_work_backend] = lambda: work_backend
    endpoint = "/v1/devops/incident-sources/shared-source-name/events"
    incident = {"event_id": "event-1", "question": "Why is the service down?", "severity": "high", "environment_ref": reference("b", "resource")}
    body = json.dumps(incident, sort_keys=True).encode()
    signature = "sha256=" + hmac.new(secrets["secret-b"].encode(), body, sha256).hexdigest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        forged = await client.post(endpoint, content=body, headers={"X-DevOps-Signature": "sha256=" + "0" * 64})
        assert forged.status_code == 401
        foreign = json.dumps({**incident, "event_id": "foreign-event", "environment_ref": reference("a", "resource")}, sort_keys=True).encode()
        foreign_signature = "sha256=" + hmac.new(secrets["secret-b"].encode(), foreign, sha256).hexdigest()
        wrong_project = await client.post(endpoint, content=foreign, headers={"X-DevOps-Signature": foreign_signature})
        assert wrong_project.status_code == 422 and not queue
        filtered = {**incident, "event_id": "event-filtered", "severity": "low"}
        filtered_body = json.dumps(filtered, sort_keys=True).encode()
        filtered_signature = "sha256=" + hmac.new(secrets["secret-b"].encode(), filtered_body, sha256).hexdigest()
        filtered_reply = await client.post(endpoint, content=filtered_body, headers={"X-DevOps-Signature": filtered_signature})
        assert filtered_reply.status_code == 409 and not queue
        selected.publish = None
        unavailable = await client.post(endpoint, content=body, headers={"X-DevOps-Signature": signature})
        assert unavailable.status_code == 503 and not queue
        async with sessions() as independent:
            pending = (await independent.scalars(select(EventReceipt))).one()
            assert pending.state == "pending" and pending.run_id is None
            assert (await independent.scalars(select(Run))).all() == []
        selected.publish = publish
        grant.binding_generation = 4
        denied = await client.post(endpoint, content=body, headers={"X-DevOps-Signature": signature})
        assert denied.status_code == 403 and not queue
        async with sessions() as independent:
            assert (await independent.scalars(select(Run))).all() == []
        grant.binding_generation = 3
        accepted = await client.post(endpoint, content=body, headers={"X-DevOps-Signature": signature})
        assert accepted.status_code == 202 and accepted.json() == {
            "event_id": "event-1",
            "state": "admitted",
            "run_id": accepted.json()["run_id"],
        }
        assert accepted.json()["run_id"]
        unexpected_scope = await client.post(
            endpoint, content=json.dumps({**incident, "project_id": "project-a"}), headers={"X-DevOps-Signature": signature}
        )
        assert unexpected_scope.status_code == 401
        repeated = await client.post(endpoint, content=body, headers={"X-DevOps-Signature": signature})
        assert repeated.status_code == 202 and repeated.json() == accepted.json()
        async with sessions() as independent:
            version = await independent.get(ConfigurationVersion, ("tenant-b", "config-b", 1))
            settings = version.settings
            version.settings = {
                **settings,
                "devops": {
                    **settings["devops"],
                    "paused": True,
                    "trigger_policies": [{**settings["devops"]["trigger_policies"][0], "enabled": False}],
                },
            }
            await independent.commit()
        replay_while_paused = await client.post(endpoint, content=body, headers={"X-DevOps-Signature": signature})
        assert replay_while_paused.status_code == 202 and replay_while_paused.json() == accepted.json()
        new_event = json.dumps({**incident, "event_id": "event-after-pause"}, sort_keys=True).encode()
        new_signature = "sha256=" + hmac.new(secrets["secret-b"].encode(), new_event, sha256).hexdigest()
        refused = await client.post(endpoint, content=new_event, headers={"X-DevOps-Signature": new_signature})
        assert refused.status_code == 409
        oversized = await client.post(endpoint, content=b"x" * 16385, headers={"X-DevOps-Signature": signature})
        assert oversized.status_code == 413

    async with sessions() as independent:
        receipt = (await independent.scalars(select(EventReceipt))).one()
        assert (receipt.tenant_id, receipt.product_id, receipt.project_id) == (
            "tenant-b",
            "product-b",
            "project-b",
        )
        assert receipt.configuration_id == "config-b" and receipt.run_input["binding_id"] == "binding-b"
        run = (await independent.scalars(select(Run))).one()
        owed = (await independent.scalars(select(Work))).one()
        admitted = (await independent.scalars(select(RunReceipt))).one()
        assert receipt.run_id == run.run_id == owed.run_id == admitted.run_id == accepted.json()["run_id"]
        assert receipt.state == "admitted" and run.admission["event_id"] == incident["event_id"]
        assert run.admission["scope"] == {
            "tenant_id": "tenant-b",
            "product_id": "product-b",
            "project_id": "project-b",
            "relationship_revision": "1",
            "owner_id": "owner-b",
            "visibility": "only_me",
        }
        assert run.admission["binding_generation"] == 3 and run.admission["actor_id"] == "owner-b"
        assert run.admission["event_source_ref"]["record_id"] == "record-b"
        assert queue == [({"schema_version": "1.0", "work_id": owed.work_id, "generation": 1}, 0)]
    async with sessions() as reset:
        version = await reset.get(ConfigurationVersion, ("tenant-b", "config-b", 1))
        version.settings = settings
        budget = await reset.get(BudgetConfig, "record-b")
        budget.budget_amount_usd = Decimal("2.00")
        await reset.commit()

    racing = True
    delivery = {**incident, "event_id": "event-delivered-twice"}
    delivery_body = json.dumps(delivery, sort_keys=True).encode()
    delivery_signature = "sha256=" + hmac.new(secrets["secret-b"].encode(), delivery_body, sha256).hexdigest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
        responses = await asyncio.gather(
            *(client.post(endpoint, content=delivery_body, headers={"X-DevOps-Signature": delivery_signature}) for _ in range(2))
        )
        assert arrivals == 2
        assert all(reply.status_code == 202 for reply in responses), [reply.text for reply in responses]
        assert responses[0].json() == responses[1].json()
        assert responses[0].json()["run_id"] != accepted.json()["run_id"]
        altered_body = json.dumps({**delivery, "question": "Different investigation"}, sort_keys=True).encode()
        altered_signature = "sha256=" + hmac.new(secrets["secret-b"].encode(), altered_body, sha256).hexdigest()
        conflict = await client.post(endpoint, content=altered_body, headers={"X-DevOps-Signature": altered_signature})
        assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"
    async with sessions() as independent:
        runs = (await independent.scalars(select(Run))).all()
        receipts = (await independent.scalars(select(EventReceipt))).all()
        admissions = (await independent.scalars(select(RunReceipt))).all()
        work = (await independent.scalars(select(Work))).all()
        assert len(runs) == len(receipts) == len(admissions) == len(work) == len(queue) == 2
        repeated = next(item for item in receipts if item.event_id == delivery["event_id"])
        assert repeated.state == "admitted" and repeated.run_id == responses[0].json()["run_id"]
        assert {item.run_id for item in runs} == {item.run_id for item in admissions} == {item.run_id for item in work}
        assert {message[0]["work_id"] for message in queue} == {item.work_id for item in work}
    await engine.dispose()


@pytest.mark.asyncio
async def test_gateway_submits_only_with_scoped_binding_and_delegation(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'manual.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    source = {
        "tenant_id": "tenant-manual",
        "project_id": "project-manual",
        "source_connection_id": "credential-manual",
        "record_type": "resource",
        "record_id": "resource-manual",
        "record_revision": "revision-one",
    }
    connection = {**source, "record_type": "connection", "record_id": "credential-manual"}
    scope = {
        "tenant_id": "tenant-manual",
        "product_id": "product-manual",
        "project_id": "project-manual",
        "relationship_revision": "1",
        "owner_id": "owner-manual",
        "visibility": "only_me",
    }
    request = {
        "service_id": "devops",
        "configuration_revision": 1,
        "binding_id": "binding-manual",
        "capability": "investigation",
        "devops_input": {"kind": "investigation", "question": "What failed?", "resource_refs": [source], "telemetry_refs": []},
    }
    policy = {**source, "record_type": "policy", "record_id": "policy-manual"}
    limits = {
        "deadline_seconds": 3600,
        "max_provider_attempts": 1,
        "max_polls": 10,
        "concurrency": 1,
        "max_cost": "1.00",
        "currency": "USD",
        "policy_ref": policy,
    }
    trigger = {
        "trigger_id": "trigger-manual",
        "kind": "manual",
        "enabled": True,
        "capability": "investigation",
        "binding_id": "binding-manual",
        "resource_refs": [source],
        "telemetry_refs": [],
    }
    account = "1" * 12
    role = f"arn:aws:iam::{account}:role/example"
    async with engine.begin() as connection_session:
        await connection_session.run_sync(Base.metadata.create_all)
        await connection_session.run_sync(BudgetConfig.__table__.create)
        await connection_session.run_sync(BudgetUsage.__table__.create)
    async with sessions() as session:
        session.add(
            BudgetConfig(
                id="policy-manual",
                org_id="tenant-manual",
                entity_type="org",
                entity_id="tenant-manual",
                period_type="daily",
                budget_amount_usd=Decimal("5.00"),
                enforcement_mode="hard",
            )
        )
        session.add(
            Configuration(
                tenant_id="tenant-manual",
                configuration_id="config-manual",
                product_id="product-manual",
                project_id="project-manual",
                service_id="devops",
                relationship_revision="1",
                owner_id="owner-manual",
                current_revision=1,
                handoff_state="linked",
            )
        )
        session.add(
            ConfigurationVersion(
                tenant_id="tenant-manual",
                configuration_id="config-manual",
                revision=1,
                state="ready",
                hash="f" * 64,
                inherited_fields=[],
                gaps=[],
                settings={
                    "enabled": True,
                    "visibility": "only_me",
                    "classification": "internal",
                    "profile_ref": {**source, "record_type": "product"},
                    "limits": limits,
                    "devops": {"hosting_connection_ref": connection, "region": "us-east-1", "trigger_policies": [trigger]},
                },
            )
        )
        session.add(
            Binding(
                tenant_id="tenant-manual",
                binding_id="binding-manual",
                configuration_id="config-manual",
                product_id="product-manual",
                project_id="project-manual",
                service_id="devops",
                scope=scope,
                revision=1,
                generation=3,
                state="ready",
                ownership="adopted",
                connection_ref=connection,
                connection_key="a" * 64,
                native_key="b" * 64,
                native_resource_ref={**source, "record_type": "agent_space"},
                provider_identity={"account_id": account, "role_arn": role, "region": "us-east-1"},
                readiness=[],
                gaps=[],
            )
        )
        await session.commit()

    actor = SimpleNamespace(org_id="tenant-manual", user_id="owner-manual", account_type="human")
    credential = SimpleNamespace(
        id="credential-manual",
        org_id="tenant-manual",
        user_id="owner-manual",
        service="aws",
        credential_type="aws_role",
        aws_verified_version_id="revision-one",
        scopes={"account_id": account, "role_arn": role},
    )
    grant = SimpleNamespace(
        id="grant-manual",
        execution_scope=None,
        revision=2,
        operations=["credential:session"],
        capabilities=["investigation"],
        binding_generation=3,
        sources=[{**connection, "record_type": "credential"}],
    )

    async def issue_child(*arguments, previous_id=None):
        assert arguments[2] is grant
        assert arguments[6] == [] and arguments[7] == "investigation"
        assert previous_id is None
        return SimpleNamespace(id="manual-work-grant", revision=1)

    monkeypatch.setattr("src.l5_integration.issue_l5_work_delegation", issue_child)
    monkeypatch.setattr("src.l5_integration.verified_connection_evidence", lambda observed: observed is credential)
    queue = []

    async def publish(envelope, delay):
        queue.append((envelope, delay))

    class AuthoritySession:
        def __init__(self, session):
            self.session = session
            self.bind = engine

        def __getattr__(self, name):
            return getattr(self.session, name)

        async def get(self, model, identifier, **kwargs):
            if model is UserCredential:
                return credential if identifier == credential.id else None
            return await self.session.get(model, identifier, **kwargs)

        async def scalar(self, statement):
            if statement.column_descriptions[0]["entity"] is CredentialServiceDelegation:
                return grant
            return await self.session.scalar(statement)

    async with sessions() as session:
        backend = GatewayConfigurationBackend(AuthoritySession(session), actor, publish=publish)

        async def current(product_id, project_id, service_id):
            if (product_id, project_id, service_id) != ("product-manual", "project-manual", "devops"):
                raise HTTPException(404, detail={"code": "not_found"})
            configuration = await session.get(Configuration, ("tenant-manual", "config-manual"))
            version = await session.get(ConfigurationVersion, ("tenant-manual", "config-manual", 1))
            return configuration, version

        backend.current = current
        app = FastAPI()
        app.include_router(router)
        from src.l5_integration import backend as backend_dependency

        app.dependency_overrides[backend_dependency] = lambda: backend
        endpoint = "/v1/products/product-manual/projects/project-manual/services/devops/runs"
        headers = {"Idempotency-Key": "manual-gateway-key-00001"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
            wrong_binding = await client.post(endpoint, json={**request, "binding_id": "binding-other"}, headers=headers)
            assert wrong_binding.status_code == 404
            grant.binding_generation = 4
            denied = await client.post(endpoint, json=request, headers=headers)
            assert denied.status_code == 403
            grant.binding_generation = 3
            submitted = await client.post(endpoint, json=request, headers=headers)
            assert submitted.status_code == 202, submitted.text
            replay = await client.post(endpoint, json=request, headers=headers)
            assert replay.status_code == 202 and replay.json() == submitted.json()
            wrong_project = await client.post(endpoint.replace("project-manual", "other-project"), json=request, headers=headers)
            assert wrong_project.status_code == 404
            configuration = await session.get(ConfigurationVersion, ("tenant-manual", "config-manual", 1))
            original_settings = configuration.settings
            configuration.settings = {**original_settings, "devops": {**original_settings["devops"], "paused": True}}
            await session.commit()
            paused = await client.post(endpoint, json=request, headers={"Idempotency-Key": "manual-paused-00001"})
            assert paused.status_code == 409 and paused.json()["detail"]["code"] == "trigger_paused"
            prior = await client.post(endpoint, json=request, headers=headers)
            assert prior.status_code == 202 and prior.json() == submitted.json()
            configuration.settings = original_settings
            budget = await session.get(BudgetConfig, "policy-manual")
            budget.budget_amount_usd = Decimal("1.00")
            await session.commit()
            exhausted = await client.post(endpoint, json=request, headers={"Idempotency-Key": "manual-exhausted-00001"})
            assert exhausted.status_code == 202, exhausted.text
            budget.budget_amount_usd = Decimal("5.00")
            session.add(
                BudgetUsage(
                    org_id="tenant-manual",
                    entity_type="org",
                    entity_id="tenant-manual",
                    period_type="daily",
                    period_start=datetime.now(UTC).date(),
                    total_cost_usd=Decimal("5.00"),
                    total_tokens=0,
                    request_count=1,
                )
            )
            await session.commit()
            spent = await client.post(endpoint, json=request, headers={"Idempotency-Key": "manual-spent-00001"})
            assert spent.status_code == 202, spent.text
            prior = await client.post(endpoint, json=request, headers=headers)
            assert prior.status_code == 202 and prior.json() == submitted.json()
        async with sessions() as independent:
            runs = (await independent.scalars(select(Run))).all()
            receipts = (await independent.scalars(select(RunReceipt))).all()
            work = (await independent.scalars(select(Work))).all()
            expected = {response.json()["run_id"] for response in (submitted, exhausted, spent)}
            assert len(runs) == len(receipts) == len(work) == len(queue) == 3
            assert {run.run_id for run in runs} == {receipt.run_id for receipt in receipts} == {item.run_id for item in work} == expected
            assert all(
                run.admission["scope"] == scope and run.admission["delegation_id"] == "manual-work-grant" and run.input == request for run in runs
            )
            assert {message[0]["work_id"] for message in queue} == {item.work_id for item in work}
        await session.rollback()

    async with sessions() as reset:
        selected_budget = await reset.get(BudgetConfig, "policy-manual")
        selected_budget.budget_amount_usd = Decimal("2.00")
        spent = (await reset.scalars(select(BudgetUsage))).one()
        spent.total_cost_usd = Decimal("0.00")
        await reset.commit()

    both_ready = asyncio.Event()
    arrivals = 0

    class RacingBackend(GatewayConfigurationBackend):
        async def _require_devops_budget(self, database, limits, selected_scope, now, replay):
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                both_ready.set()
            await asyncio.wait_for(both_ready.wait(), timeout=5)
            return await super()._require_devops_budget(database, limits, selected_scope, now, replay)

    async def competing_request(key):
        async with sessions() as separate:
            service = RacingBackend(AuthoritySession(separate), actor, publish=publish)

            async def current(product_id, project_id, service_id):
                assert (product_id, project_id, service_id) == ("product-manual", "project-manual", "devops")
                configuration = await separate.get(Configuration, ("tenant-manual", "config-manual"))
                version = await separate.get(ConfigurationVersion, ("tenant-manual", "config-manual", 1))
                return configuration, version

            service.current = current
            try:
                result = await service.submit("product-manual", "project-manual", "devops", request, key)
                return result.run_id
            except HTTPException as error:
                return error.detail["code"]

    contenders = await asyncio.gather(competing_request("concurrent-first-00001"), competing_request("concurrent-second-00001"))
    assert arrivals == 0 and "budget_exhausted" not in contenders
    async with sessions() as independent:
        runs = (await independent.scalars(select(Run).order_by(Run.run_id))).all()
        receipts = (await independent.scalars(select(RunReceipt))).all()
        queued = (await independent.scalars(select(Work))).all()
        assert len(runs) == len(receipts) == len(queued) == len(queue) == 5
        assert {item.run_id for item in runs} == {item.run_id for item in receipts} == {item.run_id for item in queued}
        assert len(set(contenders)) == 2 and set(contenders) <= {item.run_id for item in runs}
        assert all(item.admission["limits"]["max_cost"] == "1.00" for item in runs)
    await engine.dispose()
