from types import SimpleNamespace

import pytest

from installation.config import Refusal
from installation.runtime_preparation import review

ACCOUNT = "123456789012"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/installation-operator"


def request():
    return {
        "account_id": ACCOUNT,
        "region": "us-east-1",
        "environment": "demo",
        "cluster": "demo-cluster",
        "namespace": "superplane",
        "operator_role_arn": ROLE,
    }


def inspector(*, account=ACCOUNT, role="installation-operator", cluster="demo-cluster", queue_urls=None, roles=None):
    responses = {
        "sts": {"Account": account, "Arn": f"arn:aws:sts::{account}:assumed-role/{role}/session"},
        "eks": {"cluster": {"arn": f"arn:aws:eks:us-east-1:{account}:cluster/{cluster}", "status": "ACTIVE", "identity": {"oidc": {"issuer": "https://oidc.eks.us-east-1.amazonaws.com/id/example"}}}},
        "sqs": {"QueueUrls": queue_urls or []},
        "iam": {"Roles": roles or []},
    }
    return SimpleNamespace(aws=lambda service, *_: responses[service], json=lambda value: value)


def test_review_has_exact_disabled_resources_and_no_authority():
    result = review(request(), inspector())
    assert result["status"] == "review-only"
    assert result["worker_ready"] is False
    assert result["resources"]["queue_name"] == "adp-demo-superplane-domain-operations"
    assert result["resources"]["worker_role_arn"] != result["resources"]["observer_role_arn"]
    assert "session" not in str(result)


@pytest.mark.parametrize("change", [
    {"extra": "unexpected"},
    {"account_id": "not-an-account"},
    {"operator_role_arn": "arn:aws:iam::999999999999:role/other"},
    {"cluster": "not/cluster"},
])
def test_review_refuses_open_or_invalid_request(change):
    with pytest.raises(Refusal):
        review({**request(), **change}, inspector())


@pytest.mark.parametrize("observation", [
    {"account": "999999999999"},
    {"role": "different-operator"},
    {"cluster": "other-cluster"},
    {"queue_urls": ["https://sqs.us-east-1.amazonaws.com/123456789012/adp-demo-superplane-domain-operations"]},
    {"roles": [{"RoleName": "adp-demo-superplane-domain-worker"}]},
])
def test_review_refuses_identity_change_or_preexisting_name(observation):
    with pytest.raises(Refusal):
        review(request(), inspector(**observation))
