"""Read the original held Product through the protected owner after each switch."""

from __future__ import annotations

import hashlib
import json
import re

from tests.e2e.cli_uplift import ports

from . import product_sessions
from .product_profile_api import create_request


def read_owned(cfg, owned, token, *, admissions, request=product_sessions.request):
    if (
        type(admissions) is not bool
        or not isinstance(owned, dict)
        or not all(
            isinstance(owned.get(field), str) and owned[field]
            for field in (
                "run_id",
                "tenant_id",
                "actor_id",
                "product_id",
                "project_id",
                "history_ref",
                "idempotency_key",
                "idempotency_receipt",
            )
        )
        or not re.fullmatch(
            r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}",
            owned["run_id"],
        )
        or any(
            not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", owned[field])
            for field in ("product_id", "project_id")
        )
    ):
        raise ports.PortError("Product readback has no original held ownership")
    if not product_sessions.verify(
        cfg, {"user_id": owned["actor_id"], "org_id": owned["tenant_id"]}, token
    ):
        raise ports.PortError("Product readback owner is not authenticated")
    path = f"/v1/products/{owned['product_id']}/projects/{owned['project_id']}"

    def get(target):
        status, result = request(cfg, target, token=token)
        if status != 200 or not isinstance(result, dict):
            raise ports.PortError("Held Product readback is unavailable")
        return result

    current = get(path)
    original = get(path + "/revisions/1")
    edited = get(path + "/revisions/2")
    history = get(path + "/changes").get("items")
    listing = get("/v1/products").get("items")
    if (
        not all(
            isinstance(profile, dict)
            and profile.get("schema_version") == "1.0"
            and profile.get("tenant_id") == owned["tenant_id"]
            and profile.get("product_id") == owned["product_id"]
            and profile.get("project_id") == owned["project_id"]
            and profile.get("revision") == revision
            for profile, revision in ((original, 1), (edited, 2), (current, 2))
        )
        or current.get("product") != edited.get("product")
        or current.get("configuration") != edited.get("configuration")
        or not isinstance(current.get("product"), dict)
        or current.get("product", {}).get("name")
        != f"eval-product-{owned['run_id']}-edited"
        or current.get("product", {}).get("purpose") != "Ship a focused update"
        or not isinstance(history, list)
        or len(history) != 2
        or any(not isinstance(item, dict) for item in history)
        or [item.get("revision") for item in history] != [1, 2]
        or any(
            item.get("product_id") != owned["product_id"]
            or item.get("tenant_id") != owned["tenant_id"]
            or not item.get("event_id")
            for item in history
        )
        or len({item["event_id"] for item in history}) != 2
        or not isinstance(listing, list)
        or len(
            [
                item
                for item in listing
                if isinstance(item, dict) and item.get("id") == owned["product_id"]
            ]
        )
        != 1
    ):
        raise ports.PortError("Held Product or historical revisions changed")

    def digest(value):
        return (
            "sha256:"
            + hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        )

    receipt = {
        "product_id": original["product_id"],
        "project_id": original["project_id"],
        "revision": original["revision"],
    }
    if (
        digest(history) != owned["history_ref"]
        or digest(receipt) != owned["idempotency_receipt"]
    ):
        raise ports.PortError("Held Product history or original create receipt changed")
    if admissions:
        status, replay = request(
            cfg,
            "/v1/products",
            token=token,
            body=create_request(owned["run_id"]),
            idempotency_key=owned["idempotency_key"],
            expected_status=201,
        )
        if (
            status != 201
            or not isinstance(replay, dict)
            or any(
                replay.get(field) != current[field]
                for field in ("tenant_id", "product_id", "project_id", "revision")
            )
            or replay.get("product") != current["product"]
            or get(path + "/changes").get("items") != history
        ):
            raise ports.PortError("Original Product idempotency replay changed")
    return {
        "run_id": owned["run_id"],
        "product_id": owned["product_id"],
        "history_ref": digest(history),
        "idempotency_key": owned["idempotency_key"],
        "idempotency_receipt": digest(receipt),
        "supported_routes_ok": True,
        "non_product_available": True,
        "replay_verified": admissions,
    }
