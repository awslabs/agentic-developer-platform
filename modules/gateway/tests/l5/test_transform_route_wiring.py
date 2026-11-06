"""The gateway selects a qualified Transform work path without client-side execution."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, Request
from l5_services.models.base import Base
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src import l5_integration
from tests.internal.test_l5_security_tools import compiled_dispatch_catalogue  # noqa: F401


@pytest.mark.parametrize("publisher_source", ("export", "queue", "missing", "invalid"))
@pytest.mark.parametrize("authority_kind", ("binder", "callback", "missing", "invalid", "invalid_bound"))
def test_transform_publisher_is_selected_after_request_authority_binding(monkeypatch, publisher_source, authority_kind):
    authority = AsyncMock()

    class Authority:
        def bind_backend(self, selected):
            assert selected.actor is actor
            return authority if authority_kind == "binder" else object()

    actor = SimpleNamespace(org_id="tenant-a", user_id="owner-a", account_type="human")
    app = FastAPI()
    app.state.l5_transform_work_authority = {
        "binder": Authority(),
        "callback": authority,
        "missing": None,
        "invalid": object(),
        "invalid_bound": Authority(),
    }[authority_kind]
    app.state.l5_transform_artifact_store = SimpleNamespace(read=AsyncMock())
    publisher = AsyncMock()
    if publisher_source == "export":
        app.state.l5_transform_work_publisher = publisher
    elif publisher_source == "invalid":
        app.state.l5_transform_work_publisher = object()
    monkeypatch.setattr(l5_integration, "service_publisher", lambda service_id: publisher if publisher_source == "queue" else None)

    selected = l5_integration.backend(
        "transform", Request({"type": "http", "app": app}), db=SimpleNamespace(bind=None), actor=actor, x_l5_delegation_id=None
    )

    assert selected.transform_result_reader_ready
    if authority_kind in {"binder", "callback"}:
        assert selected.transform_work_authority is authority
    if authority_kind in {"binder", "callback"} and publisher_source in {"export", "queue"}:
        assert selected.publish is publisher
        assert selected.transform_submission_ready
    else:
        assert selected.publish is None
        assert not selected.transform_submission_ready
    authority.assert_not_called()
    publisher.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("configuration_change", ["create", "edit", "refresh"])
async def test_transform_host_gates_dispatch_and_scopes_result_reader(monkeypatch, configuration_change):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as database:
        await database.run_sync(Base.metadata.create_all)
    actor = SimpleNamespace(org_id="tenant-a", user_id="owner-a", account_type="human")
    app = FastAPI()
    request = Request({"type": "http", "app": app})
    reader_calls = []

    class ArtifactStore:
        async def read(self, *, reference, authority):
            reader_calls.append((reference, authority))
            return b"original patch"

    async def publisher(envelope, delay):
        raise AssertionError("route setup must not publish")

    async def work_authority(configuration, version, binding, run_input, authority):
        reader_calls.append(("qualified", run_input["sources"]))
        verified = {
            "scope": authority["scope"],
            "connection_ref": authority["connection_ref"],
            "configuration_id": configuration.configuration_id,
            "configuration_revision": version.revision,
            "binding_id": binding.binding_id,
            "binding_generation": binding.generation,
        }
        return verified, SimpleNamespace(pin_source=lambda *args, **kwargs: None)

    scope = {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a"}
    identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/example", "region": "example-region"}
    connection = {
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "source_connection_id": "connection-a",
        "record_id": "connection-a",
        "record_revision": "1",
        "record_type": "connection",
    }
    source = {**connection, "record_id": "repo-a", "record_type": "repository_commit"}
    profile_ref = {**connection, "record_id": "product-a", "record_type": "product"}
    executor = {**connection, "record_id": "executor-a", "record_type": "executor_binding"}
    choices = {
        "service_id": "transform",
        "enabled": True,
        "capabilities": ["refactor"],
        "transform": {
            "connection_ref": connection,
            "executor_binding_ref": executor,
            "test_policy_ref": {**connection, "record_id": "policy-a", "record_type": "test_policy"},
            "region": "example-region",
            "read_paths": ["src"],
            "write_paths": ["src"],
            "excluded_paths": [".git"],
            "goal_defaults": {"kind": "refactor", "summary": "Improve structure"},
        },
    }
    profile = {
        "tenant_id": "tenant-a",
        "product_id": "product-a",
        "project_id": "project-a",
        "revision": 1,
        "configuration": {"basic": {"repositories": [source]}, "selected_capabilities": [{"capability": "refactor", "ownership_mode": "managed"}]},
    }
    available = {
        "coverage": "complete",
        "next_cursor": None,
        "items": [
            {
                "connection_ref": {"kind": "credential", "id": "connection-a"},
                "record_ref": connection,
                "resources": [source],
            }
        ],
    }
    binding = SimpleNamespace(
        state="ready", gaps=[], scope=scope, connection_ref=connection, binding_id="binding-a", generation=4, provider_identity=dict(identity)
    )

    async def current(self, product_id, project_id, service_id):
        assert (product_id, project_id, service_id) == ("product-a", "project-a", "transform")
        return await self.store.current(product_id, project_id, service_id)

    async def qualification(tenant_id, product_id, project_id, selected):
        assert (tenant_id, product_id, project_id, selected) == ("tenant-a", "product-a", "project-a", executor)
        return {"status": "qualified", "executor_binding_ref": executor}

    async def binding_authority(self, product_id, project_id, service_id):
        return SimpleNamespace(binding=lambda binding_id: get_binding(binding_id)), {"scope": scope, "connection_ref": connection, **identity}

    async def get_binding(binding_id):
        assert binding_id == "binding-a"
        return binding

    async def permitted_sources(db, selected_actor, project_id):
        assert selected_actor is actor and project_id == "project-a"
        return {("connection-a", "repo-a")}

    async def list_runs(self, product_id, project_id, service_id, cursor=None):
        return []

    monkeypatch.setattr(l5_integration.GatewayConfigurationBackend, "current", current)
    monkeypatch.setattr(l5_integration.GatewayConfigurationBackend, "binding_authority", binding_authority)
    monkeypatch.setattr(l5_integration.GatewayConfigurationBackend, "list_runs", list_runs)
    monkeypatch.setattr(l5_integration, "permitted_source_refs", permitted_sources)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:

            def selected():
                return l5_integration.backend("transform", request, db=session, actor=actor, x_l5_delegation_id=None)

            gateway = selected()
            config, version = await gateway.store.create("product-a", "project-a", "transform", "1", profile, profile_ref, available, choices)
            if configuration_change == "edit":
                config, version = await gateway.store.edit(config, version, profile, available, {"enabled": True}, f'"{version.hash}"')
            elif configuration_change == "refresh":
                config, version = await gateway.store.refresh(config, version, profile, profile_ref, available, f'"{version.hash}"')
            config.handoff_state = "linked"
            await session.commit()
            binding.configuration_id = config.configuration_id
            assert version.state == "needs_input" and version.gaps == ["executor_unqualified"]
            assert not gateway.transform_submission_ready and not gateway.transform_result_reader_ready
            with pytest.raises(HTTPException) as error:
                await gateway.result_store("product-a", "project-a", "transform")
            assert error.value.status_code == 503
            app.state.l5_transform_work_publisher = publisher
            app.state.l5_transform_work_authority = work_authority
            gateway = selected()
            assert not gateway.transform_submission_ready
            app.state.l5_transform_artifact_store = ArtifactStore()
            gateway = selected()
            assert gateway.transform_submission_ready and gateway.transform_result_reader_ready
            run_input = {"sources": [source], "binding_id": "binding-a"}
            assert not (await gateway.check("product-a", "project-a", "transform"))["ready"]
            with pytest.raises(HTTPException) as error:
                await gateway.admission_authority("product-a", "project-a", "transform", run_input)
            assert error.value.status_code == 409 and reader_calls == []
            app.state.l5_transform_executor_qualification_reader = qualification
            gateway = selected()
            checked = await gateway.check("product-a", "project-a", "transform")
            assert checked["ready"] and checked["gaps"] == [] and not checked["provider_contacted"]
            assert version.state == "needs_input" and version.gaps == ["executor_unqualified"]
            verified, source_client = await gateway.admission_authority("product-a", "project-a", "transform", run_input)
            assert verified["provider_identity"] == identity
            assert verified["provider_identity"] is not binding.provider_identity
            assert verified["binding_generation"] == 4 and callable(source_client.pin_source)
            assert reader_calls == [("qualified", run_input["sources"])]
            result_store = await gateway.result_store("product-a", "project-a", "transform")
            assert result_store.transform_reader_ready
            assert result_store.artifact_authority == {
                "tenant_id": "tenant-a",
                "actor_id": "owner-a",
                "product_id": "product-a",
                "project_id": "project-a",
            }
            with pytest.raises(HTTPException) as error:
                await gateway.admission_authority(
                    "product-a", "project-a", "transform", {**run_input, "sources": [{"source_connection_id": "source-a", "record_id": "foreign"}]}
                )
            assert error.value.status_code == 404 and len(reader_calls) == 1
            for component in ("publish", "transform_work_authority", "transform_artifact_store"):
                installed = getattr(gateway, component)
                setattr(gateway, component, None)
                checked = await gateway.check("product-a", "project-a", "transform")
                assert not checked["ready"] and "submission_unavailable" in checked["gaps"]
                with pytest.raises(HTTPException) as error:
                    await gateway.admission_authority("product-a", "project-a", "transform", run_input)
                assert error.value.status_code == 503 and len(reader_calls) == 1
                setattr(gateway, component, installed)
            for observation in (None, {"status": "qualified", "executor_binding_ref": {**executor, "record_revision": "2"}}):

                async def unqualified(*args):
                    return observation

                gateway.transform_qualification_reader = unqualified
                assert not (await gateway.check("product-a", "project-a", "transform"))["ready"]
                with pytest.raises(HTTPException) as error:
                    await gateway.admission_authority("product-a", "project-a", "transform", run_input)
                assert error.value.status_code == 409 and len(reader_calls) == 1
            gateway.transform_qualification_reader = qualification
            config, version = await gateway.store.edit(config, version, profile, available, {"enabled": False}, f'"{version.hash}"')
            config.handoff_state = "linked"
            await session.commit()
            assert not (await gateway.check("product-a", "project-a", "transform"))["ready"]
            with pytest.raises(HTTPException) as error:
                await gateway.admission_authority("product-a", "project-a", "transform", run_input)
            assert error.value.status_code == 409 and len(reader_calls) == 1
            config, version = await gateway.store.edit(config, version, profile, available, {"enabled": True}, f'"{version.hash}"')
            config.handoff_state = "linked"
            await session.commit()
            for invalid_identity in (
                None,
                {},
                {**identity, "account_id": "000000000000"},
                {**identity, "role_arn": "arn:aws:iam::123456789012:role/other"},
                {**identity, "region": "other-region"},
            ):
                binding.provider_identity = invalid_identity
                with pytest.raises(HTTPException) as error:
                    await gateway.admission_authority("product-a", "project-a", "transform", run_input)
                assert error.value.status_code == 409 and len(reader_calls) == 1
            binding.provider_identity = dict(identity)

            async def conflicting_authority(*args):
                result, source_client = await work_authority(*args)
                return {**result, "provider_identity": {**identity, "region": "other-region"}}, source_client

            gateway.transform_work_authority = conflicting_authority
            with pytest.raises(HTTPException) as error:
                await gateway.admission_authority("product-a", "project-a", "transform", run_input)
            assert error.value.status_code == 409
            reader_calls.pop()
            actor.account_type = "service"
            assert not selected().transform_result_reader_ready
            with pytest.raises(HTTPException) as error:
                await selected().admission_authority("product-a", "project-a", "transform", run_input)
            assert error.value.status_code == 404 and len(reader_calls) == 1
    finally:
        await engine.dispose()


@pytest.mark.parametrize("authorized", [True, False])
async def test_transform_mcp_supported_route_preserves_owner_authority(monkeypatch, authorized, compiled_dispatch_catalogue):  # noqa: F811
    actor = SimpleNamespace(org_id="tenant-a", user_id="owner-a", account_type="human")
    app = FastAPI()
    request = Request({"type": "http", "app": app, "headers": []})
    db = SimpleNamespace(bind=None)
    monkeypatch.setattr(l5_integration, "l5_actor", AsyncMock(return_value=actor))
    backend = l5_integration.GatewayConfigurationBackend(db, actor)
    backend.current = AsyncMock(
        side_effect=None if authorized else HTTPException(404, detail={"code": "not_found"}), return_value=(SimpleNamespace(), SimpleNamespace())
    )

    async def check(product_id, project_id, service_id):
        await backend.current(product_id, project_id, service_id)
        return {"ready": False, "gaps": ["executor_unqualified"], "provider_contacted": False}

    backend.check = check
    monkeypatch.setattr(l5_integration, "backend", lambda *args, **kwargs: backend)
    result = await l5_integration.call_l5_mcp_tool(
        l5_integration.L5ToolCall(
            tool="l5___check_configuration",
            arguments={
                "product_id": "product-a",
                "project_id": "project-a",
                "service_id": "transform",
            },
        ),
        request,
        actor=actor,
        db=db,
    )
    backend.current.assert_awaited_once_with("product-a", "project-a", "transform")
    assert result["structuredContent"]["http_status"] == (200 if authorized else 404)
    if not authorized:
        assert result["isError"] is True and result["structuredContent"]["error"]["code"] == "not_found"
