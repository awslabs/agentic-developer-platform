"""The gateway container stays unprivileged, in all three places (#5675).

The gateway pod mounts `BG_TOKEN_SECRET_KEY` (the key that signs every platform
token), `BG_INTERNAL_API_KEY` (the shared secret other internal components
authenticate with), `BG_GITHUB_APP_PRIVATE_KEY` and both agent signing keys. It
ran as root with no securityContext at all, so any code execution in the process
reached every one of them: a moderate defect became theft of the platform's
signing keys, which is an incident nobody can contain by patching one bug because
it forces rotation of every credential the platform issues.

Three layers, deliberately redundant, asserted INDEPENDENTLY here:

  1. the image           — modules/gateway/Dockerfile sets USER 65532
  2. the workload        — k8s/deployment.yaml securityContext, pod AND container
  3. the namespace       — k8s/namespace.yaml Pod Security Admission labels

The redundancy is the point of the fix, so a test that only checked the effective
result would defeat it: each layer is one edit from removal, and any single
removal must fail this suite on its own. That is why the assertions below are
per-layer and why several of them look like they overlap.

Why layer 1 is asserted as *text* rather than by running the image: a container
runtime is not available where this suite runs. `runAsNonRoot: true` in layer 2
only FAILS a root image — it cannot make one non-root — so an image regression
would otherwise surface as a CrashLoopBackOff on deploy rather than a red test.

This static suite does not prove startup under these settings. The PR-time
credential-free gate using codebuild/bs-gateway-smoke.yml builds the exact image and runs
startup, readiness, and representative-request checks with the manifest-equivalent
UID, read-only root, scratch mounts, capability drop, and no-new-privileges controls.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
GATEWAY = ROOT / "modules/gateway"

# The unprivileged account the three layers have to agree on. A mismatch between
# the image's USER and the manifest's runAsUser is not a cosmetic inconsistency:
# the kubelet would refuse the pod, or it would run as a uid that does not own
# its own files.
EXPECTED_UID = 65532


def deployment():
    return yaml.safe_load((GATEWAY / "k8s/deployment.yaml").read_text())


def pod_spec():
    return deployment()["spec"]["template"]["spec"]


def gateway_container():
    return next(c for c in pod_spec()["containers"] if c["name"] == "bedrockgateway")


def dockerfile():
    return (GATEWAY / "Dockerfile").read_text()


# ---------------------------------------------------------------------------
# Layer 1 — the image
# ---------------------------------------------------------------------------


def test_image_runs_as_the_unprivileged_account():
    """Without this, `runAsNonRoot` in the manifest fails the pod instead of
    protecting it — the deploy breaks rather than the gateway being hardened."""
    text = dockerfile()
    assert f"USER {EXPECTED_UID}:{EXPECTED_UID}" in text, (
        "modules/gateway/Dockerfile must switch to the unprivileged account. "
        "Without a USER directive the image starts as root, and the pod's "
        "runAsNonRoot: true then REFUSES to start the container rather than "
        "silently protecting it."
    )
    assert f"useradd --create-home --uid {EXPECTED_UID} appuser" in text, (
        "The account must exist and own a HOME. botocore caches credential and "
        "endpoint data under HOME; with readOnlyRootFilesystem a homeless user "
        "fails mid-request instead of at startup."
    )
    assert "ENV HOME=/home/appuser" in text, (
        "The writable home volume must also be the process HOME; otherwise libraries fall back to root-owned or read-only cache paths."
    )


def test_image_switches_user_after_the_steps_that_need_privilege():
    """Ordering is the difference between a hardened image and a broken build:
    apt-get, the RDS CA fetch and the contract self-check all need root."""
    text = dockerfile()
    user_at = text.index(f"USER {EXPECTED_UID}:{EXPECTED_UID}")
    for privileged_step in ("apt-get update", "rds-global-bundle.pem", "RUN python src/auth/operation_contract.py"):
        assert text.index(privileged_step) < user_at, (
            f"{privileged_step!r} needs privilege and must stay ABOVE the USER directive, or the image build fails."
        )
    assert user_at < text.index("CMD ["), "USER must precede CMD, or the served process still runs as root."


def test_served_port_stays_bindable_by_an_unprivileged_process():
    """A port below 1024 cannot be bound without privilege, which would make the
    non-root switch an immediate startup failure."""
    port = gateway_container()["ports"][0]["containerPort"]
    assert port > 1024, f"port {port} is privileged; an unprivileged process cannot bind it"
    assert f"--port {port}" in dockerfile() or f'"{port}"' in dockerfile()


def test_application_files_remain_root_owned():
    """The image layer must protect immutable code and policy data even if the
    workload's read-only-root control is accidentally removed."""
    text = dockerfile()
    for tree in ("src/", "alembic/", "alembic.ini", "pricing_policy/", "contracts/", "cli/"):
        line = next(ln for ln in text.splitlines() if ln.startswith("COPY") and f" {tree}" in ln)
        assert "--chown" not in line, (
            f"COPY of immutable {tree} must retain root ownership; granting it "
            f"to uid {EXPECTED_UID} lets the gateway rewrite it whenever the "
            f"workload read-only control regresses: {line!r}"
        )


# ---------------------------------------------------------------------------
# Layer 2 — the workload, asserted at BOTH levels
# ---------------------------------------------------------------------------


def test_pod_level_security_context_requires_non_root():
    sc = pod_spec()["securityContext"]
    assert sc["runAsNonRoot"] is True
    assert sc["runAsUser"] == EXPECTED_UID
    assert sc["seccompProfile"]["type"] == "RuntimeDefault"
    # fsGroup so the unprivileged user can write to the emptyDir scratch mounts.
    assert sc["fsGroup"] == EXPECTED_UID


@pytest.mark.parametrize(
    "field,expected,why",
    [
        ("runAsNonRoot", True, "root in this pod reaches every mounted signing key and secret"),
        ("allowPrivilegeEscalation", False, "setuid binaries could otherwise regain privilege the pod dropped"),
        ("privileged", False, "a privileged container is equivalent to root on the node"),
        ("readOnlyRootFilesystem", True, "the process must not be able to rewrite its own code or pinned pricing data"),
    ],
)
def test_container_level_security_context(field, expected, why):
    """Asserted on the CONTAINER, not just the pod, because a container-level
    securityContext silently OVERRIDES the pod default. A future container that
    omits a field it inherits today would not read as a regression in review."""
    sc = gateway_container()["securityContext"]
    assert sc[field] is expected, f"container securityContext.{field} must be {expected}: {why}"


def test_container_drops_all_linux_capabilities():
    """capabilities and readOnlyRootFilesystem have NO pod-level equivalent —
    they exist only on the container, so this cannot be inherited."""
    assert gateway_container()["securityContext"]["capabilities"]["drop"] == ["ALL"]


def test_container_seccomp_profile_is_set():
    assert gateway_container()["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"


def test_read_only_root_has_writable_scratch_for_runtime_caches():
    """readOnlyRootFilesystem is the outage-prone half of this change: a library
    writing a cache on first use fails at runtime, not at admission."""
    container = gateway_container()
    mounts = {m["mountPath"]: m["name"] for m in container["volumeMounts"]}
    assert "/tmp" in mounts, "Python's default temp dir must be writable under a read-only root"
    assert "/home/appuser" in mounts, "botocore writes credential/endpoint caches under HOME"

    volumes = {v["name"]: v for v in pod_spec()["volumes"]}
    for name in mounts.values():
        assert "emptyDir" in volumes[name], f"{name} must be an emptyDir — not hostPath, which would share node state"
        assert "sizeLimit" in volumes[name]["emptyDir"], (
            f"{name} needs emptyDir.sizeLimit so a runaway write cannot exhaust node ephemeral storage and evict the pod"
        )


def test_application_directory_is_not_writable():
    """The whole point of a read-only root here: /app holds the code, the pinned
    pricing snapshots and the review contract. A writable /app would let a
    compromised process rewrite its own pricing data."""
    paths = [m["mountPath"] for m in gateway_container()["volumeMounts"]]
    assert not any(p == "/app" or p.startswith("/app/") for p in paths), f"/app must stay read-only, got writable mounts: {paths}"


def test_codebuild_runs_the_exact_image_with_manifest_restrictions():
    buildspec = (ROOT / "codebuild/bs-gateway-smoke.yml").read_text()
    for required in (
        "docker run --rm --entrypoint sh adp-gateway:pr-smoke",
        "find /app ! -uid 0",
        "find /app \\( -type f -o -type d \\) -perm /022",
        "touch /tmp/image-layer-write",
        'touch "$HOME/image-layer-write"',
        "touch /app/image-layer-must-stay-read-only",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "uid=65532,gid=65532",
        'test "$(id -u)" = 65532',
        "touch /tmp/runtime-write",
        'touch "$HOME/runtime-write"',
        "touch /app/must-stay-read-only",
        "-c 'import asyncpg'",
        "BG_DATABASE_URL=postgresql+asyncpg://",
        "http://127.0.0.1:18080/health",
        "http://127.0.0.1:18080/ready",
        "http://127.0.0.1:18080/v1/health",
        'docker exec "$CONTAINER_ID" id -u',
        "/proc/1/status",
        "^CapEff:",
        "0000000000000000",
        "^NoNewPrivs:",
    ):
        assert required in buildspec
    assert "sqlite+aiosqlite" not in buildspec, "the runtime image does not install the test-only SQLite driver"
    assert "-e DATABASE_URL=" not in buildspec, "gateway settings ignore database overrides without the BG_ prefix"


def test_pr_image_gate_needs_no_platform_identity_or_deployed_build_project():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gateway-ci.yml").read_text())
    job = workflow["jobs"]["build"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["permissions"] == {"contents": "read"}
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] is False
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "python platform/scripts/gateway-image-smoke.py" in commands
    assert "aws " not in commands and "docker push" not in commands
    assert not any("configure-aws-credentials" in step.get("uses", "") for step in job["steps"])


@pytest.mark.parametrize("fail_build", [False, True])
def test_canonical_smoke_phases_share_the_directory_and_stop_on_build_failure(tmp_path, fail_build):
    import importlib.util

    spec = importlib.util.spec_from_file_location("image_smoke", ROOT / "platform/scripts/gateway-image-smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "spec.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 0.2,
                "phases": {
                    "build": {"commands": ["mkdir stage", "cd stage", "exit 7" if fail_build else "printf built > image"]},
                    "post_build": {"commands": ['test "$(cat image)" = built', "touch checked"]},
                },
            }
        )
    )
    if fail_build:
        with pytest.raises(subprocess.CalledProcessError):
            module.run_buildspec(path, cwd=tmp_path)
        assert not (tmp_path / "stage/checked").exists()
    else:
        module.run_buildspec(path, cwd=tmp_path)
        assert (tmp_path / "stage/checked").exists()


# ---------------------------------------------------------------------------
# Layer 3 — the namespace
# ---------------------------------------------------------------------------

# adp-gateway is declared TWICE. Either file can be applied last, so labels on
# only one of them would make enforcement depend on apply order — present in
# review, silently gone after an unrelated deploy.
NAMESPACE_FILES = ["modules/gateway/k8s/namespace.yaml", "platform/k8s/namespaces.yaml"]


def gateway_namespace_labels(relative_path, name="adp-gateway"):
    docs = [d for d in yaml.safe_load_all((ROOT / relative_path).read_text()) if d]
    ns = next(d for d in docs if d.get("kind") == "Namespace" and d["metadata"]["name"] == name)
    return ns["metadata"]["labels"]


@pytest.mark.parametrize("relative_path", NAMESPACE_FILES)
def test_every_declaration_of_the_namespace_carries_admission_labels(relative_path):
    labels = gateway_namespace_labels(relative_path)
    modes = {m: labels.get(f"pod-security.kubernetes.io/{m}") for m in ("enforce", "warn", "audit")}
    assert modes == {"enforce": "restricted", "warn": "restricted", "audit": "restricted"}, (
        f"{relative_path} must carry the restricted Pod Security Admission labels; "
        f"got {modes}. Labelling only one declaration of adp-gateway makes "
        f"enforcement depend on which file is applied last."
    )


def test_both_declarations_agree_exactly():
    """Drift between the two files is the failure this pairing exists to catch."""
    first, second = ({k: v for k, v in gateway_namespace_labels(p).items() if k.startswith("pod-security")} for p in NAMESPACE_FILES)
    assert first == second, f"Pod Security Admission labels differ between {NAMESPACE_FILES}: {first} vs {second}"


def test_root_based_eval_pods_use_the_dedicated_namespace():
    eval_scripts = [
        "platform/evals/cli-onboarding/run-eval.sh",
        "platform/evals/budget-ratelimit/run-eval.sh",
        "platform/evals/bedrock-routing/run-eval.sh",
    ]
    for relative_path in eval_scripts:
        text = (ROOT / relative_path).read_text()
        assert "EVAL_POD_NAMESPACE:-adp-gateway-evals" in text, relative_path

    labels = gateway_namespace_labels("modules/gateway/k8s/eval-namespace.yaml", "adp-gateway-evals")
    assert labels["pod-security.kubernetes.io/enforce"] == "baseline"
    assert labels["pod-security.kubernetes.io/warn"] == "restricted"

    rbac = (ROOT / "modules/agent-factory/infra/runner-rbac.tf").read_text()
    eval_role = rbac[rbac.index('resource "kubernetes_role" "runner_gateway_evals"') :]
    eval_role = eval_role[: eval_role.index('resource "kubernetes_role_binding" "runner_gateway_evals"')]
    assert 'resources  = ["pods"]' in eval_role
    assert 'resources  = ["pods/exec", "pods/log"]' in eval_role
    assert '"secrets"' not in eval_role
    assert '"deployments"' not in eval_role


def test_automatic_deploy_never_mutates_cluster_scoped_namespaces():
    workflow = (ROOT / ".github/workflows/gateway-deploy.yml").read_text()
    assert "kubectl create namespace ${{ env.NAMESPACE }}" not in workflow
    assert "kubectl get namespace ${{ env.NAMESPACE }}" in workflow
    assert "kubectl apply -f modules/gateway/k8s/namespace.yaml" not in workflow
    assert "verify-restricted-admission.sh" not in workflow
    assert 'kubectl create --dry-run=client --validate=false -f "$f"' in workflow
    assert "grep -qx Namespace" in workflow

    rbac = (ROOT / "modules/agent-factory/infra/runner-rbac.tf").read_text()
    namespace_role = rbac[rbac.index('resource "kubernetes_cluster_role" "runner_namespace_manage"') :]
    assert "adp-gateway-evals" not in namespace_role
    assert 'verbs          = ["get", "list", "delete"]' in namespace_role


def test_deploy_all_enforces_after_rollout_and_probes_admission():
    deploy_all = (ROOT / "platform/scripts/deploy-all.sh").read_text()
    assert "kubectl create namespace adp-gateway --dry-run=client" not in deploy_all
    assert "kubectl get namespace adp-gateway >/dev/null 2>&1 || kubectl create namespace adp-gateway" in deploy_all
    assert 'kubectl create --dry-run=client --validate=false -f "$f"' in deploy_all
    assert "grep -qx Namespace" in deploy_all

    update_rollout = deploy_all.index("kubectl rollout status deployment/bedrockgateway", deploy_all.index('if [ "$UPDATE_MODE" = true ]'))
    fresh_rollout = deploy_all.index("kubectl rollout status deployment/bedrockgateway", update_rollout + 1)
    enforce = deploy_all.index("kubectl apply -f k8s/namespace.yaml", fresh_rollout)
    probe = deploy_all.index("scripts/verify-restricted-admission.sh adp-gateway", enforce)
    assert update_rollout < fresh_rollout < enforce < probe

    script = (GATEWAY / "scripts/verify-restricted-admission.sh").read_text()
    assert 'kubectl apply --dry-run=server --validate=false -f "$deployment"' in script
    assert "--verify-admission" in script
    assert "--dry-run=server" in script
    assert "privileged: true" in script
    assert '"violates PodSecurity"' in script
    assert '"restricted"' in script


def test_every_hardening_input_triggers_focused_pull_request_ci():
    workflow = (ROOT / ".github/workflows/gateway-hardening-ci.yml").read_text()
    for protected_path in (
        "'**/Dockerfile'",
        "'**/*.yaml'",
        "'**/*.yml'",
        "'.github/security/checkov-baseline.json'",
        "'modules/agent-factory/infra/main.tf'",
        "'modules/agent-factory/infra/runner-rbac.tf'",
        "'platform/infra/modules/eks/main.tf'",
        "'platform/evals/**/*.sh'",
        "'platform/scripts/deploy-all.sh'",
        "'modules/gateway/scripts/migrate-before-rollout.py'",
        "'modules/gateway/scripts/verify-restricted-admission.sh'",
        "'modules/gateway/tests/infra/test_checkov_suppression_scope.py'",
        "'modules/gateway/tests/infra/test_gateway_container_hardening.py'",
    ):
        assert protected_path in workflow
    assert "pull_request:" in workflow
    assert "--confcutdir=modules/gateway/tests/infra" in workflow
    assert "modules/gateway/tests/infra/test_gateway_container_hardening.py" in workflow
    assert "modules/gateway/tests/infra/test_checkov_suppression_scope.py" in workflow


# ---------------------------------------------------------------------------
# The migration Job inherits all of it
# ---------------------------------------------------------------------------


def test_pre_serve_migration_job_inherits_the_hardening():
    """scripts/migrate-before-rollout.py deep-copies this pod spec, and the Job is
    created BEFORE the Deployment is applied. If it did not inherit the settings
    it would be the first thing refused under namespace enforcement, failing the
    deploy before rollout — so this is asserted rather than assumed."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("pre_serve_migration", GATEWAY / "scripts/migrate-before-rollout.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    job = script.migration_job(deployment(), "registry/gateway:reviewed", "adp-gateway", "migration-test")
    job_pod = job["spec"]["template"]["spec"]
    job_container = job_pod["containers"][0]

    assert job_pod["securityContext"] == pod_spec()["securityContext"]
    assert job_container["securityContext"] == gateway_container()["securityContext"]
    # The alembic process needs the same scratch space: it runs under the same
    # read-only root.
    assert {v["name"] for v in job_pod["volumes"]} == {v["name"] for v in pod_spec()["volumes"]}
    assert {m["mountPath"] for m in job_container["volumeMounts"]} == {m["mountPath"] for m in gateway_container()["volumeMounts"]}
