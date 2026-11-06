"""Offline state-ownership and retired-route safety checks; never load live state."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from modules.tools.agentcore.agentcore_tools import legacy_retired
from modules.tools.agentcore.infra.retained_plan import verify


ROOT = Path(__file__).resolve().parents[4]
INFRA = ROOT / "modules/tools/agentcore/infra"
ROLE = "arn:aws:iam::123456789012:role/fictional-existing-worker"
COUNTED = (
    "aws_api_gateway_resource.browser", "aws_api_gateway_method.browser",
    "aws_api_gateway_integration.browser", "aws_lambda_permission.browser",
    "aws_cloudwatch_log_group.browser", "aws_iam_role.browser_gateway",
    "aws_iam_role_policy.browser_gateway", "aws_lambda_function.browser",
    "aws_api_gateway_resource.code_interpreter", "aws_api_gateway_method.code_interpreter",
    "aws_api_gateway_integration.code_interpreter", "aws_lambda_permission.code_interpreter",
    "aws_api_gateway_resource.websearch", "aws_api_gateway_method.websearch",
    "aws_api_gateway_integration.websearch", "aws_lambda_permission.websearch",
)
WORKER = ("browser_worker", "code_interpreter_worker", "websearch_worker")
ADDRESSES = [f"{name}[0]" for name in COUNTED] + [
    f'aws_iam_role_policy.{name}[{json.dumps(ROLE)}]' for name in WORKER]
API_ADDRESSES = [address for address in ADDRESSES if address.startswith("aws_api_gateway_")]
LIVE_ADDRESSES = [address for address in ADDRESSES if address not in API_ADDRESSES]


def snapshot():
    return {"variables": {"retain_legacy_routes": {"value": True},
                          "rest_api_id": {"value": "fictional-api"},
                          "tools_parent_resource_id": {"value": "fictional-parent"},
                          "api_execution_arn": {"value": "arn:aws:execute-api:us-east-1:123456789012:fictional-api"},
                          "stage_name": {"value": "dev"},
                          "worker_role_arns": {"value": [ROLE]}},
            "prior_state": {"values": {"root_module": {"resources": [
                {"address": address, "values": {
                    "id": "fictional-existing", "rest_api_id": "fictional-api",
                    "parent_id": "fictional-parent",
                    "source_arn": "arn:aws:execute-api:us-east-1:123456789012:fictional-api/dev/POST/tools/" +
                                  address.split(".", 1)[1].split("[", 1)[0].replace("_", "-")}}
                for address in ADDRESSES]}}},
            "resource_changes": [{"address": address, "change": {"actions": ["no-op"]}}
                                 for address in ADDRESSES]}


def retired_snapshot():
    fixture = snapshot()
    fixture["variables"]["retain_legacy_api_routes"] = {"value": False}
    resources = fixture["prior_state"]["values"]["root_module"]["resources"]
    fixture["resource_drift"] = []
    for entry in resources:
        address = entry["address"]
        if address not in API_ADDRESSES:
            continue
        route = address.split(".", 1)[1].split("[", 1)[0]
        before = {**entry["values"], "id": f"fictional-{address}",
                  "path_part": route.replace("_", "-"), "http_method": "POST",
                  "resource_id": f"fictional-aws_api_gateway_resource.{route}[0]"}
        fixture["resource_drift"].append({"address": address, "change": {
            "actions": ["delete"], "before": before, "after": None}})
    fixture["prior_state"]["values"]["root_module"]["resources"] = [
        entry for entry in resources if entry["address"] in LIVE_ADDRESSES]
    fixture["resource_changes"] = [
        entry for entry in fixture["resource_changes"] if entry["address"] in LIVE_ADDRESSES]
    return fixture


def test_explicit_retirement_accepts_only_absent_api_routes_and_preserves_ten_live_resources():
    fixture = retired_snapshot()
    assert len(API_ADDRESSES) == 9
    assert len(LIVE_ADDRESSES) == 10
    verify(fixture)
    fixture["resource_drift"] = []
    verify(fixture)  # Subsequent releases after the state owner records absence.
    fixture["variables"]["retain_legacy_api_routes"] = {"value": True}
    with pytest.raises(ValueError, match="all existing historical state addresses"):
        verify(fixture)


def computed_refresh_snapshot():
    """Reduced, fictionalized shape of the 39116804709 post-retirement plan.

    Separately managed IAM policies were already updated in state, while the
    role's aggregate snapshot still described the previous policy documents.
    Browser's only refreshed attribute was metadata.resource_version.
    """
    fixture = retired_snapshot()
    fixture["resource_drift"] = []
    resources = fixture["prior_state"]["values"]["root_module"]["resources"]
    changes = fixture["resource_changes"]
    role = "fictional-shared-lambda"
    policies = [
        ("code_interpreter_provider", "fictional-interpreter", [
            "bedrock-agentcore:StartCodeInterpreterSession", "bedrock-agentcore:GetCodeInterpreterSession"]),
        ("service", "fictional-runtime", ["dynamodb:GetItem", "dynamodb:UpdateItem"]),
        ("websearch_gateway", "fictional-search", ["execute-api:Invoke"]),
    ]
    inline = []
    for resource, name, actions in policies:
        policy = json.dumps({"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": actions, "Resource": "arn:aws:fictional:scoped-resource"}]})
        values = {"id": role + ":" + name, "name": name, "name_prefix": "", "role": role, "policy": policy}
        address = f"aws_iam_role_policy.{resource}[0]"
        resources.append({"address": address, "values": copy.deepcopy(values)})
        changes.append({"address": address, "change": {
            "actions": ["no-op"], "before": copy.deepcopy(values), "after": copy.deepcopy(values),
            "after_unknown": {}}})
        inline.append({"name": name, "policy": policy})
    after = {"id": role, "name": role, "arn": "arn:aws:iam::123456789012:role/" + role,
             "assume_role_policy": json.dumps({"Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                              "Principal": {"Service": "lambda.amazonaws.com"}}]}),
             "permissions_boundary": "", "managed_policy_arns": [], "inline_policy": inline}
    before = copy.deepcopy(after)
    for entry in before["inline_policy"][:2]:
        policy = json.loads(entry["policy"])
        policy["Statement"][0]["Action"].pop()
        entry["policy"] = json.dumps(policy)
    browser = {"id": "fictional-agents/fictional-browser", "metadata": [{
        "name": "fictional-browser", "namespace": "fictional-agents", "uid": "fictional-stable-uid",
        "resource_version": "200", "generation": 7, "annotations": {}, "labels": {}}],
        "spec": [{"template": [{"spec": [{"container": [{"name": "browser", "image": "old@sha256:123"}]}]}]}]}
    old_browser = copy.deepcopy(browser)
    old_browser["metadata"][0]["resource_version"] = "100"
    for address, old, current in [("aws_iam_role.service[0]", before, after),
                                  ("kubernetes_deployment.browser[0]", old_browser, browser)]:
        resources.append({"address": address, "values": copy.deepcopy(current)})
        changes.append({"address": address, "change": {"actions": ["no-op"],
                        "before": copy.deepcopy(current), "after": copy.deepcopy(current), "after_unknown": {}}})
        fixture["resource_drift"].append({"address": address, "change": {
            "actions": ["update"], "before": old, "after": copy.deepcopy(current), "after_unknown": {}}})
    fixture["configuration"] = {"root_module": {"resources": [
        {"address": "aws_iam_role.service", "expressions": {"name": {}, "assume_role_policy": {}}}]}}
    return fixture


def resource_change(fixture, address):
    return next(entry["change"] for entry in fixture["resource_changes"] if entry["address"] == address)


def test_post_retirement_release_accepts_only_bound_computed_refresh_without_mutating_plan():
    fixture = computed_refresh_snapshot()
    browser = resource_change(fixture, "kubernetes_deployment.browser[0]")
    browser["actions"] = ["update"]
    browser["after"]["spec"][0]["template"][0]["spec"][0]["container"][0]["image"] = "new@sha256:456"
    original = copy.deepcopy(fixture)
    verify(fixture)
    assert fixture == original
    fixture["resource_drift"].extend(retired_snapshot()["resource_drift"])
    verify(fixture)  # Computed refresh cannot weaken the complete nine-route retirement check.
    fixture["resource_drift"].pop()
    with pytest.raises(ValueError, match="exactly nine"):
        verify(fixture)


@pytest.mark.parametrize("attribute,value", [
    ("assume_role_policy", '{"Statement":[]}'), ("permissions_boundary", "foreign-boundary"),
    ("managed_policy_arns", ["arn:aws:iam::aws:policy/AdministratorAccess"]),
    ("name", "foreign-role"), ("id", "foreign-role"), ("arn", "foreign-arn"),
])
def test_role_aggregate_refresh_does_not_allow_other_role_drift(attribute, value):
    fixture = computed_refresh_snapshot()
    fixture["resource_drift"][0]["change"]["before"][attribute] = value
    with pytest.raises(ValueError, match="identity, trust or permissions drift"):
        verify(fixture)


@pytest.mark.parametrize("corruption", [
    pytest.param(lambda f: f["resource_drift"][0]["change"]["after"]["inline_policy"].pop(), id="missing-inline-policy"),
    pytest.param(lambda f: f["resource_drift"][0]["change"]["before"]["inline_policy"].pop(), id="added-inline-policy"),
    pytest.param(lambda f: f["resource_drift"][0]["change"]["after"]["inline_policy"].append(
        copy.deepcopy(f["resource_drift"][0]["change"]["after"]["inline_policy"][0])), id="duplicate-inline-policy"),
    pytest.param(lambda f: f["resource_drift"][0]["change"]["after"]["inline_policy"][0].update(
        policy='{"Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}'), id="widened-inline-policy"),
    pytest.param(lambda f: f["resource_drift"][0]["change"]["after"]["inline_policy"][0].update(
        policy='{"Statement":[],"Statement":[]}'), id="duplicate-json-key"),
    pytest.param(lambda f: f["resource_drift"][0]["change"]["after"]["inline_policy"][0].update(policy=None), id="unknown-policy"),
    pytest.param(lambda f: f["resource_drift"][0]["change"].update(after_unknown={"inline_policy": True}), id="unknown-refresh"),
    pytest.param(lambda f: f["resource_drift"][0]["change"].update(actions=["delete", "create"]), id="role-replacement"),
    pytest.param(lambda f: resource_change(f, "aws_iam_role_policy.service[0]").update(actions=["update"]), id="planned-policy-update"),
    pytest.param(lambda f: resource_change(f, "aws_iam_role_policy.service[0]").update(actions=["delete"]), id="planned-policy-delete"),
    pytest.param(lambda f: resource_change(f, "aws_iam_role_policy.service[0]")["before"].update(policy='{"Statement":[]}'), id="policy-before-mismatch"),
    pytest.param(lambda f: resource_change(f, "aws_iam_role_policy.service[0]")["after"].update(role="other-role"), id="policy-role-mismatch"),
    pytest.param(lambda f: resource_change(f, "aws_iam_role_policy.service[0]")["after"].update(policy='{"Statement":[]}'), id="policy-after-mismatch"),
    pytest.param(lambda f: resource_change(f, "aws_iam_role_policy.service[0]").update(after_unknown={"policy": True}), id="unknown-managed-policy"),
    pytest.param(lambda f: f["resource_changes"].remove(next(e for e in f["resource_changes"]
        if e["address"] == "aws_iam_role_policy.service[0]")), id="missing-policy-change"),
    pytest.param(lambda f: f["prior_state"]["values"]["root_module"]["resources"].remove(next(e for e in
        f["prior_state"]["values"]["root_module"]["resources"] if e["address"] == "aws_iam_role_policy.service[0]")), id="missing-policy-state"),
    pytest.param(lambda f: f["prior_state"]["values"]["root_module"]["resources"].append({
        "address": "aws_iam_role_policy.foreign[0]", "values": {"role": "fictional-shared-lambda"}}), id="extra-policy-owner"),
    pytest.param(lambda f: f["resource_changes"].append({"address": "aws_iam_role_policy.foreign[0]", "change": {
        "actions": ["create"], "before": None, "after": {"role": "fictional-shared-lambda"}}}), id="new-unowned-policy"),
    pytest.param(lambda f: f["configuration"]["root_module"]["resources"][0]["expressions"].update(inline_policy={}), id="inline-management-enabled"),
    pytest.param(lambda f: f["resource_drift"].append(copy.deepcopy(f["resource_drift"][0])), id="duplicate-role-refresh"),
    pytest.param(lambda f: f["resource_drift"].append({"address": "aws_iam_role_policy.service[0]", "change": {
        "actions": ["update"], "before": {}, "after": {}}}), id="independent-policy-drift"),
    pytest.param(lambda f: f["resource_drift"].append({"address": "aws_s3_bucket.unrelated", "change": {
        "actions": ["update"], "before": {}, "after": {}}}), id="unrelated-resource-drift"),
])
def test_computed_refresh_rejects_missing_unmatched_unknown_or_changed_policies(corruption):
    fixture = computed_refresh_snapshot()
    corruption(fixture)
    with pytest.raises(ValueError):
        verify(fixture)


@pytest.mark.parametrize("attribute,value", [
    ("uid", "foreign-uid"), ("name", "foreign-browser"), ("namespace", "foreign-namespace"),
    ("generation", 8), ("annotations", {"unsafe": "change"}), ("resource_version", None),
])
def test_browser_refresh_is_limited_to_known_resource_version(attribute, value):
    fixture = computed_refresh_snapshot()
    fixture["resource_drift"][1]["change"]["before"]["metadata"][0][attribute] = value
    with pytest.raises(ValueError, match="Browser drift"):
        verify(fixture)


def test_browser_workload_or_identity_drift_cannot_hide_behind_version_refresh():
    for key, value in (("spec", []), ("id", "foreign-deployment")):
        fixture = computed_refresh_snapshot()
        fixture["resource_drift"][1]["change"]["before"][key] = value
        with pytest.raises(ValueError, match="Browser drift"):
            verify(fixture)
    fixture = computed_refresh_snapshot()
    resource_change(fixture, "kubernetes_deployment.browser[0]")["before"]["metadata"][0]["uid"] = "foreign"
    with pytest.raises(ValueError, match="bound current resource"):
        verify(fixture)


def test_computed_refresh_compares_json_types_and_policy_object_order_exactly():
    fixture = computed_refresh_snapshot()
    # Whitespace and object-key ordering are irrelevant, but JSON value types are not.
    role_change = fixture["resource_drift"][0]["change"]
    role_change["before"]["force_detach_policies"] = False
    role_change["after"]["force_detach_policies"] = 0
    with pytest.raises(ValueError, match="identity, trust or permissions drift"):
        verify(fixture)
    fixture = computed_refresh_snapshot()
    current = resource_change(fixture, "aws_iam_role.service[0]")
    prior = next(entry["values"] for entry in fixture["prior_state"]["values"]["root_module"]["resources"]
                 if entry["address"] == "aws_iam_role.service[0]")
    for snapshot_value in [fixture["resource_drift"][0]["change"]["after"], prior,
                           current["before"], current["after"]]:
        for value in snapshot_value["inline_policy"]:
            value["policy"] = json.dumps(json.loads(value["policy"]), sort_keys=True, indent=2)
    verify(fixture)


def test_unowned_permissions_are_rejected_even_when_all_refreshed_role_snapshots_agree():
    fixture = computed_refresh_snapshot()
    current = resource_change(fixture, "aws_iam_role.service[0]")
    prior = next(entry["values"] for entry in fixture["prior_state"]["values"]["root_module"]["resources"]
                 if entry["address"] == "aws_iam_role.service[0]")
    for value in [fixture["resource_drift"][0]["change"]["after"], prior, current["before"], current["after"]]:
        value["inline_policy"][0]["policy"] = '{"Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}'
    with pytest.raises(ValueError, match="differs from the unchanged managed policies"):
        verify(fixture)


def test_retirement_refuses_live_routes_even_with_noop_operations():
    fixture = snapshot()
    fixture["variables"]["retain_legacy_api_routes"] = {"value": False}
    with pytest.raises(ValueError, match="all existing historical state addresses"):
        verify(fixture)


@pytest.mark.parametrize("value", [None, "false", 0, []])
def test_retirement_switch_requires_boolean(value):
    fixture = retired_snapshot()
    fixture["variables"]["retain_legacy_api_routes"]["value"] = value
    with pytest.raises(ValueError, match="explicit boolean"):
        verify(fixture)


@pytest.mark.parametrize("corruption", [
    lambda data: data["resource_drift"].pop(),
    lambda data: data["resource_drift"].append(copy.deepcopy(data["resource_drift"][0])),
    lambda data: data["resource_drift"][0]["change"].update(actions=["update"]),
    lambda data: data["resource_drift"][0]["change"].update(actions=["delete", "create"]),
    lambda data: data["resource_drift"][0]["change"].update(after={"id": "still-present"}),
    lambda data: data["resource_drift"][0]["change"].pop("after"),
    lambda data: data["resource_drift"][0]["change"]["before"].update(id=""),
    lambda data: data["resource_drift"][0]["change"]["before"].update(rest_api_id="another-api"),
    lambda data: data["resource_drift"][0]["change"]["before"].update(parent_id="another-parent"),
    lambda data: data["resource_drift"][0]["change"]["before"].update(path_part="another-path"),
    lambda data: data["resource_drift"][1]["change"]["before"].update(resource_id="another-resource"),
    lambda data: data["resource_drift"][2]["change"]["before"].update(resource_id="another-resource"),
    lambda data: data["resource_drift"][1]["change"]["before"].update(http_method="ANY"),
    lambda data: data["resource_drift"].append({"address": "aws_dynamodb_table.operations[0]",
                                              "change": {"actions": ["delete"], "after": None}}),
    lambda data: data["resource_drift"].append(None),
    lambda data: data["variables"]["worker_role_arns"].update(value=[]),
    lambda data: data["variables"]["worker_role_arns"].update(value=["arn:aws:iam::123456789012:role/other"]),
])
def test_retirement_rejects_partial_wrong_identity_or_unrelated_drift(corruption):
    fixture = retired_snapshot()
    corruption(fixture)
    with pytest.raises(ValueError, match="Coordinator"):
        verify(fixture)


@pytest.mark.parametrize("address", API_ADDRESSES)
@pytest.mark.parametrize("actions", [["create"], ["update"], ["delete"], ["delete", "create"], ["no-op"]])
def test_retirement_rejects_any_planned_api_route_operation(address, actions):
    fixture = retired_snapshot()
    fixture["resource_changes"].append({"address": address, "change": {"actions": actions}})
    with pytest.raises(ValueError, match="no planned resource operation"):
        verify(fixture)


@pytest.mark.parametrize("address", LIVE_ADDRESSES)
def test_retirement_preserves_each_live_resource_and_rejects_its_drift(address):
    fixture = retired_snapshot()
    fixture["resource_drift"].append({"address": address, "change": {"actions": ["delete"], "after": None}})
    with pytest.raises(ValueError, match="no unrelated drift"):
        verify(fixture)
    fixture = retired_snapshot()
    resources = fixture["prior_state"]["values"]["root_module"]["resources"]
    resources[:] = [entry for entry in resources if entry["address"] != address]
    with pytest.raises(ValueError, match="all existing historical state addresses"):
        verify(fixture)


def test_all_nineteen_existing_state_addresses_are_owned_without_mutation():
    assert len(ADDRESSES) == 19
    resources = (INFRA / "retained-legacy.tf").read_text()
    for address in COUNTED:
        resource_type, resource_name = address.split(".")
        assert f'resource "{resource_type}" "{resource_name}"' in resources
    for name in WORKER:
        assert f'resource "aws_iam_role_policy" "{name}"' in resources
    assert resources.count("prevent_destroy = true") == len(ADDRESSES)
    assert 'default     = false' in (INFRA / "retained-legacy-variables.tf").read_text()
    verify(snapshot())


@pytest.mark.parametrize("corruption", [
    lambda data: data["prior_state"]["values"]["root_module"]["resources"].pop(),
    lambda data: data["variables"]["worker_role_arns"].update(value=["arn:aws:iam::123456789012:role/other"]),
    lambda data: data["resource_changes"][0]["change"].update(actions=["create"]),
    lambda data: data["resource_changes"][0]["change"].update(actions=["delete", "create"]),
    lambda data: data["resource_changes"][0]["change"].update(actions=["update"]),
    lambda data: data["variables"]["retain_legacy_routes"].update(value=False),
    lambda data: data["prior_state"]["values"]["root_module"]["resources"][0]["values"].update(id=""),
    lambda data: data["variables"]["tools_parent_resource_id"].update(value="other-parent"),
    lambda data: data["variables"]["api_execution_arn"].update(value="arn:aws:execute-api:us-east-1:123456789012:other"),
])
def test_retention_refuses_missing_identity_new_resource_or_switch_off(corruption):
    fixture = copy.deepcopy(snapshot())
    corruption(fixture)
    with pytest.raises(ValueError, match="Coordinator"):
        verify(fixture)


def test_policy_migration_requires_separate_scoped_owner():
    fixture = snapshot()
    for change in fixture["resource_changes"]:
        if change["address"].startswith("aws_iam_role_policy."):
            change["change"]["actions"] = ["update"]
            with pytest.raises(ValueError, match="scoped worker and legacy-role policy migration"):
                verify(fixture)
            change["change"]["actions"] = ["no-op"]
    fixture["resource_changes"][7]["change"]["actions"] = ["update"]
    verify(fixture)
    source = (INFRA / "retained-legacy.tf").read_text()
    for route in ("browser", "code-interpreter", "websearch"):
        assert f'POST/tools/{route}' in source
    for policy in WORKER:
        policy_source = source.split(f'resource "aws_iam_role_policy" "{policy}"', 1)[1].split("resource ", 1)[0]
        assert 'Effect = "Deny", Action = "execute-api:Invoke"' in policy_source
        assert 'Effect = "Allow"' not in policy_source
    assert 'POST/agent/tools/call' not in source
    assert "ADP_TOOLS_WORKER_ROLES" not in source


@pytest.mark.parametrize("event", [{"resource": "/tools/browser", "httpMethod": "POST"},
                                    {"resource": "/tools/browser", "body": "malformed"},
                                    {}, None])
def test_retained_browser_refuses_without_provider_or_queue_calls(monkeypatch, event):
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: pytest.fail("called provider"))
    assert legacy_retired.lambda_handler(event, None) == {"statusCode": 404, "body": "Not Found"}


def test_retention_plan_subprocess_refuses_missing_state():
    script = INFRA / "retained_plan.py"

    def check(plan):
        return subprocess.run([sys.executable, str(script), "/dev/stdin"], input=json.dumps(plan),
                              capture_output=True, text=True)

    accepted = check(snapshot())
    assert accepted.returncode == 0, accepted.stderr
    incomplete = snapshot()
    incomplete["prior_state"]["values"]["root_module"]["resources"].pop()
    refused = check(incomplete)
    assert refused.returncode != 0 and "retained routes require all existing" in refused.stderr


@pytest.mark.parametrize("address,attribute,unsafe", [
    ("aws_api_gateway_method.browser[0]", "authorization", "NONE"),
    ("aws_api_gateway_integration.websearch[0]", "uri", "https://foreign.example.invalid/invoke"),
    ("aws_iam_role.browser_gateway[0]", "assume_role_policy", json.dumps({"Statement": [{
        "Effect": "Allow", "Action": "sts:AssumeRole",
        "Principal": {"Service": "ecs-tasks.amazonaws.com"}}]})),
    ("aws_lambda_permission.code_interpreter[0]", "principal", "*"),
])
def test_refreshed_retained_security_drift_blocks_noop_apply(address, attribute, unsafe):
    fixture = snapshot()
    previous = next(entry["values"] for entry in fixture["prior_state"]["values"]["root_module"]["resources"]
                    if entry["address"] == address)
    before = {**previous, attribute: {
        "authorization": "AWS_IAM",
        "uri": "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/"
               "arn:aws:lambda:us-east-1:123456789012:function:fictional-tools-shared/invocations",
        "assume_role_policy": json.dumps({"Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                     "Principal": {"Service": "lambda.amazonaws.com"}}]}),
        "principal": "apigateway.amazonaws.com"}[attribute]}
    after = {**before, attribute: unsafe}
    fixture["resource_drift"] = [{"address": address, "change": {
        "actions": ["update"], "before": before, "after": after}}]
    assert next(entry for entry in fixture["resource_changes"] if entry["address"] == address)["change"]["actions"] == ["no-op"]
    with pytest.raises(ValueError, match="retained .* drift"):
        verify(fixture)


def test_retained_drift_fails_subprocess_while_unrelated_drift_does_not():
    script = INFRA / "retained_plan.py"
    fixture = snapshot()
    drift = {"address": "aws_api_gateway_method.browser[0]", "change": {
        "actions": ["update"], "before": {"authorization": "AWS_IAM"},
        "after": {"authorization": "NONE"}}}
    fixture["resource_drift"] = [drift]
    result = subprocess.run([sys.executable, str(script), "/dev/stdin"], input=json.dumps(fixture),
                            capture_output=True, text=True)
    assert result.returncode != 0 and "retained resource drift" in result.stderr
    drift["address"] = "aws_dynamodb_table.operations[0]"
    result = subprocess.run([sys.executable, str(script), "/dev/stdin"], input=json.dumps(fixture),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


BROWSER_ROLE = "fictional-browser-service"


def browser_role_refresh_snapshot():
    """Dev run 39747910838 (2026-10-22): the browser service role's aggregate snapshot
    lagged its separately managed ``browser-operations`` policy (ConditionCheckItem already
    in state, policy no-op), alongside the service role and Browser version refreshes."""
    fixture = computed_refresh_snapshot()
    resources = fixture["prior_state"]["values"]["root_module"]["resources"]
    changes = fixture["resource_changes"]
    actions = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem",
               "dynamodb:DeleteItem", "dynamodb:ConditionCheckItem"]
    policy = json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "execute-api:Invoke", "Resource": ["arn:aws:fictional:authority"]},
        {"Effect": "Allow", "Action": actions, "Resource": "arn:aws:fictional:browser-operations"}]})
    values = {"id": BROWSER_ROLE + ":browser-operations", "name": "browser-operations", "name_prefix": "",
              "role": BROWSER_ROLE, "policy": policy}
    resources.append({"address": "aws_iam_role_policy.browser_service[0]", "values": copy.deepcopy(values)})
    changes.append({"address": "aws_iam_role_policy.browser_service[0]", "change": {
        "actions": ["no-op"], "before": copy.deepcopy(values), "after": copy.deepcopy(values), "after_unknown": {}}})
    after = {"id": BROWSER_ROLE, "name": BROWSER_ROLE, "arn": "arn:aws:iam::123456789012:role/" + BROWSER_ROLE,
             "assume_role_policy": json.dumps({"Statement": [{"Effect": "Allow", "Action": "sts:AssumeRoleWithWebIdentity",
                                                              "Principal": {"Federated": "arn:aws:fictional:oidc"}}]}),
             "permissions_boundary": "", "managed_policy_arns": [],
             "inline_policy": [{"name": "browser-operations", "policy": policy}]}
    before = copy.deepcopy(after)
    stale = json.loads(before["inline_policy"][0]["policy"])
    stale["Statement"][1]["Action"].remove("dynamodb:ConditionCheckItem")
    before["inline_policy"][0]["policy"] = json.dumps(stale)
    resources.append({"address": "aws_iam_role.browser_service[0]", "values": copy.deepcopy(after)})
    changes.append({"address": "aws_iam_role.browser_service[0]", "change": {
        "actions": ["no-op"], "before": copy.deepcopy(after), "after": copy.deepcopy(after), "after_unknown": {}}})
    fixture["resource_drift"].append({"address": "aws_iam_role.browser_service[0]", "change": {
        "actions": ["update"], "before": before, "after": copy.deepcopy(after), "after_unknown": {}}})
    fixture["configuration"]["root_module"]["resources"].append(
        {"address": "aws_iam_role.browser_service", "expressions": {"name": {}, "assume_role_policy": {}}})
    return fixture


def browser_drift(fixture):
    return next(entry["change"] for entry in fixture["resource_drift"]
                if entry["address"] == "aws_iam_role.browser_service[0]")


def set_live_browser_role_policy(fixture, policy):
    """The live aggregate snapshot (drift after, prior state, planned no-op) all say ``policy``."""
    browser_drift(fixture)["after"]["inline_policy"][0]["policy"] = policy
    for entry in fixture["prior_state"]["values"]["root_module"]["resources"]:
        if entry["address"] == "aws_iam_role.browser_service[0]":
            entry["values"]["inline_policy"][0]["policy"] = policy
    change = resource_change(fixture, "aws_iam_role.browser_service[0]")
    for side in ("before", "after"):
        change[side]["inline_policy"][0]["policy"] = policy


def test_browser_service_role_snapshot_refresh_releases_the_image_updates():
    fixture = browser_role_refresh_snapshot()
    for address in ("aws_lambda_function.browser[0]",):
        resource_change(fixture, address).update(actions=["update"])
    browser = resource_change(fixture, "kubernetes_deployment.browser[0]")
    browser["actions"] = ["update"]
    browser["after"]["spec"][0]["template"][0]["spec"][0]["container"][0]["image"] = "new@sha256:456"
    original = copy.deepcopy(fixture)
    verify(fixture)
    assert fixture == original
    # Without the computed exception the same drift is "unrelated" to the nine-route retirement.
    fixture["resource_drift"].extend(retired_snapshot()["resource_drift"])
    verify(fixture)


@pytest.mark.parametrize("corruption,message", [
    pytest.param(lambda f: set_live_browser_role_policy(
        f, '{"Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}'),
        "snapshot differs from the unchanged managed policies", id="widened-live-policy"),
    pytest.param(lambda f: browser_drift(f)["after"]["inline_policy"][0].update(
        policy='{"Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}'),
        "differs from the bound current resource", id="drift-after-unbound-from-state"),
    pytest.param(lambda f: browser_drift(f)["after"]["inline_policy"].append(
        {"name": "extra", "policy": '{"Statement":[]}'}), "add or remove inline policies", id="added-inline-policy"),
    pytest.param(lambda f: browser_drift(f)["before"]["inline_policy"].pop(), "complete inline-policy set",
                 id="removed-inline-policy"),
    pytest.param(lambda f: browser_drift(f)["before"].update(name="foreign-role"),
                 "identity, trust or permissions drift", id="identity-drift"),
    pytest.param(lambda f: browser_drift(f)["before"].update(assume_role_policy='{"Statement":[]}'),
                 "identity, trust or permissions drift", id="trust-drift"),
    pytest.param(lambda f: resource_change(f, "aws_iam_role_policy.browser_service[0]").update(actions=["update"]),
                 "unchanged independently managed policies", id="planned-policy-update"),
    pytest.param(lambda f: f["prior_state"]["values"]["root_module"]["resources"].append(
        {"address": "aws_iam_role_policy.foreign[0]", "values": {"role": BROWSER_ROLE}}),
        "exact known policy owners", id="extra-policy-owner"),
    pytest.param(lambda f: browser_drift(f).update(after_unknown={"inline_policy": True}),
                 "one complete known update", id="unknown-refresh"),
    pytest.param(lambda f: resource_change(f, "aws_iam_role_policy.browser_service[0]")["before"].update(
        policy='{"Statement":[]}'), "differs from the bound current resource", id="managed-policy-mismatch"),
    pytest.param(lambda f: f["configuration"]["root_module"]["resources"][-1]["expressions"].update(inline_policy={}),
                 "remain separately managed", id="inline-policy-configured"),
])
def test_browser_service_role_refresh_keeps_every_strict_check(corruption, message):
    fixture = browser_role_refresh_snapshot()
    corruption(fixture)
    with pytest.raises(ValueError, match=message):
        verify(fixture)


def test_browser_service_role_live_policy_must_equal_the_managed_policy():
    fixture = browser_role_refresh_snapshot()
    live = json.loads(browser_drift(fixture)["after"]["inline_policy"][0]["policy"])
    live["Statement"][1]["Resource"] = "arn:aws:fictional:another-table"
    set_live_browser_role_policy(fixture, json.dumps(live))
    with pytest.raises(ValueError, match="snapshot differs from the unchanged managed policies"):
        verify(fixture)
