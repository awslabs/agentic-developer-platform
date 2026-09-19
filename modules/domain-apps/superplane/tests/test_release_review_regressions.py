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
@pytest.mark.parametrize(
    "bad_input", [None, "repository", "revision", "source", "context", "tag"]
)
def test_buildspec_runs_only_the_selected_domain_build(
    tmp_path, component, short, bad_input
):
    """The build script builds one component from maintained source, and nothing else.

    Updated for U22 (#5326): the build context is now an in-repository directory instead of a
    `releases/source/` staging area with a `.superplane-revision` marker. The marker check is
    replaced by checks on the thing itself — the maintained directory the lock names must
    exist and contain a Dockerfile — which is strictly stronger, because a marker file only
    ever asserted a revision rather than demonstrating the source was present.

    Every `bad_input` variant asserts the script fails *before* touching AWS or Docker, so a
    misconfigured lane cannot push a mislabelled image or authenticate against the wrong
    account as a side effect of finding out it was misconfigured.
    """
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
    # The maintained tree, laid out exactly as the transfer placed it: the build context is
    # <module root>/src/<component>, a sibling of releases/ rather than a directory under it.
    context = script.parent.parent / "src" / component
    context.mkdir(parents=True)
    (context / "Dockerfile").write_text("FROM scratch\n")
    if component == "superplane-api":
        shutil.copytree(
            ROOT / RELEASE.parent / "src/superplane-api/scripts", context / "scripts"
        )
        for package in ("auth", "contracts"):
            shutil.copytree(
                ROOT / RELEASE.parent / package, script.parent.parent / package
            )
        assert not (context / "vendor").exists()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    trace = tmp_path / "calls"
    for name in ("aws", "docker"):
        stub = bindir / name
        stub.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$BUILD_TRACE"\n'
            'case "$*" in *get-login-password*) echo test-password;; login*) cat >/dev/null;; esac\n'
            'if [ "$1" = build ] && [ "$SOURCE_PATH" = src/superplane-api ]; then\n'
            "  for package in auth contracts; do\n"
            '    test -f "modules/domain-apps/superplane/$SOURCE_PATH/vendor/superplane-$package/pyproject.toml" || exit 91\n'
            "  done\n"
            "fi\n"
        )
        stub.chmod(0o755)
    adp_commit = "c" * 40
    env = {
        "PATH": str(bindir) + ":" + os.defpath,
        "BUILD_TRACE": str(trace),
        # Provenance only — the script must not use this to fetch anything.
        "ORIGIN_REPOSITORY": "https://github.com/aws-innovate/AISuperPlane",
        "ORIGIN_REVISION": "a" * 40,
        "SOURCE_PATH": "src/" + component,
        "ECR_REPO": "adp-" + component,
        "ACCOUNT_ID": "111122223333",
        "AWS_REGION": "us-east-1",
        "REGISTRY": "111122223333.dkr.ecr.us-east-1.amazonaws.com",
        # The ADP commit, which is what identifies the built image after the transfer.
        "IMAGE_TAG": adp_commit,
    }
    if bad_input == "repository":
        env["ECR_REPO"] = "adp-gateway"
    if bad_input == "revision":
        env["ORIGIN_REVISION"] = "not-a-revision"
    if bad_input == "source":
        (context / "Dockerfile").unlink()
    if bad_input == "context":
        # A caller trying to redirect the build at a directory the lock does not name. The
        # script recomputes the context from the module root, so this must be rejected rather
        # than silently honoured — otherwise SOURCE_PATH stops being the source of truth.
        env["SUPERPLANE_SOURCE_DIR"] = "/tmp/somewhere-else"
    if bad_input == "tag":
        env["IMAGE_TAG"] = "latest"
    result = subprocess.run(
        ["bash", "-c", command], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    if bad_input:
        assert result.returncode != 0
        assert not trace.exists(), "invalid inputs must fail before AWS or Docker"
    else:
        assert result.returncode == 0, result.stderr
        if component == "superplane-api":
            for source, package, sentinel in (
                ("auth", "superplane_auth", "policy.py"),
                ("contracts", "superplane_contracts", "emission.py"),
            ):
                assert (
                    context / "vendor" / package.replace("_", "-") / package / sentinel
                ).read_bytes() == (
                    ROOT / RELEASE.parent / source / package / sentinel
                ).read_bytes()
        calls = trace.read_text()
        # Tagged by the ADP commit; the origin revision rides along as a label. Both are
        # asserted because collapsing them is the regression this guards.
        assert (
            "docker push " + env["REGISTRY"] + "/" + env["ECR_REPO"] + ":" + adp_commit
            in calls
        )
        assert "org.opencontainers.image.revision=" + adp_commit in calls
        assert "com.adp.superplane.origin.revision=" + "a" * 40 in calls
        assert (
            "org.opencontainers.image.source=modules/domain-apps/superplane/src/"
            + component
            in calls
        )
        # The origin revision must never become the tag: rebuilds from later ADP commits
        # would collide on it, so "which build is running" would stop having an answer.
        assert ":" + "a" * 40 not in calls
        assert all(
            token not in calls
            for token in (
                "terraform",
                "kubectl",
                "adp-gateway",
                "create-project",
                "update-project",
                # The reference snapshot is evidence, not a build input.
                "ai-super-plane",
            )
        )


def test_api_build_watches_every_staged_source_package():
    import re

    module = ROOT / RELEASE.parent
    stage = (module / "src/superplane-api/scripts/stage-domain-auth.sh").read_text()
    table = re.search(r"^packages=\((.*?)^\)", stage, re.MULTILINE | re.S)
    sources = re.findall(r'"([^":]+):', table.group(1))
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/superplane-api-build.yml").read_text()
    )
    paths = workflow.get("on", workflow.get(True))["push"]["paths"]
    assert sources
    for source in sources:
        assert f"{RELEASE.parent}/{source}/**" in paths
