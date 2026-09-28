"""Native CLI login: real routes/database, Cognito stubbed at the AWS boundary."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import jwt
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth import cli_native_login as native
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

TOKENS = {"AccessToken": "access-secret", "IdToken": "id-secret", "RefreshToken": "refresh-secret", "ExpiresIn": 3600}


@pytest.fixture
def settings(monkeypatch):
    settings = SimpleNamespace(
        cognito_cli_client_id="cli-test",
        cognito_user_pool_id="us-east-1_test",
        token_secret_key="test-signing-key-never-used-outside-tests-123",
        aws_region="us-east-1",
    )
    monkeypatch.setattr(native, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def cognito():
    client = Mock()
    client.admin_initiate_auth.return_value = {"AuthenticationResult": TOKENS}
    client.admin_respond_to_auth_challenge.return_value = {"AuthenticationResult": TOKENS}
    return client


@pytest.fixture
def client(db_session, settings, cognito):
    app = FastAPI()
    app.include_router(native.router)

    async def session():
        yield db_session

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[native.get_native_client] = lambda: cognito
    return TestClient(app, raise_server_exceptions=False)


def start(client):
    return client.post("/auth/cli/password", json={"username": "admin@example.com", "password": "private-password"})


def challenge(cognito, kind="SOFTWARE_TOKEN_MFA", required=None):
    cognito.admin_initiate_auth.return_value = {
        "ChallengeName": kind,
        "Session": "cognito-session-secret",
        "ChallengeParameters": {"USER_ID_FOR_SRP": "canonical-user", "requiredAttributes": required or "[]"},
    }


def finish(client, continuation, responses=None):
    return client.post("/auth/cli/challenge", json={"continuation": continuation, "responses": responses or {"SOFTWARE_TOKEN_MFA_CODE": "123456"}})


def test_password_auth_does_not_reset_password(client, cognito, settings):
    result = start(client)
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["refresh_token"] == "refresh-secret"
    cognito.admin_initiate_auth.assert_called_once_with(
        UserPoolId=settings.cognito_user_pool_id,
        ClientId=settings.cognito_cli_client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": "admin@example.com", "PASSWORD": "private-password"},
    )
    cognito.admin_set_user_password.assert_not_called()
    cognito.admin_add_user_to_group.assert_not_called()


@pytest.mark.parametrize(
    "kind,responses,required",
    [
        ("SMS_MFA", {"SMS_MFA_CODE": "123456"}, None),
        ("SOFTWARE_TOKEN_MFA", {"SOFTWARE_TOKEN_MFA_CODE": "123456"}, None),
        ("NEW_PASSWORD_REQUIRED", {"NEW_PASSWORD": "new-private-password", "userAttributes.name": "Admin"}, '["name"]'),
    ],
)
def test_challenge_and_single_use(client, cognito, kind, responses, required):
    challenge(cognito, kind, required)
    started = start(client).json()
    completed = finish(client, started["continuation"], responses)
    assert completed.status_code == 200
    assert completed.json()["access_token"] == "access-secret"
    args = cognito.admin_respond_to_auth_challenge.call_args.kwargs
    assert args["Session"] == "cognito-session-secret"
    assert args["ChallengeName"] == kind
    assert args["ChallengeResponses"] == {"USERNAME": "canonical-user", **responses}
    assert finish(client, started["continuation"], responses).status_code == 401
    assert cognito.admin_respond_to_auth_challenge.call_count == 1
    cognito.admin_set_user_password.assert_not_called()


@pytest.mark.parametrize("mutation", ["signature", "expiry", "pool", "client", "issuer"])
def test_rejects_tampered_or_cross_deployment_continuation(client, cognito, settings, mutation):
    challenge(cognito)
    token = start(client).json()["continuation"]
    if mutation == "signature":
        token = token.rsplit(".", 1)[0] + ".invalid"
    elif mutation in {"pool", "client"}:
        setattr(settings, "cognito_user_pool_id" if mutation == "pool" else "cognito_cli_client_id", "other")
    else:
        claims = jwt.decode(token, options={"verify_signature": False})
        claims["exp" if mutation == "expiry" else "iss"] = 1 if mutation == "expiry" else "other-flow"
        token = jwt.encode(claims, settings.token_secret_key, algorithm="HS256")
    assert finish(client, token).status_code == 401
    cognito.admin_respond_to_auth_challenge.assert_not_called()


def test_challenge_cannot_replace_identity_or_response_type(client, cognito):
    challenge(cognito)
    token = start(client).json()["continuation"]
    assert finish(client, token, {"USERNAME": "victim", "SOFTWARE_TOKEN_MFA_CODE": "123456"}).status_code == 400
    assert finish(client, token, {"NEW_PASSWORD": "replacement"}).status_code == 400
    cognito.admin_respond_to_auth_challenge.assert_not_called()


@pytest.mark.parametrize("error", ["UserNotFoundException", "NotAuthorizedException", "CodeMismatchException", "PasswordResetRequiredException"])
def test_authentication_errors_are_generic(client, cognito, error):
    cognito.admin_initiate_auth.side_effect = ClientError({"Error": {"Code": error, "Message": "private-password"}}, "AdminInitiateAuth")
    result = start(client)
    assert result.status_code == 401
    assert result.json()["detail"]["error"] == "authentication_failed"
    assert "private-password" not in result.text and error not in result.text


def test_input_errors_do_not_echo_secrets(client):
    result = client.post("/auth/cli/password", json={"password": ["private-password"]})
    assert result.status_code == 400
    assert "private-password" not in result.text


def test_failed_mfa_is_consumed_and_requires_new_login(client, cognito):
    challenge(cognito)
    token = start(client).json()["continuation"]
    cognito.admin_respond_to_auth_challenge.side_effect = ClientError({"Error": {"Code": "CodeMismatchException"}}, "AdminRespondToAuthChallenge")
    assert finish(client, token).status_code == 401
    assert finish(client, token).status_code == 401
    assert cognito.admin_respond_to_auth_challenge.call_count == 1


def test_unsupported_enrollment_is_pending_without_disabling_mfa(client, cognito):
    challenge(cognito, "MFA_SETUP")
    result = start(client).json()
    assert result["status"] == "pending"
    assert "continuation" not in result
    cognito.admin_set_user_mfa_preference.assert_not_called()


def test_username_rate_limit_shared_by_requests(client, cognito):
    for _ in range(10):
        assert start(client).status_code == 200
    assert start(client).status_code == 429
    assert cognito.admin_initiate_auth.call_count == 10


def test_invalid_continuations_are_ip_rate_limited(client, cognito):
    for _ in range(30):
        assert finish(client, "invalid-token").status_code == 401
    assert finish(client, "invalid-token").status_code == 429
    cognito.admin_respond_to_auth_challenge.assert_not_called()


def test_missing_configuration_fails_closed(client, settings, cognito):
    settings.token_secret_key = ""
    assert start(client).status_code == 503
    cognito.admin_initiate_auth.assert_not_called()


@pytest.mark.parametrize("admin,status", [(False, 403), (True, 200)])
def test_admin_session_authority_is_server_side(client, admin, status):
    client.app.dependency_overrides[get_current_user] = lambda: TokenContext(
        user_id="user",
        org_id="org",
        team_id="",
        department_id="",
        account_type="human",
        is_admin=admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert client.get("/auth/cli/admin-session").status_code == status
