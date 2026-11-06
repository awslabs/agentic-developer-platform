#!/usr/bin/env python3
"""Assemble the Security qualification report from independent live observations.

Use --service devops for the DevOps report. Existing DevOps --release invocations
and Security invocations retain their original evidence contracts.

The protected release workflow publishes one pinned release, runs the real SA16
journeys and then calls this rollup. Nothing here produces a result on its own:
every required epic criterion, story criterion, scenario and live check must
appear in the independent reader's private observation bundle, bound to this
workflow run, the qualified revisions and a private evidence reference. The
required identifier sets are read from the registered delivery proposal, so a
fabricated, stale, wrong-source, incomplete or cleanup-failed bundle stays in
``unresolved`` and fails the run instead of claiming acceptance.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROPOSAL = ROOT / "docs/l5-services/flow/proposal.authored.json"
WORKFLOW_PATH = ".github/workflows/l5-services-release.yml"
REQUIRED_JOB = "L5 release and live qualification"
COMPONENT = "l5-security"
SHA = re.compile(r"^[0-9a-f]{40}$")
SECTIONS = {
    "epic-coverage": "epic_criteria",
    "story-coverage": "story_criteria",
    "scenario-coverage": "scenarios",
    "live-observations": "live_checks",
}
RECORD_FIELDS = (
    "requirement_revision",
    "source_revision",
    "definition_revision",
    "workflow_path",
    "runtime_revision",
    "run_id",
    "run_attempt",
    "verification_layer",
    "evidence_ref",
    "observed_at",
)
TARGET_FIELDS = ("connection_id", "resource_kind", "resource_id")


def proposal_revision(proposal_path=PROPOSAL):
    revision = json.loads(Path(proposal_path).read_text()).get("spec_revision")
    if not isinstance(revision, str) or not SHA.fullmatch(revision):
        raise ValueError("the accepted Security proposal has no pinned revision")
    return revision


def expected_records(proposal_path=PROPOSAL):
    """Return the required identifier list for each report section."""
    proposal = json.loads(Path(proposal_path).read_text())
    for node in proposal.get("nodes", []):
        evaluation = node.get("evaluation") or {}
        for workflow in evaluation.get("workflows", []):
            if workflow.get("path") != WORKFLOW_PATH:
                continue
            if REQUIRED_JOB not in workflow.get("required_jobs", []):
                continue
            for artifact in workflow.get("artifacts", []):
                sections = {}
                for predicate in artifact.get("predicates", []):
                    section = SECTIONS.get(predicate.get("criterion_id"))
                    if section and predicate.get("operation") == "records":
                        sections[section] = list(predicate["expected"])
                if len(sections) == len(SECTIONS):
                    return sections
    raise SystemExit("the proposal does not describe this workflow's qualification records")


def fresh(value, now, max_age_seconds):
    try:
        observed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if observed.tzinfo is None:
        return False
    return now - timedelta(seconds=max_age_seconds) <= observed <= now + timedelta(seconds=60)


def check_record(record, identifier, section, context, now, max_age_seconds):
    """Return the reasons this observation is not independently verified."""
    reasons = []
    if not isinstance(record, dict):
        return ["malformed"]
    if record.get("outcome") != "passed":
        reasons.append("outcome:%s" % record.get("outcome"))
    if record.get("evidence_verified") is not True:
        reasons.append("reader-unverified")
    for field in RECORD_FIELDS:
        value = record.get(field)
        if not isinstance(value, str) or not value:
            reasons.append("missing:%s" % field)
    if record.get("requirement_revision") != context["requirement_revision"]:
        reasons.append("wrong-requirement")
    if record.get("source_revision") != context["source_revision"]:
        reasons.append("wrong-source")
    if record.get("definition_revision") != context["definition_revision"]:
        reasons.append("wrong-definition")
    if record.get("workflow_path") != WORKFLOW_PATH:
        reasons.append("wrong-workflow")
    if record.get("runtime_revision") != context["source_revision"]:
        reasons.append("wrong-runtime")
    live_required = section == "live_checks" or identifier.startswith(("SA15/", "SA16/"))
    allowed_layers = {"independent-live"} if live_required else {"independent-live", "independent-ci"}
    if record.get("verification_layer") not in allowed_layers:
        reasons.append("unverified-layer")
    if record.get("run_id") != context["run_id"] or record.get("run_attempt") != context["run_attempt"]:
        reasons.append("wrong-run")
    if not fresh(record.get("observed_at"), now, max_age_seconds):
        reasons.append("stale")
    if section == "live_checks" and record.get("observation") != "independent-live":
        reasons.append("observation:%s" % record.get("observation"))
    reference = record.get("evidence_ref")
    if (live_required and (not isinstance(reference, str)
            or not re.fullmatch(r"private://[^/#\s]+/[^#\s]+#sha256:[0-9a-f]{64}", reference))):
        reasons.append("unbound-evidence")
    if record.get("id") != identifier:
        reasons.append("identity")
    return reasons


def emit_record(record, identifier, section, reasons):
    published = {
        "id": identifier,
        "outcome": record.get("outcome") if isinstance(record, dict) else None,
        "evidence_verified": not reasons,
    }
    if isinstance(record, dict):
        for field in RECORD_FIELDS:
            published[field] = record.get(field)
        if section == "live_checks":
            published["observation"] = record.get("observation")
    if reasons:
        if published.get("outcome") == "passed" or not published.get("outcome"):
            published["outcome"] = "incomplete"
        published["unresolved_reasons"] = reasons
    return published


def roll_up(bundle, context, expected, now, max_age_seconds, *, component=COMPONENT):
    unresolved = []
    report = {
        "schema_version": 1,
        "source_revision": context["source_revision"],
        "definition_revision": context["definition_revision"],
        "gateway_revision": bundle.get("gateway_revision") if isinstance(bundle, dict) else None,
        "run_id": context["run_id"],
        "run_attempt": context["run_attempt"],
        "correlation_id": context["correlation_id"],
        "contract_digest": context["contract_digest"],
        "workflow_path": WORKFLOW_PATH,
        "job": REQUIRED_JOB,
        "component": component,
    }
    if not isinstance(bundle, dict):
        unresolved.append("observations:unreadable")
        bundle = {}
    if bundle.get("schema_version") != 1:
        unresolved.append("observations:schema_version")
    if bundle.get("run_id") != context["run_id"] or bundle.get("run_attempt") != context["run_attempt"]:
        unresolved.append("observations:wrong-run")
    if bundle.get("source_revision") != context["source_revision"]:
        unresolved.append("observations:wrong-source")
    if bundle.get("definition_revision") != context["definition_revision"]:
        unresolved.append("observations:wrong-definition")
    if bundle.get("workflow_path") != WORKFLOW_PATH:
        unresolved.append("observations:wrong-workflow")
    gateway = bundle.get("gateway_revision")
    if not isinstance(gateway, str) or not SHA.match(gateway or ""):
        unresolved.append("gateway_revision:missing")
    elif gateway != context["source_revision"]:
        unresolved.append("gateway_revision:not-the-qualified-candidate")
    reader = bundle.get("reader")
    if not isinstance(reader, dict) or not reader.get("identity") or not reader.get("revision"):
        unresolved.append("independent-reader:missing")
    else:
        report["independent_reader"] = {"identity": reader["identity"], "revision": reader["revision"]}
    target = bundle.get("target")
    if not isinstance(target, dict) or any(not target.get(field) for field in TARGET_FIELDS):
        unresolved.append("target-identity:missing")
    else:
        report["target"] = {field: target[field] for field in TARGET_FIELDS}

    for section, identifiers in expected.items():
        observed = {}
        duplicates = set()
        records = bundle.get(section)
        if records is not None and not isinstance(records, list):
            unresolved.append(f"{section}:malformed-observations")
            records = []
        for record in records or []:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                unresolved.append(f"{section}:malformed-observations")
            elif record["id"] in observed:
                duplicates.add(record["id"])
            else:
                observed[record["id"]] = record
        published = []
        for identifier in identifiers:
            record = observed.get(identifier)
            if record is None:
                published.append({"id": identifier, "outcome": "incomplete", "evidence_verified": False,
                                  "unresolved_reasons": ["not-observed"]})
                unresolved.append("%s/%s:not-observed" % (section, identifier))
                continue
            reasons = check_record(record, identifier, section, context, now, max_age_seconds)
            if identifier in duplicates:
                reasons.append("duplicate")
            published.append(emit_record(record, identifier, section, reasons))
            for reason in reasons:
                unresolved.append("%s/%s:%s" % (section, identifier, reason))
        extra = sorted(set(observed) - set(identifiers))
        for identifier in extra:
            unresolved.append("%s/%s:not-required" % (section, identifier))
        for identifier in duplicates - set(identifiers):
            unresolved.append(f"{section}/{identifier}:duplicate")
        report[section] = published

    cleanup = bundle.get("cleanup")
    if not isinstance(cleanup, dict):
        report["cleanup"] = {"outcome": "incomplete"}
        unresolved.append("cleanup:not-observed")
    else:
        readback = cleanup.get("readback")
        published = {"outcome": cleanup.get("outcome"), "readback": readback,
                     "observed_at": cleanup.get("observed_at")}
        if cleanup.get("outcome") != "passed":
            unresolved.append("cleanup:outcome:%s" % cleanup.get("outcome"))
        if not isinstance(readback, list) or not readback or any(not item for item in readback):
            unresolved.append("cleanup:readback:missing")
            if published["outcome"] == "passed":
                published["outcome"] = "incomplete"
        if not fresh(cleanup.get("observed_at"), now, max_age_seconds):
            unresolved.append("cleanup:stale")
            if published["outcome"] == "passed":
                published["outcome"] = "incomplete"
        report["cleanup"] = published

    report["unresolved"] = sorted(set(unresolved))
    return report


def delivery_evidence(report, *, qualification_artifact="l5-security-qualification/qualification.json"):
    evidence = {
        "schema_version": 2,
        "component": report["component"],
        "workflow_path": WORKFLOW_PATH,
        "job": REQUIRED_JOB,
        "run_id": report["run_id"],
        "run_attempt": report["run_attempt"],
        "correlation_id": report["correlation_id"],
        "contract_digest": report["contract_digest"],
        "source_revision": report["source_revision"],
        "definition_revision": report["definition_revision"],
        "gateway_revision": report["gateway_revision"],
        "qualification_artifact": qualification_artifact,
        "required_checks": [
            {"name": record["id"], "outcome": record["outcome"],
             "observation": record.get("observation"),
             "evidence_verified": record["evidence_verified"]}
            for record in report.get("live_checks", [])
        ],
        "cleanup": report.get("cleanup"),
        "unresolved": report["unresolved"],
    }
    for field in ("target", "independent_reader"):
        if field in report:
            evidence[field] = report[field]
    return evidence


def write_private(path, document):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(document, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def default_path(name, fallback):
    """Resolve an output location from the protected workflow's environment."""
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp:
        return str(Path(runner_temp) / fallback)
    return str(Path.cwd() / name)


def default_observations():
    explicit = os.environ.get("ADP_L5_SECURITY_OBSERVATIONS")
    if explicit:
        return explicit
    private = os.environ.get("PRIVATE_EVIDENCE_DIR") or os.environ.get(
        "ADP_L5_SECURITY_PRIVATE_EVIDENCE_DIR")
    if private:
        return str(Path(private) / "independent-observations.json")
    return ""


def security_main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", "--observation-bundle", default=default_observations())
    parser.add_argument("--source-revision", "--source-sha",
                        default=os.environ.get("SOURCE_SHA") or os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--definition-revision", default=os.environ.get("DEFINITION_REVISION", ""))
    parser.add_argument("--qualification", "--output",
                        default=default_path("qualification.json",
                                             "l5-security-qualification/qualification.json"))
    parser.add_argument("--delivery-evidence", "--evidence",
                        default=default_path("delivery-evidence.json",
                                             "delivery-evidence/delivery-evidence.json"))
    parser.add_argument("--contract-digest", default=os.environ.get("CONTRACT_DIGEST", ""))
    parser.add_argument("--correlation-id", default=os.environ.get("CORRELATION_ID", ""))
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    parser.add_argument("--run-attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT", ""))
    parser.add_argument("--max-age-seconds", type=int, default=3600)
    parser.add_argument("--proposal", default=str(PROPOSAL))
    arguments, unknown = parser.parse_known_args(argv)
    if unknown:
        sys.stderr.write("ignoring unrecognised rollup arguments: %s\n" % " ".join(unknown))

    if not SHA.match(arguments.source_revision or ""):
        raise SystemExit("the qualified source revision must be a full commit hash")
    if not arguments.observations:
        raise SystemExit("the independent observation bundle must be supplied from private storage")
    context = {
        "requirement_revision": proposal_revision(arguments.proposal),
        "source_revision": arguments.source_revision,
        "definition_revision": arguments.definition_revision or arguments.source_revision,
        "contract_digest": arguments.contract_digest,
        "correlation_id": arguments.correlation_id,
        "run_id": arguments.run_id,
        "run_attempt": arguments.run_attempt,
    }
    if not context["run_id"] or not context["run_attempt"]:
        raise SystemExit("the rollup must run inside the protected workflow run")

    expected = expected_records(arguments.proposal)
    try:
        bundle = json.loads(Path(arguments.observations).read_text())
    except (OSError, ValueError):
        bundle = None

    report = roll_up(bundle, context, expected, datetime.now(UTC), arguments.max_age_seconds)
    write_private(arguments.qualification, report)
    write_private(arguments.delivery_evidence, delivery_evidence(report))

    if report["unresolved"]:
        sys.stderr.write(
            "qualification incomplete: %d unresolved entries; the private report retains the detail\n"
            % len(report["unresolved"])
        )
        return 1
    print("qualification complete: every required record is independently verified")
    return 0


def main(argv=None):
    """Select a service without changing either service's evidence contract."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    selector = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    selector.add_argument("--service", choices=("security", "devops", "well_architected"))
    selection, remaining = selector.parse_known_args(arguments)
    service = selection.service or os.environ.get("L5_SERVICE", "").replace("-", "_")
    if not service:
        # DevOps callers already require --release; Security has no such flag.
        service = "devops" if any(arg == "--release" or arg.startswith("--release=")
                                  for arg in remaining) else "security"
    if service == "security":
        return security_main(remaining)
    if service == "devops":
        specification = importlib.util.spec_from_file_location(
            "l5_devops_qualification", Path(__file__).with_name("qualification_devops.py"))
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module.main(remaining)
    if service == "well_architected":
        specification = importlib.util.spec_from_file_location(
            "l5_wa_qualification", Path(__file__).with_name("qualification_well_architected.py"))
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module.main(remaining)
    selector.error("L5_SERVICE must be security, devops or well_architected")


if __name__ == "__main__":
    raise SystemExit(main())
