"""Staged same-owner denied reads need actual earlier frozen terminal evidence."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import qualification_well_architected_observations as observations
import qualification_well_architected_revocation as revocation
import test_wa_release_observations as observation_fixtures

fixture = observation_fixtures.fixture


@pytest.fixture
def staged(fixture):
    case, run, receipt, snapshot = deepcopy(fixture)
    now = datetime.now(UTC)
    baseline_at = now - timedelta(seconds=30)
    result = {"result_id": case["result_id"], "revision": case["result_revision"], "run_id": case["run_id"], "content": "private"}
    manifest = {"provider_source_uri": "s3://example.invalid/source", "archive": {"artifact_id": "source", "version": "pinned"}}
    settings = {"goal_refs": case["sources"]}
    case["project_kind"] = "non_adp"
    result.update(native_job_ref=run["native_job_ref"], coverage=[], manifest_ref=manifest["archive"])
    native = {"run_id": case["run_id"], "result_revision": case["result_revision"], "result": result, "native_job_ref": run["native_job_ref"],
              "run_input": run["input"], "source_manifest": manifest, "settings": settings, "project_kind": "non_adp",
              "architecture": case["sources"][0], "goal": case["sources"][0],
              "source_receipt": {"architecture": case["sources"][0], "native_job_ref": run["native_job_ref"], "sources": case["sources"],
                                 "files": [], "archive": manifest["archive"], "provider_source_uri": manifest["provider_source_uri"],
                                 "provider_coverage": "provider_coverage_unverified"}}
    frozen = {"binding_id": "source-binding", "generation": 1, "product_revision": 2, "source_ref": case["sources"][0],
              "result": {"result_id": case["result_id"], "revision": case["result_revision"], "result_digest": observations.digest(result),
                         "run_revision": case["run_revision"], "native_job_ref_digest": observations.digest(run["native_job_ref"])}}
    frozen["result"]["source_manifest_digest"] = observations.digest(manifest)
    frozen["review_settings_digest"] = observations.digest(settings)
    receipt["selection"]["source_revocation"] = frozen
    receipt["selection_digest"] = observations.digest(receipt["selection"])
    case["qualification_experiments"][0]["selection_digest"] = receipt["selection_digest"]
    snapshot["selection_digest"] = receipt["selection_digest"]
    actor = {"user_id": "owner", "org_id": "tenant", "account_type": "human"}
    prior = {"run_id": case["run_id"], "result_revision": case["result_revision"], "scope": case["scope"],
             "qualification_observations": {"source": "authenticated-product-api", "scope": case["scope"],
                "run_id": case["run_id"], "run_revision": case["run_revision"], "reader": actor, "execution_authorized": False,
                "items": [{"receipt": receipt, "observation": snapshot, "receipt_sha256": observations.digest(receipt),
                           "observation_sha256": observations.digest(snapshot)}]},
             "clients": {"usage": {"task_id": "completed-later-task"}, "denial": {"kind": "selected_reader", "status": 404}}}
    proof = {"schema_version": 1, "source": "durable-product-source-transition", "scope": case["scope"], "actor_id": "owner",
             "experiment_id": receipt["experiment_id"], "selection_digest": receipt["selection_digest"], "run_id": case["run_id"],
             "source_ref_digest": observations.digest(frozen["source_ref"]), "source_binding_id": frozen["binding_id"],
             "generation_before": 1, "generation_after": 2, "result": frozen["result"], "selected_at": receipt["created_at"],
             "unlinked_at": (now-timedelta(seconds=20)).isoformat(), "recorded_at": (now-timedelta(seconds=19)).isoformat(),
             "observed_at": now.isoformat(), "operation_id": "unlink-operation", "operation_kind": "unlinkBinding",
             "operation_state": "succeeded", "operation_digest": "a"*64, "change_id": "actual-change", "change_revision": 3,
             "change_digest": "b"*64, "current_source_access": "denied", "execution_authorized": False, "provider_effect_count": None}
    return case, native, prior, proof, baseline_at


def connection_for(staged, mutation=None):
    case, native, prior, proof, baseline_at = deepcopy(staged)
    receipt = prior["qualification_observations"]["items"][0]["receipt"]
    calls = []
    base = "/api/v1/products/product/projects/project/services/well_architected"
    path = base + "/qualification-experiments/selected-experiment"

    def connect(selection):
        assert selection == case
        def respond(request):
            assert request.method == "GET"
            target = request.url.path
            calls.append(target)
            if target == "/api/auth/me":
                return httpx.Response(200, json={"user_id": "other" if mutation == "actor" else "owner", "org_id": "tenant", "account_type": "human"})
            if target == path:
                value = deepcopy(receipt)
                if mutation == "revoked" or mutation == "late-revoked" and calls.count(target) > 1:
                    value["revoked_at"] = value["created_at"]
                return httpx.Response(200, json=value)
            if target == path + "/source-revocation-observations/unlink-operation":
                value = deepcopy(proof)
                if mutation == "late-proof" and calls.count(target) > 1:
                    value["change_digest"] = "c"*64
                for name, key, replacement in (("failed", "operation_state", "failed"), ("foreign-operation", "operation_id", "other"),
                                              ("wrong-source", "source_ref_digest", "c"*64), ("generation", "generation_after", 3),
                                              ("effect-count", "provider_effect_count", 0), ("authority", "execution_authorized", True)):
                    if mutation == name:
                        value[key] = replacement
                if mutation == "late-baseline":
                    value["unlinked_at"] = (baseline_at-timedelta(seconds=1)).isoformat()
                return httpx.Response(200, json=value)
            if target.startswith(base + "/results/selected-result") or target == base + "/runs/selected-run/source-manifest":
                if mutation == "content":
                    return httpx.Response(200, json={"content": "leak"})
                if mutation == "auth-failure":
                    return httpx.Response(401, json={"detail": {"code": "not_found"}})
                if mutation == "detailed-denial":
                    return httpx.Response(404, json={"detail": {"code": "not_found", "source": "private"}})
                return httpx.Response(404, json={"detail": {"code": "not_found"}})
            pytest.fail("unexpected request: " + target)
        return httpx.Client(base_url=case["gateway_url"], transport=httpx.MockTransport(respond))
    return connect, calls


def test_observes_real_provenance_and_query_denial_without_overwriting_foreign_reader(staged):
    connection, calls = connection_for(staged)
    case, native, prior, proof, baseline_at = staged
    observed = revocation.collect(case, native, prior, "unlink-operation", baseline_at, connection)
    assert observed["source_revocation_observed"] is True and observed["provider_effect_count"] is None
    assert len(observed["denied_paths"]) == 5 and len(calls) == 11
    assert "results/selected-result/findings" in observed["denied_paths"]
    attached = revocation.attach(prior, observed)
    assert attached["clients"]["denial"] == prior["clients"]["denial"]
    assert attached["clients"]["source_revocation"]["reader"]["user_id"] == "owner"


@pytest.mark.parametrize("mutation", ["actor", "revoked", "late-revoked", "late-proof", "failed", "foreign-operation", "wrong-source",
                                       "generation", "effect-count", "authority", "late-baseline", "content", "auth-failure", "detailed-denial"])
def test_changed_source_or_unauthenticated_denial_does_not_prove_revocation(staged, mutation):
    connection, _ = connection_for(staged, mutation)
    case, native, prior, _, baseline_at = staged
    with pytest.raises(ValueError):
        revocation.collect(case, native, prior, "unlink-operation", baseline_at, connection)


@pytest.mark.parametrize("change", ["result", "native-generation", "manifest", "input", "settings", "source-receipt", "receipt", "snapshot", "unselected"])
def test_prior_native_evidence_must_match_original_selection(staged, change):
    case, native, prior, _, baseline_at = deepcopy(staged)
    if change == "result":
        native["result"]["content"] = "changed"
    elif change == "native-generation":
        native["native_job_ref"] = {"generation_id": "changed"}
    elif change == "manifest":
        native["source_manifest"]["archive"] = {}
    elif change == "input":
        native["run_input"] = {}
    elif change == "settings":
        native["settings"] = {}
    elif change == "source-receipt":
        native["source_receipt"]["provider_source_uri"] = "s3://other.invalid/source"
    elif change == "receipt":
        prior["qualification_observations"]["items"][0]["receipt"]["revision"] += 1
    elif change == "snapshot":
        prior["qualification_observations"]["items"][0]["observation"]["run"]["state"] = "other"
    else:
        case["qualification_experiments"][0]["experiment_id"] = "other"
    with pytest.raises(ValueError):
        revocation.collect(case, native, prior, "unlink-operation", baseline_at, lambda case: pytest.fail("opened connection"))


def test_timestamps_can_refresh_but_durable_observations_cannot(staged):
    connection, _ = connection_for(staged)
    case, native, prior, _, baseline_at = staged
    first = revocation.attach(prior, revocation.collect(case, native, prior, "unlink-operation", baseline_at, connection))
    later = deepcopy(first)
    later["source_revocation"]["proof"]["observed_at"] = datetime.now(UTC).isoformat()
    later["source_revocation"]["proof_sha256"] = observations.digest(later["source_revocation"]["proof"])
    assert revocation.stable_evaluations([first]) == revocation.stable_evaluations([later])
    later["source_revocation"]["proof"]["change_digest"] = "f"*64
    assert revocation.stable_evaluations([first]) != revocation.stable_evaluations([later])


@pytest.mark.parametrize("change", ["foreign-run", "path", "duplicate-operation", "empty", "same-case-cleanup"])
def test_operation_map_is_exact_and_never_authorizes_cleanup_on_revoked_case(staged, change):
    case = deepcopy(staged[0])
    cases = [case, {**deepcopy(case), "run_id": "second-run"}]
    value = {case["run_id"]: "unlink-operation"}
    if change == "foreign-run":
        value = {"foreign": "unlink-operation"}
    elif change == "path":
        value[case["run_id"]] = "../foreign"
    elif change == "duplicate-operation":
        value["second-run"] = "unlink-operation"
    elif change == "empty":
        value = {}
    else:
        case["qualification_cleanup"] = {"owned_cleanup_confirmed": True}
    with pytest.raises(ValueError):
        revocation.selected_operations(value, cases)


def test_other_case_may_retain_owned_cleanup_without_weakening_source_proof(staged):
    case = staged[0]
    cases = [case, {**deepcopy(case), "run_id": "second-run", "qualification_cleanup": {"owned_cleanup_confirmed": True}}]
    value = {case["run_id"]: "unlink-operation"}
    assert revocation.selected_operations(value, cases) == value


def test_revocation_receipt_remains_partial_without_independent_provider_count(staged):
    import qualification_well_architected_acceptance as acceptance
    case, native, prior, _, baseline_at = staged
    connection, _ = connection_for(staged)
    current = revocation.attach(prior, revocation.collect(case, native, prior, "unlink-operation", baseline_at, connection))
    native["result"]["scope"] = case["scope"]
    current["clients"].update(publication={"consumers": []}, clients={"api": "digest"})
    report = acceptance.evaluate([native, native], [current, current], {"source_revision": "a"*40})
    assert report["client_progress"][0]["source_revocation_observed"] is True
    assert report["client_progress"][0]["provider_effect_count_observed"] is False
    assert next(row for row in report["live_checks"] if row["id"] == "context-reuse")["outcome"] == "incomplete"


@pytest.mark.parametrize("change", [None, "baseline", "context", "proof"])
def test_staged_artifact_binds_same_release_and_is_rechecked_before_emission(staged, tmp_path, monkeypatch, change):
    import json
    from types import SimpleNamespace
    import importlib.util
    spec = importlib.util.spec_from_file_location("wa_revocation_release_test", Path(__file__).resolve().parents[2] / "scripts/qualification_well_architected_release.py")
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)

    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    case, native, prior, _, baseline_at = deepcopy(staged)
    context = {"source_revision": "a"*40, "run_id": 27, "run_attempt": 1}
    baseline = {"schema_version": 1, "context": context, "observed_at": baseline_at.isoformat(),
                "qualification_scope": release.SCOPE, "provider_coverage": "provider_coverage_unverified",
                "limitations": release.LIMITATIONS, "cases": [native, native], "evaluations": [prior, prior]}
    baseline_path = directory / "native-observations.json"
    release.driver.write_private(baseline_path, baseline)
    operations_path = directory / "operations.json"
    release.driver.write_private(operations_path, {case["run_id"]: "unlink-operation"})
    selection = {"cases": [case, case]}
    collected = [revocation.attach(prior, {"proof": {"observed_at": datetime.now(UTC).isoformat(), "change_digest": "a"*64}, "proof_sha256": "b"*64})]*2
    calls = []
    def observe(cases, native_cases, previous, operations, observed_at, driver):
        calls.append((operations, observed_at))
        assert native_cases == baseline["cases"] and previous == baseline["evaluations"]
        return deepcopy(collected)
    monkeypatch.setattr(revocation, "observe", observe)
    release.source_revocation_observations(SimpleNamespace(source_revocation_operations=operations_path), selection, directory, context)
    artifact_path = directory / "source-revocation-observations.json"
    assert artifact_path.stat().st_mode & 0o077 == 0
    retained = json.loads(artifact_path.read_text())
    if change == "baseline":
        retained["native_observation_sha256"] = "f"*64
    elif change == "context":
        retained["context"]["run_attempt"] = 2
    elif change == "proof":
        retained["evaluations"][0]["source_revocation"]["proof"]["change_digest"] = "f"*64
    artifact_path.write_text(json.dumps(retained))
    if change:
        with pytest.raises(ValueError):
            release.source_revocation_readback(selection, directory, context, baseline, baseline_path)
    else:
        current_cases, evaluations = release.source_revocation_readback(selection, directory, context, baseline, baseline_path)
        assert current_cases == baseline["cases"] and evaluations == collected and len(calls) == 2
        with pytest.raises(ValueError):
            release.source_revocation_observations(SimpleNamespace(source_revocation_operations=operations_path), selection, directory, context)
