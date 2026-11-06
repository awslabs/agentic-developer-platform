"""Bind a pre-authorized ordinary editor to a run-owned Product through its public API."""

from copy import deepcopy
from urllib.parse import quote
from uuid import uuid4

from tests.e2e.new_ui.product_fixture_ports import checked_json


def _member_fields(resource):
    return {key: value for key, value in resource.items() if key != "updated_at"}


def restore_scope_owner_role(provider):
    state = provider.state
    if not state.document.get("scope_owner_role_restore_required"):
        return
    baseline = state.document["membership"]["resource"]
    current = provider.membership.read()["resource"]
    if _member_fields({**current, "role": "member"}) != _member_fields(
        baseline
    ) or current.get("role") not in {
        "member",
        "org_admin",
    }:
        raise ValueError("refusing to overwrite an externally changed Product owner")
    if current["role"] == "org_admin":
        checked_json(
            provider.request.put(
                provider.membership.org_path
                + "/users/"
                + quote(provider.binding["actor_id"], safe=""),
                headers=provider.headers(provider.usage_control),
                data={"role": "member"},
                timeout=30_000,
            )
        )
    if _member_fields(provider.membership.read()["resource"]) != _member_fields(
        baseline
    ):
        raise RuntimeError("Product owner role was not restored before scope cleanup")
    state.save({**state.document, "scope_owner_role_restore_required": False})


def _elevate_scope_owner(provider):
    state = provider.state
    restore_scope_owner_role(provider)
    if _member_fields(provider.membership.read()["resource"]) != _member_fields(
        state.document["membership"]["resource"]
    ):
        raise ValueError(
            "Product owner differs from the protected ordinary-member baseline"
        )
    state.save({**state.document, "scope_owner_role_restore_required": True})
    checked_json(
        provider.request.put(
            provider.membership.org_path
            + "/users/"
            + quote(provider.binding["actor_id"], safe=""),
            headers=provider.headers(provider.usage_control),
            data={"role": "org_admin"},
            timeout=30_000,
        )
    )
    if provider.membership.read()["resource"].get("role") != "org_admin":
        raise RuntimeError(
            "Product owner was not authorized for shared-team assignment"
        )


def share_scope_team(provider, fixture):
    editor = provider.editor
    if editor is None or provider.editor_membership is None:
        raise ValueError("B03 requires a verified ordinary scope editor")
    state = provider.state
    saved = state.document.get("scope")
    if (
        not isinstance(saved, dict)
        or saved.get("cleanup_pending") is not True
        or saved.get("product", {}).get("product_id") != fixture.get("product_id")
        or editor.get("actor_id") != provider.binding["editor_id"]
        or editor.get("tenant_id") != fixture.get("tenant_id")
    ):
        raise ValueError("Scope editor differs from the durable owned fixture")
    scope_ids = [scope["project_id"] for scope in fixture["scopes"]]
    if len(set(scope_ids)) != 2:
        raise ValueError("Scope editor requires two distinct owned Projects")
    root = (
        provider.base_url + "/api/v1/products/" + quote(fixture["product_id"], safe="")
    )
    owner_headers = provider.headers(provider.owner)
    editor_headers = provider.headers(editor)

    def profile(project_id, headers):
        response = provider.request.get(
            root + "/projects/" + quote(project_id, safe=""),
            headers=headers,
            timeout=30_000,
        )
        if response.status != 200:
            raise RuntimeError("Shared scope is not accessible to the ordinary editor")
        value = response.json()
        if (
            value.get("tenant_id") != fixture["tenant_id"]
            or value.get("product_id") != fixture["product_id"]
            or value.get("project_id") != project_id
            or value.get("product", {}).get("owner_ref")
            != {"kind": "user", "id": fixture["actor_id"]}
        ):
            raise ValueError("Scope ownership changed during editor setup")
        return value

    first = profile(scope_ids[0], owner_headers)
    intent = saved.get("team_share")
    team_id = provider.binding["editor_team_id"]
    if intent is None:
        if first["product"].get("team_id") is not None:
            raise ValueError("Shared-team assignment was not journaled")
        intent = {
            "key": uuid4().hex,
            "revision": first["revision"],
            "team_id": team_id,
            "project_id": scope_ids[0],
            "product_id": fixture["product_id"],
            "previous_team_id": None,
        }
        updated = deepcopy(saved)
        updated["team_share"] = intent
        state.save({**state.document, "scope": updated})
    elif (
        intent.get("team_id") != team_id
        or intent.get("project_id") != scope_ids[0]
        or intent.get("product_id") != fixture["product_id"]
        or intent.get("previous_team_id") is not None
        or first["product"].get("team_id") not in (None, team_id)
    ):
        raise ValueError(
            "Shared-team replay differs from its original protected intent"
        )
    try:
        _elevate_scope_owner(provider)
        changed = checked_json(
            provider.request.patch(
                root + "/projects/" + quote(scope_ids[0], safe=""),
                headers={
                    **owner_headers,
                    "Idempotency-Key": intent["key"],
                    "If-Profile-Revision": str(intent["revision"]),
                },
                data={"team_id": team_id},
                timeout=30_000,
            )
        )
    finally:
        restore_scope_owner_role(provider)
    if changed.get("product", {}).get("team_id") != team_id:
        raise ValueError(
            "Shared-team receipt did not assign the authorized editor's team"
        )
    for scope in fixture["scopes"]:
        owned = profile(scope["project_id"], owner_headers)
        visible = profile(scope["project_id"], editor_headers)
        if (
            owned["product"] != visible["product"]
            or owned["product"].get("team_id") != team_id
            or owned["configuration"] != visible["configuration"]
            or owned["progress"] != visible["progress"]
            or owned["configuration"] != scope["configuration"]
        ):
            raise ValueError(
                "Ordinary editor cannot observe the same bound scope without a data leak"
            )
    return editor
