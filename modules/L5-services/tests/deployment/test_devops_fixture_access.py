"""Offline preparation must never widen the dedicated fixture connection."""

import importlib.util
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("fixture_access", ROOT / "modules/L5-services/scripts/prepare-devops-fixture-access.py")
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)


@pytest.fixture
def selected():
    scope = {"tenant_id": "fixture-tenant", "product_id": "fixture-product", "project_id": "fixture-project"}
    intent = {**scope, "service_id": "devops", "operation_id": "00000000-0000-0000-0000-000000000001", "scope": scope,
              "configuration_id": "00000000-0000-0000-0000-000000000002", "binding_generation": 1,
              "request": {"action": "create", "configuration_revision": 1},
              "authorization": {"purpose": "devops_setup", "control_action": "devops_setup"}}
    intent["intent_hash"] = access.digest({key: intent[key] for key in (
        "operation_id", "scope", "configuration_id", "request", "authorization", "binding_generation")})
    config = {"schema_version": 1, "stage": "setup", "account_id": "000000000000", "region": "us-east-1",
              "connection_role_arn": "arn:aws:iam::000000000000:role/fixture-connection",
              "expires_at": "2026-01-01T12:15:00+00:00", "setup_intent": intent}
    snapshot = {"role_arn": config["connection_role_arn"], "role_id": "AROAexample", "policy_name": "L5CompletionScopedAccess",
                "trust_policy": {"selected": "retained-exact-trust"}, "managed_policies": [], "permissions_boundary": None,
                "inline_policies": {"L5CompletionScopedAccess": deepcopy(access.INITIAL_POLICY)}}
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    return SimpleNamespace(config=config, snapshot=snapshot, now=now)


def setup(item):
    return access.prepare(item.config, item.snapshot, now=item.now)


def test_setup_replaces_deny_only_with_tagged_short_lived_creation_and_reads(selected):
    plan = setup(selected)
    assert plan["aws_mutations"] == 0 and plan["applied"] is False
    assert plan["before_policy"] == access.INITIAL_POLICY
    assert plan["trust_sha256"] == access.digest(selected.snapshot["trust_policy"])
    create, read, discover, deny = plan["after_policy"]["Statement"]
    assert create["Condition"]["StringEquals"]["aws:RequestTag/adp:scope"] == plan["scope_fingerprint"]
    assert read["Condition"]["StringEquals"]["aws:ResourceTag/adp:scope"] == plan["scope_fingerprint"]
    assert create["Resource"] == read["Resource"] == "arn:aws:aidevops:us-east-1:000000000000:agentspace/*"
    assert discover["Resource"] == "*" and discover["Action"] == ["aidevops:ListAgentSpaces"]
    assert create["Action"] == ["aidevops:CreateAgentSpace", "aidevops:TagResource"]
    assert create["Condition"]["ForAllValues:StringEquals"] == {"aws:TagKeys": ["adp:scope"]}
    assert deny["NotAction"] == sorted(["aidevops:CreateAgentSpace", "aidevops:TagResource", "aidevops:GetAgentSpace", "aidevops:ListAgentSpaces", "aidevops:ListGoals"])
    for statement in (create, read, discover):
        assert statement["Condition"]["DateLessThan"]["aws:CurrentTime"] == selected.config["expires_at"]


def test_setup_window_can_be_reviewed_again_without_new_operation_or_trust(selected):
    prior = setup(selected)
    selected.snapshot["inline_policies"][selected.snapshot["policy_name"]] = prior["after_policy"]
    selected.config["expires_at"] = "2026-01-01T12:30:00+00:00"
    next_plan = access.prepare(selected.config, selected.snapshot, previous_plan=prior, now=selected.now)
    assert next_plan["binding_id"] == prior["binding_id"] and next_plan["scope_fingerprint"] == prior["scope_fingerprint"]
    assert next_plan["trust_sha256"] == prior["trust_sha256"] and next_plan["before_policy"] == prior["after_policy"]


@pytest.mark.parametrize("fault", ["intent_hash", "foreign_scope", "wildcard_role", "wide_window", "expired", "extra_policy", "managed", "boundary", "different_deny"])
def test_setup_rejects_unretained_unbounded_or_changed_inputs(selected, fault):
    if fault == "intent_hash":
        selected.config["setup_intent"]["request"]["configuration_revision"] += 1
    elif fault == "foreign_scope":
        selected.config["setup_intent"]["tenant_id"] = "different"
    elif fault == "wildcard_role":
        selected.config["connection_role_arn"] = "arn:aws:iam::000000000000:role/*"
    elif fault == "wide_window":
        selected.config["expires_at"] = "2026-01-01T14:00:00+00:00"
    elif fault == "expired":
        selected.config["expires_at"] = "2026-01-01T11:59:00+00:00"
    elif fault == "extra_policy":
        selected.snapshot["inline_policies"]["other"] = {}
    elif fault == "managed":
        selected.snapshot["managed_policies"] = ["other"]
    elif fault == "boundary":
        selected.snapshot["permissions_boundary"] = "other"
    else:
        selected.snapshot["inline_policies"][selected.snapshot["policy_name"]]["Statement"][0]["Action"] = ["aidevops:*"]
    with pytest.raises(ValueError):
        setup(selected)


@pytest.fixture
def resources(selected):
    previous = setup(selected)
    selected.snapshot["inline_policies"][selected.snapshot["policy_name"]] = previous["after_policy"]
    name = "l5-investigation-0000000000000001"
    selected.config.update(stage="resources", fixture={
        "purpose": "synthetic-investigation", "synthetic": True, "fixture_id": "0000000000000001",
        "native_space_id": "actual-space", "account_id": "000000000000", "region": "us-east-1",
        "function_arn": f"arn:aws:lambda:us-east-1:000000000000:function:{name}",
        "telemetry_arn": f"arn:aws:logs:us-east-1:000000000000:log-group:/aws/lambda/{name}:*",
        "resource_role_arn": f"arn:aws:iam::000000000000:role/{name}-reader",
        "execution_role_arn": f"arn:aws:iam::000000000000:role/{name}"}, native_readback={
        "agentSpace": {"agentSpaceId": "actual-space", "name": "adp-devops-" + previous["scope_fingerprint"][:32]},
        "tags": {"adp:scope": previous["scope_fingerprint"]}})
    selected.previous = previous
    return selected


def resource_plan(item):
    return access.prepare(item.config, item.snapshot, previous_plan=item.previous, now=item.now)


def test_resource_stage_discards_create_and_pins_association_passrole_and_resource_reads(resources):
    plan = resource_plan(resources)
    statements = plan["after_policy"]["Statement"]
    allowed = {action for statement in statements if statement["Effect"] == "Allow" for action in statement["Action"]}
    assert allowed == {"aidevops:GetAgentSpace", "aidevops:ListGoals", "aidevops:ListAssociations", "aidevops:GetAssociation",
                       "aidevops:AssociateService", "iam:PassRole", "iam:GetRole", "iam:ListRolePolicies", "iam:GetRolePolicy",
                       "iam:ListAttachedRolePolicies", "iam:SimulatePrincipalPolicy", "lambda:GetFunction", "logs:FilterLogEvents"}
    assert all(statement["Resource"] != "*" for statement in statements if statement["Effect"] == "Allow")
    associate = next(item for item in statements if item.get("Action") == ["aidevops:AssociateService"])
    assert associate["Resource"] == "arn:aws:aidevops:us-east-1:000000000000:agentspace/actual-space"
    assert associate["Condition"]["StringEquals"]["aidevops:ServiceType"] == "aws"
    read = next(item for item in statements if item.get("Action") == ["aidevops:GetAssociation"])
    assert read["Resource"] == associate["Resource"] + "/association/*"
    assert read["Condition"] == statements[0]["Condition"]
    passing = next(item for item in statements if item.get("Action") == ["iam:PassRole"])
    assert passing["Resource"] == resources.config["fixture"]["resource_role_arn"]
    assert passing["Condition"]["StringEquals"] == {"iam:PassedToService": "aidevops.amazonaws.com"}
    assert plan["before_policy"] == resources.previous["after_policy"]


def test_resource_window_can_be_refreshed_from_its_exact_previous_plan(resources):
    prior = resource_plan(resources)
    resources.snapshot["inline_policies"][resources.snapshot["policy_name"]] = prior["after_policy"]
    resources.config["expires_at"] = "2026-01-01T12:30:00+00:00"
    next_plan = access.prepare(resources.config, resources.snapshot, previous_plan=prior, now=resources.now)
    assert next_plan["binding_id"] == prior["binding_id"] and next_plan["before_policy"] == prior["after_policy"]


@pytest.mark.parametrize("fault", ["snapshot_drift", "changed_trust", "different_role", "no_previous", "foreign_space", "foreign_tag", "broad_function", "broad_logs", "other_reader"])
def test_resource_stage_requires_same_role_setup_and_exact_actual_space(resources, fault):
    if fault == "snapshot_drift":
        resources.snapshot["inline_policies"] = {resources.snapshot["policy_name"]: access.INITIAL_POLICY}
    elif fault == "changed_trust":
        resources.snapshot["trust_policy"] = {"different": "trust"}
    elif fault == "different_role":
        resources.snapshot["role_id"] = "recreated-role"
    elif fault == "no_previous":
        resources.previous = None
    elif fault == "foreign_space":
        resources.config["native_readback"]["agentSpace"]["agentSpaceId"] = "foreign"
    elif fault == "foreign_tag":
        resources.config["native_readback"]["tags"]["adp:scope"] = "foreign"
    else:
        key = {"broad_function": "function_arn", "broad_logs": "telemetry_arn", "other_reader": "resource_role_arn"}[fault]
        resources.config["fixture"][key] = "*"
    with pytest.raises(ValueError):
        resource_plan(resources)


def test_actual_snapshot_is_read_only_and_rejects_partial_role_policy_listing(selected):
    iam = Mock()
    iam.get_role.return_value = {"Role": {"Arn": selected.snapshot["role_arn"], "RoleId": "AROAexample",
                                         "AssumeRolePolicyDocument": selected.snapshot["trust_policy"]}}
    iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}
    iam.list_role_policies.return_value = {"PolicyNames": [selected.snapshot["policy_name"]]}
    iam.get_role_policy.return_value = {"PolicyDocument": access.INITIAL_POLICY}
    assert access.snapshot_role(iam, role_arn=selected.snapshot["role_arn"], policy_name=selected.snapshot["policy_name"]) == selected.snapshot
    iam.put_role_policy.assert_not_called()
    iam.update_assume_role_policy.assert_not_called()
    iam.list_role_policies.return_value["IsTruncated"] = True
    with pytest.raises(ValueError):
        access.snapshot_role(iam, role_arn=selected.snapshot["role_arn"], policy_name=selected.snapshot["policy_name"])
