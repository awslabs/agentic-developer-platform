"""The gateway selects project releases only with qualified scoped evidence."""

import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from l5_services.api.router import create_router
from l5_services.models import AssessmentResult, Run, Work
from l5_services.models.base import Base
from l5_services.models.receipt import RunReceipt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src import l5_integration


@pytest.mark.asyncio
async def test_gateway_release_uses_project_repository_and_explicit_authority(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    reference = {
        "tenant_id": "tenant-one",
        "project_id": "project-one",
        "source_connection_id": "github-one",
        "record_type": "repository_commit",
        "record_id": "another-team/project",
        "record_revision": "head-123",
    }
    hosting = {**reference, "source_connection_id": "aws-one", "record_type": "connection", "record_id": "aws-one"}
    space = {**hosting, "record_type": "agent_space", "record_id": "space-one"}
    scope = {"tenant_id": "tenant-one", "project_id": "project-one"}
    trigger = {
        "kind": "manual",
        "enabled": True,
        "capability": "release_readiness",
        "binding_id": "binding-one",
        "source_refs": [{**reference, "record_revision": "configured-head"}],
    }
    investigation = {
        "kind": "manual",
        "enabled": True,
        "capability": "investigation",
        "binding_id": "binding-one",
        "resource_refs": [],
        "telemetry_refs": [],
    }
    version = SimpleNamespace(
        revision=1,
        state="ready",
        gaps=[],
        hash="configuration-one",
        inherited_fields=[],
        settings={
            "enabled": True,
            "visibility": "only_me",
            "capabilities": ["release_readiness"],
            "devops": {"trigger_policies": [trigger, investigation]},
        },
    )
    config = SimpleNamespace(
        owner_id="owner-one",
        handoff_state="linked",
        configuration_id="configuration-one",
        tenant_id="tenant-one",
        product_id="product-one",
        project_id="project-one",
        relationship_revision="1",
    )
    options = {"kind": "release_readiness", "repository_ref": reference, "base_revision": "base-123", "head_revision": "head-123"}
    request = {
        "service_id": "devops",
        "configuration_revision": 1,
        "capability": "release_readiness",
        "binding_id": "binding-one",
        "devops_input": options,
    }
    eligibility_ref = {**reference, "record_type": "native_eligibility", "record_id": "check-one"}
    eligibility = {
        "status": "ready",
        "task_permission": "allowed",
        "source_indexed": True,
        "permission_action": "CreateBacklogTask",
        "permission_space_id": "space-one",
        "indexed_revisions": ["base-123", "head-123"],
        "repository_ref": reference,
        "base_revision": "base-123",
        "head_revision": "head-123",
        "eligibility_ref": eligibility_ref,
        "association_id": "association-one",
        "reference_id": "project-one",
        "reference_url": "https://offline.invalid/change/123",
        "deployment_context": {"target": "staging", "revision": "head-123"},
    }
    calls = []

    class Source:
        async def pin_source(self, *args, **kwargs):
            raise AssertionError("admission is the only caller of pin_source")

    async def authority(run_input, verified, selected_version):
        assert run_input == request and selected_version is version
        assert verified["connection_ref"] == hosting
        calls.append("qualified")
        return {"eligibility": eligibility, "source_client": Source(), "max_bytes": 4096, "max_files": 10}

    async def publish(*args):
        pass

    allowed = {("github-one", "another-team/project")}

    async def permitted(db, actor, project_id):
        assert project_id == "project-one" and actor.org_id == "tenant-one"
        return allowed

    monkeypatch.setattr(l5_integration, "permitted_source_refs", permitted)

    async with async_sessionmaker(engine)() as session:
        actor = SimpleNamespace(org_id="tenant-one", user_id="owner-one", account_type="human")
        backend = l5_integration.GatewayConfigurationBackend(session, actor, publish=publish, release_authority=authority)

        async def current(*args):
            return config, version

        async def target(configuration, selected_version, selected_trigger, **kwargs):
            assert selected_trigger in (trigger, investigation)
            return {"scope": scope, "connection_ref": hosting, "native_space_ref": space, "trigger": selected_trigger}, None

        backend.current = current
        backend._devops_work_target = target
        verified, source = await backend.admission_authority("product-one", "project-one", "devops", request)
        assert verified["release_eligibility"] == eligibility
        assert verified["adapter_revision"] == "release-diff-v1" and isinstance(source, Source)
        assert calls == ["qualified"]

        backend.release_authority = None
        allowed.clear()
        with pytest.raises(HTTPException) as denied:
            await backend.admission_authority("product-one", "project-one", "devops", request)
        assert denied.value.status_code == 404 and calls == ["qualified"]
        allowed.add(("github-one", "another-team/project"))
        with pytest.raises(HTTPException) as unavailable:
            await backend.admission_authority("product-one", "project-one", "devops", request)
        assert unavailable.value.detail == {"code": "release_prerequisite_unavailable"}
        backend.release_authority = authority

        async def dependency():
            return backend

        app = FastAPI()
        app.include_router(create_router(dependency))
        url = "/v1/products/product-one/projects/project-one/services/devops/runs"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            checked = await client.post(url.removesuffix("/runs") + "/configuration/check")
            assert checked.status_code == 200 and checked.json()["provider_contacted"] is False
            assert checked.json()["ready"] is True and checked.json()["gaps"] == []
            assert "capability" not in checked.json()
            for name, update in (
                ("permission", {"task_permission": "denied"}),
                ("unindexed", {"source_indexed": False}),
                ("revision", {"indexed_revisions": ["base-123"]}),
            ):
                eligibility.update(update)
                prior = len(calls)
                denied = await client.post(url, json=request, headers={"Idempotency-Key": f"release-denied-{name}-00001"})
                assert denied.status_code == 409 and denied.json()["detail"]["code"] == "release_prerequisite_unavailable"
                assert len(calls) == prior + 1
                eligibility.update({"task_permission": "allowed", "source_indexed": True, "indexed_revisions": ["base-123", "head-123"]})
        assert (await session.scalars(select(Run))).all() == []
        assert (await session.scalars(select(Work))).all() == []
        assert (await session.scalars(select(AssessmentResult))).all() == []

        investigation_request = {
            "capability": "investigation",
            "binding_id": "binding-one",
            "devops_input": {"resource_refs": [], "telemetry_refs": []},
        }
        permitted, _ = await backend.admission_authority("product-one", "project-one", "devops", investigation_request)
        assert permitted["trigger"] == investigation
        assert calls == ["qualified", "qualified", "qualified", "qualified"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_release_replay_rechecks_product_repository_rights(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    reference = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "github-one", "record_id": "another-team/project"}
    run_input = {"service_id": "devops", "capability": "release_readiness", "devops_input": {"repository_ref": reference}}
    run = SimpleNamespace(
        run_id="run-one",
        configuration_id="config-one",
        product_id="product-one",
        project_id="project-one",
        input=run_input,
        admission={"exclusions": []},
        service_id="devops",
    )
    request_hash = hashlib.sha256(
        json.dumps({"input": run_input, "event_id": None, "exclusions": []}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    receipt = SimpleNamespace(run_id="run-one", request_hash=request_hash)

    class Session:
        bind = engine

        async def get(self, model, key):
            if model is RunReceipt:
                return receipt
            if model is Run:
                return run
            raise AssertionError("unexpected database lookup")

    allowed = set()

    async def permitted(db, actor, project_id):
        assert project_id == "project-one"
        return allowed

    monkeypatch.setattr(l5_integration, "permitted_source_refs", permitted)
    backend = l5_integration.GatewayConfigurationBackend(Session(), SimpleNamespace(org_id="tenant-one", user_id="owner-one", account_type="human"))

    async def current(*args):
        return SimpleNamespace(owner_id="owner-one", configuration_id="config-one"), None

    backend.current = current
    with pytest.raises(HTTPException) as denied:
        await backend.submit("product-one", "project-one", "devops", run_input, "release-replay-00001")
    assert denied.value.status_code == 404
    allowed.add(("github-one", "another-team/project"))
    assert await backend.submit("product-one", "project-one", "devops", run_input, "release-replay-00001") is run
    await engine.dispose()


@pytest.mark.asyncio
async def test_release_readback_checks_repository_rights_even_with_other_sources(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    allowed = {("github-one", "allowed/project")}

    async def permitted(db, actor, project_id):
        assert project_id == "project-one"
        return allowed

    monkeypatch.setattr(l5_integration, "permitted_source_refs", permitted)
    async with async_sessionmaker(engine)() as session:
        backend = l5_integration.GatewayConfigurationBackend(session, SimpleNamespace(org_id="tenant-one", user_id="owner-one"))
        reference = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "github-one", "record_id": "allowed/project"}
        run = SimpleNamespace(
            service_id="devops",
            input={"capability": "release_readiness", "devops_input": {"repository_ref": reference}, "sources": [{"record_id": "forged"}]},
        )
        assert await backend.run_source_rights("project-one", [run]) == [run]
        allowed.clear()
        assert await backend.run_source_rights("project-one", [run]) == []
        run.input["devops_input"]["repository_ref"] = None
        assert await backend.run_source_rights("project-one", [run]) == []
    await engine.dispose()
