import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.credentials import Credentials

from common import gateway_client


def test_existing_internal_key_uses_ssm_without_secret_value_in_configuration(
    monkeypatch,
):
    monkeypatch.setenv("INTERNAL_API_KEY_ARN", "")
    monkeypatch.setenv(
        "INTERNAL_API_KEY_PARAMETER_NAME", "/adp/dev/gateway/internal-api-key"
    )
    monkeypatch.setattr(gateway_client, "_internal_api_key", None)
    client = MagicMock()
    client.get_parameter.return_value = {"Parameter": {"Value": "test-only-key"}}
    with patch("boto3.client", return_value=client):
        assert gateway_client._resolve_internal_api_key() == "test-only-key"
    client.get_parameter.assert_called_once_with(
        Name="/adp/dev/gateway/internal-api-key", WithDecryption=True
    )


def test_lookup_signs_exact_method_body_and_existing_internal_key():
    body = b'{"installation_id":"123"}'
    req = urllib.request.Request(
        "https://abc.execute-api.us-east-1.amazonaws.com/dev/internal/v1/resolve-installation",
        data=body,
        headers={"X-Internal-Api-Key": "test-only-key"},
        method="POST",
    )
    session = MagicMock()
    session.get_credentials.return_value = Credentials(
        "fixture-access", "fixture-secret", "fixture-session"
    )
    with patch("boto3.Session", return_value=session):
        signed = gateway_client._sign_internal_lookup(req)
    assert signed.data == body
    assert signed.get_method() == "POST"
    assert "AWS4-HMAC-SHA256" in signed.get_header("Authorization")
    assert signed.get_header("X-internal-api-key") == "test-only-key"
    assert signed.get_header("X-amz-security-token") == "fixture-session"


def test_direct_internal_endpoint_does_not_receive_aws_credentials():
    req = urllib.request.Request(
        "http://internal-alb/internal/v1/resolve-user", data=b"{}"
    )
    with patch("boto3.Session") as session:
        assert gateway_client._sign_internal_lookup(req) is req
    session.assert_not_called()
    assert req.get_header("Authorization") is None


def test_identity_policy_has_only_two_literal_routes_and_one_parameter_read():
    policy = (
        Path(__file__).resolve().parents[3] / "infra" / "legacy-identity-lookups.tf"
    ).read_text()
    assert '["resolve-installation", "resolve-user"]' in policy
    assert "POST/internal/v1/${route}" in policy
    assert '"ssm:GetParameter"' in policy
    assert "*" not in policy
    assert "GetParametersByPath" not in policy
