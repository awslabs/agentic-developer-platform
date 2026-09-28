"""The production adapters, driven offline through a scripted command runner — F1.

F1 verbatim: "`ClusterAccess` and `RegistrationStore` were Protocols with no
implementation anywhere, and `bootstrap_workspace` had no caller outside its own tests."
`adapters.py` is the implementation half of that repair, and this file is what makes it
more than an assertion: every method is exercised, including its failure paths, with no
cluster, no credential and no network.

WHAT THESE TESTS ARE FOR, GIVEN THE GATES ARE TESTED ELSEWHERE

The gate modules are tested against `FakeClusterAccess`. That proves the DECISIONS are
right, and proves nothing about whether the real adapter answers the same questions the
fake does. Two defects found while writing this file were exactly that gap, and both were
invisible to 316 passing tests:

1. `KubectlClusterAccess` did not implement `establish_controller_rbac` or
   `install_controller` at all — `isinstance(adapter, ClusterAccess)` was False, and
   `_install_controller` would have died with `AttributeError` at the first real
   bootstrap, after creating the namespace and the CRDs.
2. `controller_deployments` returned the image of EVERY Deployment on the cluster, while
   `readiness._controller_checks` requires exactly one back. On any real EKS cluster
   CoreDNS and the CSI controllers are Deployments too, so readiness would have failed
   forever and the taint could never have come off.

So the tests below are written against the shapes real `kubectl` and `aws` actually
return, and they assert the adapter's ANSWER, not its argv, wherever the answer is what a
gate consumes. `_Scripted` raises on an unexpected command rather than returning a
default, because a default would let a method that stopped calling kubectl keep passing.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping, Sequence

import pytest
from superplane_bootstrap.access import ClusterAccess
from superplane_bootstrap.adapters import (
    _BOOTSTRAP_TAINT_KEY,
    AwsObserver,
    AwsPrerequisiteAccess,
    CommandResult,
    IamNodeRoleFacts,
    KubectlClusterAccess,
    NodeRoleFacts,
    SubprocessRunner,
)
from superplane_bootstrap.components import CONTROLLER_IMAGE_MARKER
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.prerequisites import PrerequisiteAccess
from superplane_bootstrap.readiness import (
    FORBIDDEN_CONTROLLER_PERMISSIONS,
    REQUIRED_CONTROLLER_PERMISSIONS,
)

from .conftest import (
    ACCOUNT_ID,
    CA_DATA,
    CLUSTER_ARN,
    CLUSTER_NAME,
    CLUSTER_SG_ID,
    CONTROLLER_NAME,
    CONTROLLER_SERVICE_ACCOUNT,
    ENDPOINT,
    NAMESPACE,
    OIDC_ISSUER,
    REGION,
    VPC_ID,
)

CONTROLLER_IMAGE = f"registry.example/{CONTROLLER_IMAGE_MARKER}:v1"
CONTROLLER_NAMESPACE = "superplane-system"
NODE_ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/superplane-workspace-node"
NAMESPACE_UID = "namespace-uid-0001"


class _Scripted:
    """A `CommandRunner` that answers only commands a test declared.

    Keyed on a substring of the joined argv rather than the exact list: the assertions
    that matter are about the adapter's ANSWER, and pinning every flag would make these
    tests fail on a harmless flag reorder while still not proving the answer is right.
    Where argv itself is the thing under test (`--dry-run=server`, `--as`, stdin), the
    test asserts on `self.calls` explicitly.

    An undeclared command RAISES. A runner returning a benign default would let an
    adapter that silently stopped invoking kubectl keep passing every test here.
    """

    def __init__(self, replies: Mapping[str, object], fail: Sequence[str] = ()) -> None:
        self._replies = dict(replies)
        self._fail = tuple(fail)
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def run(
        self, args: Sequence[str], *, data: str | None = None, timeout: int = 120
    ) -> CommandResult:
        argv = tuple(str(arg) for arg in args)
        self.calls.append((argv, data))
        joined = " ".join(argv)
        for marker in self._fail:
            if marker in joined:
                return CommandResult(
                    args=argv, returncode=1, stderr=f"synthetic failure for {marker}"
                )
        for marker, reply in self._replies.items():
            if marker in joined:
                if isinstance(reply, str):
                    return CommandResult(args=argv, returncode=0, stdout=reply)
                return CommandResult(args=argv, returncode=0, stdout=json.dumps(reply))
        raise AssertionError(f"the adapter ran an undeclared command: {joined}")

    def argv_containing(self, marker: str) -> list[tuple[str, ...]]:
        return [argv for argv, _ in self.calls if marker in " ".join(argv)]

    def stdin_for(self, marker: str) -> list[str]:
        return [
            data
            for argv, data in self.calls
            if marker in " ".join(argv) and data is not None
        ]


def _deployment(namespace: str, name: str, image: str) -> dict[str, object]:
    """A Deployment shaped as `kubectl get deployments -o json` returns it."""
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "replicas": 1,
            "template": {"spec": {"containers": [{"name": name, "image": image}]}},
        },
        "status": {"availableReplicas": 1},
    }


def _access(runner: _Scripted, **overrides) -> KubectlClusterAccess:
    access = KubectlClusterAccess(
        **{
            "runner": runner,
            "tenant_identity_reader": lambda: (),
            "imds_probe_image": "registry.example/python@sha256:" + "0" * 64,
            "kubeconfig": "/tmp/kubeconfig-does-not-need-to-exist",
            "controller_namespace": CONTROLLER_NAMESPACE,
            "controller_service_account": CONTROLLER_SERVICE_ACCOUNT,
            "controller_image": CONTROLLER_IMAGE,
            "node_role": IamNodeRoleFacts(runner=runner, node_role_arn=NODE_ROLE_ARN),
            **overrides,
        }
    )
    # Keep the historic Deployment adapter regressions executable for recovery;
    # production defaults to management mode and refuses this legacy install.
    access.controller_mode = "legacy"
    return access


def test_management_mode_never_installs_a_second_workspace_controller():
    runner = _Scripted({})
    access = _access(runner)
    access.controller_mode = "management"
    with pytest.raises(BootstrapRefused, match="canonical management controller"):
        access.install_controller("workspace", "superplane-controller", "observer")
    assert not runner.calls


# --- The defect F1 named: the adapter must actually BE a ClusterAccess ----------


def test_the_kubectl_adapter_satisfies_the_cluster_access_protocol():
    """**Defect 1, caught by this test.**

    `KubectlClusterAccess` was missing `establish_controller_rbac` and
    `install_controller` entirely, so this assertion failed. Nothing else caught it: the
    gates are tested against the fake, and `@runtime_checkable` is only consulted if
    somebody calls `isinstance` — which no production path did. The first real bootstrap
    would have raised `AttributeError` inside `_install_controller`, AFTER creating the
    namespace and the CRDs.

    Asserted by NAME rather than by `isinstance` alone, because `@runtime_checkable`
    checks only that the attributes exist and the failure message "is not a ClusterAccess"
    would not say which method is missing.
    """
    required = {
        name
        for name, _ in inspect.getmembers(ClusterAccess, inspect.isfunction)
        if not name.startswith("_")
    }
    implemented = {
        name
        for name, _ in inspect.getmembers(KubectlClusterAccess, inspect.isfunction)
        if not name.startswith("_")
    }

    assert required - implemented == set(), (
        "KubectlClusterAccess does not implement every ClusterAccess seam, so a real "
        "bootstrap dies with AttributeError partway through mutating the cluster"
    )
    assert isinstance(_access(_Scripted({})), ClusterAccess)


def test_the_aws_prerequisite_adapter_satisfies_its_protocol():
    """The same check for the F4 seam, for the same reason."""
    required = {
        name
        for name, _ in inspect.getmembers(PrerequisiteAccess, inspect.isfunction)
        if not name.startswith("_")
    }
    implemented = {
        name
        for name, _ in inspect.getmembers(AwsPrerequisiteAccess, inspect.isfunction)
        if not name.startswith("_")
    }

    assert required - implemented == set()


def test_the_adapter_taint_key_matches_the_sequence_that_removes_it():
    """The key is restated in `adapters.py` to avoid an import cycle, so it is pinned.

    A drift here would not fail loudly: `place_system_workloads` would add a toleration
    for a taint no node carries, CoreDNS would stay pending, and the failure would present
    as an unexplained readiness timeout rather than as the typo it is.
    """
    from superplane_bootstrap.workspace import BOOTSTRAP_TAINT_KEY

    assert _BOOTSTRAP_TAINT_KEY == BOOTSTRAP_TAINT_KEY


# --- controller_deployments: the second defect ---------------------------------


def test_only_workspace_controllers_are_counted_as_reconcilers():
    """**Defect 2, caught by this test.**

    `readiness._controller_checks` requires EXACTLY ONE image back from this method, and
    `components._refuse_existing_controller` applies the marker filter to the same
    method's result. The adapter returned every Deployment's image, so on this cluster —
    an ordinary EKS cluster with CoreDNS and the EBS CSI driver — readiness saw three
    reconcilers, failed `single_controller_reconciles_the_cluster` forever, and the
    bootstrap taint could never be removed.
    """
    runner = _Scripted(
        {
            "get deployments --all-namespaces": {
                "items": [
                    _deployment(
                        "kube-system",
                        "coredns",
                        "602401143452.dkr.ecr/eks/coredns:v1.11",
                    ),
                    _deployment(
                        "kube-system",
                        "ebs-csi-controller",
                        "public.ecr.aws/ebs-csi-driver/aws-ebs-csi-driver:v1.28",
                    ),
                    _deployment(
                        CONTROLLER_NAMESPACE, CONTROLLER_NAME, CONTROLLER_IMAGE
                    ),
                ]
            }
        }
    )

    assert _access(runner).controller_deployments() == (CONTROLLER_IMAGE,)


def test_a_cluster_with_no_workspace_controller_reports_none():
    """The pre-install state. `_refuse_existing_controller` requires this to be empty, so
    a method that reported CoreDNS here would refuse every first bootstrap."""
    runner = _Scripted(
        {
            "get deployments --all-namespaces": {
                "items": [
                    _deployment("kube-system", "coredns", "registry/eks/coredns:v1.11")
                ]
            }
        }
    )

    assert _access(runner).controller_deployments() == ()


def test_a_second_workspace_controller_is_visible_across_namespaces():
    """The read is `--all-namespaces` because a controller reconciling this workspace
    from ANOTHER namespace is the contention the single-reconciler gate exists to catch,
    and a namespaced read would miss it."""
    runner = _Scripted(
        {
            "get deployments --all-namespaces": {
                "items": [
                    _deployment(
                        CONTROLLER_NAMESPACE, CONTROLLER_NAME, CONTROLLER_IMAGE
                    ),
                    _deployment(
                        "somebody-elses-namespace",
                        "superplane-controller",
                        f"other.registry/{CONTROLLER_IMAGE_MARKER}:v0",
                    ),
                ]
            }
        }
    )

    assert len(_access(runner).controller_deployments()) == 2
    assert runner.argv_containing("--all-namespaces"), (
        "a namespaced read would not see the second controller at all"
    )


# --- establish_controller_rbac -------------------------------------------------


def _rbac_objects(runner: _Scripted) -> dict[str, dict]:
    """The RBAC objects the adapter applied, keyed by kind."""
    applied = json.loads(runner.stdin_for("apply")[0])
    return {item["kind"]: item for item in applied["items"]}


def _granted(objects: Mapping[str, dict]) -> set[tuple[str, str]]:
    """Every (verb, resource) pair the applied Role and ClusterRole grant.

    Resources are re-joined into the `name.group` spelling
    `REQUIRED_CONTROLLER_PERMISSIONS` uses, so the comparison is against the required set
    itself rather than against a restatement of it.
    """
    granted: set[tuple[str, str]] = set()
    for kind in ("Role", "ClusterRole"):
        for rule in objects[kind]["rules"]:
            group = rule["apiGroups"][0]
            for resource in rule["resources"]:
                qualified = f"{resource}.{group}" if group else resource
                for verb in rule["verbs"]:
                    granted.add((verb, qualified))
    return granted


def test_the_rbac_grants_exactly_the_permissions_readiness_verifies():
    """Both directions, because they are different claims.

    "Has everything it needs" and "has only what it needs" are checked separately by
    `readiness._rbac_checks`, and a credential satisfying the first while failing the
    second is a cluster-admin controller. Comparing against the imported required set
    means a change to that set fails here rather than producing RBAC that no longer
    matches the gate.
    """
    runner = _Scripted({"apply": "{}"})

    _access(runner).establish_controller_rbac(NAMESPACE, CONTROLLER_SERVICE_ACCOUNT)
    granted = _granted(_rbac_objects(runner))

    assert granted == set(REQUIRED_CONTROLLER_PERMISSIONS), (
        "the established RBAC and the verified permission set have diverged, so the "
        "controller installs and then fails its own readiness gate"
    )
    assert granted.isdisjoint(set(FORBIDDEN_CONTROLLER_PERMISSIONS))


def test_the_namespaced_permissions_are_not_granted_cluster_wide():
    """`watch pods` in a ClusterRole is `watch pods` in a BYOC tenant's namespaces.

    This is the asymmetry the two-object split exists for, and the reason the whole set
    is not granted through one ClusterRole: it would be simpler, and it would hand the
    controller read access to every pod on a supplied cluster.
    """
    runner = _Scripted({"apply": "{}"})

    _access(runner).establish_controller_rbac(NAMESPACE, CONTROLLER_SERVICE_ACCOUNT)
    objects = _rbac_objects(runner)

    cluster_resources = {
        resource
        for rule in objects["ClusterRole"]["rules"]
        for resource in rule["resources"]
    }
    assert "pods" not in cluster_resources
    assert "superplanenodes" not in cluster_resources
    assert "nodepools" in cluster_resources
    # And the cluster-scoped ones cannot be in the Role, because RBAC cannot grant them
    # there however the rule is written.
    role_resources = {
        resource for rule in objects["Role"]["rules"] for resource in rule["resources"]
    }
    assert "nodes" not in role_resources
    assert "nodepools" not in role_resources
    assert "superplanenodes" in role_resources


def test_workload_reader_is_provisioned_only_in_the_workspace_namespace():
    runner = _Scripted({"apply": "{}"})
    _access(runner).establish_controller_rbac(NAMESPACE, CONTROLLER_SERVICE_ACCOUNT)
    objects = _rbac_objects(runner)
    role = objects["Role"]
    assert role["metadata"]["namespace"] == NAMESPACE
    by_resource = {
        (rule["apiGroups"][0], resource): set(rule["verbs"])
        for rule in role["rules"]
        for resource in rule["resources"]
    }
    assert by_resource[("", "pods")] == {"get", "list", "watch"}
    assert by_resource[("", "pods/log")] == {"get"}
    assert by_resource[("apps", "deployments")] == {"get"}
    assert by_resource[("apps", "replicasets")] == {"list"}
    assert by_resource[("batch", "jobs")] == {"get"}
    for resource in ("pods", "pods/log", "deployments", "replicasets", "jobs"):
        assert all(
            resource not in rule["resources"]
            for rule in objects["ClusterRole"]["rules"]
        )
    assert not any(
        resource in {"secrets", "pods/exec", "services/proxy", "*"}
        for _, resource in by_resource
    )
    assert all(verbs <= {"get", "list", "watch"} for verbs in by_resource.values())


def test_the_observer_can_list_each_crd_in_its_declared_api_scope():
    from pathlib import Path

    import yaml

    definitions = (
        Path(__file__).parents[2] / "src/superplane-controller/deploy/crds.yaml"
    )
    scopes = {
        document["spec"]["names"]["plural"]: document["spec"]["scope"]
        for document in yaml.safe_load_all(definitions.read_text())
        if document and document.get("kind") == "CustomResourceDefinition"
    }
    runner = _Scripted({"apply": "{}"})
    _access(runner).establish_controller_rbac(NAMESPACE, CONTROLLER_SERVICE_ACCOUNT)
    objects = _rbac_objects(runner)
    for resource in ("nodepools", "superplanenodes"):
        expected = "ClusterRole" if scopes[resource] == "Cluster" else "Role"
        for kind in ("Role", "ClusterRole"):
            rules = [
                rule for rule in objects[kind]["rules"] if resource in rule["resources"]
            ]
            if kind != expected:
                assert rules == []
            else:
                assert len(rules) == 1
                assert set(rules[0]["verbs"]) == {"list", "watch"}


def test_the_controller_is_granted_no_lease_mutation_anywhere():
    """Not "leases are not in the ClusterRole" — not granted at ALL, in either object.

    The previous revision granted `create`/`update` on `leases.coordination.k8s.io` for a
    controller that ran leader election. The controller #5536 ships runs a registration
    manager with no leader election that never writes a Lease, and its own credential
    check (`management/target.go::readOnlyWorkspaceRules`) whitelists `create` only for
    the virtual self-review resources — so a credential holding `create leases` makes the
    manager refuse its own target with `credential_not_read_only`.

    Asserted over both objects rather than over the required set, because the set is what
    generates them: a test that only read the constant would pass on RBAC that granted it
    some other way.
    """
    runner = _Scripted({"apply": "{}"})

    _access(runner).establish_controller_rbac(NAMESPACE, CONTROLLER_SERVICE_ACCOUNT)
    granted = _granted(_rbac_objects(runner))

    assert not [pair for pair in granted if "leases" in pair[1]], (
        "lease mutation is granted somewhere in the controller's RBAC; the observation-"
        "only registration manager refuses a credential that holds it"
    )


def test_every_granted_verb_is_a_read():
    """The whole grant, checked as a property rather than as a list.

    `readOnlyWorkspaceRules` does not ask "is this the expected set" — it asks whether
    every verb is `get`/`list`/`watch` (or a self-review `create`). So the durable
    invariant is that this RBAC contains no mutating verb at all, which stays true as
    the required set changes and fails the moment a future edit adds a write for
    convenience. Without this, adding `("patch", "nodes")` to the set would keep every
    other test in this file green.
    """
    runner = _Scripted({"apply": "{}"})

    _access(runner).establish_controller_rbac(NAMESPACE, CONTROLLER_SERVICE_ACCOUNT)
    granted = _granted(_rbac_objects(runner))

    assert granted, "no permissions were granted at all, so this proves nothing"
    mutating = sorted(
        f"{verb} {resource}"
        for verb, resource in granted
        if verb not in {"get", "list", "watch"}
    )
    assert not mutating, (
        f"the workspace controller is granted mutating verbs ({', '.join(mutating)}); "
        "the registration manager's own credential check refuses anything beyond "
        "get/list/watch and would report credential_not_read_only"
    )


def test_the_cluster_wide_namespace_read_is_narrowed_to_this_namespace():
    """`get namespaces` is cluster-scoped, so RBAC can only grant it via a ClusterRole —
    and an unrestricted one reads EVERY namespace on the cluster, a BYOC tenant's
    included. `resourceNames` is the only thing that bounds it, and this asserts the
    bound is present and is this workspace's namespace.

    `list`/`watch` are deliberately not narrowed anywhere: RBAC ignores `resourceNames`
    for them, so naming one would read as a bound that is not actually enforced.
    """
    runner = _Scripted({"apply": "{}"})

    _access(runner).establish_controller_rbac(NAMESPACE, CONTROLLER_SERVICE_ACCOUNT)
    objects = _rbac_objects(runner)

    namespace_rules = [
        rule
        for rule in objects["ClusterRole"]["rules"]
        if "namespaces" in rule["resources"]
    ]
    assert len(namespace_rules) == 1, namespace_rules
    rule = namespace_rules[0]
    assert rule["verbs"] == ["get"]
    assert rule.get("resourceNames") == [NAMESPACE], (
        "the namespace read is not narrowed to the workspace namespace, so the "
        "controller can read every namespace on the cluster"
    )
    for other in objects["ClusterRole"]["rules"]:
        if "namespaces" in other["resources"]:
            continue
        # The narrowing must not have leaked onto a list/watch rule, where RBAC does not
        # honour it — that would look like a bound and enforce nothing.
        assert "resourceNames" not in other, other


def test_a_name_scoped_resource_refuses_to_be_granted_unnamed():
    """The fallback direction matters: `_rbac_rules` must refuse rather than emit the
    wide rule. A missing name silently producing an unrestricted `get namespaces` is
    precisely the grant the narrowing exists to prevent, and it would pass every other
    assertion in this file."""
    from superplane_bootstrap.adapters import _rbac_rules

    with pytest.raises(BootstrapRefused) as refusal:
        _rbac_rules(["namespaces"], resource_names={"namespaces": "  "})

    assert "named object only" in str(refusal.value)

    # And the same when the mapping simply does not mention it.
    with pytest.raises(BootstrapRefused):
        _rbac_rules(["namespaces"])


def test_the_rbac_is_bound_to_the_workspace_namespace_only():
    """The RoleBinding's subject and the Role it references must both be namespaced here,
    or the binding grants the namespaced half somewhere else."""
    runner = _Scripted({"apply": "{}"})

    _access(runner).establish_controller_rbac(NAMESPACE, CONTROLLER_SERVICE_ACCOUNT)
    objects = _rbac_objects(runner)

    assert objects["Role"]["metadata"]["namespace"] == NAMESPACE
    assert objects["RoleBinding"]["metadata"]["namespace"] == NAMESPACE
    assert objects["RoleBinding"]["roleRef"]["kind"] == "Role"
    for binding_kind in ("RoleBinding", "ClusterRoleBinding"):
        subject = objects[binding_kind]["subjects"][0]
        assert subject == {
            "kind": "ServiceAccount",
            "name": CONTROLLER_SERVICE_ACCOUNT,
            "namespace": NAMESPACE,
        }


def test_the_rbac_names_every_object_it_created():
    """`_install_controller` refuses when this returns nothing, and records what it does
    return as OWNED so cleanup can remove exactly that. An object created but not named
    is an object cleanup leaks — the F6 property at the adapter layer."""
    runner = _Scripted({"apply": "{}"})

    created = _access(runner).establish_controller_rbac(
        NAMESPACE, CONTROLLER_SERVICE_ACCOUNT
    )
    applied_kinds = set(_rbac_objects(runner))

    assert set(created) == applied_kinds, (
        "the adapter applied objects it did not report, so cleanup cannot name them"
    )


def test_the_cluster_scoped_rbac_name_is_unique_per_namespace():
    """ClusterRoles share one namespace-less name space.

    Two workspaces on one cluster — the BYOC case — would otherwise apply the same
    ClusterRole name and the second would silently rebind the first's permissions.
    """
    runner = _Scripted({"apply": "{}"})
    access = _access(runner)

    first = access.establish_controller_rbac("workspace-a", CONTROLLER_SERVICE_ACCOUNT)
    second = access.establish_controller_rbac("workspace-b", CONTROLLER_SERVICE_ACCOUNT)

    assert first["ClusterRole"] != second["ClusterRole"]
    assert first["ClusterRoleBinding"] != second["ClusterRoleBinding"]


def test_the_rbac_manifest_goes_in_on_stdin():
    """Nothing is written to disk and nothing appears in a process listing, the same rule
    `create_namespace` follows."""
    runner = _Scripted({"apply": "{}"})

    _access(runner).establish_controller_rbac(NAMESPACE, CONTROLLER_SERVICE_ACCOUNT)

    assert runner.stdin_for("apply"), "the manifest was not passed on stdin"
    assert runner.argv_containing("apply -f -")


def test_rbac_uses_apply_so_a_retry_over_existing_objects_succeeds():
    """The Protocol requires idempotence. `create` would fail with AlreadyExists on the
    second attempt, which would make a bootstrap retry impossible after any later step
    failed — and retrying after a partial failure is the normal case."""
    runner = _Scripted({"apply": "{}"})

    _access(runner).establish_controller_rbac(NAMESPACE, CONTROLLER_SERVICE_ACCOUNT)

    assert runner.argv_containing("apply")
    assert not runner.argv_containing("create -f")


def test_a_failed_rbac_apply_refuses_without_echoing_output():
    """`_install_controller` converts this into a refusal carrying the partial
    installation, so what matters here is that it raises rather than returning a
    half-truth — and that the message does not carry the command's stderr."""
    runner = _Scripted({}, fail=["apply"])

    with pytest.raises(BootstrapRefused) as refusal:
        _access(runner).establish_controller_rbac(NAMESPACE, CONTROLLER_SERVICE_ACCOUNT)

    assert "synthetic failure" not in str(refusal.value)
    assert NAMESPACE in str(refusal.value)


# --- install_controller --------------------------------------------------------


def test_the_controller_is_installed_with_the_adapter_s_image():
    """The image is the adapter's, not the caller's: `ClusterAccess.install_controller`
    has no parameter for one, because choosing a controller version is the release
    owner's decision rather than a per-call one."""
    runner = _Scripted(
        {
            "apply": "{}",
            f"get deployment {CONTROLLER_NAME}": _deployment(
                NAMESPACE, CONTROLLER_NAME, CONTROLLER_IMAGE
            ),
        }
    )

    _access(runner).install_controller(
        NAMESPACE, CONTROLLER_NAME, CONTROLLER_SERVICE_ACCOUNT
    )
    applied = json.loads(runner.stdin_for("apply")[0])
    container = applied["spec"]["template"]["spec"]["containers"][0]

    assert container["image"] == CONTROLLER_IMAGE
    assert (
        applied["spec"]["template"]["spec"]["serviceAccountName"]
        == CONTROLLER_SERVICE_ACCOUNT
    )
    assert inspect.signature(ClusterAccess.install_controller).parameters.keys() == {
        "self",
        "namespace",
        "name",
        "service_account",
    }


def test_the_installed_controller_is_read_back_rather_than_assumed():
    """**The F2 property at the adapter layer.**

    A successful `kubectl apply` means the object was accepted, not that a pod runs. The
    returned workload must be the READ, so `availableReplicas: 0` — the exact state F2
    was about — is reported as the fact it is instead of being masked by the apply's exit
    status.
    """
    runner = _Scripted(
        {
            "apply": "{}",
            f"get deployment {CONTROLLER_NAME}": {
                "metadata": {"name": CONTROLLER_NAME, "namespace": NAMESPACE},
                "spec": {"replicas": 1},
                "status": {"availableReplicas": 0},
            },
        }
    )

    observed = _access(runner).install_controller(
        NAMESPACE, CONTROLLER_NAME, CONTROLLER_SERVICE_ACCOUNT
    )

    assert observed.desired_replicas == 1
    assert observed.available_replicas == 0
    assert runner.argv_containing(f"get deployment {CONTROLLER_NAME}"), (
        "the adapter returned an availability claim it never read"
    )


def test_a_controller_that_cannot_be_read_back_refuses():
    """An object that cannot be observed cannot be reported as available, and returning
    None here would hand the readiness gate nothing to check."""

    # The apply succeeds and the read back says NotFound. `workload` reports that as None
    # — the correct answer to "is it there" — and `install_controller` must refuse rather
    # than pass None on as an observation.
    class _AppliedThenGone(_Scripted):
        def run(self, args, *, data=None, timeout=120):
            argv = tuple(str(a) for a in args)
            self.calls.append((argv, data))
            if "get deployment" in " ".join(argv):
                return CommandResult(
                    args=argv, returncode=1, stderr="Error from server (NotFound)"
                )
            return CommandResult(args=argv, returncode=0, stdout="{}")

    with pytest.raises(BootstrapRefused, match="could not be read back"):
        _access(_AppliedThenGone({})).install_controller(
            NAMESPACE, CONTROLLER_NAME, CONTROLLER_SERVICE_ACCOUNT
        )


def test_controller_can_run_on_pristine_nodes_before_tenant_interlock_is_cleared():
    """Readiness requires this controller while every node still has the taint."""
    runner = _Scripted(
        {
            "apply": "{}",
            f"get deployment {CONTROLLER_NAME}": _deployment(
                NAMESPACE, CONTROLLER_NAME, CONTROLLER_IMAGE
            ),
        }
    )

    _access(runner).install_controller(
        NAMESPACE, CONTROLLER_NAME, CONTROLLER_SERVICE_ACCOUNT
    )
    spec = json.loads(runner.stdin_for("apply")[0])["spec"]["template"]["spec"]

    assert spec["tolerations"] == [
        {
            "key": _BOOTSTRAP_TAINT_KEY,
            "operator": "Equal",
            "value": "pending",
            "effect": "NoSchedule",
        }
    ]


def test_the_controller_pod_satisfies_the_restricted_standard():
    """The platform enforces `restricted` on tenant namespaces, and a controller that
    could not run under it would make that claim one this platform does not itself meet."""
    runner = _Scripted(
        {
            "apply": "{}",
            f"get deployment {CONTROLLER_NAME}": _deployment(
                NAMESPACE, CONTROLLER_NAME, CONTROLLER_IMAGE
            ),
        }
    )

    _access(runner).install_controller(
        NAMESPACE, CONTROLLER_NAME, CONTROLLER_SERVICE_ACCOUNT
    )
    pod = json.loads(runner.stdin_for("apply")[0])["spec"]["template"]["spec"]

    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    container = pod["containers"][0]["securityContext"]
    assert container["allowPrivilegeEscalation"] is False
    assert container["capabilities"]["drop"] == ["ALL"]


def test_the_installed_controller_is_recognisable_to_the_reconciler_gate():
    """The image the adapter installs must be one `controller_deployments` counts.

    Otherwise the workspace's own controller is invisible to the single-reconciler gate,
    readiness passes with it uncounted, and a SECOND bootstrap against the same cluster
    would not refuse — two controllers contending over the same cluster-scoped NodePools.
    """
    installed = _Scripted(
        {
            "apply": "{}",
            f"get deployment {CONTROLLER_NAME}": _deployment(
                NAMESPACE, CONTROLLER_NAME, CONTROLLER_IMAGE
            ),
        }
    )
    _access(installed).install_controller(
        NAMESPACE, CONTROLLER_NAME, CONTROLLER_SERVICE_ACCOUNT
    )
    applied_image = json.loads(installed.stdin_for("apply")[0])["spec"]["template"][
        "spec"
    ]["containers"][0]["image"]

    listing = _Scripted(
        {
            "get deployments --all-namespaces": {
                "items": [_deployment(NAMESPACE, CONTROLLER_NAME, applied_image)]
            }
        }
    )
    assert _access(listing).controller_deployments() == (applied_image,)


@pytest.mark.parametrize(
    "image, expected",
    [
        ("", "no workspace controller image"),
        ("   ", "no workspace controller image"),
        ("registry.example/some-other-thing:v1", "does not contain"),
    ],
)
def test_an_unusable_controller_image_refuses_at_construction(image, expected):
    """Refused when the adapter is built, not when the controller is installed.

    `install_controller` runs after the namespace and the CRDs exist, so a bad image
    discovered there costs a refusal with a partial installation to clean up. Discovered
    at construction it costs nothing, and the CLI builds the adapter before it touches
    the cluster.
    """
    with pytest.raises(BootstrapRefused, match=expected):
        _access(_Scripted({}), controller_image=image)


# --- reads: an unreadable answer is never a confident one ----------------------


def test_an_absent_namespace_reads_as_none():
    class _NotFound(_Scripted):
        def run(self, args, *, data=None, timeout=120):
            argv = tuple(str(a) for a in args)
            self.calls.append((argv, data))
            return CommandResult(
                args=argv,
                returncode=1,
                stderr='Error from server (NotFound): namespaces "x" not found',
            )

    assert _access(_NotFound({})).namespace(NAMESPACE) is None


def test_an_unreadable_namespace_refuses_rather_than_reading_as_absent():
    """ "Absent" and "denied" are different answers and only the first may be None.

    Treating denied as absent would send `_adopt_or_create_namespace` on to CREATE a
    namespace that already exists — adopting it without ever comparing its labels, which
    is the F3 hazard reached through a read error.
    """
    runner = _Scripted({}, fail=["get namespace"])

    with pytest.raises(BootstrapRefused, match="refusing to treat an unreadable"):
        _access(runner).namespace(NAMESPACE)


def test_the_created_namespace_uid_comes_from_the_api_server():
    """F3's whole mechanism. The uid ADP records must be the one the API server assigned,
    because every later ownership decision compares against it."""
    runner = _Scripted(
        {
            "create": {
                "metadata": {
                    "name": NAMESPACE,
                    "uid": NAMESPACE_UID,
                    "labels": {"pod-security.kubernetes.io/enforce": "restricted"},
                }
            }
        }
    )

    observed = _access(runner).create_namespace(NAMESPACE, {"a": "b"})

    assert observed.uid == NAMESPACE_UID
    assert runner.stdin_for("create"), "the manifest was not passed on stdin"


def test_establish_crds_refuses_a_name_with_no_declared_manifest():
    """A skipped CRD surfaces later as a type the controller cannot reconcile, so an
    undeclared name refuses rather than being quietly dropped."""
    runner = _Scripted({})

    with pytest.raises(BootstrapRefused, match="no manifest was declared"):
        _access(runner).establish_crds(["nodepools.superplane.ai"])


def test_establish_crds_returns_a_fresh_read_not_an_echo(tmp_path):
    """`kubectl apply` succeeding is not the same fact as the CRD being established, and
    only the second may satisfy the gate."""
    manifest = tmp_path / "nodepools.yaml"
    manifest.write_text("---\n")
    runner = _Scripted(
        {
            "apply": "{}",
            "get crd": {"items": [{"metadata": {"name": "nodepools.superplane.ai"}}]},
        }
    )

    established = _access(
        runner, manifests={"nodepools.superplane.ai": manifest}
    ).establish_crds(["nodepools.superplane.ai"])

    assert established == ("nodepools.superplane.ai",)
    assert runner.argv_containing("get crd"), "the result was an echo, not a read"


def test_a_crd_absent_after_a_successful_apply_is_omitted(tmp_path):
    """The apply succeeded and the type is still absent — a webhook rejected it, or the
    manifest declared a different name. It must not be reported as established, or the
    controller is installed for a type it cannot reconcile. `install_components` turns
    this omission into the "required CRDs are not established" refusal."""
    manifest = tmp_path / "nodepools.yaml"
    manifest.write_text("---\n")
    runner = _Scripted({"apply": "{}", "get crd": {"items": []}})

    established = _access(
        runner, manifests={"nodepools.superplane.ai": manifest}
    ).establish_crds(["nodepools.superplane.ai"])

    assert established == ()


def test_unparseable_output_refuses_rather_than_defaulting_to_empty():
    """An empty default for "does this CRD exist" reads as a confident no."""
    runner = _Scripted({"get crd": "not json at all"})

    with pytest.raises(BootstrapRefused, match="did not return valid JSON"):
        _access(runner).custom_resource_definitions()


# --- probes: the interlock and admission --------------------------------------


def test_the_tenant_interlock_reads_live_taints_on_every_node():
    """One schedulable node is enough for a tenant pod to land on, so the answer is only
    True when EVERY node still carries the taint."""
    tainted = {
        "spec": {"taints": [{"key": _BOOTSTRAP_TAINT_KEY, "effect": "NoSchedule"}]}
    }
    clean = {"spec": {"taints": []}}

    assert (
        _access(
            _Scripted({"get nodes": {"items": [tainted, tainted]}})
        ).tenant_scheduling_denied(NAMESPACE)
        is True
    )
    assert (
        _access(
            _Scripted({"get nodes": {"items": [tainted, clean]}})
        ).tenant_scheduling_denied(NAMESPACE)
        is False
    )


def test_no_nodes_is_unanswerable_rather_than_safe():
    """ "No nodes" is not "tenant work is safely blocked". `readiness.py` refuses on None,
    and returning True here would let a cluster with no nodes pass the interlock check."""
    runner = _Scripted({"get nodes": {"items": []}})

    assert _access(runner).tenant_scheduling_denied(NAMESPACE) is None


def test_admission_is_probed_server_side():
    """A client-side dry run would skip Pod Security Admission — the very controller
    being probed — and report every unsafe pod as admitted."""
    runner = _Scripted({"apply": "{}"})

    _access(runner).dry_run_pod(NAMESPACE, {"metadata": {"name": "probe"}})

    assert runner.argv_containing("--dry-run=server")


def test_a_rejected_pod_carries_the_reason():
    """A negative test must distinguish "admission rejected this" from "the request
    failed for an unrelated reason" — opposite conclusions about the same cluster."""
    runner = _Scripted({}, fail=["apply"])

    observed = _access(runner).dry_run_pod(NAMESPACE, {"metadata": {"name": "probe"}})

    assert observed.admitted is False
    assert observed.rejected_reason


def test_the_probe_body_is_a_pod_the_api_server_can_parse():
    """The third defect: `dry_run_pod` sent `admission.py`'s ABSTRACT probe as the
    manifest — no `apiVersion`, no `kind`, no container, and `hostPID` at the top level.
    Real kubectl rejects that at client-side VALIDATION, before admission is consulted,
    so every probe came back rejected INCLUDING the conforming control — and
    `_unsafe_pod_proof` reads a rejected control as "the rejections above do not
    establish selective enforcement". The interlock could never be released, on any
    cluster, and the cause was indistinguishable from a real Pod Security rejection."""
    runner = _Scripted({"apply": "{}"})

    _access(runner).dry_run_pod(NAMESPACE, {"name": "bootstrap-probe-hostpid"})

    sent = json.loads(runner.stdin_for("--dry-run=server")[0])
    assert sent["apiVersion"] == "v1"
    assert sent["kind"] == "Pod"
    assert sent["metadata"]["name"] == "bootstrap-probe-hostpid"
    assert sent["metadata"]["namespace"] == NAMESPACE
    containers = sent["spec"]["containers"]
    assert len(containers) == 1 and containers[0]["image"], (
        "a Pod with no container is a validation error, not an admission decision"
    )


@pytest.mark.parametrize(
    ("field", "locate"),
    [
        ("hostNetwork", lambda pod: pod["spec"].get("hostNetwork")),
        ("hostPID", lambda pod: pod["spec"].get("hostPID")),
        (
            "privileged",
            lambda pod: pod["spec"]["containers"][0]["securityContext"].get(
                "privileged"
            ),
        ),
        ("hostPath", lambda pod: bool(pod["spec"].get("volumes"))),
    ],
)
def test_each_unsafe_field_lands_where_kubernetes_actually_reads_it(field, locate):
    """`UNSAFE_POD_FIELDS` is an abstract vocabulary; Kubernetes reads each of those
    four in a different place. A field placed at the top level is ignored by admission
    (or fails validation), so the probe would test nothing while reporting a verdict."""
    runner = _Scripted({"apply": "{}"})

    _access(runner).dry_run_pod(NAMESPACE, {"name": "probe", field: True})

    sent = json.loads(runner.stdin_for("--dry-run=server")[0])
    assert locate(sent) is True or locate(sent), (
        f"{field} was not placed where the API server reads it: {sent}"
    )


def test_the_conforming_control_probe_satisfies_the_restricted_standard():
    """The control must be ADMITTED on a correct cluster, because `_unsafe_pod_proof`
    reads its rejection as "admission rejects everything, so the rejections above prove
    nothing". A merely minimal pod is rejected by `restricted` for the fields it omits,
    which would make a correctly-configured cluster fail its own proof."""
    runner = _Scripted({"apply": "{}"})

    _access(runner).dry_run_pod(NAMESPACE, {"name": "bootstrap-probe-conforming"})

    sent = json.loads(runner.stdin_for("--dry-run=server")[0])
    security = sent["spec"]["containers"][0]["securityContext"]
    assert security["allowPrivilegeEscalation"] is False
    assert security["capabilities"]["drop"] == ["ALL"]
    assert security["runAsNonRoot"] is True
    assert security["seccompProfile"]["type"] == "RuntimeDefault"
    for unsafe in ("hostNetwork", "hostPID", "hostIPC"):
        assert unsafe not in sent["spec"]
    assert "privileged" not in security
    assert "volumes" not in sent["spec"]


def test_controller_permissions_asks_both_the_required_and_forbidden_pairs():
    runner = _Scripted({"create -f -": {"status": {"allowed": True}}})
    answers = _access(runner).controller_permissions(NAMESPACE)
    assert set(answers) == set(
        (*REQUIRED_CONTROLLER_PERMISSIONS, *FORBIDDEN_CONTROLLER_PERMISSIONS)
    )
    for argv, data in runner.calls:
        spec = json.loads(data)["spec"]
        assert (
            spec["user"]
            == f"system:serviceaccount:{NAMESPACE}:{CONTROLLER_SERVICE_ACCOUNT}"
        )
        assert "--as" not in argv
        attrs = spec["resourceAttributes"]
        if attrs["resource"] in {
            "nodes",
            "nodepools",
            "namespaces",
            "clusterrolebindings",
        }:
            assert "namespace" not in attrs
        else:
            assert attrs["namespace"] == NAMESPACE


def test_the_namespace_read_is_probed_with_the_name_it_was_granted_for():
    """Install and verify must ask the same question, or the gate refuses a correct install.

    `get namespaces` is granted by a ClusterRole narrowed with `resourceNames: [ns]`. An
    unnamed SubjectAccessReview asks "may it get ANY namespace", which RBAC answers `no`
    — so without the name this probe would report a denial for a controller that is
    installed exactly right, and the taint would never come off. This is the same
    install-says-one-thing-verify-says-another class the `leases` drift was.

    `delete namespaces` must stay unnamed: the forbidden set asks whether the credential
    can delete any namespace at all, and naming one would narrow a check whose breadth is
    the whole point.
    """
    runner = _Scripted({"create -f -": {"status": {"allowed": True}}})

    _access(runner).controller_permissions(NAMESPACE)

    asked = {}
    for _argv, data in runner.calls:
        attrs = json.loads(data)["spec"]["resourceAttributes"]
        asked[(attrs["verb"], attrs["resource"])] = attrs
    assert asked[("get", "namespaces")].get("name") == NAMESPACE
    assert "name" not in asked[("delete", "namespaces")]


def test_an_unanswerable_permission_is_absent_rather_than_denied():
    """`readiness._rbac_checks` refuses on a MISSING key and reports a denial for a False
    one. Recording an unanswerable question as False would turn "we could not ask" into
    "the controller lacks this", which is a different and misleading refusal."""

    class _Unknown(_Scripted):
        def run(self, args, *, data=None, timeout=120):
            argv = tuple(str(a) for a in args)
            return CommandResult(args=argv, returncode=1, stdout="error: unknown\n")

    assert _access(_Unknown({})).controller_permissions(NAMESPACE) == {}


def test_a_lease_held_by_another_controller_is_an_incomplete_handover():
    """The lease is the authority, not the Deployment: a controller whose Deployment is
    gone but whose lease is held may still be reconciling."""
    runner = _Scripted(
        {"get lease": {"spec": {"holderIdentity": "some-other-controller-xyz"}}}
    )

    handover = _access(runner).controller_handover(NAMESPACE)

    assert handover["complete"] is False


def test_an_unreadable_lease_answers_nothing():
    """No `complete` key at all, which `readiness.py` refuses on — as opposed to a
    NotFound lease, which genuinely means nothing holds the workspace."""
    runner = _Scripted({}, fail=["get lease"])

    assert "complete" not in _access(runner).controller_handover(NAMESPACE)


def test_an_absent_lease_is_a_complete_handover():
    class _NotFound(_Scripted):
        def run(self, args, *, data=None, timeout=120):
            argv = tuple(str(a) for a in args)
            return CommandResult(args=argv, returncode=1, stderr="(NotFound) not found")

    assert _access(_NotFound({})).controller_handover(NAMESPACE) == {"complete": True}


def test_only_the_named_system_workload_is_granted_a_toleration():
    """The F2 CoreDNS mechanism. One named Deployment in the system namespace at a time:
    there is no path here that patches a tenant workload or edits a shared default."""
    runner = _Scripted(
        {
            "patch": "{}",
            "get deployment coredns": _deployment(
                "kube-system", "coredns", "registry/coredns:v1"
            ),
        }
    )

    placed = _access(runner).place_system_workloads("kube-system", ["coredns"])

    assert placed == {"coredns": True}
    patch = json.loads(runner.argv_containing("patch")[0][-1])
    toleration = patch["spec"]["template"]["spec"]["tolerations"][0]
    assert toleration["key"] == _BOOTSTRAP_TAINT_KEY


def test_a_placed_workload_that_never_becomes_available_reports_false():
    """The patch succeeding is not the fact the gate needs. A Deployment the cluster
    accepted and never scheduled is the exact state F2 was about."""
    runner = _Scripted(
        {
            "patch": "{}",
            "get deployment coredns": {
                "metadata": {"name": "coredns", "namespace": "kube-system"},
                "spec": {"replicas": 2},
                "status": {"availableReplicas": 0},
            },
        }
    )

    assert _access(runner).place_system_workloads("kube-system", ["coredns"]) == {
        "coredns": False
    }


def test_taint_removal_and_restoration_re_read_the_live_taints():
    """Both return a fresh read: the gate's question is "are the nodes schedulable NOW",
    and a successful `kubectl taint` does not answer it for a node added mid-run."""
    runner = _Scripted(
        {
            "taint": "",
            "get nodes": {
                "items": [
                    {
                        "spec": {
                            "taints": [
                                {"key": _BOOTSTRAP_TAINT_KEY, "effect": "NoSchedule"}
                            ]
                        }
                    }
                ]
            },
        }
    )
    access = _access(runner)

    access.remove_bootstrap_taint(_BOOTSTRAP_TAINT_KEY)
    access.restore_bootstrap_taint(_BOOTSTRAP_TAINT_KEY)

    assert len(runner.argv_containing("get nodes")) == 2
    restore = runner.argv_containing("taint")[1]
    assert "--overwrite" in restore, (
        "restoring without --overwrite fails on a node that still carries the taint, "
        "which is the expected state on the recovery path"
    )


@pytest.mark.parametrize(
    "extra",
    [
        None,
        {},
        {"spec": {"taints": []}},
        {
            "spec": {
                "taints": [{"key": _BOOTSTRAP_TAINT_KEY, "effect": "PreferNoSchedule"}]
            }
        },
    ],
)
def test_partial_interlock_restoration_is_not_success(extra):
    nodes = (
        []
        if extra is None
        else [
            {
                "spec": {
                    "taints": [{"key": _BOOTSTRAP_TAINT_KEY, "effect": "NoSchedule"}]
                }
            },
            extra,
        ]
    )
    runner = _Scripted({"taint": "", "get nodes": {"items": nodes}})
    with pytest.raises(BootstrapRefused, match="every node"):
        _access(runner).restore_bootstrap_taint(_BOOTSTRAP_TAINT_KEY)


def test_failed_node_read_cannot_confirm_interlock_even_with_json():
    from dataclasses import replace

    class FailedRead(_Scripted):
        def run(self, args, **kwargs):
            result = super().run(args, **kwargs)
            return replace(result, returncode=1) if "get" in args else result

    runner = FailedRead(
        {
            "taint": "",
            "get nodes": {
                "items": [
                    {
                        "spec": {
                            "taints": [
                                {"key": _BOOTSTRAP_TAINT_KEY, "effect": "NoSchedule"}
                            ]
                        }
                    }
                ]
            },
        }
    )
    with pytest.raises(
        BootstrapRefused, match="reading restored bootstrap taints failed"
    ):
        _access(runner).restore_bootstrap_taint(_BOOTSTRAP_TAINT_KEY)


# --- cni_credential_scope: the fourth defect, a seam satisfied nowhere ---------


def _simulated(decisions: Mapping[str, str]) -> _Scripted:
    """A runner answering `simulate-principal-policy` per action name."""

    class _Iam(_Scripted):
        def run(self, args, *, data=None, timeout=120):
            argv = tuple(str(a) for a in args)
            self.calls.append((argv, data))
            if "simulate-principal-policy" not in argv:
                return super().run(args, data=data, timeout=timeout)
            asked = [a for a in argv if a in decisions]
            return CommandResult(
                args=argv,
                returncode=0,
                stdout=json.dumps(
                    {
                        "EvaluationResults": [
                            {"EvalActionName": a, "EvalDecision": decisions[a]}
                            for a in asked
                        ]
                    }
                ),
            )

    return _Iam(
        {
            "get serviceaccount aws-node": {
                "metadata": {
                    "annotations": {
                        "eks.amazonaws.com/role-arn": (
                            f"arn:aws:iam::{ACCOUNT_ID}:role/superplane-cni"
                        )
                    }
                }
            }
        }
    )


_ALL_DENIED = dict.fromkeys(
    (*IamNodeRoleFacts.CNI_ACTIONS, *IamNodeRoleFacts.ECR_ACTIONS), "implicitDeny"
)


def test_cni_credential_scope_answers_every_key_its_proof_requires():
    """The fourth defect. `admission._cni_scope_proof` requires three keys and
    `cni_credential_scope` returned exactly one: the node-role halves were left to "the
    CLI", which merged nothing. The proof therefore reported "no observation for
    node_role_has_cni_permissions, node_role_has_account_wide_ecr" on EVERY cluster, so
    `cni_credentials_scoped` was unprovable and the taint stayed on forever. A seam
    whose contract is satisfied nowhere is the F1 defect a second time."""
    scope = _access(_simulated(_ALL_DENIED)).cni_credential_scope()

    for key in (
        "aws_node_role_arn",
        "node_role_has_cni_permissions",
        "node_role_has_account_wide_ecr",
    ):
        assert key in scope, (
            f"{key} is required by _cni_scope_proof and was not answered"
        )
    assert scope["node_role_has_cni_permissions"] is False
    assert scope["node_role_has_account_wide_ecr"] is False


def test_the_node_role_is_simulated_not_its_policy_documents_parsed():
    """What the role CAN DO is the union of managed, inline and boundary policies.
    Re-implementing IAM evaluation here would risk reporting an over-scoped role as
    scoped, which is the one error this proof exists to prevent."""
    runner = _simulated(_ALL_DENIED)

    _access(runner).cni_credential_scope()

    simulated = runner.argv_containing("simulate-principal-policy")
    assert simulated, "the node role's permissions were not read from IAM at all"
    for argv in simulated:
        assert NODE_ROLE_ARN in argv, (
            "a role other than the declared node_role_arn was simulated, so the answer "
            "describes the wrong subject"
        )


def test_one_allowed_cni_action_is_enough_to_report_the_node_role_unscoped():
    """ANY, not ALL: the claim is "the node role does not carry CNI permissions", and a
    single allowed action falsifies it. Requiring all three would report a role holding
    two of them as scoped."""
    partial = dict(_ALL_DENIED)
    partial["ec2:AttachNetworkInterface"] = "allowed"

    scope = _access(_simulated(partial)).cni_credential_scope()

    assert scope["node_role_has_cni_permissions"] is True


def test_an_unreadable_simulation_omits_the_key_rather_than_reporting_denied():
    """A failed `aws iam` call has not established that anything is denied. The key is
    omitted so `_cni_scope_proof` refuses; a defaulted False would publish an
    unverified "the node role is scoped" and release the interlock on it."""
    runner = _Scripted(
        {"get serviceaccount aws-node": {"metadata": {"annotations": {}}}},
        fail=["simulate-principal-policy"],
    )

    scope = _access(runner).cni_credential_scope()

    assert "node_role_has_cni_permissions" not in scope
    assert "node_role_has_account_wide_ecr" not in scope


def test_a_partial_simulation_result_is_not_a_denial():
    """Fewer decisions than actions cannot distinguish "denied" from "not asked about"."""
    runner = _simulated({"ec2:CreateNetworkInterface": "implicitDeny"})

    scope = _access(runner).cni_credential_scope()

    assert "node_role_has_cni_permissions" not in scope


def test_the_node_role_facts_adapter_satisfies_its_protocol():
    """The same `isinstance` check F1's first defect slipped past, applied to the new
    seam at the moment it is introduced rather than after a live bootstrap fails."""
    facts = IamNodeRoleFacts(runner=_Scripted({}), node_role_arn=NODE_ROLE_ARN)

    assert isinstance(facts, NodeRoleFacts)
    assert inspect.signature(facts.cni_credential_facts).parameters == {}


def test_a_blank_node_role_arn_refuses_at_construction():
    """Refusing here, rather than simulating an empty ARN and getting None back, keeps
    "the infrastructure did not publish node_role_arn" distinguishable from "IAM could
    not be read" — the first is a wiring bug, the second a transient failure."""
    with pytest.raises(BootstrapRefused, match="node role ARN"):
        IamNodeRoleFacts(runner=_Scripted({}), node_role_arn="   ")


# --- the AWS adapters ---------------------------------------------------------


def test_the_observed_cluster_identity_is_read_from_aws():
    runner = _Scripted(
        {
            "eks describe-cluster": {
                "cluster": {
                    "name": CLUSTER_NAME,
                    "arn": CLUSTER_ARN,
                    "endpoint": ENDPOINT,
                    "status": "ACTIVE",
                    "version": "1.31",
                    "certificateAuthority": {"data": CA_DATA},
                    "resourcesVpcConfig": {
                        "vpcId": VPC_ID,
                        "clusterSecurityGroupId": CLUSTER_SG_ID,
                    },
                    "identity": {"oidc": {"issuer": OIDC_ISSUER}},
                }
            }
        }
    )

    observed = AwsObserver(runner=runner, region=REGION).cluster_identity(CLUSTER_NAME)

    assert observed.arn == CLUSTER_ARN
    assert observed.certificate_authority_data == CA_DATA
    assert observed.status == "ACTIVE"
    # The account comes from the ARN, not from the caller's STS answer: reading it from
    # `get-caller-identity` would compare the caller's account against itself and always
    # agree, which is the request-against-itself comparison `target.py` refuses.
    assert observed.account_id == ACCOUNT_ID
    assert not runner.argv_containing("sts get-caller-identity")


def test_the_provider_identity_is_resolved_immediately_not_cached():
    """`ProviderIdentity`'s own rule: resolved right before the gate that uses it, so a
    stale account id from an earlier phase cannot authorize a mutation elsewhere."""
    runner = _Scripted(
        {
            "sts get-caller-identity": {
                "Account": ACCOUNT_ID,
                "Arn": f"arn:aws:iam::{ACCOUNT_ID}:role/Synthetic",
            }
        }
    )

    identity = AwsObserver(runner=runner, region=REGION).provider_identity()

    assert identity.account_id == ACCOUNT_ID


def test_a_failed_aws_call_refuses_without_echoing_output():
    """A refusal message is the thing most likely to land in an issue comment, and `aws`
    echoes request bodies into stderr."""
    runner = _Scripted({}, fail=["sts get-caller-identity"])

    with pytest.raises(BootstrapRefused) as refusal:
        AwsObserver(runner=runner, region=REGION).provider_identity()

    assert "synthetic failure" not in str(refusal.value)


# --- the production runner holds no secret ------------------------------------


def test_the_subprocess_runner_strips_the_installer_secrets():
    """Same two vars `installation/runner.py::Commands` strips. A subprocess that does
    not need a secret must not be able to read one out of its environment."""
    runner = SubprocessRunner(
        env={
            "PATH": "/usr/bin",
            "SUPERPLANE_VERIFICATION_TOKEN": "must-not-propagate",
            "SUPERPLANE_DATABASE_ADMIN_URL": "postgres://must-not-propagate",
        }
    )

    assert "SUPERPLANE_VERIFICATION_TOKEN" not in runner._env
    assert "SUPERPLANE_DATABASE_ADMIN_URL" not in runner._env
    assert runner._env["PATH"] == "/usr/bin"


def test_a_missing_binary_refuses_rather_than_raising_oserror():
    """An OSError escaping into a gate's handler would be an unhandled failure mid-
    sequence; a refusal is what the gates are written to receive."""
    runner = SubprocessRunner(env={"PATH": "/nonexistent"})

    with pytest.raises(BootstrapRefused, match="unavailable or timed out"):
        runner.run(["definitely-not-a-real-binary-5533"])


def test_the_adapter_holds_a_kubeconfig_path_and_never_its_contents(tmp_path):
    """There is no attribute here that could hold a token. Asserted over the instance's
    own state, so an added field carrying file contents fails this."""
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nusers:\n- user:\n    token: SECRET-TOKEN\n")
    access = _access(_Scripted({}), kubeconfig=kubeconfig)

    for value in vars(access).values():
        assert "SECRET-TOKEN" not in str(value)


@pytest.mark.parametrize(
    "server,ca", [("https://wrong-cluster.invalid", CA_DATA), (ENDPOINT, "wrong-ca")]
)
def test_transport_refuses_wrong_kubeconfig_before_cluster_access(
    binding, provider_identity, observed_cluster, expected_target, server, ca
):
    from superplane_bootstrap.target import verify_target

    verified_target = verify_target(
        binding=binding,
        provider=provider_identity,
        observed=observed_cluster,
        cluster_ownership="adp-created",
        **expected_target,
    )
    runner = _Scripted(
        {
            "config view": {
                "clusters": [
                    {"cluster": {"server": server, "certificate-authority-data": ca}}
                ]
            }
        }
    )
    access = _access(runner)
    with pytest.raises(BootstrapRefused, match="endpoint or CA"):
        access.bind_target(verified_target, binding)
    assert len(runner.calls) == 1


def test_transport_pins_verified_snapshot_despite_source_replacement(
    tmp_path, binding, provider_identity, observed_cluster, expected_target
):
    from superplane_bootstrap.target import verify_target

    target = verify_target(
        binding=binding,
        provider=provider_identity,
        observed=observed_cluster,
        cluster_ownership="adp-created",
        **expected_target,
    )
    source = tmp_path / "source-config"
    source.write_text("original")
    runner = _Scripted(
        {
            "config view": {
                "clusters": [
                    {
                        "cluster": {
                            "server": ENDPOINT,
                            "certificate-authority-data": CA_DATA,
                        }
                    }
                ]
            },
            "get crd": {"items": []},
        }
    )
    access = _access(runner, kubeconfig=source)
    access.bind_target(target, binding)
    pinned = access._transport_path
    try:
        source.write_text("attacker replacement")
        access.custom_resource_definitions()
        command = runner.calls[-1][0]
        assert command[command.index("--kubeconfig") + 1] == str(pinned)
        assert (
            json.loads(pinned.read_text())["clusters"][0]["cluster"]["server"]
            == ENDPOINT
        )
        assert pinned.stat().st_mode & 0o777 == 0o600
        assert pinned.parent.stat().st_mode & 0o777 == 0o700
    finally:
        access.close()
    assert not pinned.exists()
    with pytest.raises(BootstrapRefused, match="transport is closed"):
        access.custom_resource_definitions()
