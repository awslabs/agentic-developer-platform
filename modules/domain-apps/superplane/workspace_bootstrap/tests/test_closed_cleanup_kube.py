"""The service transport cannot turn the observer into arbitrary Kubernetes access."""

import copy
import json
from types import SimpleNamespace

import pytest
from superplane_bootstrap import closed_cleanup_kube as transport
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.kube_grants import GENERATION_ANNOTATION, KubeGrants


@pytest.fixture
def scoped(monkeypatch):
    body = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {
            "name": "original",
            "namespace": "workspace",
            "annotations": {GENERATION_ANNOTATION: "generation"},
        },
        "rules": [{"verbs": ["get"], "resources": ["pods"], "apiGroups": [""]}],
    }
    observed = copy.deepcopy(body)
    observed["metadata"].update(uid="original-uid", resourceVersion="7")
    spec = {"body": body, "generation": "generation", "cluster_arn": "cluster"}
    identity = KubeGrants._identity(None, spec, observed)
    key = transport.ScopedKubernetes.key(body)
    monkeypatch.setattr(
        transport, "object_scope", lambda permit: ({key: body}, {key: identity})
    )
    monkeypatch.setattr(
        transport.ssl, "create_default_context", lambda **kw: "pinned-ca"
    )
    monkeypatch.setattr(
        transport,
        "token",
        lambda session, target: "reader" if session == "read" else "registrar",
    )
    calls, status = [], [200]

    class Connection:
        def __init__(self, host, port, **kw):
            assert (
                host == "test.eks.amazonaws.com"
                and port == 443
                and kw["context"] == "pinned-ca"
            )

        def request(self, method, path, **kw):
            calls.append((method, path, kw))

        def getresponse(self):
            return SimpleNamespace(
                status=status[0], read=lambda limit: json.dumps(observed).encode()
            )

        def close(self):
            pass

    kube = transport.ScopedKubernetes(
        {},
        SimpleNamespace(
            endpoint="https://test.eks.amazonaws.com",
            certificate_authority_data="Y2E=",
            cluster_arn="cluster",
        ),
        lambda: "read",
        lambda: "delete",
        lambda: None,
        connection_factory=Connection,
    )
    return kube, spec, identity, observed, calls, status


def test_delete_is_bound_to_uid_version_and_separate_session(scoped):
    kube, spec, identity, _, calls, _ = scoped
    kube.delete(spec, identity)
    assert [c[0] for c in calls] == ["GET", "DELETE"]
    assert calls[0][2]["headers"]["Authorization"] == "Bearer reader"
    assert calls[1][2]["headers"]["Authorization"] == "Bearer registrar"
    assert (
        calls[1][1]
        == "/apis/rbac.authorization.k8s.io/v1/namespaces/workspace/roles/original"
    )
    assert json.loads(calls[1][2]["body"])["preconditions"] == {
        "uid": "original-uid",
        "resourceVersion": "7",
    }


@pytest.mark.parametrize("change", ["uid", "rules", "finalizers", "resourceVersion"])
def test_delete_refuses_drift_and_finalizers(scoped, change):
    kube, spec, identity, observed, calls, _ = scoped
    if change == "rules":
        observed["rules"] = []
    elif change == "finalizers":
        observed["metadata"][change] = ["unknown-owner"]
    else:
        observed["metadata"][change] = "replacement" if change == "uid" else None
    with pytest.raises(BootstrapRefused):
        kube.delete(spec, identity)
    assert all(c[0] == "GET" for c in calls)


@pytest.mark.parametrize(
    "method,kind,name",
    [
        ("LIST", "Role", "original"),
        ("POST", "Role", "original"),
        ("GET", "Secret", "credentials"),
        ("GET", "Role", "../secrets"),
        ("DELETE", "Namespace", "workspace"),
    ],
)
def test_unlisted_reads_writes_and_url_traversal_never_reach_network(
    scoped, method, kind, name
):
    kube, spec, _, _, calls, _ = scoped
    body = copy.deepcopy(spec["body"])
    body["kind"], body["metadata"]["name"] = kind, name
    with pytest.raises(BootstrapRefused):
        kube.request(method, body)
    assert calls == []


@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 409, 500])
def test_errors_and_redirects_are_refused_without_server_body(scoped, status):
    kube, spec, _, _, calls, current = scoped
    current[0] = status
    with pytest.raises(BootstrapRefused) as caught:
        kube.observe(spec)
    assert "Bearer" not in str(caught.value) and "original-uid" not in str(caught.value)
    assert len(calls) == 1


def test_reader_propagation_probe_never_mutates(scoped, monkeypatch):
    kube = scoped[0]
    body = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "workspace"}}
    kube.reads[("v1", "Namespace", "", "workspace")] = body
    methods = []

    def request(method, selected):
        methods.append(method)
        assert selected == body
        if len(methods) == 1:
            raise BootstrapRefused("reader association still propagating")
        return body

    monkeypatch.setattr(kube, "request", request)
    monkeypatch.setattr(transport.time, "sleep", lambda _: None)
    kube.wait_ready()
    assert methods == ["GET", "GET"]


def second_read(scoped):
    kube, spec = scoped[:2]
    body = copy.deepcopy(spec["body"])
    body["metadata"]["name"] = "second-original"
    kube.reads[kube.key(body)] = body
    return body


def test_separate_roster_passes_have_fresh_checks_and_reader(scoped):
    kube, spec, _, _, calls, status = scoped
    second = second_read(scoped)
    status[0] = 404
    checks, readers = [], []
    kube.revalidate = lambda: checks.append(len(calls))
    kube.reader = lambda: readers.append(len(calls)) or "read"
    for _ in range(2):
        with kube.read_batch([spec["body"], second]):
            assert kube.request("GET", spec["body"]) is None
            assert kube.request("GET", second) is None
        assert kube._read_batch is None
    assert checks == [0, 0, 2, 2, 2, 4]
    assert readers == [0, 2]
    assert all(method == "GET" for method, _, _ in calls)
    # A later mutation uses its own ordinary fresh checks and registrar session.
    status[0] = 200
    kube.delete(spec, scoped[2])
    assert calls[-1][0] == "DELETE"
    assert calls[-1][2]["headers"]["Authorization"] == "Bearer registrar"
    assert len(checks) > 6


@pytest.mark.parametrize("fault", ["unknown", "duplicate", "changed", "empty"])
def test_invalid_batch_roster_refuses_before_any_authority_or_network(scoped, fault):
    kube, spec, _, _, calls, _ = scoped
    body = copy.deepcopy(spec["body"])
    if fault == "unknown":
        body["kind"] = "Secret"
    elif fault == "changed":
        body["rules"] = []
    bodies = (
        [] if fault == "empty" else [body, body] if fault == "duplicate" else [body]
    )
    kube.reader = lambda: pytest.fail("invalid roster reached reader")
    kube.revalidate = lambda: pytest.fail("invalid roster reached authority")
    with pytest.raises(BootstrapRefused), kube.read_batch(bodies):
        pytest.fail("invalid roster accepted")
    assert calls == [] and kube._read_batch is None


@pytest.mark.parametrize("fault", ["write", "outside-roster", "nested"])
def test_read_batch_cannot_mutate_expand_or_nest(scoped, fault):
    kube, spec, _, _, calls, _ = scoped
    second = second_read(scoped)
    with pytest.raises(BootstrapRefused):
        with kube.read_batch([spec["body"]]):
            if fault == "nested":
                with kube.read_batch([second]):
                    pytest.fail("nested batch accepted")
            elif fault == "write":
                kube.request(
                    "DELETE",
                    spec["body"],
                    deletion={"uid": "original-uid", "resourceVersion": "7"},
                )
            else:
                kube.request("GET", second)
    assert calls == [] and kube._read_batch is None


def test_revocation_during_batch_prevents_success_and_clears_reader(scoped):
    kube, spec, _, _, calls, _ = scoped

    def current():
        if calls:
            raise BootstrapRefused("source authority revoked")

    kube.revalidate = current
    with pytest.raises(BootstrapRefused, match="source authority revoked"):
        with kube.read_batch([spec["body"]]):
            kube.observe(spec)
    assert len(calls) == 1 and kube._read_batch is None
    with pytest.raises(BootstrapRefused):
        kube.delete(spec, scoped[2])
    assert len(calls) == 1


@pytest.mark.parametrize("fault", ["before-request", "after-request", "at-exit"])
def test_batch_has_finite_wall_time_and_never_reuses_expired_read_authority(
    scoped, monkeypatch, fault
):
    kube, spec, _, _, calls, _ = scoped
    clock = [0.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: clock[0])
    original_factory = kube.connection_factory

    def connection(*args, **kwargs):
        value = original_factory(*args, **kwargs)
        old_close = value.close

        def close():
            old_close()
            clock[0] = 31.0

        value.close = close
        return value

    if fault == "after-request":
        kube.connection_factory = connection
    with pytest.raises(BootstrapRefused):
        with kube.read_batch([spec["body"]]):
            if fault == "before-request":
                clock[0] = 31.0
            kube.observe(spec)
            clock[0] = 31.0
    assert len(calls) == (0 if fault == "before-request" else 1)
    assert kube._read_batch is None


def test_failed_read_still_revalidates_and_discards_batch(scoped):
    kube, spec, _, _, calls, status = scoped
    checks = []
    kube.revalidate = lambda: checks.append(len(calls))
    status[0] = 403
    with pytest.raises(BootstrapRefused):
        with kube.read_batch([spec["body"]]):
            kube.observe(spec)
    assert checks == [0, 0, 1] and kube._read_batch is None
