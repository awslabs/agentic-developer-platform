"""Synthetic saved Terraform plans and AWS responses; never contact live services."""

import copy
import json
from types import SimpleNamespace

import pytest

from installation.config import Refusal
from installation.runtime_preparation import (
    WORKER_ROUTES,
    compose,
    inspect_plan,
    inspect_state,
    prepare,
)

from .test_paid_worker import native
from .test_runtime_preparation import contract_input

__all__ = ["contract_input", "native"]


def planned(contract_input):
    selected_request, reviewed, env, lock, operator = contract_input
    proposal = compose(selected_request, reviewed, env, lock, operator)
    variables = proposal["terraform_variables"]
    account, region, environment = (
        variables[key] for key in ("account_id", "region", "environment")
    )
    prefix = f"adp-{environment}-superplane-domain"
    issuer = variables["oidc_issuer"].removeprefix("https://")
    tag = {"adp.aws-e.io/installation": proposal["installation_id"]}
    role_values = {}
    role_values["worker"] = {
        "name": prefix + "-worker",
        "path": "/",
        "tags": tag,
        "assume_role_policy": json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "sts:AssumeRoleWithWebIdentity",
                        "Principal": {
                            "Federated": f"arn:aws:iam::{account}:oidc-provider/{issuer}"
                        },
                        "Condition": {
                            "StringEquals": {
                                issuer + ":aud": "sts.amazonaws.com",
                                issuer
                                + ":sub": f"system:serviceaccount:{variables['namespace']}:superplane-paid-worker",
                            }
                        },
                    }
                ],
            }
        ),
    }
    role_values["observer"] = {
        "name": prefix + "-observer",
        "path": "/",
        "tags": tag,
        "assume_role_policy": json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "sts:AssumeRole",
                        "Principal": {"AWS": variables["keda_operator_role_arn"]},
                    }
                ],
            }
        ),
    }
    policy_values = {}
    policy_values["worker"] = {
        "name": prefix + "-worker-invoke",
        "role": prefix + "-worker",
        "policy": json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "execute-api:Invoke",
                        "Resource": [
                            f"arn:aws:execute-api:{region}:{account}:{variables['api_id']}/{variables['api_stage']}/POST/internal/v1/controller-execution/{route}"
                            for route in WORKER_ROUTES
                        ],
                    }
                ],
            }
        ),
    }
    policy_values["observer"] = {
        "name": prefix + "-observer-attributes",
        "role": prefix + "-observer",
        "policy": json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "sqs:GetQueueAttributes",
                        "Resource": proposal["gateway_binding_proposal"][
                            "queue_url"
                        ].replace(
                            f"https://sqs.{region}.amazonaws.com/{account}/",
                            f"arn:aws:sqs:{region}:{account}:",
                        ),
                    }
                ],
            }
        ),
    }
    values = {
        "aws_sqs_queue.operations": {
            "name": prefix + "-operations",
            "tags": tag,
            "fifo_queue": False,
            "sqs_managed_sse_enabled": True,
            "visibility_timeout_seconds": 3600,
            "message_retention_seconds": 345600,
            "arn": f"arn:aws:sqs:{region}:{account}:{prefix}-operations",
            "url": f"https://sqs.{region}.amazonaws.com/{account}/{prefix}-operations",
        },
        **{f"aws_iam_role.{name}": value for name, value in role_values.items()},
        **{
            f"aws_iam_role_policy.{name}": value
            for name, value in policy_values.items()
        },
    }
    changes = [
        {
            "address": address,
            "change": {
                "actions": ["create"],
                "before": None,
                "after": copy.deepcopy(value),
                "after_unknown": {},
            },
        }
        for address, value in values.items()
    ]
    return (
        proposal,
        {
            "resource_changes": changes,
            "configuration": {
                "root_module": {
                    "resources": [
                        {
                            "address": f"aws_iam_role_policy.{name}",
                            "expressions": {
                                "role": {
                                    "references": [
                                        f"aws_iam_role.{name}.id",
                                        f"aws_iam_role.{name}",
                                    ]
                                }
                            },
                        }
                        for name in ("worker", "observer")
                    ]
                }
            },
        },
        values,
    )


class FakeInspector:
    def __init__(self, request, proposal, values):
        self.request, self.proposal, self.values = request, proposal, values
        self.applied = False

    def json(self, result):
        return result

    def aws(self, service, operation, *args):
        account, region = self.request["account_id"], self.request["region"]
        if service == "sts":
            return {
                "Account": account,
                "Arn": f"arn:aws:sts::{account}:assumed-role/installation-operator/session",
            }
        if service == "eks":
            return {
                "cluster": {
                    "arn": f"arn:aws:eks:{region}:{account}:cluster/{self.request['cluster']}",
                    "status": "ACTIVE",
                    "identity": {
                        "oidc": {
                            "issuer": self.proposal["terraform_variables"][
                                "oidc_issuer"
                            ]
                        }
                    },
                }
            }
        if service == "sqs":
            queue = self.values["aws_sqs_queue.operations"]
            if operation == "list-queues":
                return {"QueueUrls": [queue["url"]] if self.applied else []}
            if operation == "get-queue-attributes":
                return {
                    "Attributes": {
                        "QueueArn": queue["arn"],
                        "FifoQueue": "false",
                        "SqsManagedSseEnabled": "true",
                    }
                }
            if operation == "list-queue-tags":
                return {"Tags": queue["tags"]}
        if service == "iam":
            if operation == "list-roles":
                return {
                    "Roles": [
                        {"RoleName": self.values[f"aws_iam_role.{name}"]["name"]}
                        for name in ("worker", "observer")
                    ]
                    if self.applied
                    else []
                }
            role_name = "worker" if args[1].endswith("-worker") else "observer"
            role = self.values[f"aws_iam_role.{role_name}"]
            if operation == "get-role":
                return {
                    "Role": {
                        "Arn": role["arn"],
                        "RoleId": role["unique_id"],
                        "Tags": [
                            {"Key": key, "Value": value}
                            for key, value in role["tags"].items()
                        ],
                        "AssumeRolePolicyDocument": json.loads(
                            role["assume_role_policy"]
                        ),
                    }
                }
            if operation == "list-role-policies":
                return {
                    "PolicyNames": [
                        self.values[f"aws_iam_role_policy.{role_name}"]["name"]
                    ]
                }
            if operation == "list-attached-role-policies":
                return {"AttachedPolicies": []}
            if operation == "get-role-policy":
                return {
                    "PolicyDocument": json.loads(
                        self.values[f"aws_iam_role_policy.{role_name}"]["policy"]
                    )
                }
        raise AssertionError((service, operation, args))


class FakeTerraform:
    def __init__(self, plan, values, inspector, *, lose_response=False):
        self.plan, self.values, self.inspector = plan, values, inspector
        self.calls = []
        self.lose_response = lose_response

    def call(self, args, **_):
        self.calls.append(args)
        action = args[2]
        if action == "show" and args[-1] == "installation.tfplan":
            return SimpleNamespace(stdout=json.dumps(self.plan))
        if action == "show":
            state = {
                "values": {
                    "root_module": {
                        "resources": [
                            {
                                "address": address,
                                "mode": "managed",
                                "values": copy.deepcopy(value),
                            }
                            for address, value in self.values.items()
                        ]
                    }
                }
            }
            queue = self.values["aws_sqs_queue.operations"]
            worker = self.values["aws_iam_role.worker"]
            observer = self.values["aws_iam_role.observer"]
            state["values"]["outputs"] = {
                "resource_identity": {
                    "value": {
                        "queue_name": queue["name"],
                        "queue_arn": queue["arn"],
                        "queue_url": queue["url"],
                        "worker_role_arn": worker["arn"],
                        "worker_role_id": worker["unique_id"],
                        "observer_role_arn": observer["arn"],
                        "observer_role_id": observer["unique_id"],
                        "worker_ready": False,
                        "installation_id": self.inspector.proposal["installation_id"],
                    }
                }
            }
            return SimpleNamespace(stdout=json.dumps(state))
        if action == "apply":
            self.inspector.applied = True
            if self.lose_response:
                raise Refusal("Terraform response unavailable; reconcile before retry")
        return SimpleNamespace(stdout="")


@pytest.fixture
def execution(contract_input):
    selected_request, _reviewed, env, lock, operator = contract_input
    proposal, plan, values = planned(contract_input)
    inspector = FakeInspector(selected_request, proposal, values)
    terraform = FakeTerraform(plan, values, inspector)
    return (
        selected_request,
        env,
        lock,
        operator,
        proposal,
        plan,
        values,
        inspector,
        terraform,
    )


def test_exact_saved_plan_digest_and_separate_approval(execution, tmp_path):
    (
        selected_request,
        env,
        lock,
        operator,
        proposal,
        plan,
        _values,
        inspector,
        terraform,
    ) = execution
    receipt = prepare(
        selected_request, env, lock, operator, inspector, terraform, tmp_path
    )
    assert receipt["status"] == "planned" and not inspector.applied
    assert receipt["plan_sha256"] == inspect_plan(plan, proposal)
    assert (tmp_path / "runtime-preparation.json").stat().st_mode & 0o077 == 0
    assert "apply" not in [args[2] for args in terraform.calls]
    with pytest.raises(Refusal, match="separate authenticated plan approver"):
        prepare(
            selected_request,
            env,
            lock,
            operator,
            inspector,
            terraform,
            tmp_path,
            approved_plan_digest=receipt["plan_sha256"],
        )
    assert "apply" not in [args[2] for args in terraform.calls]


def test_lost_response_reconciles_from_unchanged_state_without_second_apply(
    execution, tmp_path
):
    (
        selected_request,
        env,
        lock,
        operator,
        _proposal,
        _plan,
        values,
        inspector,
        terraform,
    ) = execution
    for name in ("worker", "observer"):
        values[f"aws_iam_role.{name}"]["arn"] = (
            f"arn:aws:iam::{selected_request['account_id']}:role/{values[f'aws_iam_role.{name}']['name']}"
        )
        values[f"aws_iam_role.{name}"]["unique_id"] = "AROA" + name.upper()
    terraform.lose_response = True
    receipt = prepare(
        selected_request, env, lock, operator, inspector, terraform, tmp_path
    )
    approval = SimpleNamespace(
        verify_plan=lambda **data: {
            "approved": True,
            **data,
            "approver": "independent-operator",
        }
    )
    with pytest.raises(Refusal, match="reconcile"):
        prepare(
            selected_request,
            env,
            lock,
            operator,
            inspector,
            terraform,
            tmp_path,
            approved_plan_digest=receipt["plan_sha256"],
            approval_check=approval,
        )
    result = prepare(
        selected_request, env, lock, operator, inspector, terraform, tmp_path
    )
    assert result["status"] == "applied" and result["worker_ready"] is False
    assert result["applied_identity"]["worker_role_id"] == "AROAWORKER"
    assert [args[2] for args in terraform.calls].count("apply") == 1
    assert (
        prepare(selected_request, env, lock, operator, inspector, terraform, tmp_path)
        == result
    )


@pytest.mark.parametrize(
    "attack", ["destroy", "foreign", "admin", "trust", "unknown", "omit"]
)
def test_saved_plan_refuses_destructive_or_foreign_authority(execution, attack):
    (
        _selected_request,
        _env,
        _lock,
        _operator,
        proposal,
        plan,
        _values,
        _inspector,
        _terraform,
    ) = execution
    changed = copy.deepcopy(plan)
    changes = changed["resource_changes"]
    if attack == "destroy":
        changes[0]["change"]["actions"] = ["delete", "create"]
    elif attack == "foreign":
        changes.append(
            {"address": "aws_iam_role.foreign", "change": {"actions": ["create"]}}
        )
    elif attack == "admin":
        changes[-1]["change"]["after"]["policy"] = json.dumps(
            {"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
        )
    elif attack == "trust":
        changes[1]["change"]["after"]["assume_role_policy"] = "{}"
    elif attack == "unknown":
        changes[2]["change"]["after_unknown"]["assume_role_policy"] = True
    else:
        changes.pop()
    with pytest.raises(Refusal):
        inspect_plan(changed, proposal)


def test_replay_refuses_identity_change_and_unowned_state(execution, tmp_path):
    (
        selected_request,
        env,
        lock,
        operator,
        proposal,
        _plan,
        values,
        inspector,
        terraform,
    ) = execution
    receipt = prepare(
        selected_request, env, lock, operator, inspector, terraform, tmp_path
    )
    operator["review_id"] = "different-review"
    with pytest.raises(Refusal, match="immutable review"):
        prepare(selected_request, env, lock, operator, inspector, terraform, tmp_path)
    values["aws_sqs_queue.operations"]["tags"] = {}
    with pytest.raises(Refusal, match="ownership"):
        inspect_state(
            {
                "values": {
                    "root_module": {
                        "resources": [
                            {"address": address, "mode": "managed", "values": value}
                            for address, value in values.items()
                        ]
                    }
                }
            },
            proposal,
            inspector,
        )
    assert receipt["status"] == "planned"


def test_ambiguous_lost_response_does_not_replay_apply(execution, tmp_path):
    (
        selected_request,
        env,
        lock,
        operator,
        _proposal,
        _plan,
        _values,
        inspector,
        terraform,
    ) = execution
    receipt = prepare(
        selected_request, env, lock, operator, inspector, terraform, tmp_path
    )

    def uncertain(args, **_):
        if args[2] == "apply":
            terraform.calls.append(args)
            raise Refusal("Terraform apply response lost")
        return original(args)

    original = terraform.call
    terraform.call = uncertain
    approval = SimpleNamespace(
        verify_plan=lambda **data: {
            "approved": True,
            **data,
            "approver": "independent-operator",
        }
    )
    with pytest.raises(Refusal, match="response lost"):
        prepare(
            selected_request,
            env,
            lock,
            operator,
            inspector,
            terraform,
            tmp_path,
            approved_plan_digest=receipt["plan_sha256"],
            approval_check=approval,
        )
    with pytest.raises(Refusal):
        prepare(selected_request, env, lock, operator, inspector, terraform, tmp_path)
    assert [args[2] for args in terraform.calls].count("apply") == 1


def test_saved_receipt_refuses_fabricated_readiness_and_source_mismatch(
    execution, tmp_path
):
    (
        selected_request,
        env,
        lock,
        operator,
        _proposal,
        _plan,
        _values,
        inspector,
        terraform,
    ) = execution
    prepare(selected_request, env, lock, operator, inspector, terraform, tmp_path)
    receipt_file = tmp_path / "runtime-preparation.json"
    saved = json.loads(receipt_file.read_text())
    saved["worker_ready"] = True
    receipt_file.write_text(json.dumps(saved))
    with pytest.raises(Refusal, match="unreviewed readiness"):
        prepare(selected_request, env, lock, operator, inspector, terraform, tmp_path)
    saved["worker_ready"] = False
    saved["source_sha256"] = "0" * 64
    receipt_file.write_text(json.dumps(saved))
    with pytest.raises(Refusal, match="source changed"):
        prepare(selected_request, env, lock, operator, inspector, terraform, tmp_path)
    assert [args[2] for args in terraform.calls].count("apply") == 0


def test_wrong_digest_or_self_approval_never_applies(execution, tmp_path):
    (
        selected_request,
        env,
        lock,
        operator,
        _proposal,
        _plan,
        _values,
        inspector,
        terraform,
    ) = execution
    receipt = prepare(
        selected_request, env, lock, operator, inspector, terraform, tmp_path
    )
    fake_approval = SimpleNamespace(
        verify_plan=lambda **data: {
            "approved": True,
            **data,
            "approver": selected_request["operator_role_arn"],
        }
    )
    with pytest.raises(Refusal, match="digest"):
        prepare(
            selected_request,
            env,
            lock,
            operator,
            inspector,
            terraform,
            tmp_path,
            approved_plan_digest="0" * 64,
            approval_check=fake_approval,
        )
    with pytest.raises(Refusal, match="Separate authenticated plan approver"):
        prepare(
            selected_request,
            env,
            lock,
            operator,
            inspector,
            terraform,
            tmp_path,
            approved_plan_digest=receipt["plan_sha256"],
            approval_check=fake_approval,
        )
    assert [args[2] for args in terraform.calls].count("apply") == 0


def test_replay_refuses_changed_live_role_id(execution, tmp_path):
    (
        selected_request,
        _env,
        _lock,
        _operator,
        proposal,
        _plan,
        values,
        inspector,
        _terraform,
    ) = execution
    for name in ("worker", "observer"):
        role = values[f"aws_iam_role.{name}"]
        role["arn"] = (
            f"arn:aws:iam::{selected_request['account_id']}:role/{role['name']}"
        )
        role["unique_id"] = "AROA" + name.upper()
    inspector.applied = True
    original = inspector.aws

    def replaced(service, operation, *args):
        value = original(service, operation, *args)
        if service == "iam" and operation == "get-role" and args[1].endswith("-worker"):
            value["Role"]["RoleId"] = "AROA_RECREATED"
        return value

    inspector.aws = replaced
    with pytest.raises(Refusal, match="live IAM role identity changed"):
        inspect_state(
            {
                "values": {
                    "root_module": {
                        "resources": [
                            {"address": address, "mode": "managed", "values": value}
                            for address, value in values.items()
                        ]
                    }
                }
            },
            proposal,
            inspector,
        )
