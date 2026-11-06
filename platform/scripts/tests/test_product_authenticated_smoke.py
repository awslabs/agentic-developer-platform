"""Product smoke uses a protected automated fixture without accepting a release."""

import importlib.util
import json
import stat
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "product_authenticated_smoke",
    Path(__file__).resolve().parents[1] / "product_authenticated_smoke.py",
)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)

ACCOUNT = "123456789012"
FIXTURE = f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:adp/dev/gateway/test-admin-credentials-AbCd12"


@pytest.fixture
def ports():
    values = {
        "cognito-user-pool-id": "us-east-1_test",
        "cognito-client-id": "test-client",
        "cloudfront-domain": "test.cloudfront.net",
    }
    fixture = {
        "username": "maintained-test-user",
        "password": "private-password",
        "cognito_user_pool_id": values["cognito-user-pool-id"],
        "cognito_client_id": values["cognito-client-id"],
    }
    identity = {
        "Account": ACCOUNT,
        "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/adp-dev-trusted-checks/test",
    }
    auth = {
        "AuthenticationResult": {
            "AccessToken": "private-access",
            "IdToken": "private-id",
        }
    }
    responses = [
        (200, {"status": "registered"}),
        (200, {"items": [], "next_cursor": None}),
    ]
    calls, reads = [], []

    def invoke(operation, payload, region):
        assert region == "us-east-1"
        calls.append((operation, payload))
        if operation == "sts get-caller-identity":
            return identity
        if operation == "ssm get-parameter":
            assert payload["Name"].startswith("/adp/dev/gateway/")
            return {"Parameter": {"Value": values[payload["Name"].rsplit("/", 1)[1]]}}
        if operation == "secretsmanager get-secret-value":
            assert payload == {"SecretId": FIXTURE}
            return {"SecretString": json.dumps(fixture)}
        assert operation == "cognito-idp admin-initiate-auth"
        assert payload["AuthFlow"] == "ADMIN_USER_PASSWORD_AUTH"
        assert payload["AuthParameters"] == {
            "USERNAME": fixture["username"],
            "PASSWORD": fixture["password"],
        }
        return auth

    def read(url, token):
        assert token == "private-access"
        reads.append(url)
        return responses[len(reads) - 1]

    return locals()


def run(ports):
    return smoke.check(
        "dev", ACCOUNT, FIXTURE, invoke=ports["invoke"], read=ports["read"]
    )


def test_authenticates_fixture_and_checks_live_product_route_without_acceptance(ports):
    result = run(ports)
    assert result == {
        "authenticated_product_smoke": "passed",
        "acceptance_evidence": False,
    }
    assert ports["reads"] == [
        "https://test.cloudfront.net/api/access/status",
        "https://test.cloudfront.net/api/v1/products?limit=1",
    ]
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(
    "environment,account,arn",
    [
        ("prod", ACCOUNT, FIXTURE),
        ("dev", "other", FIXTURE),
        ("dev", ACCOUNT, ""),
        ("dev", ACCOUNT, FIXTURE.replace("test-admin-credentials", "personal-token")),
    ],
)
def test_refuses_unselected_fixture_before_accessing_aws(
    ports, environment, account, arn
):
    with pytest.raises(ValueError):
        smoke.check(
            environment, account, arn, invoke=ports["invoke"], read=ports["read"]
        )
    assert not ports["calls"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("Account", "999999999999"),
        ("Arn", f"arn:aws:sts::{ACCOUNT}:assumed-role/operator/test"),
    ],
)
def test_operator_or_other_account_cannot_qualify_checks_identity(ports, field, value):
    ports["identity"][field] = value
    with pytest.raises(ValueError, match="checks identity"):
        run(ports)
    assert len(ports["calls"]) == 1


@pytest.mark.parametrize("field", ["cognito_client_id", "cognito_user_pool_id"])
def test_wrong_cognito_fixture_is_refused_before_authentication(ports, field):
    ports["fixture"][field] = "another-target"
    with pytest.raises(ValueError, match="Cognito target"):
        run(ports)
    assert not any(
        operation == "cognito-idp admin-initiate-auth"
        for operation, _ in ports["calls"]
    )


def test_challenge_or_id_token_only_is_not_a_smoke_login(ports):
    ports["auth"]["AuthenticationResult"].pop("AccessToken")
    with pytest.raises(ValueError, match="access token"):
        run(ports)
    assert not ports["reads"]


@pytest.mark.parametrize(
    "index,response",
    [
        (0, (401, {})),
        (0, (200, {"status": "new"})),
        (1, (403, {})),
        (1, (404, {})),
        (1, (200, {"status": "healthy"})),
        (1, (200, {"items": "invalid", "next_cursor": None})),
        (1, (200, {"items": [{"id": "unknown"}], "next_cursor": None})),
    ],
)
def test_denials_and_health_only_responses_cannot_pass(ports, index, response):
    ports["responses"][index] = response
    with pytest.raises(ValueError):
        run(ports)


def test_bearer_is_not_forwarded_through_redirects():
    assert (
        smoke.NoRedirect().redirect_request(
            None, None, 302, "redirect", {}, "https://outside.example"
        )
        is None
    )


@pytest.mark.parametrize("returncode", [0, 252])
def test_aws_input_can_be_read_repeatedly_and_private_file_is_removed(
    monkeypatch, returncode
):
    payload = {"AuthParameters": {"PASSWORD": "private-password"}}
    paths = []

    def run(arguments, **kwargs):
        locator = arguments[arguments.index("--cli-input-json") + 1]
        assert locator.startswith("file://")
        path = Path(locator.removeprefix("file://"))
        assert path.is_file(), (
            "AWS CLI needs repeatable file input, not a consumed stdin pipe"
        )
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text()) == json.loads(path.read_text()) == payload
        assert "private-password" not in " ".join(arguments)
        assert "input" not in kwargs
        paths.append(path)
        return subprocess.CompletedProcess(
            arguments, returncode, '{"ok": true}', "private error"
        )

    monkeypatch.setattr(smoke.subprocess, "run", run)
    if returncode:
        with pytest.raises(ValueError, match="^Product smoke AWS operation failed$"):
            smoke.aws("cognito-idp admin-initiate-auth", payload, "us-east-1")
    else:
        assert smoke.aws("cognito-idp admin-initiate-auth", payload, "us-east-1") == {
            "ok": True
        }
    assert len(paths) == 1 and not paths[0].exists()
