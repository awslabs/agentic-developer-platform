"""Select Product acceptance against an independently observed Gateway release."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from .product_profile_api import (
    CASES,
    PROTECTED_SELECTIONS,
    api_denial_oracle,
    api_product_lifecycle,
)
from .product_recovery import api_recovery_cleanup


def select(raw):
    cases = tuple(raw.split(","))
    if any(case not in CASES for case in cases) or len(cases) != len(set(cases)):
        raise ValueError("Product selection is missing, duplicated or unsupported")
    return cases


def workflow_run_id():
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", run_id) or not re.fullmatch(
        r"[1-9][0-9]{0,5}", attempt
    ):
        raise ValueError("Product selection requires an exact workflow run and attempt")
    return f"product-{run_id}-{attempt}"


def require_denial_context(cases, fixtures):
    if "api_denial_oracle" not in cases:
        return
    actors = [
        fixtures.get(field)
        for field in ("owner_token", "foreign_token", "revoked_token")
    ]
    if (
        not all(isinstance(actor, str) and actor for actor in actors)
        or len(set(actors)) != 3
    ):
        raise ValueError(
            "Product denial needs three distinct owned, foreign and revoked actor sessions"
        )
    if "api_product_lifecycle" in cases:
        if cases.index("api_denial_oracle") < cases.index("api_product_lifecycle"):
            raise ValueError("Product lifecycle must precede denial for one run")
    elif (
        not all(
            isinstance(fixtures.get(field), str) and fixtures[field]
            for field in ("product_id", "project_id", "edit_key")
        )
        or not isinstance(fixtures.get("edited_fields"), dict)
        or not fixtures["edited_fields"]
    ):
        raise ValueError("Product denial needs an owned operation receipt for replay")


def require_recovery_context(cases, fixtures, state_store):
    if "api_recovery_cleanup" not in cases:
        return
    if not all(
        callable(getattr(state_store, operation, None))
        for operation in ("load", "save")
    ):
        raise TypeError("Product recovery needs a protected durable state store")
    if not all(
        isinstance(fixtures.get(field), str) and fixtures[field]
        for field in ("foreign_token", "shared_product_id", "shared_project_id")
    ):
        raise ValueError(
            "Product recovery needs a separate shared-tenant Product fixture"
        )
    if fixtures["foreign_token"] == fixtures["owner_token"]:
        raise ValueError(
            "Product recovery needs distinct owner and shared actor sessions"
        )


def run(
    selection,
    expected_revision,
    fixtures,
    snapshot,
    client_factory,
    cleanup,
    *,
    state_store=None,
    retain_owned=None,
):
    cases = select(selection)
    if retain_owned is not None and (
        cases != ("api_product_lifecycle",) or not callable(retain_owned)
    ):
        raise ValueError(
            "Recovery hold requires the isolated Product lifecycle and a durable owner"
        )
    if not re.fullmatch(r"[a-f0-9]{40}", expected_revision):
        raise ValueError("Product selection needs a full source revision")
    if not callable(cleanup):
        raise TypeError("Product selection needs an owned-fixture recovery handler")
    if (
        not isinstance(fixtures, dict)
        or not all(
            isinstance(fixtures.get(field), str) and fixtures[field]
            for field in ("tenant_id", "other_tenant_id", "owner_token", "run_id")
        )
        or fixtures["tenant_id"] == fixtures["other_tenant_id"]
    ):
        raise ValueError("Product selection needs two distinct owned tenant fixtures")
    if "api_product_lifecycle" in cases and (
        not all(
            callable(getattr(state_store, operation, None))
            for operation in ("load", "save")
        )
        or not isinstance(fixtures.get("actor_id"), str)
        or not fixtures["actor_id"]
    ):
        raise ValueError(
            "Product lifecycle needs a durable ownership store and fixture actor"
        )
    require_denial_context(cases, fixtures)
    require_recovery_context(cases, fixtures, state_store)
    before = snapshot()
    if before != expected_revision:
        raise ValueError("Served Product release differs from selected source")
    results = {}
    complete = False
    try:
        for case in cases:
            if case == "api_product_lifecycle":
                results[case] = api_product_lifecycle(
                    client_factory,
                    tenant_id=fixtures["tenant_id"],
                    other_tenant_id=fixtures["other_tenant_id"],
                    owner_token=fixtures["owner_token"],
                    run_id=fixtures["run_id"],
                    actor_id=fixtures.get("actor_id"),
                    source_revision=before,
                    state_store=state_store,
                    idempotency_key=fixtures.get("idempotency_key"),
                )
            elif case == "api_denial_oracle":
                revocation = fixtures.get("revoke_actor")
                revocation_evidence = revocation() if callable(revocation) else None
                if revocation is not None and (
                    not isinstance(revocation_evidence, dict)
                    or revocation_evidence.get("membership_revoked") is not True
                ):
                    raise ValueError(
                        "Product member revocation did not produce verified evidence"
                    )
                receipt = results.get("api_product_lifecycle", fixtures)
                results[case] = api_denial_oracle(
                    client_factory,
                    tenant_id=fixtures["tenant_id"],
                    other_tenant_id=fixtures["other_tenant_id"],
                    owner_token=fixtures["owner_token"],
                    foreign_token=fixtures["foreign_token"],
                    revoked_token=fixtures["revoked_token"],
                    product_id=receipt["product_id"],
                    project_id=receipt["project_id"],
                    edit_key=receipt["edit_key"],
                    edited_fields=receipt["edited_fields"],
                    expected_product_id=fixtures.get(
                        "expected_product_id", receipt["product_id"]
                    ),
                )
                if revocation_evidence is not None:
                    results[case]["revocation"] = revocation_evidence
            elif case == "api_recovery_cleanup":
                results[case] = api_recovery_cleanup(
                    client_factory,
                    state_store,
                    cleanup,
                    tenant_id=fixtures["tenant_id"],
                    other_tenant_id=fixtures["other_tenant_id"],
                    owner_token=fixtures["owner_token"],
                    foreign_token=fixtures["foreign_token"],
                    shared_product_id=fixtures["shared_product_id"],
                    shared_project_id=fixtures["shared_project_id"],
                    run_id=fixtures["run_id"],
                    source_revision=before,
                )
            else:
                raise ValueError(f"Product case {case} is not implemented")
        if snapshot() != before:
            raise ValueError("Served Product release changed during acceptance")
        complete = True
        return results
    finally:
        if any(case != "api_recovery_cleanup" for case in cases):
            owned = dict(results)
            if "api_product_lifecycle" in cases and state_store is not None:
                document, manifest = state_store.load(f"lifecycle-{fixtures['run_id']}")
                if document is not None or manifest is not None:
                    if (
                        not isinstance(document, dict)
                        or not isinstance(manifest, dict)
                        or any(
                            document.get(field) != manifest.get(field)
                            or manifest.get(field) != expected
                            for field, expected in (
                                ("run_id", fixtures["run_id"]),
                                ("tenant_id", fixtures["tenant_id"]),
                                ("actor_id", fixtures.get("actor_id")),
                                ("source_revision", before),
                            )
                        )
                        or not isinstance(document.get("idempotency_key"), str)
                        or document["idempotency_key"]
                        != manifest.get("idempotency_key")
                    ):
                        raise ValueError(
                            "Product cleanup ownership manifest differs from the run"
                        )
                    owned["owned_manifest"] = manifest
            retained = False
            try:
                if complete and retain_owned is not None:
                    if retain_owned(owned) is not True:
                        raise ValueError(
                            "Product recovery hold was not durably retained"
                        )
                    retained = True
            finally:
                if not retained:
                    cleanup(owned)


def publish_cleanup_boundary(report, output_path, *, completed=False):
    if not output_path:
        return
    cleanup_ok = (
        completed is True
        and report.get("status") == "pass"
        and report.get("cleanup_verified") is True
        and report.get("observed_revision") == report.get("expected_revision")
        and bool(re.fullmatch(r"[a-f0-9]{40}", report.get("expected_revision") or ""))
        and bool(re.fullmatch(r"[a-f0-9]{40}", report.get("workflow_revision") or ""))
        and isinstance(report.get("observation_ref"), str)
        and bool(report["observation_ref"])
        and report.get("run_id") == workflow_run_id()
        and isinstance(report.get("selected_cases"), list)
        and bool(report["selected_cases"])
        and all(case in CASES for case in report["selected_cases"])
        and len(set(report["selected_cases"])) == len(report["selected_cases"])
    )
    with open(output_path, "a", encoding="utf-8") as output:
        output.write(f"cleanup_ok={'true' if cleanup_ok else 'false'}\n")


def resolved_config(expected_revision):
    from tests.e2e.cli_uplift import config

    if not re.fullmatch(r"[a-f0-9]{40}", expected_revision):
        raise ValueError("Product selection requires an exact source revision")
    if not os.environ.get("CLI_UPLIFT_EVAL_BINDINGS_JSON") or not os.environ.get(
        "CLI_UPLIFT_EVAL_GATEWAY_CATALOG"
    ):
        raise ValueError(
            "Protected Gateway deployment observation bindings are missing"
        )
    cfg = config.from_environment(
        {
            "CLI_UPLIFT_EVAL_BINDINGS": str(
                config.EXAMPLE_PATH.with_name("bindings.dev.json")
            ),
            "CLI_UPLIFT_EVAL_BINDINGS_JSON": os.environ[
                "CLI_UPLIFT_EVAL_BINDINGS_JSON"
            ],
            "CLI_UPLIFT_EVAL_GATEWAY_CATALOG": os.environ[
                "CLI_UPLIFT_EVAL_GATEWAY_CATALOG"
            ],
            "CLI_UPLIFT_EVAL_EXPECTED_REVISION": expected_revision,
            "CLI_UPLIFT_EVAL_INSTANCE_PROFILE": os.environ.get(
                "CLI_UPLIFT_EVAL_INSTANCE_PROFILE", ""
            ),
            "CLI_UPLIFT_EVAL_SECURITY_GROUP_ID": os.environ.get(
                "CLI_UPLIFT_EVAL_SECURITY_GROUP_ID", ""
            ),
            "CLI_UPLIFT_EVAL_STATE_BUCKET": os.environ.get(
                "CLI_UPLIFT_EVAL_STATE_BUCKET", ""
            ),
            "CLI_UPLIFT_EVAL_CREDENTIAL_SECRET_NAME": os.environ.get(
                "CLI_UPLIFT_EVAL_CREDENTIAL_SECRET_NAME", ""
            ),
            "CLI_UPLIFT_EVAL_INTERNAL_INVOKE_URL": os.environ.get(
                "CLI_UPLIFT_EVAL_INTERNAL_INVOKE_URL", ""
            ),
        }
    )
    if not cfg.get("gateway_deployment"):
        raise ValueError("Protected Gateway deployment binding is missing")
    required = {
        "expected_revision": expected_revision,
        "instance_profile": os.environ.get("CLI_UPLIFT_EVAL_INSTANCE_PROFILE"),
        "state_bucket": os.environ.get("CLI_UPLIFT_EVAL_STATE_BUCKET"),
        "credential_secret_name": os.environ.get(
            "CLI_UPLIFT_EVAL_CREDENTIAL_SECRET_NAME"
        ),
        "internal_invoke_url": os.environ.get("CLI_UPLIFT_EVAL_INTERNAL_INVOKE_URL"),
    }
    if any(not value or cfg.get(key) != value for key, value in required.items()):
        raise ValueError(
            "Protected Product evaluator launch and selected release bindings are missing or mismatched"
        )
    return cfg


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--dispatch-report")
    parser.add_argument("--api-evidence")
    parser.add_argument("--hold-for-rollback", action="store_true")
    args = parser.parse_args(argv)
    if args.hold_for_rollback and (
        args.cases != "api_product_lifecycle" or args.api_evidence
    ):
        raise ValueError("Product recovery hold is a separate non-acceptance lifecycle")
    report = {
        "selected_cases": [],
        "expected_revision": args.expected_revision,
        "workflow_revision": os.environ.get("GITHUB_SHA", ""),
        "run_id": None,
        "observed_revision": None,
        "observation_ref": None,
        "status": "incomplete",
        "cleanup_verified": False,
    }
    try:
        report["selected_cases"] = list(select(args.cases))
        if args.cases not in PROTECTED_SELECTIONS:
            raise ValueError("Protected Product runner requires canonical case order")
        if not re.fullmatch(r"[a-f0-9]{40}", args.expected_revision):
            raise ValueError("Product selection requires an exact source revision")
        if os.environ.get("PRODUCT_API_FIXTURES_JSON"):
            raise ValueError("Static Product actor fixtures are not authorized")
        report["run_id"] = workflow_run_id()
        from tests.e2e.cli_uplift import ports

        from .product_inventory import observe

        cfg = {
            **resolved_config(args.expected_revision),
            "product_cases": args.cases,
            **({"hold_for_rollback": True} if args.hold_for_rollback else {}),
        }
        transport = ports.default_ports(cfg)
        identity = transport["aws"].call("sts", "get_caller_identity")
        account = str(cfg["platform_account"])
        if identity.get("Account") != account or not re.fullmatch(
            rf"arn:aws:sts::{re.escape(account)}:assumed-role/adp-cli-uplift-eval-orchestrator/[A-Za-z0-9+=,.@_/-]+",
            str(identity.get("Arn", "")),
        ):
            raise ValueError(
                "Protected Product evaluator identity differs from the selected account and role"
            )
        inventory = observe(transport["aws"], cfg, args.expected_revision)
        report["observed_revision"] = inventory["source_revision"]
        if report["observed_revision"] != args.expected_revision:
            raise ValueError(
                "Served Gateway release differs from selected Product release"
            )
        from . import product_execution

        evaluation = product_execution.evaluate(
            cfg, inventory, report["run_id"], transport["aws"]
        )
        report["observation_ref"] = evaluation["native"]["observation_ref"]
        if args.hold_for_rollback:
            report["evaluation_id"] = evaluation["evaluation_id"]
            report["status"] = "held"
            return
        if args.api_evidence:
            output = Path(args.api_evidence)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(
                    {
                        "run_id": report["run_id"],
                        "source_revision": report["observed_revision"],
                        "workflow_revision": report["workflow_revision"],
                        "observation_ref": report["observation_ref"],
                        "evaluation_id": evaluation["evaluation_id"],
                        "inventory": {
                            "source_revision": inventory["source_revision"],
                            "digest": inventory["digest"],
                            "pods": [
                                {"uid": pod["uid"], "digest": pod["digest"]}
                                for pod in inventory["pods"]
                            ],
                        },
                        "native": evaluation["native"],
                        "product": evaluation["product"],
                    },
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            )
            output.chmod(0o600)
        report["cleanup_verified"] = True
        report["status"] = "pass"
    finally:
        if args.dispatch_report:
            output = Path(args.dispatch_report)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
        publish_cleanup_boundary(
            report,
            os.environ.get("GITHUB_OUTPUT"),
            completed=report["status"] == "pass",
        )


if __name__ == "__main__":
    main()
