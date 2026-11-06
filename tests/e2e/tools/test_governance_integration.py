"""Isolated governance oracles; no fixture here attests provider billing."""

import json
from copy import deepcopy
from uuid import UUID

import pytest

from tests.e2e.tools import runner
from tests.e2e.tools.governance_integration import FAMILIES, governance_ac01, governance_ac02, governance_ac03, governance_ac04
from tests.e2e.tools.scenarios import CLIENTS

REVISION = "a" * 40
IMAGE = "example.invalid/adp@sha256:" + "b" * 64


def identity(number):
    return str(UUID(int=number))


def fixture():
    denied = {"status": 429, "retry_after_seconds": 15, "provider_calls_before": 0,
              "provider_calls_after": 0, "reservations_before": 0, "reservations_after": 0}
    request_id, receipt_id, reservation_id = (identity(number) for number in (1, 2, 3))
    replay = {"request_id": request_id, "receipt_id": receipt_id, "reservation_id": reservation_id}
    owned = {"tenant_id": "tenant-a", "team_id": "team-a", "user_id": "user-a", "tool": "search",
             "run_id": "run-a", "request_id": request_id, "receipt_id": receipt_id,
             "estimated_cost_usd": "0.007", "settled_cost_usd": None, "accounting_status": "estimate_only"}
    return {
        "governance_ac01": {family: {"cap": deepcopy(denied), "rate": deepcopy(denied)} for family in FAMILIES},
        "governance_ac02": {"replay": {"attempts": [dict(replay), dict(replay)], "cap_usd": "20",
                                       "held_usd": "1", "other_held_usd": "1", "provider_calls_before": 0,
                                       "provider_calls_after": 1, "result_status": "unknown"}},
        "governance_ac03": {"activity": {"owned": owned, "export": dict(owned), "foreign_status": 403}},
        "governance_ac04": {
            client + ":" + tool: {"expected_operations": [identity(10 + index)],
                                   "operations": [{"request_id": identity(10 + index),
                                                   "receipt_id": identity(30 + index),
                                                   "operation_ref": identity(10 + index),
                                                   "execution_id": identity(50 + index),
                                                   "tool_name": ("websearch" if tool == "search" else
                                                                 "code_interpreter" if tool == "interpreter" else "browser") +
                                                                ("___search" if tool == "search" else "___start"),
                                                   "tenant_id": "tenant-a", "team_id": "team-a",
                                                   "user_id": "user-a", "run_id": "run-a",
                                                   "result_status": "confirmed",
                                                   "cleanup_status": "confirmed" if tool != "search" else "not_required",
                                                   "estimated_cost_usd": "0.01", "settled_cost_usd": "0.009",
                                                   "accounting_status": "settled"}],
                                   "pricing_approved": True, "run_cap_usd": "20"}
            for index, (client, tool) in enumerate((client, tool) for client in CLIENTS
                                                    for tool in ("search", "interpreter", "browser"))},
    }


class OfflineObserver:
    isolated = True

    def __init__(self, observations):
        self.exports = {}
        self.providers = {}
        self.calls = []
        for cell, fixture_row in observations["governance_ac04"].items():
            client, tool = cell.split(":")
            for operation in fixture_row["operations"]:
                receipt_id = operation["receipt_id"]
                self.exports[(client, tool, receipt_id)] = {**operation, "tool": operation["tool_name"], "call_id": receipt_id}
                termination = ({"status": "verified", "source": "aws-agentcore-session", "provider_status": "TERMINATED",
                                "observed_at": "2026-01-01T00:00:01Z", "cleanup_elapsed_seconds": 1}
                               if tool != "search" else {"status": "unsupported", "source": "capability"})
                self.providers[(client, operation["tool_name"], operation["operation_ref"],
                                operation["execution_id"])] = {
                    "adapter": "adp-core-provider-observer-v1", "offline_only": True,
                    "client": client, "operation": operation["tool_name"],
                    "operation_ref": operation["operation_ref"], "execution_id": operation["execution_id"],
                    "facts": {"correlation": {"status": "verified", "source": "provider-owned-ledger", "provider_request_id": operation["request_id"],
                                              "provider_receipt_id": receipt_id},
                              "durable_outcome": {"status": "verified", "source": "provider-owned-ledger", "state": operation["result_status"]},
                              "termination": termination,
                              "cost_usd": {"status": "unsupported", "reason": "separate-accounting-required"}},
                }

    def export(self, *, receipt_id, client, tool):
        self.calls.append(("export", client, tool, receipt_id))
        return self.exports.get((client, tool, receipt_id))

    def observe(self, *, operation_ref, execution_id, operation, client, timeout):
        assert timeout == 30
        self.calls.append(("provider", client, operation, operation_ref))
        return self.providers.get((client, operation, operation_ref, execution_id))


def run(tmp_path, monkeypatch, pattern, observations=None, *, live=False, observer=None):
    monkeypatch.setattr(runner, "verify_gateway", lambda _: pytest.fail("fixture contacted gateway"))
    passed = runner.evaluate(pattern, tmp_path, "https://example.invalid", REVISION, IMAGE, IMAGE,
                             command=[], client_factory=lambda _: pytest.fail("fixture opened MCP/provider client"),
                             governance_fixture=observations, governance_observer=observer or
                             (OfflineObserver(observations) if observations is not None else None), live=live)
    return passed, json.loads((tmp_path / "evidence.json").read_text())


@pytest.mark.parametrize("scenario,cell_count", [("SCN-8078-01", len(FAMILIES)), ("SCN-8078-02", 1),
                                                ("SCN-8078-03", 1), ("SCN-8078-04", 3 * len(CLIENTS))])
def test_each_governance_selector_records_only_local_evidence(tmp_path, monkeypatch, scenario, cell_count):
    passed, report = run(tmp_path, monkeypatch, scenario, fixture())
    assert passed and not report["live_attestation"]
    assert report["selected_scenario_ids"] == [scenario]
    assert len(report["scenarios"]) == cell_count
    assert all(row["status"] == "pass" and row["evidence_scope"] == "repository-integration"
               and row["observations"] for row in report["scenarios"])


@pytest.mark.parametrize("scenario", ["SCN-8078-01", "SCN-8078-02", "SCN-8078-03", "SCN-8078-04"])
def test_live_governance_never_uses_local_fixture_or_provider(tmp_path, monkeypatch, scenario):
    passed, report = run(tmp_path, monkeypatch, scenario, fixture(), live=True)
    assert not passed and not report["live_attestation"]
    assert all(row["status"] == "blocked" and row["evidence_scope"] == "deployed-target"
               and "independent" in row["reason"] for row in report["scenarios"])


@pytest.mark.parametrize("scenario", ["SCN-8078-01", "SCN-8078-02", "SCN-8078-03", "SCN-8078-04"])
def test_governance_without_observation_refuses_to_pass(tmp_path, monkeypatch, scenario):
    passed, report = run(tmp_path, monkeypatch, scenario)
    assert not passed and all(row["status"] == "blocked" for row in report["scenarios"])


@pytest.mark.parametrize("field", ["provider_calls_after", "reservations_after", "retry_after_seconds", "status"])
def test_denied_tool_needs_no_provider_dispatch_or_extra_hold(field):
    observation = fixture()["governance_ac01"][FAMILIES[0]]
    observation["cap"][field] = {"provider_calls_after": 1, "reservations_after": 1,
                                 "retry_after_seconds": 0, "status": 200}[field]
    with pytest.raises(ValueError):
        governance_ac01(FAMILIES[0], observation)


@pytest.mark.parametrize("mutation", ["new_receipt", "over_cap", "unknown_released", "duplicate_dispatch"])
def test_replay_must_retain_one_original_hold(mutation):
    observation = fixture()["governance_ac02"]["replay"]
    if mutation == "new_receipt":
        observation["attempts"][1]["receipt_id"] = identity(40)
    elif mutation == "over_cap":
        observation["other_held_usd"] = "20"
    elif mutation == "unknown_released":
        observation["held_usd"] = "0"
    else:
        observation["provider_calls_after"] = 2
    with pytest.raises(ValueError):
        governance_ac02(observation)


@pytest.mark.parametrize("mutation", ["foreign_access", "settled_estimate", "private_payload", "wrong_owner"])
def test_activity_rejects_foreign_or_unsettled_misattribution(mutation):
    observation = fixture()["governance_ac03"]["activity"]
    if mutation == "foreign_access":
        observation["foreign_status"] = 200
    elif mutation == "settled_estimate":
        observation["owned"]["settled_cost_usd"] = observation["export"]["settled_cost_usd"] = "0"
    elif mutation == "private_payload":
        observation["export"]["secret"] = "private"
    else:
        observation["export"]["tenant_id"] = "tenant-b"
    with pytest.raises(ValueError):
        governance_ac03(observation)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown", "no_settlement", "no_cleanup",
                                     "unpriced", "over_cap", "over_estimate", "over_settlement", "wrong_result"])
def test_cross_family_accounting_refuses_incomplete_rows(mutation):
    observation = fixture()["governance_ac04"][CLIENTS[0] + ":browser"]
    operation = observation["operations"][0]
    if mutation == "missing":
        observation["operations"] = []
    elif mutation == "duplicate":
        observation["operations"].append(dict(operation))
    elif mutation == "unknown":
        operation["accounting_status"] = "unknown"
    elif mutation == "no_settlement":
        operation["settled_cost_usd"] = None
    elif mutation == "no_cleanup":
        operation["cleanup_status"] = "unknown"
    elif mutation == "unpriced":
        observation["pricing_approved"] = False
    elif mutation == "over_cap":
        observation["run_cap_usd"] = "21"
    elif mutation == "over_estimate":
        operation["estimated_cost_usd"] = "21"
    elif mutation == "over_settlement":
        operation["settled_cost_usd"] = "21"
    else:
        operation["result_status"] = "unknown"
    with pytest.raises(ValueError):
        governance_ac04("browser", observation)


def test_runner_marks_missing_and_unknown_rows_failed_without_live_attestation(tmp_path, monkeypatch):
    observations = fixture()
    observations["governance_ac04"][CLIENTS[0] + ":browser"]["operations"][0]["settled_cost_usd"] = None
    passed, report = run(tmp_path, monkeypatch, "SCN-8078-04", observations)
    assert not passed and not report["live_attestation"]
    assert len([row for row in report["scenarios"] if row["status"] == "fail"]) == 1
    assert report["scenarios"][2]["status"] == "fail"


@pytest.mark.parametrize("mutation", ["missing_export", "foreign_tenant", "wrong_receipt", "wrong_execution",
                                     "unknown_settlement", "private_export", "wrong_call_id", "missing_provider", "wrong_binding",
                                     "wrong_provider_receipt", "wrong_source", "unknown_result", "pending_cleanup", "late_cleanup"])
def test_operation_link_rejects_foreign_unknown_or_unmatched_readbacks(tmp_path, monkeypatch, mutation):
    observations = fixture()
    observer = OfflineObserver(observations)
    client, tool = CLIENTS[0], "browser"
    operation = observations["governance_ac04"][client + ":" + tool]["operations"][0]
    export_key = (client, tool, operation["receipt_id"])
    provider_key = (client, operation["tool_name"], operation["operation_ref"], operation["execution_id"])
    exported = observer.exports[export_key]
    provider = observer.providers[provider_key]
    if mutation == "missing_export":
        observer.exports.pop(export_key)
    elif mutation == "foreign_tenant":
        exported["tenant_id"] = "tenant-b"
    elif mutation == "wrong_receipt":
        exported["receipt_id"] = identity(77)
    elif mutation == "wrong_execution":
        exported["execution_id"] = identity(77)
    elif mutation == "unknown_settlement":
        exported["accounting_status"] = "estimate_only"
        exported["settled_cost_usd"] = None
    elif mutation == "private_export":
        exported["secret"] = "private-result-sentinel"
    elif mutation == "wrong_call_id":
        exported["call_id"] = identity(77)
    elif mutation == "missing_provider":
        observer.providers.pop(provider_key)
    elif mutation == "wrong_binding":
        provider["operation_ref"] = identity(77)
    elif mutation == "wrong_provider_receipt":
        provider["facts"]["correlation"]["provider_receipt_id"] = identity(77)
    elif mutation == "wrong_source":
        provider["facts"]["correlation"]["source"] = "fixture-claim"
    elif mutation == "unknown_result":
        provider["facts"]["durable_outcome"]["status"] = "pending"
    elif mutation == "pending_cleanup":
        provider["facts"]["termination"]["status"] = "pending"
    else:
        provider["facts"]["termination"]["cleanup_elapsed_seconds"] = 601
    passed, report = run(tmp_path, monkeypatch, "SCN-8078-04", observations, observer=observer)
    assert not passed and not report["live_attestation"]
    assert sum(row["status"] == "fail" for row in report["scenarios"]) == 1
    assert "private-result-sentinel" not in json.dumps(report)


def test_result_and_cleanup_link_to_export_for_every_client_tool(tmp_path, monkeypatch):
    observations = fixture()
    observer = OfflineObserver(observations)
    passed, report = run(tmp_path, monkeypatch, "SCN-8078-04", observations, observer=observer)
    assert passed and not report["live_attestation"]
    assert len(observer.calls) == 2 * len(CLIENTS) * 3
    for row in report["scenarios"]:
        operation = observations["governance_ac04"][row["cell_id"]]["operations"][0]
        assert row["observations"]["linked_operations"] == [{
            "request_id": operation["request_id"], "receipt_id": operation["receipt_id"],
            "execution_id": operation["execution_id"], "result_status": operation["result_status"],
            "cleanup_status": operation["cleanup_status"], "cost_status": "settled"}]
        assert "provider" not in row["observations"] and "export" not in row["observations"]


def test_unbound_or_live_observer_cannot_read_provider(tmp_path, monkeypatch):
    observations = fixture()
    observer = OfflineObserver(observations)
    observer.isolated = False
    passed, report = run(tmp_path, monkeypatch, "SCN-8078-04", observations, observer=observer)
    assert not passed and all(row["status"] == "fail" for row in report["scenarios"])
    assert observer.calls == []
    observer.isolated = True
    passed, report = run(tmp_path, monkeypatch, "SCN-8078-04", observations, live=True, observer=observer)
    assert not passed and all(row["status"] == "blocked" for row in report["scenarios"])
    assert observer.calls == []
