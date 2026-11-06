"""Assemble sanitized B05 evidence without promoting missing live work to acceptance."""

import hashlib
import json
import re

SCHEMA = "product-v06/v1"
BOLT_ID = "B05"
DECISION_PROCEDURE = (
    "docs/delivery-flow/plans/product-onboarding-8141/human-checks.md"
    "#record-one-decision-using-the-existing-verification-stories"
)
CASES = {
    "AC-01": ("SCN-01", "tests/e2e/product_onboarding/test_v06.py::live_sources_reuse",
              "Discovered repository and AWS IDs match independent readback without provider mutation"),
    "AC-02": ("SCN-02", "tests/e2e/product_onboarding/test_v06.py::live_source_status",
              "Partial and unavailable coverage is honest and expired or revoked access removes readiness and details"),
    "AC-03": ("SCN-03", "tests/e2e/product_onboarding/test_v06.py::live_source_recovery",
              "Cursor resume and verification avoid duplicates and owned cleanup preserves other values and providers"),
}
SURFACES = ("api", "installed_cli", "browser", "independent_provider")
ARTIFACTS = ("api_image_digest", "ui_manifest_sha256", "cli_sha256", "api_workflow_revision",
             "browser_workflow_revision", "cli_workflow_revision", "schema_head")
REVISION = re.compile(r"[a-f0-9]{40}\Z")
DIGEST = re.compile(r"(?:sha256:)?[a-f0-9]{64}\Z")
REFERENCE = re.compile(r"(?:evidence|receipts)/(?:[a-z0-9][a-z0-9._-]{0,99}/)*[a-z0-9][a-z0-9._-]{0,99}\Z")
SUMMARY = re.compile(r"[A-Za-z0-9 ,.:%()'/-]+\Z")


def build_packet(inputs, *, schema=SCHEMA, bolt_id=BOLT_ID, cases_spec=CASES):
    """Keep all supplied attempts, but make every required failure visibly incomplete."""
    if not isinstance(inputs, dict):
        raise TypeError("V06 evidence inputs must be a mapping")
    issues = []

    def reference(value, label):
        if not isinstance(value, str) or not REFERENCE.fullmatch(value) or ".." in value or re.search(r"[0-9]{12}", value):
            issues.append(f"{label} needs a protected opaque reference")
            return None
        return value

    def summary(value, label):
        if (not isinstance(value, str) or not value.strip() or len(value) > 500 or not SUMMARY.fullmatch(value)
                or re.search(r"https?://|arn:aws|\b[0-9]{7,}\b|[A-Za-z0-9_-]{24,}", value, re.I)):
            issues.append(f"{label} needs a sanitized summary")
            return None
        return value

    run_id = inputs.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", run_id) or re.search(r"[0-9]{12}", run_id):
        issues.append("run identity is invalid")
        run_id = None
    pinned = {field: inputs.get(field) for field in ("source_revision", "requirements_revision", "plan_revision")}
    for field, value in pinned.items():
        if not isinstance(value, str) or not REVISION.fullmatch(value):
            issues.append(f"{field} is not a full commit SHA")
            pinned[field] = None
    provided = inputs.get("artifacts") if isinstance(inputs.get("artifacts"), dict) else {}
    artifacts = {}
    for field in ARTIFACTS:
        value = provided.get(field)
        pattern = DIGEST if field.endswith(("digest", "sha256")) else REVISION
        if field == "schema_head":
            pattern = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
        if not isinstance(value, str) or not pattern.fullmatch(value):
            issues.append(f"{field} is not a pinned artifact")
            value = None
        artifacts[field] = value
    supplied_deployment = inputs.get("deployment") if isinstance(inputs.get("deployment"), dict) else {}
    deployment = {field: supplied_deployment.get(field) if supplied_deployment.get(field) == artifacts[field] else None
                  for field in ARTIFACTS}
    deployment["source_revision"] = (supplied_deployment.get("source_revision")
                                     if supplied_deployment.get("source_revision") == pinned["source_revision"] else None)
    if any(deployment[field] is None for field in (*ARTIFACTS, "source_revision")):
        issues.append("deployed source, schema, workflow or client differs from pinned artifacts")
    deployment["receipt_ref"] = reference(supplied_deployment.get("receipt_ref"), "deployment receipt")
    deployment["observation_ref"] = reference(supplied_deployment.get("observation_ref"), "deployment readback")
    if deployment["receipt_ref"] == deployment["observation_ref"]:
        issues.append("deployment readback cannot reuse its receipt")

    supplied_surfaces = inputs.get("surfaces") if isinstance(inputs.get("surfaces"), dict) else {}
    if set(supplied_surfaces) != set(SURFACES):
        issues.append("API, CLI, browser and provider surface reports must be separate")
    reports = {}
    report_refs = []
    for surface in SURFACES:
        entry = supplied_surfaces.get(surface) if isinstance(supplied_surfaces.get(surface), dict) else {}
        report_ref = reference(entry.get("report_ref"), f"{surface} report")
        report_refs.append(report_ref)
        reports[surface] = {"report_ref": report_ref, "run_id": run_id if entry.get("run_id") == run_id else None,
                            "source_revision": pinned["source_revision"] if entry.get("source_revision") == pinned["source_revision"] else None,
                            "status": entry.get("status") if entry.get("status") == "pass" else None,
                            "workflow_run_id": entry.get("workflow_run_id") if type(entry.get("workflow_run_id")) is int else None,
                            "workflow_attempt": entry.get("workflow_attempt") if type(entry.get("workflow_attempt")) is int else None}
        workflow_field = {"api": "api_workflow_revision", "browser": "browser_workflow_revision",
                          "installed_cli": "cli_workflow_revision"}.get(surface)
        if workflow_field:
            reports[surface]["workflow_revision"] = (artifacts[workflow_field]
                                                      if entry.get("workflow_revision") == artifacts[workflow_field] else None)
            if reports[surface]["workflow_revision"] is None:
                issues.append(f"{surface} report has a stale workflow revision")
        if (reports[surface]["run_id"] is None or reports[surface]["source_revision"] is None
                or reports[surface]["status"] is None or not isinstance(reports[surface]["workflow_run_id"], int)
                or reports[surface]["workflow_run_id"] <= 0 or not isinstance(reports[surface]["workflow_attempt"], int)
                or reports[surface]["workflow_attempt"] <= 0):
            issues.append(f"{surface} report is missing, failed or from another run")
    if len(set(report_refs)) != len(SURFACES):
        issues.append("Surface reports cannot share an artifact")

    supplied_cases = inputs.get("cases") if isinstance(inputs.get("cases"), dict) else {}
    if set(supplied_cases) != set(cases_spec):
        issues.append("V06 case inventory is incomplete or unexpected")
    cases = {}
    used_refs = set()
    for criterion, (scenario, selector, expected) in cases_spec.items():
        supplied = supplied_cases.get(criterion) if isinstance(supplied_cases.get(criterion), dict) else {}
        if supplied.get("scenario_id") != scenario or supplied.get("test_ref") != selector:
            issues.append(f"{criterion} scenario selector is missing or mismatched")
        actual = summary(supplied.get("actual"), f"{criterion} actual")
        report_ref = reference(supplied.get("report_ref"), f"{criterion} scenario report")
        if report_ref in used_refs:
            issues.append(f"{criterion} scenario report must be separate")
        used_refs.add(report_ref)
        provided_attempts = supplied.get("attempts")
        if not isinstance(provided_attempts, list) or not provided_attempts:
            issues.append(f"{criterion} needs attempt records")
            provided_attempts = []
        attempts = []
        attempt_ids = set()
        for index, supplied_attempt in enumerate(provided_attempts):
            entry = supplied_attempt if isinstance(supplied_attempt, dict) else {}
            surface = entry.get("surface")
            attempt_id = entry.get("attempt_id")
            if (surface not in SURFACES or not isinstance(attempt_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_/-]{1,100}", attempt_id) or attempt_id in attempt_ids):
                issues.append(f"{criterion} attempt {index} has invalid surface or identity")
                attempt_id = None
            attempt_ids.add(attempt_id)
            if (entry.get("run_id") != run_id or entry.get("source_revision") != pinned["source_revision"]
                    or entry.get("requirements_revision") != pinned["requirements_revision"]
                    or entry.get("test_ref") != selector or type(entry.get("workflow_run_id")) is not int
                    or entry["workflow_run_id"] <= 0 or type(entry.get("workflow_attempt")) is not int
                    or entry["workflow_attempt"] <= 0):
                issues.append(f"{criterion} attempt {index} differs from the pinned run or workflow")
            if entry.get("result") not in ("pass", "fail", "skip", "not_run", "interrupted"):
                issues.append(f"{criterion} attempt {index} has unknown result")
            artifact_ref = reference(entry.get("artifact_ref"), f"{criterion} attempt {index} artifact")
            readback_ref = reference(entry.get("readback_ref"), f"{criterion} attempt {index} independent readback")
            if artifact_ref == readback_ref:
                issues.append(f"{criterion} attempt {index} needs a separate readback")
            attempt = {
                "attempt_id": attempt_id, "surface": surface if surface in SURFACES else None,
                "test_ref": selector if entry.get("test_ref") == selector else None,
                "run_id": run_id if entry.get("run_id") == run_id else None,
                "source_revision": pinned["source_revision"] if entry.get("source_revision") == pinned["source_revision"] else None,
                "requirements_revision": pinned["requirements_revision"] if entry.get("requirements_revision") == pinned["requirements_revision"] else None,
                "workflow_run_id": entry.get("workflow_run_id") if type(entry.get("workflow_run_id")) is int else None,
                "workflow_attempt": entry.get("workflow_attempt") if type(entry.get("workflow_attempt")) is int else None,
                "result": entry.get("result") if entry.get("result") in ("pass", "fail", "skip", "not_run", "interrupted") else None,
                "expected": expected, "actual": summary(entry.get("actual"), f"{criterion} attempt {index} actual"),
                "artifact_ref": artifact_ref, "readback_ref": readback_ref,
            }
            if surface == "browser":
                attempt["screenshot_ref"] = reference(entry.get("screenshot_ref"), f"{criterion} browser screenshot")
                attempt["trace_ref"] = reference(entry.get("trace_ref"), f"{criterion} browser trace")
            if surface == "installed_cli":
                attempt["command"] = summary(entry.get("command"), f"{criterion} CLI command")
                for field in ("stdout_ref", "stderr_ref"):
                    attempt[field] = reference(entry.get(field), f"{criterion} CLI {field}")
                attempt["exit_code"] = entry.get("exit_code") if type(entry.get("exit_code")) is int else None
                attempt["installed_cli_sha256"] = (entry.get("installed_cli_sha256")
                                                   if entry.get("installed_cli_sha256") == artifacts["cli_sha256"] else None)
                if attempt["exit_code"] != 0 or attempt["installed_cli_sha256"] is None:
                    issues.append(f"{criterion} CLI attempt failed or uses another installation")
            attempts.append(attempt)
        final_artifacts = []
        for surface in SURFACES:
            matching = [attempt for attempt in attempts if attempt["surface"] == surface]
            if not matching or matching[-1]["result"] != "pass":
                issues.append(f"{criterion}.{surface} has no final passing attempt")
            elif (matching[-1]["workflow_run_id"] != reports[surface]["workflow_run_id"]
                  or matching[-1]["workflow_attempt"] != reports[surface]["workflow_attempt"]):
                issues.append(f"{criterion}.{surface} does not match its separate surface report")
            if matching:
                final_artifacts.append(matching[-1]["artifact_ref"])
        if len(set(final_artifacts)) != len(SURFACES):
            issues.append(f"{criterion} needs distinct API, CLI, browser and provider artifacts")
        cases[criterion] = {"scenario_id": scenario, "test_ref": selector, "expected": expected,
                            "actual": actual, "report_ref": report_ref, "attempts": attempts}

    supplied_cleanup = inputs.get("cleanup") if isinstance(inputs.get("cleanup"), dict) else {}
    cleanup = {"run_id": run_id if supplied_cleanup.get("run_id") == run_id else None,
               "status": supplied_cleanup.get("status") if supplied_cleanup.get("status") == "verified" else None,
               "owned_absent": supplied_cleanup.get("owned_absent") is True,
               "reused_assets_preserved": supplied_cleanup.get("reused_assets_preserved") is True,
               "receipt_ref": reference(supplied_cleanup.get("receipt_ref"), "cleanup receipt"), "cases": {}}
    for criterion in cases_spec:
        entry = supplied_cleanup.get("cases", {}).get(criterion) if isinstance(supplied_cleanup.get("cases"), dict) else None
        entry = entry if isinstance(entry, dict) else {}
        cleanup["cases"][criterion] = {"receipt_ref": reference(entry.get("receipt_ref"), f"{criterion} cleanup receipt"),
                                       "owned_absent": entry.get("owned_absent") is True,
                                       "shared_unchanged": entry.get("shared_unchanged") is True}
        if not cleanup["cases"][criterion]["owned_absent"] or not cleanup["cases"][criterion]["shared_unchanged"]:
            issues.append(f"{criterion} cleanup is incomplete or affects shared providers")
    if (cleanup["run_id"] != run_id or cleanup["status"] != "verified" or not cleanup["owned_absent"]
            or not cleanup["reused_assets_preserved"]):
        issues.append("run-owned cleanup or shared provider preservation is incomplete")
    packet = {"evidence_schema": schema, "bolt_id": bolt_id, "run_id": run_id, **pinned,
              "artifacts": artifacts, "deployment": deployment, "surfaces": reports, "cases": cases, "cleanup": cleanup,
              "status": "review_ready" if not issues else "incomplete", "issues": issues}
    packet["packet_revision"] = "sha256:" + hashlib.sha256(
        json.dumps(packet, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(),
    ).hexdigest()
    packet["review"] = {
        "ac_id": "AC-04", "scenario_id": "SCN-04", "bolt_id": bolt_id,
        "procedure_ref": DECISION_PROCEDURE, "plan_revision": packet["plan_revision"],
        "packet_revision": packet["packet_revision"],
        "status": "pending" if packet["status"] == "review_ready" else "not_ready",
    }
    return packet


def review_binding(packet, *, bolt_id=BOLT_ID, packet_builder=build_packet):
    """Identify the exact pending review target without asserting human acceptance."""
    if not isinstance(packet, dict) or packet.get("status") != "review_ready":
        raise ValueError(f"Incomplete {bolt_id} packet cannot be submitted for human review")
    if packet_builder(packet) != packet:
        raise ValueError(f"{bolt_id} packet or review binding differs from its verified revision")
    return dict(packet["review"])


def decision_binding(packet, decisions, *, bolt_id=BOLT_ID, packet_builder=build_packet):
    """Validate an authoritative ADP decision readback; never create a decision."""
    pending = review_binding(packet, bolt_id=bolt_id, packet_builder=packet_builder)
    if not isinstance(decisions, list) or not decisions:
        raise ValueError(f"{bolt_id} needs a recorded decision for this packet revision")
    seen_revisions = set()
    seen_receipts = set()
    current = None
    for decision in decisions:
        if (not isinstance(decision, dict) or set(decision) != {
                "bolt_id", "plan_revision", "packet_revision", "decision", "decision_ref",
        } or decision.get("bolt_id") != bolt_id):
            raise ValueError(f"{bolt_id} decision readback has an unknown or malformed binding")
        revision = decision["packet_revision"]
        receipt = decision["decision_ref"]
        if (not isinstance(revision, str) or not revision.startswith("sha256:") or not DIGEST.fullmatch(revision)
                or revision in seen_revisions or not isinstance(decision["plan_revision"], str)
                or not REVISION.fullmatch(decision["plan_revision"])
                or decision["decision"] not in ("accept", "reject")
                or not isinstance(receipt, str) or not REFERENCE.fullmatch(receipt)
                or ".." in receipt or re.search(r"[0-9]{12}", receipt) or receipt in seen_receipts):
            raise ValueError(f"{bolt_id} decision readback is duplicated, stale or unsafe")
        seen_revisions.add(revision)
        seen_receipts.add(receipt)
        if revision == pending["packet_revision"]:
            if decision["plan_revision"] != pending["plan_revision"]:
                raise ValueError(f"{bolt_id} decision uses a different plan revision")
            current = decision
        elif decision["decision"] == "accept":
            raise ValueError(f"{bolt_id} already has an accepted packet revision")
    if current is None:
        raise ValueError(f"{bolt_id} needs a recorded decision for this packet revision")
    return dict(current)
