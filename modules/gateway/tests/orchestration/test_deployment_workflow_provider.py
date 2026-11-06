"""Provider-shaped workflow evidence and bounded scoped dispatch."""

import base64
import gzip
import hashlib
import io
import json
import runpy
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import yaml

from src.orchestration.deployment_manifest import WorkflowRef, load_packaged_manifest
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
            return httpx.Response(200, json={"commit": {"sha": getattr(ctx, "dispatch_revision", SOURCE)}})
        if "/contents/" in path:
            content = YAML + (b"# modified" if ctx.changed and request.url.params["ref"] == SOURCE else b"")
            content = getattr(ctx, "workflow_contents", {}).get(request.url.params["ref"], content)
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
        if path.endswith("/actions/runs/42"):
            return httpx.Response(200, json=ctx.run)
        if path.endswith("/actions/runs/42/artifacts"):
            return httpx.Response(
                200,
                json={
                    "artifacts": []
                    if ctx.missing
                    else [
                        dict(
                            id=77,
                            name=getattr(
                                ctx, "context_artifact_name", "adp-deployment-context-" + ctx.document["workflow_path"].rsplit("/", 1)[1] + "-1"
                            ),
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


def qualified_gateway(ctx):
    entry = load_packaged_manifest().entry("adp-dev-gateway")
    ctx.workflow = entry.workflow
    ctx.target.resource_id = entry.resource_id
    ctx.document["resource_id"] = entry.resource_id
    ctx.definition = replace(
        ctx.definition,
        approved_revision=entry.workflow.definition_revision,
        evidence_transport="s3-oidc-v1",
    )

    def read(**expected):
        assert expected["environment"] == "dev" and expected["workflow_path"] == entry.workflow.path
        if expected["attempt"] != 1:
            return None
        payload = json.dumps(ctx.document).encode()
        return payload, hashlib.sha256(payload).hexdigest(), "s3://fixture/context?versionId=one"

    ctx.provider.evidence_store = SimpleNamespace(read=Mock(side_effect=read))
    return entry


@pytest.mark.parametrize("failure", ["wrong_target", "stale_source", "stale_workflow", "stale_run", "different_attempt"])
async def test_qualified_gateway_refuses_wrong_or_stale_run_evidence(provider, failure):
    ctx = provider
    entry = qualified_gateway(ctx)
    if failure == "wrong_target":
        ctx.document["resource_id"] = "unapproved-cluster/namespace"
    elif failure == "stale_source":
        ctx.document["source_revision"] = "c" * 40
    elif failure == "stale_workflow":
        ctx.document["workflow_revision"] = entry.workflow.definition_revision
    elif failure == "stale_run":
        ctx.run["head_sha"] = "d" * 40
    else:
        ctx.document["run_attempt"] = 2
    with pytest.raises(CycleBlockedError):
        await observe(ctx, run_id=42)
    assert not any(method == "POST" for method, _ in ctx.calls)


async def test_qualified_gateway_adopts_same_automatic_run_after_lost_response(provider):
    ctx = provider
    entry = qualified_gateway(ctx)
    first, _ = await observe(ctx, run_id=42)
    second, _ = await observe(ctx, run_id=42)
    assert first.run_id == second.run_id == 42
    assert first.run_attempt == second.run_attempt == 1
    assert first.artifact_digest == second.artifact_digest
    assert first.artifact_ref == second.artifact_ref == "s3://fixture/context?versionId=one"
    assert first.context == second.context
    assert first.context.resource_id == entry.resource_id
    assert not any(method == "POST" for method, _ in ctx.calls)
    ctx.run["run_attempt"] = 2
    pending, incomplete = await observe(ctx, run_id=42)
    assert pending is None and incomplete


async def test_matching_push_workflow_is_observed_without_dispatch(provider):
    result, incomplete = await observe(provider)
    assert result.run_id == 42 and result.conclusion == "success" and not incomplete
    assert all(method == "GET" for method, _ in provider.calls)
    assert result.context.source_revision == SOURCE and result.artifact_id == 77


@pytest.mark.parametrize("event,environment", [("push", "dev"), ("workflow_dispatch", "prod")])
async def test_interceptor_context_producer_is_adopted_by_workflow_consumer(provider, event, environment):
    root = Path(__file__).resolve().parents[4]
    path = ".github/workflows/tools-interceptor-deploy.yml"
    document = yaml.load((root / path).read_text(), Loader=yaml.BaseLoader)
    steps = document["jobs"]["deploy"]["steps"]
    upload = next(step for step in steps if step.get("name") == "Publish deployment workflow context")
    assert "uses" not in upload and document["env"]["ADP_DEPLOYMENT_EVIDENCE_STORE"] == "s3-oidc-v1"
    assert upload["run"] == (
        'python3 "$RUNNER_TEMP/adp-deployment-evidence/scripts/publish-deployment-evidence.py" '
        'context tools-interceptor-deploy.yml "$RUNNER_TEMP/adp-deployment-context/deployment-context.json"'
    )
    assert upload["env"]["ADP_WORKFLOW_REVISION"] == "${{ github.workflow_sha }}"
    correlation = "c" * 64 if event == "workflow_dispatch" else ""
    revision = APPROVED if event == "workflow_dispatch" else SOURCE
    env = {
        "CONTEXT_SOURCE": SOURCE,
        "CONTEXT_REVISION": revision,
        "CONTEXT_CORRELATION": correlation,
        "CONTEXT_REPOSITORY_ID": "17",
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1",
        "INPUT_ENVIRONMENT": environment,
        "INPUT_ROLLBACK_TO_VERSION": "",
        "ACCOUNT_ID": "123456789012",
        "AWS_REGION": "us-east-1",
        "TOOLS_INTERCEPTOR_FUNCTION_NAME": "example-interceptor",
    }
    arn = "arn:aws:lambda:us-east-1:123456789012:function:example-interceptor"

    def read(args):
        if args == ["sts", "get-caller-identity"]:
            return {"Account": "123456789012"}
        assert args == ["lambda", "get-function", "--function-name", "example-interceptor", "--region", "us-east-1"]
        return {"Configuration": {"FunctionName": "example-interceptor", "FunctionArn": arn}}

    producer = runpy.run_path(str(root / "modules/tools/interceptor/deployment_context.py"))
    provider.document = producer["context"](env, read)
    payload = json.dumps(provider.document).encode()
    digest = hashlib.sha256(payload).hexdigest()
    reference = "s3://example-deployment-evidence/deployment-evidence/v1/17/42/1/context/tools-interceptor-deploy.yml.json?versionId=one"
    evidence_read = Mock(return_value=(payload, digest, reference))
    provider.provider.evidence_store = SimpleNamespace(read=evidence_read)
    provider.workflow = WorkflowRef(path, APPROVED, {"environment": frozenset({"dev", "prod"})}, correlation_input="adp_correlation")
    provider.definition = replace(
        provider.definition,
        dispatch_revision=revision,
        defaults={name: value["default"] for name, value in document["on"]["workflow_dispatch"]["inputs"].items()},
        evidence_transport=document["env"]["ADP_DEPLOYMENT_EVIDENCE_STORE"],
    )
    provider.target = SimpleNamespace(account_id="123456789012", region="us-east-1", resource_kind="lambda-function", resource_id=arn)
    provider.run.update(path=path, event=event, head_sha=revision, display_title="ADP deployment " + correlation)
    result, incomplete = await provider.provider.observe(
        provider.binding,
        workflow=provider.workflow,
        definition=provider.definition,
        target=provider.target,
        source_revision=SOURCE,
        inputs={"environment": environment},
        correlation=correlation or None,
    )
    assert not incomplete
    assert result is not None and result.run_id == 42 and result.conclusion == "success"
    assert result.context.source_revision == SOURCE and result.context.workflow_revision == revision
    assert result.context.resource_id == arn and result.artifact_id is None
    assert result.artifact_ref == reference and result.artifact_digest == digest
    evidence_read.assert_called_once_with(
        account="123456789012",
        environment=environment,
        region="us-east-1",
        repository="org/repo",
        repository_id=17,
        run_id=42,
        attempt=1,
        kind="context",
        name="tools-interceptor-deploy.yml",
        workflow_path=path,
        workflow_revision=revision,
    )
    assert not any("/artifacts" in request_path for _, request_path in provider.calls)


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


async def test_product_owned_workflow_name_does_not_select_a_platform_caller(provider):
    workflow = replace(provider.workflow, path=".github/workflows/run-gateway-migrations.yml")
    provider.calls.clear()
    definition = await provider.provider.definition(provider.binding, workflow, SOURCE, for_dispatch=False, generic_delivery=True)
    assert definition.caller_path is None
    assert not any(path.endswith("/gateway-deploy.yml") for _, path in provider.calls)


@pytest.mark.parametrize("for_dispatch", [True, False])
async def test_generic_delivery_can_repair_workflow_without_rebuilding_artifact(provider, for_dispatch):
    provider.dispatch_revision = "c" * 40
    provider.workflow_contents = {SOURCE: YAML + b"# older workflow at the verified artifact source\n"}
    definition = await provider.provider.definition(provider.binding, provider.workflow, SOURCE, for_dispatch=for_dispatch, generic_delivery=True)
    assert definition.approved_revision == APPROVED
    assert definition.source_revision == SOURCE
    assert definition.blob_sha == provider.definition.blob_sha


async def test_generic_delivery_still_refuses_unapproved_dispatch_workflow(provider):
    provider.dispatch_revision = "c" * 40
    provider.workflow_contents = {provider.dispatch_revision: YAML + b"# unapproved change\n"}
    with pytest.raises(CycleBlockedError, match="deployment_workflow_revision_mismatch"):
        await provider.provider.definition(provider.binding, provider.workflow, SOURCE, generic_delivery=True)
    assert not any(method == "POST" for method, _ in provider.calls)


async def test_legacy_delivery_still_requires_source_workflow_match(provider):
    provider.dispatch_revision = "c" * 40
    provider.workflow_contents = {SOURCE: YAML + b"# older workflow at the artifact source\n"}
    with pytest.raises(CycleBlockedError, match="deployment_workflow_revision_mismatch"):
        await provider.provider.definition(provider.binding, provider.workflow, SOURCE)


@pytest.mark.parametrize("generic_delivery,expected", [(True, "false"), (False, "False")])
async def test_workflow_boolean_defaults_preserve_the_evidence_protocol(provider, monkeypatch, generic_delivery, expected):
    monkeypatch.setattr(__import__(__name__, fromlist=["YAML"]), "YAML", YAML + b"      dry_run:\n        type: boolean\n        default: false\n")
    definition = await provider.provider.definition(provider.binding, provider.workflow, SOURCE, generic_delivery=generic_delivery)
    assert definition.defaults["dry_run"] == expected


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


async def test_context_omitting_a_defaulted_input_compares_as_its_default(provider):
    """tools-facade-deploy records only the inputs it received; an omitted input ran with its declared default."""
    ctx = provider
    ctx.definition = replace(ctx.definition, defaults={**ctx.definition.defaults, "rollback_to_digest": ""})
    assert "rollback_to_digest" not in ctx.document["inputs"]
    result, _ = await ctx.provider.observe(
        ctx.binding,
        workflow=ctx.workflow,
        definition=ctx.definition,
        target=ctx.target,
        source_revision=SOURCE,
        inputs={"environment": "dev"},
    )
    assert result is not None and result.run_id == 42


@pytest.mark.parametrize("selector,default,selected", [("release_group", "none", "atlassian-foundation"), ("stage", "platform", "tool-adapters")])
async def test_native_foundation_observation_requires_explicit_selector(provider, selector, default, selected):
    ctx = provider
    ctx.definition = replace(ctx.definition, defaults={**ctx.definition.defaults, selector: default})
    ctx.run.update(event="workflow_dispatch", display_title="ADP deployment native-fixture")
    ctx.document["correlation"] = "native-fixture"
    ctx.document["inputs"][selector] = selected
    arguments = dict(
        binding=ctx.binding,
        workflow=ctx.workflow,
        definition=ctx.definition,
        target=ctx.target,
        source_revision=SOURCE,
        correlation="native-fixture",
        run_id=42,
    )
    with pytest.raises(CycleBlockedError, match="deployment_run_inputs_mismatch"):
        await ctx.provider.observe(**arguments, inputs={})
    result, _ = await ctx.provider.observe(**arguments, inputs={selector: selected})
    assert result is not None and result.run_id == 42
    ctx.document["inputs"][selector] = default
    with pytest.raises(CycleBlockedError, match="deployment_run_inputs_mismatch"):
        await ctx.provider.observe(**arguments, inputs={selector: selected})


@pytest.mark.parametrize("case", ["dispatched_input_omitted", "recorded_value_differs"])
async def test_omitted_or_changed_inputs_still_refuse(provider, case):
    ctx = provider
    ctx.definition = replace(ctx.definition, defaults={**ctx.definition.defaults, "rollback_to_digest": ""})
    if case == "dispatched_input_omitted":
        # The dispatcher explicitly set rollback_to_digest; a context that omits it did not run with it.
        inputs = {"environment": "dev", "rollback_to_digest": "sha256:" + "a" * 64}
    else:
        ctx.document["inputs"]["rollback_to_digest"] = "sha256:" + "b" * 64
        inputs = {"environment": "dev"}
    with pytest.raises(CycleBlockedError, match="deployment_run_inputs_mismatch"):
        await ctx.provider.observe(
            ctx.binding,
            workflow=ctx.workflow,
            definition=ctx.definition,
            target=ctx.target,
            source_revision=SOURCE,
            inputs=inputs,
        )


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


async def test_download_redirect_requires_opt_in_and_preserves_response_limit():
    location = "https://pipelines.actions.githubusercontent.com/logs"

    def respond(request):
        assert request.url.host == "api.github.com"
        return httpx.Response(302, headers={"location": location}, stream=httpx.ByteStream(b"body"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = WorkflowProvider(client=client)
        with pytest.raises(httpx.HTTPStatusError):
            await provider.request(None, "GET", "/repos/org/repo/actions/runs/42/logs", token="test-token")
        response = await provider.request(None, "GET", "/repos/org/repo/actions/runs/42/logs", token="test-token", allow_download_redirect=True)
        assert response.status_code == 302 and response.headers["location"] == location
        with pytest.raises(CycleBlockedError, match="deployment_provider_response_limit"):
            await provider.request(None, "GET", "/repos/org/repo/actions/runs/42/logs", token="test-token", allow_download_redirect=True, max_bytes=3)


async def test_s3_definition_uses_signed_store_without_github_artifact_quota(provider):
    ctx = provider
    ctx.definition = replace(ctx.definition, evidence_transport="s3-oidc-v1")
    reads = []
    reference = "s3://adp-dev-deployment-evidence-123456789012/deployment-evidence/v1/17/42/1/context/gateway-deploy.yml.json?versionId=one"

    def read(**expected):
        reads.append(expected)
        payload = json.dumps(ctx.document).encode()
        return payload, hashlib.sha256(payload).hexdigest(), reference

    ctx.provider.evidence_store = SimpleNamespace(read=read)
    run, incomplete = await observe(ctx)
    assert not incomplete and run.artifact_id is None and run.artifact_ref == reference
    assert reads[0]["repository_id"] == 17 and reads[0]["attempt"] == 1
    assert reads[0]["workflow_revision"] == SOURCE
    assert not any("/artifacts" in path for _, path in ctx.calls)


@pytest.mark.parametrize("transport", ["s3-oidc-v1", "github-artifact", "unknown"])
async def test_delivery_transport_is_read_from_verified_definition(provider, monkeypatch, transport):
    monkeypatch.setitem(globals(), "YAML", YAML + f"\nenv:\n  ADP_DELIVERY_EVIDENCE_STORE: {transport}\n".encode())
    ctx = provider
    if transport == "unknown":
        with pytest.raises(CycleBlockedError, match="transport_unknown"):
            await ctx.provider.definition(ctx.binding, ctx.workflow, SOURCE)
        return
    ctx.definition = await ctx.provider.definition(ctx.binding, ctx.workflow, SOURCE)
    assert ctx.definition.delivery_evidence_transport == transport
    run, incomplete = await observe(ctx)
    assert not incomplete and run.delivery_evidence_transport == transport


async def test_s3_signature_failure_cannot_fall_back_to_github_artifact(provider):
    ctx = provider
    ctx.definition = replace(ctx.definition, evidence_transport="s3-oidc-v1")

    def read(**expected):
        raise ValueError("invalid signature; sensitive content")

    ctx.provider.evidence_store = SimpleNamespace(read=read)
    with pytest.raises(CycleBlockedError, match="deployment_s3_evidence_unverifiable") as caught:
        await observe(ctx)
    assert "sensitive" not in str(caught.value)
    assert not any("/artifacts" in path for _, path in ctx.calls)


async def test_s3_payload_must_match_verified_run(provider):
    ctx = provider
    ctx.definition = replace(ctx.definition, evidence_transport="s3-oidc-v1")
    ctx.document["run_id"] = 99
    ctx.provider.evidence_store = SimpleNamespace(read=lambda **_: (json.dumps(ctx.document).encode(), "a" * 64, "s3://version"))
    with pytest.raises(CycleBlockedError, match="deployment_context_identity_mismatch"):
        await observe(ctx)


@pytest.mark.parametrize("write", [False, True])
async def test_provider_token_can_read_pull_request_files(monkeypatch, write):
    """`GET /pulls/{n}/files` needs `pull_requests: read`; without it changed-files
    lookups answered 403 and every deployment phase blocked on evidence (#7452)."""
    from src.orchestration.deployment_workflow_provider import WorkflowProvider

    minted = AsyncMock(return_value=("token", (datetime.now(UTC) + timedelta(minutes=30)).isoformat()))
    monkeypatch.setattr("src.orchestration.deployment_workflow_provider.resolve_tenant_app_credentials", AsyncMock(return_value=("app", "key")))
    monkeypatch.setattr("src.orchestration.deployment_workflow_provider.mint_installation_token_with_expiry", minted)
    binding = SimpleNamespace(org_id="org", installation_id=42, repo="org/repo")
    assert await WorkflowProvider().token(binding, write=write) == "token"
    permissions = minted.await_args.kwargs["permissions"]
    assert permissions["pull_requests"] == "read"
    assert permissions["contents"] == "read" and permissions["metadata"] == "read"
    assert permissions["actions"] == ("write" if write else "read")
    assert minted.await_args.kwargs["repositories"] == ["repo"]


async def test_correlated_run_listing_asks_for_dispatch_runs_in_small_bounded_pages(provider):
    """A busy repository's repo-wide run list exceeded the 1 MiB read bound and held every runtime verification (TLS-X05, 2026-10-15)."""
    from src.orchestration.deployment_workflow_provider import MAX_LISTING_BYTES, RUNS_PAGE_SIZE

    seen = []

    async def request(binding, method, path, *, token=None, max_bytes=None, params=None, **kwargs):
        seen.append((path, dict(params or {}), max_bytes))
        return httpx.Response(200, json={"workflow_runs": [provider.run]})

    provider.provider.request = request
    assert await provider.provider.runs(provider.binding, SOURCE, correlation="orch:35c6e7f2-f43e-5c94-b30d-f405fa0016af") == [provider.run]
    path, params, max_bytes = seen[-1]
    assert path.endswith("/actions/runs")
    assert params["event"] == "workflow_dispatch" and "head_sha" not in params
    assert params["per_page"] == RUNS_PAGE_SIZE and max_bytes == MAX_LISTING_BYTES
    # An uncorrelated observation is still keyed by the source revision.
    await provider.provider.runs(provider.binding, SOURCE)
    assert seen[-1][1]["head_sha"] == SOURCE and "event" not in seen[-1][1]


async def test_listing_pages_tolerate_provider_sized_responses_but_stay_bounded():
    from src.orchestration.deployment_workflow_provider import MAX_LISTING_BYTES, WorkflowProvider
    from src.orchestration.review_cycle import CycleBlockedError

    big = {"workflow_runs": [{"id": 1, "padding": "x" * 1500}] * 1000}  # ~1.5 MiB: over the archive bound, inside the listing bound
    huge = {"workflow_runs": [{"id": 1, "padding": "x" * 5000}] * 1000}  # ~5 MiB: past the listing bound

    def respond(request):
        return httpx.Response(200, json=big if request.url.params.get("page") == "1" else huge)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        p = WorkflowProvider(client=client)
        p.token = AsyncMock(return_value="t")
        binding = SimpleNamespace(repo="org/repo")
        assert len(json.dumps(big).encode()) > 1024 * 1024 < MAX_LISTING_BYTES
        page = await p.request(binding, "GET", "/repos/org/repo/actions/runs", token="t", max_bytes=MAX_LISTING_BYTES, params={"page": 1})
        assert len(page.json()["workflow_runs"]) == 1000
        with pytest.raises(CycleBlockedError, match="deployment_provider_response_limit"):
            await p.request(binding, "GET", "/repos/org/repo/actions/runs", token="t", max_bytes=MAX_LISTING_BYTES, params={"page": 2})


async def test_s3_read_falls_back_to_dev_when_the_workflow_environment_default_is_empty(provider):
    """#7829 (door4, 2026-10-15 23:25Z): agent-context-deploy.yml declares `environment` with
    default '' and resolves `|| 'dev'` inside the job. The reader derived environment '' from
    the workflow defaults, so `bucket_name` refused and every receipt read was
    `deployment_s3_evidence_unverifiable` although both objects existed under adp-dev-…."""
    ctx = provider
    ctx.definition = replace(ctx.definition, evidence_transport="s3-oidc-v1", defaults={**ctx.definition.defaults, "environment": ""})
    reads = []

    def read(**expected):
        reads.append(expected)
        payload = json.dumps(ctx.document).encode()
        return payload, hashlib.sha256(payload).hexdigest(), "s3://bucket/context?versionId=one"

    ctx.provider.evidence_store = SimpleNamespace(read=read)
    run, incomplete = await observe(ctx)
    assert not incomplete and reads[0]["environment"] == "dev"


async def test_context_artifact_keeps_64k_limit(provider):
    # Valid context strings are padded as JSON whitespace to isolate the artifact
    # byte bound from schema field limits and from the larger workflow read cap.
    import src.orchestration.deployment_workflow_provider as module

    payload = json.dumps(provider.document).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zipped:
        zipped.writestr("deployment-context.json", payload + b" " * (65537 - len(payload)))
    archive = output.getvalue()
    provider.provider.pages = AsyncMock(
        return_value=[
            {
                "id": 77,
                "name": "adp-deployment-context-gateway-deploy.yml-1",
                "expired": False,
                "size_in_bytes": len(archive),
                "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
            }
        ]
    )
    provider.provider.request = AsyncMock(return_value=httpx.Response(200, content=archive))
    with pytest.raises(CycleBlockedError, match="deployment_context_archive_invalid"):
        await provider.provider.context(provider.binding, provider.run, PATH)
    assert module.MAX_CONTEXT_BYTES == 65536
