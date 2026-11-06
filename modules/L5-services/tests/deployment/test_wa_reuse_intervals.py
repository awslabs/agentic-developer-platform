"""Actual interval facts compose with exact reuse without completing fault cases."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import qualification_well_architected_acceptance as acceptance
import qualification_well_architected_reuse_intervals as intervals
from qualification_installed_artifacts import case_identity
from test_wa_reuse_acceptance import collect as collect_reuse, live_case, fixture, staged  # noqa: F401


@pytest.fixture
def collected(live_case):
    _, native, observed = collect_reuse(live_case)
    native.update(binding_id="original-profile-binding", provider_identity={"account_id": "123456789012", "region": "us-east-1", "role_arn": "original-role"})
    clients = observed["clients"]
    usage = clients["usage"]
    end = intervals.instant(clients["source_revocation"]["baseline_at"]) - timedelta(seconds=10)
    start = end - timedelta(minutes=10)
    when = end - timedelta(seconds=30)
    scope = {**native["result"]["scope"], "actor_id": usage["actor_id"], "result_scope": native["result"]["scope"],
             "run_id": native["run_id"], "result_id": native["result"]["result_id"], "result_revision": native["result_revision"],
             "result_digest": intervals.digest(native["result"]), "binding_id": native["binding_id"],
             "source_refs": native["sources"], "provider_identity": native["provider_identity"],
             "native_job_ref": native["native_job_ref"], "run_input_digest": intervals.digest(native["run_input"]),
             "run_admission_digest": intervals.digest(usage["origin"]["admission"]),
             "binding_state": "ready", "cleanup": {"state": "ready", "creation_receipt_digest": "c" * 64},
             "publications": [{"destination": item["destination"], "publication_id": item["publication_id"], "contribution_hash": "c" * 64}
                              for item in clients["publication"]["consumers"]]}
    selected = {"result_id": native["result"]["result_id"], "result_revision": native["result_revision"], "task_id": usage["task_id"],
                "context_retry_key": "original_context_retry", "insights_retry_key": "original_insights_retry"}
    runtime = {"runs": [{"run_id": native["run_id"], "state": "succeeded"}], "work": [], "transport": [], "replays": [], "usage": None}
    first = {"contract": "wa-reuse-checkpoint/v1", "source": "authenticated-reuse-readback", "scope": scope, "selection": selected,
             "read_started_at": start.isoformat(), "observed_at": start.isoformat(), "runtime": runtime, "execution_authorized": False}
    last = deepcopy(first)
    last.update(read_started_at=end.isoformat(), observed_at=end.isoformat())
    for peer in clients["publication"]["consumers"]:
        field = "context_retry_key" if peer["destination"] == "product-context" else "insights_retry_key"
        last["runtime"]["replays"].append({"destination": peer["destination"], "idempotency_key": selected[field],
            "publication_id": peer["publication_id"], "request_hash": "a" * 64,
            "response": {"publication_id": peer["publication_id"], "result_id": scope["result_id"], "state": "published",
                         "contribution_hash": "c" * 64, "invalidation_watermark": 0, "receipt_id": peer["receipt_id"],
                         "consumer_revision": peer["consumer_revision"]}})
    last["runtime"]["usage"] = {key: usage[key] for key in ("task_id", "actor_id", "retrieval_receipt", "usage_receipt")}
    last["runtime"]["usage"].update(result_id=scope["result_id"], publication_id="product-context", observed_at=when.isoformat())
    document = {"contract": "wa-reuse-interval/v1", "execution_authorized": False, "native_qualified": False,
                "native_audit": {"source": "independent-cloudtrail-read", "start": start.isoformat(), "end": end.isoformat(),
                                 "events": [], "pagination_complete": True, "delivery_complete": False}}
    for name, value in (("before", first), ("after", last)):
        document[name] = {"checkpoint_id": name, "snapshot": value, "snapshot_sha256": intervals.digest(value),
                          "execution_authorized": False, "native_qualified": False}
    installed = {"source": "independent-installed-read", "source_revision": "a" * 40,
                 "case_identities": [case_identity(native)] * 2, "snapshot_sha256": [str(i) * 64 for i in (1, 2, 3)],
                 "observation_interval": {"start": start.isoformat(), "end": end.isoformat()}}
    observed["reuse_interval"] = document
    return native, observed, installed


def rehash(document):
    for name in ("before", "after"):
        document[name]["snapshot_sha256"] = intervals.digest(document[name]["snapshot"])


def evaluate(collected):
    case, observed, installed = collected
    return intervals.evaluate(observed["reuse_interval"], case, observed["clients"], {"source_revision": "a" * 40}, installed)


def test_collected_replays_and_later_use_establish_only_scoped_no_new_start(collected):
    result = evaluate(collected)
    assert result["outcome"] == "passed", result
    assert result["adp_start_count"] == 0 and result["provider_effect_count"] is None
    assert result["accountwide_native_zero_proved"] is False
    assert "publication_fault_history_not_proved" in result["limitations"]


def test_composition_advances_context_reuse_but_keeps_original_fault_and_wa07_checks(collected):
    case, observed, installed = collected
    report = acceptance.evaluate([case, case], [observed, observed], {"source_revision": "a" * 40}, installed=installed)
    assert len(report["live_checks"]) == 15
    assert next(row for row in report["live_checks"] if row["id"] == "context-reuse")["outcome"] == "passed"
    for identifier in ("WA05/AC-02", "WA07/AC-04", "WA07/AC-05"):
        assert next(row for row in report["story_criteria"] if row["id"] == identifier)["outcome"] == "incomplete"
    assert report["outcome"] == "incomplete"


@pytest.mark.parametrize("fault", ["uninstalled", "outside-install", "audit-missing", "audit-start", "new-send", "new-run",
                                   "old-usage", "old-replay", "wrong-task", "wrong-receipt", "wrong-destination", "partial-replay",
                                   "wrong-replay-key", "missing-request-hash", "usage-after-end", "wrong-admission", "unmatched-send"])
def test_unknown_or_mismatched_evidence_cannot_manufacture_zero(collected, fault):
    _, observed, installed = collected
    document = observed["reuse_interval"]
    first, last = (document[name]["snapshot"] for name in ("before", "after"))
    before, after = first["runtime"], last["runtime"]
    if fault == "uninstalled":
        installed["source_revision"] = "b" * 40
    elif fault == "outside-install":
        installed["observation_interval"]["start"] = last["observed_at"]
    elif fault == "audit-missing":
        document["native_audit"] = {"source": "unavailable"}
    elif fault == "audit-start":
        document["native_audit"]["events"] = [{"event_id": "actual-start"}]
    elif fault in {"new-send", "unmatched-send"}:
        row = {"observation_id": "unmatched", "send_id": "unknown", "phase": "before_send", "run_id": "new-run",
               "attempt_id": "attempt", "observed_at": first["observed_at"], "facts": {"contract": "wa-start-transport/v1"}}
        after["transport"].append(row)
        if fault == "unmatched-send":
            before["transport"].append(row)
    elif fault == "new-run":
        after["runs"].append({"run_id": "new-run"})
    elif fault == "old-usage":
        before["usage"] = deepcopy(after["usage"])
    elif fault == "old-replay":
        before["replays"] = deepcopy(after["replays"])
    elif fault == "wrong-task":
        after["usage"]["task_id"] = "foreign-task"
    elif fault == "wrong-receipt":
        after["usage"]["usage_receipt"] = {"receipt_id": "wrong"}
    elif fault == "wrong-destination":
        after["replays"][0]["response"]["publication_id"] = "foreign-publication"
    elif fault == "partial-replay":
        after["replays"].pop()
    elif fault == "wrong-replay-key":
        after["replays"][0]["idempotency_key"] = "substituted_retry_key"
    elif fault == "missing-request-hash":
        after["replays"][0]["request_hash"] = ""
    elif fault == "usage-after-end":
        after["usage"]["observed_at"] = (intervals.instant(last["read_started_at"]) + timedelta(seconds=1)).isoformat()
    else:
        for value in (first, last):
            value["scope"]["run_admission_digest"] = "f" * 64
    rehash(document)
    result = evaluate(collected)
    assert result["outcome"] == "incomplete" and result["adp_start_count"] is None


@pytest.mark.parametrize("fault", ["digest", "scope", "duplicate-row", "history-erased", "native-id", "contract"])
def test_changed_original_evidence_is_rejected(collected, fault):
    document = collected[1]["reuse_interval"]
    first, last = (document[name]["snapshot"] for name in ("before", "after"))
    if fault == "digest":
        document["before"]["snapshot_sha256"] = "f" * 64
    else:
        if fault == "scope":
            last["scope"]["actor_id"] = "another-owner"
        elif fault == "duplicate-row":
            last["runtime"]["runs"] *= 2
        elif fault == "history-erased":
            first["runtime"]["transport"] = [{"observation_id": "original"}]
        elif fault == "native-id":
            last["scope"]["native_job_ref"] = {"generation_id": "another-generation"}
        else:
            last["contract"] = "wa-periodic-checkpoint/v1"
        rehash(document)
    with pytest.raises(ValueError):
        evaluate(collected)


def test_collector_reads_exact_ids_without_any_replay_or_usage_mutation(collected):
    case, observed, _ = collected
    requests = []
    selection = {"scope": case["result"]["scope"], "qualification_reuse_interval": {"before_id": "before", "after_id": "after"}}

    def connect(_):
        def respond(request):
            requests.append(request)
            assert request.method == "POST" and request.url.path.endswith("/qualification-experiments/reuse-intervals/read")
            return httpx.Response(200, json=observed["reuse_interval"])
        return httpx.Client(base_url="https://example.invalid", transport=httpx.MockTransport(respond))

    assert intervals.collect(selection, case, connect) == observed["reuse_interval"]
    assert len(requests) == 1
    selection["qualification_reuse_interval"]["provider_effect_count"] = 0
    with pytest.raises(ValueError):
        intervals.collect(selection, case, connect)


@pytest.mark.parametrize("change", [None, "native", "context", "interval", "source-proof"])
def test_staged_interval_artifact_binds_original_release_and_survives_only_matching_source_proof(collected, tmp_path, monkeypatch, change):
    import json
    from types import SimpleNamespace
    import qualification_well_architected_release as release

    case, observed, _ = collected
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    context = {"source_revision": "a" * 40, "run_id": 27, "run_attempt": 1}
    selection = {"cases": [{"run_id": case["run_id"]}, {"run_id": "second-run"}]}
    baseline = {"schema_version": 1, "context": context, "observed_at": observed["reuse_interval"]["before"]["snapshot"]["observed_at"],
                "qualification_scope": release.SCOPE, "provider_coverage": "provider_coverage_unverified",
                "limitations": release.LIMITATIONS, "cases": [case, case], "evaluations": [observed, observed]}
    baseline_path = directory / "native-observations.json"
    release.driver.write_private(baseline_path, baseline)
    selector_path = directory / "reuse-intervals.json"
    chosen = {case["run_id"]: {"before_id": "before", "after_id": "after"}}
    release.driver.write_private(selector_path, chosen)
    calls = []

    def observe(selections, native_cases, previous, selectors, driver):
        calls.append(selectors)
        assert selectors == chosen and native_cases == baseline["cases"] and previous == baseline["evaluations"]
        return deepcopy(baseline["evaluations"])

    monkeypatch.setattr(intervals, "observe", observe)
    options = SimpleNamespace(reuse_intervals=selector_path, source_revocation_operations=None)
    release.reuse_interval_observations(options, selection, directory, context)
    artifact_path = directory / "reuse-interval-observations.json"
    assert artifact_path.stat().st_mode & 0o077 == 0
    retained = json.loads(artifact_path.read_text())
    if change == "native":
        retained["native_observation_sha256"] = "f" * 64
    elif change == "context":
        retained["context"]["run_attempt"] = 2
    elif change == "interval":
        retained["evaluations"][0]["reuse_interval"]["native_audit"]["events"] = [{"event_id": "extra"}]
    artifact_path.write_text(json.dumps(retained))
    if change in {"native", "context", "interval"}:
        with pytest.raises(ValueError):
            release.reuse_interval_readback(selection, directory, context, baseline, baseline_path)
        return
    assert release.reuse_interval_readback(selection, directory, context, baseline, baseline_path)[1] == baseline["evaluations"]
    with pytest.raises(ValueError):
        release.reuse_interval_observations(options, selection, directory, context)
    if change == "source-proof":
        from hashlib import sha256
        source = {"schema_version": 1, "context": context, "native_observation_sha256": sha256(baseline_path.read_bytes()).hexdigest(),
                  "cleanup_observation_sha256": None, "reuse_observation_sha256": "f" * 64, "operations": {case["run_id"]: "unlink"}}
        release.driver.write_private(directory / "source-revocation-observations.json", source)
        with pytest.raises(ValueError, match="replay/use evidence changed"):
            release.source_revocation_readback(selection, directory, context, baseline, baseline_path)


def test_observe_enriches_only_later_task_facts_and_preserves_original_denials(collected, monkeypatch):
    from types import SimpleNamespace
    import qualification_well_architected_clients as clients
    import qualification_well_architected_cleanup as cleanup

    case, observed, _ = collected
    selection = {"run_id": case["run_id"], "qualification_clients": {"denied_token_env": "DENIED"}}
    second = {"run_id": "second-run"}
    monkeypatch.setattr(intervals, "collect", lambda *args: deepcopy(observed["reuse_interval"]))
    collected_clients = []

    def current(selected, connection, *, read_only):
        assert read_only is True and selected["qualification_clients"]["design_task_id"] == "later-task"
        assert "retained_reuse" not in selected["qualification_clients"]
        collected_clients.append(selected)
        return deepcopy(observed["clients"])

    monkeypatch.setattr(clients, "collect", current)
    monkeypatch.setattr(cleanup, "observe_case", lambda selected, native, previous, driver: deepcopy(previous))
    result = intervals.observe([selection, second], [case, case], [observed, observed],
                               {case["run_id"]: {"before_id": "before", "after_id": "after"}}, SimpleNamespace(product_connection=object()))
    assert len(collected_clients) == 1
    assert result[0]["clients"]["denial_baseline"] == observed["clients"]["denial_baseline"]
    assert result[0]["clients"]["denial"] == observed["clients"]["denial"]
