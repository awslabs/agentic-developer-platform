"""HTTP collector -> real unlink readback -> original acceptance composition."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import qualification_well_architected_acceptance as acceptance
import qualification_well_architected_clients as clients
import qualification_well_architected_observations as observations
import qualification_well_architected_revocation as revocation
from qualification_installed_artifacts import case_identity
from test_wa_source_revocation import fixture, staged


@pytest.fixture
def live_case(staged):
    case, native, prior, proof, baseline_at = deepcopy(staged)
    case["qualification_clients"] = {"design_task_id": "later-task", "denied_token_env": "DENIED_TOKEN"}
    native["sources"] = case["sources"]
    now = datetime.now(UTC)
    actor = prior["qualification_observations"]["reader"]
    result = native["result"]
    result.update(scope=case["scope"], recommendations=[])
    manifest = native["source_manifest"]
    manifest.update(scope=case["scope"], run_id=case["run_id"], sources=case["sources"],
                    source_snapshot_ids=["actual-snapshot"], architecture={"files": []})
    payloads = {"content": b"original retained source", "manifest": b'{"files":["source"]}'}
    receipt = {"snapshot_id": "actual-snapshot", "source_ref": case["sources"][0],
               "expires_at": (now + timedelta(hours=1)).isoformat()}
    for part, field in (("content", "content"), ("manifest", "file_manifest")):
        receipt[field] = {"artifact_id": "products/source-snapshots/" + part, "version": "original-version",
                          "sha256": sha256(payloads[part]).hexdigest(), "bytes": len(payloads[part])}
    origin = {"run_id": case["run_id"], "trigger_id": "manual", "input": native["run_input"],
              "admission": {"admitted_at": (now - timedelta(hours=2)).isoformat()}}
    task = {"task_id": "later-task", "invocation_id": "later-invocation", "status": "completed", "version": 1,
            "created_at": (now - timedelta(hours=1)).isoformat(), "product_id": "product", "project_id": "project"}
    citation = "well_architected_result:selected-result@1"
    reference = {"tenant_id": "tenant", "project_id": "project", "source_connection_id": "l5-well-architected",
                 "record_type": "well_architected_result", "record_id": case["result_id"], "record_revision": "1"}
    consumers = [{"destination": destination, "publication_id": destination, "state": "published",
                  "receipt_id": destination + "-ack", "consumer_revision": "1", "projection_verified_on_ack": True}
                 for destination in ("product-context", "engineering-insights")]
    usage = {"result": result, "citation": citation}
    for destination, name in (("product-context", "retrieval_receipt"), ("engineering-insights", "usage_receipt")):
        usage[name] = {"receipt_id": name, "actor_id": "owner", "task_id": "later-task", "citation": citation,
                       "contribution_id": destination, "result_ref": reference}
    usage["usage_receipt"]["retrieval_receipt_id"] = "retrieval_receipt"
    retained = prior["qualification_observations"]["items"][0]
    selection = retained["receipt"]["selection"]
    frozen = selection["source_revocation"]["result"]
    frozen.update(result_digest=observations.digest(result), source_manifest_digest=observations.digest(manifest))
    selection["run_admission_digest"] = observations.digest(origin["admission"])
    selected_digest = observations.digest(selection)
    retained["receipt"]["selection_digest"] = selected_digest
    retained["observation"]["selection_digest"] = selected_digest
    retained["receipt_sha256"] = observations.digest(retained["receipt"])
    retained["observation_sha256"] = observations.digest(retained["observation"])
    case["qualification_experiments"][0]["selection_digest"] = selected_digest
    proof.update(result=frozen, selection_digest=selected_digest)
    state = {"revoked": False, "fault": None}
    calls = []
    base = "/api/v1/products/product/projects/project/services/well_architected"
    result_path = base + "/results/selected-result"
    run_path = base + "/runs/selected-run"
    snapshot_path = run_path + "/source-snapshots/actual-snapshot"

    def connection(selected):
        def respond(request):
            assert request.method == "GET"  # the entire observation is read-only
            path = request.url.path
            foreign = selected["token_env"] == "DENIED_TOKEN"
            calls.append((foreign, path))
            if path == "/api/auth/me":
                user = "foreign" if foreign and state["fault"] != "same-actor" else "owner"
                return httpx.Response(200, json={**actor, "user_id": user})
            if path == base + "/qualification-experiments/selected-experiment":
                return httpx.Response(200, json=retained["receipt"])
            if path == base + "/qualification-experiments/selected-experiment/source-revocation-observations/unlink-operation":
                return httpx.Response(200, json=proof)
            if foreign or state["revoked"]:
                if state["fault"] == "query-leak" and path.endswith("/findings"):
                    return httpx.Response(404, json={"detail": {"code": "not_found"}, "private_count": 3})
                if state["fault"] == "artifact-leak" and path.endswith("/content"):
                    return httpx.Response(200, content=payloads["content"])
                return httpx.Response(404, json={"detail": {"code": "not_found"}})
            if path == "/api/v1/tasks/later-task":
                return httpx.Response(200, json=task)
            if path == run_path:
                return httpx.Response(200, json=origin)
            if path == result_path:
                return httpx.Response(200, json=result)
            if path == result_path + "/publication-status":
                return httpx.Response(200, json={"result_id": result["result_id"], "revision": 1, "consumers": consumers})
            if path == result_path + "/design-usage/later-task":
                return httpx.Response(200, json=usage)
            if path == result_path + "/findings":
                return httpx.Response(200, json={"items": []})
            if path == run_path + "/source-manifest":
                return httpx.Response(200, json=manifest)
            if path == snapshot_path:
                changed = deepcopy(receipt)
                if state["fault"] == "unpinned-artifact":
                    changed["content"]["version"] = "latest"
                if state["fault"] == "foreign-snapshot":
                    changed["source_ref"] = {"record_id": "other"}
                return httpx.Response(200, json=changed)
            if path.startswith(snapshot_path + "/"):
                return httpx.Response(200, content=b"changed" if state["fault"] == "changed-bytes" else payloads[path.rsplit("/", 1)[1]])
            pytest.fail("unselected request: " + path)
        return httpx.Client(base_url=case["gateway_url"], transport=httpx.MockTransport(respond))
    return case, native, prior, proof, baseline_at, connection, state, calls


def collect(live_case):
    case, native, prior, _, baseline_at, connection, state, calls = live_case
    prior["clients"] = clients.collect(case, connection, read_only=True)
    state["revoked"] = True
    current = revocation.attach(prior, revocation.collect(case, native, prior, "unlink-operation", baseline_at, connection))
    return case, native, current


def evaluate(native, current):
    installed = {"source": "independent-installed-read", "source_revision": "a" * 40,
                 "case_identities": [case_identity(native)] * 2, "snapshot_sha256": [str(index) * 64 for index in (1, 2, 3)]}
    return acceptance.evaluate([native, native], [current, current], {"source_revision": "a" * 40}, installed=installed)


def test_successful_owner_bytes_then_foreign_and_revoked_denials_map_only_exact_reuse_criterion(live_case):
    _, native, current = collect(live_case)
    report = evaluate(native, current)
    assert {row["id"] for row in report["story_criteria"] if row["outcome"] == "passed"} == {"WA05/AC-03"}
    row = next(row for row in report["story_criteria"] if row["id"] == "WA05/AC-03")
    assert row["observation"] == "independent-live" and row["evidence_ref"] == "native-observations.json"
    assert next(row for row in report["scenarios"] if row["id"] == "WA05/SCN-03")["outcome"] == "passed"
    assert next(row for row in report["live_checks"] if row["id"] == "context-reuse")["outcome"] == "incomplete"
    assert report["outcome"] == "incomplete" and report["reuse_progress"][0]["provider_effect_count"] is None
    paths = current["clients"]["denial"]["denied_paths"]
    assert len(paths) == 6 and any(path.endswith("/content") for path in paths)
    assert not any("/artifacts/" in path for _, path in live_case[-1])


@pytest.mark.parametrize("fault", ["same-actor", "query-leak", "artifact-leak", "unpinned-artifact", "foreign-snapshot", "changed-bytes"])
def test_owner_denial_baseline_and_foreign_checks_refuse_unproven_or_leaking_surfaces(live_case, fault):
    live_case[-2]["fault"] = fault
    with pytest.raises(ValueError):
        collect(live_case)


@pytest.mark.parametrize("fault", ["query-leak", "artifact-leak"])
def test_real_unlink_is_insufficient_when_revoked_reader_can_still_read(live_case, fault):
    case, native, prior, _, baseline_at, connection, state, _ = live_case
    prior["clients"] = clients.collect(case, connection, read_only=True)
    state.update(revoked=True, fault=fault)
    with pytest.raises(ValueError):
        revocation.collect(case, native, prior, "unlink-operation", baseline_at, connection)


@pytest.mark.parametrize("fault", ["missing-query", "missing-artifact", "foreign-result", "foreign-publication", "foreign-source", "same-reader",
                                   "task-origin", "early-task", "receipt-link", "proof-hash", "provider-count"])
def test_composition_rechecks_exact_sources_readers_tasks_receipts_and_native_unknown(live_case, fault):
    _, native, current = collect(live_case)
    evidence = current["clients"]
    if fault in {"missing-query", "missing-artifact"}:
        suffix = "/findings" if fault == "missing-query" else "/content"
        evidence["source_revocation"]["denied_paths"] = [path for path in evidence["source_revocation"]["denied_paths"] if not path.endswith(suffix)]
    elif fault == "foreign-result":
        evidence["result_id"] = "other"
    elif fault == "foreign-publication":
        evidence["publication"]["result_id"] = "other"
    elif fault == "foreign-source":
        evidence["denial_baseline"]["manifest"]["sources"] = [{"record_id": "other"}]
    elif fault == "same-reader":
        evidence["denial"]["reader"] = evidence["usage"]["actor"]
    elif fault == "task-origin":
        evidence["usage"]["origin"]["admission"]["task_id"] = "later-task"
    elif fault == "early-task":
        evidence["usage"]["task"]["created_at"] = evidence["usage"]["origin"]["admission"]["admitted_at"]
    elif fault == "receipt-link":
        evidence["usage"]["usage_receipt"]["retrieval_receipt_id"] = "other"
    elif fault == "proof-hash":
        evidence["source_revocation"]["proof_sha256"] = "a" * 64
    else:
        evidence["usage"]["provider_effect_count"] = 0
    with pytest.raises(ValueError):
        evaluate(native, current)


def test_legacy_or_missing_artifact_evidence_stays_actionably_incomplete(live_case):
    _, native, current = collect(live_case)
    current["clients"].pop("denial_baseline")
    report = evaluate(native, current)
    row = next(row for row in report["story_criteria"] if row["id"] == "WA05/AC-03")
    assert row["outcome"] == "incomplete"
    assert "successful owner result/query and pinned source artifact reads" in report["reuse_progress"][0]["required_evidence"]
