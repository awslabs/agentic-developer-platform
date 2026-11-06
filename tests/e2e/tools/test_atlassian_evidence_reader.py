"""Controlled records for the retained Atlassian consumer; no live attestation."""

import copy
import hashlib
import io
import json
import tarfile

import pytest

from tests.e2e.tools import atlassian_flow_summary as summary, evidence_reader as reader
from tests.e2e.tools.atlassian_client_matrix import CLIENTS, READS, WRITES
from tests.e2e.tools.atlassian_integration import SetupActionLedger
from tests.e2e.tools.test_atlassian_flow_summary import BINDING, reports  # noqa: F401
from tests.e2e.tools.test_evidence_reader import RUN, fixture


def encoded(value):
    return summary.canonical(value)


def checksum(row):
    row["evidence_sha256"] = hashlib.sha256(
        encoded({key: value for key, value in row.items() if key != "evidence_sha256"})
    ).hexdigest()


def data(tmp_path, *, setup=False):
    _, _, objects = fixture(tmp_path)
    archive = next(value for key, value in objects.items() if key.endswith(".tar.gz"))
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as packed:
        files = {member.name: packed.extractfile(member).read() for member in packed}
    report = json.loads(files["evidence.json"])
    scenario = "SCN-8454-01" if setup else "SCN-8454-02"
    report.update(
        scenario_filter=scenario, selected_scenario_ids=[scenario], scenarios=[]
    )
    arguments = {
        key: RUN[key]
        for key in (
            "run_id",
            "attempt",
            "revision",
            "gateway",
            "gateway_image",
            "facade_image",
            "authorization_binding",
            "release_source_revision",
        )
    }
    arguments["scenario"] = scenario
    if setup:
        binding = {
            "run_id": "owned-run",
            "owner_id": "owned-member",
            "fixture_kind": "fresh",
            "gateway_sha256": "d" * 64,
        }
        actions = [
            {
                "action": action,
                "actor": "machine"
                if action in SetupActionLedger.MACHINE_ACTIONS
                else "human",
                "status": "observed",
                "evidence_sha256": "a" * 64,
            }
            for action in sorted(
                SetupActionLedger.MACHINE_ACTIONS | {"consent", "site_selection"}
            )
        ]
        snapshot = {
            **binding,
            "version": 1,
            "phase": "cleaned",
            "connection_id": "connection",
            "revision": "revision",
            "actions": actions,
        }
        files["setup-recovery.json"] = encoded(snapshot)
        arguments["setup_binding"] = binding
        report["scenarios"] = [
            {
                "scenario_id": scenario,
                "client": "operator",
                "tool": "setup",
                "status": "pass",
                "evidence_scope": "deployed-target",
                "cleanup": {"status": "confirmed"},
                "observations": {
                    "scenario": "SCN-01",
                    "status": "pass",
                    "cleanup": "confirmed",
                    "fixture_kind": "fresh",
                    "unverified_actions": [],
                    "policy_required": False,
                },
            }
        ]
    else:
        for client in sorted(CLIENTS):
            for tool in sorted(READS | WRITES):
                observed = {
                    "source": "confirmed-client-operation-and-direct-provider",
                    "operation_id": client + "-" + tool,
                    "effect_count": 1 if tool in WRITES else 0,
                    **{
                        key: "a" * 64
                        for key in (
                            "content_sha256",
                            "installed_cli_sha256",
                            "installed_mcp_sha256",
                            "discovery_sha256",
                        )
                    },
                }
                record = {
                    "schema": "installed-client-evidence/v1",
                    "scenario_id": scenario,
                    "client": client,
                    "cell_id": client + ":" + tool,
                    "status": "pass",
                    "acceptance_ids": ["AC-02"],
                    "run": {"id": RUN["run_id"], "attempt": RUN["attempt"]},
                    "target": report["deployed_revisions"],
                    "source": {
                        "harness_revision": RUN["revision"],
                        "gateway_revision": RUN["gateway"],
                        "invocation": observed["source"],
                    },
                    "provider_source": "direct-atlassian-provider",
                }
                row = {
                    "scenario_id": scenario,
                    "client": client,
                    "tool": tool,
                    "cell_id": record["cell_id"],
                    "status": "pass",
                    "client_version": "fixture-1",
                    "evidence_scope": "deployed-target",
                    "cleanup": {"status": "confirmed"},
                    "observations": observed,
                    "client_evidence": record,
                }
                checksum(row)
                report["scenarios"].append(row)
    files["evidence.json"] = encoded(report)
    return files, report, arguments


@pytest.mark.parametrize("setup,count", [(True, 1), (False, 16)])
def test_reader_recognizes_atlassian_records_without_requiring_older_client_rows(
    tmp_path, setup, count
):
    files, _, arguments = data(tmp_path, setup=setup)
    assert reader.validate_rows(files, **arguments) == count


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "cleanup",
        "effect",
        "source",
        "scope",
        "client",
        "content",
        "operation",
        "tampered",
        "run",
        "authorization",
    ],
)
def test_matrix_refuses_incomplete_wrong_or_unqualified_evidence(tmp_path, change):
    files, report, arguments = data(tmp_path)
    row = report["scenarios"][0]
    if change == "missing":
        report["scenarios"].pop()
    elif change == "duplicate":
        report["scenarios"].append(copy.deepcopy(row))
    elif change == "cleanup":
        row["cleanup"]["status"] = "unverified"
    elif change == "effect":
        row["observations"]["effect_count"] = True
    elif change == "source":
        row["observations"]["source"] = "generated-fixture"
    elif change == "scope":
        row["evidence_scope"] = "repository-integration"
    elif change == "client":
        row["client_evidence"]["client"] = "cli"
    elif change == "content":
        del row["observations"]["content_sha256"]
    elif change == "operation":
        report["scenarios"][1]["observations"]["operation_id"] = row["observations"][
            "operation_id"
        ]
        checksum(report["scenarios"][1])
    elif change == "run":
        row["client_evidence"]["run"]["attempt"] = "9"
    elif change == "authorization":
        del files["authorization.json"]
    if change != "tampered":
        checksum(row)
    else:
        row["client_version"] = "different"
    files["evidence.json"] = encoded(report)
    with pytest.raises(ValueError):
        reader.validate_rows(files, **arguments)


@pytest.mark.parametrize(
    "change", ["owner", "run", "gateway", "phase", "action", "policy", "binding"]
)
def test_setup_requires_owned_complete_ledger_and_confirmed_cleanup(tmp_path, change):
    files, report, arguments = data(tmp_path, setup=True)
    snapshot = json.loads(files["setup-recovery.json"])
    if change in {"owner", "run", "gateway"}:
        key = {"owner": "owner_id", "run": "run_id", "gateway": "gateway_sha256"}[
            change
        ]
        snapshot[key] = "f" * 64
    elif change == "phase":
        snapshot["phase"] = "selected"
    elif change == "action":
        snapshot["actions"].pop()
    elif change == "policy":
        report["scenarios"][0]["observations"]["policy_required"] = True
    else:
        arguments.pop("setup_binding")
    files["setup-recovery.json"] = encoded(snapshot)
    files["evidence.json"] = encoded(report)
    with pytest.raises(ValueError):
        reader.validate_rows(files, **arguments)


def test_retained_reader_rejects_duplicate_json_fields(tmp_path):
    files, _, arguments = data(tmp_path)
    files["evidence.json"] = files["evidence.json"].replace(
        b'"matrix_complete":true', b'"matrix_complete":false,"matrix_complete":true'
    )
    with pytest.raises(ValueError, match="Duplicate"):
        reader.validate_rows(files, **arguments)


def test_missing_scenarios_cannot_be_hidden_by_a_complete_producer_summary(
    tmp_path, reports  # noqa: F811
):
    files, evaluation, arguments = data(tmp_path)
    live, stages = reports
    evaluation.update(
        harness_revision=BINDING["definition_revision"],
        github_run_id=BINDING["run_id"],
        github_run_attempt=BINDING["run_attempt"],
    )
    files["evidence.json"] = encoded(evaluation)
    files["live.json"] = encoded(live)
    files.update(
        {f"stage-{stage}.json": encoded(value) for stage, value in stages.items()}
    )
    files["atlassian-flow-summary.json"] = encoded(
        summary.assemble_summary(live, stages, binding=BINDING)
    )
    result = reader.read_flow_bundle(files, binding=BINDING, validation=arguments)
    assert result["completion_ready"] is False
    assert "readback.scenario-inventory" in result["aggregation"]["failed_predicates"]
    assert "SCN-8454-03" in result["scenario_readback"]["unverified_scenarios"]
    assert result["scenario_readback"]["rows"] == evaluation["scenarios"]


def test_unimplemented_scenarios_remain_unqualified_even_when_status_claims_pass(
    tmp_path, reports  # noqa: F811
):
    files, evaluation, arguments = data(tmp_path)
    live, stages = reports
    evaluation.update(
        selected_scenario_ids=list(summary.FLOW_SCENARIOS),
        harness_revision=BINDING["definition_revision"],
        github_run_id=BINDING["run_id"],
        github_run_attempt=BINDING["run_attempt"],
    )
    files.update(
        {
            "evidence.json": encoded(evaluation),
            "live.json": encoded(live),
            **{
                f"stage-{stage}.json": encoded(value) for stage, value in stages.items()
            },
        }
    )
    result = reader.read_flow_bundle(files, binding=BINDING, validation=arguments)
    assert result["completion_ready"] is False
    assert result["scenario_readback"]["qualified_scenarios"] == []
    assert len(summary.completion_contract()) == 81
