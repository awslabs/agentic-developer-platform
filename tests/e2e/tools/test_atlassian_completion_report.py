"""Model independent reader outputs; never claim a live provider was exercised."""

from copy import deepcopy

import pytest

from tests.e2e.tools import atlassian_completion_report as completion
from tests.e2e.tools import atlassian_flow_summary as flow
from tests.e2e.tools import test_atlassian_flow_summary as fixture_source
from tests.e2e.tools.test_atlassian_flow_summary import BINDING


@pytest.fixture
def reports():
    return fixture_source.reports.__wrapped__()


def evaluation(*, rows=True):
    inventory = completion.inventory()
    cases = []
    if rows:
        for cell in inventory["cells"]:
            client, tool = cell.split(":", 1)
            cases.append(
                {
                    "scenario_id": "SCN-8454-02",
                    "client": "claude-code" if client == "claude" else client,
                    "tool": tool,
                    "status": "pass",
                    "cleanup": {"status": "confirmed"},
                }
            )
        cases.extend(
            {
                "scenario_id": identifier,
                "status": "pass",
                "cleanup": {"status": "confirmed"},
            }
            for identifier in flow.FLOW_SCENARIOS
            if identifier != "SCN-8454-02"
        )
    return {
        "schema_version": 1,
        "evidence_schema": "workflow-evaluation/v1",
        "harness_revision": BINDING["definition_revision"],
        "github_run_id": BINDING["run_id"],
        "github_run_attempt": BINDING["run_attempt"],
        "selected_scenario_ids": list(flow.FLOW_SCENARIOS),
        "scenarios": cases,
        "matrix_complete": True,
        "live_attestation": True,
    }


def complete_inputs(reports):
    return {
        "binding": deepcopy(BINDING),
        "qualified_scenarios": set(flow.FLOW_SCENARIOS),
        "qualified_cases": set(completion.inventory()["cases"]),
        "qualified_original_criteria": {
            criterion
            for criterion in completion.inventory()["criteria"]
            if not criterion.startswith("8454.")
        },
        "controlled_stages": deepcopy(reports[1]),
    }


def test_missing_data_generates_every_original_row_without_fabricating_absence():
    result = completion.build_reports(
        evaluation(rows=False), binding=BINDING, qualified_scenarios=set()
    )
    live, stages, summary = (result[key] for key in ("live", "stages", "summary"))
    assert len(live["tool_cells"]) == 16 and len(live["cases"]) == 21
    assert len(live["criteria"]) == 42 and len(live["stages"]) == 9
    assert set(stages) == set(flow.STAGES) and len(stages) == 7
    assert (
        live["unresolved_operations"] is None and live["cleanup_status"] == "unverified"
    )
    assert live["provider_live_attestation"] is False
    assert (
        not summary["completion_ready"] and summary["aggregation"]["failed_predicates"]
    )
    assert not any(row["evidence_verified"] for row in live["criteria"])


def test_raw_pass_flags_cannot_supply_independent_qualification():
    result = completion.build_reports(
        evaluation(), binding=BINDING, qualified_scenarios=set()
    )
    assert all(row["status"] == "unverified" for row in result["live"]["tool_cells"])
    assert not result["summary"]["completion_ready"]


def test_full_internal_reader_outputs_have_a_reachable_completion_path(reports):
    source, inputs = evaluation(), complete_inputs(reports)
    before = deepcopy((source, inputs))
    result = completion.build_reports(source, **inputs)
    assert result["summary"]["completion_ready"]
    assert result["summary"]["aggregation"]["failed_predicates"] == []
    assert len(flow.completion_contract()) == 81
    assert completion.verify_reports(result, source, **inputs) == result
    assert (source, inputs) == before


def test_controlled_phase_success_does_not_promote_any_live_criterion(reports):
    result = completion.build_reports(
        evaluation(rows=False),
        binding=BINDING,
        qualified_scenarios=set(),
        controlled_stages=reports[1],
    )
    assert all(row["status"] == "unverified" for row in result["live"]["criteria"])
    assert all(
        stage["verification_level"] == "controlled_integration"
        for stage in result["stages"].values()
    )
    assert not result["live"]["provider_live_attestation"]


def test_one_scenario_only_qualifies_its_original_harness_ac():
    result = completion.build_reports(
        evaluation(), binding=BINDING, qualified_scenarios={"SCN-8454-01"}
    )
    assert [
        row["criterion_id"]
        for row in result["live"]["criteria"]
        if row["evidence_verified"]
    ] == ["8454.AC-01"]
    assert result["live"]["unresolved_operations"] is None


@pytest.mark.parametrize(
    "fault",
    [
        "missing_cell",
        "duplicate_cell",
        "wrong_client",
        "skipped",
        "foreign_binding",
        "unknown_scenario",
    ],
)
def test_invalid_qualified_source_is_rejected(reports, fault):
    source, inputs = evaluation(), complete_inputs(reports)
    if fault == "missing_cell":
        source["scenarios"].pop(0)
    elif fault == "duplicate_cell":
        source["scenarios"].append(deepcopy(source["scenarios"][0]))
    elif fault == "wrong_client":
        source["scenarios"][0]["client"] = "other"
    elif fault == "skipped":
        source["scenarios"][0]["status"] = "skipped"
    elif fault == "foreign_binding":
        source["github_run_id"] = "9999999"
    else:
        inputs["qualified_scenarios"].add("SCN-8454-99")
    with pytest.raises(ValueError):
        completion.build_reports(source, **inputs)


def test_unresolved_original_operation_is_preserved_and_prevents_completion(reports):
    source = evaluation()
    source["scenarios"][0]["unresolved_operations"] = ["original-operation"]
    result = completion.build_reports(source, **complete_inputs(reports))
    assert result["live"]["unresolved_operations"] == ["original-operation"]
    assert result["live"]["cleanup_status"] == "unverified"
    assert not result["summary"]["completion_ready"]
    assert "live.unknowns" in result["summary"]["aggregation"]["failed_predicates"]


def test_missing_case_and_provider_attestation_remain_explicit(reports):
    inputs = complete_inputs(reports)
    inputs["qualified_cases"].remove("identity-expiry-refresh")
    source = evaluation()
    source["live_attestation"] = False
    result = completion.build_reports(source, **inputs)
    assert not result["live"]["provider_live_attestation"]
    assert "case:identity-expiry-refresh" in result["live"]["unresolved_prerequisites"]
    assert not result["summary"]["completion_ready"]


@pytest.mark.parametrize("field", ["criteria", "cases", "stages", "summary"])
def test_independent_rebuild_rejects_result_promotion(field):
    source = evaluation(rows=False)
    inputs = {"binding": BINDING, "qualified_scenarios": set()}
    result = completion.build_reports(source, **inputs)
    if field == "criteria":
        result["live"]["criteria"][0].update(status="pass", evidence_verified=True)
    elif field == "cases":
        result["live"]["cases"][0]["status"] = "pass"
    elif field == "stages":
        result["stages"]["identity"]["runtime_verified"] = True
    else:
        result["summary"]["completion_ready"] = True
    with pytest.raises(ValueError):
        completion.verify_reports(result, source, **inputs)


def test_partial_controlled_stage_retains_all_original_criteria(reports):
    stage = deepcopy(reports[1]["identity"])
    stage["criteria"] = stage["criteria"][:1]
    result = completion.build_reports(
        evaluation(rows=False),
        binding=BINDING,
        qualified_scenarios=set(),
        controlled_stages={"identity": stage},
    )
    rows = result["stages"]["identity"]["criteria"]
    assert len(rows) == 4 and rows[0]["phase_status"] == "pass"
    assert all(row["phase_status"] == "unverified" for row in rows[1:])
    assert "controlled-stage:identity" in result["live"]["unresolved_prerequisites"]


def test_raw_configuration_cannot_supply_harness_qualification():
    with pytest.raises(ValueError):
        completion.build_reports(
            evaluation(),
            binding=BINDING,
            qualified_scenarios=set(),
            qualified_original_criteria={"8454.AC-01"},
        )
    with pytest.raises(ValueError):
        completion.build_reports(
            evaluation(), binding=BINDING, qualified_scenarios=list(flow.FLOW_SCENARIOS)
        )
