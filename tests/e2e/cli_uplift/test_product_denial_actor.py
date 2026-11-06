"""An ordinary Product actor is bound by the gateway, not by an asserted JWT."""

import base64
import json
import sys
from pathlib import Path

import pytest

from tests.e2e.cli_uplift import contracts

sys.path.insert(0, str(Path(__file__).resolve().parent / "remote"))
import common
import install_auth
import product_denial_actor


RUN_ID = "adp-e2e-20261020-113000-abcdef"
ORG = RUN_ID + "-product-" + "a" * 24
USERNAME = RUN_ID + "-product-user@adp-eval.example"
SUBJECT = "ordinary-cognito-subject"


def token(subject=SUBJECT):
    claims = base64.urlsafe_b64encode(json.dumps({"sub": subject}).encode()).decode()
    return "header." + claims.rstrip("=") + ".signature"


@pytest.fixture
def actor(monkeypatch):
    config = {
        "evaluation_id": RUN_ID,
        "product_workspace_id": ORG,
        "credential_secret": f"adp/cli-uplift-eval/{RUN_ID}-product-fixtures",
    }
    evidence = {"onboard": {"org_id": ORG, "team_id": "owned-team"}}
    calls = []
    inventory = []
    created = {
        "id": "run-user-id",
        "email": USERNAME,
        "org_id": ORG,
        "team_id": "owned-team",
        "role": "member",
        "cognito_sub": SUBJECT,
    }
    login = {"access_token": "ordinary-token", "id_token": token()}
    fixtures = {
        "non_admin_username": USERNAME,
        "non_admin_password": "private-password",
    }
    monkeypatch.setattr(common, "clean_env", lambda _config: {})
    monkeypatch.setattr(
        common, "fixture_secret", lambda _config, _env, key: fixtures[key]
    )

    def api(_config, path, bearer, *, method="GET", body=None, expect=(200,)):
        calls.append((path, bearer, method, body))
        if path == "/auth/cli/password":
            assert method == "POST" and bearer == "admin-token" and expect == (200,)
            assert body == {
                "username": USERNAME,
                "password": fixtures["non_admin_password"],
            }
            return 200, login
        assert path == f"/api/admin/identity/organizations/{ORG}/users"
        assert bearer == "admin-token"
        if method == "POST":
            assert expect == (201,)
            assert body["cognito_identity"] == {
                "username": USERNAME,
                "expected_sub": SUBJECT,
            }
            assert body["team_id"] == "owned-team" and body["role"] == "member"
            assert body["send_invite"] is False
            inventory.append(created.copy())
            return 201, created.copy()
        assert expect == (200,)
        return 200, {
            "users": []
            if inventory and config.get("test_missing_readback")
            else list(inventory)
        }

    monkeypatch.setattr(common, "api", api)
    return config, evidence, calls, inventory, created, login, fixtures


def test_registers_ordinary_actor_against_independent_canonical_read(actor):
    config, evidence, calls, _inventory, created, login, fixtures = actor
    install_auth._register_denial_actor(
        config, evidence, "admin-token", ORG, "owned-team"
    )
    assert [method for _path, _bearer, method, _body in calls] == [
        "POST",
        "GET",
        "POST",
        "GET",
    ]
    assert evidence["denial_actor"] == {
        "org_id": ORG,
        "user_id": created["id"],
        "username": USERNAME,
        "cognito_sub_bound": True,
    }
    assert not any(
        secret in json.dumps(evidence)
        for secret in (
            fixtures["non_admin_password"],
            login["id_token"],
            login["access_token"],
        )
    )


@pytest.mark.parametrize(
    "wrong",
    [
        "workspace",
        "secret",
        "team",
        "username",
        "login",
        "subject",
        "prior",
        "identity",
        "readback",
    ],
)
def test_refuses_incomplete_or_foreign_actor_without_claiming_evidence(actor, wrong):
    config, evidence, calls, inventory, created, login, fixtures = actor
    if wrong == "workspace":
        config["product_workspace_id"] = "foreign"
    elif wrong == "secret":
        config["credential_secret"] = "shared-secret"
    elif wrong == "team":
        evidence["onboard"]["team_id"] = "foreign"
    elif wrong == "username":
        fixtures["non_admin_username"] = "other-user@adp-eval.example"
    elif wrong == "login":
        login["access_token"] = "admin-token"
    elif wrong == "subject":
        login["id_token"] = "bad-token"
    elif wrong == "prior":
        inventory.append({"email": USERNAME, "id": "other-id"})
    elif wrong == "identity":
        created["cognito_sub"] = "foreign-subject"
    else:
        config["test_missing_readback"] = True
    with pytest.raises(common.RemoteError):
        install_auth._register_denial_actor(
            config, evidence, "admin-token", ORG, "owned-team"
        )
    assert "denial_actor" not in evidence
    if wrong in {
        "workspace",
        "secret",
        "team",
        "username",
        "login",
        "subject",
        "prior",
    }:
        assert not any(
            method == "POST" and path.endswith("/users")
            for path, _bearer, method, _body in calls
        )


ACTOR_ID = "40e3ff6c-51de-4344-a1f4-207a5c35d5b0"
OWNER_ID = "owner-row-id"
TEAM = "owned-team"
ACTIVE_REVISION = "a" * 64
REVOKED_REVISION = "b" * 64
DENIAL_ACTOR = {
    "org_id": ORG,
    "username": USERNAME,
    "user_id": ACTOR_ID,
    "cognito_sub_bound": True,
}
DENIAL_SECRETS = {
    "non_admin_username": USERNAME,
    "non_admin_password": "private-password",
}
MEMBER_ROW = {
    "id": ACTOR_ID,
    "org_id": ORG,
    "email": USERNAME,
    "role": "member",
    "cognito_sub": SUBJECT,
}
ACTIVE = {
    "id": ACTOR_ID,
    "org_id": ORG,
    "revision": ACTIVE_REVISION,
    "resource": {"email": USERNAME, "membership_status": "active", "team_id": TEAM},
    "delete_permitted": True,
}
REVOKED = {
    "id": ACTOR_ID,
    "org_id": ORG,
    "revision": REVOKED_REVISION,
    "resource": {"email": USERNAME, "membership_status": "revoked", "team_id": ""},
}


@pytest.fixture
def denial(monkeypatch):
    """`prepare` driven against a gateway that answers its own routes only."""
    config = {
        "evaluation_id": RUN_ID,
        "product_denial_actor": DENIAL_ACTOR,
        "product_actor_id": OWNER_ID,
        "product_team_id": TEAM,
        "product_cases": "workspace,api_denial_oracle",
        "credential_secret": f"adp/cli-uplift-eval/{RUN_ID}-product-fixtures",
    }
    requests = []
    state = {"revoked": False}
    monkeypatch.setattr(common, "clean_env", lambda _config: {})
    monkeypatch.setattr(
        common, "fixture_secret", lambda _config, _env, key: DENIAL_SECRETS[key]
    )

    def api(_config, path, bearer, *, method="GET", body=None, expect=(200,)):
        assert bearer == "owner-token"
        requests.append((method, path))
        if path == "/auth/cli/password":
            assert method == "POST"
            assert body == {
                "username": USERNAME,
                "password": DENIAL_SECRETS["non_admin_password"],
            }
            return 200, {"access_token": "member-token", "id_token": token()}
        if path.endswith("/users"):
            return 200, {"users": [MEMBER_ROW]}
        if method == "DELETE":
            state["revoked"] = True
        snapshot = REVOKED if state["revoked"] else ACTIVE
        return 200, json.loads(json.dumps(snapshot))

    monkeypatch.setattr(common, "api", api)
    return config, requests


def test_denial_actor_uses_only_routes_the_gateway_actually_mounts(denial):
    """Every path prepare and revoke send is a route of the deployed app.

    The member snapshot and its revocation were spelled
    `/api/admin/organizations/<org>/hierarchy/member/<member>`, which is not a
    route at all: deploy run 39969181872 answered it HTTP 404 and failed the
    protected Product driver at `purpose=product_v01, stage=start`, exactly as
    #8242's run 39767400131 did for the team inventory. `app.py` mounts each
    UNIT_MODULES router with no prefix of its own, `src/admin/routes.py`
    declares prefix `/admin`, and `src/admin/identity/router.py` declares
    prefix `/api/admin/identity` — so only the canonical identity read belongs
    under `/api/admin`.
    """
    config, requests = denial
    member_token, revoke = product_denial_actor.prepare(
        config, "owner-token", ORG, OWNER_ID
    )
    assert member_token == "member-token"
    assert revoke() == {"user_id": ACTOR_ID, "membership_revoked": True}

    mounted = contracts.admin_routes()
    assert (
        contracts.mounted_route("GET", f"/admin/organizations/{ORG}/teams", mounted)
        == "/admin/organizations/{org_id}/teams"
    )
    assert contracts.mounted_route(
        "GET", f"/api/admin/identity/organizations/{ORG}/users", mounted
    )
    for wrong in (
        f"/api/admin/organizations/{ORG}/teams",
        f"/api/admin/organizations/{ORG}/hierarchy/member/{ACTOR_ID}",
    ):
        assert contracts.mounted_route("GET", wrong, mounted) is None

    # The hierarchy routes live on a sub-router `src/admin/routes.py` includes
    # into its own `/admin` router, which contracts deliberately does not walk.
    # So the organization stem is pinned to the mounted admin table above, and
    # the tail to that module's `prefix="/organizations/{org_id}/hierarchy"`
    # with its `@router.get("/{kind}/{key}")` and `@router.delete(...)`.
    member = f"/admin/organizations/{ORG}/hierarchy/member/{ACTOR_ID}"
    assert [request for request in requests if "hierarchy" in request[1]] == [
        ("GET", member),
        ("DELETE", member + "?expected_revision=" + ACTIVE_REVISION),
        ("GET", member),
    ]
    for method, path in requests:
        stem = path.split("?", 1)[0]
        assert (
            path == "/auth/cli/password"
            or contracts.mounted_route(method, path, mounted)
            or stem.startswith(f"/admin/organizations/{ORG}/hierarchy/")
        ), path
