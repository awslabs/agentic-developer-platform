"""F18: missing worker authority must refuse before installation mutates."""

import json
import pytest
from superplane_bootstrap.errors import BootstrapRefused
from .conftest import FakeRegistrationStore
from .test_workspace import _access, _run
from .test_adapters import _Scripted, _access as production_access


@pytest.mark.parametrize("denied", ["create", "patch", "get"])
def test_missing_install_permission_refuses_before_namespace(
    binding, provider_identity, observed_cluster, expected_target, denied
):
    access = _access()
    access.bootstrap_permission = lambda **kw: kw["verb"] != denied
    result = _run(
        access,
        FakeRegistrationStore(),
        binding,
        provider_identity,
        observed_cluster,
        expected_target,
    )
    assert not result.ready
    assert "bootstrap permission" in str(result.refusal)
    assert not access.namespaces
    assert not access.installed_controllers


@pytest.mark.parametrize("answer", [None, "true", 1])
def test_unanswered_permission_is_not_authority(
    binding, provider_identity, observed_cluster, expected_target, answer
):
    access = _access()
    access.bootstrap_permission = lambda **kw: answer
    result = _run(
        access,
        FakeRegistrationStore(),
        binding,
        provider_identity,
        observed_cluster,
        expected_target,
    )
    assert not result.ready
    assert not access.namespaces


def test_missing_bind_and_rule_authority_refuses(
    binding, provider_identity, observed_cluster, expected_target
):
    access = _access()
    access.bootstrap_permission = lambda **kw: kw["verb"] not in {
        "bind",
        "escalate",
        "watch",
    }
    result = _run(
        access,
        FakeRegistrationStore(),
        binding,
        provider_identity,
        observed_cluster,
        expected_target,
    )
    assert not result.ready
    assert not access.namespaces


def test_ordinary_allows_do_not_prove_rbac_escalation_authority(
    binding, provider_identity, observed_cluster, expected_target
):
    access = _access()
    access.bootstrap_permission = lambda **kw: kw["verb"] not in {"bind", "escalate"}
    result = _run(
        access,
        FakeRegistrationStore(),
        binding,
        provider_identity,
        observed_cluster,
        expected_target,
    )
    assert not result.ready
    assert not access.namespaces


@pytest.mark.parametrize(
    "status", [{}, {"allowed": "true"}, {"allowed": True, "evaluationError": "unknown"}]
)
def test_production_unanswered_self_review_refuses(status):
    runner = _Scripted({"create -f -": {"status": status}})
    with pytest.raises(BootstrapRefused, match="bootstrap permission"):
        production_access(runner).bootstrap_permission(
            verb="create", resource="namespaces"
        )


def test_review_uses_actual_worker_and_exact_scope():
    runner = _Scripted({"create -f -": {"status": {"allowed": True}}})
    assert production_access(runner).bootstrap_permission(
        verb="bind",
        resource="roles.rbac.authorization.k8s.io",
        namespace="synthetic",
        name="worker-role",
    )
    body = json.loads(runner.calls[0][1])
    assert body["kind"] == "SelfSubjectAccessReview"
    assert body["spec"] == {
        "resourceAttributes": {
            "verb": "bind",
            "group": "rbac.authorization.k8s.io",
            "resource": "roles",
            "namespace": "synthetic",
            "name": "worker-role",
        }
    }
    assert all("--as" not in arg for arg in runner.calls[0][0])


@pytest.mark.parametrize(
    "verb,resource",
    [
        ("create", "clusterroles"),
        ("patch", "customresourcedefinitions"),
        ("bind", "clusterroles"),
        ("escalate", "roles"),
    ],
)
def test_missing_worker_permissions_refuse_before_component_installation(
    tmp_path, verb, resource
):
    from .test_integration import _FakeCluster, _run as run_production
    from .conftest import NAMESPACE

    class DeniedCluster(_FakeCluster):
        def _kubectl(self, argv, data):
            body = json.loads(data) if data and data.startswith("{") else {}
            if body.get("kind") == "SelfSubjectAccessReview":
                attrs = body["spec"]["resourceAttributes"]
                if (attrs["verb"], attrs["resource"]) == (verb, resource):
                    return self._ok(argv, {"status": {"allowed": False}})
            return super()._kubectl(argv, data)

    cluster = DeniedCluster()
    outcome, store = run_production(cluster, tmp_path)
    assert "bootstrap permission denied" in str(outcome.refusal)
    assert not outcome.ready
    assert (
        cluster.namespaces[NAMESPACE]["labels"]["pod-security.kubernetes.io/enforce"]
        == "restricted"
    )
    assert not cluster.crds
    assert not cluster.rbac_applied
    assert not cluster.authority_cloud.entries
    assert all(row["revoked"] for row in store.authority_rows.values())
    assert cluster.taints
    assert not store.rows
