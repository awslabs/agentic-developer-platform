"""Pre-token human authority comes from a subject-verified user record."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import boto3
import pytest

ROOT = Path(__file__).resolve().parents[2]
TRIGGER = ROOT / "infra/modules/cognito/lambda/pre_token_generation.py"
POLICY = ROOT / "infra/modules/cognito/lambda.tf"
AUTHORITY = {"custom:org_id", "custom:team_id", "custom:department_id", "custom:role"}


@pytest.fixture
def trigger(monkeypatch):
    resource = Mock()
    monkeypatch.setattr(boto3, "resource", lambda *args, **kwargs: resource)
    client = Mock()
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)
    specification = importlib.util.spec_from_file_location("verified_pre_token", TRIGGER)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    client.admin_get_user.return_value = {
        "Username": "canonical-user",
        "UserAttributes": [
            {"Name": key, "Value": value}
            for key, value in {
                "sub": "verified-sub",
                "custom:org_id": "selected-a",
                "custom:team_id": "team-a",
                "custom:department_id": "department-a",
                "custom:role": "member",
            }.items()
        ],
    }
    return module, client, resource


def event(source="TokenGeneration_Authentication", *, username="canonical-user"):
    return {
        "version": "3",
        "triggerSource": source,
        "userPoolId": "managed-pool",
        "userName": username,
        "request": {
            "userAttributes": {
                "sub": "verified-sub",
                "custom:org_id": "forged-org",
                "custom:team_id": "forged-team",
                "custom:department_id": "forged-department",
                "custom:role": "platform_admin",
                "custom:account_type": "service",
            },
            "clientMetadata": {"org_id": "metadata-org"},
        },
        "response": {"claimsAndScopeOverrideDetails": {"idTokenGeneration": {"claimsToAddOrOverride": {"custom:org_id": "stale-org"}}}},
    }


@pytest.mark.parametrize(
    "source",
    [
        "TokenGeneration_Authentication",
        "TokenGeneration_HostedAuth",
        "TokenGeneration_RefreshTokens",
        "TokenGeneration_NewPasswordChallenge",
    ],
)
def test_human_tokens_use_protected_record_for_both_tokens(trigger, source):
    module, client, _ = trigger
    input_event = event(source)
    result = module.handler(input_event, None)
    client.admin_get_user.assert_called_once_with(UserPoolId="managed-pool", Username="canonical-user")
    expected = {
        "custom:org_id": "selected-a",
        "custom:team_id": "team-a",
        "custom:department_id": "department-a",
        "custom:role": "member",
        "custom:account_type": "human",
    }
    for token in ("idTokenGeneration", "accessTokenGeneration"):
        overrides = result["response"]["claimsAndScopeOverrideDetails"][token]
        assert overrides["claimsToAddOrOverride"] == expected
        assert overrides["claimsToSuppress"] == []


def test_federated_username_and_selection_switch_ignore_stale_event(trigger):
    module, client, _ = trigger
    client.admin_get_user.return_value["Username"] = "GitHub_123"
    for attribute in client.admin_get_user.return_value["UserAttributes"]:
        if attribute["Name"] == "custom:org_id":
            attribute["Value"] = "selected-b"
    result = module.handler(event("TokenGeneration_HostedAuth", username="GitHub_123"), None)
    client.admin_get_user.assert_called_once_with(UserPoolId="managed-pool", Username="GitHub_123")
    for token in ("idTokenGeneration", "accessTokenGeneration"):
        assert result["response"]["claimsAndScopeOverrideDetails"][token]["claimsToAddOrOverride"]["custom:org_id"] == "selected-b"


def test_unassigned_user_suppresses_all_tenant_authority(trigger):
    module, client, _ = trigger
    client.admin_get_user.return_value["UserAttributes"] = [
        {"Name": "sub", "Value": "verified-sub"},
        {"Name": "custom:role", "Value": "platform_admin"},
    ]
    result = module.handler(event("TokenGeneration_RefreshTokens"), None)
    for token in ("idTokenGeneration", "accessTokenGeneration"):
        overrides = result["response"]["claimsAndScopeOverrideDetails"][token]
        assert overrides["claimsToAddOrOverride"] == {"custom:account_type": "human"}
        assert set(overrides["claimsToSuppress"]) == AUTHORITY


@pytest.mark.parametrize(
    "failure",
    [
        "missing_pool",
        "missing_username",
        "missing_subject",
        "wrong_subject",
        "wrong_username",
        "disabled",
        "missing_record",
        "read_error",
        "duplicate_subject",
    ],
)
def test_missing_or_unverified_record_never_returns_privileged_event(trigger, caplog, failure):
    module, client, _ = trigger
    input_event = event()
    if failure == "missing_pool":
        input_event.pop("userPoolId")
    elif failure == "missing_username":
        input_event.pop("userName")
    elif failure == "missing_subject":
        input_event["request"]["userAttributes"].pop("sub")
    elif failure == "wrong_subject":
        client.admin_get_user.return_value["UserAttributes"][0]["Value"] = "other-sub"
    elif failure == "wrong_username":
        client.admin_get_user.return_value["Username"] = "other-user"
    elif failure == "disabled":
        client.admin_get_user.return_value["Enabled"] = False
    elif failure == "missing_record":
        client.admin_get_user.return_value = {"Username": "canonical-user"}
    elif failure == "read_error":
        client.admin_get_user.side_effect = RuntimeError("private-details")
    else:
        client.admin_get_user.return_value["UserAttributes"].append({"Name": "sub", "Value": "other-sub"})
    with pytest.raises((ValueError, RuntimeError)):
        module.handler(input_event, None)
    if failure.startswith("missing_") and failure != "missing_record":
        client.admin_get_user.assert_not_called()
    assert "forged-org" not in caplog.text
    assert "private-details" not in caplog.text


def test_registry_bound_service_claims_need_no_human_user_read(trigger):
    module, client, resource = trigger
    resource.Table.return_value.get_item.return_value = {"Item": {"org_id": "registered-org", "agent_name": "registered-service"}}
    result = module.handler(
        {
            "triggerSource": "TokenGeneration_ClientCredentials",
            "callerContext": {"clientId": "registered-client"},
            "request": {"clientMetadata": {"org_id": "forged-org"}},
        },
        None,
    )
    claims = result["response"]["claimsAndScopeOverrideDetails"]["accessTokenGeneration"]["claimsToAddOrOverride"]
    assert claims["custom:org_id"] == "registered-org"
    assert claims["custom:account_type"] == "service"
    client.admin_get_user.assert_not_called()


@pytest.mark.parametrize(
    ("source", "username"),
    [
        ("TokenGeneration_Authentication", "canonical-user"),
        ("TokenGeneration_HostedAuth", "GitHub_123"),
        ("TokenGeneration_RefreshTokens", "canonical-user"),
        ("TokenGeneration_NewPasswordChallenge", "canonical-user"),
    ],
)
def test_projected_human_token_lifecycle_and_service_separation(trigger, monkeypatch, source, username):
    monkeypatch.syspath_prepend(str(ROOT.parent / "domain-apps/superplane/auth"))
    from superplane_auth.policy import (
        TRUSTED_VALIDATION_PATH,
        DomainTokenPolicy,
        TokenRejectedError,
    )

    module, client, resource = trigger
    client.admin_get_user.return_value["Username"] = username
    protected = client.admin_get_user.return_value["UserAttributes"]
    for attribute in protected:
        if attribute["Name"] == "custom:org_id":
            attribute["Value"] = "selected-b"
    policy = DomainTokenPolicy(allowed_client_ids=["domain-client"], expected_issuer="https://issuer.example")

    def token_claims(overrides, token):
        values = {
            "sub": "verified-sub",
            "iss": "https://issuer.example",
            "token_use": token,
            "client_id": "domain-client",
            "custom:org_id": "stale-org",
        }
        for attribute in overrides.get("claimsToSuppress", []):
            values.pop(attribute, None)
        values.update(overrides["claimsToAddOrOverride"])
        return values

    response = module.handler(event(source, username=username), None)
    overrides = response["response"]["claimsAndScopeOverrideDetails"]
    for token, token_use in (("idTokenGeneration", "id"), ("accessTokenGeneration", "access")):
        claims = token_claims(overrides[token], token_use)
        assert claims["custom:org_id"] == "selected-b"
        if token_use == "id":
            with pytest.raises(TokenRejectedError):
                policy.admit_candidate(claims, validation_path=TRUSTED_VALIDATION_PATH)
        else:
            candidate = policy.admit_candidate(claims, validation_path=TRUSTED_VALIDATION_PATH)
            assert (candidate.subject, candidate.org_id, candidate.account_type) == ("verified-sub", "selected-b", "human")

    protected[:] = [attribute for attribute in protected if attribute["Name"] != "custom:org_id"]
    onboarding = module.handler(event("TokenGeneration_RefreshTokens", username=username), None)
    overrides = onboarding["response"]["claimsAndScopeOverrideDetails"]
    for token, token_use in (("idTokenGeneration", "id"), ("accessTokenGeneration", "access")):
        assert "custom:org_id" in overrides[token]["claimsToSuppress"]
        with pytest.raises(TokenRejectedError):
            policy.admit_candidate(
                token_claims(overrides[token], token_use),
                validation_path=TRUSTED_VALIDATION_PATH,
            )

    previous_reads = client.admin_get_user.call_count
    resource.Table.return_value.get_item.return_value = {"Item": {"org_id": "registered-service-org", "agent_name": "executor"}}
    service = module.handler(
        {
            "triggerSource": "TokenGeneration_ClientCredentials",
            "callerContext": {"clientId": "domain-client"},
            "request": {"clientMetadata": {"org_id": "selected-b"}},
        },
        None,
    )["response"]["claimsAndScopeOverrideDetails"]
    assert "idTokenGeneration" not in service
    service_claims = token_claims(service["accessTokenGeneration"], "access")
    service_candidate = policy.admit_candidate(service_claims, validation_path=TRUSTED_VALIDATION_PATH)
    assert (service_candidate.org_id, service_candidate.account_type) == ("registered-service-org", "service")
    assert client.admin_get_user.call_count == previous_reads


def test_user_read_policy_is_pool_scoped_and_read_only():
    policy = POLICY.read_text().split('resource "aws_iam_role_policy" "pre_token_generation_user_read" {', 1)[1].split("# Lambda Function", 1)[0]
    assert 'Action   = ["cognito-idp:AdminGetUser"]' in policy
    assert "Resource = aws_cognito_user_pool.main.arn" in policy
    assert "*" not in policy
