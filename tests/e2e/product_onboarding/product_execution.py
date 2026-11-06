"""Drive Product on the retained evaluator's owned instance and durable manifest."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from tests.e2e.cli_uplift import bundle, config, ports, runner, stages, statestore
from tests.e2e.cli_uplift import live as evaluator

from . import product_inventory, product_sessions
from .product_profile_api import create_request


class RetirementConflict(ports.PortError):
    pass


# The tree this run actually ships and drives. `bundle.sources()` builds the
# instance bundle from this same root, so the CLI the journey installs and
# invokes is the one checked in here.
REVIEWED_ROOT = bundle.PACKAGE_DIR.parents[2]


def reviewed_auth_helper():
    """Read the reviewed CLI auth helper for the preflight isolation check.

    The evaluator's own default reads it from `HARNESS_ROOT`, which only the
    pinned CLI-uplift evaluation exports after assembling `HARNESS_COMMIT`.
    Product acceptance assembles no pinned harness - it runs the reviewed tree
    directly against the observed release - so that default aborted preflight
    with "HARNESS_ROOT is not set" before any case ran. The contract itself is
    unchanged and still enforced by `preflight.check_harness_isolation`: it is
    asserted here against the tree whose CLI is shipped and executed, and the
    pinned-harness guard stays in place for the evaluation that does assemble
    one.
    """
    path = REVIEWED_ROOT / config.CLI_AUTH_HELPER
    if not path.is_file():
        raise ports.PortError(f"The reviewed tree has no {config.CLI_AUTH_HELPER}")
    return path.read_text()


def build_stages(
    cfg, inventory, workflow_run_id, supplied=None, *, store=None, recovering=False
):
    region = cfg.get("region", "")
    invoke_url = cfg.get("internal_invoke_url", "")
    if (
        not isinstance(workflow_run_id, str)
        or not re.fullmatch(
            r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", workflow_run_id
        )
        or inventory.get("source_revision") != cfg.get("expected_revision")
        or not isinstance(invoke_url, str)
        or not re.fullmatch(
            rf"https://[a-z0-9]+\.execute-api\.{re.escape(region)}\.amazonaws\.com/[A-Za-z0-9_-]+",
            invoke_url,
        )
    ):
        raise ValueError(
            "Product run needs the observed release and reviewed protected retirement route"
        )
    if cfg.get("hold_for_rollback") is True and store is None:
        raise ValueError(
            "Product rollback hold requires a private durable recovery store"
        )
    cfg = {**cfg, "product_run_id": workflow_run_id}
    # An explicit override still wins, so the offline seam is unchanged.
    wired = evaluator.wire(
        cfg, {"harness_auth_helper": reviewed_auth_helper, **(supplied or {})}
    )
    mapping = stages.build_stages(cfg, wired)

    def journey(ctx):
        instance_id = ctx["document"]["instance_id"]
        run_id = ctx["evaluation_id"]
        session = ctx["document"].get("session") or {}
        if not (
            session.get("user_id")
            # The authenticated Cognito subject above, and the ADP `users` row
            # the bootstrap journey registered for itself, are different
            # identifiers. Both are needed here: the subject is what
            # `/auth/cli/admin-session` can confirm, the row is what this run
            # owns, cleans up and proves ownership of in the manifest. Requiring
            # it here names the missing field instead of letting recovery
            # retention refuse with "no original actor ownership".
            and session.get("adp_user_id")
            and session.get("org_id")
            and session.get("session_ref")
        ):
            raise ports.PortError(
                "Product needs the freshly authenticated bootstrap administrator"
            )
        if ctx["manifest"].on_change is None:
            raise ports.PortError("Product writes require a durable recovery manifest")
        if ctx["document"].get("product_evidence"):
            raise ports.PortError("Product lifecycle already ran for this evaluation")
        if store is not None:
            product_sessions.retain(cfg, ctx, store, wired["ssm"])
        workspace_id = (
            run_id
            + "-product-"
            + hashlib.sha256((run_id + ":product-workspace").encode()).hexdigest()[:24]
        )
        base = {
            "evaluation_id": run_id,
            "instance_id": instance_id,
            "gateway_url": cfg["gateway_url"],
            "platform_account": str(cfg["platform_account"]),
            "region": cfg["region"],
            # The remote setup contract: every shipped module builds its child
            # environment with `common.clean_env`, which reads
            # `config["sts_endpoint"]`. Omitting the approved endpoints here
            # failed `product_actor` with KeyError: sts_endpoint on the instance
            # before any login. `product_workspace`, `product_probe` and
            # `product_v01` all extend this payload, so they carry it too.
            "sts_endpoint": cfg["sts_endpoint"],
            "secrets_endpoint": cfg["secrets_endpoint"],
            "work_dir": bundle.REMOTE_DIR,
            "session_ref": session["session_ref"],
            "product_cases": cfg["product_cases"],
            "product_workspace_id": workspace_id,
        }
        worker = wired["run_worker"]
        manifest = ctx["manifest"]
        workspace = worker(instance_id, "product_workspace", base, manifest=manifest)
        ctx["product_workspace_evidence"] = workspace
        actor_fixture = evaluator._admin_fixtures(
            wired["aws"], cfg, product_workspace_id=workspace_id
        )(ctx)
        if "api_denial_oracle" in cfg["product_cases"].split(","):
            ordinary_username = actor_fixture.get("non_admin_username")
            if ordinary_username != f"{run_id}-product-user@adp-eval.example":
                raise ports.PortError("Product denial has no run-owned ordinary actor")
            manifest.record(
                "adp_user_email",
                workspace_id + "/" + ordinary_username,
                account=str(cfg["platform_account"]),
                region=cfg["region"],
            )
        actor = worker(
            instance_id,
            "product_actor",
            {
                **base,
                **actor_fixture,
                "work_dir": bundle.REMOTE_DIR + "/product-actor",
                "product_team_id": workspace["team_id"],
                "cognito_user_pool_id": cfg["cognito_user_pool_id"],
                "expected_hashes": ctx["expected_hashes"],
                "login_required": True,
                "admin_challenges_required": True,
                "admin_setup_required": False,
            },
            manifest=manifest,
        )
        actor_session = actor["session"]
        denial_actor = None
        if "api_denial_oracle" in cfg["product_cases"].split(","):
            denial_actor = actor.get("denial_actor")
            ordinary_username = f"{run_id}-product-user@adp-eval.example"
            if (
                not isinstance(denial_actor, dict)
                or denial_actor.get("org_id") != workspace_id
                or denial_actor.get("username") != ordinary_username
                or not isinstance(denial_actor.get("user_id"), str)
                or not denial_actor["user_id"]
                # The denial actor's own ADP row: it must be a different account
                # from the owner in BOTH namespaces, otherwise one of the two
                # producers published the wrong identifier.
                or denial_actor["user_id"]
                in {actor_session.get("user_id"), actor_session.get("adp_user_id")}
                or denial_actor.get("cognito_sub_bound") is not True
                or not any(
                    entry["kind"] == "adp_user_email"
                    and entry["id"] == workspace_id + "/" + ordinary_username
                    and entry.get("account") == str(cfg["platform_account"])
                    and entry.get("region") == cfg["region"]
                    for entry in manifest.outstanding()
                )
            ):
                raise ports.PortError("Product denial actor is not owned or verified")
        owner_row_id = actor_session.get("adp_user_id")
        if (
            not isinstance(owner_row_id, str)
            or not owner_row_id
            or owner_row_id == actor_session.get("user_id")
            or owner_row_id == session["adp_user_id"]
        ):
            raise ports.PortError(
                "Product owner registration did not report its own ADP user row"
            )
        # Recorded under the ADP `users` ROW id, never the Cognito subject. That
        # is the identifier the sweep's `delete_adp_user` resolves against
        # `GET /api/admin/identity/organizations/<org>/users`, so a subject
        # recorded here matches no row: the deleter reads the real account as
        # "already gone" and the run reports a clean sweep over an account it
        # never removed. It is also the identifier `product_sessions.binding`
        # matches to prove the recovery session owned this actor.
        manifest.record(
            "adp_user",
            workspace_id + "/" + owner_row_id,
            account=str(cfg["platform_account"]),
            region=cfg["region"],
        )
        ctx["document"]["product_actor_session"] = actor_session
        if store is not None:
            product_sessions.retain(cfg, ctx, store, wired["ssm"], owner=True)
        selected = {
            **base,
            "expected_revision": cfg["expected_revision"],
            "product_inventory": inventory,
        }
        proof = worker(instance_id, "product_probe", selected, manifest=manifest)
        probe_pods = proof.get("pods") if isinstance(proof, dict) else None
        if (
            not isinstance(proof, dict)
            or proof.get("success") is not True
            or proof.get("source_revision") != cfg["expected_revision"]
            or not isinstance(inventory.get("pods"), list)
            or not isinstance(probe_pods, list)
            or len(probe_pods) != len(inventory["pods"])
            or any(
                not isinstance(pod, dict)
                or not isinstance(expected, dict)
                or pod.get("uid") != expected.get("uid")
                or not isinstance(expected.get("uid"), str)
                or not expected["uid"]
                or not isinstance(pod.get("schema_sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", pod["schema_sha256"])
                for pod, expected in zip(probe_pods, inventory["pods"])
            )
            or len({pod["schema_sha256"] for pod in probe_pods}) != 1
        ):
            raise ports.PortError(
                "Product process probe does not match every serving pod"
            )
        if (
            product_inventory.observe(wired["aws"], cfg, cfg["expected_revision"])
            != inventory
        ):
            raise ports.PortError(
                "Product serving inventory changed during evaluator setup"
            )
        intent_key = (
            "product-create-"
            + hashlib.sha256((workflow_run_id + ":" + run_id).encode()).hexdigest()
        )
        manifest.record(
            "product_fixture",
            workflow_run_id,
            detail={
                "tenant_id": workspace_id,
                # The authenticated subject the Product API attributes the
                # fixture to, and separately the ADP `users` row that owns and
                # must clean up the actor account. An independent retirement
                # needs both: the first to prove the intent is the original
                # actor's, the second to find the row it has to delete.
                "actor_id": actor_session["user_id"],
                "actor_row_id": owner_row_id,
                "source_revision": cfg["expected_revision"],
                "idempotency_key": intent_key,
                "instance_id": instance_id,
                "actor_secret_id": f"adp/cli-uplift-eval/{run_id}-product-fixtures",
                "actor_cognito_id": (
                    f"{cfg['cognito_user_pool_id']}/{run_id}-product-admin@adp-eval.example"
                ),
            },
            account=str(cfg["platform_account"]),
            region=cfg["region"],
        )
        result = worker(
            instance_id,
            "product_v01",
            {
                **selected,
                "hold_for_rollback": cfg.get("hold_for_rollback", False),
                "product_probe": proof,
                "product_run_id": workflow_run_id,
                "workflow_run_id": workflow_run_id,
                "product_actor_session_ref": actor_session["session_ref"],
                "other_tenant_id": session["org_id"],
                "internal_invoke_url": cfg["internal_invoke_url"],
                "product_intent_key": intent_key,
                "product_actor_id": actor_session["user_id"],
                # Both identifiers, for the same reason the fixture detail
                # records both: the driver proves the subject it authenticated
                # as, and persists the ADP `users` row the protected retirement
                # resolves ownership against.
                "product_actor_row_id": owner_row_id,
                **(
                    {
                        "product_denial_actor": denial_actor,
                        "product_team_id": workspace["team_id"],
                        "credential_secret": actor_fixture["credential_secret"],
                    }
                    if denial_actor
                    else {}
                ),
            },
            manifest=manifest,
        )
        selected_cases = base["product_cases"].split(",")
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("results"), dict)
            or set(result["results"]) != set(selected_cases)
            or len(result["results"]) != len(selected_cases)
            or any(
                not isinstance(result["results"][case], dict)
                or not result["results"][case]
                for case in selected_cases
            )
            or (
                denial_actor is not None
                and (
                    result["results"]["api_denial_oracle"].get("product_id")
                    != result["results"]
                    .get("api_product_lifecycle", result.get("lifecycle", {}))
                    .get("product_id")
                    or not result["results"]["api_denial_oracle"].get("product_id")
                    or result["results"]["api_denial_oracle"].get("revision") != 2
                    or result["results"]["api_denial_oracle"].get("history_count") != 2
                    or result["results"]["api_denial_oracle"].get("revocation")
                    != {
                        "user_id": denial_actor["user_id"],
                        "membership_revoked": True,
                    }
                )
            )
        ):
            raise ports.PortError(
                "Product worker did not execute every selected API case"
            )
        if cfg.get("hold_for_rollback") is True:
            held = result.get("held")
            lifecycle = result["results"].get("api_product_lifecycle") or {}
            if (
                selected_cases != ["api_product_lifecycle"]
                or not isinstance(held, dict)
                or result.get("cleanup") != {"fixture_removed": False}
                or held.get("run_id") != workflow_run_id
                or held.get("tenant_id") != workspace_id
                or held.get("actor_id") != actor_session["user_id"]
                or held.get("source_revision") != cfg["expected_revision"]
                or held.get("idempotency_key") != intent_key
                or any(
                    held.get(field) != lifecycle.get(field)
                    for field in ("product_id", "project_id")
                )
                or any(
                    not re.fullmatch(r"sha256:[a-f0-9]{64}", str(held.get(field)))
                    for field in ("history_ref", "idempotency_receipt")
                )
            ):
                raise ports.PortError(
                    "Product release hold has no durable original history or receipt"
                )
            ctx["document"]["product_evidence"] = {
                "source_revision": cfg["expected_revision"],
                "run_id": workflow_run_id,
                "held": held,
            }
            store.save(ctx["evaluation_id"], ctx["document"], manifest.read())
            return
        if result.get("cleanup", {}).get("fixture_removed") is not True:
            raise ports.PortError("Product retirement did not verify the owned fixture")
        if "api_recovery_cleanup" in selected_cases:
            recovery = result["results"]["api_recovery_cleanup"]
            if (
                recovery.get("cleanup_verified") is not True
                or recovery.get("shared_unchanged") is not True
                or recovery.get("product_id") != result["cleanup"].get("product_id")
                or not isinstance(recovery.get("attempts"), list)
                or not any(
                    isinstance(attempt, dict)
                    and attempt.get("state") == "response_unknown"
                    for attempt in recovery["attempts"]
                )
            ):
                raise ports.PortError(
                    "Product recovery did not verify interruption and cleanup"
                )
        manifest.mark("product_fixture", workflow_run_id, "deleted")
        ctx["document"]["product_evidence"] = {
            "source_revision": cfg["expected_revision"],
            "run_id": workflow_run_id,
            "workspace_id": workspace_id,
            "observation": proof,
            "results": result["results"],
            "cleanup": result["cleanup"],
        }

    mapping["journeys"] = journey
    ordinary_cleanup = mapping["cleanup"]

    def cleanup_after_product(ctx):
        manifest = ctx["manifest"]
        if (
            cfg.get("hold_for_rollback") is True
            and not recovering
            and ctx["document"].get("product_evidence", {}).get("held")
        ):
            try:
                state = manifest.read()
                store.save(ctx["evaluation_id"], ctx["document"], state)
                if store.load(ctx["evaluation_id"])[1] == state:
                    return False
            except Exception:
                pass

        if store is not None and (
            any(
                entry["kind"] in {"adp_org", "product_fixture"}
                for entry in manifest.outstanding()
            )
            or (
                any(
                    entry["kind"] in {"adp_user", "adp_user_email"}
                    for entry in manifest.outstanding()
                )
                and store._get(product_sessions.key(ctx["evaluation_id"])) is not None
            )
        ):
            ctx["product_recovery_session"] = product_sessions.restore(cfg, ctx, store)
        pending = [
            entry
            for entry in manifest.outstanding()
            if entry["kind"] == "product_fixture"
            and entry["id"] == workflow_run_id
            and entry["prefix"] == ctx["evaluation_id"]
        ]
        if pending:
            detail = pending[0]["detail"]
            recovery_intent = {
                "run_id": workflow_run_id,
                **{
                    key: detail.get(key)
                    for key in (
                        "tenant_id",
                        "source_revision",
                        "idempotency_key",
                    )
                },
                # The protected retirement resolves ownership against the ADP
                # `users` row: it matches ProductOperation.actor_id,
                # Product.owner_user_id, created_by and the binding provenance,
                # and its manifest forbids extra fields, so the row id is what
                # travels on the wire. The authenticated Cognito subject stays
                # in detail["actor_id"] and is verified separately; sending the
                # subject here made retirement refuse the run's own fixture.
                "actor_id": detail.get("actor_row_id"),
            }
            try:
                verify_retirement_ownership(cfg, ctx, pending[0], recovery_intent)
                if recovering:
                    wired["deleters"](cfg, ctx)["ec2_instance"](
                        ctx["document"]["instance_id"],
                        account=str(cfg["platform_account"]),
                        region=cfg["region"],
                    )
                    result = retire_after_termination(
                        cfg, wired["aws"], ctx, pending[0], recovery_intent, store=store
                    )
                else:
                    try:
                        result = wired["run_worker"](
                            ctx["document"]["instance_id"],
                            "product_cleanup",
                            {
                                "evaluation_id": ctx["evaluation_id"],
                                "instance_id": ctx["document"]["instance_id"],
                                "product_run_id": workflow_run_id,
                                "workflow_run_id": workflow_run_id,
                                "product_recovery_intent": recovery_intent,
                                "expected_revision": cfg["expected_revision"],
                                "internal_invoke_url": cfg["internal_invoke_url"],
                                # remote/common.clean_env pins AWS_ENDPOINT_URL_STS
                                # from this key, so the on-instance cleanup worker
                                # cannot build its environment without it.
                                "sts_endpoint": cfg["sts_endpoint"],
                                "work_dir": bundle.REMOTE_DIR,
                                "platform_account": str(cfg["platform_account"]),
                                "region": cfg["region"],
                            },
                            manifest=manifest,
                        )
                    except ports.PortError:
                        result = retire_after_termination(
                            cfg,
                            wired["aws"],
                            ctx,
                            pending[0],
                            recovery_intent,
                            store=store,
                        )
                receipt = result.get("detail") if isinstance(result, dict) else None
                if (
                    not isinstance(result, dict)
                    or result.get("success") is not True
                    or result.get("stage") != "complete"
                    or not isinstance(receipt, dict)
                    or receipt.get("run_id") != workflow_run_id
                    or receipt.get("fixture_removed") is not True
                    or not all(
                        isinstance(receipt.get(key), str) and receipt[key]
                        for key in ("product_id", "project_id")
                    )
                ):
                    raise ports.PortError(
                        "Product recovery retirement was not verified"
                    )
                manifest.mark("product_fixture", workflow_run_id, "deleted")
                ctx["document"]["product_cleanup_receipt"] = receipt
            except Exception:
                ordinary_cleanup(ctx)
                raise ports.PortError(
                    "Product retirement is still outstanding"
                ) from None
        result = ordinary_cleanup(ctx)
        if result and store is not None:
            completed_manifest = manifest.read()
            store.save(ctx["evaluation_id"], ctx["document"], completed_manifest)
            if store.load(ctx["evaluation_id"])[1] != completed_manifest:
                raise ports.PortError("Product cleanup state was not durably verified")
            product_sessions.clear(ctx, store)
        ctx.pop("product_recovery_session", None)
        return result

    mapping["cleanup"] = cleanup_after_product
    return mapping


def verify_retirement_ownership(cfg, ctx, pending, recovery_intent):
    evaluation_id = ctx["evaluation_id"]
    instance_id = ctx["document"]["instance_id"]
    run_id = recovery_intent["run_id"]
    workspace_id = (
        evaluation_id
        + "-product-"
        + hashlib.sha256((evaluation_id + ":product-workspace").encode()).hexdigest()[
            :24
        ]
    )
    detail = pending.get("detail", {})
    manifest = ctx["manifest"]
    if (
        not re.fullmatch(r"i-[a-f0-9]{8,17}", instance_id)
        or pending.get("id") != run_id
        or pending.get("prefix") != evaluation_id
        or pending.get("account") != str(cfg["platform_account"])
        or pending.get("region") != cfg["region"]
        or not isinstance(detail, dict)
        or detail.get("instance_id") != instance_id
        or detail.get("tenant_id") != workspace_id
        or detail.get("source_revision") != cfg["expected_revision"]
        or detail.get("idempotency_key")
        != "product-create-"
        + hashlib.sha256((run_id + ":" + evaluation_id).encode()).hexdigest()
        or not isinstance(detail.get("actor_id"), str)
        or not detail["actor_id"]
        # The owner's ADP `users` row, recorded alongside the subject by the
        # journey that created the fixture. A retirement that only knew the
        # subject could not name the account it still has to delete, and the
        # two are never the same value.
        or not isinstance(detail.get("actor_row_id"), str)
        or not detail["actor_row_id"]
        or detail["actor_row_id"] == detail["actor_id"]
        # The wire actor is the ADP `users` row, and the subject is kept
        # separately, so the two are checked against their own fields.
        or recovery_intent.get("actor_id") != detail["actor_row_id"]
        or not all(
            detail.get(key) == value
            for key, value in recovery_intent.items()
            if key not in ("run_id", "actor_id")
        )
        or not any(
            entry["kind"] == "ec2_instance"
            and entry["id"] == instance_id
            and entry.get("account") == str(cfg["platform_account"])
            and entry.get("region") == cfg["region"]
            for entry in manifest.outstanding()
        )
        or not any(
            entry["kind"] == "adp_user"
            and entry["id"] == workspace_id + "/" + detail["actor_row_id"]
            for entry in manifest.outstanding()
        )
    ):
        raise ports.PortError(
            "Independent Product retirement has no original run ownership"
        )


def retire_after_termination(cfg, aws, ctx, pending, recovery_intent, *, store=None):
    verify_retirement_ownership(cfg, ctx, pending, recovery_intent)
    instance_id = ctx["document"]["instance_id"]
    caller = aws.call("sts", "get_caller_identity")
    account = str(cfg["platform_account"])
    if caller.get("Account") != account or not re.fullmatch(
        rf"arn:aws:sts::{re.escape(account)}:assumed-role/adp-cli-uplift-eval-orchestrator/[A-Za-z0-9+=,.@_/-]+",
        caller.get("Arn", ""),
    ):
        raise ports.PortError(
            "Independent Product retirement needs the reviewed recovery role"
        )
    response = aws.call("ec2", "describe_instances", InstanceIds=[instance_id])
    instances = [
        item
        for reservation in response.get("Reservations", [])
        for item in reservation.get("Instances", [])
    ]
    if (
        len(instances) != 1
        or instances[0].get("InstanceId") != instance_id
        or instances[0].get("State", {}).get("Name") != "terminated"
    ):
        raise ports.PortError("Original Product evaluator is not confirmed terminated")
    try:
        return retire_independently(cfg, aws, recovery_intent)
    except RetirementConflict:
        if store is None:
            raise
        observed = product_inventory.observe(aws, cfg, cfg["expected_revision"])
        if observed.get("source_revision") != recovery_intent["source_revision"]:
            raise ports.PortError(
                "Product release changed before create reconciliation"
            )
        owner = product_sessions.restore(cfg, ctx, store, owner=True)
        expected_detail = pending.get("detail") or {}
        expected_row = expected_detail.get("actor_row_id")
        expected_subject = expected_detail.get("actor_id")
        if (
            not expected_row
            or not expected_subject
            # The restored session proves the subject it authenticated as; the
            # intent carries the ADP `users` row the retirement resolves.
            or owner["user_id"] != expected_subject
            or owner.get("adp_user_id") != expected_row
            or recovery_intent["actor_id"] != expected_row
            or owner["org_id"] != recovery_intent["tenant_id"]
        ):
            raise ports.PortError(
                "Product recovery actor differs from the original intent"
            ) from None
        body = create_request(recovery_intent["run_id"])
        attempts = ctx["document"].setdefault("product_recovery_attempts", [])
        attempts.append(
            {
                "state": "reconcile_started",
                "idempotency_key": recovery_intent["idempotency_key"],
                "source_revision": recovery_intent["source_revision"],
            }
        )
        store.save(ctx["evaluation_id"], ctx["document"], ctx["manifest"].read())
        status, receipt = product_sessions.request(
            cfg,
            # The mounted product_onboarding create route. The reviewed gateway
            # base already carries the external `/api` prefix CloudFront strips,
            # so spelling `/api/v1/products` here reconciled the original create
            # against `<base>/api/api/v1/products`, which nothing serves.
            "/v1/products",
            token=owner["access_token"],
            body=body,
            idempotency_key=recovery_intent["idempotency_key"],
            expected_status=201,
        )
        if (
            status != 201
            or not isinstance(receipt, dict)
            or receipt.get("tenant_id") != owner["org_id"]
            or receipt.get("revision") != 1
            or not all(
                isinstance(receipt.get(field), str) and receipt[field]
                for field in ("product_id", "project_id")
            )
            or any(
                receipt.get("product", {}).get(field) != body[field]
                for field in ("name", "purpose", "intended_users", "starting_point")
            )
        ):
            raise ports.PortError("Product original create could not be reconciled")
        attempts.append(
            {
                "state": "reconcile_received",
                "product_id": receipt["product_id"],
                "project_id": receipt["project_id"],
            }
        )
        store.save(ctx["evaluation_id"], ctx["document"], ctx["manifest"].read())
        return retire_independently(
            cfg,
            aws,
            {
                **recovery_intent,
                "product_id": receipt["product_id"],
                "project_id": receipt["project_id"],
            },
        )


def retire_independently(cfg, aws, recovery_intent):
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    account = str(cfg["platform_account"])
    region = cfg["region"]
    origin = cfg["internal_invoke_url"]
    caller = aws.call("sts", "get_caller_identity")
    if (
        caller.get("Account") != account
        or not re.fullmatch(
            rf"arn:aws:sts::{re.escape(account)}:assumed-role/adp-cli-uplift-eval-orchestrator/[A-Za-z0-9+=,.@_/-]+",
            caller.get("Arn", ""),
        )
        or not re.fullmatch(
            rf"https://[a-z0-9]+\.execute-api\.{re.escape(region)}\.amazonaws\.com/[A-Za-z0-9_-]+",
            origin,
        )
    ):
        raise ports.PortError(
            "Independent Product retirement identity or route differs"
        )
    url = origin + "/internal/v1/product-evaluation/retire"
    body = json.dumps(recovery_intent, sort_keys=True, separators=(",", ":")).encode()
    try:
        credentials = aws.session().get_credentials().get_frozen_credentials()
        signed = AWSRequest(
            method="POST",
            url=url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        SigV4Auth(credentials, "execute-api", region).add_auth(signed)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(
                self, request, response, code, message, headers, new_url
            ):
                return None

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        request = urllib.request.Request(
            url, data=body, headers=dict(signed.headers), method="POST"
        )
        with opener.open(request, timeout=20) as response:
            if response.status != 200:
                raise ports.PortError("Protected Product retirement was not accepted")
            raw = response.read(8193)
        if len(raw) > 8192:
            raise ports.PortError("Protected Product retirement receipt is oversized")
        receipt = json.loads(raw)
    except urllib.error.HTTPError as error:
        if error.code == 409:
            raise RetirementConflict(
                "Product retirement needs original-create reconciliation"
            ) from None
        raise ports.PortError(
            "Independent Product retirement was denied or unavailable"
        ) from None
    except Exception:
        raise ports.PortError(
            "Independent Product retirement was denied or unavailable"
        ) from None
    if (
        not isinstance(receipt, dict)
        or receipt.get("run_id") != recovery_intent["run_id"]
        or receipt.get("fixture_removed") is not True
        or type(receipt.get("cleanup_revision")) is not int
        or receipt["cleanup_revision"] < 1
        or not all(
            isinstance(receipt.get(key), str) and receipt[key]
            for key in ("product_id", "project_id")
        )
    ):
        raise ports.PortError("Independent Product retirement receipt is incomplete")
    return {"success": True, "stage": "complete", "detail": receipt}


def native_observation(cfg, inventory, aws):
    from .product_native import observe

    return observe(cfg, inventory, aws)


def recovery_available(store):
    if store is None or not all(
        callable(getattr(store, operation, None))
        for operation in (
            "_put",
            "_get",
            "save",
            "load",
            "bind_product_run",
            "product_run",
            "claim",
            "release",
        )
    ):
        raise ValueError(
            "Product cancellation recovery requires private durable state; Product writes refuse"
        )


def recovery_binding(cfg, workflow_run_id, evaluation_id):
    runner.check_evaluation_id(evaluation_id)
    return {
        "workflow_run_id": workflow_run_id,
        "evaluation_id": evaluation_id,
        "expected_revision": cfg["expected_revision"],
        "product_cases": cfg["product_cases"],
        "config_fingerprint": runner.fingerprint(cfg),
        "harness_commit": config.HARNESS_COMMIT,
    }


def recover(cfg, workflow_run_id, aws, *, store=None, supplied=None):
    account = str(cfg["platform_account"])
    caller = aws.call("sts", "get_caller_identity")
    if caller.get("Account") != account or not re.fullmatch(
        rf"arn:aws:sts::{re.escape(account)}:assumed-role/adp-cli-uplift-eval-orchestrator/[A-Za-z0-9+=,.@_/-]+",
        caller.get("Arn", ""),
    ):
        raise ports.PortError("Product recovery needs the reviewed recovery role")
    if store is None:
        store = statestore.default_store(cfg, aws)
    if store is None:
        raise ports.PortError("Product recovery requires encrypted durable state")
    if cfg.get("hold_for_rollback") is True:
        from .product_qualification_state import held_evaluation

        binding = held_evaluation(store, workflow_run_id)
    else:
        binding = store.product_run(workflow_run_id)
    if not isinstance(binding, dict) or not isinstance(
        binding.get("evaluation_id"), str
    ):
        raise ports.PortError("Product recovery has no bound evaluation identity")
    expected = recovery_binding(cfg, workflow_run_id, binding["evaluation_id"])
    if binding != expected:
        raise ports.PortError(
            "Product recovery binding differs from the selected release"
        )
    document, manifest = store.load(binding["evaluation_id"])
    if (
        not isinstance(document, dict)
        or document.get("product_run_id") != workflow_run_id
        or not isinstance(manifest, dict)
        or manifest.get("prefix") not in ("", binding["evaluation_id"])
        or not isinstance(manifest.get("resources"), list)
        or any(
            not isinstance(entry, dict)
            or entry.get("prefix") != binding["evaluation_id"]
            for entry in manifest["resources"]
        )
    ):
        raise ports.PortError(
            "Product recovery state is not owned by this workflow run"
        )
    workspace_id = (
        binding["evaluation_id"]
        + "-product-"
        + hashlib.sha256(
            (binding["evaluation_id"] + ":product-workspace").encode()
        ).hexdigest()[:24]
    )
    create_key = (
        "product-create-"
        + hashlib.sha256(
            (workflow_run_id + ":" + binding["evaluation_id"]).encode()
        ).hexdigest()
    )
    if any(
        entry.get("id") != workflow_run_id
        or entry.get("account") != str(cfg["platform_account"])
        or entry.get("region") != cfg["region"]
        or not isinstance(entry.get("detail"), dict)
        or entry["detail"].get("tenant_id") != workspace_id
        or entry["detail"].get("instance_id") != document.get("instance_id")
        or not re.fullmatch(r"i-[a-f0-9]{8,17}", str(document.get("instance_id", "")))
        or entry["detail"].get("source_revision") != cfg["expected_revision"]
        or entry["detail"].get("idempotency_key") != create_key
        for entry in manifest["resources"]
        if entry.get("kind") == "product_fixture"
    ):
        raise ports.PortError("Product recovery fixture is not owned by this run")
    with tempfile.TemporaryDirectory(prefix="product-recovery-") as root:
        work = Path(root)
        config_path = work / "config.json"
        config_path.write_text(json.dumps({**cfg, "product_run_id": workflow_run_id}))
        config_path.chmod(0o600)
        state_dir = work / "state"
        result = runner.main(
            [
                "--mode",
                "cleanup",
                "--restore",
                "--evaluation-id",
                binding["evaluation_id"],
                "--config",
                str(config_path),
                "--state-dir",
                str(state_dir),
            ],
            stages=lambda selected: build_stages(
                selected,
                {"source_revision": cfg["expected_revision"]},
                workflow_run_id,
                supplied,
                store=store,
                recovering=True,
            ),
            store=store,
        )
        document = runner.State(state_dir).read() or {}
        if result != 0 or document.get("cleanup_ok") is not True:
            raise ports.PortError("Product independent recovery is incomplete")
        return {"evaluation_id": binding["evaluation_id"], "cleanup_verified": True}


def evaluate(cfg, inventory, workflow_run_id, aws, *, supplied=None, store=None):
    native = native_observation(cfg, inventory, aws)
    if (
        not isinstance(native, dict)
        or native.get("source_revision") != cfg["expected_revision"]
        or native.get("database_heads") != native.get("image_heads")
        or not native.get("database_heads")
        or native.get("tick_verified") is not True
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(inventory.get("digest", "")))
        or native.get("tick_digest") != inventory["digest"]
        or not isinstance(native.get("observation_ref"), str)
        or not native["observation_ref"]
        or native.get("pod_uids") != [pod["uid"] for pod in inventory["pods"]]
        or not isinstance(native.get("database_heads"), list)
        or len(native["database_heads"]) != len(inventory["pods"])
        or any(
            not isinstance(head, str) or not head or len(head) > 128
            for head in native["database_heads"]
        )
        or len(set(native["database_heads"])) != 1
    ):
        raise ports.PortError("Product native migration and tick proof is incomplete")
    from .product_inventory import observe

    if observe(aws, cfg, cfg["expected_revision"]) != inventory:
        raise ports.PortError(
            "Product serving inventory changed after native observation"
        )
    if cfg.get("hold_for_rollback") is True:
        from .product_rollback_artifacts import qualify_baseline

        candidate, baseline, baseline_ref = qualify_baseline(cfg, native, aws)
    if store is None:
        store = statestore.default_store(cfg, aws)
    recovery_available(store)
    evaluation_id = runner.new_evaluation_id(time.time(), os.urandom(16).hex())
    if cfg.get("hold_for_rollback") is True:
        from .product_qualification_state import held_evaluation

        held_evaluation(
            store,
            workflow_run_id,
            recovery_binding(cfg, workflow_run_id, evaluation_id),
        )
    else:
        store.bind_product_run(
            workflow_run_id, recovery_binding(cfg, workflow_run_id, evaluation_id)
        )
    cfg = {**cfg, "product_run_id": workflow_run_id}
    with tempfile.TemporaryDirectory(prefix="product-evaluation-") as root:
        work = Path(root)
        config_path = work / "config.json"
        config_path.write_text(json.dumps(cfg))
        config_path.chmod(0o600)
        state_dir = work / "state"
        output_dir = work / "output"
        exit_code = runner.main(
            [
                "--config",
                str(config_path),
                "--suite",
                "case:E02",
                "--evaluation-id",
                evaluation_id,
                "--state-dir",
                str(state_dir),
                "--out-dir",
                str(output_dir),
                "--announce-id-to",
                os.environ.get("GITHUB_OUTPUT", ""),
            ],
            stages=lambda selected: build_stages(
                selected, inventory, workflow_run_id, supplied, store=store
            ),
            store=store,
        )
        document = runner.State(state_dir).read() or {}
        product = document.get("product_evidence") or {}
        if cfg.get("hold_for_rollback") is True:
            retained, manifest = store.load(evaluation_id)
            held = product.get("held")
            outstanding = (
                manifest.get("resources") if isinstance(manifest, dict) else None
            )
            if (
                exit_code != 1
                or document.get("cleanup_ok") is not False
                or any(
                    document.get("stages", {}).get(stage) != "complete"
                    for stage in runner.STAGES
                    if stage != "cleanup"
                )
                or document.get("stages", {}).get("cleanup") != "failed"
                or document.get("product_run_id") != workflow_run_id
                or product.get("source_revision") != cfg["expected_revision"]
                or product.get("run_id") != workflow_run_id
                or not isinstance(held, dict)
                or held.get("run_id") != workflow_run_id
                or manifest.get("prefix") != evaluation_id
                or not isinstance(outstanding, list)
                or not any(
                    entry.get("kind") == "product_fixture"
                    and entry.get("id") == workflow_run_id
                    and entry.get("prefix") == evaluation_id
                    and entry.get("status") == "pending"
                    and entry.get("account") == str(cfg["platform_account"])
                    and entry.get("region") == cfg["region"]
                    and isinstance(entry.get("detail"), dict)
                    and entry["detail"].get("tenant_id") == held.get("tenant_id")
                    and entry["detail"].get("actor_id") == held.get("actor_id")
                    and entry["detail"].get("idempotency_key")
                    == held.get("idempotency_key")
                    and entry["detail"].get("source_revision")
                    == cfg["expected_revision"]
                    for entry in outstanding
                )
                or not isinstance(retained, dict)
                or retained.get("product_evidence") != product
            ):
                raise ports.PortError(
                    "Product recovery hold is not durably owned and pending"
                )
        elif (
            exit_code != 0
            or document.get("cleanup_ok") is not True
            or product.get("source_revision") != cfg["expected_revision"]
            or product.get("run_id") != workflow_run_id
            or product.get("cleanup", {}).get("fixture_removed") is not True
        ):
            raise ports.PortError(
                "Retained evaluator did not verify Product lifecycle and cleanup"
            )
        if cfg.get("hold_for_rollback") is True:
            from .product_qualification_state import RetainedQualification, held_release
            from .product_rollback_artifacts import qualification

            held_release(store, workflow_run_id, native)
            state = RetainedQualification(store, candidate, baseline)
            state.retain(
                {
                    "status": "incomplete",
                    "candidate": qualification.release_identity(candidate),
                    "baseline": qualification.release_identity(baseline),
                    "candidate_ref": native["release_ref"],
                    "baseline_ref": baseline_ref,
                    "attempts": [],
                    "owned": None,
                }
            )
        return {
            "evaluation_id": document["evaluation_id"],
            "native": native,
            "product": product,
        }
