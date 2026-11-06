"""Project reader-qualified results into every original acceptance inventory.

The qualification sets and controlled-stage reports are internal outputs of
independent readers. They must never be loaded from operator configuration or
inferred from producer pass flags. This module does not replace those readers;
it removes the need to hand-author live.json and seven stage summary files.
"""

from copy import deepcopy
import hashlib
import re

from tests.e2e.tools import atlassian_flow_summary as flow


def _require(condition, message="Reader-qualified completion inputs required"):
    if not condition:
        raise ValueError(message)


def _hash(value):
    return hashlib.sha256(flow.canonical(value)).hexdigest()


def inventory():
    contract = flow.completion_contract()
    records = {
        item["criterion_id"]: item
        for item in contract
        if item["operation"] == "records"
    }
    return {
        "cells": records["live.new-tools"]["expected"],
        "cases": records["live.coverage"]["expected"],
        "criteria": records["completion.criteria"]["expected"],
        "stages": records["completion.stages"]["expected"],
        "stage_criteria": {
            stage: records[stage + ".cases"]["expected"] for stage in flow.STAGES
        },
    }


def _qualified(values, allowed):
    _require(type(values) is set and values <= set(allowed))
    return values


def _evaluation(evaluation, binding, qualified):
    flow.validate_binding(binding)
    _require(type(evaluation) is dict)
    _require(
        evaluation.get("evidence_schema") == "workflow-evaluation/v1"
        and type(evaluation.get("schema_version")) is int
        and evaluation["schema_version"] == 1
        and evaluation.get("harness_revision") == binding["definition_revision"]
        and evaluation.get("github_run_id") == binding["run_id"]
        and evaluation.get("github_run_attempt") == binding["run_attempt"]
    )
    selected = evaluation.get("selected_scenario_ids")
    rows = evaluation.get("scenarios")
    _require(type(selected) is list and all(type(item) is str for item in selected))
    _require(len(selected) == len(set(selected)))
    _require(type(rows) is list and all(type(row) is dict for row in rows))
    _require(len(rows) <= 512 and len(flow.canonical(evaluation)) <= flow.MAX_BYTES)
    _require(qualified <= set(selected))
    for scenario in qualified:
        matching = [row for row in rows if row.get("scenario_id") == scenario]
        _require(
            matching
            and all(
                row.get("status") == "pass"
                and row.get("cleanup", {}).get("status") == "confirmed"
                for row in matching
            )
        )
    return rows


def _cell(row):
    client = {"claude-code": "claude", "codex": "codex"}.get(row.get("client"))
    return None if client is None else client + ":" + str(row.get("tool"))


def _controlled(stage, supplied, binding, original):
    """Preserve a partial measured stage and fill every missing original field."""
    missing = {
        "binding": deepcopy(binding),
        "stage_id": stage,
        "criteria": [
            {
                "criterion_id": criterion,
                "phase_status": "unverified",
                "evidence_verified": False,
            }
            for criterion in original
        ],
        "runtime_verified": False,
        "independent_verification": "unverified",
        "cleanup_status": "unverified",
        "unresolved_operations": None,
        "verification_level": "unverified",
        "fixture_source": "unverified",
        "provider_live_attestation": False,
        "test_runtime_artifacts_match": False,
    }
    if supplied is None:
        return missing
    _require(
        type(supplied) is dict
        and supplied.get("binding") == binding
        and supplied.get("stage_id") == stage
    )
    rows = supplied.get("criteria", [])
    _require(type(rows) is list and all(type(row) is dict for row in rows))
    identifiers = [row.get("criterion_id") for row in rows]
    _require(
        all(
            type(identifier) is str and identifier in original
            for identifier in identifiers
        )
        and len(identifiers) == len(set(identifiers))
    )
    by_id = {row["criterion_id"]: row for row in rows}
    result = {**missing, **deepcopy(supplied)}
    result["criteria"] = [
        deepcopy(by_id.get(row["criterion_id"], row)) for row in missing["criteria"]
    ]
    return result


def _stage_pass(stage, report):
    document = {"stage_reports": {stage: report}}
    predicates = [
        item
        for item in flow.completion_contract()
        if item["pointer"].startswith("/stage_reports/" + stage + "/")
    ]
    return bool(predicates) and all(
        flow._matches(document, item) for item in predicates
    )


def build_reports(
    evaluation,
    *,
    binding,
    qualified_scenarios,
    qualified_cases=None,
    qualified_original_criteria=None,
    controlled_stages=None,
):
    """Generate complete inventories; unobserved work stays explicitly unverified.

    ``qualified_cases`` and ``qualified_original_criteria`` come from semantic
    readers of the original cases, not this producer or an operator JSON file.
    A controlled-stage phase pass never grants a full live story criterion.
    Only the six fully qualified SCN-8454 scenarios map directly to their own
    six ACs. Older story and epic criteria require their original verification.
    """
    original = inventory()
    qualified = _qualified(qualified_scenarios, flow.FLOW_SCENARIOS)
    cases = _qualified(
        set() if qualified_cases is None else qualified_cases, original["cases"]
    )
    criteria = _qualified(
        set() if qualified_original_criteria is None else qualified_original_criteria,
        original["criteria"],
    )
    # AC-8454 authority comes only from the corresponding scenario reader.
    _require(not any(criterion.startswith("8454.") for criterion in criteria))
    criteria = criteria | {
        "8454.AC-" + scenario.rsplit("-", 1)[1] for scenario in qualified
    }
    rows = _evaluation(evaluation, binding, qualified)
    stages = {} if controlled_stages is None else controlled_stages
    _require(type(stages) is dict and set(stages) <= set(flow.STAGES))
    stages = {
        stage: _controlled(
            stage, stages.get(stage), binding, original["stage_criteria"][stage]
        )
        for stage in flow.STAGES
    }
    cells, seen = [], {}
    for row in rows:
        if row.get("scenario_id") != "SCN-8454-02":
            continue
        key = _cell(row)
        _require(
            key in original["cells"] and key not in seen,
            "Unique original client cells required",
        )
        seen[key] = row
    for key in original["cells"]:
        row = seen.get(key)
        passed = (
            "SCN-8454-02" in qualified
            and row is not None
            and row.get("status") == "pass"
            and row.get("cleanup", {}).get("status") == "confirmed"
        )
        cells.append(
            {
                "cell_id": key,
                "status": "pass" if passed else "unverified",
                "cleanup": "confirmed" if passed else "unverified",
            }
        )
    complete_scenarios = qualified == set(flow.FLOW_SCENARIOS)
    if "SCN-8454-02" in qualified:
        _require(
            set(seen) == set(original["cells"]),
            "All sixteen original client cells required",
        )
    unknowns = []
    for row in rows:
        pending = row.get("unresolved_operations", [])
        _require(
            type(pending) is list
            and all(
                type(item) is str and re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", item)
                for item in pending
            )
        )
        unknowns.extend(pending)
    # Missing qualification is not evidence that there are no unknown effects.
    operations = sorted(set(unknowns)) if unknowns or complete_scenarios else None
    cleanup = (
        complete_scenarios
        and operations == []
        and all(row["cleanup"] == "confirmed" for row in cells)
    )
    story_stages = {
        "identity-proof": [
            criterion
            for criterion in original["criteria"]
            if criterion.startswith("8446.")
        ],
        "foundation": [
            criterion
            for criterion in original["criteria"]
            if criterion.startswith("8447.")
        ],
        **original["stage_criteria"],
    }
    stage_rows = []
    for stage in original["stages"]:
        ready = set(story_stages[stage]) <= criteria
        if stage in stages:
            ready = ready and _stage_pass(stage, stages[stage])
        # Foundation/proof runtime coverage is independently measured as part
        # of the complete current release, not inherited from historical PR CI.
        ready = ready and "release-six-components" in cases
        stage_rows.append(
            {
                "stage_id": stage,
                "status": "pass" if ready else "unverified",
                "runtime_verified": ready,
            }
        )
    prerequisite = [
        "criterion:" + item for item in original["criteria"] if item not in criteria
    ]
    prerequisite.extend(
        "case:" + item for item in original["cases"] if item not in cases
    )
    prerequisite.extend(
        "controlled-stage:" + stage
        for stage in flow.STAGES
        if not _stage_pass(stage, stages[stage])
    )
    prerequisite.extend(
        "scenario:" + item for item in flow.FLOW_SCENARIOS if item not in qualified
    )
    if operations is None:
        prerequisite.append("operation-inventory:unverified")
    live = {
        "binding": deepcopy(binding),
        "matrix_complete": complete_scenarios
        and all(row["status"] == "pass" for row in cells),
        "tool_cells": cells,
        "cases": [
            {"case_id": name, "status": "pass" if name in cases else "unverified"}
            for name in original["cases"]
        ],
        "criteria": [
            {
                "criterion_id": name,
                "status": "pass" if name in criteria else "unverified",
                "evidence_verified": name in criteria,
            }
            for name in original["criteria"]
        ],
        "stages": stage_rows,
        "cleanup_status": "confirmed" if cleanup else "unverified",
        "unresolved_operations": operations,
        "unresolved_prerequisites": prerequisite,
        "final_release_state": "candidate_verified"
        if {"release-six-components", "adapter-four-routes", "group-rollback-redeploy"}
        <= cases
        else "unverified",
        "provider_live_attestation": evaluation.get("live_attestation") is True
        and {"SCN-8454-02", "SCN-8454-03", "SCN-8454-04", "SCN-8454-05"} <= qualified,
        "source_evaluation_sha256": _hash(evaluation),
    }
    summary = flow.assemble_evaluation_summary(
        live, stages, evaluation, binding=binding, qualified_scenarios=qualified
    )
    return {"live": live, "stages": stages, "summary": summary}


def verify_reports(reports, evaluation, **inputs):
    """Rebuild with independently reread sources and rerun original predicates."""
    expected = build_reports(evaluation, **inputs)
    _require(
        flow._same(reports, expected),
        "Completion reports differ from independently qualified sources",
    )
    return expected
