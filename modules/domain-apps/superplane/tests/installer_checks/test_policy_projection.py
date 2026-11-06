import copy
from pathlib import Path

import pytest

from installation import lifecycle_worker
from installation.config import Refusal
from installation.projection_recovery import verifier_delta


@pytest.fixture
def policy():
    return {
        "kind": "NetworkPolicy",
        "metadata": {"name": "worker", "labels": {"owner": "installation"}},
        "spec": {
            "podSelector": {"matchLabels": {"app": "worker"}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [],
        },
    }


def test_api_omitted_empty_policy_rules_remain_deny_all(policy):
    actual = copy.deepcopy(policy)
    del actual["spec"]["ingress"]
    del actual["spec"]["egress"]
    before = copy.deepcopy(actual)
    assert lifecycle_worker.projection_matches(actual, policy)
    assert actual == before


@pytest.mark.parametrize("field", ["ingress", "egress"])
def test_added_rules_are_still_refused(policy, field):
    actual = copy.deepcopy(policy)
    actual["spec"][field] = [{}]
    assert not lifecycle_worker.projection_matches(actual, policy)


@pytest.mark.parametrize("field", ["ingress", "egress"])
def test_missing_nonempty_rules_are_still_refused(policy, field):
    policy["spec"][field] = [{"ports": [{"protocol": "TCP", "port": 443}]}]
    actual = copy.deepcopy(policy)
    del actual["spec"][field]
    assert not lifecycle_worker.projection_matches(actual, policy)


def test_active_egress_is_exact_while_empty_ingress_may_be_omitted(policy):
    policy["spec"]["egress"] = [{"to": [{"ipBlock": {"cidr": "10.0.0.1/32"}}]}]
    actual = copy.deepcopy(policy)
    del actual["spec"]["ingress"]
    assert lifecycle_worker.projection_matches(actual, policy)
    actual["spec"]["egress"][0]["to"][0]["ipBlock"]["cidr"] = "0.0.0.0/0"
    assert not lifecycle_worker.projection_matches(actual, policy)


@pytest.mark.parametrize("change", ["type", "owner", "selector", "policyTypes"])
def test_policy_identity_and_selection_still_match(policy, change):
    actual = copy.deepcopy(policy)
    del actual["spec"]["ingress"]
    if change == "type":
        actual["kind"] = "Other"
    elif change == "owner":
        actual["metadata"]["labels"]["owner"] = "foreign"
    elif change == "selector":
        actual["spec"]["podSelector"] = {}
    else:
        actual["spec"]["policyTypes"] = ["Ingress"]
    assert not lifecycle_worker.projection_matches(actual, policy)


def test_other_resource_lists_are_not_normalized():
    assert not lifecycle_worker.projection_matches(
        {"kind": "Other", "spec": {}}, {"kind": "Other", "spec": {"egress": []}}
    )


def test_omission_requires_explicit_policy_type(policy):
    policy["spec"]["policyTypes"] = ["Ingress"]
    actual = copy.deepcopy(policy)
    del actual["spec"]["egress"]
    assert not lifecycle_worker.projection_matches(actual, policy)


def original_source(current):
    import ast

    tree = ast.parse(current)
    tree.body = [
        n
        for n in tree.body
        if not (isinstance(n, ast.FunctionDef) and n.name == "projection_matches")
    ]
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "installed_snapshot":
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and child.id == "projection_matches":
                    child.id = "contains"
    return ast.unparse(tree)


def test_recovery_accepts_only_the_narrow_verifier_change():
    current = Path(lifecycle_worker.__file__).read_text()
    original = original_source(current)
    verifier_delta(original, current)
    with pytest.raises(Refusal, match="other worker behavior"):
        verifier_delta(
            original, current.replace('"hostNetwork"', '"changedHostNetwork"')
        )
    with pytest.raises(Refusal, match="Unexpected projection"):
        verifier_delta(
            original,
            current.replace(
                "and projection_matches(actual, desired)",
                "and projection_matches(actual, actual)",
            ),
        )
    with pytest.raises(Refusal, match="Exact projection helper"):
        verifier_delta(original, original)


@pytest.mark.parametrize(
    "change",
    [
        "release",
        "environment",
        "status",
        "lock",
        "cleanup",
        "route",
        "adapter",
        "volume",
        "token",
        "quiescence",
    ],
)
def test_recovery_review_refuses_changed_or_unrecovered_installation(
    monkeypatch, change
):
    from installation import projection_recovery

    environment = {"account_id": "123456789012"}
    release = {"source_revision": "a" * 40}
    previous = {
        "status": "failed",
        "remote_lock": None,
        "temporary_preflight": {"cleanup_required": False},
        "public_route_enabled": False,
        "adapter_stage": {"state": "disabled-restored"},
        "lifecycle_foundations": {"retained": True},
        "bootstrap": {"token_secret_removed": True},
        "completed": ["adapter-stage-quiescence"],
        "environment": copy.deepcopy(environment),
        "release_lock": copy.deepcopy(release),
        "run_id": "recorded",
        "installation_id": "original",
        "plan_sha256": "b" * 64,
    }
    monkeypatch.setattr(
        projection_recovery, "source_proof", lambda revision: {"source": revision}
    )
    assert (
        projection_recovery.review_inputs(environment, release, previous)["run_id"]
        == "recorded"
    )
    if change == "release":
        previous["release_lock"]["source_revision"] = "c" * 40
    elif change == "environment":
        previous["environment"]["account_id"] = "999999999999"
    elif change == "status":
        previous["status"] = "installed-and-verified"
    elif change == "lock":
        previous["remote_lock"] = {"etag": "retained"}
    elif change == "cleanup":
        previous["temporary_preflight"]["cleanup_required"] = True
    elif change == "route":
        previous["public_route_enabled"] = True
    elif change == "adapter":
        previous["adapter_stage"]["state"] = "active"
    elif change == "volume":
        previous["lifecycle_foundations"]["retained"] = False
    elif change == "token":
        previous["bootstrap"]["token_secret_removed"] = False
    elif change == "quiescence":
        previous["completed"] = []
    with pytest.raises(Refusal):
        projection_recovery.review_inputs(environment, release, previous)
