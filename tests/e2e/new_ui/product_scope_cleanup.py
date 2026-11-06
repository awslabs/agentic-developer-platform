"""Durable compensation of owned browser scope fixtures."""

from copy import deepcopy
from urllib.parse import quote
from uuid import uuid4

from tests.e2e.new_ui.product_scope_actors import restore_scope_owner_role


def prepare_scope_retirement(provider, case):
    state = provider.state
    saved = state.document.get("scope")
    if (
        not isinstance(saved, dict)
        or saved.get("cleanup_pending") is not True
        or saved.get("case") != case
    ):
        raise ValueError(
            "scope associations require the original run-owned browser case"
        )
    product = saved["product"]
    second = saved["receipts"].get("second_project", {}).get("project_id")
    first = product["project_id"]
    if not second or second == first or product.get("product_id") is None:
        raise ValueError("Scope retirement requires both original Project receipts")
    document, manifest = provider.run(
        case, state.document["cases"][case]["document"]
    )._read()
    if (
        manifest is None
        or manifest.get("product_id") != product["product_id"]
        or manifest.get("project_id") != first
    ):
        raise ValueError(
            "Scope retirement Product differs from its original create intent"
        )
    if manifest.get("scope") is None:
        root = (
            provider.base_url
            + "/api/v1/products/"
            + quote(product["product_id"], safe="")
        )
        for index, project_id in enumerate((first, second)):
            source = saved["receipts"].get(f"source_{index}")
            configured = saved["receipts"].get(f"configuration_{index}")
            if (
                not isinstance(source, dict)
                or source.get("project_id") != project_id
                or not source.get("id")
                or not isinstance(configured, dict)
            ):
                raise ValueError("Scope setup has unresolved creation receipts")
            response = provider.request.get(
                root + "/projects/" + quote(project_id, safe=""),
                headers=provider.headers(provider.owner),
                timeout=30_000,
            )
            if response.status != 200:
                raise RuntimeError(
                    "Owned Project cannot be reconciled before retirement"
                )
            profile = response.json()
            selection = saved["inputs"][index]["selection"]
            reference = {
                "tenant_id": state.binding["tenant_id"],
                "project_id": project_id,
                "source_connection_id": selection["connection_ref"]["id"],
                "record_type": "repository",
                "record_id": selection["native_id"],
                "record_revision": saved["inputs"][index]["record_revision"],
            }
            if (
                profile.get("product_id") != product["product_id"]
                or profile.get("project_id") != project_id
                or profile.get("tenant_id") != state.binding["tenant_id"]
                or profile.get("product", {}).get("owner_ref")
                != {"kind": "user", "id": state.binding["actor_id"]}
                or profile.get("configuration", {}).get("basic", {}).get("repositories")
                != [reference]
                or profile.get("configuration", {})
                .get("financials", {})
                .get("release_target_date")
                != saved["inputs"][index]["release_target_date"]
            ):
                raise ValueError("Scope Project differs from its original ownership")
        updated = deepcopy(saved)
        updated.setdefault("retire_key", uuid4().hex)
        state.save({**state.document, "scope": updated})

    restore_scope_owner_role(provider)
    if provider.editor_membership is not None:
        provider.editor_membership.restore(state)
    provider.membership.restore(state)
    saved = state.document["scope"]
    expected = {
        "project_id": second,
        "intents": saved["intents"],
        "inputs": saved["inputs"],
        "sources": [saved["receipts"][f"source_{index}"]["id"] for index in range(2)],
        "team_share": saved.get("team_share"),
        "editor_id": state.binding.get("editor_id"),
        "retire_key": saved["retire_key"],
    }
    if manifest.get("scope") not in (None, expected):
        raise ValueError("Scope retirement differs from its durable compensation intent")
    mode = state.binding.get("scope_fixture_mode")
    if manifest.get("scope_fixture_mode") not in (None, mode) or (
        manifest.get("scope") is not None and manifest.get("scope_fixture_mode") != mode
    ):
        raise ValueError("Scope retirement differs from its protected fixture mode")
    if manifest.get("scope") is None:
        retirement = {**manifest, "scope": expected}
        if mode == "connected_scope":
            retirement["scope_fixture_mode"] = mode
        provider.run(case, document).store.save(state.binding["run_id"], document, retirement)


def confirm_scope_retirement(provider, receipt):
    state = provider.state
    scope = state.document["scope"]
    if (
        receipt.get("scope_project_id")
        != scope["receipts"]["second_project"]["project_id"]
        or receipt.get("retire_key") != scope["retire_key"]
        or receipt.get("fixture_removed") is not True
    ):
        raise RuntimeError(
            "Protected retirement did not confirm owned scope compensation"
        )
    product_id = quote(scope["product"]["product_id"], safe="")
    second_id = quote(scope["receipts"]["second_project"]["project_id"], safe="")
    response = provider.request.get(
        provider.base_url + "/api/v1/products/" + product_id + "/projects/" + second_id,
        headers=provider.headers(provider.observer),
        timeout=30_000,
    )
    if response.status != 404:
        raise RuntimeError(
            "Second retired Project remains readable to the owning observer"
        )
    updated = deepcopy(scope)
    updated["cleanup_pending"] = False
    updated["cleanup_receipt"] = receipt
    state.save({**state.document, "scope": updated})
