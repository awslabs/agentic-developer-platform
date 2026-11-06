"""Synthetic client receipts cannot establish installed reuse acceptance."""

import json
from types import SimpleNamespace

import pytest

from l5_services.installed_release_probe import ReleaseProofError
from l5_services.wa_reuse_evidence import verify_wa_reuse
from test_wa07_safety_evidence import safety_case


SCOPE = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
OPERATIONS = ["configure", "check", "run", "read_result", "publication_status"]


def fixture():
    return SimpleNamespace(source_revision="a" * 40, gateway_image_digest="b" * 64,
                           frontend_asset_sha256="c" * 64, cli_manifest_sha256="d" * 64)


def reuse_case():
    result = {"source": "installed-api", "evidence_ref": "private/api-result", "scope": SCOPE.copy(),
              "result_id": "result-example", "revision": 2, "run_id": "run-example", "sha256": "b" * 64}
    reference = {"tenant_id": SCOPE["tenant_id"], "project_id": SCOPE["project_id"],
                 "source_connection_id": "l5-well-architected", "record_type": "well_architected_result",
                 "record_id": result["result_id"], "record_revision": "2"}
    installed = fixture()
    digests = {"api": installed.gateway_image_digest, "frontend": installed.frontend_asset_sha256,
               "cli": installed.cli_manifest_sha256, "mcp": "e" * 64}
    clients = [{"client": name, "source": "installed-client", "evidence_ref": f"private/{name}",
                "source_revision": installed.source_revision, "artifact_sha256": digest,
                "status": 200, "journey": OPERATIONS.copy(), "run_id": result["run_id"],
                "result_ref": reference.copy(), "result_sha256": result["sha256"]}
               for name, digest in digests.items()]
    publications = [{"destination": destination, "source": "installed-consumer",
                     "evidence_ref": f"private/{destination}", "state": "published", "scope": SCOPE.copy(),
                     "schema_version": "1.0", "record_refs": [reference.copy()],
                     "projection_verified_on_ack": True, "contribution_hash": "f" * 64,
                     "publication_id": f"publication-{destination}", "receipt_id": f"ack-{destination}",
                     "consumer_revision": "1"}
                    for destination in ("product-context", "engineering-insights")]
    citation = "well_architected_result:result-example@2"
    task_id, actor_id = "later-task", "design-owner"
    retrieval = {"receipt_id": "retrieval-example", "task_id": task_id, "actor_id": actor_id,
                 "result_ref": reference.copy(), "citation": citation,
                 "contribution_id": publications[0]["publication_id"]}
    usage = {"receipt_id": "usage-example", "task_id": task_id, "actor_id": actor_id,
             "result_ref": reference.copy(), "citation": citation,
             "contribution_id": publications[1]["publication_id"],
             "retrieval_receipt_id": retrieval["receipt_id"]}
    denials = [{"kind": "foreign_actor", "source": "installed-client", "evidence_ref": "private/foreign",
                "actor_id": "foreign-actor", "request_scope": {**SCOPE, "project_id": "foreign-project"},
                "result_ref": reference.copy(), "status": 404, "private_counts_visible": False,
                "derived_content_bytes": 0},
               {"kind": "revoked_source", "source": "installed-client", "evidence_ref": "private/revoked",
                "actor_id": actor_id, "request_scope": SCOPE.copy(), "source_revoked": True,
                "result_ref": reference.copy(), "status": 404, "private_counts_visible": False,
                "derived_content_bytes": 0}]
    return {"source": "authenticated-runtime", "evidence_ref": "private/reuse",
            "scope": SCOPE.copy(), "result": result, "mcp_artifact_sha256": digests["mcp"],
            "clients": clients, "publications": publications,
            "later_usage": {"source": "installed-api", "evidence_ref": "private/later",
                            "task_id": task_id, "actor_id": actor_id, "result_ref": reference.copy(),
                            "status": 200, "result_sha256": result["sha256"], "citation": citation,
                            "provider_calls_before": 1, "provider_calls_after": 1,
                            "retrieval_receipt": retrieval, "usage_receipt": usage},
            "denials": denials}


def test_reuse_case_reconciles_client_peer_and_later_task_to_retained_result():
    result = verify_wa_reuse(reuse_case(), fixture(), safety_case())
    assert result["result_ref"]["record_revision"] == "2"
    assert result["task_id"] == "later-task"


@pytest.mark.parametrize("change,error", [
    (lambda case: case["clients"][0].update(source="mock"), "client_parity_unverified"),
    (lambda case: case["clients"][1].update(result_sha256="0" * 64), "client_parity_unverified"),
    (lambda case: case["clients"][2].update(source_revision="foreign"), "client_parity_unverified"),
    (lambda case: case["clients"][3].update(artifact_sha256="0" * 64), "client_parity_unverified"),
    (lambda case: case["clients"][0]["journey"].remove("run"), "client_parity_unverified"),
    (lambda case: case["publications"].pop(), "consumers_missing"),
    (lambda case: case["publications"][0].update(state="unavailable"), "consumer_ack_unverified"),
    (lambda case: case["publications"][1].update(projection_verified_on_ack=False), "consumer_ack_unverified"),
    (lambda case: case["later_usage"].update(task_id="run-example"), "later_task_unverified"),
    (lambda case: case["later_usage"].update(provider_calls_after=2), "later_task_unverified"),
    (lambda case: case["later_usage"]["usage_receipt"].update(retrieval_receipt_id="foreign"),
     "later_receipt_unverified"),
    (lambda case: case["denials"][0].update(derived_content_bytes=7), "denied_content_unverified"),
    (lambda case: case["denials"][1].update(source_revoked=False), "denied_content_unverified"),
    (lambda case: case["denials"][1].update(evidence_ref=case["denials"][0]["evidence_ref"]),
     "denials_missing"),
    (lambda case: case["result"].update(sha256="0" * 64), "result_changed"),
])
def test_reuse_case_rejects_mock_partial_or_foreign_receipts(change, error):
    case = reuse_case()
    change(case)
    with pytest.raises(ReleaseProofError, match=f"wa_reuse_{error}"):
        verify_wa_reuse(case, fixture(), safety_case())


def test_reuse_selector_requires_private_release_and_safety_inputs(monkeypatch):
    from test_wa07 import test_ac_04

    for name in ("L5_WA07_RELEASE_MANIFEST", "L5_WA07_RELEASE_READBACK", "L5_WA07_SAFETY_CASE",
                 "L5_WA07_REUSE_CASE", "L5_WA07_AWS_CONNECTION"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(pytest.skip.Exception, match="deployed-target"):
        test_ac_04()
    monkeypatch.setenv("L5_WA07_REUSE_CASE", "/private/nonexistent")
    with pytest.raises(pytest.fail.Exception, match="release manifest"):
        test_ac_04()
    for name in ("L5_WA07_RELEASE_MANIFEST", "L5_WA07_RELEASE_READBACK", "L5_WA07_SAFETY_CASE"):
        monkeypatch.setenv(name, "/private/nonexistent")
    with pytest.raises(pytest.fail.Exception, match="selected WA07 AWS connection"):
        test_ac_04()


def test_reuse_selector_rejects_incompatible_client_before_claiming_live_success(tmp_path, monkeypatch):
    import test_wa07

    case = reuse_case()
    case["clients"][0]["result_sha256"] = "0" * 64
    paths = [tmp_path / name for name in ("release.json", "readback.json", "safety.json", "reuse.json")]
    for path, payload in zip(paths, ({}, {}, safety_case(), case)):
        path.write_text(json.dumps(payload))
    for name, path in zip(("L5_WA07_RELEASE_MANIFEST", "L5_WA07_RELEASE_READBACK",
                           "L5_WA07_SAFETY_CASE", "L5_WA07_REUSE_CASE"), paths):
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("L5_WA07_AWS_CONNECTION", "fictional-local")
    monkeypatch.setattr(test_wa07.InstalledFixture, "from_manifest", lambda path: fixture())
    monkeypatch.setattr(test_wa07, "_live_worker_readback", lambda *args: None)
    monkeypatch.setattr(test_wa07, "verify_wa_release", lambda *args: None)
    monkeypatch.setattr(test_wa07, "verify_wa_safety", lambda *args: None)
    with pytest.raises(ReleaseProofError, match="wa_reuse_client_parity_unverified"):
        test_wa07.test_ac_04()
