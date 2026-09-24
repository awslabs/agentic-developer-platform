"""A rollout cannot mutate the cluster outside the domain's boundary — Issue #5042 (U3).

## The reproduction these tests lock down

PR #5283's review (finding 4) fed the rollout lane's rendered guard a digest-pinned Deployment
in namespace `adp` together with a ClusterRoleBinding to `cluster-admin`. It returned **0** and
printed "all digest-pinned", because it checked image SHAPE and nothing else.

`test_reproduced_case_core_namespace_plus_cluster_admin_binding` is that exact fixture, and
`test_the_old_digest_only_check_would_have_passed_it` runs the SUPERSEDED logic against the same
fixture to show the difference is the new check rather than the fixture being obviously invalid.
Without that second test the first proves only that a hostile manifest fails — not that it used
to pass.

## Why these run the real scripts as subprocesses

The properties under test are about the rollout lane's behaviour at its entry point: what exit
code the workflow's step sees, and whether `kubectl` is reached at all. Importing `validate()`
and asserting on its return value would test a function the workflow does not call directly, and
would not establish ordering. `test_rejected_manifests_never_reach_kubectl` therefore puts a stub
`kubectl` on PATH that logs its invocations, runs the guard, and asserts the log is empty.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".github" / "workflows").is_dir():
            return candidate
    raise AssertionError("could not locate the repository root from this test file")


REPO_ROOT = _repo_root()
MODULE_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = MODULE_ROOT / "infra" / "scripts"
GUARD = SCRIPTS_DIR / "check_rendered_manifests.py"
RENDERER = SCRIPTS_DIR / "render_manifests.py"
MANIFEST_DIR = MODULE_ROOT / "k8s"
LOCK_FILE = MODULE_ROOT / "releases" / "superplane.lock.yaml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "superplane-k8s-deploy.yml"

ACCOUNT = "879318057152"
ENVIRONMENT = "dev"
NAMESPACE = "superplane"
SKYPILOT_NAMESPACE = "skypilot"

# The digest U2's lock actually resolves for skypilot-api. Read from the lock rather than
# copied, so this test cannot drift from the contract it is asserting against.
PINNED_DIGEST = yaml.safe_load(LOCK_FILE.read_text(encoding="utf-8"))["images"][
    "skypilot-api"
]
PINNED_IMAGE = f"registry-1.docker.io/berkeleyskypilot/skypilot@{PINNED_DIGEST}"

# A syntactically perfect digest that the lock does not pin. This is the distinction the old
# guard could not make: 64 hex characters is a shape, not a provenance.
UNPINNED_IMAGE = "registry-1.docker.io/berkeleyskypilot/skypilot@sha256:" + "ab" * 32

RENDER_ENV = {
    "SP_NAMESPACE": NAMESPACE,
    "SP_SKYPILOT_NAMESPACE": SKYPILOT_NAMESPACE,
    "SP_CONTROL_PLANE_ROLE_ARN": f"arn:aws:iam::{ACCOUNT}:role/adp-{ENVIRONMENT}-superplane-control-plane",
    "SP_SKYPILOT_ROLE_ARN": f"arn:aws:iam::{ACCOUNT}:role/adp-{ENVIRONMENT}-superplane-skypilot-api",
    "SP_SKYPILOT_IMAGE": PINNED_IMAGE,
    "SP_DATABASE_SECRET_NAME": f"adp/{ENVIRONMENT}/superplane/database",
    "SP_JWT_SECRET_NAME": f"adp/{ENVIRONMENT}/superplane/jwt-signing-key",
    "SP_AWS_REGION": "us-east-1",
}


def _run_guard(directory: Path, **overrides) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(GUARD),
        "--rendered-dir",
        str(directory),
        "--lock-file",
        str(overrides.get("lock_file", LOCK_FILE)),
        "--account-id",
        overrides.get("account_id", ACCOUNT),
        "--environment",
        overrides.get("environment", ENVIRONMENT),
    ]
    for namespace in overrides.get("namespaces", [NAMESPACE, SKYPILOT_NAMESPACE]):
        command += ["--namespace", namespace]
    return subprocess.run(command, capture_output=True, text=True)


def _write(directory: Path, *documents: dict, name: str = "manifest.yaml") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        "\n---\n".join(yaml.safe_dump(doc, sort_keys=False) for doc in documents),
        encoding="utf-8",
    )
    return directory


def _deployment(
    *,
    namespace: str = SKYPILOT_NAMESPACE,
    image: str = PINNED_IMAGE,
    service_account: str = "skypilot-api",
    resources: dict | None = None,
    pod_extra: dict | None = None,
    container_extra: dict | None = None,
) -> dict:
    container = {
        "name": "skypilot-api",
        "image": image,
        "resources": resources
        if resources is not None
        else {
            "requests": {"cpu": "200m", "memory": "512Mi"},
            "limits": {"cpu": "1", "memory": "2Gi"},
        },
    }
    container.update(container_extra or {})
    pod_spec = {"serviceAccountName": service_account, "containers": [container]}
    pod_spec.update(pod_extra or {})
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "skypilot-api", "namespace": namespace},
        "spec": {
            "selector": {"matchLabels": {"app.kubernetes.io/name": "skypilot-api"}},
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/name": "skypilot-api"}},
                "spec": pod_spec,
            },
        },
    }


def _network_policy(namespace: str = SKYPILOT_NAMESPACE) -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "default-deny", "namespace": namespace},
        "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
    }


# ---------------------------------------------------------------------------
# The review's reproduced case, and proof it used to pass.
# ---------------------------------------------------------------------------

CLUSTER_ADMIN_BINDING = {
    "apiVersion": "rbac.authorization.k8s.io/v1",
    "kind": "ClusterRoleBinding",
    "metadata": {"name": "superplane-admin"},
    "roleRef": {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": "cluster-admin",
    },
    "subjects": [
        {"kind": "ServiceAccount", "name": "superplane-api", "namespace": "adp"}
    ],
}


def test_reproduced_case_core_namespace_plus_cluster_admin_binding(tmp_path):
    """The review's exact fixture: digest-pinned, namespace `adp`, bound to cluster-admin."""
    directory = _write(
        tmp_path / "rendered",
        _deployment(namespace="adp"),
        CLUSTER_ADMIN_BINDING,
    )
    result = _run_guard(directory)
    assert result.returncode != 0, (
        "the reproduced case still passes: a digest-pinned Deployment in a core namespace "
        f"with a cluster-admin binding was accepted\n{result.stdout}"
    )
    assert "adp" in result.stdout, "the core namespace was not named in the refusal"
    assert "ClusterRoleBinding" in result.stdout, (
        "the cluster-admin binding was not flagged"
    )


def test_the_old_digest_only_check_would_have_passed_it(tmp_path):
    """Run the SUPERSEDED logic against the same fixture.

    Without this, the test above proves only that a hostile manifest fails — not that the
    guard changed. The body below is the pre-fix check from superplane-k8s-deploy.yml,
    reproduced verbatim in substance: walk every `image` key, require a 64-hex digest.
    """
    directory = _write(
        tmp_path / "rendered", _deployment(namespace="adp"), CLUSTER_ADMIN_BINDING
    )

    digest_re = re.compile(r"@sha256:[0-9a-f]{64}$")
    unpinned, checked = [], 0
    for path in sorted(directory.iterdir()):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            stack = [doc]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    for key, value in node.items():
                        if key == "image" and isinstance(value, str):
                            checked += 1
                            if not digest_re.search(value):
                                unpinned.append(value)
                        else:
                            stack.append(value)
                elif isinstance(node, list):
                    stack.extend(node)

    assert checked == 1
    assert unpinned == [], (
        "the old check would have rejected this fixture, so it does not demonstrate the gap"
    )


# ---------------------------------------------------------------------------
# Namespace ownership.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "namespace",
    ["adp", "adp-gateway", "kube-system", "default", "arc-runners", "bedrockgw"],
)
def test_core_namespace_is_rejected(tmp_path, namespace):
    directory = _write(
        tmp_path / namespace,
        _deployment(namespace=namespace),
        _network_policy(namespace),
    )
    result = _run_guard(directory)
    assert result.returncode != 0, (
        f"a workload in the core namespace {namespace!r} was accepted"
    )


def test_unlisted_namespace_is_rejected(tmp_path):
    """Not a core namespace, just not one this rollout was told the domain owns."""
    directory = _write(
        tmp_path / "other",
        _deployment(namespace="someone-elses"),
        _network_policy("someone-elses"),
    )
    result = _run_guard(directory)
    assert result.returncode != 0


def test_missing_namespace_is_rejected(tmp_path):
    """An object with no namespace lands wherever the kubeconfig context points."""
    doc = _deployment()
    del doc["metadata"]["namespace"]
    directory = _write(tmp_path / "nons", doc, _network_policy())
    result = _run_guard(directory)
    assert result.returncode != 0
    assert "metadata.namespace" in result.stdout


def test_a_core_namespace_supplied_as_permitted_refuses_to_validate(tmp_path):
    """The allowlist arrives as data, so the data itself is checked.

    If an SSM parameter were edited to `adp`, an allowlist-only check would accept every
    object in `adp`. This is the independent tripwire for that.
    """
    directory = _write(
        tmp_path / "rendered", _deployment(namespace="adp"), _network_policy("adp")
    )
    result = _run_guard(directory, namespaces=["adp", SKYPILOT_NAMESPACE])
    assert result.returncode != 0, (
        "a core namespace passed as a permitted namespace was honoured"
    )
    assert "core ADP" in result.stdout


def test_no_permitted_namespaces_refuses_rather_than_passing_vacuously(tmp_path):
    """ "Could not validate" must be distinguishable from "validated, found problems".

    With no permitted namespaces, every object also fails the ordinary "not one of the
    domain's namespaces" check — so a test asserting only on the exit code passes even if the
    refusal is removed (confirmed by mutation). That is the wrong outcome for the wrong
    reason: the run reports per-object findings as though the check had been performed, when
    in fact the check had nothing to compare against. Asserting the reason pins the
    distinction the guard's ManifestViolation path exists to make.
    """
    directory = _write(tmp_path / "rendered", _deployment(), _network_policy())
    result = _run_guard(directory, namespaces=[])
    assert result.returncode != 0, (
        "with no permitted namespaces every check passed vacuously"
    )
    assert "vacuously" in result.stdout, (
        "the run denied, but reported per-object findings rather than refusing to validate at "
        f"all — so an unvalidatable input is indistinguishable from a validated bad one:\n"
        f"{result.stdout}"
    )


# ---------------------------------------------------------------------------
# Cluster scope and RBAC.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        "ClusterRole",
        "ClusterRoleBinding",
        "CustomResourceDefinition",
        "MutatingWebhookConfiguration",
        "ValidatingWebhookConfiguration",
        "PersistentVolume",
        "StorageClass",
        "PriorityClass",
    ],
)
def test_cluster_scoped_kinds_are_rejected(tmp_path, kind):
    """And rejected FOR BEING CLUSTER-SCOPED, not incidentally.

    The reason is asserted because two mechanisms can reject these kinds: the explicit
    cluster-scope list, and the kind allowlist that everything unrecognised falls through to.
    A test satisfied by either would not notice if someone added `ClusterRoleBinding` to
    NAMESPACED_KINDS — the allowlist would then admit it and the suite would stay green.
    (Confirmed: emptying FORBIDDEN_CLUSTER_SCOPED_KINDS left an earlier version of this test
    passing.)
    """
    directory = _write(
        tmp_path / kind,
        {"apiVersion": "v1", "kind": kind, "metadata": {"name": "superplane-thing"}},
    )
    result = _run_guard(directory)
    assert result.returncode != 0, f"a domain app was permitted to create a {kind}"
    assert "cluster-scoped" in result.stdout, (
        f"{kind} was rejected, but not for being cluster-scoped:\n{result.stdout}"
    )


def test_namespace_object_for_a_foreign_namespace_is_rejected(tmp_path):
    """`Namespace` is the one permitted cluster-scoped kind — for OUR namespaces only."""
    directory = _write(
        tmp_path / "rendered",
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "kube-system"}},
    )
    result = _run_guard(directory)
    assert result.returncode != 0


@pytest.mark.parametrize("field", ["verbs", "resources"])
def test_wildcard_role_is_rejected(tmp_path, field):
    rule = {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get"]}
    rule[field] = ["*"]
    directory = _write(
        tmp_path / field,
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": "skypilot-api", "namespace": SKYPILOT_NAMESPACE},
            "rules": [rule],
        },
    )
    result = _run_guard(directory)
    assert result.returncode != 0, f"a Role with wildcard {field} was accepted"


@pytest.mark.parametrize("role", ["cluster-admin", "admin", "edit"])
def test_rolebinding_to_a_cluster_wide_role_is_rejected(tmp_path, role):
    """A namespaced RoleBinding can still reference a ClusterRole, granting far too much."""
    directory = _write(
        tmp_path / role,
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": "skypilot-api", "namespace": SKYPILOT_NAMESPACE},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": role,
            },
            "subjects": [
                {
                    "kind": "ServiceAccount",
                    "name": "skypilot-api",
                    "namespace": SKYPILOT_NAMESPACE,
                }
            ],
        },
    )
    result = _run_guard(directory)
    assert result.returncode != 0, (
        f"a RoleBinding to the ClusterRole {role!r} was accepted"
    )


# ---------------------------------------------------------------------------
# Identity.
# ---------------------------------------------------------------------------


def test_default_service_account_is_rejected(tmp_path):
    directory = _write(
        tmp_path / "rendered", _deployment(service_account="default"), _network_policy()
    )
    result = _run_guard(directory)
    assert result.returncode != 0


def test_absent_service_account_is_rejected(tmp_path):
    doc = _deployment()
    del doc["spec"]["template"]["spec"]["serviceAccountName"]
    directory = _write(tmp_path / "rendered", doc, _network_policy())
    result = _run_guard(directory)
    assert result.returncode != 0


def test_cross_account_irsa_annotation_is_rejected(tmp_path):
    """A role ARN in another account is an identity this domain has no business assuming."""
    directory = _write(
        tmp_path / "rendered",
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {
                "name": "skypilot-api",
                "namespace": SKYPILOT_NAMESPACE,
                "annotations": {
                    "eks.amazonaws.com/role-arn": (
                        "arn:aws:iam::605440105851:role/adp-dev-superplane-skypilot-api"
                    )
                },
            },
        },
    )
    result = _run_guard(directory)
    assert result.returncode != 0, (
        "an IRSA annotation naming another account was accepted"
    )
    assert "605440105851" in result.stdout


def test_foreign_role_irsa_annotation_is_rejected(tmp_path):
    """Right account, wrong role: the gateway's identity is not the domain's to assume."""
    directory = _write(
        tmp_path / "rendered",
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {
                "name": "skypilot-api",
                "namespace": SKYPILOT_NAMESPACE,
                "annotations": {
                    "eks.amazonaws.com/role-arn": f"arn:aws:iam::{ACCOUNT}:role/bedrockgw-dev-service"
                },
            },
        },
    )
    result = _run_guard(directory)
    assert result.returncode != 0
    assert "bedrockgw-dev-service" in result.stdout


def test_cross_environment_irsa_annotation_is_rejected(tmp_path):
    """A dev rollout must not bind pods to the prod role."""
    directory = _write(
        tmp_path / "rendered",
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {
                "name": "skypilot-api",
                "namespace": SKYPILOT_NAMESPACE,
                "annotations": {
                    "eks.amazonaws.com/role-arn": (
                        f"arn:aws:iam::{ACCOUNT}:role/adp-prod-superplane-skypilot-api"
                    )
                },
            },
        },
    )
    result = _run_guard(directory, environment="dev")
    assert result.returncode != 0


def test_correct_irsa_annotation_is_accepted(tmp_path):
    """The other direction, so the check above is not passing by rejecting everything."""
    directory = _write(
        tmp_path / "rendered",
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {
                "name": "skypilot-api",
                "namespace": SKYPILOT_NAMESPACE,
                "annotations": {
                    "eks.amazonaws.com/role-arn": (
                        f"arn:aws:iam::{ACCOUNT}:role/adp-dev-superplane-skypilot-api"
                    )
                },
            },
        },
    )
    result = _run_guard(directory)
    assert result.returncode == 0, (
        f"a correct IRSA annotation was rejected:\n{result.stdout}"
    )


@pytest.mark.parametrize("field", ["hostNetwork", "hostPID", "hostIPC"])
def test_host_namespace_escape_is_rejected(tmp_path, field):
    directory = _write(
        tmp_path / field, _deployment(pod_extra={field: True}), _network_policy()
    )
    result = _run_guard(directory)
    assert result.returncode != 0


def test_hostpath_volume_is_rejected(tmp_path):
    directory = _write(
        tmp_path / "rendered",
        _deployment(
            pod_extra={"volumes": [{"name": "host", "hostPath": {"path": "/"}}]}
        ),
        _network_policy(),
    )
    result = _run_guard(directory)
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# Digest provenance — the check the old guard could not make.
# ---------------------------------------------------------------------------


def test_wellformed_digest_the_lock_does_not_pin_is_rejected(tmp_path):
    """64 hex characters is a shape, not a provenance."""
    directory = _write(
        tmp_path / "rendered", _deployment(image=UNPINNED_IMAGE), _network_policy()
    )
    result = _run_guard(directory)
    assert result.returncode != 0, (
        "an image with a perfectly-formed digest the lock does not pin was accepted — this is "
        "exactly what the digest-shape check could not distinguish"
    )
    assert "not provenance" in result.stdout


def test_floating_tag_is_rejected(tmp_path):
    directory = _write(
        tmp_path / "rendered",
        _deployment(image="berkeleyskypilot/skypilot:latest"),
        _network_policy(),
    )
    result = _run_guard(directory)
    assert result.returncode != 0


def test_init_container_image_is_checked_too(tmp_path):
    """An unpinned init container is as much a deploy as an unpinned main container."""
    directory = _write(
        tmp_path / "rendered",
        _deployment(
            pod_extra={
                "initContainers": [
                    {
                        "name": "fetch",
                        "image": "amazon/aws-cli:latest",
                        "resources": {
                            "requests": {"cpu": "10m", "memory": "32Mi"},
                            "limits": {"cpu": "50m", "memory": "64Mi"},
                        },
                    }
                ]
            }
        ),
        _network_policy(),
    )
    result = _run_guard(directory)
    assert result.returncode != 0
    assert "aws-cli" in result.stdout


def test_a_lock_with_no_resolved_digests_refuses_to_validate(tmp_path):
    """Otherwise the digest-binding check passes vacuously for every image."""
    empty_lock = tmp_path / "empty-lock.yaml"
    empty_lock.write_text(
        yaml.safe_dump({"images": {}, "pending_images": {}}), encoding="utf-8"
    )
    directory = _write(tmp_path / "rendered", _deployment(), _network_policy())
    result = _run_guard(directory, lock_file=empty_lock)
    assert result.returncode != 0
    assert "vacuous" in result.stdout or "could not distinguish" in result.stdout


# ---------------------------------------------------------------------------
# Resource bounds and GPU placement.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "resources",
    [
        {},
        {"requests": {"cpu": "100m", "memory": "128Mi"}},
        {"limits": {"cpu": "1", "memory": "1Gi"}},
        {"requests": {"cpu": "100m"}, "limits": {"cpu": "1", "memory": "1Gi"}},
        {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "1Gi"}},
    ],
)
def test_incomplete_resource_bounds_are_rejected(tmp_path, resources):
    """Requests AND limits, CPU AND memory. A partial declaration bounds nothing."""
    directory = _write(
        tmp_path / "rendered", _deployment(resources=resources), _network_policy()
    )
    result = _run_guard(directory)
    assert result.returncode != 0, f"resources={resources} was accepted as bounded"


def test_gpu_without_placement_is_rejected(tmp_path):
    """A GPU container with no selector schedules wherever the context points.

    For this runner that is the ADP management cluster (platform isolation requirement,
    2026-09-16).
    """
    directory = _write(
        tmp_path / "rendered",
        _deployment(
            resources={
                "requests": {"cpu": "1", "memory": "4Gi"},
                "limits": {"cpu": "2", "memory": "8Gi", "nvidia.com/gpu": 1},
            }
        ),
        _network_policy(),
    )
    result = _run_guard(directory)
    assert result.returncode != 0
    assert "management cluster" in result.stdout


def test_gpu_with_node_selector_is_accepted(tmp_path):
    """The other direction: placement is the requirement, not a GPU ban."""
    directory = _write(
        tmp_path / "rendered",
        _deployment(
            resources={
                "requests": {"cpu": "1", "memory": "4Gi"},
                "limits": {"cpu": "2", "memory": "8Gi", "nvidia.com/gpu": 1},
            },
            pod_extra={"nodeSelector": {"adp.aws-e.io/workspace": "gpu"}},
        ),
        _network_policy(),
    )
    result = _run_guard(directory)
    assert result.returncode == 0, (
        f"a correctly-placed GPU pod was rejected:\n{result.stdout}"
    )


# ---------------------------------------------------------------------------
# Failure paths: every one denies.
# ---------------------------------------------------------------------------


def test_empty_directory_is_rejected(tmp_path):
    """'applied nothing' must not share a green tick with 'applied everything'."""
    directory = tmp_path / "empty"
    directory.mkdir()
    result = _run_guard(directory)
    assert result.returncode != 0


def test_missing_directory_is_rejected(tmp_path):
    result = _run_guard(tmp_path / "absent")
    assert result.returncode != 0


def test_malformed_yaml_is_rejected(tmp_path):
    directory = tmp_path / "rendered"
    directory.mkdir()
    (directory / "broken.yaml").write_text(
        "kind: Deployment\n  bad: [indent", encoding="utf-8"
    )
    result = _run_guard(directory)
    assert result.returncode != 0


def test_surviving_placeholder_is_rejected(tmp_path):
    """kubectl accepts a namespace of "REPLACE_WITH_NAMESPACE" without complaint."""
    directory = tmp_path / "rendered"
    directory.mkdir()
    (directory / "ns.yaml").write_text(
        "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: REPLACE_WITH_NAMESPACE\n",
        encoding="utf-8",
    )
    result = _run_guard(directory)
    assert result.returncode != 0
    assert "REPLACE_WITH_NAMESPACE" in result.stdout


def test_non_object_document_is_rejected(tmp_path):
    directory = tmp_path / "rendered"
    directory.mkdir()
    (directory / "list.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    result = _run_guard(directory)
    assert result.returncode != 0


def test_workload_without_a_networkpolicy_is_reported(tmp_path):
    """Presence is checked. The refusal text must not claim enforcement."""
    directory = _write(tmp_path / "rendered", _deployment())
    result = _run_guard(directory)
    assert result.returncode != 0
    assert "NetworkPolicy" in result.stdout
    assert "4999" in result.stdout, (
        "the NetworkPolicy finding does not name the platform-owned prerequisite, so a reader "
        "could take it as an enforcement claim"
    )


# ---------------------------------------------------------------------------
# The guard must see the whole rendered set, not just its top level — A18 (#5674).
#
# The guard walked the rendered directory with iterdir(), which yields only the
# top level. The apply step hands kubectl the directory, and kubectl reads the
# files inside a directory it is given. So a manifest one level down was applied
# but never validated, and the guard printed "RBAC scope ... conform" over it and
# exited 0. Same class of false green as the digest-only check this suite already
# covers: a report on the subset it happened to look at, phrased as the whole set.
#
# test_the_flat_scan_would_have_passed_the_nested_fixture runs the superseded
# iterdir() logic against the same fixture, so these tests establish that the
# guard CHANGED rather than that a cluster-admin binding is obviously invalid —
# the same discipline as test_the_old_digest_only_check_would_have_passed_it.
# ---------------------------------------------------------------------------


def _nested_hostile_set(root: Path) -> Path:
    """A compliant top level plus a deliberately non-compliant manifest one level down."""
    directory = _write(root / "rendered", _deployment(), _network_policy())
    nested = directory / "extra"
    nested.mkdir()
    (nested / "hostile.yaml").write_text(
        yaml.safe_dump(CLUSTER_ADMIN_BINDING, sort_keys=False), encoding="utf-8"
    )
    return directory


def test_a_nested_manifest_is_validated(tmp_path):
    """The fixture the flat scan missed: valid top level, cluster-admin binding below it."""
    directory = _nested_hostile_set(tmp_path)
    result = _run_guard(directory)
    assert result.returncode != 0, (
        "a ClusterRoleBinding to cluster-admin in a subdirectory of the rendered set was "
        f"accepted, so the guard is still only checking the top level\n{result.stdout}"
    )
    assert "ClusterRoleBinding" in result.stdout
    assert "extra/hostile.yaml" in result.stdout, (
        "the violation does not identify which nested file it came from, so two same-named "
        f"files in different subdirectories would be indistinguishable\n{result.stdout}"
    )


def test_the_flat_scan_would_have_passed_the_nested_fixture(tmp_path):
    """Run the SUPERSEDED discovery against the same fixture.

    Without this, the test above proves only that a cluster-admin binding fails — not that
    the recursion is what changed. The body is the pre-fix discovery from
    `_iter_documents`, reproduced in substance: iterdir(), filtered to YAML suffixes.
    """
    directory = _nested_hostile_set(tmp_path)

    seen = sorted(p.name for p in directory.iterdir() if p.suffix in {".yaml", ".yml"})
    assert seen == ["manifest.yaml"], (
        "this test no longer reproduces the old behaviour; update it deliberately"
    )
    assert "hostile.yaml" not in seen, (
        "the flat scan never saw the nested manifest — that was the gap"
    )

    # And it is genuinely reachable: kubectl applies the files inside a directory it is
    # handed, so 'not scanned' meant 'applied unchecked' rather than 'ignored'.
    recursive = sorted(
        p.relative_to(directory).as_posix()
        for p in directory.rglob("*")
        if p.is_file() and p.suffix in {".yaml", ".yml"}
    )
    assert recursive == ["extra/hostile.yaml", "manifest.yaml"]


def test_a_compliant_nested_manifest_is_accepted(tmp_path):
    """Recursion must not turn subdirectories themselves into the violation."""
    directory = _write(tmp_path / "rendered", _deployment())
    nested = directory / "policies"
    nested.mkdir()
    (nested / "netpol.yaml").write_text(
        yaml.safe_dump(_network_policy(), sort_keys=False), encoding="utf-8"
    )
    result = _run_guard(directory)
    assert result.returncode == 0, (
        "a compliant manifest in a subdirectory was rejected, so the recursion is too strict "
        f"rather than more complete\n{result.stdout}"
    )


# ---------------------------------------------------------------------------
# The real manifests, rendered by the real renderer.
# ---------------------------------------------------------------------------


def _render(tmp_path: Path, **env_overrides) -> subprocess.CompletedProcess:
    output = tmp_path / "rendered"
    env = {**os.environ, **RENDER_ENV, **env_overrides}
    return subprocess.run(
        [
            sys.executable,
            str(RENDERER),
            "--source-dir",
            str(MANIFEST_DIR),
            "--output-dir",
            str(output),
            "--lock-file",
            str(LOCK_FILE),
        ],
        capture_output=True,
        text=True,
        env=env,
    )


def test_the_shipped_manifests_render_and_pass(tmp_path):
    """End to end on the real files, through the real renderer, past the real guard.

    The most important test here. Every hostile fixture above proves the guard rejects
    something; this proves the guard is satisfiable by the manifests U3 actually ships — a
    check nothing can pass is not a check.
    """
    render = _render(tmp_path)
    assert render.returncode == 0, (
        f"the shipped manifests failed to render:\n{render.stdout}\n{render.stderr}"
    )

    result = _run_guard(tmp_path / "rendered")
    assert result.returncode == 0, (
        f"the shipped manifests failed validation:\n{result.stdout}"
    )


def test_the_shipped_manifests_declare_the_lock_pinned_skypilot_image(tmp_path):
    """The deployed image is the lock's, not merely digest-shaped."""
    assert _render(tmp_path).returncode == 0
    images = []
    for path in sorted((tmp_path / "rendered").iterdir()):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if isinstance(doc, dict) and doc.get("kind") == "Deployment":
                for container in doc["spec"]["template"]["spec"]["containers"]:
                    images.append(container["image"])
    assert images, "no Deployment container images found in the rendered set"
    for image in images:
        assert image.endswith(PINNED_DIGEST), f"{image} is not the lock-pinned digest"


def test_the_shipped_manifests_reference_no_pending_image(tmp_path):
    """The three unbuildable images must not appear. A placeholder digest would be believed."""
    lock = yaml.safe_load(LOCK_FILE.read_text(encoding="utf-8"))
    pending = list(lock.get("pending_images") or {})
    assert pending, "the lock records no pending images, so this test proves nothing"

    assert _render(tmp_path).returncode == 0
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "rendered").iterdir()
    )
    for name in pending:
        repository = (lock["pending_images"][name] or {}).get("ecr_repository")
        for token in filter(None, (name, repository)):
            for line in combined.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                assert token not in stripped, (
                    f"a rendered manifest references the pending image token {token!r}: {line!r}"
                )


def test_skypilot_allowed_clouds_match_the_lock(tmp_path):
    """The ConfigMap copies U2's pinned list; the copy must not drift.

    SkyPilot reads a config file, not a lock file, so a copy is unavoidable — which makes
    this assertion the thing that keeps it honest.
    """
    assert _render(tmp_path).returncode == 0
    lock = yaml.safe_load(LOCK_FILE.read_text(encoding="utf-8"))
    expected = lock["skypilot_config"]["allowed_clouds"]

    found = None
    for path in sorted((tmp_path / "rendered").iterdir()):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if isinstance(doc, dict) and doc.get("kind") == "ConfigMap":
                config = yaml.safe_load(doc["data"]["config.yaml"])
                found = config.get("allowed_clouds")
    assert found is not None, "no SkyPilot config.yaml found in the rendered ConfigMap"
    assert found == expected, (
        f"the ConfigMap's allowed_clouds {found} has drifted from the lock's {expected}"
    )


def test_skypilot_service_matches_the_address_the_controller_uses(tmp_path):
    """The upstream controller's client defaults to a specific host and port.

    Renaming the Service or moving the port breaks the controller at first launch — long
    after the rollout reported success.
    """
    assert _render(tmp_path).returncode == 0
    services = []
    for path in sorted((tmp_path / "rendered").iterdir()):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if isinstance(doc, dict) and doc.get("kind") == "Service":
                services.append(doc)
    assert len(services) == 1, f"expected exactly one Service, found {len(services)}"
    service = services[0]
    assert service["metadata"]["name"] == "skypilot-api"
    assert service["metadata"]["namespace"] == SKYPILOT_NAMESPACE
    assert [port["port"] for port in service["spec"]["ports"]] == [46580]
    assert service["spec"]["type"] == "ClusterIP", (
        "a LoadBalancer would put an API that can launch GPU compute on a public address"
    )


def test_skypilot_role_grants_no_pod_creation(tmp_path):
    """The Role must not let SkyPilot schedule onto the ADP management cluster.

    This is the mechanism behind the isolation claim: `workspace_cluster_context` being
    empty is a configuration, but the absence of pod-create rights is enforcement.
    """
    assert _render(tmp_path).returncode == 0
    roles = []
    for path in sorted((tmp_path / "rendered").iterdir()):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if isinstance(doc, dict) and doc.get("kind") == "Role":
                roles.append(doc)
    assert roles, "no Role found in the rendered set"
    for role in roles:
        for rule in role["rules"]:
            verbs = set(rule.get("verbs") or [])
            resources = set(rule.get("resources") or [])
            if resources & {"pods", "pods/exec"}:
                mutating = verbs & {
                    "create",
                    "delete",
                    "update",
                    "patch",
                    "deletecollection",
                }
                assert not mutating, (
                    f"Role {role['metadata']['name']} grants {sorted(mutating)} on pods, which "
                    f"is what SkyPilot needs to provision on THIS (management) cluster"
                )


def test_rendering_refuses_a_value_that_would_restructure_the_document(tmp_path):
    result = _render(tmp_path, SP_NAMESPACE="superplane\nevil: true")
    assert result.returncode != 0
    assert "SP_NAMESPACE" in result.stdout


def test_rendering_refuses_an_empty_value(tmp_path):
    result = _render(tmp_path, SP_SKYPILOT_IMAGE="")
    assert result.returncode != 0


def test_rendering_accepts_arns_and_digests(tmp_path):
    """A regression pin: an early draft blacklisted `:` and refused every real ARN."""
    result = _render(tmp_path)
    assert result.returncode == 0, (
        f"the renderer rejected legitimate values containing ':', '/' or '@':\n{result.stdout}"
    )


# ---------------------------------------------------------------------------
# The instrumented entry-point property: rejected manifests never reach the cluster.
# ---------------------------------------------------------------------------


def _workflow_steps() -> list[dict]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = next(iter(doc["jobs"].values()))
    return job["steps"]


def test_validation_precedes_every_cluster_mutation_in_step_order():
    """Ordering is the property. A guard that runs after `kubectl apply` is decoration."""
    names = [(step.get("name") or step.get("uses") or "") for step in _workflow_steps()]
    guard_index = next(
        i for i, n in enumerate(names) if "validate the rendered object set" in n
    )
    apply_index = next(i for i, n in enumerate(names) if n.strip() == "Apply")
    dry_run_index = next(i for i, n in enumerate(names) if "dry run" in n.lower())
    assert guard_index < apply_index, (
        f"the rendered-manifest guard (step {guard_index}) does not run before Apply "
        f"(step {apply_index})"
    )
    assert guard_index < dry_run_index, (
        "the guard runs after the server-side dry run, which already contacts the cluster"
    )


def test_the_render_step_passes_values_as_env_not_shell_interpolation():
    """`${{ }}` inside a `run:` body is interpolation before the shell parses.

    The pre-fix step built a `sed` script that way, so an SSM value containing `|` or `$(…)`
    was source code rather than data.
    """
    for step in _workflow_steps():
        name = step.get("name") or ""
        if "Render manifests" not in name:
            continue
        body = step.get("run") or ""
        assert "sed" not in body, "the render step still builds a sed script"
        assert "${{" not in body, (
            "the render step still interpolates a GitHub expression into its shell body"
        )
        assert step.get("env"), "the render step passes no values as env"
        return
    raise AssertionError("no 'Render manifests' step found")


def test_the_guard_step_passes_values_as_env_not_shell_interpolation():
    for step in _workflow_steps():
        name = step.get("name") or ""
        if "validate the rendered object set" not in name:
            continue
        body = step.get("run") or ""
        assert "${{" not in body, (
            "the guard step interpolates GitHub expressions directly into its argument list"
        )
        assert step.get("env"), "the guard step passes no values as env"
        return
    raise AssertionError("no rendered-manifest guard step found")


def test_rejected_manifests_never_reach_kubectl(tmp_path):
    """Run the guard with a stub `kubectl` on PATH; assert it was never invoked.

    The instrumented entry-point test. A unit assertion on the exit code cannot establish
    that nothing touched the cluster — only observing the tool can.
    """
    log = tmp_path / "kubectl-invocations.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "kubectl"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"pathlib.Path({str(log)!r}).open('a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    directory = _write(
        tmp_path / "rendered", _deployment(namespace="adp"), CLUSTER_ADMIN_BINDING
    )

    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run(
        [
            sys.executable,
            str(GUARD),
            "--rendered-dir",
            str(directory),
            "--lock-file",
            str(LOCK_FILE),
            "--namespace",
            NAMESPACE,
            "--namespace",
            SKYPILOT_NAMESPACE,
            "--account-id",
            ACCOUNT,
            "--environment",
            ENVIRONMENT,
        ],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0, "the hostile rendered set passed validation"
    assert not log.exists(), (
        f"kubectl was invoked despite a rejected manifest set:\n{log.read_text()}"
    )
