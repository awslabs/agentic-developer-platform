"""Exercise real SDK refresh/signing while deployment tools hold customer creds."""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import botocore.session
from botocore.stub import Stubber
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adp_cred.assume import cmd_assume
from adp_cred.client import _do_request
from adp_trigger.transport_identity import (
    gateway_signing_region,
    preserve_worker_identity,
    worker_credentials,
)


ROLE = "arn:aws:iam::123456789012:role/existing-worker"
GATEWAY = "https://example.execute-api.us-east-1.amazonaws.com/dev"


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(("AWS_", "ADP_WORKER_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ROLE_ARN", ROLE)
    monkeypatch.setenv("AWS_ROLE_SESSION_NAME", "worker-session")
    token = tmp_path / "web-token"
    token.write_text("pod-token-1")
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", str(token))
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", GATEWAY)
    for variable, value in (
        ("ADP_RUN_CREDENTIAL_FILE", "run-1"),
        ("ADP_WORKLOAD_TOKEN_FILE", "proof-1"),
    ):
        file = tmp_path / variable
        file.write_text(value)
        monkeypatch.setenv(variable, str(file))
    return token


def sts_response(key):
    return {
        "Credentials": {
            "AccessKeyId": key,
            "SecretAccessKey": "test-platform-secret",
            "SessionToken": "test-platform-session",
            "Expiration": datetime.now(timezone.utc) + timedelta(hours=1),
        }
    }


def install_customer_env(monkeypatch):
    env = os.environ.copy()
    preserve_worker_identity(env)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIA_CUSTOMER_DEPLOY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "customer-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "customer-session")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.delenv("AWS_ROLE_ARN")
    monkeypatch.delenv("AWS_WEB_IDENTITY_TOKEN_FILE")


@pytest.mark.parametrize("authority", ["true", "false"])
def test_platform_refresh_and_customer_role_chaining_remain_independent(
    worker_env, monkeypatch, authority
):
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", authority)
    install_customer_env(monkeypatch)
    session = botocore.session.get_session()
    assert session.get_credentials().access_key == "ASIA_CUSTOMER_DEPLOY"
    assert session.get_config_variable("region") == "us-west-2"
    sts = session.create_client("sts", region_name="us-east-1")
    with Stubber(sts) as stub, patch.object(session, "create_client", return_value=sts):
        for epoch in (1, 2):
            stub.add_response(
                "assume_role_with_web_identity",
                sts_response(f"ASIA_PLATFORM_KEY_{epoch}"),
                {
                    "RoleArn": ROLE,
                    "RoleSessionName": "worker-session",
                    "WebIdentityToken": f"pod-token-{epoch}",
                },
            )
        creds = worker_credentials(session)
        assert creds.get_frozen_credentials().access_key == "ASIA_PLATFORM_KEY_1"
        worker_env.write_text("pod-token-2")
        creds._expiry_time = datetime.now(timezone.utc) - timedelta(seconds=1)
        creds._refresh_using.__self__._cache.clear()
        assert creds.get_frozen_credentials().access_key == "ASIA_PLATFORM_KEY_2"
        stub.assert_no_pending_responses()

    # Ordinary SDK role chaining still consumes the customer's current session.
    chained = session.create_client("sts", region_name="us-west-2")
    assert chained._request_signer._credentials.access_key == "ASIA_CUSTOMER_DEPLOY"
    with Stubber(chained) as stub:
        args = {
            "RoleArn": "arn:aws:iam::222222222222:role/customer-next-hop",
            "RoleSessionName": "deployment",
            "ExternalId": "customer-external-id",
        }
        stub.add_response("assume_role", sts_response("ASIA_CHAINED_CUSTOMER"), args)
        assert chained.assume_role(**args)["Credentials"]["AccessKeyId"] == "ASIA_CHAINED_CUSTOMER"


def test_nested_exec_retains_platform_identity_and_latest_customer_credentials(
    worker_env, monkeypatch
):
    monkeypatch.setenv("ENABLE_USER_CREDENTIALS", "1")
    for key in ("ADP_USER_ID", "ADP_AGENT_ID", "ADP_TASK_ID"):
        monkeypatch.setenv(key, "test")
    first_env = None
    for epoch in (1, 2):
        response = {
            "profile_name": "customer",
            "access_key_id": f"customer-key-{epoch}",
            "secret_access_key": "customer-secret",
            "session_token": "customer-token",
            "region": "us-west-2",
        }
        with (
            patch("adp_cred.assume._do_request", return_value=response),
            patch("adp_cred.assume._write_aws_credentials"),
            patch("os.execvpe") as execute,
        ):
            cmd_assume(["--label", "deployment", "--exec", "aws", "sts", "get-caller-identity"])
        env = execute.call_args.args[2]
        assert env["ADP_WORKER_IRSA_ROLE_ARN"] == ROLE
        assert env["ADP_WORKER_IRSA_TOKEN_FILE"] == str(worker_env)
        assert env["ADP_WORKER_AWS_REGION"] == "us-east-1"
        assert env["AWS_REGION"] == "us-west-2"
        assert env["AWS_ACCESS_KEY_ID"] == f"customer-key-{epoch}"
        assert not any(
            key in env for key in ("AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_PROFILE")
        )
        if first_env is None:
            first_env = env
            for key in ("AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_PROFILE"):
                monkeypatch.delenv(key, raising=False)
            for key, value in env.items():
                monkeypatch.setenv(key, value)


def test_cli_signs_platform_identity_and_refreshed_proof_from_customer_exec(
    worker_env, monkeypatch
):
    install_customer_env(monkeypatch)
    session = botocore.session.get_session()
    sts = session.create_client("sts", region_name="us-east-1")
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"ok": true}'
    with (
        Stubber(sts) as stub,
        patch.object(session, "create_client", return_value=sts),
        patch("botocore.session.get_session", return_value=session),
        patch("adp_cred.client.build_opener") as opener,
    ):
        opener.return_value.open.return_value = response
        for epoch in (1, 2):
            stub.add_response(
                "assume_role_with_web_identity",
                sts_response(f"ASIA_PLATFORM_KEY_{epoch}"),
                {
                    "RoleArn": ROLE,
                    "RoleSessionName": "worker-session",
                    "WebIdentityToken": "pod-token-1",
                },
            )
            with open(os.environ["ADP_RUN_CREDENTIAL_FILE"], "w") as file:
                file.write(f"run-{epoch}")
            assert _do_request(
                "POST",
                f"{GATEWAY}/internal/v1/credential-assume-role",
                None,
                True,
                {"label": "prod"},
            ) == {"ok": True}
            request = opener.return_value.open.call_args.args[0]
            signature = request.get_header("Authorization")
            assert f"Credential=ASIA_PLATFORM_KEY_{epoch}/" in signature
            assert "/us-east-1/execute-api/" in signature
            assert "x-adp-run-credential" in signature and "x-adp-workload-token" in signature
            assert request.get_header("X-adp-run-credential") == f"run-{epoch}"
            assert "CUSTOMER" not in signature
        stub.assert_no_pending_responses()
    assert session.get_credentials().access_key == "ASIA_CUSTOMER_DEPLOY"


@pytest.mark.parametrize(
    "missing", ["ADP_WORKER_IRSA_ROLE_ARN", "ADP_WORKER_IRSA_TOKEN_FILE", "both"]
)
def test_missing_platform_identity_never_falls_back_to_customer(worker_env, monkeypatch, missing):
    install_customer_env(monkeypatch)
    for key in ("ADP_WORKER_IRSA_ROLE_ARN", "ADP_WORKER_IRSA_TOKEN_FILE"):
        if missing in (key, "both"):
            monkeypatch.delenv(key)
    with pytest.raises(RuntimeError, match="IRSA identity unavailable"):
        worker_credentials(botocore.session.get_session())


@pytest.mark.parametrize(
    "endpoint,use_sigv4",
    [("http://gateway.test", True), (GATEWAY, False), ("https://user:password@gateway.test", True)],
)
def test_protected_cli_refuses_insecure_transport(worker_env, endpoint, use_sigv4):
    with patch("adp_cred.client.urlopen") as send, patch("adp_cred.client.build_opener") as opener:
        with pytest.raises(RuntimeError, match="HTTPS and SigV4"):
            _do_request("POST", endpoint, "legacy-key", use_sigv4, {})
        send.assert_not_called()
        opener.assert_not_called()


def test_protected_cli_missing_proof_never_sends(worker_env, monkeypatch):
    monkeypatch.delenv("ADP_RUN_CREDENTIAL_FILE")
    with patch("adp_cred.client.build_opener") as opener:
        # Identity proof is checked before resolving deferred STS credentials.
        from lib.gateway_credential_client import GatewayCredentialError

        with pytest.raises(GatewayCredentialError, match="identity unavailable"):
            _do_request("POST", GATEWAY, None, True, {})
        opener.assert_not_called()


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        (GATEWAY, "us-east-1"),
        ("https://example.execute-api.cn-north-1.amazonaws.com.cn/dev", "cn-north-1"),
        ("https://custom-gateway.test/dev", "us-east-1"),
        ("https://example.execute-api.eu-west-1.amazonaws.com.invalid/dev", "us-east-1"),
    ],
)
def test_gateway_region_does_not_follow_customer_region(
    worker_env, monkeypatch, endpoint, expected
):
    install_customer_env(monkeypatch)
    assert gateway_signing_region(endpoint) == expected
