"""Create one isolated Product workspace using the instance's administrator session."""

from __future__ import annotations

import re
from urllib.parse import quote

import common


def execute(config, evidence):
    run_id = config.get("evaluation_id")
    workspace_id = config.get("product_workspace_id")
    common.require(
        isinstance(run_id, str)
        and re.fullmatch(r"adp-e2e-[0-9]{8}-[0-9]{6}-[a-f0-9]{6}", run_id)
        and isinstance(workspace_id, str)
        and re.fullmatch(re.escape(run_id) + r"-product-[a-f0-9]{24}", workspace_id),
        "Product workspace is not a unique run-owned evaluation identity",
    )
    owner = common.load_session(config)
    common.require(
        isinstance(owner.get("user_id"), str)
        and bool(owner["user_id"])
        and isinstance(owner.get("org_id"), str)
        and bool(owner["org_id"])
        and owner["org_id"] != workspace_id,
        "Product workspace bootstrap needs a separate administrator",
    )
    token = owner["access_token"]
    status, identity = common.api(
        config, "/auth/cli/admin-session", token, expect=(200,)
    )
    common.require(
        status == 200
        and isinstance(identity, dict)
        and identity.get("verified") is True
        and identity.get("role") == "platform_admin"
        and identity.get("user_id") == owner["user_id"]
        and identity.get("org_id") == owner["org_id"],
        "Product workspace bootstrap is not the verified platform administrator",
    )
    base = "/api/admin/identity/organizations"
    path = base + "/" + quote(workspace_id, safe="")
    status, _ = common.api(config, path, token, expect=(404,))
    common.require(status == 404, "Product workspace identity already exists")
    name = "eval-regression-" + run_id
    status, created = common.api(
        config,
        base,
        token,
        method="POST",
        body={"id": workspace_id, "name": name},
        expect=(201,),
    )
    common.require(
        status == 201
        and isinstance(created, dict)
        and created.get("id") == workspace_id
        and created.get("name") == name,
        "Canonical Product workspace creation returned a different tenant",
    )
    status, workspace = common.api(config, path, token, expect=(200,))
    common.require(
        status == 200
        and isinstance(workspace, dict)
        and workspace.get("id") == workspace_id
        and workspace.get("name") == name
        and isinstance(workspace.get("settings"), dict)
        and workspace["settings"].get("status") != "archived",
        "New Product workspace was not readable through the canonical API",
    )
    status, users = common.api(config, path + "/users", token, expect=(200,))
    common.require(
        status == 200 and isinstance(users, dict) and users.get("users") == [],
        "New Product workspace already contains users",
    )
    # The team collection is mounted at `/admin/organizations/<org>/teams`: it is
    # a route of the admin router (`modules/gateway/src/admin/routes.py`, prefix
    # `/admin`), which `modules/gateway/src/app.py` includes without any further
    # prefix. It is NOT under the identity prefix that serves this workspace read
    # and its `users` collection, and it carries no `/api` segment at all. Both
    # wrong spellings were observed live: the identity-prefixed form answered HTTP
    # 404 (run 39733378117) and `/api/admin/organizations/...` answered HTTP 404
    # (run 39767400131). `tests/e2e/cli_uplift/test_product_workspace.py` pins
    # this path to the route table derived from the gateway's own sources.
    teams_path = "/admin/organizations/" + quote(workspace_id, safe="") + "/teams"
    status, listing = common.api(
        config, teams_path + "?page=1&page_size=100", token, expect=(200,)
    )
    common.require(
        status == 200
        and isinstance(listing, dict)
        and listing.get("page") == 1
        and listing.get("page_size") == 100
        and listing.get("total") == 1
        and listing.get("has_more") is False
        and isinstance(listing.get("items"), list)
        and len(listing["items"]) == 1
        and isinstance(listing["items"][0], dict)
        and listing["items"][0].get("org_id") == workspace_id
        and isinstance(listing["items"][0].get("id"), str)
        and bool(listing["items"][0]["id"]),
        "Canonical Product workspace has no isolated default team",
    )
    evidence.update(
        success=True,
        stage="complete",
        workspace_id=workspace_id,
        team_id=listing["items"][0]["id"],
    )


if __name__ == "__main__":
    raise SystemExit(common.run_script(execute))
