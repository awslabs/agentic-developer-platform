"""DevOps task sessions cannot widen the configured native-space authority."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.internal.l5_devops_policy import READ_ACTIONS, resource_policy, run_policy, setup_policy


@pytest.fixture
def selected_work():
    reference = {
        "tenant_id": "tenant-one",
        "project_id": "project-one",
        "source_connection_id": "connection-one",
        "record_type": "agent_space",
        "record_id": "space-one",
    }
    identity = {"account_id": "123456789012", "region": "us-east-1", "role_arn": "arn:aws:iam::123456789012:role/Selected"}
    binding = SimpleNamespace(
        service_id="devops",
        tenant_id="tenant-one",
        project_id="project-one",
        connection_ref={"source_connection_id": "connection-one"},
        native_resource_ref=reference,
        provider_identity=identity,
    )
    run = SimpleNamespace(
        service_id="devops",
        input={"capability": "investigation"},
        admission={"native_space_ref": reference.copy(), "provider_identity": identity.copy()},
    )
    return binding, run


@pytest.mark.parametrize("capability", ["investigation", "release_readiness", "release_verification"])
def test_run_policy_is_native_space_and_deadline_bounded(selected_work, capability):
    binding, run = selected_work
    run.input["capability"] = capability
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    policy = json.loads(run_policy(binding, run, deadline))
    assert policy == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Resource": "arn:aws:aidevops:us-east-1:123456789012:agentspace/space-one",
                "Action": ["aidevops:" + action for action in (*READ_ACTIONS, "CreateBacklogTask", "UpdateBacklogTask")],
                "Condition": {"DateLessThan": {"aws:CurrentTime": deadline.isoformat()}},
            },
            {
                "Effect": "Allow",
                "Action": "aidevops:GetAssociation",
                "Resource": "arn:aws:aidevops:us-east-1:123456789012:agentspace/space-one/association/*",
                "Condition": {"DateLessThan": {"aws:CurrentTime": deadline.isoformat()}},
            },
        ],
    }
    assert all("*" not in action and action.startswith("aidevops:") for action in policy["Statement"][0]["Action"])


@pytest.mark.parametrize("kind", ["submit", "reconcile", "poll", "collect", "cancel"])
def test_known_native_job_cannot_start_another_investigation(selected_work, kind):
    binding, run = selected_work
    run.native_job_ref = {"record_id": "execution-one", "record_revision": "task-one"}
    policy = json.loads(run_policy(binding, run, datetime.now(UTC) + timedelta(seconds=30), work_kind=kind))
    actions = policy["Statement"][0]["Action"]
    assert "aidevops:CreateBacklogTask" not in actions
    assert ("aidevops:UpdateBacklogTask" in actions) == (kind == "cancel")
    assert {"aidevops:GetBacklogTask", "aidevops:ListJournalRecords", "aidevops:GetRecommendation"}.issubset(actions)


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("reference", "tenant_id", "foreign"),
        ("reference", "project_id", "foreign"),
        ("reference", "source_connection_id", "foreign"),
        ("reference", "record_type", "native_job"),
        ("reference", "record_id", "*"),
        ("reference", "record_id", "space-one/*"),
        ("reference", "record_id", ""),
        ("reference", "record_id", None),
        ("identity", "account_id", "*"),
        ("identity", "account_id", "123"),
        ("identity", "region", "*"),
        ("identity", "role_arn", "arn:aws:iam::999999999999:role/Selected"),
        ("identity", "role_arn", "arn:aws:iam::123456789012:role/*"),
        ("identity", "role_arn", "arn:aws:iam::123456789012:root"),
    ],
)
def test_invalid_native_scope_cannot_produce_a_policy(selected_work, target, field, value):
    binding, run = selected_work
    if target == "reference":
        binding.native_resource_ref[field] = value
        run.admission["native_space_ref"] = binding.native_resource_ref.copy()
    else:
        binding.provider_identity[field] = value
        run.admission["provider_identity"] = binding.provider_identity.copy()
    with pytest.raises(HTTPException) as denied:
        run_policy(binding, run, datetime.now(UTC))
    assert denied.value.status_code == 409


@pytest.mark.parametrize("case", ["service", "binding_service", "capability", "space", "identity"])
def test_unowned_or_changed_admission_cannot_produce_a_policy(selected_work, case):
    binding, run = selected_work
    if case == "service":
        run.service_id = "security"
    elif case == "binding_service":
        binding.service_id = "security"
    elif case == "capability":
        run.input["capability"] = "native_ui"
    elif case == "space":
        run.admission["native_space_ref"]["record_id"] = "foreign-space"
    else:
        run.admission["provider_identity"]["account_id"] = "999999999999"
    with pytest.raises(HTTPException) as denied:
        run_policy(binding, run, datetime.now(UTC))
    assert denied.value.status_code == 409


@pytest.fixture
def selected_setup(selected_work):
    binding, _run = selected_work
    intent = SimpleNamespace(
        service_id="devops",
        tenant_id=binding.tenant_id,
        product_id="product-one",
        project_id=binding.project_id,
        operation_id="setup-one",
        request={"action": "create"},
    )
    verified = {
        **binding.provider_identity,
        "connection_ref": {**binding.native_resource_ref, "record_type": "connection", "record_id": "connection-one"},
    }
    return intent, verified, binding.native_resource_ref


def test_setup_creation_policy_requires_operation_ownership_and_deadline(selected_setup):
    from l5_services.devops.setup import setup_selection

    intent, verified, _reference = selected_setup
    deadline = datetime.now(UTC) + timedelta(seconds=45)
    policy = json.loads(setup_policy(intent, verified, deadline))
    create, observe, listing = policy["Statement"]
    fingerprint = setup_selection(intent, verified).fingerprint()
    assert create["Action"] == ["aidevops:CreateAgentSpace", "aidevops:TagResource"]
    assert create["Condition"]["StringEquals"]["aws:RequestTag/adp:scope"] == fingerprint
    assert create["Condition"]["ForAllValues:StringEquals"] == {"aws:TagKeys": ["adp:scope"]}
    assert observe["Action"] == ["aidevops:GetAgentSpace", "aidevops:ListGoals", "aidevops:UpdateGoal"]
    assert observe["Condition"]["StringEquals"]["aws:ResourceTag/adp:scope"] == fingerprint
    assert create["Resource"] == observe["Resource"] == "arn:aws:aidevops:us-east-1:123456789012:agentspace/*"
    assert listing["Action"] == ["aidevops:ListAgentSpaces"] and listing["Resource"] == "*"
    for statement in policy["Statement"]:
        assert statement["Condition"]["DateLessThan"] == {"aws:CurrentTime": deadline.isoformat()}
        assert statement["Condition"]["StringEquals"]["aws:RequestedRegion"] == "us-east-1"
    intent.operation_id = "another-operation"
    assert json.loads(setup_policy(intent, verified, deadline))["Statement"][0] != create


def test_adoption_policy_cannot_create_or_modify_native_resources(selected_setup):
    intent, verified, reference = selected_setup
    intent.request = {"action": "link", "native_resource_ref": reference}
    policy = json.loads(setup_policy(intent, verified, datetime.now(UTC)))
    assert len(policy["Statement"]) == 1
    statement = policy["Statement"][0]
    assert statement["Action"] == ["aidevops:GetAgentSpace", "aidevops:ListGoals"]
    assert statement["Resource"] == "arn:aws:aidevops:us-east-1:123456789012:agentspace/space-one"


def test_resource_verification_fits_sts_limit_with_full_length_names(selected_work):
    binding, _ = selected_work
    binding.product_id, binding.scope, binding.state = "product-one", {"product_id": "product-one"}, "ready"
    binding.native_resource_ref["record_id"] = "00000000-0000-0000-0000-000000000001"
    binding.connection_ref = {
        **binding.native_resource_ref,
        "record_type": "connection",
        "record_id": "connection-one",
        "record_revision": "version-one",
    }
    selection = {
        "account_id": "123456789012",
        "region": "us-east-1",
        "role_arn": "arn:aws:iam::123456789012:role/" + "r" * 64,
        "setup_role_arn": binding.provider_identity["role_arn"],
        "connection_ref": {**binding.connection_ref, "record_type": "credential"},
        "workload_kind": "lambda",
        "cluster_name": None,
        "namespace": None,
        "resource_arn": "arn:aws:lambda:us-east-1:123456789012:function:" + "f" * 64,
        "telemetry_arn": "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/" + "f" * 64 + ":*",
    }
    intent = SimpleNamespace(
        tenant_id=binding.tenant_id,
        product_id=binding.product_id,
        project_id=binding.project_id,
        service_id="devops",
        scope=binding.scope,
        request={"action": "check", "resource_selection": selection},
        authorization={"purpose": "devops_resource_verification"},
    )
    verified = {**binding.provider_identity, "connection_ref": binding.connection_ref, "resource_selection": selection}
    deadline = datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=UTC)
    policy_text = resource_policy(intent, verified, deadline, binding)
    assert len(policy_text) <= 2048
    statements = json.loads(policy_text)["Statement"]
    reads = statements[0]
    assert set(reads["Action"]) == {"aidevops:GetAgentSpace", "aidevops:ListGoals", "aidevops:ListAssociations", "aidevops:GetAssociation"}
    space = "arn:aws:aidevops:us-east-1:123456789012:agentspace/00000000-0000-0000-0000-000000000001"
    assert reads["Resource"] == [space, space + "/association/*"]
    assert statements[-1]["Action"] == ["lambda:GetFunction", "logs:FilterLogEvents"]
    assert statements[-1]["Resource"] == [selection["resource_arn"], selection["telemetry_arn"]]
    for statement in statements:
        assert statement["Condition"]["DateLessThan"] == {"aws:CurrentTime": deadline.isoformat()}
    assert statements[1]["Condition"]["StringEquals"]["aidevops:ServiceType"] == "aws"
    assert statements[2]["Condition"]["StringEquals"]["iam:PassedToService"] == "aidevops.amazonaws.com"


@pytest.mark.parametrize("boundary", ["foreign_project", "foreign_connection", "wildcard", "wrong_kind", "unsupported_cleanup", "other_service"])
def test_setup_refuses_widened_native_authority(selected_setup, boundary):
    intent, verified, reference = selected_setup
    intent.request = {"action": "link", "native_resource_ref": reference.copy()}
    if boundary == "other_service":
        intent.service_id = "security"
    elif boundary == "unsupported_cleanup":
        intent.request["action"] = "cleanup"
    else:
        field, value = {
            "foreign_project": ("project_id", "another-project"),
            "foreign_connection": ("source_connection_id", "another-connection"),
            "wildcard": ("record_id", "*"),
            "wrong_kind": ("record_type", "native_job"),
        }[boundary]
        intent.request["native_resource_ref"][field] = value
    with pytest.raises(HTTPException) as denied:
        setup_policy(intent, verified, datetime.now(UTC))
    assert denied.value.status_code == 409
