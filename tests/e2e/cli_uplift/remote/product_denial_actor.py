"""Authenticate a run-owned member and revoke only its verified membership."""

from __future__ import annotations

import base64
import binascii
import json
import re
from urllib.parse import quote

import common


def prepare(config, owner_token, tenant_id, owner_user_id):
    run_id = config.get("evaluation_id")
    actor = config.get("product_denial_actor")
    username = f"{run_id}-product-user@adp-eval.example"
    common.require(
        isinstance(run_id, str)
        and re.fullmatch(r"adp-e2e-[0-9]{8}-[0-9]{6}-[a-f0-9]{6}", run_id)
        and isinstance(actor, dict)
        and actor.get("org_id") == tenant_id
        and actor.get("username") == username
        and isinstance(actor.get("user_id"), str)
        and actor["user_id"]
        and actor["user_id"] != owner_user_id
        and actor.get("cognito_sub_bound") is True
        and owner_user_id == config.get("product_actor_id")
        and config.get("credential_secret")
        == f"adp/cli-uplift-eval/{run_id}-product-fixtures"
        and "api_denial_oracle" in config.get("product_cases", "").split(","),
        "Product denial needs a run-owned distinct verified member",
    )
    environment = common.clean_env(config)
    common.require(
        common.fixture_secret(config, environment, "non_admin_username") == username,
        "Product denial member does not match the run-owned secret",
    )
    password = common.fixture_secret(config, environment, "non_admin_password")
    status, login = common.api(
        config,
        "/auth/cli/password",
        owner_token,
        method="POST",
        body={"username": username, "password": password},
        expect=(200,),
    )
    common.require(
        status == 200
        and isinstance(login, dict)
        and isinstance(login.get("access_token"), str)
        and login["access_token"]
        and login["access_token"] != owner_token
        and isinstance(login.get("id_token"), str)
        and login["id_token"],
        "Product denial member did not authenticate independently",
    )
    try:
        claims = json.loads(
            base64.urlsafe_b64decode(login["id_token"].split(".")[1] + "===")
        )
        subject = claims["sub"]
    except (IndexError, KeyError, TypeError, ValueError, UnicodeError, binascii.Error):
        raise common.RemoteError(
            "Product denial member has no Cognito subject"
        ) from None
    common.require(
        isinstance(subject, str) and subject and len(subject) <= 255,
        "Product denial member has no bounded Cognito subject",
    )
    base = "/api/admin/identity/organizations/" + quote(tenant_id, safe="")
    _, inventory = common.api(config, base + "/users", owner_token, expect=(200,))
    common.require(
        isinstance(inventory, dict)
        and isinstance(inventory.get("users"), list)
        and len(
            [
                row
                for row in inventory["users"]
                if isinstance(row, dict)
                and row.get("id") == actor["user_id"]
                and row.get("org_id") == tenant_id
                and row.get("email") == username
                and row.get("role") == "member"
                and row.get("cognito_sub") == subject
            ]
        )
        == 1,
        "Product denial member is absent from the canonical identity inventory",
    )
    # The hierarchy surface is mounted by the `/admin` router, not under
    # `/api/admin`: `app.py` includes `src.admin.routes` with no prefix of its
    # own, that router declares `prefix="/admin"` and includes
    # `src.admin.hierarchy`, whose own prefix is
    # `/organizations/{org_id}/hierarchy`. Only the canonical identity read
    # above lives under `/api/admin/identity`, because that is a second router
    # with its own prefix. `/api/admin/organizations/...` is not a route at all,
    # so deploy run 39969181872 failed with `/api/admin/organizations/
    # <workspace>/hierarchy/member/<member> returned HTTP 404, expected
    # (200,)` — the same wrong prefix #8242 observed live in run 39767400131.
    path = (
        "/admin/organizations/"
        + quote(tenant_id, safe="")
        + "/hierarchy/member/"
        + quote(actor["user_id"], safe="")
    )
    _, snapshot = common.api(config, path, owner_token, expect=(200,))
    common.require(
        isinstance(snapshot, dict)
        and snapshot.get("id") == actor["user_id"]
        and snapshot.get("org_id") == tenant_id
        and isinstance(snapshot.get("resource"), dict)
        and snapshot["resource"].get("email") == username
        and snapshot["resource"].get("membership_status") == "active"
        and snapshot["resource"].get("team_id") == config.get("product_team_id")
        and isinstance(snapshot.get("revision"), str)
        and re.fullmatch(r"[a-f0-9]{64}", snapshot["revision"])
        and snapshot.get("delete_permitted") is True,
        "Product denial member has no active revision-checked membership",
    )

    def revoke():
        _, result = common.api(
            config,
            path + "?expected_revision=" + snapshot["revision"],
            owner_token,
            method="DELETE",
            expect=(200,),
        )
        _, fresh = common.api(config, path, owner_token, expect=(200,))
        common.require(
            isinstance(result, dict)
            and isinstance(fresh, dict)
            and fresh == result
            and result.get("id") == actor["user_id"]
            and result.get("org_id") == tenant_id
            and result.get("revision") != snapshot["revision"]
            and isinstance(result.get("resource"), dict)
            and result["resource"].get("membership_status") == "revoked"
            and result["resource"].get("email") == username
            and result["resource"].get("team_id") == "",
            "Product denial membership removal was not independently observed",
        )
        return {"user_id": actor["user_id"], "membership_revoked": True}

    return login["access_token"], revoke
