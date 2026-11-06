"""Revocation reads join before owner-thread inventories and journal publication."""

from collections import Counter
from copy import deepcopy
from threading import Barrier, Lock, current_thread
from types import SimpleNamespace

import pytest

from superplane_bootstrap.authority_backend import BootstrapGrantBackend
from superplane_bootstrap.errors import BootstrapRefused


NAMESPACE = "synthetic-workspace"
PERMISSIONS = {
    ("list", "clusterrolebindings.rbac.authorization.k8s.io", None): True,
    ("list", "clusterroles.rbac.authorization.k8s.io", None): True,
    ("create", "subjectaccessreviews.authorization.k8s.io", None): True,
    ("list", "rolebindings.rbac.authorization.k8s.io", NAMESPACE): True,
    ("list", "serviceaccounts", NAMESPACE): True,
    ("patch", "nodes", None): True,
    ("create", "pods", NAMESPACE): False,
    ("create", "roles.rbac.authorization.k8s.io", NAMESPACE): False,
    ("get", "secrets", NAMESPACE): False,
    ("patch", "deployments.apps", NAMESPACE): False,
}


def backend_for(*, retained=True, fault=None):
    owner, lock = current_thread(), Lock()
    state = SimpleNamespace(
        fault=fault,
        active=0,
        peak=0,
        observed=[],
        verified=[],
        permissions=[],
        events=[],
        ordinal=Counter(),
    )
    barriers = {kind: Barrier(4) for kind in ("grant", "permission")}
    specs = [
        {
            "key": str(i),
            "kind": "kubernetes",
            "lifetime": "temporary" if i < 4 else "workspace",
        }
        for i in range(8)
    ]
    specs[2].update(kind="eks-entry", groups=["temporary-group"])
    specs[3]["kind"] = "eks-policy"
    specs += [
        {"key": "workspace-namespace", "kind": "kubernetes", "lifetime": "resource"},
        {"key": "adopted", "kind": "kubernetes", "lifetime": "temporary"},
        {"key": "unstarted", "kind": "kubernetes", "lifetime": "temporary"},
    ]
    plan = {"grants": specs}
    progress = {
        str(i): {
            "phase": "granted" if retained and i >= 4 else "revoked",
            "identity": {"uid": str(i)},
        }
        for i in range(8)
    }
    progress.update(
        retain_workspace=retained,
        adopted={"phase": "adopted"},
        **{"workspace-namespace": {"phase": "granted"}},
    )
    # A retained adopted workspace grant must still be freshly verified.
    progress["4"]["phase"] = "adopted"
    if fault == "phase":
        progress["0"]["phase"] = "revoke_intent"

    def owner_event(name):
        assert current_thread() is owner and state.active == 0
        state.events.append(name)

    def enter(kind):
        assert current_thread() is not owner
        with lock:
            state.active += 1
            state.peak = max(state.peak, state.active)
            state.ordinal[kind] += 1
            ordinal = state.ordinal[kind]
        if retained and ordinal <= 4:
            barriers[kind].wait(timeout=5)

    def leave():
        with lock:
            state.active -= 1

    def current(**kwargs):
        assert kwargs == {"recovery": True}
        owner_event("current")

    def observe(spec):
        enter("grant")
        try:
            state.observed.append(spec["key"])
            if spec["key"] == "0" and state.fault == "remaining":
                return {"uid": "not-removed"}
            if spec["key"] == "0" and state.fault == "unavailable":
                raise BootstrapRefused("grant read unavailable")
            if spec["lifetime"] == "workspace":
                return {
                    "uid": "changed"
                    if spec["key"] == "4" and state.fault == "identity"
                    else spec["key"]
                }
            return None
        finally:
            leave()

    def verify(spec, identity):
        assert current_thread() is not owner
        state.verified.append(spec["key"])
        if spec["key"] == "4" and state.fault == "grant-permission":
            raise BootstrapRefused("retained grant permissions changed")

    def entries(**kwargs):
        owner_event("inventory")
        return {"accessEntries": ["other-role"]}

    def entry(**kwargs):
        owner_event("entry")
        return {
            "accessEntry": {
                "principalArn": "other-role",
                "kubernetesGroups": ["temporary-group"]
                if state.fault == "group"
                else [],
            }
        }

    def cleanup(plan):
        owner_event("cleanup")
        if state.fault == "cleanup":
            raise BootstrapRefused("cleanup group mapped")

    def policies(**kwargs):
        owner_event("policies")
        return {
            "associatedAccessPolicies": ["unexpected"]
            if state.fault == "policy"
            else []
        }

    def permission(*, verb, resource, namespace):
        enter("permission")
        try:
            key = (verb, resource, namespace)
            state.permissions.append(key)
            if key == next(iter(PERMISSIONS)):
                if state.fault == "permission":
                    return False
                if state.fault == "unanswered":
                    return None
            return PERMISSIONS[key]
        finally:
            leave()

    backend = BootstrapGrantBackend.__new__(BootstrapGrantBackend)
    backend.clients = SimpleNamespace(
        verify=current,
        eks=SimpleNamespace(
            list_access_entries=entries,
            describe_access_entry=entry,
            list_associated_access_policies=policies,
        ),
        target=SimpleNamespace(cluster_name="synthetic"),
        principals={"supervisor": "supervisor"},
        supervisor_access=SimpleNamespace(bootstrap_permission=permission),
    )
    backend.release = SimpleNamespace(namespace=NAMESPACE)
    backend.observe, backend.verify, backend._verify_cleanup_unmapped = (
        observe,
        verify,
        cleanup,
    )
    return backend, plan, progress, state


@pytest.mark.parametrize(
    "fault",
    (
        None,
        "remaining",
        "unavailable",
        "identity",
        "grant-permission",
        "phase",
        "group",
        "cleanup",
        "policy",
        "permission",
        "unanswered",
    ),
)
def test_revocation_checks_are_bounded_joined_and_keep_owner_inventories(fault):
    backend, plan, progress, state = backend_for(fault=fault)
    original = deepcopy((plan, progress))
    published = False
    if fault:
        with pytest.raises(BootstrapRefused):
            backend.verify_revoked(plan, progress)
    else:
        backend.verify_revoked(plan, progress)
        assert state.active == 0
        published = True
    assert published is (fault is None)
    assert (plan, progress) == original
    assert backend.registrar_removed is True
    assert state.active == 0 and state.peak == 4
    assert Counter(state.observed) == Counter(str(i) for i in range(8))
    assert Counter(state.verified) == Counter(
        str(i) for i in range(5 if fault == "identity" else 4, 8)
    )
    grant_faults = {"remaining", "unavailable", "identity", "grant-permission", "phase"}
    if fault in grant_faults:
        assert state.events == ["current"]
    else:
        assert state.events[:3] == ["current", "inventory", "entry"]
    if fault is None or fault in {"permission", "unanswered"}:
        assert state.events == ["current", "inventory", "entry", "cleanup", "policies"]
        assert Counter(state.permissions) == Counter(PERMISSIONS.keys())
    else:
        assert not state.permissions


def test_failed_bootstrap_preserves_existing_absence_read_rules():
    backend, plan, progress, state = backend_for(retained=False)
    backend.verify_revoked(plan, progress)
    # The supervisor was removed too: Kubernetes absence was already checked
    # immediately after deletion. Only EKS remains independently observable.
    assert Counter(state.observed) == Counter(("2", "3"))
    assert not state.verified and not state.permissions
    assert state.events == ["current", "inventory", "entry"]


def test_revocation_does_not_reuse_previous_successful_reads():
    backend, plan, progress, state = backend_for()
    backend.verify_revoked(plan, progress)
    state.fault = "remaining"
    with pytest.raises(BootstrapRefused, match="authority remains"):
        backend.verify_revoked(plan, progress)
    assert Counter(state.observed) == Counter({str(i): 2 for i in range(8)})
    assert state.active == 0


def test_namespace_state_handling_stays_on_owner_even_for_unexpected_classification():
    backend, plan, progress, state = backend_for()
    spec = {"key": "workspace-namespace", "kind": "kubernetes", "lifetime": "workspace"}
    plan["grants"] = [spec]
    progress[spec["key"]] = {"identity": {"uid": "namespace"}}
    owner = current_thread()

    def observe(_):
        assert current_thread() is owner
        return {"uid": "namespace"}

    def verify(*args):
        assert current_thread() is owner

    backend.observe, backend.verify = observe, verify
    backend.verify_revoked(plan, progress)
    assert Counter(state.permissions) == Counter(PERMISSIONS.keys())
