"""Provider-shaped workflow evidence and bounded scoped dispatch."""

import base64
import gzip
import hashlib
import io
import json
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.orchestration.deployment_manifest import WorkflowRef
from src.orchestration.deployment_workflow_provider import WorkflowProvider
from src.orchestration.review_cycle import CycleBlockedError

SOURCE = "a" * 40
APPROVED = "b" * 40
PATH = ".github/workflows/gateway-deploy.yml"
YAML = b"""name: Deploy
on:
  workflow_dispatch:
    inputs:
      environment:
        default: dev
      adp_correlation:
        default: ''
      adp_source_revision:
        default: ''
      adp_definition_revision:
        default: ''
"""


@pytest.fixture
async def provider(monkeypatch):
    binding = SimpleNamespace(org_id="tenant", repo="org/repo", installation_id=7, provider_repository_id=17, pr_number=9)
    workflow = WorkflowRef(PATH, APPROVED, {"environment": frozenset({"dev"})}, correlation_input="adp_correlation")
    target = SimpleNamespace(account_id="123456789012", region="us-east-1", resource_kind="eks-namespace", resource_id="cluster/namespace")
    document = dict(
        schema_version=1,
        repository_id=17,
        run_id=42,
        run_attempt=1,
        workflow_path=PATH,
        workflow_revision=SOURCE,
        source_revision=SOURCE,
        account_id=target.account_id,
        region=target.region,
        resource_kind=target.resource_kind,
        resource_id=target.resource_id,
        inputs={"environment": "dev"},
        correlation="",
    )
    run = dict(id=42, run_attempt=1, head_sha=SOURCE, repository={"id": 17}, path=PATH, event="push", status="completed", conclusion="success")
    ctx = SimpleNamespace(
        binding=binding, workflow=workflow, target=target, document=document, run=run, missing=False, changed=False, forged=False, calls=[]
    )
    mint = AsyncMock(return_value=("scoped-token", (datetime.now(UTC) + timedelta(hours=1)).isoformat()))
    monkeypatch.setattr("src.orchestration.deployment_workflow_provider.resolve_tenant_app_credentials", AsyncMock(return_value=("app", "key")))
    monkeypatch.setattr("src.orchestration.deployment_workflow_provider.mint_installation_token_with_expiry", mint)

    def package():
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as zipped:
            zipped.writestr("deployment-context.json", json.dumps(ctx.document))
        return output.getvalue()

    def respond(request):
        assert request.headers["Authorization"] == "Bearer scoped-token"
        ctx.calls.append((request.method, request.url.path))
        path = request.url.path
        if path == "/repos/org/repo":
            return httpx.Response(200, json={"id": 17, "default_branch": "main"})
        if path == "/repos/org/repo/branches/main":
            return httpx.Response(200, json={"commit": {"sha": SOURCE}})
        if "/contents/" in path:
            content = YAML + (b"# modified" if ctx.changed and request.url.params["ref"] == SOURCE else b"")
            return httpx.Response(
                200,
                json=dict(
                    type="file",
                    encoding="base64",
                    size=len(content),
                    content=base64.b64encode(content).decode(),
                    sha="0" * 40 if ctx.forged else hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest(),
                ),
            )
        if path.endswith("/actions/runs"):
            return httpx.Response(200, json={"workflow_runs": [ctx.run]})
        if path.endswith("/actions/runs/42/artifacts"):
            return httpx.Response(
                200,
                json={
                    "artifacts": []
                    if ctx.missing
                    else [
                        dict(
                            id=77,
                            name="adp-deployment-context-" + ctx.document["workflow_path"].rsplit("/", 1)[1] + "-1",
                            expired=False,
                            size_in_bytes=len(package()),
                            digest="sha256:" + hashlib.sha256(package()).hexdigest(),
                        )
                    ]
                },
            )
        if path.endswith("/actions/artifacts/77/zip"):
            return httpx.Response(200, content=package())
        if path.endswith("/dispatches"):
            ctx.dispatch = json.loads(request.content)
            return httpx.Response(204)
        pytest.fail(f"Unexpected {request.method} {path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        ctx.provider = WorkflowProvider(client=client)
        ctx.definition = await ctx.provider.definition(binding, workflow, SOURCE)
        ctx.mint = mint
        yield ctx


async def observe(ctx, **kwargs):
    return await ctx.provider.observe(
        ctx.binding,
        workflow=ctx.workflow,
        definition=ctx.definition,
        target=ctx.target,
        source_revision=SOURCE,
        inputs={"environment": "dev"},
        **kwargs,
    )


async def test_matching_push_workflow_is_observed_without_dispatch(provider):
    result, incomplete = await observe(provider)
    assert result.run_id == 42 and result.conclusion == "success" and not incomplete
    assert all(method == "GET" for method, _ in provider.calls)
    assert result.context.source_revision == SOURCE and result.artifact_id == 77


@pytest.mark.parametrize("field,value", [("account_id", "999999999999"), ("source_revision", "c" * 40), ("run_id", 99), ("repository_id", 99)])
async def test_wrong_target_source_or_provider_identity_cannot_be_adopted(provider, field, value):
    provider.document[field] = value
    with pytest.raises(CycleBlockedError):
        await observe(provider)


async def test_changed_workflow_bytes_are_not_the_approved_definition(provider):
    provider.changed = True
    with pytest.raises(CycleBlockedError, match="deployment_workflow_revision_mismatch"):
        await provider.provider.definition(provider.binding, provider.workflow, SOURCE)


async def test_definition_bytes_must_hash_to_the_git_object_id_the_provider_named(provider):
    # The blob digest is Git's object ID (not a security credential), but it must still
    # fail closed: content that does not hash to the reported `sha` is never a definition.
    provider.forged = True
    with pytest.raises(CycleBlockedError, match="deployment_workflow_blob_mismatch"):
        await provider.provider.definition(provider.binding, provider.workflow, SOURCE)


async def test_definition_blob_sha_is_the_git_blob_object_id_of_the_definition(provider):
    # Pins the Git object-ID formula independently of the provider's own implementation:
    # `usedforsecurity=False` must not perturb the digest Git and the GitHub API agree on.
    assert provider.definition.blob_sha == hashlib.sha1(b"blob " + str(len(YAML)).encode() + b"\0" + YAML).hexdigest()


async def test_missing_context_leaves_matching_run_unverifiable(provider):
    provider.missing = True
    run, incomplete = await observe(provider)
    assert run is None and incomplete


async def test_unrelated_manual_workflow_does_not_count_as_our_dispatch(provider):
    provider.run.update(event="workflow_dispatch", display_title="Somebody else's dispatch")
    run, incomplete = await observe(provider, correlation="our-correlation")
    assert run is None and not incomplete


async def test_dispatch_uses_one_repo_token_and_checks_authority_before_effect(provider):
    authorized = []

    async def authorize():
        assert not any(method == "POST" for method, _ in provider.calls)
        authorized.append(True)

    await provider.provider.dispatch(
        provider.binding,
        workflow=provider.workflow,
        definition=provider.definition,
        source_revision=SOURCE,
        inputs={"environment": "dev"},
        correlation="stable-key",
        reauthorize=authorize,
    )
    assert authorized == [True] and provider.dispatch["inputs"] == {
        "environment": "dev",
        "adp_correlation": "stable-key",
        "adp_source_revision": SOURCE,
        "adp_definition_revision": SOURCE,
    }
    assert provider.dispatch["ref"] == "main"
    assert provider.mint.await_args.kwargs["permissions"]["actions"] == "write"
    assert all(call.kwargs["repositories"] == ["repo"] for call in provider.mint.await_args_list)


async def test_unapproved_correlation_transport_cannot_dispatch(provider):
    with pytest.raises(CycleBlockedError, match="deployment_workflow_correlation_unavailable"):
        await provider.provider.dispatch(
            provider.binding,
            workflow=replace(provider.workflow, correlation_input=None),
            definition=provider.definition,
            source_revision=SOURCE,
            inputs={"environment": "dev"},
            correlation="stable-key",
            reauthorize=AsyncMock(),
        )
    assert not any(method == "POST" for method, _ in provider.calls)


async def test_revoked_authority_prevents_dispatch(provider):
    with pytest.raises(CycleBlockedError, match="revoked"):
        await provider.provider.dispatch(
            provider.binding,
            workflow=provider.workflow,
            definition=provider.definition,
            source_revision=SOURCE,
            inputs={"environment": "dev"},
            correlation="stable-key",
            reauthorize=AsyncMock(side_effect=CycleBlockedError("revoked")),
        )
    assert not any(method == "POST" for method, _ in provider.calls)


async def test_provider_response_is_bounded_before_full_buffering(provider):
    with pytest.raises(CycleBlockedError, match="deployment_provider_response_limit"):
        await provider.provider.request(provider.binding, "GET", "/repos/org/repo/actions/artifacts/77/zip", max_bytes=10)


async def test_observation_definition_does_not_require_current_default_branch(provider):
    provider.calls.clear()
    definition = await provider.provider.definition(provider.binding, provider.workflow, SOURCE, for_dispatch=False)
    assert definition.blob_sha == provider.definition.blob_sha
    assert all("/branches/" not in path for _, path in provider.calls)


async def test_reusable_migration_context_requires_pinned_parent_and_reference(provider):
    ctx = provider
    child = ".github/workflows/run-gateway-migrations.yml"
    ctx.workflow = replace(ctx.workflow, path=child)
    ctx.definition = await ctx.provider.definition(ctx.binding, ctx.workflow, SOURCE)
    ctx.document["workflow_path"] = child
    ctx.run["referenced_workflows"] = [{"path": ctx.binding.repo + "/" + child + "@main", "sha": SOURCE}]
    run, _ = await observe(ctx)
    assert run.run_id == 42
    ctx.run["path"] = ".github/workflows/unapproved.yml"
    assert (await observe(ctx))[0] is None
    ctx.run["path"] = PATH
    ctx.run["referenced_workflows"][0]["sha"] = "f" * 40
    assert (await observe(ctx))[0] is None


async def test_automatic_run_may_use_approved_default_connection_inputs(provider):
    ctx = provider
    ctx.definition = replace(ctx.definition, defaults={**ctx.definition.defaults, "customer_account_id": ""})
    ctx.workflow = replace(
        ctx.workflow, allowed_inputs={**ctx.workflow.allowed_inputs, "customer_account_id": frozenset({"", ctx.target.account_id})}
    )
    ctx.document["inputs"]["customer_account_id"] = ""
    result, _ = await ctx.provider.observe(
        ctx.binding,
        workflow=ctx.workflow,
        definition=ctx.definition,
        target=ctx.target,
        source_revision=SOURCE,
        inputs={"environment": "dev", "customer_account_id": ctx.target.account_id},
    )
    assert result.run_id == 42


async def test_github_gzip_response_is_decoded_once_and_keeps_response_metadata():
    payload = b'{"id":17,"default_branch":"main"}'
    compressed = gzip.compress(payload)

    def respond(request):
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-length": str(len(compressed)), "x-github-request-id": "request-1"},
            stream=httpx.ByteStream(compressed),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = WorkflowProvider(client=client)
        response = await provider.request(None, "GET", "/repos/org/repo", token="test-token")
        assert response.json() == {"id": 17, "default_branch": "main"}
        assert response.headers["x-github-request-id"] == "request-1"
        assert "content-encoding" not in response.headers
        assert int(response.headers["content-length"]) == len(payload)
        with pytest.raises(CycleBlockedError, match="deployment_provider_response_limit"):
            await provider.request(None, "GET", "/repos/org/repo", token="test-token", max_bytes=len(payload) - 1)
