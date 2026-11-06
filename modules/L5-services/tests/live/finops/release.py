"""Service exports for the shared release dispatcher's positional contract."""

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
import re
from pathlib import Path

from driver import ROOT, FinOpsDriver, private_path, run_qualification
from evaluator import evaluate
from fixtures import materialize_clients, write_private


def arguments(*, emitter):
    parser = argparse.ArgumentParser(description="Bounded private FinOps release qualification")
    if not emitter:
        parser.add_argument("action", choices=("check-browser", "preflight", "run"))
    parser.add_argument("source_sha")
    parser.add_argument("fixture")
    parser.add_argument("evidence_dir")
    selected = parser.parse_args()
    manifest = json.loads(private_path(selected.fixture).read_text())
    if (re.fullmatch(r"[a-f0-9]{40}", selected.source_sha) is None
            or not isinstance(manifest, dict)
            or manifest.get("source_sha", manifest.get("release_sha")) != selected.source_sha
            or manifest.get("release_sha", selected.source_sha) != selected.source_sha):
        raise ValueError("FinOps fixture differs from the selected release revision")
    manifest["release_sha"] = selected.source_sha
    manifest = materialize_clients(manifest, Path(selected.fixture).parent)
    return selected, manifest


def qualify():
    selected, manifest = arguments(emitter=False)
    if selected.action == "run":
        if "bootstrap" in manifest:
            from bootstrap import prepare

            manifest = prepare(manifest, selected.evidence_dir)
        run_qualification(manifest, selected.evidence_dir)
        return
    driver = FinOpsDriver(manifest, selected.evidence_dir, mode="preflight")
    try:
        if selected.action == "check-browser":
            driver.check_browser_dependencies()
        else:
            driver.installation_preflight()
    finally:
        driver.close()


def qualification_report(identity, fixture_digest, record, observation_path, run):
    requirements = (ROOT / "docs/l5-services/finops/stories.json").read_bytes()
    plan = json.loads(requirements)
    sections = {"epic_criteria": set(), "story_criteria": set(), "scenarios": set(), "live_checks": set()}
    for story in plan["stories"]:
        for criterion in story["acceptance"]:
            sections["epic_criteria"].update(criterion["epic_criteria"])
            sections["story_criteria"].add(f"{story['alias']}/{criterion['id']}")
            sections["scenarios"].add(f"{story['alias']}/{criterion['scenario_id']}")
            if criterion["verification"] == "manual":
                sections["live_checks"].add(f"{story['alias']}/{criterion['test']}")
    report = {
        **identity, "phase": "qualification", "component": "l5-finops", "job": "qualify-selected",
        "workflow_path": ".github/workflows/l5-services-release.yml",
        "run_id": str(identity["run_id"]), "run_attempt": str(identity["run_attempt"]),
        "correlation_id": identity["correlation"], "requirements_revision": plan["revision"],
        "requirements_sha256": hashlib.sha256(requirements).hexdigest(),
        "fixture_sha256": fixture_digest, "observed_at": datetime.now(UTC).isoformat(),
        "qualification_scope": "selected_product_native_readback",
        "outcome": "incomplete", "validation": "schema_only", "native_ready": False,
        "live_acceptance": "not_evaluated", "native_console_automation": "prohibited", "gateway_revision": None,
        "product_run_id": run["run_id"], "scope": record["scope"],
        "native_observation": {"artifact": observation_path.name,
            "sha256": hashlib.sha256(observation_path.read_bytes()).hexdigest()},
        "readbacks": [{"id": name, "outcome": "passed", "evidence_verified": True,
            "verification_layer": "schema-only", "observation": "schema_only",
            "sha256": hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
            for name, value in (("original_run", run),
                                ("native_answer", record["native"]), ("billing", record["billing"]),
                                ("clients", record["clients"]), ("publication", record["publication"]))],
        "cleanup": {"outcome": "passed", "evidence_verified": True, "status": "retained",
            "scope": "original_run_retention", "run_id": run["run_id"],
            "verification_layer": "schema-only", "readback": record["run_readback_sha256"]},
        "unresolved": ["installed_release:not-observed", "independent-live:not-established",
            "aws_native_console_automation:prohibited_security_restriction",
            "documented_native_inquiry_api:not-qualified"],
    }
    for section, identifiers in sections.items():
        report[section] = [{"id": identifier, "outcome": "incomplete", "evidence_verified": False,
                            "unresolved_reasons": ["not-evaluated-by-selected-run-readback"]}
                           for identifier in sorted(identifiers)]
        report["unresolved"].extend(f"{section}/{identifier}:not-evaluated" for identifier in sorted(identifiers))
    return report


def emit():
    from release_context import release_identity

    selected, manifest = arguments(emitter=True)
    identity = release_identity(os.environ)
    fixture_digest = hashlib.sha256(private_path(selected.fixture).read_bytes()).hexdigest()
    if (identity["service"] != "finops" or identity["source_revision"] != selected.source_sha
            or os.environ.get("L5_FIXTURE_DIGEST") != fixture_digest):
        raise ValueError("FinOps qualification differs from the selected release or fixture")
    if "bootstrap" in manifest:
        from bootstrap import read_selection

        manifest = read_selection(manifest, private_path(selected.evidence_dir, directory=True))
    driver = FinOpsDriver(manifest, selected.evidence_dir, mode="readback")
    try:
        path = private_path(driver.directory / f"finops-{manifest['receipt']}-qualification.json")
        if path.stat().st_size > 65536:
            raise ValueError("FinOps qualification observation is oversized")
        record = json.loads(path.read_text())
        if not isinstance(record, dict):
            raise ValueError("FinOps qualification observation must be an object")
        expected_digest = record.pop("sha256", None)
        digest = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if (expected_digest != digest or record.get("validation") != "schema_only"
                or record.get("kind") != "finops_qualification_observation"
                or record.get("selected_release_sha") != selected.source_sha
                or record.get("scope") != manifest["scope"] or record.get("run_id") != driver.owned_run_id):
            raise ValueError("FinOps observation is not bound to this release and original run")
        driver.preflight()
        run = driver.read_run(driver.owned_run_id)
        result = driver.read_result(driver.owned_run_id)
        answer = driver.native_answer(result)
        clients = {"cli": driver.cli_result(result), "mcp": driver.mcp_result(result),
                   "ui": driver.ui_result(result, answer)}
        publication = driver.publication(result)
        evaluate(manifest, run, result, answer, clients, publication)

        def digest_of(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        native = result["native"]
        billing = run["admission"]["billing_scope"]
        if (record.get("configuration_revision") != manifest["configuration_revision"]
                or record.get("binding_id") != manifest["binding_id"]
                or record.get("result_id") != result["result_id"] or record.get("revision") != result["revision"]
                or record.get("native") != {"conversation_id": native["conversation_id"],
                    **{field: native[field] for field in ("turn_id", "turn_locator") if native.get(field)},
                    "answer_artifact": native["answer_artifact"]}
                or record.get("billing") != {"view_ref": billing["view_ref"], "account_ids": billing["account_ids"],
                    "scope_digest": billing["scope_digest"], "snapshot_artifact": billing["snapshot_artifact"],
                    "query_hash": result["oracle"]["query_hash"], "collection_id": result["oracle"]["collection_id"],
                    "pages": result["oracle"]["pages"]}
                or record.get("run_readback_sha256") != digest_of(run)
                or record.get("result_sha256") != digest_of(result)
                or record.get("clients") != {surface: digest_of(value) for surface, value in clients.items()}
                or record.get("publication") != {item["destination"]: {
                    "receipt_id": item["receipt_id"], "projection_verified_on_ack": item["projection_verified_on_ack"]}
                    for item in publication["consumers"]}):
            raise ValueError("FinOps observation differs from fresh authoritative readbacks")
        report = qualification_report(identity, fixture_digest, record, path, run)
        write_private(driver.directory / "qualification.json", report)
        print(json.dumps({"validation": "schema_only", "source_sha": selected.source_sha,
                          "observation_sha256": expected_digest, "run_id": driver.owned_run_id}))
    finally:
        driver.close()
