"""Installed observation collection stays scoped and cannot invent fault history."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import qualification_well_architected as driver
import qualification_well_architected_acceptance as acceptance
import qualification_well_architected_observations as observations


@pytest.fixture
def fixture():
    now = datetime.now(UTC)
    scope = {"tenant_id": "tenant", "product_id": "product", "project_id": "project"}
    case = {"gateway_url": "https://selected.example.invalid", "token_env": "OWNER_TOKEN", "scope": scope,
            "run_id": "selected-run", "run_revision": 2, "result_id": "selected-result", "result_revision": 1,
            "binding_id": "selected-binding", "configuration_revision": 1,
            "sources": [{"record_id": "selected-source", "record_revision": "pinned"}],
            "provider_identity": {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/example", "region": "us-east-1"}}
    run = {"run_id": case["run_id"], "scope": scope, "revision": 2, "state": "completed", "provider_state": "completed",
           "input": {"sources": case["sources"]}, "admission": {"configuration_hash": "a" * 64},
           "native_job_ref": {"generation_id": "selected-generation"}, "deadline_at": now.isoformat()}
    event = {"trigger_id": "schedule", "event_id": "selected-event"}
    choice = {"mode": "observations", "scope": {**scope, "created_by": "owner", "binding_id": case["binding_id"],
        "configuration_revision": 1, "provider_identity": case["provider_identity"], "sources": [{"record_type": "credential"}, *case["sources"]]},
        "targets": {"binding_id": case["binding_id"], "run_id": case["run_id"], "events": [event]},
        "run_input_digest": observations.digest(run["input"]), "run_admission_digest": observations.digest(run["admission"])}
    selected = {"experiment_id": "selected-experiment", "revision": 1, "selection_digest": observations.digest(choice)}
    case["qualification_experiments"] = [selected]
    receipt = {**selected, "mode": "observations", "selection": choice, "created_at": (now-timedelta(minutes=1)).isoformat(),
               "expires_at": (now+timedelta(hours=1)).isoformat(), "revoked_at": None, "active": True, "execution_authorized": False}
    observed = {"schema_version": 1, "source": "durable-runtime", "experiment_id": selected["experiment_id"],
        "selection_digest": selected["selection_digest"], "observed_at": now.isoformat(), "execution_authorized": False,
        "run": {key: run[key] for key in ("run_id", "revision", "state", "provider_state", "native_job_ref", "deadline_at")},
        "events": [{**event, "event_kind": "schedule", "configuration_revision": 1, "state": "admitted", "reason": None,
                    "run_id": case["run_id"], "received_at": now.isoformat(), "recovery_attempts": 0, "retry_at": None}],
        "holds": [{"artifact": {"artifact_id": "source-snapshot", "version": "pinned-version"}, "reason": "remote-uncertainty",
                   "required_until": now.isoformat(), "policy_expires_at": now.isoformat(), "revoked_at": None,
                   "released_at": None, "terminal_proof": None}], "limitations": list(observations.LIMITATIONS)}
    return case, run, receipt, observed


def transport(fixture, change=None):
    case, run, receipt, observed = deepcopy(fixture)
    calls = []
    base = "/api/v1/products/product/projects/project/services/well_architected"
    receipt_path = base + "/qualification-experiments/selected-experiment"

    def connection(selection):
        assert selection == case

        def respond(request):
            assert request.method == "GET"
            path = request.url.path
            calls.append(path)
            if path == "/api/auth/me":
                return httpx.Response(200, json={"user_id": "different" if change == "late-owner" and calls.count(path) > 1 else "owner",
                                                "org_id": "tenant", "account_type": "human"})
            if path == base + "/runs/selected-run":
                return httpx.Response(200, json={**run, **({"revision": 3} if change == "late-run" and calls.count(path) > 1 else {})})
            if path == receipt_path:
                value = deepcopy(receipt)
                if change == "revoked" or change == "late-revoke" and calls.count(path) > 1:
                    value["revoked_at"] = value["created_at"]
                if change == "expired":
                    value["expires_at"] = value["created_at"]
                if change == "scope":
                    value["selection"]["scope"]["project_id"] = "foreign"
                if change == "execution-authority":
                    value["execution_authorized"] = True
                if change == "selected-source":
                    value["selection"]["scope"]["sources"][1]["record_revision"] = "changed"
                return httpx.Response(200, json=value)
            if path == receipt_path + "/observations":
                value = deepcopy(observed)
                if change == "extra-event":
                    value["events"].append({**value["events"][0], "event_id": "foreign"})
                if change == "wrong-event":
                    value["events"][0]["event_id"] = "foreign"
                if change == "generation":
                    value["run"]["native_job_ref"]["generation_id"] = "other"
                if change == "omitted-limitations":
                    value["limitations"] = []
                if change == "old-observation":
                    value["observed_at"] = (datetime.now(UTC)-timedelta(hours=1)).isoformat()
                if change == "duplicate-hold":
                    value["holds"].append(value["holds"][0])
                return httpx.Response(200, json=value)
            pytest.fail("unselected request: " + path)

        return httpx.Client(base_url=case["gateway_url"], transport=httpx.MockTransport(respond))

    return case, connection, calls


def test_collects_existing_exact_receipts_with_only_first_party_gets(fixture):
    case, connection, calls = transport(fixture)
    result = observations.collect(case, connection)
    assert len(calls) == 7
    assert result["execution_authorized"] is False
    assert result["items"][0]["receipt"] == fixture[2]
    assert result["items"][0]["observation"] == fixture[3]
    assert result["items"][0]["observation_sha256"] == observations.digest(fixture[3])
    assert result["limitations"] == list(observations.LIMITATIONS)


@pytest.mark.parametrize("change", ["revoked", "expired", "scope", "selected-source", "execution-authority", "late-revoke", "late-owner",
                                    "late-run", "extra-event", "wrong-event", "generation", "omitted-limitations", "old-observation", "duplicate-hold"])
def test_changed_or_unselected_observations_fail_closed(fixture, change):
    case, connection, _ = transport(fixture, change)
    with pytest.raises(ValueError):
        observations.collect(case, connection)


@pytest.mark.parametrize("change", ["empty", "duplicate", "unbounded", "path", "revision", "extra-field"])
def test_invalid_selection_fails_before_opening_connection(fixture, change):
    case = deepcopy(fixture[0])
    selected = case["qualification_experiments"]
    if change == "empty":
        selected.clear()
    elif change == "duplicate":
        selected.append(selected[0])
    elif change == "unbounded":
        selected.extend(selected * 10)
    elif change == "path":
        selected[0]["experiment_id"] = "../foreign"
    elif change == "revision":
        selected[0]["revision"] = True
    else:
        selected[0]["url"] = "https://foreign.invalid"
    with pytest.raises(ValueError):
        observations.collect(case, lambda selected: pytest.fail("connection opened before selection validated"))


def test_release_collects_selected_receipts_without_promoting_snapshot_to_scenario(fixture, monkeypatch):
    case, connection, _ = transport(fixture)
    native = {"run_id": case["run_id"], "result_revision": case["result_revision"], "result": {"scope": case["scope"]}}
    class Session:
        observation = {"runtime": {"attempts": []}}

    def qualify(selected, installed, *, session_factory, **kwargs):
        session_factory(selected[0])
        return [native]

    monkeypatch.setattr(driver, "ProductNativeSession", lambda case: Session())
    monkeypatch.setattr(driver, "qualify_cases", qualify)
    monkeypatch.setattr(driver, "product_connection", connection)
    cases, collected = driver.observe_release({"cases": [case]}, object(), read_only=True)
    assert collected[0]["qualification_observations"]["items"][0]["observation"] == fixture[3]
    report = acceptance.evaluate(cases * 2, collected * 2, {"source_revision": "a" * 40})
    for record in report["live_checks"]:
        if record["id"] in {"source-retention", "periodic-admission", "uncertain-generation-recovery"}:
            assert record["outcome"] == "incomplete"
