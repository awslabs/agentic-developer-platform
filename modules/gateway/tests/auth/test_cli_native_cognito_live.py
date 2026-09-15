"""Opt-in candidate-router test using #5173 identities and actual Cognito/refresh.

This proves the candidate native handler against real Cognito and JWT keys. The
request database is local SQLite and AWS calls use the explicit operator profile;
it does NOT certify deployed native endpoints, gateway IAM, or PostgreSQL limits.
"""

import importlib.util
import io
import json
import os
import re
import secrets
import subprocess
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth import cli_native_login as native
from src.auth import dependencies
from src.auth.cognito_jwt import CognitoJWTValidator
from src.shared.database import get_db

CLI = Path(__file__).parents[2] / "cli"
sys.path.insert(0, str(CLI))
import adp_common as common  # noqa: E402


@pytest.mark.skipif(not os.environ.get("ADP_NATIVE_LIVE_CONFIG"), reason="requires explicit #5173 live config and private fixture state")
def test_real_cognito_admin_login_refresh_and_nonadmin_refusal(tmp_path, monkeypatch, db_session, request):
    config = common.read_private_json(os.environ["ADP_NATIVE_LIVE_CONFIG"])
    state = common.read_private_json(os.environ["ADP_NATIVE_LIVE_STATE"])
    assert re.fullmatch(r"adp-e2e-\d{8}-\d{6}-[a-f0-9]{6}", state["prefix"])
    assert state["cleanup"] != "complete"
    session = boto3.Session(profile_name=config["platform_profile"], region_name=config["region"])
    assert session.client("sts").get_caller_identity()["Account"] == config["platform_account"]
    with urllib.request.urlopen(config["gateway_url"] + "/.well-known/cognito-config", timeout=30) as response:
        discovery = json.load(response)
    assert all(discovery[key] == state["cognito"][key] for key in ("user_pool_id", "client_id", "region"))
    cli_client_id = os.environ["ADP_NATIVE_LIVE_CLIENT"]
    session.client("cognito-idp").describe_user_pool_client(UserPoolId=discovery["user_pool_id"], ClientId=cli_client_id)
    settings = SimpleNamespace(
        cognito_cli_client_id=cli_client_id,
        cognito_user_pool_id=discovery["user_pool_id"],
        token_secret_key=secrets.token_urlsafe(40),
        aws_region=discovery["region"],
    )
    monkeypatch.setattr(native, "get_settings", lambda: settings)
    validator = CognitoJWTValidator(
        user_pool_id=discovery["user_pool_id"],
        client_id=cli_client_id,
        allowed_client_ids=[cli_client_id],
        region=discovery["region"],
    )
    monkeypatch.setattr(dependencies, "_get_cognito_validator", lambda: validator)
    app = FastAPI()
    app.include_router(native.router)

    async def database():
        yield db_session

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[native.get_native_client] = lambda: session.client("cognito-idp")
    client = TestClient(app, raise_server_exceptions=False)

    class CandidateApi:
        def request(self, method, path, body=None, **kwargs):
            headers = {"Authorization": "Bearer " + kwargs["token"]} if kwargs.get("token") else {}
            response = client.request(method, path, json=body, headers=headers)
            if response.status_code != 200:
                raise common.CliError(f"Candidate route returned HTTP {response.status_code}", exit_code=3 if response.status_code == 403 else 5)
            return response.json()

    monkeypatch.setenv("HOME", str(tmp_path))
    request.addfinalizer(lambda: (tmp_path / ".bedrock-gateway/tokens.json").unlink(missing_ok=True))
    common.write_json(common.config_path(), {"gateway_url": config["gateway_url"]})
    spec = importlib.util.spec_from_file_location("live_adp_admin", CLI / "adp-admin.py")
    admin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(admin)

    def login(who):
        user = state["users"][who]
        assert user["username"].startswith(state["prefix"])
        credentials = {"username": user.get("login_username", user["username"]), "password": user["password"]}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(credentials)))
        return admin.login(admin.parser().parse_args(["login", "--credentials-stdin", "--json"]), CandidateApi())

    assert login("admin")["status"] == "verified"
    before = common.read_private_json(tmp_path / ".bedrock-gateway/tokens.json")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("AWS_", "ADP_GATEWAY_"))}
    refreshed = subprocess.run(["bash", str(CLI / "bg-cognito-auth.sh"), "refresh"], env=env, capture_output=True, text=True, timeout=120)
    assert refreshed.returncode == 0, "Gateway refresh rejected the native login session"
    after = common.read_private_json(tmp_path / ".bedrock-gateway/tokens.json")
    rotated = after["refresh_token"] != before["refresh_token"]
    assert rotated, "Expected Cognito refresh rotation"
    verified = client.get("/auth/cli/admin-session", headers={"Authorization": "Bearer " + after["access_token"]})
    assert verified.status_code == 200
    with pytest.raises(common.CliError) as denied:
        login("member1")
    assert denied.value.exit_code == 3
    preserved = common.read_private_json(tmp_path / ".bedrock-gateway/tokens.json") == after
    assert preserved, "Denied login changed the existing session"
