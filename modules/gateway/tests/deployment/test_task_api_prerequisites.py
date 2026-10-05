"""M2M trigger and narrowly additive deployment preparation regressions."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import boto3

ROOT = Path(__file__).resolve().parents[2]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_registered_client_credentials_get_server_owned_service_claims(monkeypatch):
    resource = Mock()
    resource.Table.return_value.get_item.return_value = {"Item": {"org_id": "tenant-a", "agent_name": "task-pilot"}}
    monkeypatch.setattr(boto3, "resource", lambda *args, **kwargs: resource)
    trigger = load(ROOT / "infra/modules/cognito/lambda/pre_token_generation.py", "task_m2m_trigger")
    event = {
        "version": "3",
        "triggerSource": "TokenGeneration_ClientCredentials",
        "callerContext": {"clientId": "registered-client"},
        "request": {"scopes": ["adp-tasks/submit"], "clientMetadata": {"org_id": "attacker-tenant"}},
    }
    result = trigger.handler(event, None)
    claims = result["response"]["claimsAndScopeOverrideDetails"]["accessTokenGeneration"]["claimsToAddOrOverride"]
    assert claims["custom:org_id"] == "tenant-a"
    assert claims["custom:account_type"] == "service"
    assert claims["custom:client_id"] == "registered-client"
    resource.Table.return_value.get_item.assert_called_once_with(Key={"client_id": "registered-client"})
    assert result["request"]["scopes"] == ["adp-tasks/submit"]


def test_v3_preserves_human_refresh_claim_semantics(monkeypatch):
    monkeypatch.setattr(boto3, "resource", Mock())
    trigger = load(ROOT / "infra/modules/cognito/lambda/pre_token_generation.py", "task_human_trigger")
    result = trigger.handler(
        {
            "version": "3",
            "triggerSource": "TokenGeneration_RefreshTokens",
            "request": {"userAttributes": {"custom:org_id": "human-tenant", "custom:role": "member"}},
        },
        None,
    )
    claims = result["response"]["claimsAndScopeOverrideDetails"]["accessTokenGeneration"]["claimsToAddOrOverride"]
    assert claims == {"custom:org_id": "human-tenant", "custom:role": "member", "custom:account_type": "human"}


def test_v3_update_preserves_all_other_mutable_pool_settings():
    prepare = load(ROOT / "scripts/prepare-task-api-deployment.py", "task_prepare")
    pool = {
        "Id": "region_pool",
        "Name": "ignored-output-only",
        "MfaConfiguration": "OPTIONAL",
        "UserPoolTier": "ESSENTIALS",
        "LambdaConfig": {"PreSignUp": "signup-arn", "PreTokenGenerationConfig": {"LambdaArn": "existing-token-arn", "LambdaVersion": "V2_0"}},
        "Policies": {"PasswordPolicy": {"MinimumLength": 14}},
        "DeletionProtection": "ACTIVE",
    }
    allowed = set(pool) - {"Id", "Name"} | {"UserPoolId"}
    old = prepare.pool_update(pool, allowed, enable_m2m=False)
    new = prepare.pool_update(pool, allowed, enable_m2m=True)
    assert pool["LambdaConfig"]["PreTokenGenerationConfig"]["LambdaVersion"] == "V2_0"
    new["LambdaConfig"]["PreTokenGenerationConfig"]["LambdaVersion"] = "V2_0"
    assert new == old
    assert old["Policies"] == pool["Policies"]
    assert old["LambdaConfig"]["PreSignUp"] == "signup-arn"


def test_task_policy_has_no_scan_bucket_listing_or_tenant_delete():
    prepare = load(ROOT / "scripts/prepare-task-api-deployment.py", "task_prepare_policy")
    policy = prepare.render_policy(
        request_table_arn="arn:aws:dynamodb:us-east-1:123456789012:table/requests",
        authority_table_arn="arn:aws:dynamodb:us-east-1:123456789012:table/authority",
        artifact_bucket_arn="arn:aws:s3:::artifacts",
        dynamodb_key_arn="arn:aws:kms:us-east-1:123456789012:key/test",
        region="us-east-1",
        account_id="123456789012",
    )
    for statement in policy["Statement"]:
        actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        assert not {"dynamodb:Scan", "s3:ListBucket", "dynamodb:*", "s3:*"} & set(actions)
        if "dynamodb:DeleteItem" in actions and statement["Resource"].endswith("/authority"):
            assert statement["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"] == ["TASK_WORK_ID#*", "TASK_ADMISSION_CLEANUP#*"]
        if "s3:GetObject" in actions:
            assert statement["Resource"] == "arn:aws:s3:::artifacts/tasks/*"
