"""Fresh Product actors use only a verified team of their own workspace."""

import sys
from pathlib import Path

import pytest

from tests.e2e.cli_uplift import contracts

sys.path.insert(0, str(Path(__file__).resolve().parent / "remote"))
import common
import install_auth


@pytest.fixture
def onboard(monkeypatch):
    calls = []
    config = {
        "created_username": "product-admin@adp-eval.example",
        "evaluation_id": "adp-e2e-20261020-abcdef",
    }
    evidence = {
        "login": {
            "org_id": "owned-org",
            "user_id": "owned-sub",
            "username": config["created_username"],
            "role": "platform_admin",
        }
    }

    def install(
        *,
        users=None,
        teams=None,
        has_more=False,
        status=200,
        total=None,
        workspace=None,
    ):
        def api(_config, path, token, *, expect, method="GET", body=None):
            assert token == "private-token"
            assert expect == ((201,) if method == "POST" else (200,))
            calls.append((path, method, body))
            if path.endswith("/teams?page=1&page_size=100"):
                if status != 200:
                    raise common.RemoteError("Teams endpoint denied")
                return 200, {
                    "items": teams,
                    "page": 1,
                    "page_size": 100,
                    "total": len(teams) if total is None else total,
                    "has_more": has_more,
                }
            if method == "POST":
                return 201, {"id": "run-user", "cognito_sub": "owned-sub"}
            if path.endswith("/owned-org"):
                return 200, workspace
            return 200, {"users": users}

        monkeypatch.setattr(common, "api", api)
        return config, evidence, calls

    return install


def test_new_workspace_uses_its_only_verified_team_without_existing_users(onboard):
    config, evidence, calls = onboard(
        users=[], teams=[{"id": "actual-team", "org_id": "owned-org"}]
    )
    install_auth._onboard(config, evidence, "private-token")
    assert calls[-1][1] == "POST"
    assert calls[-1][2]["team_id"] == "actual-team"
    assert calls[-1][2]["cognito_identity"] == {
        "username": config["created_username"],
        "expected_sub": "owned-sub",
    }
    assert evidence["resources"] == [["adp_user", "owned-org/run-user"]]


def test_team_inventory_uses_a_route_the_gateway_actually_mounts(onboard):
    """Every admin path this worker sends is a route of the deployed app.

    The fixture above matches only the `/teams?page=1&page_size=100` suffix, so
    any prefix passes the rest of this module while the deployment answers 404:
    `/api/admin/identity/organizations/...` did in run 39733378117 and
    `/api/admin/organizations/...` did in run 39767400131. Asserting one more
    literal would only record the next guess, so the expectation is derived from
    the gateway's own routers — `app.py` mounts each UNIT_MODULES router with no
    prefix of its own, `src/admin/routes.py` declares prefix `/admin`, and
    `src/admin/identity/router.py` declares prefix `/api/admin/identity`.
    """
    mounted = contracts.admin_routes()
    config, evidence, calls = onboard(
        users=[], teams=[{"id": "actual-team", "org_id": "owned-org"}]
    )
    install_auth._onboard(config, evidence, "private-token")

    teams = [path for path, _method, _body in calls if "/teams" in path]
    assert teams == ["/admin/organizations/owned-org/teams?page=1&page_size=100"]
    assert (
        contracts.mounted_route("GET", teams[0], mounted)
        == "/admin/organizations/{org_id}/teams"
    )
    for path, method, _body in calls:
        assert contracts.mounted_route(method, path, mounted), path

    # The identity-scoped reads and the account write stay on the identity
    # router, which really does serve them.
    identity_scoped = [path for path, _method, _body in calls if "/teams" not in path]
    assert identity_scoped
    assert all(
        path.startswith("/api/admin/identity/organizations/owned-org")
        for path in identity_scoped
    )

    # Both spellings the live deployment refused are not routes at all.
    for wrong in (
        "/api/admin/organizations/owned-org/teams?page=1&page_size=100",
        "/api/admin/identity/organizations/owned-org/teams?page=1&page_size=100",
    ):
        assert contracts.mounted_route("GET", wrong, mounted) is None


def test_existing_workspace_uses_a_listed_team_from_existing_users(onboard):
    config, evidence, calls = onboard(
        users=[{"team_id": "second-team"}],
        teams=[
            {"id": "first-team", "org_id": "owned-org"},
            {"id": "second-team", "org_id": "owned-org"},
        ],
    )
    install_auth._onboard(config, evidence, "private-token")
    assert calls[-1][2]["team_id"] == "second-team"


@pytest.mark.parametrize(
    "users,teams,has_more,status",
    [
        ([], [], False, 200),
        (
            [],
            [
                {"id": "first", "org_id": "owned-org"},
                {"id": "second", "org_id": "owned-org"},
            ],
            False,
            200,
        ),
        ([], [{"id": "foreign", "org_id": "other-org"}], False, 200),
        ([], [{"id": "first", "org_id": "owned-org"}], True, 200),
        (
            [{"team_id": "missing"}],
            [{"id": "first", "org_id": "owned-org"}],
            False,
            200,
        ),
        ([], [{"id": "first", "org_id": "owned-org"}], False, 403),
    ],
)
def test_unverified_or_ambiguous_team_refuses_before_actor_creation(
    onboard, users, teams, has_more, status
):
    config, evidence, calls = onboard(
        users=users, teams=teams, has_more=has_more, status=status
    )
    with pytest.raises(common.RemoteError, match="team|Teams"):
        install_auth._onboard(config, evidence, "private-token")
    assert not any(method == "POST" for _path, method, _body in calls)
    assert not evidence.get("resources")


def test_partial_team_inventory_refuses_before_actor_creation(onboard):
    config, evidence, calls = onboard(
        users=[], teams=[{"id": "first", "org_id": "owned-org"}], total=2
    )
    with pytest.raises(common.RemoteError, match="inventory"):
        install_auth._onboard(config, evidence, "private-token")
    assert not any(method == "POST" for _path, method, _body in calls)
    assert not evidence.get("resources")


@pytest.mark.parametrize(
    "wrong", ["tenant", "name", "archived", "selection", "username", "role"]
)
def test_product_actor_requires_verified_run_workspace_before_onboarding(
    onboard, wrong
):
    workspace = {
        "id": "owned-org",
        "name": "eval-regression-adp-e2e-20261020-abcdef",
        "settings": {},
    }
    config, evidence, calls = onboard(
        users=[],
        teams=[{"id": "actual-team", "org_id": "owned-org"}],
        workspace=workspace,
    )
    config.update(
        product_cases="api_product_lifecycle", product_workspace_id="owned-org"
    )
    if wrong == "tenant":
        config["product_workspace_id"] = "another-org"
    elif wrong == "name":
        workspace["name"] = "shared-platform"
    elif wrong == "archived":
        workspace["settings"] = {"status": "archived"}
    elif wrong == "username":
        evidence["login"]["username"] = "other-admin@adp-eval.example"
    elif wrong == "role":
        evidence["login"]["role"] = "org_admin"
    else:
        config["product_cases"] = "unreviewed_case"
    with pytest.raises(common.RemoteError, match="Product actor"):
        install_auth._onboard(config, evidence, "private-token")
    assert not any(method == "POST" for _path, method, _body in calls)
    assert not evidence.get("resources")


def test_product_actor_registers_only_after_workspace_read(onboard):
    workspace = {
        "id": "owned-org",
        "name": "eval-regression-adp-e2e-20261020-abcdef",
        "settings": {},
    }
    config, evidence, calls = onboard(
        users=[],
        teams=[{"id": "actual-team", "org_id": "owned-org"}],
        workspace=workspace,
    )
    config.update(
        product_cases="api_product_lifecycle", product_workspace_id="owned-org"
    )
    install_auth._onboard(config, evidence, "private-token")
    assert calls[0][0].endswith("/owned-org")
    assert calls[-1][1] == "POST"


def test_selected_denial_case_registers_actor_after_admin(monkeypatch, onboard):
    workspace = {
        "id": "owned-org",
        "name": "eval-regression-adp-e2e-20261020-abcdef",
        "settings": {},
    }
    config, evidence, calls = onboard(
        users=[],
        teams=[{"id": "actual-team", "org_id": "owned-org"}],
        workspace=workspace,
    )
    config.update(
        product_cases="api_product_lifecycle,api_denial_oracle",
        product_workspace_id="owned-org",
    )
    registrations = []
    monkeypatch.setattr(
        install_auth,
        "_register_denial_actor",
        lambda *args: registrations.append((args, list(calls))),
    )
    install_auth._onboard(config, evidence, "private-token")
    assert len(registrations) == 1
    assert registrations[0][0] == (
        config,
        evidence,
        "private-token",
        "owned-org",
        "actual-team",
    )
    assert registrations[0][1][-1][1] == "POST"
    assert evidence["onboard"]["cognito_sub_bound"] is True
