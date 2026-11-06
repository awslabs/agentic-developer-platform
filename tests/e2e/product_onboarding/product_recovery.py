"""Resume Product writes from a protected durable state store and verify cleanup."""

from __future__ import annotations

import json
import re
import uuid

from tests.e2e.cli_uplift.ports import PortError
from tests.e2e.cli_uplift.statestore import S3StateStore

from .product_profile_api import checked


class ProductStateStore(S3StateStore):
    """Reuse encrypted private storage without mistaking denied reads for new runs."""

    def load(self, run_id):
        return super().load(f"product-v01-{run_id}")

    def save(self, run_id, document, manifest=None):
        return super().save(f"product-v01-{run_id}", document, manifest=manifest)

    def _get(self, key):
        try:
            response = self.aws.call("s3", "get_object", Bucket=self.bucket, Key=key)
        except PortError as exc:
            if str(exc).split(": ")[-1] == "NoSuchKey":
                return None
            raise RuntimeError("Product recovery state read is unavailable") from None
        body = response.get("Body")
        raw = body.read() if hasattr(body, "read") else body
        if not raw:
            raise RuntimeError("Product recovery state is empty")
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            raise RuntimeError("Product recovery state is invalid") from None


class InterruptedWrite(RuntimeError):
    pass


def api_recovery_cleanup(client_factory, state_store, cleanup, *, tenant_id,
                         other_tenant_id, owner_token, foreign_token, shared_product_id,
                         shared_project_id, run_id, source_revision):
    """Replay one persisted key after unknown response; never invent a second write."""
    if not re.fullmatch(r"[a-z0-9-]{8,64}", run_id) or not re.fullmatch(r"[a-f0-9]{40}", source_revision):
        raise ValueError("Recovery requires a bounded run ID and pinned source")
    if not all((tenant_id, other_tenant_id, owner_token, foreign_token,
                shared_product_id, shared_project_id)) or tenant_id == other_tenant_id:
        raise ValueError("Recovery requires separate owned and shared fixtures")
    if not callable(cleanup):
        raise TypeError("Recovery requires an authorized run-owned cleanup handler")
    body = {
        "name": f"eval-product-{run_id}", "purpose": "Verify interrupted write recovery",
        "intended_users": ["Maintainers"], "starting_point": "idea",
        "project": {"mode": "create"},
    }
    shared_path = f"/v1/products/{shared_product_id}/projects/{shared_project_id}"
    with client_factory(foreign_token) as foreign:
        shared_baseline = checked(foreign.get(shared_path), 200, other_tenant_id,
                                  product_id=shared_product_id, project_id=shared_project_id)
    document, manifest = state_store.load(run_id)
    if document is None:
        if manifest is not None:
            raise ValueError("Recovery manifest exists without an operation record")
        document = {
            "run_id": run_id, "tenant_id": tenant_id, "source_revision": source_revision,
            "request": body, "idempotency_key": f"product-recovery-{uuid.uuid4().hex}",
            "attempts": [], "receipt": None,
        }
    elif (document.get("run_id"), document.get("tenant_id"), document.get("source_revision"),
          document.get("request")) != (run_id, tenant_id, source_revision, body):
        raise ValueError("Recovery state differs from the selected run or release")
    if not isinstance(document.get("idempotency_key"), str) or not document["idempotency_key"].startswith("product-recovery-"):
        raise ValueError("Recovery state has no valid original operation key")
    if not isinstance(document.get("attempts"), list):
        raise TypeError("Recovery state has no valid recorded attempts")
    if document["attempts"] and manifest is None:
        raise ValueError("Recovery ownership manifest is missing")
    if manifest is not None and (manifest.get("run_id"), manifest.get("tenant_id")) != (run_id, tenant_id):
        raise ValueError("Recovery ownership manifest differs from the selected run")

    def record(state, **fields):
        document["attempts"].append({"state": state, **fields})
        state_store.save(run_id, document, manifest={
            "run_id": run_id, "tenant_id": tenant_id,
            "product_id": (document.get("receipt") or {}).get("product_id"),
            "project_id": (document.get("receipt") or {}).get("project_id"),
        })

    if document.get("cleanup_verified") is True:
        raise ValueError("Recovery run is already cleaned; it cannot start another write")
    receipt = document.get("receipt")
    if receipt is None:
        record("write_started")
        try:
            with client_factory(owner_token) as owner:
                response = owner.post("/v1/products", json=body, headers={
                    "Idempotency-Key": document["idempotency_key"],
                })
        except OSError as exc:
            record("response_unknown", error=type(exc).__name__)
            raise InterruptedWrite("Product write response lost; resume with the original run ID") from None
        created = checked(response, 201, tenant_id, revision=1)
        assert created["product"]["name"] == body["name"]
        receipt = {"product_id": created["product_id"], "project_id": created["project_id"], "revision": 1}
        document["receipt"] = receipt
        record("write_readback_pending")
    path = f"/v1/products/{receipt['product_id']}/projects/{receipt['project_id']}"

    def clean(owned_product_ids):
        record("cleanup_started", owned_product_ids=owned_product_ids)
        cleanup({"run_id": run_id, "tenant_id": tenant_id, **receipt,
                 "owned_product_ids": owned_product_ids})
        with client_factory(owner_token) as owner:
            absent = owner.get(path)
            assert absent.status_code == 404
            listing = owner.get("/v1/products")
            assert listing.status_code == 200
            assert all(item["name"] != body["name"] for item in listing.json()["items"])
        with client_factory(foreign_token) as foreign:
            shared_after = checked(foreign.get(shared_path), 200, other_tenant_id,
                                   product_id=shared_product_id, project_id=shared_project_id)
            assert shared_after == shared_baseline

    with client_factory(owner_token) as owner:
        current = checked(owner.get(path), 200, tenant_id,
                          product_id=receipt["product_id"], project_id=receipt["project_id"], revision=1)
        assert current["product"]["name"] == body["name"]
        listing = owner.get("/v1/products")
        assert listing.status_code == 200
        run_products = [item for item in listing.json()["items"] if item["name"] == body["name"]]
        history = owner.get(f"{path}/changes")
        assert history.status_code == 200
        changes = history.json()["items"]
        assert len(changes) == 1 and changes[0]["revision"] == 1
        assert changes[0]["product_id"] == receipt["product_id"]
    candidate_product_ids = [item["id"] for item in run_products]
    if candidate_product_ids != [receipt["product_id"]]:
        record("mutation_count_mismatch", candidate_product_ids=candidate_product_ids)
        try:
            clean([receipt["product_id"]])
        except Exception as exc:
            record("cleanup_failed", error=type(exc).__name__)
            raise
        raise AssertionError("Interrupted Product write produced duplicate or missing effects")
    record("single_mutation_verified", change_event_id=changes[0]["event_id"])

    try:
        clean([receipt["product_id"]])
    except Exception as exc:
        record("cleanup_failed", error=type(exc).__name__)
        raise
    document["cleanup_verified"] = True
    record("cleanup_verified")
    return {**receipt, "attempts": list(document["attempts"]), "cleanup_verified": True}
