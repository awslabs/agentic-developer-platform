"""refresh-controller.sh converges the live controller with the checkout, or does nothing.

Proven here with a fake ``kubectl`` on PATH (no cluster): the no-op when Dev Box is not
deployed, the exact sequence when it is, the refusal of a floating image tag, and the
two faults seen on dev 2026-10-19 — an unknown container name must be refused rather
than silently added (``kubectl set image`` has that property; a strategic patch does
not), and the stray ``dev-box-controller`` container that earlier bug created must be
removed. Background: #8208 merged, the gateway rolled twice, and the controller kept
old code and image for six hours because nothing called this path.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "refresh-controller.sh"
GATEWAY_IMAGE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@sha256:" + "a" * 64
RELEASED_IMAGE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@sha256:" + "b" * 64

FAKE_KUBECTL = """#!/usr/bin/env bash
# Records every invocation; answers the reads the script performs.
echo "$*" >> "$FAKE_KUBECTL_LOG"
case "$*" in
  *"get deploy/dev-box-controller -o jsonpath={range .spec.template.spec.containers[*]}"*)
    printf '%s\\n' $FAKE_CONTAINERS ;;
  *"annotations.adp"*"source-revision"*) printf '%s' "$FAKE_RUNNING_REVISION" ;;
  *"get deploy/dev-box-controller -o jsonpath"*) printf '%s' "$FAKE_GATEWAY_IMAGE" ;;
  *"get deploy/dev-box-controller"*) [ "$FAKE_CONTROLLER_PRESENT" = "1" ] && exit 0 || exit 1 ;;
  *"get deploy bedrockgateway"*) printf '%s' "$FAKE_GATEWAY_IMAGE" ;;
  *"create configmap"*) printf 'apiVersion: v1\\nkind: ConfigMap\\n' ;;
  *"apply -f -"*) cat >/dev/null ;;
  *"set image deploy/dev-box-controller"*) ;;
  *"patch deploy/dev-box-controller"*) ;;
  *"rollout status"*) [ "$FAKE_ROLLOUT_OK" = "1" ] && exit 0 || exit 1 ;;
  *"logs deploy/dev-box-controller"*) echo "controller log line" ;;
esac
exit 0
"""


@pytest.fixture
def fake_kubectl(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    kubectl = bindir / "kubectl"
    kubectl.write_text(FAKE_KUBECTL)
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "kubectl.log"
    log.write_text("")

    def run(
        *args,
        present=True,
        rollout_ok=True,
        gateway_image=GATEWAY_IMAGE,
        containers="controller",
        running_revision="",
        script=SCRIPT,
    ):
        env = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "FAKE_KUBECTL_LOG": str(log),
            "FAKE_CONTROLLER_PRESENT": "1" if present else "0",
            "FAKE_ROLLOUT_OK": "1" if rollout_ok else "0",
            "FAKE_GATEWAY_IMAGE": gateway_image,
            "FAKE_CONTAINERS": containers,
            "FAKE_RUNNING_REVISION": running_revision,
        }
        result = subprocess.run(["bash", str(script), *args], env=env, capture_output=True, text=True)
        return result, log.read_text().splitlines()

    return run


WRITE_VERBS = ("apply", "set image", "patch", "rollout")


def _verb(call):
    """The kubectl verb of a recorded call, ignoring a leading ``-n <namespace>``."""
    tokens = call.split()
    if tokens[:1] == ["-n"]:
        tokens = tokens[2:]
    return "set image" if tokens[:2] == ["set", "image"] else tokens[0]


def _writes(calls):
    return [c for c in calls if _verb(c) in WRITE_VERBS]


def test_no_controller_is_a_clean_no_op(fake_kubectl):
    result, calls = fake_kubectl(present=False)
    assert result.returncode == 0, result.stderr
    assert "nothing to refresh" in result.stdout
    assert not _writes(calls), calls


def test_refresh_applies_code_then_sets_image_and_revision_then_waits(fake_kubectl):
    result, calls = fake_kubectl("--image", RELEASED_IMAGE, "--source-revision", "f" * 40)
    assert result.returncode == 0, result.stderr
    writes = _writes(calls)
    kinds = [_verb(c) for c in writes]
    assert kinds == ["apply", "set image", "patch", "rollout"], kinds
    set_image = next(c for c in calls if " set image " in c)
    assert f"controller={RELEASED_IMAGE}" in set_image
    merge = next(c for c in calls if " patch " in c)
    assert "--type=merge" in merge
    body = json.loads(merge.split(" -p ", 1)[1])
    assert body["spec"]["template"]["metadata"]["annotations"]["adp.aws-e.io/source-revision"] == "f" * 40
    assert "containers" not in json.dumps(body), "the annotation patch must never touch containers"
    assert RELEASED_IMAGE in result.stdout


def test_image_defaults_to_the_gateway_image_the_environment_runs(fake_kubectl):
    result, calls = fake_kubectl("--source-revision", "1" * 40)
    assert result.returncode == 0, result.stderr
    assert any(f"controller={GATEWAY_IMAGE}" in c for c in calls if " set image " in c)


def test_unknown_container_name_is_refused_before_any_write(fake_kubectl):
    result, calls = fake_kubectl("--image", RELEASED_IMAGE, "--container", "dev-box-controller")
    assert result.returncode == 2
    assert "no container named dev-box-controller" in result.stderr
    assert not _writes(calls), calls


def test_stray_container_from_the_earlier_bug_is_removed(fake_kubectl):
    result, calls = fake_kubectl("--image", RELEASED_IMAGE, containers="dev-box-controller controller")
    assert result.returncode == 0, result.stderr
    removal = next(c for c in calls if " patch " in c and "--type=json" in c)
    ops = json.loads(removal.split(" -p ", 1)[1])
    assert ops == [{"op": "remove", "path": "/spec/template/spec/containers/0"}]
    assert "removing stray container dev-box-controller" in result.stdout
    # The real controller still gets the image after the removal.
    assert calls.index(removal) < next(i for i, c in enumerate(calls) if " set image " in c)


def test_floating_tag_is_refused_before_any_write(fake_kubectl):
    result, calls = fake_kubectl("--image", "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-gateway:latest")
    assert result.returncode == 2
    assert "pinned by digest" in result.stderr
    assert not _writes(calls), calls


def test_failed_rollout_prints_logs_and_fails(fake_kubectl):
    result, calls = fake_kubectl("--image", RELEASED_IMAGE, rollout_ok=False)
    assert result.returncode == 1
    assert any("logs deploy/dev-box-controller" in c for c in calls)
    assert "did not become ready" in result.stderr


def test_dry_run_reads_but_never_writes(fake_kubectl):
    result, calls = fake_kubectl("--dry-run", "--image", RELEASED_IMAGE)
    assert result.returncode == 0, result.stderr
    assert "dry-run" in result.stdout
    assert not _writes(calls), calls


@pytest.fixture
def history(tmp_path, fake_kubectl):
    """A copy of the script inside a throwaway git repository with two commits.

    CI checks the repository out shallowly, so the real history cannot stand in for
    "running" and "candidate"; the script resolves its repository from its own path, so
    a copied script in a temporary repository gives the ancestor check real commits.
    """
    repo = tmp_path / "repo"
    module = repo / "modules" / "user-services" / "dev-box"
    (module / "scripts").mkdir(parents=True)
    (module / "controller").mkdir()
    (module / "controller" / "worker.py").write_text("# controller\n")
    script = module / "scripts" / "refresh-controller.sh"
    script.write_text(SCRIPT.read_text())
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    git = ["git", "-C", str(repo)]
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run([*git, "init", "-q"], check=True, env=env)
    subprocess.run([*git, "add", "-A"], check=True, env=env)
    subprocess.run([*git, "commit", "-q", "-m", "older"], check=True, env=env)
    older = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True, env=env).stdout.strip()
    (module / "controller" / "worker.py").write_text("# controller v2\n")
    subprocess.run([*git, "commit", "-q", "-am", "newer"], check=True, env=env)
    newer = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True, env=env).stdout.strip()

    def run(*args, **kwargs):
        return fake_kubectl(*args, script=script, **kwargs)

    run.older, run.newer = older, newer
    return run


def test_older_candidate_keeps_the_running_code_and_only_moves_the_image(history):
    result, calls = history("--image", RELEASED_IMAGE, "--source-revision", history.older, running_revision=history.newer)
    assert result.returncode == 0, result.stderr
    assert f"refusing to roll the controller back from {history.newer} to {history.older}" in result.stdout
    kinds = [_verb(c) for c in _writes(calls)]
    assert kinds == ["set image", "rollout"], kinds
    assert not any(" patch " in c and "source-revision" in c for c in calls), "the recorded revision stays the running one"
    assert f"controller code kept at {history.newer}" in result.stdout


def test_older_candidate_with_the_same_image_is_a_no_op(history):
    result, calls = history("--source-revision", history.older, running_revision=history.newer)  # image defaults to the running one
    assert result.returncode == 0, result.stderr
    assert "nothing to refresh" in result.stdout and "refusing to roll the controller back" in result.stdout
    assert not _writes(calls), calls


def test_same_revision_refreshes_as_before(history):
    result, calls = history("--image", RELEASED_IMAGE, "--source-revision", history.newer, running_revision=history.newer)
    assert result.returncode == 0, result.stderr
    assert [_verb(c) for c in _writes(calls)] == ["apply", "set image", "patch", "rollout"]
    assert "refusing" not in result.stdout


def test_newer_candidate_applies_the_code(history):
    result, calls = history("--image", RELEASED_IMAGE, "--source-revision", history.newer, running_revision=history.older)
    assert result.returncode == 0, result.stderr
    assert [_verb(c) for c in _writes(calls)] == ["apply", "set image", "patch", "rollout"]
    assert f"controller code from {history.newer}" in result.stdout


def test_allow_downgrade_applies_an_older_candidate_deliberately(history):
    result, calls = history("--image", RELEASED_IMAGE, "--source-revision", history.older, "--allow-downgrade", running_revision=history.newer)
    assert result.returncode == 0, result.stderr
    assert [_verb(c) for c in _writes(calls)] == ["apply", "set image", "patch", "rollout"]
    assert "refusing" not in result.stdout


def test_unknown_running_revision_applies_and_says_so(history):
    result, calls = history("--image", RELEASED_IMAGE, "--source-revision", history.newer, running_revision="0" * 40)
    assert result.returncode == 0, result.stderr
    assert "not in this checkout's history; cannot compare" in result.stdout
    assert [_verb(c) for c in _writes(calls)] == ["apply", "set image", "patch", "rollout"]


def test_missing_annotation_applies_as_before(history):
    result, calls = history("--image", RELEASED_IMAGE, "--source-revision", history.newer, running_revision="")
    assert result.returncode == 0, result.stderr
    assert "running: unknown" in result.stdout
    assert [_verb(c) for c in _writes(calls)] == ["apply", "set image", "patch", "rollout"]


def test_dry_run_reports_the_image_only_decision(history):
    result, calls = history("--dry-run", "--image", RELEASED_IMAGE, "--source-revision", history.older, running_revision=history.newer)
    assert result.returncode == 0, result.stderr
    assert "would keep the running controller code and only set the controller image" in result.stdout
    assert not _writes(calls), calls
