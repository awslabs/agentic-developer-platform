"""Real original-session recovery and cleanup with modeled external boundaries."""

from copy import deepcopy
import json

import pytest

from tests.e2e.tools import atlassian_recovery_consumer as consumer
from tests.e2e.tools import atlassian_recovery_scenario as scenario
from tests.e2e.tools import test_atlassian_fixture_lifecycle as lifecycle_suite


runtime = lifecycle_suite.runtime
lifecycle = lifecycle_suite.lifecycle


@pytest.mark.parametrize("selection", ["SCN-8454-0[12]", "SCN-8454-0[124]"])
def test_combined_setup_and_native_cases_do_not_open_a_generic_client(
    tmp_path, lifecycle, selection
):
    from tests.e2e.tools import runner
    from tests.e2e.tools import test_atlassian_native_matrix as native_suite

    matrix, _, _, runtime_state = lifecycle
    matrix.close()
    plans, fixtures, state, connection = runtime_state
    calls = []

    def client_factory(command, **kwargs):
        calls.append(kwargs.get("environ"))
        assert kwargs.get("environ") is not None
        return connection(command, **kwargs)

    output = tmp_path / "combined"
    assert (
        runner.evaluate(
            selection,
            output,
            "https://gateway.example.test",
            native_suite.SOURCE,
            native_suite.IMAGE,
            native_suite.IMAGE,
            command=["unused"],
            live=True,
            client_factory=client_factory,
            atlassian_client_fixtures=fixtures,
            atlassian_native_plans=plans,
        )
        is False
    )
    report = json.loads((output / "evidence.json").read_text())
    assert calls and all(call is not None for call in calls)
    assert len(state["dispatches"]) == 16
    assert report["scenarios"][0]["status"] == "blocked"
    assert all(
        row["status"] == "pass"
        for row in report["scenarios"]
        if row["scenario_id"] == "SCN-8454-02"
    )


@pytest.mark.parametrize("client", ["claude-code", "codex"])
def test_original_interrupted_run_is_recovered_and_cleaned_twice(lifecycle, client):
    matrix, restart, control, state = lifecycle
    state[2]["uncertain"] = (client, "create_jira_issue")
    assert matrix.execute(client, "create_jira_issue")["status"] == "unknown"
    matrix.close()
    matrix = restart()
    result = scenario.run_recovery(matrix, client, produce=False)
    assert result["status"] == "pass"
    assert state[2]["dispatches"] == [(client, "create_jira_issue")]
    assert len(state[2]["exchanges"]) == 1
    assert control["deletions"] == [client]
    record = consumer.read_json(
        matrix.fixtures[client]["ledger"].path.parent / consumer.JOURNAL
    )
    assert [attempt["status"] for attempt in record["attempts"]] == [
        "started",
        "confirmed",
        "confirmed",
    ]
    matrix.close()
    matrix = restart()
    assert consumer.consume(matrix, client)["status"] == "confirmed"
    assert state[2]["dispatches"] == [(client, "create_jira_issue")]
    assert control["deletions"] == [client]
    matrix.close()


def test_consumer_does_not_start_a_fresh_session(lifecycle):
    matrix, _, control, state = lifecycle
    with pytest.raises(OSError):
        consumer.consume(matrix, "codex")
    assert not state[2]["exchanges"] and not state[2]["dispatches"]
    assert not control["deletions"]
    matrix.close()


def test_real_failed_first_repair_retains_unknown_until_status_recovers(lifecycle):
    matrix, restart, control, state = lifecycle
    state[2]["uncertain"] = ("codex", "create_jira_issue")
    assert matrix.execute("codex", "create_jira_issue")["status"] == "unknown"
    matrix.close()
    state[2]["status_failure"] = True
    matrix = restart()
    assert consumer.consume(matrix, "codex")["status"] == "pending"
    assert control["deletions"] == []
    matrix.close()
    state[2]["status_failure"] = False
    matrix = restart()
    assert scenario.run_recovery(matrix, "codex", produce=False)["status"] == "pass"
    assert state[2]["dispatches"] == [("codex", "create_jira_issue")]
    matrix.close()


@pytest.mark.parametrize(
    "fault",
    ["receipt", "provider", "unknown", "extra_call", "first_repair", "unrelated"],
)
def test_reader_rejects_incomplete_or_substituted_recovery(lifecycle, fault):
    matrix, _, _, state = lifecycle
    state[2]["uncertain"] = ("codex", "create_jira_issue")
    assert matrix.execute("codex", "create_jira_issue")["status"] == "unknown"
    result = scenario.run_recovery(matrix, "codex", produce=False)
    assert result["status"] == "pass"
    evidence = deepcopy(result["observations"])
    if fault == "receipt":
        next(iter(evidence["originals"].values()))["original"]["private_receipt"][
            "root_human_id"
        ] = "another-owner"
    elif fault == "provider":
        evidence["provider"]["direct"]["created-one"] = {"id": "created-one"}
    elif fault == "unknown":
        evidence["current"]["pending"].append("original-operation")
    elif fault == "extra_call":
        evidence["current"]["original_calls"]["new"] = {}
    elif fault == "first_repair":
        evidence["consumer"]["attempts"].pop(0)
    else:
        evidence["unrelated"]["effect_count"] = 1
    with pytest.raises((ValueError, KeyError)):
        scenario.qualify_recovery(
            evidence, client="codex", revision=matrix.gateway_revision
        )
    matrix.close()


@pytest.mark.parametrize("selection", ["SCN-8454-04", "SCN-8454-0[24]"])
def test_shared_runner_produces_its_own_fault_without_promoting_local_durability(
    tmp_path, lifecycle, selection
):
    from tests.e2e.tools import evidence_reader, runner
    from tests.e2e.tools import test_atlassian_native_matrix as native_suite

    matrix, restart, control, runtime_state = lifecycle
    matrix.close()
    plans, fixtures, state, connection = runtime_state
    output = tmp_path / "evidence"
    passed = runner.evaluate(
        selection,
        output,
        "https://gateway.example.test",
        native_suite.SOURCE,
        native_suite.IMAGE,
        native_suite.IMAGE,
        command=["unused"],
        live=True,
        client_factory=connection,
        atlassian_client_fixtures=fixtures,
        atlassian_native_plans=plans,
    )
    report = json.loads((output / "evidence.json").read_text())
    assert passed is False
    assert len(state["dispatches"]) == 16 and len(state["exchanges"]) == 2
    assert sorted(control["deletions"]) == ["claude-code", "codex"]
    assert all(
        row["status"] == ("pass" if row["scenario_id"] == "SCN-8454-02" else "blocked")
        for row in report["scenarios"]
    )
    assert report["live_attestation"] is False
    for row in report["scenarios"]:
        if row["scenario_id"] == "SCN-8454-04":
            assert row["recovery"]["cleanup"] == "confirmed"
            assert row["recovery"]["runner_loss"] == "same-filesystem-only"
    matrix = restart()
    bindings = {
        client: {
            key: value
            for key, value in consumer.binding(matrix, client).items()
            if key != "plan_sha256"
        }
        for client in fixtures
    }
    arguments = dict(
        selected=report["selected_scenario_ids"],
        run_id=report["github_run_id"],
        attempt=report["github_run_attempt"],
        revision=report["harness_revision"],
        target=report["deployed_revisions"],
        recovery_bindings=bindings,
    )
    with pytest.raises(ValueError):
        evidence_reader.validate_atlassian_rows({}, report, **arguments)
    for row in report["scenarios"]:
        row["status"] = "pass"
    with pytest.raises(ValueError):
        evidence_reader.validate_atlassian_rows({}, report, **arguments)
    matrix.close()
