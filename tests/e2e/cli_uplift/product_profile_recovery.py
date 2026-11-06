"""Retire one CLI-owned Product through the reviewed protected evaluator route."""

from __future__ import annotations

import re

from tests.e2e.product_onboarding import product_execution, product_sessions

from . import cleanup, ports


def create_body(evaluation_id):
    return {
        "name": "Owned Product " + evaluation_id,
        "purpose": "Installed CLI verification",
        "intended_users": ["Evaluation owner"],
        "starting_point": "idea",
        "project": {"mode": "create"},
    }


def validate(cfg, ctx, entry):
    evaluation_id = ctx["evaluation_id"]
    document = ctx["document"]
    owner = document.get("product_actor_session") or {}
    bootstrap = document.get("session") or {}
    detail = entry.get("detail") or {}
    if (
        not isinstance(evaluation_id, str)
        or not re.fullmatch(r"adp-e2e-[0-9]{8}-[0-9]{6}-[a-f0-9]{6}", evaluation_id)
        or document.get("product_run_id") != cfg.get("product_run_id")
        or entry.get("kind") != "product_fixture"
        or entry.get("id") != evaluation_id
        or entry.get("prefix") != evaluation_id
        or entry.get("account") != str(cfg.get("platform_account"))
        or entry.get("region") != cfg.get("region")
        or detail.get("case") != "PR03-C01"
        or detail.get("source_revision") != cfg.get("expected_revision")
        or detail.get("instance_id") != document.get("instance_id")
        or detail.get("idempotency_key") != "create_" + evaluation_id
        or detail.get("tenant_id") != owner.get("org_id")
        # The create was performed by the authenticated owner session, so the
        # recorded actor is its Cognito SUBJECT.
        or detail.get("actor_id") != owner.get("user_id")
        # Its ADP `users` row is a separate identifier, and the one the owned
        # resources below are recorded under. It must exist and must not be the
        # bootstrap actor's row, or the retirement would be reconciled against an
        # account this evaluation's Product owner never held.
        or not isinstance(owner.get("adp_user_id"), str)
        or not owner["adp_user_id"]
        or owner["adp_user_id"] == bootstrap.get("adp_user_id")
        or detail.get("actor_secret_id")
        != f"adp/cli-uplift-eval/{evaluation_id}-product-fixtures"
        or detail.get("actor_cognito_id")
        != f"{cfg.get('cognito_user_pool_id')}/{evaluation_id}-product-admin@adp-eval.example"
        or bootstrap.get("created_username")
        != f"{evaluation_id}-admin@adp-eval.example"
        or not isinstance(ctx.get("manifest"), cleanup.Manifest)
        or ctx["manifest"].prefix != evaluation_id
        or not ctx["manifest"].on_change
    ):
        raise ports.PortError(
            "Product retirement has no original durable CLI ownership"
        )
    for kind, identifier in (
        ("ec2_instance", document["instance_id"]),
        ("adp_org", owner["org_id"]),
        # The owned actor is the ADP `users` ROW the owner journey registered.
        # `owner["user_id"]` is that session's Cognito subject and names no
        # resource, so recording or checking it here would look for an owned
        # account that cannot exist.
        ("adp_user", owner["org_id"] + "/" + owner["adp_user_id"]),
    ):
        if not any(
            item["kind"] == kind
            and item["id"] == identifier
            and item.get("account") == str(cfg["platform_account"])
            and item.get("region") == cfg["region"]
            for item in ctx["manifest"].outstanding()
        ):
            raise ports.PortError("Product retirement lost its owned host or actor")
    return {
        "run_id": evaluation_id,
        "tenant_id": owner["org_id"],
        # The protected retirement route resolves ownership against the ADP
        # `users` row (ProductOperation.actor_id, Product.owner_user_id,
        # created_by and the binding provenance all store it), and its manifest
        # forbids extra fields, so the row id is the wire actor. The Cognito
        # subject is proved separately above against detail["actor_id"].
        "actor_id": owner["adp_user_id"],
        "source_revision": cfg["expected_revision"],
        "idempotency_key": detail["idempotency_key"],
    }


def build(cfg, aws, http, ctx, store):
    def retire(identifier, *, account=None, region=None):
        entries = [
            entry
            for entry in ctx["manifest"].outstanding()
            if entry["kind"] == "product_fixture" and entry["id"] == identifier
        ]
        if (
            len(entries) != 1
            or str(account) != str(cfg["platform_account"])
            or region != cfg["region"]
        ):
            raise ports.PortError("Product retirement location or intent differs")
        intent = validate(cfg, ctx, entries[0])
        owner = product_sessions.restore(cfg, ctx, store, owner=True)
        if (
            # `validate` already matched the recorded Cognito subject; the
            # intent itself carries the ADP `users` row the retirement resolves.
            owner.get("adp_user_id") != intent["actor_id"]
            or owner["user_id"] != (entries[0].get("detail") or {}).get("actor_id")
            or owner["org_id"] != intent["tenant_id"]
        ):
            raise ports.PortError("Product recovery actor differs from original create")
        try:
            result = product_execution.retire_independently(cfg, aws, intent)
        except product_execution.RetirementConflict:
            attempts = ctx["document"].setdefault("product_recovery_attempts", [])
            attempts.append(
                {
                    "state": "reconcile_started",
                    "idempotency_key": intent["idempotency_key"],
                }
            )
            store.save(ctx["evaluation_id"], ctx["document"], ctx["manifest"].read())
            status, receipt = product_sessions.request(
                cfg,
                # Mounted app path: the reviewed base already carries the
                # external `/api` prefix CloudFront strips.
                "/v1/products",
                token=owner["access_token"],
                body=create_body(identifier),
                idempotency_key=intent["idempotency_key"],
                expected_status=201,
            )
            if (
                status != 201
                or not isinstance(receipt, dict)
                or receipt.get("tenant_id") != intent["tenant_id"]
                or not all(
                    isinstance(receipt.get(key), str) and receipt[key]
                    for key in ("product_id", "project_id")
                )
                or receipt.get("product", {}).get("name")
                != create_body(identifier)["name"]
            ):
                raise ports.PortError("Original Product create could not be reconciled")
            attempts.append(
                {
                    "state": "reconcile_received",
                    "product_id": receipt["product_id"],
                    "project_id": receipt["project_id"],
                }
            )
            store.save(ctx["evaluation_id"], ctx["document"], ctx["manifest"].read())
            result = product_execution.retire_independently(
                cfg,
                aws,
                {
                    **intent,
                    "product_id": receipt["product_id"],
                    "project_id": receipt["project_id"],
                },
            )
        receipt = result.get("detail") if isinstance(result, dict) else None
        if (
            not isinstance(receipt, dict)
            or receipt.get("fixture_removed") is not True
            or receipt.get("run_id") != identifier
            or type(receipt.get("cleanup_revision")) is not int
            or receipt["cleanup_revision"] < 1
            or not all(
                isinstance(receipt.get(key), str) and receipt[key]
                for key in ("product_id", "project_id")
            )
            or any(
                attempt.get("owned_product", {}).get(key) != receipt[key]
                for attempt in ctx["document"].get("product_cli_attempts", [])
                if attempt.get("case_id") == "PR03-C01"
                and attempt.get("status") == "passed"
                for key in ("product_id", "project_id")
            )
        ):
            raise ports.PortError("Protected Product retirement did not verify removal")
        # Mounted app paths appended to the reviewed base, which already carries
        # the external `/api` prefix CloudFront strips. product_onboarding is
        # mounted at `/v1/products/...`, so the extra `/api` spelled here read
        # the retired routes back from `<base>/api/api/v1/products/...`: a 404
        # that nothing served, which would have "proved" removal of anything.
        path = f"/v1/products/{receipt['product_id']}/projects/{receipt['project_id']}"
        for suffix in ("", "/changes", "/revisions/1"):
            status, _ = http.get(
                cfg["gateway_url"].rstrip("/") + path + suffix,
                token=owner["access_token"],
                expect=None,
            )
            if status != 404:
                raise ports.PortError(
                    "Retired Product current or historical route remains visible"
                )
        status, listing = http.get(
            cfg["gateway_url"].rstrip("/") + "/v1/products?limit=100",
            token=owner["access_token"],
            expect=None,
        )
        if (
            status != 200
            or not isinstance(listing, dict)
            or not isinstance(listing.get("items"), list)
            or any(
                item.get("id") == receipt["product_id"]
                for item in listing["items"]
                if isinstance(item, dict)
            )
        ):
            raise ports.PortError("Retired Product remains in owner listing")
        ctx["document"]["product_retirement_receipt"] = {
            key: receipt[key]
            for key in (
                "run_id",
                "product_id",
                "project_id",
                "cleanup_revision",
                "fixture_removed",
            )
        }
        store.save(ctx["evaluation_id"], ctx["document"], ctx["manifest"].read())

    retire.available = bool(
        store
        and cfg.get("product_run_id")
        and cfg.get("internal_invoke_url")
        and cfg.get("state_bucket")
        and cfg.get("expected_revision")
        and http
    )
    return retire
