"""Product Profile acceptance using supplied HTTP clients; no cloud access at import."""

from __future__ import annotations

import itertools
import json
import re
import uuid

CASES = ("api_product_lifecycle", "api_denial_oracle", "api_recovery_cleanup")
PROTECTED_SELECTIONS = frozenset(
    ",".join(selection)
    for size in range(1, len(CASES) + 1)
    for selection in itertools.combinations(CASES, size)
)


def checked(
    response, status, tenant_id, *, product_id=None, project_id=None, revision=None
):
    assert response.status_code == status, (
        f"Product API returned {response.status_code}, expected {status}"
    )
    profile = response.json()
    assert profile["schema_version"] == "1.0"
    assert profile["tenant_id"] == tenant_id
    assert profile["product_id"] and profile["project_id"]
    if product_id is not None:
        assert profile["product_id"] == product_id
    if project_id is not None:
        assert profile["project_id"] == project_id
    if revision is not None:
        assert profile["revision"] == revision
    assert response.headers["Profile-Revision"] == str(profile["revision"])
    return profile


def lifecycle_intent(
    state_store,
    *,
    tenant_id,
    actor_id,
    run_id,
    source_revision,
    request,
    idempotency_key=None,
):
    if (
        not all(
            callable(getattr(state_store, method, None)) for method in ("load", "save")
        )
        or not isinstance(run_id, str)
        or re.fullmatch(r"[a-z0-9-]{8,64}", run_id) is None
        or not actor_id
        or not isinstance(source_revision, str)
        or len(source_revision) != 40
        or any(character not in "0123456789abcdef" for character in source_revision)
        or (idempotency_key is not None and not isinstance(idempotency_key, str))
    ):
        raise ValueError(
            "Product lifecycle needs durable state, a fixture actor and pinned source"
        )
    state_key = f"lifecycle-{run_id}"
    document, manifest = state_store.load(state_key)
    if document is None:
        if manifest is not None:
            raise ValueError(
                "Product lifecycle manifest exists without its create intent"
            )
        document = {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "source_revision": source_revision,
            "request": request,
            "idempotency_key": idempotency_key or f"product-create-{uuid.uuid4().hex}",
            "attempts": [],
            "receipt": None,
        }
        manifest = {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "source_revision": source_revision,
            "idempotency_key": document["idempotency_key"],
        }
    if (
        not isinstance(document, dict)
        or not isinstance(manifest, dict)
        or any(
            document.get(field) != value or manifest.get(field) != value
            for field, value in (
                ("run_id", run_id),
                ("tenant_id", tenant_id),
                ("actor_id", actor_id),
                ("source_revision", source_revision),
            )
        )
        or document.get("request") != request
        or manifest.get("idempotency_key") != document.get("idempotency_key")
        or (
            idempotency_key is not None
            and document.get("idempotency_key") != idempotency_key
        )
    ):
        raise ValueError(
            "Product lifecycle intent differs from the selected fixture or release"
        )
    if (
        not isinstance(document.get("idempotency_key"), str)
        or not document["idempotency_key"].startswith("product-create-")
        or not isinstance(document.get("attempts"), list)
    ):
        raise ValueError("Product lifecycle has no valid original create attempt")
    return state_key, document, manifest


def create_request(run_id):
    return {
        "name": f"eval-product-{run_id}",
        "purpose": "Coordinate a small release",
        "intended_users": ["Maintainers"],
        "starting_point": "idea",
        "project": {"mode": "create"},
    }


def api_product_lifecycle(
    client_factory,
    *,
    tenant_id,
    other_tenant_id,
    owner_token,
    run_id,
    actor_id,
    source_revision,
    state_store,
    expected_product_id=None,
    idempotency_key=None,
):
    """Create, retry, edit and independently read one owned Product/Project pair."""
    assert tenant_id and other_tenant_id and tenant_id != other_tenant_id
    assert owner_token and run_id
    name = f"eval-product-{run_id}"
    fields = create_request(run_id)
    state_key, document, manifest = lifecycle_intent(
        state_store,
        tenant_id=tenant_id,
        actor_id=actor_id,
        run_id=run_id,
        source_revision=source_revision,
        request=fields,
        idempotency_key=idempotency_key,
    )
    if document["receipt"] is not None:
        raise ValueError(
            "Product lifecycle already has an owned create receipt; recover or clean it first"
        )
    create_key = document["idempotency_key"]
    document["attempts"].append({"state": "create_started"})
    state_store.save(state_key, document, manifest=manifest)
    with client_factory(owner_token) as client:
        try:
            response = client.post(
                "/v1/products", json=fields, headers={"Idempotency-Key": create_key}
            )
        except OSError as exc:
            document["attempts"].append(
                {"state": "response_unknown", "error": type(exc).__name__}
            )
            state_store.save(state_key, document, manifest=manifest)
            from .product_recovery import InterruptedWrite

            raise InterruptedWrite(
                "Product lifecycle response lost; resume with the original run ID"
            ) from None
        created = checked(response, 201, tenant_id, revision=1)
        product_id, project_id = created["product_id"], created["project_id"]
        document["receipt"] = {
            "product_id": product_id,
            "project_id": project_id,
            "revision": 1,
        }
        manifest.update(product_id=product_id, project_id=project_id)
        document["attempts"].append({"state": "create_received"})
        state_store.save(state_key, document, manifest=manifest)
        if expected_product_id is not None:
            assert product_id == expected_product_id
        path = f"/v1/products/{product_id}/projects/{project_id}"
        replay = checked(
            client.post(
                "/v1/products", json=fields, headers={"Idempotency-Key": create_key}
            ),
            201,
            tenant_id,
            product_id=product_id,
            project_id=project_id,
            revision=1,
        )
        assert replay == created
        assert created["product"]["name"] == name
        assert created["product"]["purpose"] == fields["purpose"]
        assert created["product"]["intended_users"] == fields["intended_users"]
        assert created["product"]["starting_point"] == "idea"
        assert created["project"]["id"] == project_id
        assert created["project"]["tenant_id"] == tenant_id
        assert created["project"]["name"] == name
        assert created["configuration"] == {}
        assert created["provenance"] == []
        edit = {"name": f"{name}-edited", "purpose": "Ship a focused update"}
        edit_key = f"product-edit-{uuid.uuid4().hex}"
        document["edit_key"] = edit_key
        document["attempts"].append({"state": "edit_started"})
        state_store.save(state_key, document, manifest=manifest)
        updated = checked(
            client.patch(
                path,
                json=edit,
                headers={
                    "Idempotency-Key": edit_key,
                    "If-Profile-Revision": "1",
                },
            ),
            200,
            tenant_id,
            product_id=product_id,
            project_id=project_id,
            revision=2,
        )
        assert updated["product"]["name"] == edit["name"]
        assert updated["product"]["purpose"] == edit["purpose"]
        assert updated["product"]["intended_users"] == fields["intended_users"]
    with client_factory(owner_token) as fresh_client:
        readback = checked(
            fresh_client.get(path),
            200,
            tenant_id,
            product_id=product_id,
            project_id=project_id,
            revision=2,
        )
        assert readback == updated
    document["attempts"].append({"state": "fresh_readback_verified", "revision": 2})
    state_store.save(state_key, document, manifest=manifest)
    return {
        "product_id": product_id,
        "project_id": project_id,
        "revision": 2,
        "edit_key": edit_key,
        "edited_fields": edit,
        "create_key": create_key,
    }


def denied(response, expected_status, hidden):
    assert response.status_code in expected_status
    body = response.json()
    if response.status_code == 404:
        assert body.get("detail", {}).get("code") == "not_found"
    assert all(value not in json.dumps(body) for value in hidden)


def api_denial_oracle(
    client_factory,
    *,
    tenant_id,
    other_tenant_id,
    owner_token,
    foreign_token,
    revoked_token,
    product_id,
    project_id,
    edit_key,
    edited_fields,
    expected_product_id=None,
):
    """Exercise stale writes, invisible reads/history and replay under live actor scope."""
    assert all(
        (
            tenant_id,
            other_tenant_id,
            owner_token,
            foreign_token,
            revoked_token,
            product_id,
            project_id,
            edit_key,
            edited_fields,
        )
    )
    assert tenant_id != other_tenant_id
    if expected_product_id is not None:
        assert product_id == expected_product_id
    path = f"/v1/products/{product_id}/projects/{project_id}"
    with client_factory(owner_token) as owner:
        baseline = checked(
            owner.get(path),
            200,
            tenant_id,
            product_id=product_id,
            project_id=project_id,
            revision=2,
        )
        assert {
            field: baseline["product"][field] for field in edited_fields
        } == edited_fields
        history = owner.get(f"{path}/changes")
        assert history.status_code == 200
        history_items = history.json()["items"]
        assert [item["revision"] for item in history_items] == [1, 2]
        assert all(
            item["product_id"] == product_id and item["tenant_id"] == tenant_id
            for item in history_items
        )
        listing = owner.get("/v1/products")
        assert listing.status_code == 200
        assert any(
            item["id"] == product_id and project_id in item["project_ids"]
            for item in listing.json()["items"]
        )
        stale = owner.patch(
            path,
            json={"purpose": "Unauthorized stale overwrite"},
            headers={
                "If-Profile-Revision": "1",
                "Idempotency-Key": f"product-stale-{uuid.uuid4().hex}",
            },
        )
        assert stale.status_code == 412
        assert stale.json().get("detail", {}).get("code") == "stale_revision"
        replay = checked(
            owner.patch(
                path,
                json=edited_fields,
                headers={
                    "If-Profile-Revision": "1",
                    "Idempotency-Key": edit_key,
                },
            ),
            200,
            tenant_id,
            product_id=product_id,
            project_id=project_id,
            revision=2,
        )
        assert replay == baseline

    hidden = (
        product_id,
        project_id,
        baseline["product"]["purpose"],
        baseline["product"]["name"],
    )
    with client_factory(foreign_token) as foreign:
        for target in (path, f"{path}/changes", f"{path}/revisions/1"):
            denied(foreign.get(target), {404}, hidden)
        foreign_list = foreign.get("/v1/products")
        assert foreign_list.status_code == 200
        assert all(
            product_id != item["id"] and project_id not in item["project_ids"]
            for item in foreign_list.json()["items"]
        )
        for key, fields in (
            (edit_key, edited_fields),
            (f"product-foreign-{uuid.uuid4().hex}", {"name": "Forbidden"}),
        ):
            denied(
                foreign.patch(
                    path,
                    json=fields,
                    headers={
                        "If-Profile-Revision": "1",
                        "Idempotency-Key": key,
                    },
                ),
                {404},
                hidden,
            )

    with client_factory(revoked_token) as revoked:
        for target in (path, "/v1/products", f"{path}/changes", f"{path}/revisions/1"):
            denied(
                revoked.get(target),
                {401, 403} if target == "/v1/products" else {401, 403, 404},
                hidden,
            )
        denied(
            revoked.patch(
                path,
                json=edited_fields,
                headers={
                    "If-Profile-Revision": "1",
                    "Idempotency-Key": edit_key,
                },
            ),
            {401, 403, 404},
            hidden,
        )

    with client_factory(owner_token) as fresh_owner:
        readback = checked(
            fresh_owner.get(path),
            200,
            tenant_id,
            product_id=product_id,
            project_id=project_id,
            revision=2,
        )
        assert readback == baseline
        final_history = fresh_owner.get(f"{path}/changes")
        assert final_history.status_code == 200
        assert final_history.json()["items"] == history_items
    return {
        "product_id": product_id,
        "revision": 2,
        "history_count": len(history_items),
    }
