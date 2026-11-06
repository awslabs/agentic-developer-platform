#!/usr/bin/env python3
"""Roll DA19 private observations into native qualification and delivery evidence.

The native evaluator requires independently verified per-record observations.
Selector summaries retain the selected two-project scope and readback digests,
but cannot qualify any epic, story, scenario, portability or SSO check alone.
Both output files are private, mode-0600 records with target references hashed.

Success booleans alone never qualify. Every selector summary must carry a
run-bound private evidence digest, artifact digests equal to the published
release, independent readback references and a current observation time; a
missing, stale or self-referential summary becomes a blocker, and blockers fail
the job unless --report-only is requested.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from functools import lru_cache

ROOT = Path(__file__).resolve().parents[3]
PLAN = ROOT / "docs/l5-services/devopsagnent/delivery-plan.json"
SCHEMA = "delivery-evidence/v2"
EVIDENCE_NAME = "l5-devops-qualification"
REQUIRED_JOB = "L5 release and live qualification"
REVISION = "R3-secure-native-ui-ingress"
ARTIFACTS = ("api", "ui", "cli", "mcp", "worker")
SELECTORS = ("journeys", "recovery", "context", "portability", "acceptance")
LIVE_CHECKS = ("sso-gateway-only-ingress", "sso-denial-before-federation")
IDENTITY_FIELDS = ("product", "project", "repository", "team", "policy", "environment", "source_sha")
EXPECTED_PAIRS = 63
EXPECTED_EPIC = 10
PORTABILITY_ASSERTIONS = ("two-non-adp-products", "non-eks-workload",
                          "cross-account-role", "retained-assets-unchanged")


def shared_rollup():
    path = Path(__file__).with_name("qualification-rollup.py")
    specification = importlib.util.spec_from_file_location("l5_shared_rollup", path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    check_record = module.check_record

    def check_devops_record(record, identifier, section, context, now, max_age_seconds):
        if not isinstance(record, dict):
            return check_record(record, identifier, section, context, now, max_age_seconds)
        runtime_matches = (isinstance(context["runtime_revision"], str) and bool(context["runtime_revision"])
                           and record.get("runtime_revision") == context["runtime_revision"])
        selected = {**record, "runtime_revision": context["source_revision"]} if runtime_matches else record
        reasons = check_record(selected, identifier, section, context, now, max_age_seconds)
        if not runtime_matches and "wrong-runtime" not in reasons:
            reasons.append("wrong-runtime")
        return reasons

    module.check_record = check_devops_record
    return module


def digest(value, size=64):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % size, value) is not None


def reference(value):
    """Publish a re-derivable digest instead of a private identity or address."""
    if value in (None, ""):
        return None
    return "sha256:" + hashlib.sha256(str(value).encode()).hexdigest()


def moment(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def accepted_contract():
    """Read the accepted plan; a changed contract must be reviewed, not rolled up."""
    plan = json.loads(PLAN.read_text())
    pairs = sorted({(story["alias"], acceptance, scenario)
                    for story in plan["stories"]
                    for acceptance, scenario in zip(story["acceptance_ids"],
                                                    story["scenario_ids"], strict=True)})
    epic = sorted({criterion for story in plan["stories"] for criterion in story["epic_criteria"]})
    if (plan.get("requirements_revision") != REVISION or len(pairs) != EXPECTED_PAIRS
            or len(epic) != EXPECTED_EPIC):
        raise SystemExit("accepted R3 story or epic contract changed; review requirements first")
    return pairs, epic


def summary_path(directory, name):
    """Accept the selector's private file name or a prepared per-selector summary."""
    direct = Path(directory) / f"{name}.json"
    if direct.is_file():
        return direct
    candidates = sorted(Path(directory).glob(f"devops-*-{name}.json"))
    return candidates[-1] if candidates else None


def load(path):
    if path is None:
        return None
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


@lru_cache(maxsize=1)
def selector_evidence():
    path = Path(__file__).resolve().parents[1] / "tests/live/devops/selector_evidence.py"
    specification = importlib.util.spec_from_file_location("devops_selector_evidence", path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def selector_document(directory, name):
    summary = load(summary_path(directory, name)) or {}
    if not isinstance(summary, dict):
        return {}
    if "selector_evidence_version" in summary:
        try:
            return selector_evidence().verified_summary(directory, name, summary)
        except (OSError, TypeError, ValueError, KeyError, IndexError):
            return {}
    return summary


def release_record(path, blockers):
    release = load(path) or {}
    artifacts = release.get("artifacts")
    if (not digest(release.get("source_sha"), 40) or not release.get("deployment_id")
            or not isinstance(artifacts, dict) or set(artifacts) != set(ARTIFACTS)
            or not all(digest(value) for value in artifacts.values())
            or moment(release.get("deployed_at")) is None):
        blockers.append("release identity must pin the source SHA, deployment ID, deployed "
                        "time and all five artifact digests")
        return release, None
    return release, moment(release["deployed_at"])


def selector_summary(directory, name, release, deployed_at, blockers):
    """Keep one selector's attested references; never trust a bare status flag."""
    summary = selector_document(directory, name)
    if not summary:
        blockers.append(f"{name} selector produced no run-bound summary")
        return None
    observed_at = moment(summary.get("observed_at"))
    readbacks = sorted({reference(value) for value in (summary.get("readback_refs") or []) if value})
    record = {
        "selector": name,
        "status": summary.get("status"),
        "qualification_run_id": reference(summary.get("run_id")),
        "evidence_ref": reference(summary.get("evidence_ref")),
        "evidence_sha256": summary.get("evidence_sha256"),
        "observed_at": summary.get("observed_at"),
        "readback_count": len(readbacks),
        "readback_refs": readbacks,
        "artifacts_match_release": summary.get("artifacts") == release.get("artifacts"),
        "deployment_match": summary.get("deployment_id") == release.get("deployment_id"),
    }
    if summary.get("status") != "passed":
        blockers.append(f"{name} selector did not pass against the deployed target")
    if not summary.get("run_id"):
        blockers.append(f"{name} selector summary has no qualification run ID")
    if not digest(summary.get("evidence_sha256")) or not summary.get("evidence_ref"):
        blockers.append(f"{name} selector summary lacks a private evidence digest reference")
    if not readbacks:
        blockers.append(f"{name} selector cites no independent readback")
    if observed_at is None or (deployed_at is not None and not deployed_at <= observed_at):
        blockers.append(f"{name} selector observation is missing, naive or older than the release")
    if not record["artifacts_match_release"]:
        blockers.append(f"{name} selector observed different artifact digests than the release")
    if not record["deployment_match"]:
        blockers.append(f"{name} selector observed another deployment")
    return record


def native_results(directory, blockers):
    """Native provider results must come from real completed jobs, not claims."""
    observed = []
    for name in ("journeys", "context", "portability"):
        results = selector_document(directory, name).get("native_results")
        if not isinstance(results, list) or not results or any(
                not isinstance(item, dict) or not item.get("native_id")
                or not item.get("readback_ref") or item.get("status") != "completed"
                for item in results):
            blockers.append(f"{name} selector reported no completed native provider results")
            continue
        observed.extend({"selector": name, "capability": item.get("capability"),
                         "native_id": reference(item["native_id"]),
                         "readback_ref": reference(item["readback_ref"])}
                        for item in results)
    return observed


def live_checks(directory, release, blockers):
    checks = selector_document(directory, "acceptance").get("live_checks")
    if not isinstance(checks, dict) or set(checks) != set(LIVE_CHECKS):
        blockers.append("both R3 gateway-only SSO live checks are required")
        return {}
    kept = {}
    seen = set()
    for name, check in sorted(checks.items()):
        if (not isinstance(check, dict) or check.get("status") != "passed"
                or not check.get("readback_ref") or check["readback_ref"] in seen
                or check.get("source_sha") != release.get("source_sha")
                or check.get("deployment_id") != release.get("deployment_id")):
            blockers.append(f"live check {name} needs its own passing current readback")
            continue
        seen.add(check["readback_ref"])
        kept[name] = {"status": "passed", "readback_ref": reference(check["readback_ref"]),
                      "source_sha": check["source_sha"],
                      "deployment_id": reference(check["deployment_id"]),
                      "observed_at": check.get("observed_at")}
    return kept


def covered_cases(directory, pairs, epic, blockers):
    summary = selector_document(directory, "acceptance")
    cases = summary.get("cases")
    reported = {}
    if isinstance(cases, list):
        for case in cases:
            if (isinstance(case, dict) and case.get("status") == "passed"
                    and digest(case.get("readback_sha256")) and case.get("native_id")
                    and case.get("scope")):
                reported[(case.get("story"), case.get("acceptance"),
                          case.get("scenario"))] = case["readback_sha256"]
    missing = [list(pair) for pair in pairs if pair not in reported]
    if missing:
        blockers.append(f"{len(missing)} of {len(pairs)} AC/SCN pairs have no passing native evidence")
    if len(set(reported.values())) != len(reported):
        blockers.append("AC/SCN evidence reuses the same readback digest")
    if set(summary.get("epic_criteria") or []) != set(epic):
        blockers.append("the ten epic criteria need independent current observations")
    cleanup = summary.get("cleanup")
    if cleanup != {"status": "confirmed", "owned_absent": True, "retained_unchanged": True}:
        blockers.append("owned cleanup was not independently confirmed")
    return missing, reported, cleanup


def selected_scope(directory, blockers):
    """Two authorized non-ADP projects qualify DA19; the hosting namespace does not."""
    summary = selector_document(directory, "portability")
    projects = summary.get("projects")
    scope = {"project_count": 0, "projects": [], "distinct_fields": [],
             "non_eks_workloads": 0, "workloads": [],
             "cross_account_role": reference(summary.get("cross_account_role")),
             "retained_unchanged": summary.get("retained_unchanged") is True}
    if not isinstance(projects, list) or len(projects) != 2 or any(
            not isinstance(project, dict) for project in projects):
        blockers.append("exactly two selected non-ADP projects are required")
        return scope
    scope["project_count"] = len(projects)
    scope["projects"] = [{field: reference(project.get(field)) for field in IDENTITY_FIELDS}
                         for project in projects]
    scope["workloads"] = sorted({project.get("workload") for project in projects
                                 if project.get("workload")})
    scope["non_eks_workloads"] = len([workload for workload in scope["workloads"]
                                      if workload != "eks"])
    for field in IDENTITY_FIELDS:
        values = [project.get(field) for project in projects]
        if any(value in (None, "", "adp") for value in values) or values[0] == values[1]:
            blockers.append(f"the two selected projects need distinct non-ADP {field} values")
        else:
            scope["distinct_fields"].append(field)
    if not scope["non_eks_workloads"]:
        blockers.append("at least one selected workload must be non-EKS")
    if not scope["cross_account_role"]:
        blockers.append("the selected cross-account resource role is missing")
    if not scope["retained_unchanged"]:
        blockers.append("retained assets were not confirmed unchanged")
    return scope


def context_reuse(directory, blockers):
    reuse = selector_document(directory, "context").get("reuse")
    required = ("result_ref", "publication_ref", "source_ref", "later_decision_ref", "denial_ref")
    if not isinstance(reuse, dict) or any(not reuse.get(field) for field in required):
        blockers.append("later Context reuse needs independent result, publication, source, "
                        "later-decision and denial readbacks")
        return {}
    if len({reuse[field] for field in required}) != len(required):
        blockers.append("later Context reuse cites the same readback more than once")
    return {field: reference(reuse[field]) for field in required}


def two_real_users(directory, blockers):
    summary = selector_document(directory, "recovery")
    users = [user for user in (summary.get("users") or []) if user]
    denied = summary.get("denied_provider_calls")
    if len(set(users)) != 2:
        blockers.append("two real authorized users are required for recovery qualification")
    if not isinstance(denied, int) or denied <= 0:
        blockers.append("denied launches before provider federation were not observed")
    cleanup = summary.get("cleanup")
    if cleanup != {"status": "confirmed", "owned_absent": True, "retained_unchanged": True}:
        blockers.append("recovery cleanup of owned POC routes was not independently confirmed")
    return {"users": len(set(users)), "user_refs": sorted({reference(user) for user in set(users)}),
            "denied_provider_calls": denied, "cleanup": cleanup}


def build(arguments):
    pairs, epic = accepted_contract()
    blockers = []
    release, deployed_at = release_record(arguments.release, blockers)
    summaries = {}
    for name in SELECTORS:
        record = selector_summary(arguments.evidence, name, release, deployed_at, blockers)
        if record is not None:
            summaries[name] = record
    run_ids = {record["qualification_run_id"] for record in summaries.values()
               if record.get("qualification_run_id")}
    if len(run_ids) > 1:
        blockers.append("selectors used different qualification run IDs")
    missing, reported, cleanup = covered_cases(arguments.evidence, pairs, epic, blockers)
    document = {
        "schema": SCHEMA,
        "evidence": EVIDENCE_NAME,
        "story": "DA19",
        "epic": "8865",
        "requirements_revision": REVISION,
        "architecture_revision": "R2",
        "revisions": {
            "source": release.get("source_sha") or os.environ.get("SOURCE_SHA"),
            "definition": os.environ.get("DEFINITION_SHA"),
            "runtime": {"deployment_id": release.get("deployment_id"),
                        "deployed_at": release.get("deployed_at"),
                        "artifacts": release.get("artifacts")},
        },
        "run": {
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "workflow": os.environ.get("GITHUB_WORKFLOW"),
            "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF"),
            "repository": os.environ.get("GITHUB_REPOSITORY"),
            "job": REQUIRED_JOB,
            "environment": os.environ.get("TARGET_ENVIRONMENT"),
            "qualification_run_id": sorted(run_ids)[0] if len(run_ids) == 1 else None,
        },
        "release_owner": os.environ.get("RELEASE_OWNER") or "DA19",
        "grouped_releases": 1,
        "criteria": {
            "epic": epic,
            "cases": [list(pair) for pair in pairs],
            "case_count": len(pairs),
            "case_evidence": {"/".join(pair): reported[pair] for pair in pairs if pair in reported},
            "missing_cases": missing,
        },
        "live_checks": live_checks(arguments.evidence, release, blockers),
        "scope": selected_scope(arguments.evidence, blockers),
        "context_reuse": context_reuse(arguments.evidence, blockers),
        "recovery": two_real_users(arguments.evidence, blockers),
        "native_results": native_results(arguments.evidence, blockers),
        "cleanup": cleanup,
        "selectors": summaries,
    }
    document["blockers"] = blockers
    document["qualified"] = not blockers
    return document


def independent_reasons(record, selector_refs):
    if not isinstance(record, dict):
        return []
    reasons = []
    if record.get("verification_layer") != "independent-live":
        reasons.append("not-independent-live")
    if record.get("evidence_ref") and reference(record["evidence_ref"]) in selector_refs:
        reasons.append("selector-summary-is-not-readback")
    return reasons


def native_report(arguments, delivery):
    """Require independent, per-record observations; selector counts are not proof."""
    shared = shared_rollup()
    pairs, epic = accepted_contract()
    expected = {
        "epic_criteria": epic,
        "story_criteria": sorted({f"{story}/{acceptance}" for story, acceptance, _ in pairs}),
        "scenarios": sorted({f"{story}/{scenario}" for story, _, scenario in pairs}),
        "live_checks": list(LIVE_CHECKS),
    }
    observation_path = arguments.observations or Path(arguments.evidence) / "independent-observations.json"
    bundle = load(observation_path)
    context = {
        "requirement_revision": REVISION,
        "runtime_revision": delivery["revisions"]["runtime"]["deployment_id"],
        "source_revision": delivery["revisions"]["source"],
        "definition_revision": delivery["revisions"]["definition"],
        "run_id": delivery["run"]["run_id"],
        "run_attempt": delivery["run"]["run_attempt"],
        "correlation_id": os.environ.get("CORRELATION_ID") or None,
        "contract_digest": os.environ.get("CONTRACT_DIGEST") or None,
    }
    report = shared.roll_up(bundle, context, expected, datetime.now(UTC),
                            arguments.max_age_seconds, component=EVIDENCE_NAME)
    selector_refs = {item["evidence_ref"] for item in delivery["selectors"].values()
                     if item.get("evidence_ref")}
    if not context["run_id"] or not context["run_attempt"]:
        report["unresolved"].append("workflow-run:missing")
    if not digest(context["source_revision"], 40) or not digest(context["definition_revision"], 40):
        report["unresolved"].append("workflow-revisions:missing")
    if isinstance(bundle, dict):
        if reference(bundle.get("qualification_run_id")) != delivery["run"]["qualification_run_id"]:
            report["unresolved"].append("observations:wrong-qualification-run")
        for section in expected:
            records = bundle.get(section)
            if isinstance(records, list) and len([row.get("id") for row in records
                                                   if isinstance(row, dict)]) != len({row.get("id") for row in records
                                                                                       if isinstance(row, dict)}):
                report["unresolved"].append(f"{section}:duplicate-record")
            for record in report[section]:
                if record.get("evidence_verified"):
                    reasons = []
                    if (record.get("requirement_revision") != REVISION
                            or record.get("runtime_revision") != delivery["revisions"]["runtime"]["deployment_id"]):
                        reasons.append("wrong-requirement-or-runtime")
                    reasons.extend(independent_reasons(record, selector_refs))
                    if reasons:
                        record["evidence_verified"] = False
                        record["unresolved_reasons"] = reasons
                        report["unresolved"].extend(f"{section}/{record['id']}:{reason}" for reason in reasons)
        observed = {item.get("id"): item for item in bundle.get("portability", {}).get("assertions", [])
                    if isinstance(item, dict) and isinstance(item.get("id"), str)} if isinstance(
                        bundle.get("portability"), dict) and isinstance(
                            bundle["portability"].get("assertions"), list) else {}
    else:
        observed = {}
    assertions = []
    for identifier in PORTABILITY_ASSERTIONS:
        record = observed.get(identifier)
        reasons = shared.check_record(record, identifier, "portability", context,
                                      datetime.now(UTC), arguments.max_age_seconds)
        if isinstance(record, dict) and record.get("requirement_revision") != REVISION:
            reasons.append("wrong-requirement")
        if isinstance(record, dict) and record.get("runtime_revision") != delivery["revisions"]["runtime"]["deployment_id"]:
            reasons.append("wrong-runtime")
        reasons.extend(independent_reasons(record, selector_refs))
        assertions.append(shared.emit_record(record, identifier, "portability", reasons))
        report["unresolved"].extend(f"portability/{identifier}:{reason}" for reason in reasons)
    report["portability"] = {"assertions": assertions}
    report["requirements_revision"] = REVISION
    report["release"] = delivery["revisions"]["runtime"]
    report["unresolved"].extend(f"selector:{blocker}" for blocker in delivery["blockers"])
    report["unresolved"] = sorted(set(report["unresolved"]))
    for section in expected:
        for record in report[section]:
            if record.get("evidence_ref"):
                record["evidence_ref"] = reference(record["evidence_ref"])
    for record in assertions:
        if record.get("evidence_ref"):
            record["evidence_ref"] = reference(record["evidence_ref"])
    if "independent_reader" in report:
        report["independent_reader"]["identity"] = reference(report["independent_reader"]["identity"])
    if "target" in report:
        for field in ("connection_id", "resource_id"):
            report["target"][field] = reference(report["target"][field])
    if isinstance(report.get("cleanup", {}).get("readback"), list):
        report["cleanup"]["readback"] = [reference(item) for item in report["cleanup"]["readback"]]
    report["release"]["deployment_id"] = reference(report["release"]["deployment_id"])
    evidence = shared.delivery_evidence(
        report, qualification_artifact="l5-devops-qualification/qualification.json")
    return report, evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description="Emit native DA19 qualification and delivery evidence")
    parser.add_argument("--release", required=True, help="published release identity JSON")
    parser.add_argument("--evidence", required=True,
                        help="directory of run-bound private selector summaries")
    parser.add_argument("--output", required=True, help="qualification.json to write")
    parser.add_argument("--delivery-evidence", help="separate delivery-evidence/v2 JSON output")
    parser.add_argument("--observations", help="private independent per-record observation bundle")
    parser.add_argument("--max-age-seconds", type=int, default=3600)
    parser.add_argument("--report-only", action="store_true",
                        help="write the record and keep blockers open without failing")
    arguments = parser.parse_args(argv)
    delivery = build(arguments)
    document, evidence = native_report(arguments, delivery)
    output = Path(arguments.output)
    delivery_path = Path(arguments.delivery_evidence or output.with_name("delivery-evidence.json"))
    if output.resolve() == delivery_path.resolve():
        parser.error("qualification and delivery evidence must have distinct paths")
    shared_rollup().write_private(output, document)
    shared_rollup().write_private(delivery_path, evidence)
    for blocker in document["unresolved"]:
        print(f"blocker: {blocker}", file=sys.stderr)
    print(f"{EVIDENCE_NAME} schema_version=1 unresolved={len(document['unresolved'])}")
    return 0 if not document["unresolved"] or arguments.report_only else 1


if __name__ == "__main__":
    raise SystemExit(main())
