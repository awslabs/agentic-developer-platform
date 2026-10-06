from types import SimpleNamespace

import pytest

from installation.config import Refusal
from installation.runtime_preparation import review

from .test_paid_worker import native

__all__ = ["native"]

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


def inspector(
    *,
    account=ACCOUNT,
    role="installation-operator",
    cluster="demo-cluster",
    queue_urls=None,
    roles=None,
):
    responses = {
        "sts": {
            "Account": account,
            "Arn": f"arn:aws:sts::{account}:assumed-role/{role}/session",
        },
        "eks": {
            "cluster": {
                "arn": f"arn:aws:eks:us-east-1:{account}:cluster/{cluster}",
                "status": "ACTIVE",
                "identity": {
                    "oidc": {
                        "issuer": "https://oidc.eks.us-east-1.amazonaws.com/id/example"
                    }
                },
            }
        },
        "sqs": {"QueueUrls": queue_urls or []},
        "iam": {"Roles": roles or []},
    }
    return SimpleNamespace(
        aws=lambda service, *_: responses[service], json=lambda value: value
    )


def test_review_has_exact_disabled_resources_and_no_authority():
    result = review(request(), inspector())
    assert result["status"] == "review-only"
    assert result["worker_ready"] is False
    assert result["resources"]["queue_name"] == "adp-demo-superplane-domain-operations"
    assert (
        result["resources"]["worker_role_arn"]
        != result["resources"]["observer_role_arn"]
    )
    assert "session" not in str(result)


@pytest.mark.parametrize(
    "change",
    [
        {"extra": "unexpected"},
        {"account_id": "not-an-account"},
        {"operator_role_arn": "arn:aws:iam::999999999999:role/other"},
        {"cluster": "not/cluster"},
    ],
)
def test_review_refuses_open_or_invalid_request(change):
    with pytest.raises(Refusal):
        review({**request(), **change}, inspector())


@pytest.mark.parametrize(
    "observation",
    [
        {"account": "999999999999"},
        {"role": "different-operator"},
        {"cluster": "other-cluster"},
        {
            "queue_urls": [
                "https://sqs.us-east-1.amazonaws.com/123456789012/adp-demo-superplane-domain-operations"
            ]
        },
        {"roles": [{"RoleName": "adp-demo-superplane-domain-worker"}]},
    ],
)
def test_review_refuses_identity_change_or_preexisting_name(observation):
    with pytest.raises(Refusal):
        review(request(), inspector(**observation))


@pytest.fixture
def contract_input(native):
    from installation import producer_role

    env, lock = native
    env["api_producer_role"] = {"api_id": "abcdefghij", "stage": "dev"}
    env["api_adapters"]["dispatcher"] = producer_role.dispatcher(env)
    env["api_adapters"]["vault"] = {"secret_key_ref": {"name": "selected-vault"}}
    env["api_adapters"]["verification"] = {}
    selected_request = {
        key: env[key]
        for key in ("account_id", "region", "environment", "cluster", "namespace")
    }
    selected_request["operator_role_arn"] = (
        f"arn:aws:iam::{env['account_id']}:role/installation-operator"
    )
    reviewed = review(
        selected_request, inspector(account=env["account_id"], cluster=env["cluster"])
    )
    worker = {
        key: value
        for key, value in env["paid_worker"].items()
        if key
        not in {
            "mode",
            "namespace",
            "role_arn",
            "queue_observer_role_arn",
            "queue_url",
            "queue_arn",
        }
    }
    operator = {
        "review_id": "change-001",
        "keda_operator_role_arn": f"arn:aws:iam::{env['account_id']}:role/existing-keda-operator",
        "producer_registry_id": "11111111-1111-1111-1111-111111111111",
        "worker_registry_id": "22222222-2222-2222-2222-222222222222",
        "database_secret_id": f"arn:aws:secretsmanager:{env['region']}:{env['account_id']}:secret:domain-db-abc123",
        "observation_credential_secret_id": f"arn:aws:secretsmanager:{env['region']}:{env['account_id']}:secret:observation-abc123",
        "observation_url": "https://observer.example.test/verify",
        "repo": "example/superplane",
        "policy_configmap": "superplane-lifecycle-policy",
        "state_claim": "superplane-lifecycle-state",
        "worker": worker,
    }
    return selected_request, reviewed, env, lock, operator


def test_contract_reuses_actual_worker_validator_and_fixed_gateway_binding(
    contract_input,
):
    from installation import paid_worker
    from installation.config import LABEL
    from installation.runtime_preparation import compose

    selected_request, reviewed, env, lock, operator = contract_input
    result = compose(selected_request, reviewed, env, lock, operator)
    assert result["status"] == "requires-shared-owner-review"
    assert result["binding_attested"] is False and result["worker_ready"] is False
    assert (
        result["gateway_binding_proposal"]["queue_url"]
        == reviewed["resources"]["queue_url"]
    )
    assert result["gateway_binding_proposal"]["worker_image_digests"] == [
        lock["images"][paid_worker.COMPONENT]
    ]
    assert (
        result["terraform_variables"]["installation_id"] == reviewed["installation_id"]
    )
    assert result["registry_proposals"][0]["credential_scopes"] == [
        "domain:operation-producer"
    ]
    assert result["registry_proposals"][1]["credential_scopes"] == [
        "domain:operation-executor",
        "domain:operation-recovery",
    ]
    projected = dict(env, paid_worker=result["paid_worker"])
    paid_worker.validate(projected, lock)
    documents = [{"metadata": {"labels": {LABEL: "owned"}}}]
    paid_worker.project(projected, lock, documents)
    scaled = next(doc for doc in documents if doc.get("kind") == "ScaledJob")
    assert scaled["metadata"]["annotations"]["autoscaling.keda.sh/paused"] == "true"
    assert scaled["spec"]["maxReplicaCount"] == 0


@pytest.mark.parametrize(
    "missing",
    ["vault", "verification", "dispatcher", "approver", "database", "policy", "state"],
)
def test_contract_refuses_missing_authority_or_dependency(contract_input, missing):
    from installation.runtime_preparation import compose

    selected_request, reviewed, env, lock, operator = contract_input
    if missing in ("vault", "verification", "dispatcher"):
        env["api_adapters"].pop(missing)
    elif missing == "approver":
        operator.pop("review_id")
    elif missing == "database":
        env.pop("database")
    elif missing == "policy":
        operator["policy_configmap"] = "unknown-policy"
    else:
        operator["state_claim"] = "unknown-state"
    with pytest.raises(Refusal):
        compose(selected_request, reviewed, env, lock, operator)


def test_contract_rejects_review_substitution_and_never_outputs_secrets(contract_input):
    from installation.runtime_preparation import compose

    selected_request, reviewed, env, lock, operator = contract_input
    reviewed["resources"]["queue_url"] = (
        "https://sqs.us-east-1.amazonaws.com/000000000000/foreign"
    )
    with pytest.raises(Refusal):
        compose(selected_request, reviewed, env, lock, operator)
    reviewed = review(
        selected_request, inspector(account=env["account_id"], cluster=env["cluster"])
    )
    result = compose(selected_request, reviewed, env, lock, operator)
    assert "private-token" not in str(result)
    assert result["schema_owner"] == "modules/harness/jobs/harness_jobs/schema.py"
    assert (
        result["gateway_binding_proposal"]["database_schema"]
        == result["paid_worker"]["operation_schema"]
    )


def test_gateway_binding_and_terraform_variable_shapes_match_actual_sources(
    contract_input,
):
    import ast
    import re
    from pathlib import Path

    from installation.runtime_preparation import compose

    selected_request, reviewed, env, lock, operator = contract_input
    result = compose(selected_request, reviewed, env, lock, operator)
    domain_apps = Path(__file__).resolve().parents[3]
    gateway = ast.parse(
        (
            domain_apps.parent / "gateway/src/internal/domain_operation_store.py"
        ).read_text()
    )
    binding = next(
        node
        for node in gateway.body
        if isinstance(node, ast.ClassDef) and node.name == "DomainBinding"
    )
    fields = {
        node.target.id for node in binding.body if isinstance(node, ast.AnnAssign)
    }
    assert set(result["gateway_binding_proposal"]) == fields
    terraform = (
        domain_apps / "superplane/infra/domain-runtime/variables.tf"
    ).read_text()
    variables = set(re.findall(r'^variable "([a-z_]+)"', terraform, re.MULTILINE))
    assert set(result["terraform_variables"]) == variables
    assert (domain_apps.parent / "harness/jobs/harness_jobs/schema.py").exists()
