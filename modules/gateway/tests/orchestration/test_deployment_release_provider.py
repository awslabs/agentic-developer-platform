"""Release archives authenticate run identity and build hashes, not health."""

import hashlib
import importlib.util
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from src.orchestration.deployment_release_provider import ReleaseProvider
from src.orchestration.deployment_runtime_contract import ReleaseArtifact
from src.orchestration.deployment_workflow_provider import WorkflowContext
from src.orchestration.review_cycle import CycleBlockedError

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("release_evidence", ROOT / "modules/gateway/scripts/deployment-release-evidence.py")
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


@pytest.fixture
def build():
    env = dict(
        ADP_RELEASE_SOURCE=SHA,
        ADP_RELEASE_DEFINITION=SHA,
        ADP_RELEASE_WORKFLOW=".github/workflows/gateway-deploy.yml",
        GITHUB_REPOSITORY_ID="123",
        GITHUB_RUN_ID="42",
        GITHUB_RUN_ATTEMPT="1",
        ACCOUNT_ID="123456789012",
        CUSTOMER_ACCOUNT_ID="999999999999",
        AWS_REGION="us-east-1",
        ENVIRONMENT="dev",
        AWS_SECRET_ACCESS_KEY="never-publish",
        ADP_PRODUCT_PROFILE_ENABLED="false",
        ADP_PRODUCT_ONBOARDING_ENABLED="false",
    )
    authenticated_account = env["ACCOUNT_ID"]

    def read(args):
        return {"Account": authenticated_account} if args[0] == "sts" else {"imageDetails": [{"imageDigest": DIGEST, "imageTags": [SHA]}]}

    return env, read


def test_producer_backend_hashes_actual_image_and_never_exports_credentials(build):
    env, read = build
    release = ReleaseArtifact.model_validate(producer.produce(env, "gateway-backend", read))
    assert release.image_digest == DIGEST and release.source_revision == SHA
    assert release.product_profile_enabled is False
    assert release.product_onboarding_enabled is False
    assert release.product_profile_schema_version == "1.0"
    assert release.migration_head == producer.migration_head(ROOT / "modules/gateway/alembic/versions")
    assert release.cli_manifest_sha256 == hashlib.sha256((ROOT / "modules/gateway/cli/command-manifest.json").read_bytes()).hexdigest()
    assert "never-publish" not in release.model_dump_json()


@pytest.mark.parametrize("flag", ["ADP_PRODUCT_PROFILE_ENABLED", "ADP_PRODUCT_ONBOARDING_ENABLED"])
def test_backend_receipt_requires_resolved_rollout_flags(build, flag):
    env, read = build
    env.pop(flag)
    with pytest.raises(ValueError, match="rollout flags"):
        producer.produce(env, "gateway-backend", read)


def test_migration_head_refuses_missing_parents_and_multiple_heads(tmp_path):
    (tmp_path / "first.py").write_text('revision = "first"\ndown_revision = "missing"\n')
    with pytest.raises(ValueError, match="complete head"):
        producer.migration_head(tmp_path)
    (tmp_path / "first.py").write_text('revision = "first"\ndown_revision = None\n')
    (tmp_path / "other.py").write_text('revision = "other"\ndown_revision = None\n')
    with pytest.raises(ValueError, match="complete head"):
        producer.migration_head(tmp_path)


def test_frontend_producer_hashes_exact_bytes_and_rejects_symlinks(build, tmp_path):
    env, read = build
    (tmp_path / "index.html").write_bytes(b"built frontend")
    release = ReleaseArtifact.model_validate(producer.produce(env, "gateway-frontend", read, tmp_path))
    assert release.assets == {"index.html": hashlib.sha256(b"built frontend").hexdigest()}
    (tmp_path / "link").symlink_to(tmp_path / "index.html")
    with pytest.raises(ValueError, match="symlinks"):
        producer.produce(env, "gateway-frontend", read, tmp_path)


@pytest.mark.parametrize("failure", ["account", "tag", "digest", "source"])
def test_producer_refuses_unverifiable_release(build, failure):
    env, read = build
    if failure == "account":
        env["ACCOUNT_ID"] = "999999999999"
    elif failure == "source":
        env["ADP_RELEASE_SOURCE"] = "main"
    else:
        real = read

        def read(args):
            data = real(args)
            if args[0] == "ecr":
                data["imageDetails"][0]["imageTags" if failure == "tag" else "imageDigest"] = [] if failure == "tag" else "latest"
            return data

    with pytest.raises(ValueError):
        producer.produce(env, "gateway-backend", read)


@pytest.mark.parametrize(
    "workflow,job,component",
    [
        ("gateway-deploy.yml", "deploy-backend", "gateway-backend"),
        ("gateway-deploy.yml", "deploy-frontend", "gateway-frontend"),
        ("run-gateway-migrations.yml", "migrate", "gateway-migrations"),
    ],
)
def test_actual_workflows_publish_bound_release_evidence(workflow, job, component):
    document = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    steps = document["jobs"][job]["steps"]
    records = [s for s in steps if "ADP_RELEASE_SOURCE" in s.get("env", {})]
    assert len(records) == 1
    record = records[0]
    expected_script = '"$RUNNER_TEMP/adp-deployment-evidence/scripts/deployment-release-evidence.py"'
    assert record["run"] == f"python3 {expected_script} {component}"
    assert record["env"]["GITHUB_REPOSITORY_ID"] == "${{ github.repository_id }}"
    assert record["env"]["ADP_RELEASE_SOURCE"] == "${{ inputs.manual_source_revision || inputs.adp_source_revision || github.sha }}"
    upload = steps[steps.index(record) + 1]
    assert "publish-deployment-evidence.py" in upload["run"]
    assert f" release {component} " in upload["run"]
    assert upload["run"].endswith('/release.json"')
    assert not any("upload-artifact" in step.get("uses", "") for step in steps)
    assert document["env"]["ADP_DEPLOYMENT_EVIDENCE_STORE"] == "s3-oidc-v1"


@pytest.fixture
async def release_provider(build):
    env, read = build
    document = producer.produce(env, "gateway-backend", read)
    context = WorkflowContext(
        **{k: v for k, v in document.items() if k in WorkflowContext.model_fields},
        resource_kind="eks-namespace",
        inputs={"environment": "dev"},
        correlation="",
    )
    ctx = SimpleNamespace(document=document, filename="release.json", tamper=False, missing=False, expired=False, calls=[])

    def archive():
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as zipped:
            zipped.writestr(ctx.filename, json.dumps(ctx.document))
        return data.getvalue()

    def response(request):
        ctx.calls.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer scoped-test"
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(
                200,
                json={
                    "artifacts": []
                    if ctx.missing
                    else [
                        {
                            "id": 88,
                            "name": "adp-release-gateway-backend-1",
                            "expired": ctx.expired,
                            "size_in_bytes": len(archive()),
                            "digest": "sha256:" + ("f" * 64 if ctx.tamper else hashlib.sha256(archive()).hexdigest()),
                        }
                    ]
                },
            )
        if request.url.path.endswith("/88/zip"):
            return httpx.Response(200, content=archive())
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        ctx.provider = ReleaseProvider(client=client)
        ctx.provider.token = AsyncMock(return_value="scoped-test")
        ctx.binding = SimpleNamespace(repo="org/repo", provider_repository_id=123)
        ctx.run = SimpleNamespace(run_id=42, run_attempt=1, context=context)
        yield ctx


async def test_artifact_authenticated_for_bound_provider_run(release_provider):
    ctx = release_provider
    release, digest, ref = await ctx.provider.release(ctx.binding, ctx.run, "gateway-backend")
    assert release.image_digest == DIGEST and len(digest) == 64 and ref.endswith("/88")


@pytest.mark.parametrize(
    "failure", ["source_revision", "repository_id", "run_attempt", "account_id", "component", "tamper", "missing", "expired", "filename"]
)
async def test_forged_missing_or_mismatched_build_evidence_refused(release_provider, failure):
    ctx = release_provider
    if failure in {"tamper", "missing", "expired"}:
        setattr(ctx, failure, True)
    elif failure == "filename":
        ctx.filename = "../release.json"
    else:
        ctx.document[failure] = {
            "source_revision": "c" * 40,
            "repository_id": 999,
            "run_attempt": 2,
            "account_id": "999999999999",
            "component": "gateway-migrations",
        }[failure]
    with pytest.raises(CycleBlockedError):
        await ctx.provider.release(ctx.binding, ctx.run, "gateway-backend")


@pytest.mark.parametrize(
    "status,base,permitted",
    [("behind", SHA, True), ("diverged", SHA, False), ("behind", "b" * 40, False), ("identical", SHA, False), ("ahead", SHA, False)],
)
async def test_newer_release_requires_provider_ancestry(status, base, permitted):
    """The ancestry question is asked as `compare/{actual}...{source}` (#7812): the pinned
    merge is an ancestor exactly when the provider reports `actual` *behind* it with the
    merge base at `source`. That direction carries no commits or patches, so a busy main
    (66 commits / 1.18 MB forward on 2026-10-15) stays inside the 1 MiB response bound."""
    provider = ReleaseProvider()
    provider.request = AsyncMock(
        side_effect=[httpx.Response(200, json={"id": 123}), httpx.Response(200, json={"status": status, "merge_base_commit": {"sha": base}})]
    )
    binding = SimpleNamespace(repo="org/repo", provider_repository_id=123)
    if permitted:
        await provider.contains(binding, SHA, "c" * 40)
    else:
        with pytest.raises(CycleBlockedError, match="deployment_newer_release_does_not_contain_merge"):
            await provider.contains(binding, SHA, "c" * 40)
    assert provider.request.await_args_list[1].args[2] == "/repos/org/repo/compare/" + "c" * 40 + "..." + SHA


async def test_s3_release_is_bound_to_context_and_retains_actual_version(release_provider):
    ctx = release_provider
    ctx.run.artifact_ref = "s3://bucket/context?versionId=context-version"
    reference = "s3://bucket/release?versionId=release-version"
    reads = []

    def read(**expected):
        reads.append(expected)
        payload = json.dumps(ctx.document).encode()
        return payload, hashlib.sha256(payload).hexdigest(), reference

    ctx.provider.evidence_store = SimpleNamespace(read=read)
    release, digest, ref = await ctx.provider.release(ctx.binding, ctx.run, "gateway-backend")
    assert release.image_digest == DIGEST and ref == reference and len(digest) == 64
    assert reads[0]["name"] == "gateway-backend" and reads[0]["workflow_revision"] == ctx.run.context.workflow_revision
    assert not ctx.calls
    ctx.document["source_revision"] = "c" * 40
    with pytest.raises(CycleBlockedError, match="identity_mismatch"):
        await ctx.provider.release(ctx.binding, ctx.run, "gateway-backend")


def test_interceptor_alias_release_matches_controller_schema():
    import importlib.util

    script = ROOT / "modules/tools/interceptor/release_evidence.py"
    spec = importlib.util.spec_from_file_location("interceptor_release", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    arn = "arn:aws:lambda:us-east-1:123456789012:function:example-interceptor"
    context = dict(
        schema_version=1,
        repository_id=123,
        run_id=42,
        run_attempt=1,
        source_revision=SHA,
        workflow_revision=SHA,
        workflow_path=".github/workflows/tools-interceptor-deploy.yml",
        account_id="123456789012",
        region="us-east-1",
        resource_kind="lambda-function",
        resource_id=arn,
    )
    release = module.build_release(context, {"version": "42", "code_sha256": "a" * 43 + "="}, 42, 1)
    parsed = ReleaseArtifact.model_validate(release)
    assert parsed.lambda_version == "42" and parsed.resource_id == arn


async def test_s3_release_read_falls_back_to_dev_when_the_context_environment_is_empty(release_provider):
    """#7829: the recorded context carries the raw workflow input (''), not the job's resolved value."""
    ctx = release_provider
    ctx.run.artifact_ref = "s3://bucket/context?versionId=context-version"
    ctx.run.context = ctx.run.context.model_copy(update={"inputs": {**ctx.run.context.inputs, "environment": ""}})
    reads = []

    def read(**expected):
        reads.append(expected)
        payload = json.dumps(ctx.document).encode()
        return payload, hashlib.sha256(payload).hexdigest(), "s3://bucket/release?versionId=release-version"

    ctx.provider.evidence_store = SimpleNamespace(read=read)
    await ctx.provider.release(ctx.binding, ctx.run, "gateway-backend")
    assert reads[0]["environment"] == "dev"


def test_relocated_actions_producer_resolves_candidate_from_workflow_workspace(build, tmp_path):
    env, read = build
    env["GITHUB_WORKSPACE"] = str(ROOT)
    relocated_path = tmp_path / "release-evidence.py"
    relocated_path.write_bytes((ROOT / "modules/gateway/scripts/deployment-release-evidence.py").read_bytes())
    selected = importlib.util.spec_from_file_location("relocated_release", relocated_path)
    module = importlib.util.module_from_spec(selected)
    selected.loader.exec_module(module)
    release = ReleaseArtifact.model_validate(module.produce(env, "gateway-backend", read))
    assert release.migration_head == producer.migration_head(ROOT / "modules/gateway/alembic/versions")


def test_product_recovery_release_keeps_prior_receipt_and_pending_readback(build, tmp_path):
    env, read = build
    env.update(RUNNER_TEMP=str(tmp_path), ADP_PROFILE_ROLLBACK="true")
    previous = {
        "baseline_source_revision": "c" * 40,
        "baseline_image_digest": "sha256:" + "d" * 64,
        "admissions_disabled_before_rollback": True,
        "migration_mode": "retained",
        "data_readback": "pending",
    }
    (tmp_path / "product-recovery.json").write_text(json.dumps(previous))
    release = ReleaseArtifact.model_validate(producer.produce(env, "gateway-backend", read))
    assert release.product_recovery.baseline_source_revision == previous["baseline_source_revision"]
    assert release.product_recovery.data_readback == "pending"
    env["ADP_PRODUCT_PROFILE_ENABLED"] = "true"
    with pytest.raises(ValueError, match="disabled"):
        producer.produce(env, "gateway-backend", read)


@pytest.mark.parametrize(
    "field,value",
    [
        ("migration_mode", "downgrade"),
        ("admissions_disabled_before_rollback", False),
        ("data_readback", "passed"),
    ],
)
def test_product_recovery_receipt_cannot_claim_unobserved_result(build, tmp_path, field, value):
    env, read = build
    env.update(RUNNER_TEMP=str(tmp_path), ADP_PROFILE_ROLLBACK="true")
    previous = {
        "baseline_source_revision": "c" * 40,
        "baseline_image_digest": "sha256:" + "d" * 64,
        "admissions_disabled_before_rollback": True,
        "migration_mode": "retained",
        "data_readback": "pending",
    }
    previous[field] = value
    (tmp_path / "product-recovery.json").write_text(json.dumps(previous))
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReleaseArtifact.model_validate(producer.produce(env, "gateway-backend", read))
