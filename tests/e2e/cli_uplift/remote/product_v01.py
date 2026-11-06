"""Run the selected lifecycle with a private actor and verified retirement."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re

import common
import product_cleanup
import product_denial_actor
import product_probe
from product_http import ProductClient, local_owner
from product_state import ProductState
from tests.e2e.product_onboarding import live
from tests.e2e.product_onboarding.product_profile_api import (
    PROTECTED_SELECTIONS,
    checked,
)
from tests.e2e.product_onboarding.product_recovery import InterruptedWrite


def run_selected(config, observe):
    common.require(
        config.get("product_cases") in PROTECTED_SELECTIONS,
        "The on-instance Product driver accepts only reviewed ordered selections",
    )
    denial = "api_denial_oracle" in config["product_cases"].split(",")
    recovery = "api_recovery_cleanup" in config["product_cases"].split(",")
    hold = config.get("hold_for_rollback") is True
    common.require(
        not hold or config["product_cases"] == "api_product_lifecycle",
        "Product rollback hold requires one isolated lifecycle",
    )
    held = {}
    evaluation_id = config.get("evaluation_id")
    run_id = config.get("product_run_id")
    common.require(
        isinstance(evaluation_id, str)
        and re.fullmatch(r"adp-e2e-[0-9]{8}-[0-9]{6}-[a-f0-9]{6}", evaluation_id)
        and isinstance(run_id, str)
        and re.fullmatch(r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", run_id)
        and run_id == config.get("workflow_run_id"),
        "Product run identity does not match the protected workflow dispatch",
    )
    session, client_factory = (
        local_owner(config, allow_foreign=True)
        if denial or recovery
        else local_owner(config)
    )
    tenant_id = session["org_id"]
    common.require(
        session["user_id"] == config.get("product_actor_id"),
        "Product driver actor differs from the durably recorded create intent",
    )
    # The authenticated Cognito subject above and the ADP `users` row below are
    # different identifiers. Product retirement is matched against the row
    # (`ProductOperation.actor_id`, `Product.owner_user_id`, `created_by`), so
    # the ownership this driver persists for cleanup has to be recorded under
    # the row id. Sending the subject instead made the protected retirement
    # refuse with "Product fixture ownership could not be verified" after the
    # fixture had already been created.
    actor_row_id = session.get("adp_user_id")
    common.require(
        isinstance(actor_row_id, str)
        and bool(actor_row_id)
        and actor_row_id != session["user_id"]
        and actor_row_id == config.get("product_actor_row_id"),
        "Product driver has no ADP user row for the durably recorded create intent",
    )
    common.require(
        tenant_id == config.get("product_workspace_id"),
        "Product actor does not own the selected evaluation workspace",
    )
    other_tenant_id = config.get("other_tenant_id")
    common.require(
        isinstance(other_tenant_id, str)
        and bool(other_tenant_id)
        and common.load_session(config).get("org_id") == other_tenant_id
        and other_tenant_id != tenant_id,
        "Product lifecycle needs a separate tenant identity",
    )
    state = ProductState(config["work_dir"], run_id)
    intent_key = config.get("product_intent_key")
    common.require(
        isinstance(intent_key, str)
        and re.fullmatch(r"product-create-[a-f0-9]{64}", intent_key),
        "Product driver needs the original durably recorded create key",
    )
    cleanup_evidence = {}
    recovery_evidence = {}
    denial_fixtures = {}
    if denial:
        revoked_token, revoke = product_denial_actor.prepare(
            config, session["access_token"], tenant_id, session["user_id"]
        )
        bootstrap = common.load_session(config)
        common.require(
            revoked_token != bootstrap["access_token"],
            "Product denial member must not reuse the foreign bootstrap session",
        )
        verified_factory = client_factory

        @contextlib.contextmanager
        def client_factory(actor):
            if actor == revoked_token:
                yield ProductClient(config["gateway_url"], actor)
            else:
                with verified_factory(actor) as client:
                    yield client

        denial_fixtures = {
            "foreign_token": bootstrap["access_token"],
            "revoked_token": revoked_token,
            "revoke_actor": revoke,
        }

    def shared_snapshot():
        bootstrap = common.load_session(config)
        with client_factory(bootstrap["access_token"]) as foreign:
            response = foreign.get("/v1/products")
            common.require(
                response.status_code == 200, "Shared Product inventory is unavailable"
            )
            listing = response.json()
            common.require(
                isinstance(listing.get("items"), list)
                and not listing.get("next_cursor"),
                "Shared Product inventory is incomplete",
            )
            snapshots = []
            for product in listing["items"]:
                for project_id in product["project_ids"]:
                    path = f"/v1/products/{product['id']}/projects/{project_id}"
                    snapshot = checked(
                        foreign.get(path),
                        200,
                        other_tenant_id,
                        product_id=product["id"],
                        project_id=project_id,
                    )
                    snapshots.append(snapshot)
            return listing, snapshots

    shared_before = shared_snapshot() if recovery else None

    def cleanup(owned):
        document, manifest = state.load(f"lifecycle-{run_id}")
        common.require(
            isinstance(document, dict)
            and isinstance(manifest, dict)
            and manifest.get("run_id") == run_id
            and manifest.get("tenant_id") == tenant_id
            and manifest.get("actor_id") == actor_row_id
            and manifest.get("source_revision") == config.get("expected_revision")
            and owned.get("owned_manifest") == manifest,
            "Product cleanup has no matching persisted ownership",
        )
        try:
            if recovery and document.get("receipt") is not None:
                path = f"/v1/products/{manifest['product_id']}/projects/{manifest['project_id']}"
                with client_factory(session["access_token"]) as owner:
                    history = owner.get(path + "/changes")
                    common.require(
                        history.status_code == 200
                        and [item["revision"] for item in history.json()["items"]]
                        == [1, 2],
                        "Recovered Product has duplicate or missing committed mutations",
                    )
        finally:
            product_cleanup.execute(config, cleanup_evidence)
        receipt = cleanup_evidence.get("detail") or {}
        common.require(
            cleanup_evidence.get("success") is True
            and receipt.get("run_id") == run_id
            and receipt.get("fixture_removed") is True
            and all(
                receipt.get(field) == manifest[field]
                for field in ("product_id", "project_id")
                if field in manifest
            ),
            "Protected Product cleanup did not retire the owned fixture",
        )
        product_id, project_id = receipt.get("product_id"), receipt.get("project_id")
        common.require(
            isinstance(product_id, str)
            and isinstance(project_id, str)
            and product_id
            and project_id,
            "Protected Product cleanup returned no owned Product identity",
        )
        path = f"/v1/products/{product_id}/projects/{project_id}"
        with client_factory(session["access_token"]) as owner:
            common.require(
                all(
                    owner.get(target).status_code == 404
                    for target in (path, path + "/changes", path + "/revisions/1")
                ),
                "Retired Product is still visible through its current or historical API",
            )
        if recovery:
            common.require(
                shared_snapshot() == shared_before,
                "Shared Product assets changed during recovery",
            )
            recovery_evidence.update(
                product_id=product_id,
                project_id=project_id,
                fixture_removed=True,
                cleanup_verified=True,
                shared_unchanged=True,
                attempts=document["attempts"],
            )

    fixtures = {
        "tenant_id": tenant_id,
        "other_tenant_id": other_tenant_id,
        "owner_token": session["access_token"],
        # Persisted into the ownership manifest product_cleanup posts to the
        # protected retirement route, which resolves ADP `users` rows.
        "actor_id": actor_row_id,
        "run_id": run_id,
        "idempotency_key": intent_key,
        **denial_fixtures,
    }
    if recovery:
        common.require(
            observe() == config["expected_revision"],
            "Product release changed before recovery",
        )
        interrupted = False

        @contextlib.contextmanager
        def interrupted_client(token):
            with client_factory(token) as client:

                class LostCreateResponse:
                    def post(self, path, *, json, headers):
                        nonlocal interrupted
                        response = client.post(path, json=json, headers=headers)
                        common.require(
                            response.status_code == 201,
                            "Recovery create was not committed",
                        )
                        interrupted = True
                        raise ConnectionError(
                            "Deliberately interrupted Product create response"
                        )

                yield LostCreateResponse()

        try:
            live.api_product_lifecycle(
                interrupted_client,
                **{
                    key: fixtures[key]
                    for key in (
                        "tenant_id",
                        "other_tenant_id",
                        "owner_token",
                        "actor_id",
                        "run_id",
                        "idempotency_key",
                    )
                },
                source_revision=config["expected_revision"],
                state_store=state,
            )
        except InterruptedWrite:
            common.require(
                interrupted, "Recovery did not reach its controlled interruption"
            )
        except Exception:
            document, manifest = state.load(f"lifecycle-{run_id}")
            if manifest is not None:
                cleanup({"owned_manifest": manifest})
            raise
        state = ProductState(config["work_dir"], run_id)

    execution_cases = "api_product_lifecycle" + (",api_denial_oracle" if denial else "")

    def retain_owned(owned):
        document, manifest = state.load(f"lifecycle-{run_id}")
        lifecycle = owned.get("api_product_lifecycle") or {}
        common.require(
            isinstance(document, dict)
            and isinstance(manifest, dict)
            and owned.get("owned_manifest") == manifest
            and document.get("receipt")
            == {
                "product_id": lifecycle.get("product_id"),
                "project_id": lifecycle.get("project_id"),
                "revision": 1,
            }
            and manifest.get("idempotency_key") == intent_key
            and lifecycle.get("revision") == 2,
            "Product rollback hold has no original owned create receipt",
        )
        path = (
            f"/v1/products/{manifest['product_id']}/projects/{manifest['project_id']}"
        )
        with client_factory(session["access_token"]) as owner:
            response = owner.get(path + "/changes")
            history = (
                response.json().get("items") if response.status_code == 200 else None
            )
        common.require(
            isinstance(history, list)
            and len(history) == 2
            and [item.get("revision") for item in history] == [1, 2]
            and all(
                item.get("product_id") == manifest["product_id"]
                and item.get("event_id")
                for item in history
            )
            and len({item["event_id"] for item in history}) == 2,
            "Product rollback hold has no complete original history",
        )
        held.update(
            {
                "run_id": run_id,
                "product_id": manifest["product_id"],
                "project_id": manifest["project_id"],
                "tenant_id": tenant_id,
                "actor_id": session["user_id"],
                "source_revision": config["expected_revision"],
                "history_ref": "sha256:"
                + hashlib.sha256(
                    json.dumps(history, sort_keys=True).encode()
                ).hexdigest(),
                "idempotency_key": intent_key,
                "idempotency_receipt": "sha256:"
                + hashlib.sha256(
                    json.dumps(document["receipt"], sort_keys=True).encode()
                ).hexdigest(),
            }
        )
        return True

    results = live.run(
        execution_cases,
        config["expected_revision"],
        fixtures,
        observe,
        client_factory,
        cleanup,
        state_store=state,
        retain_owned=retain_owned if hold else None,
    )
    common.require(
        hold or cleanup_evidence.get("success") is True,
        "Product cleanup was not verified",
    )
    lifecycle = results["api_product_lifecycle"]
    if recovery:
        common.require(
            recovery_evidence.get("shared_unchanged") is True,
            "Recovery cleanup was not verified",
        )
        results["api_recovery_cleanup"] = recovery_evidence
    return {
        "results": {case: results[case] for case in config["product_cases"].split(",")},
        "lifecycle": lifecycle,
        "cleanup": {"fixture_removed": False} if hold else cleanup_evidence["detail"],
        **({"held": held} if hold else {}),
    }


def execute(config, evidence):
    proof = config.get("product_probe")
    inventory = config.get("product_inventory")
    common.require(
        isinstance(proof, dict)
        and proof.get("success") is True
        and proof.get("source_revision") == config.get("expected_revision")
        and isinstance(inventory, dict)
        and inventory.get("source_revision") == config.get("expected_revision")
        and isinstance(inventory.get("pods"), list)
        and isinstance(proof.get("pods"), list)
        and len(proof["pods"]) == len(inventory["pods"])
        and len(proof["pods"]) > 0,
        "Product driver needs a complete protected per-pod process receipt",
    )

    def observe():
        current = {}
        product_probe.execute(config, current)
        common.require(
            current.get("success") is True
            and current.get("source_revision") == proof["source_revision"]
            and current.get("pods") == proof["pods"],
            "Product process schema or serving pod changed during the lifecycle",
        )
        return current["source_revision"]

    result = run_selected(config, observe)
    evidence.update(success=True, stage="complete", **result)


if __name__ == "__main__":
    raise SystemExit(common.run_script(execute))
