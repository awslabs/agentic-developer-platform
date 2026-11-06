"""Bind V04 installed-CLI selectors to the browser fixture's private receipt."""

import hashlib
import json
import re
from pathlib import Path

from tests.e2e.product_onboarding.product_v04_evidence import SURFACE_SELECTORS
from tests.e2e.product_onboarding.product_v04_publish import private_file


def scope_cli_evaluator(evidence_root, environment):
    required = (
        "PRODUCT_EXPECTED_REVISION",
        "PRODUCT_BROWSER_WORKFLOW_REVISION",
        "GITHUB_SHA",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
    )
    if (
        environment.get("PRODUCT_B03_MODE") != "true"
        or environment.get("E2E_NEW_UI_ENABLED") != "1"
        or any(
            not re.fullmatch(r"[0-9a-f]{40}", environment.get(key, ""))
            for key in required[:3]
        )
        or any(
            not environment.get(key, "").isdecimal() or int(environment[key]) <= 0
            for key in required[3:]
        )
    ):
        raise ValueError("B03 evaluator needs a pinned, authenticated browser attempt")
    root = Path(evidence_root).resolve() / "test-results"
    run = int(environment["GITHUB_RUN_ID"])
    attempt = int(environment["GITHUB_RUN_ATTEMPT"])
    run_id = f"product-{run}-{attempt}"
    source = environment["PRODUCT_EXPECTED_REVISION"]
    workflow = environment["PRODUCT_BROWSER_WORKFLOW_REVISION"]
    checkout = environment["GITHUB_SHA"]

    def read(reference):
        return json.loads(
            private_file(root, reference).read_text(),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError("invalid JSON")
            ),
        )

    def evaluate(selector):
        criteria = {
            values["cli"]: criterion for criterion, values in SURFACE_SELECTORS.items()
        }
        if selector not in criteria:
            raise ValueError("V04 CLI selector is not in the reviewed inventory")
        browser_path = root / "product-v04-browser-results.json"
        cli_path = root / "product-v04-cli-results.json"
        browser, cli = read(browser_path), read(cli_path)
        if not isinstance(browser, dict) or not isinstance(cli, dict):
            raise TypeError("V04 CLI and browser receipts must be records")
        if any(
            browser.get(key) != expected
            for key, expected in (
                ("run_id", run_id),
                ("source_revision", source),
                ("workflow_revision", workflow),
                ("checkout_revision", checkout),
                ("workflow_run_id", run),
                ("workflow_attempt", attempt),
            )
        ) or any(
            cli.get(key) != expected
            for key, expected in (
                ("run_id", run_id),
                ("source_revision", source),
                ("workflow_revision", workflow),
                ("workflow_run_id", run),
                ("workflow_attempt", attempt),
            )
        ):
            raise ValueError(
                "V04 CLI and browser receipts differ from the selected attempt"
            )
        context = browser.get("context")
        identity = context.get("identities") if isinstance(context, dict) else None
        deployment = context.get("deployment") if isinstance(context, dict) else None
        observed = deployment.get("observed") if isinstance(deployment, dict) else None
        checksum = cli.get("installed_cli_sha256")
        if (
            not isinstance(identity, dict)
            or not isinstance(identity.get("requirements_revision"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", identity["requirements_revision"])
            or identity.get("source_revision") != source
            or not isinstance(checksum, str)
            or not re.fullmatch(r"[0-9a-f]{64}", checksum)
            or not isinstance(identity.get("cli_checksum"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", identity["cli_checksum"])
            or cli.get("release_manifest_sha256") != identity["cli_checksum"]
        ):
            raise ValueError(
                "V04 CLI artifact or requirements differ from the signed release"
            )
        if (
            not isinstance(observed, dict)
            or observed.get("source_revision") != source
            or observed.get("cli_checksum") != identity["cli_checksum"]
        ):
            raise ValueError("V04 CLI manifest differs from the deployed runtime")
        read(deployment.get("observation_ref"))
        cleanup = read(browser.get("cleanup_ref"))
        cli_cleanup = cli.get("cleanup")
        if (
            not isinstance(cleanup, dict)
            or cleanup.get("verified") is not True
            or not isinstance(cli_cleanup, dict)
            or cli_cleanup.get("status") != "verified"
            or cli_cleanup.get("original_projects_preserved") is not True
            or cli_cleanup.get("reused_assets_preserved") is not True
        ):
            raise ValueError("V04 owned fixture cleanup is not verified")
        cases = cli.get("cases")
        if not isinstance(cases, dict) or set(cases) != set(SURFACE_SELECTORS):
            raise ValueError("V04 installed CLI case inventory is incomplete")
        criterion = criteria[selector]
        selected = cases.get(criterion)
        if not isinstance(selected, dict):
            raise TypeError("V04 installed CLI case has no protected attempts")
        for field in (
            ("attempts", "api_attempts") if criterion == "AC-03" else ("attempts",)
        ):
            records = selected.get(field)
            if not isinstance(records, list) or not records:
                raise ValueError("V04 installed CLI case has no protected attempts")
            for record in records:
                if (
                    not isinstance(record, dict)
                    or record.get("run_id") != run_id
                    or record.get("source_revision") != source
                    or record.get("requirements_revision")
                    != identity["requirements_revision"]
                    or record.get("workflow_run_id") != run
                    or record.get("workflow_attempt") != attempt
                    or record.get("test_ref") != selector
                    or record.get("surface")
                    != ("api" if field == "api_attempts" else "cli")
                    or not record.get("attempt_id")
                ):
                    raise ValueError(
                        "V04 installed CLI attempt differs from the selected run"
                    )
                if field == "attempts" and (
                    record.get("installed_cli_sha256") != checksum
                    or record.get("exit_code") != 0
                    and record.get("result") == "pass"
                ):
                    raise ValueError(
                        "V04 installed CLI command differs from the installed artifact"
                    )
                for key in (
                    ("artifact_ref", "readback_ref")
                    if field == "api_attempts"
                    else ("artifact_ref", "readback_ref", "stdout_ref")
                ):
                    private_file(root, record.get(key))
            if records[-1].get("result") != "pass":
                raise ValueError("V04 installed CLI case did not pass")
        return {
            "selector": selector,
            "status": "passed",
            "run_id": run_id,
            "evidence_ref": "evidence/test-results/product-v04-cli-results.json",
            "report_path": str(cli_path),
            "report_sha256": hashlib.sha256(cli_path.read_bytes()).hexdigest(),
        }

    return evaluate
