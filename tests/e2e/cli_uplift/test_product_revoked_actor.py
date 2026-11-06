"""The denial oracle uses a real member login removed by the hierarchy API."""

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "remote"))
import common
import product_denial_actor


RUN_ID = "adp-e2e-20261020-113000-abcdef"
ORG = RUN_ID + "-product-" + "a" * 24
EMAIL = RUN_ID + "-product-user@adp-eval.example"


@pytest.fixture
def actor(monkeypatch):
    config = {
        "evaluation_id": RUN_ID,
        "product_actor_id": "admin-user",
        "product_denial_actor": {
            "org_id": ORG,
            "username": EMAIL,
            "user_id": "ordinary-user",
            "cognito_sub_bound": True,
        },
        "product_team_id": "owned-team",
        "product_cases": "api_product_lifecycle,api_denial_oracle",
        "credential_secret": f"adp/cli-uplift-eval/{RUN_ID}-product-fixtures",
    }
    encoded = base64.urlsafe_b64encode(json.dumps({"sub": "ordinary-sub"}).encode())
    login = {
        "access_token": "ordinary-token",
        "id_token": "header." + encoded.decode().rstrip("=") + ".signature",
    }
    row = {
        "id": "ordinary-user",
        "org_id": ORG,
        "email": EMAIL,
        "role": "member",
        "cognito_sub": "ordinary-sub",
    }
    snapshot = {
        "id": "ordinary-user",
        "org_id": ORG,
        "revision": "a" * 64,
        "resource": {
            "email": EMAIL,
            "membership_status": "active",
            "team_id": "owned-team",
        },
        "delete_permitted": True,
    }
    calls = []
    fixtures = {"non_admin_username": EMAIL, "non_admin_password": "secret-password"}
    monkeypatch.setattr(common, "clean_env", lambda _config: {})
    monkeypatch.setattr(
        common, "fixture_secret", lambda _config, _env, key: fixtures[key]
    )

    def api(_config, path, token, *, method="GET", body=None, expect=(200,)):
        calls.append((path, method, token))
        assert token == "admin-token"
        if path == "/auth/cli/password":
            assert method == "POST" and body == {
                "username": EMAIL,
                "password": fixtures["non_admin_password"],
            }
            return 200, login
        if path.endswith("/users"):
            return 200, {"users": [row]}
        if method == "DELETE":
            assert path.endswith("?expected_revision=" + "a" * 64)
            snapshot["revision"] = "b" * 64
            snapshot["resource"] = {
                "email": EMAIL,
                "membership_status": "revoked",
                "team_id": "",
            }
            return 200, json.loads(json.dumps(snapshot))
        if config.get("test_stale_readback") and snapshot["revision"] == "b" * 64:
            return 200, {**snapshot, "revision": "a" * 64}
        return 200, json.loads(json.dumps(snapshot))

    monkeypatch.setattr(common, "api", api)
    return config, login, row, snapshot, calls, fixtures


def test_revokes_only_ordinary_membership_after_owned_readback(actor):
    config, _login, _row, _snapshot, calls, fixtures = actor
    token, revoke = product_denial_actor.prepare(
        config, "admin-token", ORG, "admin-user"
    )
    assert token == "ordinary-token"
    assert [method for _path, method, _token in calls] == ["POST", "GET", "GET"]
    assert revoke() == {"user_id": "ordinary-user", "membership_revoked": True}
    assert [method for _path, method, _token in calls] == [
        "POST",
        "GET",
        "GET",
        "DELETE",
        "GET",
    ]
    assert not any("/users/ordinary-user" in path for path, _method, _token in calls)
    assert fixtures["non_admin_password"] not in str(calls)


@pytest.mark.parametrize(
    "wrong",
    [
        "owner",
        "tenant",
        "intent",
        "fixture",
        "token",
        "subject",
        "canonical",
        "membership",
        "team",
        "revision",
        "dependency",
    ],
)
def test_rejects_unsafe_denial_actor_before_membership_removal(actor, wrong):
    config, login, row, snapshot, calls, fixtures = actor
    if wrong == "owner":
        config["product_denial_actor"]["user_id"] = "admin-user"
    elif wrong == "tenant":
        config["product_denial_actor"]["org_id"] = "shared"
    elif wrong == "intent":
        config["credential_secret"] = "shared-secret"
    elif wrong == "fixture":
        fixtures["non_admin_username"] = "shared@adp-eval.example"
    elif wrong == "token":
        login["access_token"] = "admin-token"
    elif wrong == "subject":
        login["id_token"] = "invalid"
    elif wrong == "canonical":
        row["cognito_sub"] = "other-sub"
    elif wrong == "membership":
        snapshot["resource"]["membership_status"] = "revoked"
    elif wrong == "team":
        snapshot["resource"]["team_id"] = "other-team"
    elif wrong == "revision":
        snapshot["revision"] = "invalid"
    else:
        snapshot["delete_permitted"] = False
    with pytest.raises(common.RemoteError):
        product_denial_actor.prepare(config, "admin-token", ORG, "admin-user")
    assert not any(method == "DELETE" for _path, method, _token in calls)


def test_revocation_without_fresh_observation_never_reports_success(actor):
    config, _login, _row, snapshot, _calls, _fixtures = actor
    _, revoke = product_denial_actor.prepare(config, "admin-token", ORG, "admin-user")
    config["test_stale_readback"] = True
    with pytest.raises(common.RemoteError, match="not independently observed"):
        revoke()
