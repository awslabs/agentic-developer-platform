"""The container-hardening checks stay in force for the gateway (#5675).

This suite exists because of how the original gap was hidden. `.github/security/
checkov.yml` suppressed CKV_DOCKER_3 (USER not set) and CKV_K8S_20
(allowPrivilegeEscalation) for the WHOLE repository, and CKV_DOCKER_3's written
justification claimed "the runtime is isolated inside EKS pods with non-root
security context enforced at the pod spec level."

That sentence was false when it was written and stayed false for as long as the
skip existed: modules/gateway/k8s/deployment.yaml had no securityContext of any
kind. So the scanner was not merely silent about the gateway running as root — it
carried a note telling every reviewer the opposite. That is the specific failure
this suite is designed to prevent from recurring, and it is why the assertions
target the CONFIG rather than the manifests (which
test_gateway_container_hardening.py covers).

The distinction that matters: a per-resource exception is a documented decision
about one workload, while a repository-wide skip silently covers every workload
added afterwards — including the one pod that holds the platform's signing keys.
These tests permit the former and reject the latter.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
CONFIG = ROOT / ".github/security/checkov.yml"

# Checks that must never be suppressed repository-wide again. Each maps to the
# concrete exposure a blanket skip would restore on the gateway pod.
PROTECTED = {
    "CKV_DOCKER_3": "a runtime image with no USER runs as root, defeating the image layer of the hardening",
    "CKV_K8S_20": "allowPrivilegeEscalation lets a setuid binary regain privilege the pod dropped",
}


def config():
    return yaml.safe_load(CONFIG.read_text())


@pytest.mark.parametrize("check_id", sorted(PROTECTED))
def test_hardening_checks_are_not_suppressed_repository_wide(check_id):
    skipped = config().get("skip-check") or []
    assert check_id not in skipped, (
        f"{check_id} is suppressed repository-wide in .github/security/checkov.yml, "
        f"which re-opens the gap issue #5675 closed: {PROTECTED[check_id]}. "
        f"If one workload genuinely needs an exception, scope it to that resource "
        f"(a `checkov.io/skip1` metadata annotation for Kubernetes, a "
        f"`#checkov:skip=` comment for a Dockerfile) so the gateway stays covered "
        f"and future workloads inherit enforcement by default."
    )


def test_the_disproved_justification_is_not_an_active_suppression_reason():
    """The old comment asserted the very control whose absence the check reports,
    so the claim must not come back as a live justification.

    Asserted against ACTIVE skip entries rather than the file text, because the
    current file quotes the false sentence on purpose — in a `NOTE:` explaining
    why it was wrong. A plain substring search over the whole file would match
    that historical citation and fail for the wrong reason (it did, on the first
    run of this suite). What must never return is the pairing of the claim with a
    real suppression.
    """
    skipped = set(config().get("skip-check") or [])
    assert "CKV_DOCKER_3" not in skipped, (
        "CKV_DOCKER_3 is suppressed again. Its historical justification — that the "
        "gateway's non-root security context was 'enforced at the pod spec level' — "
        "was false for as long as the skip existed, and the check now passes on "
        "merit, so there is nothing left to justify."
    )

    # The sentence may appear only in explanatory NOTE context, never attached to
    # a live skip. Locate it and confirm the surrounding block is commentary.
    text = CONFIG.read_text()
    marker = "the runtime is isolated inside EKS pods"
    if marker in text:
        line = next(ln for ln in text.splitlines() if marker in ln)
        assert line.lstrip().startswith("#"), f"the disproved claim must remain commentary, not an active entry: {line!r}"


def test_the_workflow_scans_the_repository_root():
    workflow = (ROOT / ".github/workflows/security-scan.yml").read_text()
    assert "--directory ." in workflow
    assert config()["directory"] == "."
    frameworks = config()["framework"]
    assert "kubernetes" in frameworks and "dockerfile" in frameworks


def test_exact_workflow_scope_has_no_unsuppressed_hardening_failures():
    checkov = shutil.which("checkov")
    if checkov is None:
        pytest.skip("checkov is installed by the security workflow")

    result = subprocess.run(
        [
            checkov,
            "--directory",
            ".",
            "--config-file",
            ".github/security/checkov.yml",
            "--baseline",
            ".github/security/checkov-baseline.json",
            "--check",
            ",".join(sorted(PROTECTED)),
            "--output",
            "json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_only_ingestion_has_a_local_runtime_user_exception():
    exceptions = []
    for dockerfile in ROOT.rglob("Dockerfile"):
        for line in dockerfile.read_text().splitlines():
            if "checkov:skip=CKV_DOCKER_3:" in line:
                exceptions.append((dockerfile.relative_to(ROOT).as_posix(), line.partition(":")[-1]))

    assert [path for path, _ in exceptions] == ["modules/agent-context/images/ingestion/Dockerfile"]
    assert len(exceptions[0][1].strip()) > 80


def test_privilege_escalation_has_no_resource_waivers():
    waivers = []
    for manifest in (*ROOT.rglob("*.yaml"), *ROOT.rglob("*.yml")):
        if "CKV_K8S_20" in manifest.read_text():
            waivers.append(manifest.relative_to(ROOT).as_posix())
    assert waivers == [".github/security/checkov.yml"]
