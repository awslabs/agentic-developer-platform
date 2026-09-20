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
        AWS_REGION="us-east-1",
        ENVIRONMENT="dev",
        AWS_SECRET_ACCESS_KEY="never-publish",
    )

    def read(args):
        return {"Account": env["ACCOUNT_ID"]} if args[0] == "sts" else {"imageDetails": [{"imageDigest": DIGEST, "imageTags": [SHA]}]}

    return env, read


def test_producer_backend_hashes_actual_image_and_never_exports_credentials(build):
    env, read = build
    release = ReleaseArtifact.model_validate(producer.produce(env, "gateway-backend", read))
    assert release.image_digest == DIGEST and release.source_revision == SHA
    assert "never-publish" not in release.model_dump_json()


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
        env["CUSTOMER_ACCOUNT_ID"] = "999999999999"
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
    record = next(s for s in steps if "deployment-release-evidence.py" in s.get("run", ""))
    assert record["run"].endswith(component)
    assert record["env"]["GITHUB_REPOSITORY_ID"] == "${{ github.repository_id }}"
    assert record["env"]["ADP_RELEASE_SOURCE"] == "${{ inputs.adp_source_revision || github.sha }}"
    upload = steps[steps.index(record) + 1]
    assert upload["with"]["name"] == f"adp-release-{component}-${{{{ github.run_attempt }}}}"
    assert upload["with"]["path"].endswith("/release.json")


@pytest.fixture
async def release_provider(build):
    env, read = build
    document = producer.produce(env, "gateway-backend", read)
    context = WorkflowContext(
        **{k: v for k, v in document.items() if k not in {"component", "image_digest", "assets", "produced_at"}},
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


@pytest.mark.parametrize("status,base,permitted", [("ahead", SHA, True), ("diverged", SHA, False), ("ahead", "b" * 40, False)])
async def test_newer_release_requires_provider_ancestry(status, base, permitted):
    provider = ReleaseProvider()
    provider.request = AsyncMock(
        side_effect=[httpx.Response(200, json={"id": 123}), httpx.Response(200, json={"status": status, "merge_base_commit": {"sha": base}})]
    )
    binding = SimpleNamespace(repo="org/repo", provider_repository_id=123)
    if permitted:
        await provider.contains(binding, SHA, "c" * 40)
    else:
        with pytest.raises(CycleBlockedError):
            await provider.contains(binding, SHA, "c" * 40)
