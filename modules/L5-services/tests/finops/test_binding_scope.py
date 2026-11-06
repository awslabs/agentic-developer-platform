"""Adoption checks scoped billing authority without inventing native view attachment."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from l5_services.adapters.finops.binding import FinOpsBindingProvider


@pytest.fixture
def scope_case():
    account = "123456789012"
    view = f"arn:aws:billing::{account}:billingview/example-view"
    role = f"arn:aws:iam::{account}:role/ExampleData"
    identity = {"account_id": account, "role_arn": f"arn:aws:iam::{account}:role/Example", "region": "us-east-1"}
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    connection = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                  "source_connection_id": "connection-example", "record_id": "connection-example",
                  "record_type": "connection", "record_revision": "revision-1"}
    reference = {**connection, "record_type": "finops_agent", "record_id": "agent-example"}
    verified = {**identity, "scope": scope, "connection_ref": connection, "configuration_revision": 1,
                "finops": {"agent_mode": "adopt", "native_agent_ref": reference["record_id"],
                           "native_agent_display_name": "Example Agent", "data_role_ref": role,
                           "billing_scope": {"view_ref": view, "account_ids": [account]}}}
    native = {"state": "ready", "native_resource_ref": reference, "provider_identity": identity,
              "native_agent_display_name": "Example Agent", "data_role_ref": role}
    bridge = SimpleNamespace(read_agent=AsyncMock(return_value=native))
    view_response = {"billingView": {"arn": view, "ownerAccountId": account, "sourceAccountId": account,
        "billingViewType": "CUSTOM", "healthStatus": {"statusCode": "HEALTHY"},
        "dataFilterExpression": {"dimensions": {"key": "LINKED_ACCOUNT", "values": [account]}}}}
    billing = SimpleNamespace(get_billing_view=Mock(return_value=view_response))
    actions = ["ce:GetCostAndUsage", "ce:GetDimensionValues"]
    policy = {"Statement": [{"Effect": "Allow", "Action": actions, "Resource": view},
                             {"Effect": "Deny", "Action": "ce:*", "NotResource": view}]}
    simulation = {"EvaluationResults": [{"EvalActionName": action,
                  "EvalResourceName": "arn:aws:billing::${Account}:billingview/${BillingViewId}",
                  "EvalDecision": "allowed", "ResourceSpecificResults": [
                      {"EvalResourceName": view, "EvalResourceDecision": "allowed", "MissingContextValues": []}]
                  } for action in actions], "IsTruncated": False}
    iam = SimpleNamespace(get_role=Mock(return_value={"Role": {"Arn": role}}),
        list_role_policies=Mock(return_value={"PolicyNames": ["ScopedBilling"], "IsTruncated": False}),
        list_attached_role_policies=Mock(return_value={"AttachedPolicies": [], "IsTruncated": False}),
        get_role_policy=Mock(return_value={"PolicyDocument": policy}), simulate_principal_policy=Mock(return_value=simulation))
    archive = SimpleNamespace(bucket="example-private-bucket", client=SimpleNamespace(put_object=Mock(return_value={"VersionId": "version-1"})))
    provider = FinOpsBindingProvider(bridge, {"binding_generation": 1, "operation_id": "operation-example"}, billing, iam, archive)
    return provider, reference, verified, native, policy, simulation, view_response


async def test_adopt_supported_native_role_without_native_view_field(scope_case):
    provider, reference, verified, native, _, _, _ = scope_case
    assert "view_ref" not in native
    adopted = await provider.read(reference, verified)
    assert adopted["state"] == "ready"
    record = adopted["readiness"][0]["native_readback"]
    assert record["billing_scope"]["view_ref"] == verified["finops"]["billing_scope"]["view_ref"]
    assert record["billing_scope"]["snapshot_artifact"]["version"] == "version-1"
    provider.iam.simulate_principal_policy.assert_called_once()
    assert provider.iam.simulate_principal_policy.call_args.kwargs["PolicySourceArn"] == verified["finops"]["data_role_ref"]
    snapshot = json.loads(provider.archive.client.put_object.call_args.kwargs["Body"])
    assert snapshot["effective_permissions"] == provider.iam.simulate_principal_policy.return_value
    assert snapshot["billing_view"]["sourceAccountId"] == verified["account_id"]
    assert "product_verified" not in str(adopted)


@pytest.mark.parametrize("statement", [
    {"Effect": "Deny", "Action": "billing:*", "Resource": "*"},
    {"Effect": "Allow", "Action": "*", "Resource": "*"},
    {"Effect": "Allow", "Action": "ec2:DescribeInstances", "Resource": "*"},
    {"Effect": "Allow", "Action": "ce:*", "Resource": "selected"},
    {"Effect": "Allow", "Action": "ce:GetCostAndUsage", "Resource": "*"},
    {"Effect": "Allow", "Action": "ce:GetCostAndUsage", "Resource": "arn:aws:billing::123456789012:billingview/foreign"},
    {"Effect": "Allow", "NotAction": "billing:DeleteBillingView", "Resource": "selected"},
    {"Effect": "Allow", "Action": "sts:AssumeRole", "Resource": "selected"},
])
async def test_reject_denied_irrelevant_or_broader_role_policy(scope_case, statement):
    provider, reference, verified, _, policy, _, _ = scope_case
    policy["Statement"] = [{**statement, "Resource": verified["finops"]["billing_scope"]["view_ref"]
                             if statement.get("Resource") == "selected" else statement["Resource"]}]
    with pytest.raises(HTTPException) as failure:
        await provider.read(reference, verified)
    assert failure.value.detail["code"] == "billing_authority_unverified"
    provider.archive.client.put_object.assert_not_called()


@pytest.mark.parametrize("fault", ("deny", "missing", "context", "truncated", "boundary", "organization", "foreign", "duplicate",
                                 "missing_boundary", "malformed", "malformed_result", "api_denied", "resource_denied",
                                 "resource_absent", "resource_duplicate", "resource_malformed"))
async def test_reject_inconclusive_effective_permissions(scope_case, fault):
    provider, reference, verified, _, _, simulation, _ = scope_case
    if fault == "deny":
        simulation["EvaluationResults"][0]["EvalDecision"] = "explicitDeny"
    elif fault == "missing":
        simulation["EvaluationResults"].pop()
    elif fault == "context":
        simulation["EvaluationResults"][0]["ResourceSpecificResults"][0]["MissingContextValues"] = ["aws:PrincipalTag/example"]
    elif fault == "truncated":
        simulation["IsTruncated"] = True
    elif fault == "boundary":
        simulation["EvaluationResults"][0]["ResourceSpecificResults"][0]["PermissionsBoundaryDecisionDetail"] = {
            "AllowedByPermissionsBoundary": False}
    elif fault == "organization":
        simulation["EvaluationResults"][0]["OrganizationsDecisionDetail"] = {"AllowedByOrganizations": False}
    elif fault == "foreign":
        simulation["EvaluationResults"][0]["ResourceSpecificResults"][0]["EvalResourceName"] = "foreign-view"
    elif fault == "duplicate":
        simulation["EvaluationResults"][1] = simulation["EvaluationResults"][0]
    elif fault == "missing_boundary":
        provider.iam.get_role.return_value["Role"]["PermissionsBoundary"] = {"PermissionsBoundaryArn": "example-boundary"}
    elif fault == "malformed":
        provider.iam.simulate_principal_policy.return_value = None
    elif fault == "malformed_result":
        simulation["EvaluationResults"][0] = None
    elif fault == "resource_denied":
        simulation["EvaluationResults"][0]["ResourceSpecificResults"][0]["EvalResourceDecision"] = "explicitDeny"
    elif fault == "resource_absent":
        simulation["EvaluationResults"][0].pop("ResourceSpecificResults")
    elif fault == "resource_duplicate":
        simulation["EvaluationResults"][0]["ResourceSpecificResults"] *= 2
    elif fault == "resource_malformed":
        simulation["EvaluationResults"][0]["ResourceSpecificResults"] = [None]
    else:
        provider.iam.simulate_principal_policy.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Private role details"}}, "SimulatePrincipalPolicy")
    with pytest.raises(HTTPException) as failure:
        await provider.read(reference, verified)
    assert failure.value.detail["code"] == "billing_authority_unverified"
    provider.archive.client.put_object.assert_not_called()


@pytest.mark.parametrize("fault", ("absent", "conditional", "other_view", "missing_action", "unrecognized_pattern"))
async def test_reject_incomplete_outside_view_deny(scope_case, fault):
    provider, reference, verified, _, policy, _, _ = scope_case
    fence = policy["Statement"][1]
    if fault == "absent":
        policy["Statement"].pop()
    elif fault == "conditional":
        fence["Condition"] = {"StringEquals": {"aws:PrincipalTag/example": "restricted"}}
    elif fault == "other_view":
        fence["NotResource"] = [fence["NotResource"], "arn:aws:billing::123456789012:billingview/foreign"]
    elif fault == "missing_action":
        fence["Action"] = "ce:GetCostAndUsage"
    else:
        fence["Action"] = "ce:[gG]et*"
    with pytest.raises(HTTPException) as failure:
        await provider.read(reference, verified)
    assert failure.value.detail["code"] == "billing_authority_unverified"
    provider.iam.simulate_principal_policy.assert_not_called()
    provider.archive.client.put_object.assert_not_called()


@pytest.mark.parametrize("field", ("data_role_ref", "provider_identity", "native_agent_display_name"))
async def test_reject_missing_native_configuration_without_independent_scope_claim(scope_case, field):
    provider, reference, verified, native, _, _, _ = scope_case
    native.pop(field)
    with pytest.raises(HTTPException) as failure:
        await provider.read(reference, verified)
    assert failure.value.detail["code"] == "native_scope_unverified"
    provider.billing.get_billing_view.assert_not_called()
    provider.archive.client.put_object.assert_not_called()


@pytest.mark.parametrize("source_account", (None, "000000000000"))
async def test_reject_missing_or_foreign_billing_source_account(scope_case, source_account):
    provider, reference, verified, _, _, _, view_response = scope_case
    view_response["billingView"]["sourceAccountId"] = source_account
    with pytest.raises(HTTPException) as failure:
        await provider.read(reference, verified)
    assert failure.value.detail["code"] == "billing_scope_unverified"
    provider.archive.client.put_object.assert_not_called()
