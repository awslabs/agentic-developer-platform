"""Execute the declared buildspecs with stub tooling; no AWS/Docker access."""

import os
import shutil
import subprocess
from pathlib import Path

import _release_path  # noqa: F401
import pytest
import yaml
from releases.resolve_lock import (
    BuildInputs,
    LockError,
    resolve_build_inputs,
    resolved_digest,
)

ROOT = Path(__file__).resolve().parents[4]
RELEASE = Path("modules/domain-apps/superplane/releases")


@pytest.mark.parametrize(
    "component",
    ["superplane-api", "superplane-controller", "superplane-platform-monitor"],
)
def test_first_digest_promotion_preserves_rebuild_inputs(tmp_path, component):
    data = yaml.safe_load((ROOT / RELEASE / "superplane.lock.yaml").read_text())
    data["source_access"]["status"] = "resolved"
    lock = tmp_path / "lock.yaml"
    lock.write_text(yaml.safe_dump(data))
    first = resolve_build_inputs(component, lock)
    metadata = data["pending_images"].pop(component)
    metadata.pop("blocked_by")
    data["image_sources"][component] = metadata
    data["images"][component] = "sha256:" + "a" * 64
    lock.write_text(yaml.safe_dump(data))
    assert resolve_build_inputs(component, lock) == first
    assert resolved_digest(component, lock) == "sha256:" + "a" * 64

    del data["image_sources"][component]
    lock.write_text(yaml.safe_dump(data))
    with pytest.raises(LockError, match="no build source metadata"):
        resolve_build_inputs(component, lock)


def test_image_cannot_be_pending_and_resolved(tmp_path):
    data = yaml.safe_load((ROOT / RELEASE / "superplane.lock.yaml").read_text())
    data["images"]["superplane-api"] = "sha256:" + "a" * 64
    lock = tmp_path / "lock.yaml"
    lock.write_text(yaml.safe_dump(data))
    with pytest.raises(LockError, match="both pending and resolved"):
        resolve_build_inputs("superplane-api", lock)


@pytest.mark.parametrize(
    "digest", ["sha256:", "sha256:latest", "sha256:" + "0" * 64, "sha256:" + "g" * 64]
)
def test_malformed_or_placeholder_digest_never_reaches_a_deploy(tmp_path, digest):
    lock = tmp_path / "lock.yaml"
    lock.write_text(
        yaml.safe_dump({"upstream": {"revision": "a" * 40}, "images": {"test": digest}})
    )
    with pytest.raises(LockError):
        resolved_digest("test", lock)


@pytest.mark.parametrize(
    "revision", ["main", "a" * 40 + "\nINJECTED=value", "$(touch bad)"]
)
def test_build_inputs_cannot_inject_workflow_environment(revision):
    with pytest.raises(LockError):
        BuildInputs(
            "superplane-api",
            "https://github.com/aws-innovate/AISuperPlane",
            revision,
            "src/superplane-api",
            "adp-superplane-api",
        ).as_env_lines()


@pytest.mark.parametrize(
    "component,short",
    [
        ("superplane-api", "api"),
        ("superplane-controller", "controller"),
        ("superplane-platform-monitor", "monitor"),
    ],
)
@pytest.mark.parametrize("bad_input", [None, "repository", "revision", "source"])
def test_buildspec_runs_only_the_selected_domain_build(
    tmp_path, component, short, bad_input
):
    spec_path = RELEASE / "buildspecs" / (short + ".yml")
    workflow = (
        ROOT / ".github/workflows" / ("superplane-" + short + "-build.yml")
    ).read_text()
    assert str(spec_path) in workflow
    spec = yaml.safe_load((ROOT / spec_path).read_text())
    command = spec["phases"]["build"]["commands"][0]
    script = tmp_path / RELEASE / "build-image.sh"
    script.parent.mkdir(parents=True)
    shutil.copy(ROOT / RELEASE / "build-image.sh", script)
    source = script.parent / "source"
    context = source / "src" / component
    context.mkdir(parents=True)
    (context / "Dockerfile").write_text("FROM scratch\n")
    (source / ".superplane-revision").write_text("a" * 40)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    trace = tmp_path / "calls"
    for name in ("aws", "docker"):
        stub = bindir / name
        stub.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$BUILD_TRACE"\n'
            'case "$*" in *get-login-password*) echo test-password;; login*) cat >/dev/null;; esac\n'
        )
        stub.chmod(0o755)
    env = {
        "PATH": str(bindir) + ":" + os.defpath,
        "BUILD_TRACE": str(trace),
        "UPSTREAM_REVISION": "a" * 40,
        "UPSTREAM_PATH": "src/" + component,
        "ECR_REPO": "adp-" + component,
        "ACCOUNT_ID": "111122223333",
        "AWS_REGION": "us-east-1",
        "REGISTRY": "111122223333.dkr.ecr.us-east-1.amazonaws.com",
    }
    if bad_input == "repository":
        env["ECR_REPO"] = "adp-gateway"
    if bad_input == "revision":
        env["UPSTREAM_REVISION"] = "b" * 40
    if bad_input == "source":
        (source / ".superplane-revision").unlink()
    result = subprocess.run(
        ["bash", "-c", command], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    if bad_input:
        assert result.returncode != 0
        assert not trace.exists(), "invalid inputs must fail before AWS or Docker"
    else:
        assert result.returncode == 0, result.stderr
        calls = trace.read_text()
        assert (
            "docker build --label org.opencontainers.image.revision=" + "a" * 40
            in calls
        )
        assert (
            "docker push " + env["REGISTRY"] + "/" + env["ECR_REPO"] + ":" + "a" * 40
            in calls
        )
        assert all(
            token not in calls
            for token in (
                "terraform",
                "kubectl",
                "adp-gateway",
                "create-project",
                "update-project",
            )
        )
