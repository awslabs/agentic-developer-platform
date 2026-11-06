"""Retained Transform evidence and replay remain subject to current authority."""

import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from l5_services.api.router import create_router
from l5_services.models.base import Base as L5Base
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.evidence import AssessmentResult
from l5_services.models.receipt import RunReceipt
from l5_services.models.result import ResultDetail
from l5_services.models.run import Run
from l5_services.models.work import Work
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src import l5_integration
from src.internal import tools_l5
from src.l5_service_identity import worker_id
from src.shared.models.base import Base as GatewayBase
from src.shared.models.vault import CredentialServiceDelegation

ROOT = "/v1/products/product-a/projects/project-a/services/transform"
SCOPE = {
    "tenant_id": "tenant-a",
    "product_id": "product-a",
    "project_id": "project-a",
    "relationship_revision": "1",
    "owner_id": "owner-a",
    "visibility": "product_team",
}
CONNECTION = {
    "tenant_id": "tenant-a",
    "project_id": "project-a",
    "source_connection_id": "connection-a",
    "record_type": "connection",
    "record_id": "connection-a",
    "record_revision": "version-a",
}
SOURCE = {**CONNECTION, "record_type": "repository_commit", "record_id": "repo-a"}
INPUT = {
    "service_id": "transform",
    "configuration_revision": 2,
    "binding_id": "binding-a",
    "capability": "refactor",
    "sources": [SOURCE],
    "transform_input": {"goal": {"kind": "refactor", "summary": "Improve structure"}},
}
PATCH = b"diff --git a/source b/source\n+new\n"
REFERENCE = {
    "tenant_id": "tenant-a",
    "project_id": "project-a",
    "artifact_id": "artifact-a",
    "version": "1",
    "sha256": hashlib.sha256(PATCH).hexdigest(),
    "bytes": len(PATCH),
    "media_type": "text/x-diff",
    "expires_at": "2030-01-01T00:00:00Z",
}
RESULT = {
    "service_id": "transform",
    "result_id": "result-a",
    "run_id": "run-a",
    "revision": 1,
    "scope": SCOPE,
    "verification_status": "partial",
    "tests_before": [{"status": "passed"}],
    "tests_after": [{"status": "failed"}],
    "limitations": ["candidate check failed"],
    "cost_status": "unknown",
    "original_patch": {"sha256": REFERENCE["sha256"], "bytes": len(PATCH)},
    "retained_artifacts": {"original_patch": REFERENCE},
}
KEY = "transform-replay-key-001"
EXPIRES = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture
async def boundary(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(GatewayBase.metadata.create_all)
        await connection.run_sync(L5Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        configuration = Configuration(
            tenant_id="tenant-a",
            configuration_id="config-a",
            product_id="product-a",
            project_id="project-a",
            service_id="transform",
            relationship_revision="1",
            owner_id="owner-a",
            current_revision=2,
            handoff_state="linked",
        )
        db.add(configuration)
        db.add(
            ConfigurationVersion(
                tenant_id="tenant-a",
                configuration_id="config-a",
                revision=2,
                settings={"visibility": "product_team", "source_refs": [SOURCE], "enabled": True},
                inherited_fields=[],
                gaps=[],
                state="ready",
                hash="a" * 64,
            )
        )
        run = Run(
            tenant_id="tenant-a",
            run_id="run-a",
            product_id="product-a",
            project_id="project-a",
            service_id="transform",
            configuration_id="config-a",
            configuration_revision=2,
            binding_id="binding-a",
            trigger_id="trigger-a",
            binding_generation=1,
            revision=2,
            input=INPUT,
            admission={"scope": SCOPE, "connection_ref": CONNECTION, "delegation_id": "grant-a", "delegation_version": 1, "exclusions": []},
            scope=SCOPE,
            deadline_at=EXPIRES,
            state="outcome_unknown",
            provider_state="unknown",
        )
        db.add(run)
        db.add(
            Binding(
                tenant_id="tenant-a",
                binding_id="binding-a",
                configuration_id="config-a",
                product_id="product-a",
                project_id="project-a",
                service_id="transform",
                scope=SCOPE,
                revision=1,
                generation=1,
                state="ready",
                ownership="customer_linked",
                connection_ref=CONNECTION,
                connection_key="a" * 64,
                readiness=[],
                gaps=[],
            )
        )
        grant = CredentialServiceDelegation(
            id="grant-a",
            tenant_id="tenant-a",
            credential_id="connection-a",
            service_id=worker_id("transform"),
            product_id="product-a",
            project_id="project-a",
            created_by="owner-a",
            operations=["credential:session"],
            sources=[{**CONNECTION, "record_type": "credential"}],
            capabilities=["refactor"],
            visibility="restricted",
            revision=1,
            binding_generation=1,
            expires_at=EXPIRES,
        )
        db.add(grant)
        request_hash = hashlib.sha256(
            json.dumps({"input": INPUT, "event_id": None, "exclusions": []}, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        db.add(
            RunReceipt(
                tenant_id="tenant-a",
                product_id="product-a",
                project_id="project-a",
                service_id="transform",
                actor_id="owner-a",
                operation="submit",
                idempotency_key=KEY,
                request_hash=request_hash,
                run_id="run-a",
                expires_at=EXPIRES,
            )
        )
        db.add(AssessmentResult(tenant_id="tenant-a", result_id="result-a", run_id="run-a", version=1, payload=RESULT))
        db.add(ResultDetail(tenant_id="tenant-a", result_id="result-a", payload=RESULT))
        await db.commit()
        actor = SimpleNamespace(org_id="tenant-a", user_id="owner-a", account_type="human")
        rights = {("connection-a", "repo-a")}
        connections = {"connection-a"}
        effects = []
        active = {"membership": True}

        async def profile(self, product_id, project_id):
            if not active["membership"]:
                raise HTTPException(404, detail={"code": "not_found"})
            return {"tenant_id": "tenant-a", "project_id": "project-a", "revision": 1}

        async def relationship(self, product_id, project_id):
            return SimpleNamespace(relationship_revision=1, source_connection_id="connection-a")

        async def sources(self, project_id):
            return rights

        async def permitted(db_session, request_actor, project_id):
            return rights

        async def authorized(db_session, request_actor):
            return [({"provider": "aws", "connection_ref": {"id": selected}}, None) for selected in connections]

        class ArtifactStore:
            async def read(self, *, reference, authority):
                effects.append((reference, authority))
                return PATCH

        monkeypatch.setattr(l5_integration.GatewayConfigurationBackend, "profile", profile)
        monkeypatch.setattr(l5_integration.GatewayConfigurationBackend, "relationship", relationship)
        monkeypatch.setattr(l5_integration.GatewayConfigurationBackend, "source_rights", sources)
        monkeypatch.setattr(l5_integration, "permitted_source_refs", permitted)
        monkeypatch.setattr(l5_integration, "authorized_connections", authorized)
        publisher = AsyncMock(side_effect=AssertionError("replay must not publish"))
        work_authority = AsyncMock(side_effect=AssertionError("replay must not requalify"))
        backend = l5_integration.GatewayConfigurationBackend(
            db, actor, publish=publisher, transform_work_authority=work_authority, transform_artifact_store=ArtifactStore()
        )
        monkeypatch.setattr(tools_l5, "files", lambda package: Path(__file__).resolve().parents[4] / "docs/l5-services")
        tools_l5.l5_tool_catalogue.cache_clear()
        tools_l5.l5_operations.cache_clear()
        app = FastAPI()
        app.include_router(create_router(lambda: backend))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield SimpleNamespace(
                db=db,
                actor=actor,
                rights=rights,
                connections=connections,
                grant=grant,
                binding_id="binding-a",
                backend=backend,
                client=client,
                effects=effects,
                publisher=publisher,
                work_authority=work_authority,
                configuration=configuration,
                active=active,
            )
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    await engine.dispose()


async def effect_counts(db):
    return tuple([await db.scalar(select(func.count()).select_from(model)) for model in (Run, Work, RunReceipt, AssessmentResult)])


def scoped_cli(boundary, monkeypatch):
    cli_path = Path(__file__).resolve().parents[2] / "cli/adp-service.py"
    spec = importlib.util.spec_from_file_location("transform_scoped_cli", cli_path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli.common, "access_token", lambda: "test-token")
    monkeypatch.setattr(cli.common, "capability_cache_context", lambda token=None: (None, {"tenant_claim": "tenant-a"}))
    requests = []

    class ScopedOpener:
        def open(self, request, timeout):
            target = urlsplit(request.full_url)
            path = target.path
            route = path + ("?" + target.query if target.query else "")
            body = json.loads(request.data) if request.data else None
            requests.append((request.get_method(), route, body, dict(request.header_items())))
            if path == "/me/cli-capabilities":
                payload = {
                    "schema_version": "2026-09-21",
                    "gateway": {"state": "yes", "release": "fixture"},
                    "tenant": {"org_id": "tenant-a"},
                    "operations": [
                        {
                            "id": "service.mutate",
                            "supported": "yes",
                            "enabled": "yes",
                            "permitted": "yes",
                            "ready": "yes",
                        }
                    ],
                }
                response = httpx.Response(200, json=payload)
            else:
                response = asyncio.run(boundary.client.request(request.get_method(), route, json=body, headers=dict(request.header_items())))
            if response.status_code >= 400:
                raise urllib.error.HTTPError(request.full_url, response.status_code, "denied", response.headers, io.BytesIO(response.content))
            stream = io.BytesIO(response.content)
            stream.status = response.status_code
            stream.headers = response.headers
            return stream

    client = object.__new__(cli.common.Api)
    client.binding = None
    client.base = "http://test"
    client.opener = ScopedOpener()
    client.last_etag = None
    return cli, client, requests


@pytest.mark.asyncio
async def test_client_parity(boundary, monkeypatch):
    """The installed clients read the same scoped SQL receipt and retained evidence as the API."""
    cli, client, requests = scoped_cli(boundary, monkeypatch)
    scope = ["--product", "product-a", "--project", "project-a", "--service", "transform", "--json"]
    mcp_scope = {"product_id": "product-a", "project_id": "project-a", "service_id": "transform"}

    async def cli_call(*arguments):
        return await asyncio.to_thread(cli.execute, cli.parser().parse_args([*arguments, *scope]), client)

    boundary.db.add(
        ConfigurationVersion(
            tenant_id="tenant-a",
            configuration_id="config-a",
            revision=1,
            settings={"visibility": "product_team", "source_refs": [{**SOURCE, "record_id": "foreign-repo"}]},
            inherited_fields=[],
            gaps=[],
            state="ready",
            hash="b" * 64,
        )
    )
    await boundary.db.commit()
    baseline = await effect_counts(boundary.db)
    direct_options = await boundary.client.get(ROOT + "/configuration/options")
    direct_config = await boundary.client.get(ROOT + "/configuration")
    direct_history = await boundary.client.get(ROOT + "/configuration/versions")
    assert direct_options.status_code == direct_config.status_code == direct_history.status_code == 200
    assert direct_config.json()["revision"] == boundary.configuration.current_revision == 2
    assert direct_config.headers["etag"] == '"' + "a" * 64 + '"'
    assert [item["revision"] for item in direct_history.json()["items"]] == [2]
    assert len((await boundary.db.scalars(select(ConfigurationVersion).where(ConfigurationVersion.configuration_id == "config-a"))).all()) == 2
    direct_run = await boundary.client.get(ROOT + "/runs/run-a")
    direct_result = await boundary.client.get(ROOT + "/results/result-a")
    direct_artifact = await boundary.client.get(ROOT + "/artifacts/artifact-a/content")
    sql_run = await boundary.db.get(Run, ("tenant-a", "run-a"))
    sql_result = await boundary.db.get(AssessmentResult, ("tenant-a", "result-a"))
    assert direct_run.status_code == direct_result.status_code == direct_artifact.status_code == 200
    assert direct_run.json()["state"] == sql_run.state == "outcome_unknown"
    assert direct_result.json()["tests_after"] == sql_result.payload["tests_after"] == [{"status": "failed"}]
    assert direct_artifact.content == PATCH
    assert direct_artifact.headers["X-Adp-Content-Sha256"] == REFERENCE["sha256"] == hashlib.sha256(PATCH).hexdigest()

    cli_options = await cli_call("options")
    cli_config = await cli_call("config", "show")
    cli_history = await cli_call("config", "history")
    cli_runs = await cli_call("runs")
    cli_run = await cli_call("status", "--run-id", "run-a")
    cli_results = await cli_call("results")
    cli_result = await cli_call("result", "show", "--result-id", "result-a")
    cli_wait = await cli_call("wait", "--run-id", "run-a", "--wait-seconds", "1", "--poll-seconds", "1")
    mcp_options = await tools_l5.dispatch_l5("l5___get_options", mcp_scope, boundary.backend)
    mcp_config = await tools_l5.dispatch_l5("l5___get_configuration", mcp_scope, boundary.backend)
    mcp_history = await tools_l5.dispatch_l5("l5___list_configuration_versions", mcp_scope, boundary.backend)
    mcp_run = await tools_l5.dispatch_l5("l5___get_run", {**mcp_scope, "assessment_ref": "run-a"}, boundary.backend)
    mcp_result = await tools_l5.dispatch_l5("l5___get_result", {**mcp_scope, "result_id": "result-a"}, boundary.backend)
    mcp_patch = await tools_l5.dispatch_l5("l5___download_artifact", {**mcp_scope, "artifact_id": "artifact-a"}, boundary.backend)

    assert cli_options["detail"] == mcp_options["structuredContent"]["data"] == direct_options.json()
    assert cli_config["detail"] == mcp_config["structuredContent"]["data"] == direct_config.json()
    assert cli_config["etag"] == mcp_config["structuredContent"]["etag"] == direct_config.headers["etag"]
    assert cli_history["detail"] == mcp_history["structuredContent"]["data"] == direct_history.json()
    assert cli_runs["detail"]["items"] == (await boundary.client.get(ROOT + "/runs")).json()["items"]
    assert cli_results["detail"]["items"] == (await boundary.client.get(ROOT + "/results")).json()["items"]
    assert cli_run["detail"] == mcp_run["structuredContent"]["data"] == direct_run.json()
    assert cli_result["detail"] == mcp_result["structuredContent"]["data"] == direct_result.json()
    assert cli_run["etag"] == mcp_run["structuredContent"]["etag"] == direct_run.headers["etag"] == '"2"'
    assert cli_result["etag"] == mcp_result["structuredContent"]["etag"] == direct_result.headers["etag"] == '"1"'
    assert cli_wait["status"] == "unknown" and cli_wait["error"]["code"] == "run_outcome_unknown"
    assert mcp_patch["structuredContent"]["http_status"] == 200
    assert base64.b64decode(mcp_patch["structuredContent"]["data"]["content_base64"]) == PATCH
    assert [request[1].split("?", 1)[0] for request in requests] == [
        ROOT + suffix
        for suffix in (
            "/configuration/options",
            "/configuration",
            "/configuration/versions",
            "/runs",
            "/runs/run-a",
            "/results",
            "/results/result-a",
            "/runs/run-a",
        )
    ]
    assert await effect_counts(boundary.db) == baseline
    boundary.publisher.assert_not_awaited()
    boundary.work_authority.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_boundary_errors(boundary, monkeypatch, tmp_path, capsys):
    cli, client, requests = scoped_cli(boundary, monkeypatch)
    monkeypatch.setattr(cli.common, "Api", lambda: client)
    scope = ["--product", "product-a", "--project", "project-a", "--service", "transform", "--json"]
    mcp_scope = {"product_id": "product-a", "project_id": "project-a", "service_id": "transform"}
    changed = {**INPUT, "transform_input": {"goal": {"kind": "refactor", "summary": "Different goal"}}}
    run_file = tmp_path / "run.json"
    run_file.write_text(json.dumps(changed))
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"enabled": False}))

    async def cli_call(*arguments):
        code = await asyncio.to_thread(cli.main, [*arguments, *scope])
        return code, json.loads(capsys.readouterr().out)

    baseline = await effect_counts(boundary.db)
    artifact_reads = len(boundary.effects)
    for revocation in ("foreign_actor", "revoked_membership", "revoked_source", "revoked_delegation"):
        if revocation == "foreign_actor":
            boundary.actor.user_id = "foreign-a"
        elif revocation == "revoked_membership":
            boundary.active["membership"] = False
        elif revocation == "revoked_source":
            boundary.rights.clear()
        else:
            boundary.grant.revoked_at = datetime(2026, 10, 28, tzinfo=UTC)
            await boundary.db.flush()

        direct = await boundary.client.get(ROOT + "/artifacts/artifact-a/content")
        assert direct.status_code == 404 and direct.json()["detail"]["code"] == "not_found"
        for operation in (
            ("status", "--run-id", "run-a"),
            ("result", "show", "--result-id", "result-a"),
            ("artifact", "show", "--artifact-id", "artifact-a"),
            ("run", "--input", str(run_file), "--idempotency-key", KEY),
        ):
            _, error = await cli_call(*operation)
            assert error["error"]["http_status"] == 404, (revocation, operation, error)
            assert error["detail"] == {} and "data" not in error
        for tool, arguments in (
            ("get_result", {"result_id": "result-a"}),
            ("download_artifact", {"artifact_id": "artifact-a"}),
            ("submit_run", {"input": INPUT, "idempotency_key": KEY}),
        ):
            response = await tools_l5.dispatch_l5("l5___" + tool, {**mcp_scope, **arguments}, boundary.backend)
            assert response["isError"] and response["structuredContent"]["http_status"] == 404
            assert "data" not in response["structuredContent"]
        assert await effect_counts(boundary.db) == baseline
        assert len(boundary.effects) == artifact_reads
        boundary.actor.user_id = "owner-a"
        boundary.active["membership"] = True
        boundary.rights.add(("connection-a", "repo-a"))
        boundary.grant.revoked_at = None
        await boundary.db.flush()

    version = await boundary.db.get(ConfigurationVersion, ("tenant-a", "config-a", 2))

    async def save(product_id, project_id, service_id, settings, idempotency_key, if_none_match, if_match):
        return await boundary.backend.store.edit(boundary.configuration, version, {}, [], settings, if_match)

    boundary.backend.save = save
    conflict = await boundary.client.post(ROOT + "/runs", json=changed, headers={"Idempotency-Key": KEY})
    stale = await boundary.client.put(
        ROOT + "/configuration", json={"enabled": False}, headers={"Idempotency-Key": "config-stale-key-001", "If-Match": '"stale"'}
    )
    assert conflict.status_code == 409 and stale.status_code == 412
    for operation, expected in (
        (("run", "--input", str(run_file), "--idempotency-key", KEY), 409),
        (("config", "update", "--input", str(config_file), "--idempotency-key", "config-stale-key-002", "--if-match", '"stale"'), 412),
    ):
        exit_code, error = await cli_call(*operation)
        assert exit_code == 4 and error["error"]["http_status"] == expected
    for tool, arguments, expected in (
        ("submit_run", {"input": changed, "idempotency_key": KEY}, 409),
        ("save_configuration", {"input": {"enabled": False}, "idempotency_key": "config-stale-key-003", "if_match": '"stale"'}, 412),
    ):
        response = await tools_l5.dispatch_l5("l5___" + tool, {**mcp_scope, **arguments}, boundary.backend)
        assert response["isError"] and response["structuredContent"]["http_status"] == expected
        assert "data" not in response["structuredContent"]
    assert await effect_counts(boundary.db) == baseline
    assert len(boundary.effects) == artifact_reads
    assert boundary.configuration.current_revision == 2
    assert [request[1] for request in requests if request[0] == "POST" and request[1].endswith("/runs")]
    boundary.publisher.assert_not_awaited()
    boundary.work_authority.assert_not_awaited()


@pytest.mark.asyncio
async def test_separate_scm_handoff(boundary, monkeypatch, tmp_path, capsys):
    from src.agentauth.github_provider import GitHubProvider

    cli, client, requests = scoped_cli(boundary, monkeypatch)
    monkeypatch.setattr(cli.common, "Api", lambda: client)
    cli_scope = ["--product", "product-a", "--project", "project-a", "--service", "transform", "--json"]
    mcp_scope = {"product_id": "product-a", "project_id": "project-a", "service_id": "transform"}
    scm_state = {"branch_head": "existing-head", "pull_requests": [], "merges": [], "deployments": []}
    snapshot = {key: value.copy() if isinstance(value, list) else value for key, value in scm_state.items()}
    writer_calls = []

    async def unexpected_write(*args, **kwargs):
        writer_calls.append((args, kwargs))
        raise AssertionError("Transform result inspection must not invoke the Task-scoped SCM writer")

    for method in ("publish_commit", "upsert_pull_request", "merge_pull_request"):
        monkeypatch.setattr(GitHubProvider, method, unexpected_write)

    baseline = await effect_counts(boundary.db)
    sql_result = await boundary.db.get(AssessmentResult, ("tenant-a", "result-a"))
    sql_detail = await boundary.db.get(ResultDetail, ("tenant-a", "result-a"))
    direct_result = await boundary.client.get(ROOT + "/results/result-a")
    direct_patch = await boundary.client.get(ROOT + "/artifacts/artifact-a/content")
    assert direct_result.status_code == direct_patch.status_code == 200
    assert direct_result.json() == sql_result.payload == sql_detail.payload
    assert direct_result.json()["original_patch"]["sha256"] == REFERENCE["sha256"]
    assert direct_patch.content == PATCH and hashlib.sha256(PATCH).hexdigest() == REFERENCE["sha256"]

    output = tmp_path / "original.diff"
    cli_result = await asyncio.to_thread(cli.main, ["result", "show", "--result-id", "result-a", *cli_scope])
    assert cli_result == 0 and json.loads(capsys.readouterr().out)["detail"] == sql_result.payload
    cli_patch = await asyncio.to_thread(cli.main, ["artifact", "download", "--artifact-id", "artifact-a", "--output", str(output), *cli_scope])
    assert cli_patch == 0 and json.loads(capsys.readouterr().out)["detail"]["sha256"] == REFERENCE["sha256"]
    assert output.read_bytes() == PATCH
    mcp_result = await tools_l5.dispatch_l5("l5___get_result", {**mcp_scope, "result_id": "result-a"}, boundary.backend)
    mcp_patch = await tools_l5.dispatch_l5("l5___download_artifact", {**mcp_scope, "artifact_id": "artifact-a"}, boundary.backend)
    assert mcp_result["structuredContent"]["data"] == sql_result.payload
    assert mcp_result["structuredContent"]["etag"] == direct_result.headers["etag"] == '"1"'
    assert base64.b64decode(mcp_patch["structuredContent"]["data"]["content_base64"]) == PATCH
    assert "l5.publish_transform_result" not in {tool["name"] for tool in tools_l5.available_l5_catalogue()["tools"]}
    inspected_requests = list(requests)
    artifact_reads = len(boundary.effects)

    for _ in range(2):
        for action in (
            ("publication", "show", "--publication-id", "result-a"),
            ("publication", "retry", "--publication-id", "result-a", "--input", "not-read.json", "--idempotency-key", "publish-retry-key-001"),
        ):
            code = await asyncio.to_thread(cli.main, [*action, *cli_scope])
            error = json.loads(capsys.readouterr().out)
            assert code == 5 and error["error"]["code"] == "capability_unavailable"
            assert "no separately authorized SCM handoff" in error["error"]["message"]
        with pytest.raises(HTTPException) as unavailable:
            await tools_l5.dispatch_l5(
                "l5___publish_transform_result",
                {
                    **mcp_scope,
                    "result_id": "result-a",
                    "idempotency_key": "publish-retry-key-001",
                },
                boundary.backend,
            )
        assert unavailable.value.status_code == 503 and "unavailable" in str(unavailable.value.detail)
        assert requests == inspected_requests and len(boundary.effects) == artifact_reads
        assert await effect_counts(boundary.db) == baseline
        assert scm_state == snapshot and not writer_calls
    boundary.publisher.assert_not_awaited()
    boundary.work_authority.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_revoked_and_source_denials_leave_retained_evidence_and_effects_unchanged(boundary):
    baseline = await effect_counts(boundary.db)
    owner_read = await boundary.client.get(ROOT + "/artifacts/artifact-a/content")
    assert owner_read.status_code == 200 and owner_read.content == PATCH
    assert len(boundary.effects) == 1
    boundary.actor.user_id = "foreign-a"
    for path in ("/runs/run-a", "/results/result-a", "/artifacts/artifact-a", "/artifacts/artifact-a/content"):
        denied = await boundary.client.get(ROOT + path)
        assert denied.status_code == 404 and denied.json()["detail"]["code"] == "not_found"
    mcp = await tools_l5.dispatch_l5(
        "l5___get_result",
        {"product_id": "product-a", "project_id": "project-a", "service_id": "transform", "result_id": "result-a"},
        boundary.backend,
    )
    assert mcp["isError"] is True and mcp["structuredContent"]["http_status"] == 404
    assert "data" not in mcp["structuredContent"]
    assert (await boundary.client.get(ROOT + "/runs")).json() == {"items": []}
    assert (await boundary.client.get(ROOT + "/results")).json() == {"items": []}
    boundary.actor.user_id = "owner-a"
    for action in ("actor", "source", "connection", "delegation", "binding"):
        if action == "actor":
            boundary.active["membership"] = False
        elif action == "source":
            boundary.rights.clear()
        elif action == "connection":
            boundary.connections.clear()
        elif action == "delegation":
            boundary.grant.revoked_at = datetime.now(UTC)
            await boundary.db.flush()
        else:
            binding = await boundary.db.get(Binding, ("tenant-a", boundary.binding_id))
            binding.generation = 2
            await boundary.db.flush()
        for path in ("/runs/run-a", "/results/result-a", "/artifacts/artifact-a/content"):
            denied = await boundary.client.get(ROOT + path)
            assert denied.status_code == 404, (action, path, denied.text)
        replay = await boundary.client.post(ROOT + "/runs", json=INPUT, headers={"Idempotency-Key": KEY})
        assert replay.status_code == 404, (action, replay.text)
        mutation = await boundary.client.post(
            ROOT + "/runs/run-a/cancel", json={"reason": "stop"}, headers={"Idempotency-Key": "cancel-revoked-a-01"}
        )
        assert mutation.status_code == 404, (action, mutation.text)
        if action == "actor":
            boundary.active["membership"] = True
        elif action == "source":
            boundary.rights.add(("connection-a", "repo-a"))
        elif action == "connection":
            boundary.connections.add("connection-a")
        elif action == "delegation":
            boundary.grant.revoked_at = None
            await boundary.db.flush()
    assert await effect_counts(boundary.db) == baseline
    assert len(boundary.effects) == 1
    boundary.publisher.assert_not_awaited()
    boundary.work_authority.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_refuses_receipt_pointing_at_sibling_service(boundary):
    original = await boundary.client.post(ROOT + "/runs", json=INPUT, headers={"Idempotency-Key": KEY})
    assert original.status_code == 202

    run = await boundary.db.get(Run, ("tenant-a", "run-a"))
    run.service_id = "devops"
    await boundary.db.commit()

    replay = await boundary.client.post(ROOT + "/runs", json=INPUT, headers={"Idempotency-Key": KEY})
    assert replay.status_code == 404
    assert replay.json()["detail"]["code"] == "not_found"
    boundary.publisher.assert_not_awaited()
    boundary.work_authority.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("finops_admission_enabled", ["false", "true"])
async def test_stale_etag_and_changed_replay_deny_without_new_work(boundary, monkeypatch, finops_admission_enabled):
    monkeypatch.setenv("L5_FINOPS_ADMISSION_ENABLED", finops_admission_enabled)
    baseline = await effect_counts(boundary.db)
    recorded = await boundary.client.post(ROOT + "/runs", json=INPUT, headers={"Idempotency-Key": KEY})
    assert recorded.status_code == 202 and recorded.headers["etag"] == '"2"'
    assert recorded.json()["state"] == "outcome_unknown"
    changed = {**INPUT, "transform_input": {"goal": {"kind": "refactor", "summary": "Another goal"}}}
    conflict = await boundary.client.post(ROOT + "/runs", json=changed, headers={"Idempotency-Key": KEY})
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"
    version = await boundary.db.get(ConfigurationVersion, ("tenant-a", "config-a", 2))

    async def save(product_id, project_id, service_id, settings, idempotency_key, if_none_match, if_match):
        return await boundary.backend.store.edit(boundary.configuration, version, {}, [], settings, if_match)

    boundary.backend.save = save
    stale = await boundary.client.put(
        ROOT + "/configuration", json={"enabled": False}, headers={"If-Match": '"stale"', "Idempotency-Key": "config-stale-key-001"}
    )
    assert stale.status_code == 412 and stale.json()["detail"]["code"] == "stale_revision"
    assert boundary.configuration.current_revision == 2
    mcp_scope = {"product_id": "product-a", "project_id": "project-a", "service_id": "transform"}
    mcp_conflict = await tools_l5.dispatch_l5(
        "l5___submit_run",
        {
            **mcp_scope,
            "input": changed,
            "idempotency_key": KEY,
        },
        boundary.backend,
    )
    mcp_stale = await tools_l5.dispatch_l5(
        "l5___save_configuration",
        {
            **mcp_scope,
            "input": {"enabled": False},
            "idempotency_key": "config-stale-key-002",
            "if_match": '"stale"',
        },
        boundary.backend,
    )
    assert mcp_conflict["isError"] and mcp_conflict["structuredContent"]["http_status"] == 409
    assert mcp_conflict["structuredContent"]["error"]["code"] == "idempotency_conflict"
    assert mcp_stale["isError"] and mcp_stale["structuredContent"]["http_status"] == 412
    assert mcp_stale["structuredContent"]["error"]["code"] == "stale_revision"
    assert await effect_counts(boundary.db) == baseline
    boundary.publisher.assert_not_awaited()
    boundary.work_authority.assert_not_awaited()
