"""Controlled legacy drivers; these tests make no live acceptance claim."""

from copy import deepcopy

import pytest

from tests.e2e.tools import atlassian_regression_scenario as regression
from tests.e2e.tools.test_github_integration import MatrixDriver, MatrixObserver
from tests.e2e.tools.test_jira_integration import (
    Driver as JiraDriver,
    Observer as JiraObserver,
)
from tests.e2e.tools.test_slack_integration import (
    LiveDriver as SlackDriver,
    LiveObserver as SlackObserver,
)
from tests.e2e.tools.test_runner import GATEWAY_IMAGE, IMAGE, SHA
from tests.e2e.tools import test_atlassian_flow_summary as flow_tests


BINDING = {
    "source_revision": SHA,
    "definition_revision": "d" * 40,
    "run_id": "101",
    "run_attempt": "1",
    "release_sha256": "e" * 64,
}
TARGET = {"gateway": SHA, "gateway_image": GATEWAY_IMAGE, "facade_image": IMAGE}
MODELS = {
    "github": (MatrixDriver, MatrixObserver),
    "jira": (JiraDriver, JiraObserver),
    "slack": (SlackDriver, SlackObserver),
}


def fixture(connector, client, defect=None):
    driver_type, observer_type = MODELS[connector]
    observer = observer_type(defect)
    # Model the independently captured catalogue, not a status claim. The
    # checked-in legacy models intentionally use different immutable targets.
    original_deployment = observer.deployment
    observer.deployment = lambda **kwargs: {**original_deployment(**kwargs), **TARGET}
    observer.catalogue = lambda **kwargs: {
        "source": "independent-deployed-catalogue",
        "target": deepcopy(TARGET),
        "tools": {
            name: deepcopy(tool)
            for name, tool in regression.BASELINE.items()
            if name.startswith(connector + "___")
        },
    }
    return {"driver": driver_type(observer, client), "observer": observer}


def collect(fixtures=None):
    return regression.collect_regression(
        binding=BINDING, target=TARGET, fixtures=fixtures, live=False
    )


def test_registered_scenario_retains_all_unavailable_legacy_cells(
    tmp_path, monkeypatch
):
    import json
    from tests.e2e.tools import runner

    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    output = tmp_path / "evidence"
    assert (
        runner.evaluate(
            "SCN-8454-05",
            output,
            "https://gateway.example.test",
            TARGET["gateway"],
            TARGET["gateway_image"],
            TARGET["facade_image"],
            command=["unused"],
            live=True,
            client_factory=lambda *_: pytest.fail("Legacy driver owns client startup"),
            atlassian_flow_binding=BINDING,
        )
        is False
    )
    report = json.loads((output / "evidence.json").read_bytes())
    assert report["selected_scenario_ids"] == ["SCN-8454-05"]
    row = report["scenarios"][0]
    assert row["status"] == "blocked"
    assert len(row["observations"]["cells"]) == 9
    assert {cell["status"] for cell in row["observations"]["cells"]} == {"blocked"}
    assert set(row["predecessors"]) == {"8063", "8076", "7266"}
    assert report["live_attestation"] is False


def test_registered_controlled_regression_cannot_pass_live_reader(
    tmp_path, monkeypatch
):
    import json
    from tests.e2e.tools import evidence_reader, runner

    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    output = tmp_path / "evidence"
    fixtures = {
        (connector, client): fixture(connector, client)
        for connector in regression.CHECKERS
        for client in regression.CLIENTS
    }
    assert (
        runner.evaluate(
            "SCN-8454-05",
            output,
            "https://gateway.example.test",
            TARGET["gateway"],
            TARGET["gateway_image"],
            TARGET["facade_image"],
            command=["unused"],
            atlassian_flow_binding=BINDING,
            atlassian_regression_fixtures=fixtures,
        )
        is True
    )
    report = json.loads((output / "evidence.json").read_bytes())
    assert report["scenarios"][0]["regression"]["status"] == "pass"
    assert report["live_attestation"] is False
    with pytest.raises(ValueError):
        evidence_reader.validate_atlassian_rows(
            {},
            report,
            selected=["SCN-8454-05"],
            run_id="101",
            attempt="1",
            revision=BINDING["definition_revision"],
            target=TARGET,
            regression_binding=BINDING,
        )


@pytest.fixture
def completed():
    return collect(
        {
            (connector, client): fixture(connector, client)
            for connector in MODELS
            for client in regression.CLIENTS
        }
    )


@pytest.mark.parametrize("connector", MODELS)
@pytest.mark.parametrize("client", regression.CLIENTS)
def test_executes_existing_semantic_checker_and_cleanup_then_readonly_validation(
    connector, client
):
    model = fixture(connector, client)
    row = regression.run_legacy_cell(
        connector, client, **model, target=TARGET, live=False
    )
    assert row["status"] == "pass", row
    before = deepcopy(model["observer"].__dict__)
    qualified = regression.qualify_legacy_cell(
        row, connector=connector, client=client, target=TARGET, require_live=False
    )
    assert qualified["status"] == "pass" and qualified["cleanup"] == "confirmed"
    assert model["observer"].__dict__ == before
    with pytest.raises(ValueError):
        regression.qualify_legacy_cell(
            row, connector=connector, client=client, target=TARGET
        )


def test_complete_controlled_collection_retains_pinned_inventory_and_all_predicates(
    completed,
):
    result = regression.qualify_regression(
        completed, binding=BINDING, target=TARGET, require_live=False
    )
    assert len(result["cells"]) == 9
    assert (
        result["baseline_sha256"]
        == "4a365f4f39d87a8629435c1a7510e8cc6b74eb9638790360ecb77d63528ff9b9"
    )
    assert len(regression.flow.completion_contract()) == 81
    assert completed["predecessor_inventory"]["7266"] == [
        "TLS-S15-AC01",
        "TLS-S15-AC02",
        "TLS-S15-AC03",
    ]
    assert "8080.AC-04" in completed["predecessor_inventory"]["8063"]
    assert "8063.EC-03" in completed["predecessor_inventory"]["8063"]
    with pytest.raises(ValueError):
        regression.qualify_regression(completed, binding=BINDING, target=TARGET)


def test_missing_provider_access_keeps_every_required_cell_and_never_passes():
    report = regression.collect_regression(binding=BINDING, target=TARGET)
    assert len(report["cells"]) == 9
    assert all(
        row["status"] == "blocked"
        and row["reason"] == "missing-provider-access"
        and row["events"] == []
        for row in report["cells"]
    )
    with pytest.raises(ValueError):
        regression.qualify_regression(report, binding=BINDING, target=TARGET)


@pytest.mark.parametrize(
    "field",
    (
        "schema",
        "binding",
        "target",
        "baseline",
        "contract_sha256",
        "predecessor_inventory",
    ),
)
def test_foreign_or_changed_top_level_binding_rejected(completed, field):
    completed[field] = {}
    with pytest.raises(ValueError):
        regression.qualify_regression(
            completed, binding=BINDING, target=TARGET, require_live=False
        )


@pytest.mark.parametrize(
    "change",
    (
        lambda rows: rows.pop(),
        lambda rows: rows.append(deepcopy(rows[0])),
        lambda rows: rows.__setitem__(1, deepcopy(rows[0])),
        lambda rows: rows.reverse(),
        lambda rows: rows[0].update(status="skip"),
        lambda rows: rows[0].update(events=[]),
        lambda rows: rows[0].update(result={"status": "pass"}),
        lambda rows: rows[0]["events"].pop(),
        lambda rows: rows[0]["events"].append(deepcopy(rows[0]["events"][-1])),
        lambda rows: rows[0]["events"].reverse(),
        lambda rows: rows[0]["catalogue"]["tools"].clear(),
        lambda rows: rows[0]["catalogue"]["tools"]["github___get_user"].update(
            scope="repo"
        ),
    ),
)
def test_omitted_duplicate_tampered_or_status_only_evidence_rejected(completed, change):
    change(completed["cells"])
    with pytest.raises((ValueError, regression.FixtureUnavailable)):
        regression.qualify_regression(
            completed, binding=BINDING, target=TARGET, require_live=False
        )


@pytest.mark.parametrize(
    "connector,defect",
    (
        ("github", "repo_mutation"),
        ("github", "wrong_user_dispatch"),
        ("github", "unknown_cleanup"),
        ("github", "no_refresh"),
        ("jira", "extra_dispatch"),
        ("jira", "wrong_owner"),
        ("jira", "mutation"),
        ("jira", "unknown_cleanup"),
        ("slack", "wrong_identity"),
        ("slack", "wrong_user"),
        ("slack", "uncleaned"),
    ),
)
def test_original_provider_count_identity_refresh_and_cleanup_failures_preserved(
    connector, defect, monkeypatch
):
    if defect == "uncleaned":
        clock = iter((0, 601))
        monkeypatch.setattr(
            "tests.e2e.tools.slack_integration.monotonic", lambda: next(clock)
        )
    row = regression.run_legacy_cell(
        connector, "cli", **fixture(connector, "cli", defect), target=TARGET, live=False
    )
    assert row["status"] == "blocked"
    with pytest.raises(ValueError):
        regression.qualify_legacy_cell(
            row, connector=connector, client="cli", target=TARGET, require_live=False
        )


def test_scope_or_schema_difference_blocks_before_any_provider_effect():
    model = fixture("slack", "cli")
    original = model["observer"].catalogue()
    original["tools"]["slack___post_message"]["inputSchema"]["additionalProperties"] = (
        True
    )
    model["observer"].catalogue = lambda **kwargs: original
    row = regression.run_legacy_cell("slack", "cli", **model, target=TARGET, live=False)
    assert row["status"] == "blocked" and row["events"] == []
    assert model["observer"].states == {}


def test_live_fixture_flags_required_before_calls():
    model = fixture("github", "cli")
    row = regression.run_legacy_cell("github", "cli", **model, target=TARGET, live=True)
    assert row["reason"] == "missing-provider-access"
    assert model["observer"].attempts == {}


def test_cross_attempt_observation_rejected_even_when_result_status_unchanged(
    completed,
):
    completed["cells"][0]["events"][-1]["kwargs"]["attempt_id"] = (
        "11111111-1111-4111-8111-111111111111"
    )
    with pytest.raises(ValueError):
        regression.qualify_regression(
            completed, binding=BINDING, target=TARGET, require_live=False
        )


def test_mutated_provider_count_cannot_be_certified_by_original_pass(completed):
    event = next(
        item
        for item in completed["cells"][0]["events"]
        if item["side"] == "observer"
        and item["method"] == "provider_calls"
        and item["result"] == 2
    )
    event["result"] = 3
    with pytest.raises(ValueError):
        regression.qualify_regression(
            completed, binding=BINDING, target=TARGET, require_live=False
        )


def test_unknown_fixture_key_is_not_silently_dropped():
    with pytest.raises(ValueError):
        collect({("other", "cli"): {}})


def test_predecessor_gaps_preserve_original_criteria_without_inventing_coverage(
    completed,
):
    assessed = regression.predecessor_gaps(
        completed, binding=BINDING, target=TARGET, require_live=False
    )
    assert [
        row["criterion_id"] for row in assessed["8063"] if row["status"] == "pass"
    ] == ["8075.AC-04", "8076.AC-04", "8077.AC-04"]
    assert all(row["status"] == "unverified" for row in assessed["7266"])
    assert [row["status"] for row in assessed["8076"]] == ["unverified"] * 3 + ["pass"]
    live = regression.predecessor_gaps(completed, binding=BINDING, target=TARGET)
    assert all(row["status"] == "unverified" for rows in live.values() for row in rows)


def test_rollup_retains_original_legacy_requirements_and_qualified_scenarios():
    live, stages = flow_tests.reports.__wrapped__()
    binding = live["binding"]
    evaluation = {
        "evidence_schema": "workflow-evaluation/v1",
        "schema_version": 1,
        "harness_revision": binding["definition_revision"],
        "github_run_id": binding["run_id"],
        "github_run_attempt": binding["run_attempt"],
        "matrix_complete": True,
        "live_attestation": True,
        "selected_scenario_ids": list(regression.flow.FLOW_SCENARIOS),
    }
    qualified = set(regression.flow.FLOW_SCENARIOS)
    result = regression.regression_rollup(
        live, stages, evaluation, binding=binding, qualified_scenarios=qualified
    )
    assert result["completion_ready"] is True
    for row in live["cases"]:
        if row["case_id"] == "legacy-7266":
            row["status"] = "unverified"
    result = regression.regression_rollup(
        live, stages, evaluation, binding=binding, qualified_scenarios=qualified
    )
    assert result["completion_ready"] is False
    assert "live.coverage" in result["aggregation"]["failed_predicates"]


def test_unknown_predicate_inventory_is_not_an_approved_shortcut(completed):
    completed["predecessor_inventory"]["8063"].pop()
    with pytest.raises(ValueError):
        regression.predecessor_gaps(
            completed, binding=BINDING, target=TARGET, require_live=False
        )
