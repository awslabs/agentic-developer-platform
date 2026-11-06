"""Exercise the missing receipt producer without claiming a live deployment."""

import json
import shutil
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tests.e2e.product_onboarding import product_completion_observe as observer
from tests.e2e.product_onboarding import product_completion_release as release


def artifact(component="gateway-backend", **changes):
    from src.orchestration.deployment_runtime_contract import ReleaseArtifact

    document = {
        "schema_version": 1,
        "repository_id": 123,
        "run_id": 100,
        "run_attempt": 1,
        "workflow_path": ".github/workflows/gateway-deploy.yml",
        "workflow_revision": "b" * 40,
        "source_revision": "b" * 40,
        "account_id": "000000000000",
        "region": "us-east-1",
        "resource_id": "test-cluster/test-namespace",
        "component": component,
        "image_digest": "sha256:" + "c" * 64,
        "product_profile_schema_version": "1.0",
        "product_profile_enabled": True,
        "product_onboarding_enabled": True,
        "migration_head": "test_head",
        "cli_manifest_sha256": "d" * 64,
        "produced_at": datetime.now(UTC),
    }
    if component == "gateway-frontend":
        document.update(
            workflow_path=".github/workflows/gateway-frontend-deploy.yml",
            image_digest=None,
            assets={"index.html": "e" * 64},
        )
    return ReleaseArtifact(**{**document, **changes})


def test_fingerprint_tracks_staged_runtime_sources_but_ignores_acceptance_code(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(observer, "SOURCE_ROOT", tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)

    def commit(path, content):
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "test",
            ],
            check=True,
        )
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

    first = commit("modules/gateway/src/app.py", "runtime")
    second = commit("tests/e2e/product_onboarding/test_fixture.py", "acceptance")
    assert observer.fingerprint(first, "gateway-backend") == observer.fingerprint(
        second, "gateway-backend"
    )
    observer.compatible_source(first, second, "gateway-backend")
    third = commit("modules/harness/jobs/harness_jobs/identity.py", "staged runtime")
    assert observer.fingerprint(second, "gateway-backend") != observer.fingerprint(
        third, "gateway-backend"
    )
    with pytest.raises(ValueError, match="source differs"):
        observer.compatible_source(first, third, "gateway-backend")
    # Browser suites execute in isolated evidence directories, outside .git.
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    monkeypatch.chdir(evidence)
    observer.compatible_source(first, second, "gateway-backend")
    with pytest.raises(ValueError, match="cannot be verified"):
        observer.compatible_source(first, "f" * 40, "gateway-backend")
    with pytest.raises(ValueError, match="immutable"):
        observer.fingerprint("main", "gateway-backend")


def test_receipt_must_match_provider_identity_before_reuse(monkeypatch):
    selected = artifact()
    run = {
        "id": 100,
        "run_attempt": 1,
        "head_sha": "b" * 40,
        "path": selected.workflow_path,
        "repository": {"id": 123},
        "head_branch": "main",
        "conclusion": "success",
    }
    monkeypatch.setattr(observer, "github", lambda _: {"workflow_runs": [run]})
    monkeypatch.setattr(observer, "fingerprint", lambda *_: "a" * 64)

    def read(**kwargs):
        assert kwargs["workflow_revision"] == run["head_sha"]
        return selected.model_dump_json(), "c" * 64, "s3://signed-test-receipt"

    inputs = {
        "source": "a" * 40,
        "account": "000000000000",
        "region": "us-east-1",
        "environment": "dev",
        "repository": "aws-e/adp",
        "repository_id": 123,
        "store": SimpleNamespace(read=read),
    }
    assert observer.latest_release("gateway-backend", **inputs)[0] == selected
    selected = artifact(run_id=101)
    with pytest.raises(ValueError, match="differs from its workflow"):
        observer.latest_release("gateway-backend", **inputs)


@pytest.mark.parametrize("missing", [True, False])
def test_release_observation_rejects_missing_or_incompatible_receipts(
    monkeypatch, missing
):
    selected = artifact()
    monkeypatch.setattr(
        observer,
        "github",
        lambda _: {
            "workflow_runs": [
                {
                    "id": selected.run_id,
                    "run_attempt": selected.run_attempt,
                    "head_sha": selected.workflow_revision,
                    "path": selected.workflow_path,
                    "repository": {"id": selected.repository_id},
                    "head_branch": "main",
                    "conclusion": "success",
                }
            ]
        },
    )
    monkeypatch.setattr(observer, "fingerprint", lambda revision, _: revision[0] * 64)
    inputs = {
        "source": "a" * 40,
        "account": selected.account_id,
        "region": selected.region,
        "environment": "dev",
        "repository": "aws-e/adp",
        "repository_id": selected.repository_id,
        "store": SimpleNamespace(
            read=lambda **_: None
            if missing
            else (selected.model_dump_json(), "c" * 64, "s3://signed-test-receipt")
        ),
    }
    with pytest.raises(
        ValueError, match="No successful compatible gateway-deploy.yml release"
    ):
        observer.latest_release("gateway-backend", **inputs)


@pytest.mark.parametrize(
    "missing_file,corrupt_file",
    [(None, None), ("adp-aws.py", None), (None, "adp-product.py")],
)
def test_installed_cli_checks_every_served_file_in_isolated_directory(
    tmp_path, monkeypatch, missing_file, corrupt_file
):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "modules/gateway/cli"
    source.mkdir(parents=True)
    names = ("adp", "adp-product.py", "adp-onboard.py", "adp_common.py", "adp-aws.py")
    (source / "install.sh").write_text('CLI_FILES="' + " ".join(names) + '"\n')
    for name in names:
        (source / name).write_text("shipped " + name)
    root = tmp_path / "attempt"
    root.mkdir()
    calls = []

    def run(args, *, env, **kwargs):
        calls.append((args, env["BG_CONFIG_DIR"]))
        if args[0] == "bash":
            assert args[2:] == [
                "--gateway-url",
                "https://gateway.example.invalid",
                "--prefix",
                str(root / "cli"),
                "--no-path-edit",
            ]
            (root / "cli").mkdir()
            for name in names:
                if name != missing_file:
                    shutil.copyfile(source / name, root / "cli" / name)
            if corrupt_file:
                (root / "cli" / corrupt_file).write_text("changed in transit")
        return subprocess.CompletedProcess(args, 0, stdout="ok")

    monkeypatch.setattr(observer.subprocess, "run", run)
    if missing_file or corrupt_file:
        with pytest.raises(
            ValueError,
            match="Installed CLI file is missing"
            if missing_file
            else "differs from the selected source",
        ):
            observer.install_cli(root, "https://gateway.example.invalid", "a" * 40)
        assert len(calls) == 1
    else:
        installed = observer.install_cli(
            root, "https://gateway.example.invalid", "a" * 40
        )
        assert set(installed["files"]) == set(names)
        assert len(installed["commands"]) == 3
        assert len(calls) == 4
    assert all(config == str(root / "cli-config") for _, config in calls)


def test_contract_baseline_allows_merge_but_rejects_changed_workflow(
    tmp_path, monkeypatch
):
    from tests.e2e.product_onboarding.product_completion_context import (
        verify_contract_pins,
    )
    from tests.e2e.product_onboarding import product_completion_context

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(product_completion_context, "SOURCE_ROOT", tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    workflow = tmp_path / ".github/workflows/product-profile-release.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("reviewed workflow")

    def commit():
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "test",
            ],
            check=True,
        )
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

    approved = commit()
    (tmp_path / "README.md").write_text("Later change")
    merged = commit()
    contract = SimpleNamespace(baseline_revision=approved, definition_revision=approved)
    environment = {
        "ADP_SOURCE_REVISION": merged,
        "ADP_DEFINITION_REVISION": merged,
        "GITHUB_WORKFLOW_REF": "aws-e/adp/.github/workflows/product-profile-release.yml@refs/heads/main",
    }
    isolated = tmp_path / "evidence"
    isolated.mkdir()
    monkeypatch.chdir(isolated)
    verify_contract_pins(contract, environment)
    monkeypatch.chdir(tmp_path)
    workflow.write_text("unreviewed replacement")
    environment["ADP_DEFINITION_REVISION"] = commit()
    with pytest.raises(ValueError, match="differs from the accepted definition"):
        verify_contract_pins(contract, environment)
    contract.baseline_revision = environment["ADP_DEFINITION_REVISION"]
    with pytest.raises(ValueError, match="does not contain"):
        verify_contract_pins(contract, environment)


@pytest.mark.parametrize("enabled", [True, False])
def test_observation_creates_real_planner_inputs_only_after_runtime_verification(
    tmp_path, monkeypatch, enabled
):
    import boto3
    from src.orchestration import deployment_runtime_reader as readers

    tmp_path.chmod(0o700)
    backend = artifact()
    frontend = artifact("gateway-frontend")
    session = SimpleNamespace(
        client=lambda name: SimpleNamespace(
            get_caller_identity=lambda: {"Account": backend.account_id},
            describe_cluster=lambda **_: {"cluster": {"name": "test-cluster"}},
        )
    )
    monkeypatch.setattr(boto3, "Session", lambda **_: session)
    monkeypatch.setattr(
        observer,
        "latest_release",
        lambda component, **_: (
            backend if component == "gateway-backend" else frontend,
            "d" * 64,
            "s3://signed-test-receipt",
        ),
    )
    monkeypatch.setattr(observer, "fingerprint", lambda *_: "f" * 64)
    runtime = {
        "healthy": True,
        "product_profile_enabled": enabled,
        "product_onboarding_enabled": True,
        "migration_head": "test_head",
        "pod_uids": ["test-pod"],
    }
    monkeypatch.setattr(
        readers,
        "PilotRuntimeReader",
        lambda **_: SimpleNamespace(
            inspect=lambda *args, **kwargs: SimpleNamespace(
                model_dump=lambda **_: runtime
            ),
            frontend=lambda *args: 1,
        ),
    )
    monkeypatch.setattr(
        observer,
        "install_cli",
        lambda *args: {
            "sha256": "a" * 64,
            "files": {"adp": "a" * 64},
            "commands": [{"exit_code": 0}],
        },
    )
    environment = {
        "ADP_SOURCE_REVISION": "a" * 40,
        "ADP_DEFINITION_REVISION": "a" * 40,
        "GITHUB_REPOSITORY": "aws-e/adp",
        "GITHUB_REPOSITORY_ID": "123",
        "AWS_REGION": "us-east-1",
        "ACCOUNT_ID": "000000000000",
        "ADP_GATEWAY_BASE_URL": "https://gateway.example.invalid",
        "ADP_RUNTIME_PROOF_INVOKE_URL": "https://proof.example.invalid/dev",
        "GITHUB_WORKFLOW_SHA": "a" * 40,
        "GITHUB_SHA": "a" * 40,
        "ADP_CORRELATION": "d" * 64,
        "ADP_CONTRACT_DIGEST": "c" * 64,
        "PRODUCT_ENVIRONMENT": "dev",
        "PRODUCT_SUITE": "product-profile",
        "PRODUCT_MAX_PARALLEL": "8",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_RUN_ID": "101",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    if not enabled:
        with pytest.raises(ValueError, match="admissions"):
            observer.observe(tmp_path, environment)
        assert not (tmp_path / "desired.json").exists()
        return
    observer.observe(tmp_path, environment)
    release.write_plan(
        tmp_path / "desired.json",
        tmp_path / "current.json",
        tmp_path / "plan.json",
        environment=environment,
    )
    release.publish(
        tmp_path / "component-receipts.json",
        tmp_path / "manifest.json",
        desired_path=tmp_path / "desired.json",
        current_path=tmp_path / "current.json",
        plan_path=tmp_path / "plan.json",
        environment=environment,
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert set(manifest["components"]) == set(release.COMPONENTS)
    assert (
        manifest["deferred_integrations"]["engineering-insights"]["live_acceptance"]
        is False
    )
    assert all(
        (tmp_path / item["evidence_ref"]).is_file()
        for item in manifest["components"].values()
    )
