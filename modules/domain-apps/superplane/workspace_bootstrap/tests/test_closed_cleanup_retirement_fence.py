"""Exercise real permit scope and transport for the native dormant fence pair."""

import copy
import json
from types import SimpleNamespace

import pytest
from superplane_bootstrap import closed_cleanup_kube as transport
from superplane_bootstrap.closed_cleanup_permit import object_scope
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.kube_grants import KubeGrants
from superplane_bootstrap.retirement_fence import documents


@pytest.fixture
def retained():
    generation = "abcdef01" * 8
    name = "sp-bootstrap-" + generation[:24] + "-retirement"
    grants, progress = [], {"components": {}}
    live = {}
    for body, suffix in zip(documents(name, generation), ("policy", "binding")):
        spec = {
            "key": "retirement-fence-" + suffix,
            "kind": "kubernetes",
            "actor": "registrar",
            "cluster_arn": "cluster",
            "generation": generation,
            "body": copy.deepcopy(body),
            "lifetime": "workspace",
            "original_allocation_id": "original-allocation",
        }
        observed = copy.deepcopy(body)
        observed["metadata"].update(uid="original-" + suffix, resourceVersion="7")
        grants.append(spec)
        progress[spec["key"]] = {
            "phase": "granted",
            "identity": KubeGrants._identity(None, spec, observed),
        }
        live[body["kind"]] = observed
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": "workspace"},
    }
    grants.append(
        {
            "key": "namespace",
            "kind": "kubernetes",
            "body": namespace,
            "lifetime": "resource",
        }
    )
    component = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"namespace": "workspace", "name": "reader"},
    }
    return {
        "qualification": {
            "generation": generation,
            "cluster_arn": "cluster",
            "retained_plan": {"version": 1, "grants": grants},
            "retained_progress": progress,
            "inherited_components": {"reader": {"desired": component}},
        }
    }, live


def test_canonical_dormant_pair_is_deletable_but_components_are_retained(retained):
    permit, _ = retained
    reads, deletes = object_scope(permit)
    assert len(reads) == 4 and len(deletes) == 2
    assert {key[1] for key in deletes} == {
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
    }


@pytest.mark.parametrize(
    "change",
    [
        "active",
        "foreign-name",
        "generation",
        "cluster",
        "actor",
        "lifetime",
        "key",
        "allocation",
        "missing-allocation",
        "binding-target",
        "extra-spec",
        "extra-body",
        "missing-pair",
        "duplicate-pair",
        "arbitrary-admission",
    ],
)
def test_noncanonical_or_foreign_fence_scope_is_refused(retained, change):
    permit, _ = retained
    q = permit["qualification"]
    grants = q["retained_plan"]["grants"]
    policy, binding = grants[:2]
    if change == "active":
        policy["body"]["spec"]["validations"][0]["expression"] = "false"
    elif change == "foreign-name":
        policy["body"]["metadata"]["name"] = "foreign-retirement"
    elif change in {"generation", "actor", "lifetime"}:
        policy[change] = "foreign"
    elif change == "cluster":
        policy["cluster_arn"] = "foreign"
    elif change == "key":
        policy["key"] = "foreign"
        q["retained_progress"]["foreign"] = q["retained_progress"][
            "retirement-fence-policy"
        ]
    elif change == "allocation":
        policy["original_allocation_id"] = "foreign"
    elif change == "missing-allocation":
        policy.pop("original_allocation_id")
    elif change == "binding-target":
        binding["body"]["spec"]["policyName"] = "foreign"
    elif change == "extra-spec":
        policy["unrecognized-owner"] = "foreign"
    elif change == "extra-body":
        policy["body"]["metadata"]["labels"] = {"foreign": "true"}
    elif change == "missing-pair":
        grants.remove(binding)
    elif change == "duplicate-pair":
        grants.append(copy.deepcopy(binding))
    else:
        policy["body"]["kind"] = "MutatingWebhookConfiguration"
    with pytest.raises(BootstrapRefused):
        object_scope(permit)


@pytest.mark.parametrize("phase", ["adopted", "revoked"])
def test_completed_pair_member_is_not_deleted_again(retained, phase):
    permit, _ = retained
    permit["qualification"]["retained_progress"]["retirement-fence-policy"]["phase"] = (
        phase
    )
    _, deletes = object_scope(permit)
    assert [key[1] for key in deletes] == ["ValidatingAdmissionPolicyBinding"]


@pytest.fixture
def sealed(retained, monkeypatch):
    permit, live = retained
    monkeypatch.setattr(transport.ssl, "create_default_context", lambda **kw: "ca")
    monkeypatch.setattr(transport, "token", lambda session, target: session)
    calls = []

    class Connection:
        def __init__(self, host, port, **kw):
            assert host == "test.eks.amazonaws.com" and kw["context"] == "ca"

        def request(self, method, path, **kw):
            calls.append((method, path, kw))

        def getresponse(self):
            kind = (
                "ValidatingAdmissionPolicyBinding"
                if "/validatingadmissionpolicybindings/" in calls[-1][1]
                else "ValidatingAdmissionPolicy"
            )
            return SimpleNamespace(
                status=200, read=lambda limit: json.dumps(live[kind]).encode()
            )

        def close(self):
            pass

    kube = transport.ScopedKubernetes(
        permit,
        SimpleNamespace(
            endpoint="https://test.eks.amazonaws.com",
            certificate_authority_data="Y2E=",
            cluster_arn="cluster",
        ),
        lambda: "reader",
        lambda: "registrar",
        lambda: None,
        connection_factory=Connection,
    )
    return kube, permit["qualification"], live, calls


@pytest.mark.parametrize("index", [0, 1])
def test_exact_original_fence_delete_uses_native_preconditions(sealed, index):
    kube, q, _, calls = sealed
    spec = q["retained_plan"]["grants"][index]
    identity = q["retained_progress"][spec["key"]]["identity"]
    kube.delete(spec, identity)
    assert [call[0] for call in calls] == ["GET"] * (index + 1) + ["DELETE"]
    assert all(
        call[2]["headers"]["Authorization"] == "Bearer reader" for call in calls[:-1]
    )
    assert calls[-1][2]["headers"]["Authorization"] == "Bearer registrar"
    assert json.loads(calls[-1][2]["body"])["preconditions"] == {
        "uid": identity["uid"],
        "resourceVersion": "7",
    }


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize(
    "change", ["active-or-target", "generation", "uid", "resourceVersion", "finalizers"]
)
def test_live_activation_drift_and_replacement_refuse_before_delete(
    sealed, index, change
):
    kube, q, live, calls = sealed
    spec = q["retained_plan"]["grants"][index]
    body = live[spec["body"]["kind"]]
    if change == "active-or-target":
        if index == 0:
            body["spec"]["validations"][0]["expression"] = "false"
        else:
            body["spec"]["policyName"] = "foreign"
    elif change == "generation":
        body["metadata"]["annotations"]["superplane.aws-e/authority-generation"] = (
            "foreign"
        )
    else:
        body["metadata"][change] = {
            "uid": "replacement",
            "resourceVersion": None,
            "finalizers": ["owner"],
        }[change]
    with pytest.raises(BootstrapRefused):
        kube.delete(spec, q["retained_progress"][spec["key"]]["identity"])
    assert calls and all(call[0] == "GET" for call in calls)


@pytest.mark.parametrize("change", ["active", "uid", "generation", "finalizers"])
def test_unchanged_binding_cannot_remove_activated_or_changed_policy(sealed, change):
    kube, q, live, calls = sealed
    policy = live["ValidatingAdmissionPolicy"]
    if change == "active":
        policy["spec"]["validations"][0]["expression"] = "false"
    elif change == "generation":
        policy["metadata"]["annotations"]["superplane.aws-e/authority-generation"] = (
            "foreign"
        )
    else:
        policy["metadata"][change] = "replacement" if change == "uid" else ["owner"]
    spec = q["retained_plan"]["grants"][1]
    with pytest.raises(BootstrapRefused):
        kube.delete(spec, q["retained_progress"][spec["key"]]["identity"])
    assert [call[0] for call in calls] == ["GET"]


@pytest.mark.parametrize("kind", ["Namespace", "ServiceAccount"])
def test_retained_objects_cannot_be_deleted(sealed, kind):
    kube, _, _, calls = sealed
    body = next(body for key, body in kube.reads.items() if key[1] == kind)
    with pytest.raises(BootstrapRefused):
        kube.request(
            "DELETE", body, deletion={"uid": "anything", "resourceVersion": "7"}
        )
    assert calls == []
