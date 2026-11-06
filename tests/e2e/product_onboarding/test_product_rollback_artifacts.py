"""Qualify signed immutable installed artifacts before an evaluator holds a Product."""

import io
import json
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import jwt
import pytest

from src.orchestration.deployment_runtime_contract import ReleaseArtifact
from tests.e2e.product_onboarding import product_rollback_artifacts as artifacts


def test_reviewed_main_history_is_available_to_live_baseline_qualification(tmp_path):
    workflow = (artifacts.ROOT / ".github/workflows/gateway-live-tests.yml").read_text()
    live_job = workflow.split("  live:\n", 1)[1].split("  recover_product:\n", 1)[0]
    recovery_job = workflow.split("  recover_product:\n", 1)[1]
    assert (
        "fetch-depth: 0"
        in live_job.split("- name: Assume gateway checks identity", 1)[0]
    )
    assert (
        "fetch-depth: 0"
        in recovery_job.split(
            "- name: Verify protected Product recovery configuration", 1
        )[0]
    )
    history_check = live_job.split(
        "- name: Verify reviewed Product source has complete main history", 1
    )[1]
    assert (
        "git fetch"
        not in history_check.split("- name: Assume gateway checks identity", 1)[0]
    )
    assert history_check.index(
        "git rev-parse --verify refs/remotes/origin/main"
    ) < history_check.index("git merge-base --is-ancestor")
    assert "refs/remotes/origin/main" in history_check
    assert artifacts.BASELINE_SOURCE in history_check
    assert artifacts.qualification.RUNTIME_FLOOR in history_check
    assert live_job.index(
        "- name: Verify reviewed Product source has complete main history"
    ) < live_job.index("- name: Run selected Product API cases")

    source = tmp_path / "source"
    source.mkdir()

    def git(*args, cwd=source, check=True):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=check, capture_output=True, text=True
        )

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (source / "revision").write_text("baseline")
    git("add", "revision")
    git("commit", "-m", "baseline")
    baseline = git("rev-parse", "HEAD").stdout.strip()
    (source / "revision").write_text("candidate")
    git("commit", "-am", "candidate")
    candidate = git("rev-parse", "HEAD").stdout.strip()

    shallow = tmp_path / "shallow"
    git("clone", "--depth=1", source.as_uri(), str(shallow))
    assert (
        git(
            "merge-base", "--is-ancestor", baseline, candidate, cwd=shallow, check=False
        ).returncode
        != 0
    )
    full = tmp_path / "full"
    git("clone", source.as_uri(), str(full))
    git("remote", "set-url", "origin", "https://example.invalid/private.git", cwd=full)
    git("rev-parse", "--verify", "refs/remotes/origin/main^{commit}", cwd=full)
    git("merge-base", "--is-ancestor", baseline, candidate, cwd=full)
    git(
        "merge-base",
        "--is-ancestor",
        candidate,
        "refs/remotes/origin/main",
        cwd=full,
    )
    assert (
        git(
            "merge-base", "--is-ancestor", candidate, baseline, cwd=full, check=False
        ).returncode
        != 0
    )


def release(source, digest, *, run, workflow, attempt=1):
    return ReleaseArtifact(
        schema_version=1,
        repository_id=17,
        run_id=run,
        run_attempt=attempt,
        source_revision=source,
        workflow_revision=workflow,
        workflow_path=".github/workflows/gateway-deploy.yml",
        account_id="000000000000",
        region="us-east-1",
        resource_id="cluster/adp-gateway",
        component="gateway-backend",
        image_digest=digest,
        product_profile_enabled=True,
        product_onboarding_enabled=False,
        product_profile_schema_version="1.0",
        migration_head="actual-head",
        cli_manifest_sha256="c" * 64,
        produced_at=datetime.now(UTC),
    ).model_dump(mode="json")


@pytest.fixture
def signed_baseline(monkeypatch):
    baseline = release(
        artifacts.BASELINE_SOURCE,
        "sha256:" + "b" * 64,
        run=artifacts.BASELINE_RUN,
        workflow="d" * 40,
        attempt=artifacts.BASELINE_ATTEMPT,
    )
    bucket = "adp-dev-deployment-evidence-000000000000"
    key = f"deployment-evidence/v1/17/{artifacts.BASELINE_RUN}/{artifacts.BASELINE_ATTEMPT}/release/gateway-backend.json"
    reference = f"s3://{bucket}/{key}?versionId=one"
    token = jwt.encode({"workflow_sha": "d" * 40}, "f" * 64, algorithm="HS256")
    raw = json.dumps({"signature": token}).encode()

    class Body(io.BytesIO):
        def close(self):
            super().close()

    values = {"raw": raw, "reference": reference, "read": True}

    def get_object(**kwargs):
        assert kwargs == {
            "Bucket": bucket,
            "Key": key,
            "ExpectedBucketOwner": "000000000000",
        }
        return {
            "Body": Body(values["raw"]),
            "ContentLength": len(values["raw"]),
            "VersionId": "one",
            "ServerSideEncryption": "AES256",
        }

    def read(**kwargs):
        assert kwargs["workflow_revision"] == "d" * 40
        assert kwargs["run_id"] == artifacts.BASELINE_RUN
        assert kwargs["attempt"] == artifacts.BASELINE_ATTEMPT
        assert kwargs["repository"] == "example/repo"
        return (
            (json.dumps(baseline).encode(), "digest", values["reference"])
            if values["read"]
            else None
        )

    store = SimpleNamespace(s3=SimpleNamespace(get_object=get_object), read=read)
    monkeypatch.setattr(artifacts, "EvidenceStore", lambda **_: store)
    return baseline, values


def test_installed_baseline_requires_signed_same_version_receipt(signed_baseline):
    baseline, _ = signed_baseline
    result, reference = artifacts.read_baseline(
        SimpleNamespace(client=lambda _: object()),
        account="000000000000",
        region="us-east-1",
        repository="example/repo",
        repository_id=17,
    )
    assert result == baseline
    assert reference.endswith("?versionId=one")


@pytest.mark.parametrize(
    "failure",
    [
        "wrong_version",
        "missing",
        "wrong_source",
        "wrong_workflow",
        "wrong_attempt",
        "invalid_hint",
    ],
)
def test_baseline_refuses_missing_stale_or_unverified_signed_receipt(
    signed_baseline, failure
):
    baseline, values = signed_baseline
    if failure == "wrong_version":
        values["reference"] = values["reference"].replace("one", "two")
    elif failure == "missing":
        values["read"] = False
    elif failure == "wrong_source":
        baseline["source_revision"] = "0" * 40
    elif failure == "wrong_workflow":
        baseline["workflow_revision"] = "0" * 40
    elif failure == "wrong_attempt":
        baseline["run_attempt"] = artifacts.BASELINE_ATTEMPT - 1
    else:
        values["raw"] = json.dumps(
            {"signature": jwt.encode({}, "f" * 64, algorithm="HS256")}
        ).encode()
    with pytest.raises((ValueError, KeyError)):
        artifacts.read_baseline(
            SimpleNamespace(client=lambda _: object()),
            account="000000000000",
            region="us-east-1",
            repository="example/repo",
            repository_id=17,
        )


@pytest.fixture
def pair(monkeypatch):
    source = "f" * 40
    candidate = release(source, "sha256:" + "a" * 64, run=42, workflow="d" * 40)
    baseline = release(
        artifacts.BASELINE_SOURCE,
        "sha256:" + "b" * 64,
        run=artifacts.BASELINE_RUN,
        workflow="d" * 40,
    )
    target = {
        field: candidate[field]
        for field in ("account_id", "region", "resource_id", "repository_id")
    }
    native = {
        "release_artifact": candidate,
        "target": target,
        "release_ref": "s3://private/candidate?versionId=one",
        "runtime_receipt": {
            "actual_revision": source,
            "image_digest": candidate["image_digest"],
            "migration_head": "actual-head",
            "product_profile_enabled": True,
            "product_onboarding_enabled": False,
        },
    }
    config = {"platform_account": target["account_id"], "region": target["region"]}
    selected = {"cluster": "cluster", "namespace": "adp-gateway"}
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    monkeypatch.setattr(artifacts.deployment_provenance, "binding", lambda _: selected)
    return config, native, baseline


def test_qualify_requires_actual_schema_reviewed_ancestry_and_both_immutable_tags(pair):
    config, native, baseline = pair
    expected = {
        native["release_artifact"]["image_digest"]: "f" * 40,
        baseline["image_digest"]: baseline["source_revision"],
    }
    result = artifacts.qualify_baseline(
        config,
        native,
        object(),
        baseline_reader=lambda *_args, **_kwargs: (
            baseline,
            "s3://private/baseline?versionId=one",
        ),
        ancestor=lambda *_: True,
        immutable_source=lambda _aws, _selected, digest: {
            "source_sha": expected[digest]
        },
    )
    assert result[1] == baseline and result[2].endswith("versionId=one")


@pytest.mark.parametrize("failure", ["schema", "target", "engine", "tag", "admissions"])
def test_qualify_rejects_mismatched_or_unsafe_baseline_before_fixture(pair, failure):
    config, native, baseline = pair
    tags = {
        native["release_artifact"]["image_digest"]: "f" * 40,
        baseline["image_digest"]: baseline["source_revision"],
    }
    if failure == "schema":
        baseline["migration_head"] = "stale-head"
    elif failure == "target":
        baseline["resource_id"] = "other-cluster/adp-gateway"
    elif failure == "tag":
        tags[baseline["image_digest"]] = "0" * 40
    elif failure == "admissions":
        native["runtime_receipt"]["product_profile_enabled"] = False
    with pytest.raises(ValueError):
        artifacts.qualify_baseline(
            config,
            native,
            object(),
            baseline_reader=lambda *_args, **_kwargs: (
                baseline,
                "s3://private/baseline?versionId=one",
            ),
            ancestor=lambda *_: failure != "engine",
            immutable_source=lambda _aws, _selected, digest: {
                "source_sha": tags[digest]
            },
        )
