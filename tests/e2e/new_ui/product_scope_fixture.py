"""Run-owned V04 scope inputs from real Product routes and durable browser state."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from tests.e2e.product_onboarding.product_v02_record import private_json


def load_scope_fixture(path: Path, binding: dict) -> dict:
    if path.stat().st_mode & 0o077:
        raise ValueError("Protected scope fixture must not be group or world readable")
    fixture = json.loads(path.read_text())
    if any(
        fixture.get(key) != binding[key]
        for key in ("run_id", "source_revision", "tenant_id", "actor_id")
    ):
        raise ValueError("Scope fixture differs from the protected browser run")
    if fixture.get("owned_fixture") is not True:
        raise ValueError("Scope fixture requires run ownership")
    from tests.e2e.new_ui.test_product_scopes import assert_scope_fixture_inputs

    assert_scope_fixture_inputs(fixture)
    return fixture


def _inputs_valid(inputs):
    if not isinstance(inputs, list) or len(inputs) != 2:
        raise ValueError("Exactly two scope source selections are required")
    native_ids, dates = [], []
    for item in inputs:
        if not isinstance(item, dict) or set(item) != {
            "selection",
            "record_revision",
            "release_target_date",
        }:
            raise ValueError(
                "Scope inputs require a pinned source selection and plan date"
            )
        selection = item["selection"]
        if (
            not isinstance(selection, dict)
            or set(selection)
            != {"kind", "provider", "connection_ref", "namespace", "native_id"}
            or selection["kind"] != "repository"
            or selection["provider"] != "github"
            or not isinstance(selection["connection_ref"], dict)
            or selection["connection_ref"].get("kind") != "github_installation"
            or not all(
                isinstance(value, str) and value
                for value in (
                    selection["connection_ref"].get("id"),
                    selection["namespace"],
                    selection["native_id"],
                    item["record_revision"],
                )
            )
        ):
            raise ValueError(
                "Scope sources require two authorized GitHub installation selections"
            )
        try:
            dates.append(date.fromisoformat(item["release_target_date"]).isoformat())
        except (TypeError, ValueError) as error:
            raise ValueError("Scope plans require pinned calendar dates") from error
        native_ids.append(selection["native_id"])
    if (
        len(set(native_ids)) != 2
        or len(set(dates)) != 2
    ):
        raise ValueError(
            "Scope repository and plan inputs must be distinct"
        )


def verify_scope_sources(request, base_url, owner, product, inputs):
    """Require ordinary owner visibility of each repository on its tenant connection."""
    _inputs_valid(inputs)
    if not owner.get("access_token") or not product.get("product_id") or not product.get("project_id"):
        raise ValueError("B03 source discovery requires the authenticated owned Product")
    root = (
        base_url + "/api/v1/products/" + quote(product["product_id"], safe="")
        + "/projects/" + quote(product["project_id"], safe="") + "/sources"
    )
    for item in inputs:
        selection = item["selection"]
        connection = selection["connection_ref"]["id"]
        cursor = None
        for _ in range(100):
            url = root + "?connection_kind=github_installation&connection_id=" + quote(connection, safe="") + "&limit=100"
            if cursor is not None:
                url += "&resource_cursor=" + quote(cursor, safe="")
            response = request.get(url, headers={"Authorization": "Bearer " + owner["access_token"]}, timeout=30_000)
            if response.status != 200:
                raise ValueError("B03 source connection is not authorized to the fixture owner")
            body = response.json()
            sources = body.get("items") if isinstance(body, dict) else None
            if not isinstance(sources, list) or len(sources) != 1:
                raise ValueError("B03 source discovery did not return the selected connection")
            source = sources[0]
            if (not isinstance(source, dict) or source.get("connection_ref") != selection["connection_ref"]
                    or source.get("provider") != "github" or source.get("coverage") not in {"complete", "partial"}
                    or not isinstance(source.get("resources"), list)):
                raise ValueError("B03 source discovery did not verify the tenant-owned connection")
            if any(resource.get("kind") == "repository" and resource.get("namespace") == selection["namespace"]
                   and resource.get("native_id") == selection["native_id"] for resource in source["resources"]
                   if isinstance(resource, dict)):
                break
            next_cursor = source.get("resource_cursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise ValueError("B03 repository is not accessible on the tenant-owned installation")
            cursor = next_cursor
        else:
            raise ValueError("B03 repository discovery exceeded the bounded installation inventory")


def prepare_scope_fixture(
    state, request, base_url, owner, product, inputs, path: Path, *, case="scope"
) -> dict:
    """Replay only the original intents; never create a second scope on lost response."""
    _inputs_valid(inputs)
    binding = state.binding
    if not all(
        product.get(key) for key in ("product_id", "project_id", "product_name")
    ):
        raise ValueError("The run-owned Product create receipt is required")
    if (
        not owner.get("access_token")
        or owner.get("actor_id") != binding["actor_id"]
        or owner.get("tenant_id") != binding["tenant_id"]
    ):
        raise ValueError("Scope writes require the authenticated fixture owner")
    desired = {
        "product": product,
        "inputs": inputs,
        "intents": {},
        "receipts": {},
        "cleanup_pending": True,
        "case": case,
    }
    saved = state.document.get("scope")
    if saved is None:
        state.save({**state.document, "scope": deepcopy(desired)})
    elif (
        any(saved.get(key) != desired[key] for key in ("product", "inputs", "case"))
        or saved.get("cleanup_pending") is not True
    ):
        raise ValueError(
            "Recover the original scope fixture instead of creating another intent"
        )

    product_id = quote(product["product_id"], safe="")
    root = base_url + "/api/v1/products/" + product_id
    headers = {"Authorization": "Bearer " + owner["access_token"]}

    def read(project_id):
        response = request.get(
            root + "/projects/" + quote(project_id, safe=""),
            headers=headers,
            timeout=30_000,
        )
        if response.status != 200:
            raise RuntimeError("Owned scope readback failed")
        profile = response.json()
        if (
            profile.get("product_id") != product["product_id"]
            or profile.get("project_id") != project_id
            or profile.get("tenant_id") != binding["tenant_id"]
            or profile.get("product", {}).get("owner_ref")
            != {"kind": "user", "id": binding["actor_id"]}
        ):
            raise ValueError("Scope readback differs from the original Product owner")
        return profile

    def mutate(label, method, route, body, project_id):
        scope = state.document["scope"]
        intent = scope["intents"].get(label)
        if intent is None:
            current = read(project_id)
            intent = {
                "key": uuid4().hex,
                "revision": current["revision"],
                "body": body,
                "route": route,
            }
            scope = deepcopy(scope)
            scope["intents"][label] = intent
            state.save({**state.document, "scope": scope})
        elif intent["body"] != body or intent["route"] != route:
            raise ValueError("Scope replay differs from its original durable intent")
        response = getattr(request, method)(
            root + route,
            headers={
                **headers,
                "Idempotency-Key": intent["key"],
                "If-Profile-Revision": str(intent["revision"]),
            },
            data=body,
            timeout=30_000,
        )
        if response.status not in (200, 201):
            raise RuntimeError(
                f"Original scope operation could not be reconciled: HTTP {response.status}"
            )
        receipt = response.json()
        prior = state.document["scope"]["receipts"].get(label)
        if prior is not None and prior != receipt:
            raise ValueError("Scope receipt changed on replay")
        if prior is None:
            scope = deepcopy(state.document["scope"])
            scope["receipts"][label] = receipt
            state.save({**state.document, "scope": scope})
        return receipt

    first_id = product["project_id"]
    second = mutate(
        "second_project",
        "post",
        "/projects",
        {"mode": "create", "name": "eval-scope-" + binding["run_id"]},
        first_id,
    )
    second_id = second.get("project_id")
    if not isinstance(second_id, str) or not second_id or second_id == first_id:
        raise ValueError("Second scope binding differs from the first owned Project")
    try:
        second_profile = read(second_id)
    except RuntimeError as error:
        raise ValueError(
            "Second scope binding is not independently readable"
        ) from error
    if (
        second_profile.get("project", {}).get("name")
        != "eval-scope-" + binding["run_id"]
    ):
        raise ValueError(
            "Second scope binding differs from the original run-owned intent"
        )
    project_ids = (first_id, second_id)
    for index, project_id in enumerate(project_ids):
        selection = inputs[index]["selection"]
        source = mutate(
            f"source_{index}",
            "post",
            "/projects/" + quote(project_id, safe="") + "/bindings",
            selection,
            project_id,
        )
        if (
            source.get("project_id") != project_id
            or source.get("selection", {}).get("connection_ref")
            != selection["connection_ref"]
            or source.get("status") == "restricted"
            or not source.get("id")
        ):
            raise ValueError("Scope source is not authorized to the selected Project")
        reference = {
            "tenant_id": binding["tenant_id"],
            "project_id": project_id,
            "source_connection_id": selection["connection_ref"]["id"],
            "record_type": "repository",
            "record_id": selection["native_id"],
            "record_revision": inputs[index]["record_revision"],
        }
        configuration = {
            "basic": {"repositories": [reference]},
            "financials": {"release_target_date": inputs[index]["release_target_date"]},
        }
        if index == 1:
            configuration["applicability"] = [
                {
                    "check_id": "financials",
                    "state": "later",
                    "reason": "Review this scope's financial plan later.",
                }
            ]
        configured = mutate(
            f"configuration_{index}",
            "patch",
            "/projects/" + quote(project_id, safe="") + "/configuration",
            {"configuration": configuration},
            project_id,
        )
        if configured.get("project_id") != project_id:
            raise ValueError("Configuration receipt belongs to a different Project")

    profiles = [read(project_id) for project_id in project_ids]
    if (
        profiles[0]["product"] != profiles[1]["product"]
        or profiles[0]["progress"] == profiles[1]["progress"]
    ):
        raise ValueError("Scope identity or progress readbacks are not isolated")
    scopes = [
        {
            "project_id": profile["project_id"],
            "name": profile["project"]["name"],
            "configuration": profile["configuration"],
            "progress": profile["progress"],
        }
        for profile in profiles
    ]
    fixture = {
        **{
            key: binding[key]
            for key in ("run_id", "source_revision", "tenant_id", "actor_id")
        },
        "product_id": product["product_id"],
        "product_name": product["product_name"],
        "owned_fixture": True,
        "scopes": scopes,
    }
    from tests.e2e.new_ui.test_product_scopes import assert_scope_fixture_inputs

    assert_scope_fixture_inputs(fixture)
    try:
        private_json(path, fixture)
    except FileExistsError:
        if load_scope_fixture(path, binding) != fixture:
            raise ValueError(
                "Existing protected scope fixture differs from the reconciled readback"
            ) from None
    return fixture
