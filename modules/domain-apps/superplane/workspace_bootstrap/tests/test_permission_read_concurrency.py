"""Permission batches retain the complete capability contract and join on refusal."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Barrier, Event, Lock, current_thread
from types import SimpleNamespace

import pytest

from superplane_bootstrap.authority_backend import BootstrapGrantBackend
from superplane_bootstrap.errors import BootstrapRefused


NAMESPACE = "synthetic-workspace"
SERVICE_ACCOUNT = "synthetic-controller"
ROLE = SERVICE_ACCOUNT + "-workspace"
CLUSTER_ROLE = SERVICE_ACCOUNT + "-" + NAMESPACE + "-cluster"
RBAC = ".rbac.authorization.k8s.io"
CRDS = ("nodepools.superplane.ai", "superplanenodes.superplane.ai")


def permission_contract(mode):
    """Independent expected requests, including the repeated supervisor secret check."""
    expected = [("installer", "get", "namespaces", None, NAMESPACE, True)]
    installed = [
        ("serviceaccounts", NAMESPACE, SERVICE_ACCOUNT),
        ("roles" + RBAC, NAMESPACE, ROLE),
        ("rolebindings" + RBAC, NAMESPACE, ROLE),
        ("clusterroles" + RBAC, None, CLUSTER_ROLE),
        ("clusterrolebindings" + RBAC, None, CLUSTER_ROLE),
        *[
            ("customresourcedefinitions.apiextensions.k8s.io", None, crd)
            for crd in CRDS
        ],
    ]
    if mode == "legacy":
        installed.append(("deployments.apps", NAMESPACE, SERVICE_ACCOUNT))
    for resource, namespace, name in installed:
        expected.extend(
            ("installer", verb, resource, namespace, selected, True)
            for verb, selected in (("create", None), ("get", name), ("patch", name))
        )
    for resource, namespace, name in (
        ("roles" + RBAC, NAMESPACE, ROLE),
        ("clusterroles" + RBAC, None, CLUSTER_ROLE),
    ):
        expected.extend(
            ("installer", verb, resource, namespace, name, True)
            for verb in ("bind", "escalate")
        )
    if mode == "management":
        expected.extend(
            ("installer", verb, "deployments.apps", NAMESPACE, name, False)
            for verb, name in (("create", None), ("patch", SERVICE_ACCOUNT))
        )
    for actor in ("installer", "supervisor"):
        expected.extend(
            (actor, verb, resource, namespace, name, False)
            for verb, resource, namespace, name in (
                ("patch", "namespaces", None, NAMESPACE),
                ("create", "namespaces", None, None),
                ("get", "secrets", NAMESPACE, None),
                ("create", "pods/exec", NAMESPACE, None),
                ("impersonate", "users", None, None),
                ("bind", "clusterroles" + RBAC, None, "cluster-admin"),
                ("escalate", "clusterroles" + RBAC, None, "cluster-admin"),
            )
        )
    expected.extend(
        ("supervisor", verb, resource, namespace, None, True)
        for verb, resource, namespace in (
            ("list", "clusterrolebindings" + RBAC, None),
            ("list", "clusterroles" + RBAC, None),
            ("create", "subjectaccessreviews.authorization.k8s.io", None),
            ("list", "rolebindings" + RBAC, NAMESPACE),
            ("list", "serviceaccounts", NAMESPACE),
            ("patch", "nodes", None),
        )
    )
    expected.extend(
        ("supervisor", verb, resource, NAMESPACE, None, False)
        for verb, resource in (
            ("create", "pods"),
            ("create", "roles" + RBAC),
            ("get", "secrets"),
            ("patch", "deployments.apps"),
        )
    )
    return expected


def backend_for(mode="management", *, probe=None, final=None):
    expected = permission_contract(mode)
    answers = {row[:-1]: row[-1] for row in expected}
    state = SimpleNamespace(
        owner=None, active=0, peak=0, threaded=0, observed=[], final_checks=0
    )
    lock = Lock()

    def binding():
        state.owner = current_thread()

    def read(actor, *, verb, resource, namespace=None, name=None):
        key = (actor, verb, resource, namespace, name)
        assert state.owner is not None
        assert key in answers, "unexpected permission request"
        with lock:
            state.active += 1
            state.peak = max(state.peak, state.active)
            state.observed.append(key)
            if current_thread() is not state.owner:
                state.threaded += 1
            ordinal = state.threaded
        try:
            if probe is not None:
                return probe(key, answers[key], ordinal, state.owner)
            return answers[key]
        finally:
            with lock:
                state.active -= 1

    def namespace(name):
        assert current_thread() is state.owner
        assert name == NAMESPACE
        assert state.observed[0] == expected[0][:-1]
        return object()

    def verified():
        assert current_thread() is state.owner
        assert state.active == 0
        state.final_checks += 1
        if final is not None:
            final()

    backend = BootstrapGrantBackend.__new__(BootstrapGrantBackend)
    backend.verify_worker_binding = binding
    backend.release = SimpleNamespace(
        namespace=NAMESPACE,
        service_account=SERVICE_ACCOUNT,
        controller=SERVICE_ACCOUNT,
        crds=CRDS,
    )
    backend.clients = SimpleNamespace(
        installer_access=SimpleNamespace(
            bootstrap_permission=lambda **kw: read("installer", **kw),
            namespace=namespace,
            controller_mode=mode,
        ),
        supervisor_access=SimpleNamespace(
            bootstrap_permission=lambda **kw: read("supervisor", **kw)
        ),
        target=SimpleNamespace(cluster_name="synthetic-cluster"),
        principals={"supervisor": "synthetic-supervisor"},
        eks=SimpleNamespace(
            list_associated_access_policies=lambda **kw: {
                "associatedAccessPolicies": []
            }
        ),
        verify=verified,
    )
    return backend, state, expected


@pytest.mark.parametrize("mode,count", [("management", 52), ("legacy", 53)])
def test_complete_permission_contract_overlaps_with_bounded_concurrency(mode, count):
    first_wave = Barrier(4)

    def probe(key, answer, ordinal, owner):
        if current_thread() is not owner and ordinal <= 4:
            first_wave.wait(timeout=5)
        return answer

    backend, state, expected = backend_for(mode, probe=probe)
    backend.verify_worker_permissions()
    assert len(expected) == count
    assert Counter(state.observed) == Counter(row[:-1] for row in expected)
    assert state.active == 0 and state.peak == 4
    assert state.threaded == count - 1
    assert state.final_checks == 1


@pytest.mark.parametrize(
    "changed", sorted(set(permission_contract("management")), key=str)
)
def test_every_required_allow_and_deny_remains_enforced(changed):
    def probe(key, answer, ordinal, owner):
        return not answer if key == changed[:-1] else answer

    backend, state, _ = backend_for(probe=probe)
    with pytest.raises(BootstrapRefused):
        backend.verify_worker_permissions()
    assert state.active == 0
    assert state.final_checks == 0


@pytest.mark.parametrize("answer", [None, 0, 1, "true", "false"])
@pytest.mark.parametrize("verb", ["get", "create"])
def test_non_boolean_permission_answers_cannot_satisfy_allow_or_deny(answer, verb):
    def probe(key, expected, ordinal, owner):
        if key == (
            "installer",
            verb,
            "namespaces",
            None,
            NAMESPACE if verb == "get" else None,
        ):
            return answer
        return expected

    backend, state, _ = backend_for(probe=probe)
    with pytest.raises(BootstrapRefused):
        backend.verify_worker_permissions()
    assert state.active == 0


@pytest.mark.parametrize("failure", ["denied", "transport"])
def test_failed_read_waits_for_every_started_read_before_returning(failure):
    blocked, release, failed = Event(), Event(), Event()

    def probe(key, answer, ordinal, owner):
        if key == ("installer", "get", "serviceaccounts", NAMESPACE, SERVICE_ACCOUNT):
            blocked.set()
            assert release.wait(timeout=5)
        if key == ("installer", "create", "serviceaccounts", NAMESPACE, None):
            assert blocked.wait(timeout=5)
            failed.set()
            if failure == "transport":
                raise OSError("synthetic transport failure")
            return False
        return answer

    backend, state, expected = backend_for(probe=probe)
    with ThreadPoolExecutor(max_workers=1) as caller:
        result = caller.submit(backend.verify_worker_permissions)
        try:
            assert failed.wait(timeout=5)
            with pytest.raises(TimeoutError):
                result.result(timeout=0.05)
            assert state.final_checks == 0
        finally:
            release.set()
        with pytest.raises(OSError if failure == "transport" else BootstrapRefused):
            result.result(timeout=5)
    assert Counter(state.observed) == Counter(row[:-1] for row in expected)
    assert state.active == 0


def test_final_identity_change_refuses_a_successful_permission_batch():
    def changed():
        raise BootstrapRefused("bootstrap identity changed")

    backend, state, expected = backend_for(final=changed)
    with pytest.raises(BootstrapRefused, match="identity changed"):
        backend.verify_worker_permissions()
    assert Counter(state.observed) == Counter(row[:-1] for row in expected)
    assert state.final_checks == 1


def test_initial_binding_failure_prevents_permission_reads():
    backend, state, _ = backend_for()

    def changed():
        raise BootstrapRefused("bootstrap binding changed")

    backend.verify_worker_binding = changed
    with pytest.raises(BootstrapRefused, match="binding changed"):
        backend.verify_worker_permissions()
    assert not state.observed
