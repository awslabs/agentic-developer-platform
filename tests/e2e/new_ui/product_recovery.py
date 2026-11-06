"""Browser adapter for the maintained Product state store and protected retiree."""

from __future__ import annotations

import re
import time
from urllib.parse import quote


class ProductRun:
    def __init__(self, store, protected_retire, *, run_id, tenant_id, actor_id, source_revision, owner_token, request_body, name_suffix=""):
        if not all(callable(getattr(store, method, None)) for method in ("load", "save")) or not callable(protected_retire):
            raise ValueError("Product run requires the maintained durable store and protected retirement")
        if not re.fullmatch(r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", run_id):
            raise ValueError("Product run ID differs from the protected workflow")
        if not re.fullmatch(r"[a-f0-9]{40}", source_revision) or not tenant_id or not actor_id or not owner_token:
            raise ValueError("Product run owner or source is missing")
        if not isinstance(request_body, dict) or request_body.get("project") != {"mode": "create"} or request_body.get("starting_point") != "idea":
            raise ValueError("Product run requires the reviewed three-field create request")
        if not isinstance(request_body.get("name"), str) or not request_body["name"] or not isinstance(request_body.get("purpose"), str) or not request_body["purpose"] or not isinstance(request_body.get("intended_users"), list) or not request_body["intended_users"]:
            raise ValueError("Product run requires all three fields before mutation")
        if (name_suffix not in ("", "-resume_ui", "-resume_cli", "-progress", "-recovery",
                                "-guidance_idea", "-guidance_existing_assets", "-guidance_running_product",
                                "-v11_idea", "-v11_existing_assets", "-v11_running_product", "-v11_resume", "-v11_release")
                or set(request_body) != {"name", "purpose", "intended_users", "starting_point", "project"}
                or request_body["name"] != f"eval-product-{run_id}{name_suffix}"):
            raise ValueError("Product create name differs from the protected V01 run")
        self.store = store
        self.protected_retire = protected_retire
        self.run_id = run_id
        self.tenant_id = tenant_id
        self.actor_id = actor_id
        self.source_revision = source_revision
        self.owner_token = owner_token
        self.request_body = request_body

    def _read(self):
        document, manifest = self.store.load(self.run_id)
        if document is not None:
            if not isinstance(manifest, dict) or any(
                document.get(field) != getattr(self, field) or manifest.get(field) != getattr(self, field)
                for field in ("run_id", "tenant_id", "actor_id", "source_revision")
            ) or document.get("request") != self.request_body or document.get("idempotency_key") != manifest.get("idempotency_key") or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", str(document.get("idempotency_key", ""))):
                raise ValueError("Restored Product intent differs from the owned run")
            receipt = document.get("receipt")
            if receipt is not None and (not isinstance(receipt, dict) or any(
                receipt.get(field) != manifest.get(field) for field in ("product_id", "project_id")
            )):
                raise ValueError("Restored Product IDs differ from the protected ownership")
        elif manifest is not None:
            raise ValueError("Product ownership exists without the original create intent")
        return document, manifest

    def record_intent(self, body, idempotency_key):
        if body != self.request_body or not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", idempotency_key):
            raise ValueError("Browser write differs from the original owned mutation")
        document, manifest = self._read()
        if document is not None and document["idempotency_key"] != idempotency_key:
            raise ValueError("A resumed browser write cannot create a new operation key")
        if document is None:
            document = {
                "run_id": self.run_id, "tenant_id": self.tenant_id, "actor_id": self.actor_id,
                "source_revision": self.source_revision, "request": self.request_body,
                "idempotency_key": idempotency_key, "receipt": None,
            }
            manifest = {field: document[field] for field in ("run_id", "tenant_id", "actor_id", "source_revision", "idempotency_key")}
        self.store.save(self.run_id, document, manifest=manifest)
        if self._read() != (document, manifest):
            raise RuntimeError("Product create intent was not durably readable before dispatch")
        return manifest

    def reconcile(self, request, base_url):
        document, manifest = self._read()
        if document is None:
            raise ValueError("Cannot replay a Product without its persisted original intent")
        receipt = document.get("receipt")
        if receipt is None:
            response = request.post(
                base_url + "/api/v1/products",
                headers={"Authorization": "Bearer " + self.owner_token, "Idempotency-Key": document["idempotency_key"]},
                data=document["request"], timeout=30_000,
            )
            if response.status not in (200, 201):
                raise RuntimeError(f"Original Product operation could not be reconciled: HTTP {response.status}")
            payload = response.json()
            if payload.get("tenant_id") != self.tenant_id or any(
                not isinstance(payload.get(field), str) or not payload[field]
                for field in ("product_id", "project_id")
            ) or any(payload.get("product", {}).get(field) != document["request"][field]
                     for field in ("name", "purpose", "intended_users")) or payload.get("product", {}).get("owner_ref") != {"kind": "user", "id": self.actor_id} or any(
                payload.get("project", {}).get(field) != expected
                for field, expected in (("id", payload.get("project_id")), ("tenant_id", self.tenant_id),
                                        ("owner_user_id", self.actor_id), ("name", self.request_body["name"]))
            ):
                raise ValueError("Replayed Product receipt differs from the original owned intent")
            receipt = {field: payload[field] for field in ("product_id", "project_id")}
            document = {**document, "receipt": receipt}
            manifest = {**manifest, **receipt}
            self.store.save(self.run_id, document, manifest=manifest)
            if self._read() != (document, manifest):
                raise RuntimeError("Product receipt was not durably readable after reconciliation")
        return receipt

    def retire(self, request, base_url, observer):
        receipt = self.reconcile(request, base_url)
        document, manifest = self._read()
        if not isinstance(observer, dict) or observer.get("tenant_id") != self.tenant_id or not observer.get("access_token"):
            raise ValueError("Independent owning-tenant observer is required for cleanup proof")
        result = self.protected_retire(manifest)
        if not isinstance(result, dict) or result.get("fixture_removed") is not True or any(
            result.get(field) != manifest[field] for field in ("run_id", "product_id", "project_id")
        ) or type(result.get("cleanup_revision")) is not int or result["cleanup_revision"] < 1:
            raise RuntimeError("Protected owning-service retirement was not verified")
        headers = {"Authorization": "Bearer " + observer["access_token"]}
        path = "/products/" + quote(receipt["product_id"], safe="") + "/projects/" + quote(receipt["project_id"], safe="")
        for suffix in ("", "/changes", "/revisions/1"):
            response = request.get(base_url + "/api/v1" + path + suffix, headers=headers, timeout=10_000)
            if response.status != 404:
                raise RuntimeError("Retired Product is still readable through its public API")
        deadline = time.monotonic() + 30
        cursor = None
        seen = set()
        for _ in range(5):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Retired Product discovery exceeded its time budget")
            url = base_url + "/api/v1/products" + ("?cursor=" + quote(cursor, safe="") if cursor else "")
            listing = request.get(url, headers=headers, timeout=min(int(remaining * 1000), 10_000))
            if listing.status != 200:
                raise RuntimeError("Independent owning-tenant observer could not list Products")
            page = listing.json()
            if not isinstance(page.get("items"), list) or len(page["items"]) > 100 or any(
                item.get("id") == receipt["product_id"] for item in page["items"]
            ):
                raise RuntimeError("Owned Product remains visible or discovery exceeded its page bound")
            cursor = page.get("next_cursor")
            if cursor is None:
                # Scope cleanup confirms these owning-service fields against its
                # durable intent. Preserve them without copying unrelated payloads.
                scope_receipt = {field: result[field] for field in ("scope_project_id", "retire_key") if field in result}
                return {"run_id": self.run_id, "product_id": receipt["product_id"], "project_id": receipt["project_id"], "cleanup_revision": result["cleanup_revision"], "fixture_removed": True,
                        "owned_absent": True, "shared_unchanged": result.get("shared_unchanged") is True, **scope_receipt}
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise RuntimeError("Product discovery returned an invalid or repeated cursor")
            seen.add(cursor)
        raise RuntimeError("Owned Product cleanup could not be verified within five pages")


def dispatch_owned_product(route, product_run):
    if route.request.method != "POST":
        route.continue_()
        return True
    try:
        product_run.record_intent(route.request.post_data_json, route.request.headers.get("idempotency-key"))
    except Exception:
        route.abort(error_code="blockedbyclient")
        return False
    route.continue_()
    return True
