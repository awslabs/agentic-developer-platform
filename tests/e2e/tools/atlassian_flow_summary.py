"""Assemble the exact flow-27 contract from independently qualified reports.

This module checks inventory, predicates and common release/run bindings. It
does not qualify raw provider observations: the existing scenario readers must
do that before supplying these reports. In particular, it never creates a
provider attestation or promotes a controlled stage result into a live result.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path


PLAN = (
    Path(__file__).resolve().parents[3]
    / "docs/delivery-flow/plans/atlassian-adapters-8445/proposal.authored.json"
)
STAGES = (
    "identity",
    "reads-parity",
    "connections",
    "write-recovery",
    "clients",
    "release-automation",
    "harness",
)
MAX_BYTES = 8 * 1024 * 1024
BINDING_FIELDS = {
    "source_revision",
    "definition_revision",
    "run_id",
    "run_attempt",
    "release_sha256",
}
FLOW_SCENARIOS = tuple(f"SCN-8454-{number:02}" for number in range(1, 7))
MISSING = object()


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON field in acceptance evidence")
        result[key] = value
    return result


def _decode(data):
    def invalid_number(_):
        raise ValueError("Non-finite number in acceptance evidence")

    return json.loads(
        data, object_pairs_hook=_unique_object, parse_constant=invalid_number
    )


def completion_contract():
    """Move the original seven stage predicates exactly as batch-completion specifies."""
    plan = _decode(PLAN.read_bytes())
    final, stages = None, {}
    for node in plan["nodes"]:
        for workflow in node.get("evaluation", {}).get("workflows", []):
            for artifact in workflow.get("artifacts", []):
                predicates = artifact.get("predicates", [])
                if (
                    node["address"].endswith("/w11-acceptance/code-evidence")
                    and artifact["path"] == "atlassian-flow-summary.json"
                ):
                    if final is not None:
                        raise ValueError("Ambiguous final acceptance contract")
                    final = copy.deepcopy(predicates)
                if artifact["path"] == "atlassian-stage-summary.json":
                    identity = [
                        p["expected"] for p in predicates if p["pointer"] == "/stage_id"
                    ]
                    if len(identity) == 1 and identity[0] in STAGES:
                        if identity[0] in stages:
                            raise ValueError("Ambiguous stage acceptance contract")
                        stages[identity[0]] = copy.deepcopy(predicates)
    if final is None or set(stages) != set(STAGES):
        raise ValueError("The complete original acceptance contract is required")
    # The authored file retains the original separate gates. Never change their
    # expected values while relocating execution into the combined campaign.
    if any(p["pointer"].startswith("/stage_reports/") for p in final):
        raise ValueError("Review the relocated contract before updating the reader")
    for stage in STAGES:
        for predicate in stages[stage]:
            predicate["pointer"] = f"/stage_reports/{stage}" + predicate["pointer"]
            final.append(predicate)
    if len({p["criterion_id"] for p in final}) != len(final) or any(
        p["operation"] not in {"equals", "records"} for p in final
    ):
        raise ValueError("Unsupported or duplicate acceptance predicate")
    return final


def _same(actual, expected):
    """JSON equality must not accept integer 1 as the boolean true."""
    return type(actual) is type(expected) and canonical(actual) == canonical(expected)


def _at(document, pointer):
    current = document
    for part in pointer.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        if type(current) is not dict or key not in current:
            return MISSING
        current = current[key]
    return current


def _matches(document, predicate):
    value = _at(document, predicate["pointer"])
    if value is MISSING:
        return False
    if predicate["operation"] == "equals":
        return _same(value, predicate["expected"])
    if type(value) is not list or any(type(row) is not dict for row in value):
        return False
    field = predicate["id_field"]
    ids = [row.get(field) for row in value]
    if (
        any(type(identifier) is not str for identifier in ids)
        or len(set(ids)) != len(ids)
        or set(ids) != set(predicate["expected"])
    ):
        return False
    return all(
        any(_same(row.get(key, MISSING), choice) for choice in allowed)
        for row in value
        for key, allowed in predicate["required_fields"].items()
    )


def validate_binding(binding):
    if type(binding) is not dict or set(binding) != BINDING_FIELDS:
        raise ValueError(
            "A workflow-selected source, definition, run and release binding is required"
        )
    for key, value in binding.items():
        pattern = (
            r"[1-9][0-9]*"
            if key in {"run_id", "run_attempt"}
            else r"[0-9a-f]{64}"
            if key == "release_sha256"
            else r"[0-9a-f]{40}"
        )
        if type(value) is not str or re.fullmatch(pattern, value) is None:
            raise ValueError("Invalid workflow-selected acceptance binding")


def assemble_summary(live_report, stage_reports, *, binding):
    """Preserve supplied observations; readiness is the conjunction of all predicates."""
    validate_binding(binding)
    if (
        type(live_report) is not dict
        or type(stage_reports) is not dict
        or any(stage not in STAGES for stage in stage_reports)
        or any(type(report) is not dict for report in stage_reports.values())
        or {"stage_reports", "aggregation"} & live_report.keys()
    ):
        raise ValueError("Separate live and stage reports are required")
    # Also validates JSON types and bounds before copying any protected payload.
    if len(canonical([live_report, stage_reports])) > MAX_BYTES:
        raise ValueError("Acceptance reports exceed the bound")
    report = copy.deepcopy(live_report)
    report["stage_reports"] = copy.deepcopy(stage_reports)
    contract = completion_contract()
    failures = [
        p["criterion_id"]
        for p in contract
        if p["pointer"] != "/completion_ready" and not _matches(report, p)
    ]
    if not _same(live_report.get("binding", MISSING), binding):
        failures.append("binding.live")
    for stage in STAGES:
        if not _same(stage_reports.get(stage, {}).get("binding", MISSING), binding):
            failures.append("binding." + stage)
    # A producer's explicit hold is retained even when its other rows pass.
    if (
        "completion_ready" in live_report
        and live_report["completion_ready"] is not True
    ):
        failures.append("completion.producer-hold")
    report["completion_ready"] = not failures
    report["aggregation"] = {
        "schema": "atlassian-flow-aggregation/v1",
        "contract_sha256": hashlib.sha256(canonical(contract)).hexdigest(),
        "live_report_sha256": hashlib.sha256(canonical(live_report)).hexdigest(),
        "stage_report_sha256": {
            stage: hashlib.sha256(canonical(value)).hexdigest()
            for stage, value in sorted(stage_reports.items())
        },
        "failed_predicates": failures,
    }
    return report


def verify_summary(summary, live_report, stage_reports, *, binding):
    """Rebuild from reread source reports; this is not a raw provider verifier."""
    expected = assemble_summary(live_report, stage_reports, binding=binding)
    if not _same(summary, expected):
        raise ValueError("Retained summary differs from the source reports")
    if expected["completion_ready"] is not True:
        raise ValueError(
            "Acceptance is incomplete: "
            + ", ".join(expected["aggregation"]["failed_predicates"])
        )
    return expected


def assemble_evaluation_summary(
    live_report, stage_reports, evaluation, *, binding, qualified_scenarios
):
    """Require reader-qualified scenario coverage in addition to all 81 predicates.

    The retained-evidence reader supplies qualified_scenarios after checking the
    actual scenario records. A producer's live_attestation flag alone cannot
    supply this set. Unknown and incomplete rows are preserved for diagnosis.
    """
    if (
        type(evaluation) is not dict
        or type(qualified_scenarios) is not set
        or not qualified_scenarios <= set(FLOW_SCENARIOS)
    ):
        raise ValueError("Reader-qualified flow scenarios are required")
    result = assemble_summary(live_report, stage_reports, binding=binding)
    selected = evaluation.get("selected_scenario_ids")
    coverage = (
        type(selected) is list
        and all(type(value) is str for value in selected)
        and len(selected) == len(FLOW_SCENARIOS)
        and set(selected) == set(FLOW_SCENARIOS)
    )
    bound = (
        evaluation.get("evidence_schema") == "workflow-evaluation/v1"
        and type(evaluation.get("schema_version")) is int
        and evaluation["schema_version"] == 1
        and evaluation.get("harness_revision") == binding["definition_revision"]
        and evaluation.get("github_run_id") == binding["run_id"]
        and evaluation.get("github_run_attempt") == binding["run_attempt"]
    )
    if not bound or not coverage or evaluation.get("matrix_complete") is not True:
        result["aggregation"]["failed_predicates"].append("readback.scenario-inventory")
    if evaluation.get("live_attestation") is not True:
        result["aggregation"]["failed_predicates"].append("readback.live-attestation")
    unverified = sorted(set(FLOW_SCENARIOS) - qualified_scenarios)
    result["aggregation"]["failed_predicates"].extend(
        "readback." + scenario for scenario in unverified
    )
    result["completion_ready"] = not result["aggregation"]["failed_predicates"]
    result["scenario_readback"] = {
        "evaluation_sha256": hashlib.sha256(canonical(evaluation)).hexdigest(),
        "qualified_scenarios": sorted(qualified_scenarios),
        "unverified_scenarios": unverified,
        "rows": copy.deepcopy(evaluation.get("scenarios", [])),
    }
    if len(canonical(result)) > MAX_BYTES:
        raise ValueError("Acceptance reports exceed the bound")
    return result


def _read_private(path):
    path = Path(path)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError("Private evidence directory required")
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise ValueError("Private evidence directory required")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or not 0 < metadata.st_size <= MAX_BYTES
        ):
            raise ValueError("Bounded private evidence file required")
        data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("Acceptance evidence exceeds the bound")
        return _decode(data)


def _write_private(path, report):
    path = Path(path)
    if (
        path.is_symlink()
        or path.exists()
        or path.parent.is_symlink()
        or not path.parent.is_dir()
        or path.parent.stat().st_mode & 0o077
        or path.parent.stat().st_uid != os.getuid()
    ):
        raise ValueError("A new output in a private evidence directory is required")
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".flow-summary-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(report) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Linking a new name refuses an output created concurrently.
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("assemble", "check"))
    parser.add_argument(
        "--inputs",
        type=Path,
        required=True,
        help="Private directory containing live.json and stage-ID.json reports",
    )
    parser.add_argument(
        "--binding",
        type=Path,
        required=True,
        help="Workflow-selected binding, independent of report contents",
    )
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        binding = _read_private(args.binding)
        live = _read_private(args.inputs / "live.json")
        stages = {}
        for stage in STAGES:
            try:
                stages[stage] = _read_private(args.inputs / f"stage-{stage}.json")
            except FileNotFoundError:
                continue
        if args.action == "assemble":
            result = assemble_summary(live, stages, binding=binding)
            _write_private(args.summary, result)
        else:
            result = verify_summary(
                _read_private(args.summary), live, stages, binding=binding
            )
    except (OSError, ValueError, TypeError, KeyError):
        print(
            json.dumps(
                {"status": "blocked", "reason": "acceptance report validation failed"}
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": "report_contract_satisfied"
                if result["completion_ready"]
                else "blocked",
                "failed_predicates": result["aggregation"]["failed_predicates"],
            }
        )
    )
    return 0 if result["completion_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
