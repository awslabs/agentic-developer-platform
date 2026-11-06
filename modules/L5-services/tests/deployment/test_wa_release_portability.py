"""Real source bytes and Product HTTP readbacks drive portable acceptance."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import qualification_well_architected as driver
import qualification_well_architected_acceptance as acceptance
import qualification_well_architected_clients as clients
from qualification_installed_artifacts import case_identity
import qualification_well_architected_portability as portability


def fixture(index):
    scope = {"tenant_id": "tenant", "product_id": "product", "project_id": f"project-{index}"}
    ref = {"tenant_id": "tenant", "project_id": scope["project_id"], "source_connection_id": "installation",
           "record_type": "repository_commit", "record_id": str(200 + index), "record_revision": str(index) * 40}
    goal = {**ref, "source_connection_id": "documents", "record_type": "document", "record_id": f"goal-{index}"}
    source = {"Resources": {"Storage": {"Type": "AWS::S3::Bucket", "Properties": {"VersioningConfiguration": {"Status": "Enabled"}}}}} if index == 1 else {
        "resource": {"aws_dynamodb_table": {"records": {"hash_key": "id", "billing_mode": "PAY_PER_REQUEST"}}}}
    contents = {"sources/repository/main.json": json.dumps(source).encode(),
                "sources/goal/goals.md": f"Preserve {index} independently managed copies of customer records.\n".encode()}
    entries = [{"source_ref": reference, "revision": reference["record_revision"], "path": name,
                "archive_path": path, "bytes": len(contents[path]), "sha256": sha256(contents[path]).hexdigest()}
               for reference, name, path in ((ref, "main.json", "sources/repository/main.json"),
                                             (goal, "goals.md", "sources/goal/goals.md"))]
    architecture = {"files": entries}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", json.dumps(architecture))
        for path, content in contents.items():
            archive.writestr(path, content)
    archived = output.getvalue()
    manifest = {"archive": {"artifact_id": "private/source.zip", "version": "v1", "bytes": len(archived), "sha256": sha256(archived).hexdigest()},
                "provider_source_uri": "s3://private-source/private/source.zip", "provider_coverage": "provider_coverage_unverified",
                "architecture": architecture}
    case = {"architecture": ref, "goal": goal, "settings": {"goal_refs": [goal]}, "source_manifest": manifest,
            "run_id": f"run-{index}", "result_revision": 1, "result": {"scope": scope}}
    now = datetime.now(UTC)
    binding = {"id": f"binding-{index}", "project_id": scope["project_id"], "revision": 1, "generation": 1,
               "status": "healthy", "source_revision": ref["record_revision"], "observed_at": now.isoformat(),
               "stale_after": (now + timedelta(hours=1)).isoformat(), "selection": {"kind": "repository", "provider": "github",
               "native_id": ref["record_id"], "connection_ref": {"kind": "github_installation", "id": "installation"}}}
    selection = {"scope": scope, "qualification_portability": {"architecture_path": "main.json", "goal_path": "goals.md"}}
    return selection, case, binding, archived


def collect(selected, case, binding, archived, *, fault=None):
    calls = []
    def respond(request):
        assert request.method == "GET"
        assert request.url.path == f"/api/v1/products/product/projects/{selected['scope']['project_id']}/bindings"
        calls.append(str(request.url))
        row = deepcopy(binding)
        if fault == "late-revoke" and len(calls) == 2:
            row["status"] = "restricted"
        if fault == "revision":
            row["source_revision"] = "f" * 40
        return httpx.Response(200, json={"items": [row], "next_cursor": "repeated" if fault == "pagination" else None})
    product = lambda selection: httpx.Client(base_url="https://offline.invalid", transport=httpx.MockTransport(respond))
    native = SimpleNamespace(read_archive=lambda uri, ref: archived)
    return portability.collect(selected, case, native, product), calls


def proof():
    cases, observations = [], []
    for index in (1, 2):
        selected, case, binding, archived = fixture(index)
        observed, calls = collect(selected, case, binding, archived)
        assert len(calls) == 2
        cases.append(case)
        observations.append({"run_id": case["run_id"], "result_revision": 1, "scope": case["result"]["scope"], "portability": observed})
    installed = {"source": "independent-installed-read", "source_revision": "a" * 40,
                 "case_identities": [case_identity(case) for case in cases], "snapshot_sha256": [str(index) * 64 for index in (1, 2, 3)]}
    return cases, observations, installed, {"source_revision": "a" * 40, "repository_id": 100}


def test_real_bytes_and_provenance_advance_only_the_original_portability_criterion():
    cases, observations, installed, context = proof()
    report = acceptance.evaluate(cases, observations, context, installed=installed)
    assert next(row for row in report["live_checks"] if row["id"] == "two-non-adp-projects")["outcome"] == "passed"
    # Portability does not waive the story's independent provider-ingestion proof.
    assert next(row for row in report["story_criteria"] if row["id"] == "WA07/AC-02")["outcome"] == "incomplete"
    assert report["outcome"] == "incomplete"
    assert next(row for row in report["live_checks"] if row["id"] == "uncertain-generation-recovery")["outcome"] == "incomplete"


@pytest.mark.parametrize("fault", ["late-revoke", "revision", "pagination", "archive", "missing-file"])
def test_live_collection_refuses_incomplete_or_changed_inputs(fault):
    selected, case, binding, archived = fixture(1)
    if fault == "archive":
        archived = archived + b"tampered"
    if fault == "missing-file":
        selected["qualification_portability"]["architecture_path"] = "unselected.json"
    with pytest.raises(ValueError):
        collect(selected, case, binding, archived, fault=fault)


@pytest.mark.parametrize("fault", ["platform", "missing", "content", "resources", "goal", "checkpoint", "scope", "manifest"])
def test_portability_verifier_rejects_tampered_or_incomplete_evidence(fault):
    cases, observations, installed, context = proof()
    if fault == "platform":
        context["repository_id"] = 201
    elif fault == "missing":
        del observations[1]["portability"]
    elif fault == "content":
        observations[0]["portability"]["architecture"]["content_utf8"] += " "
    elif fault == "resources":
        observations[0]["portability"]["architecture"]["resources_sha256"] = "f" * 64
    elif fault == "goal":
        observations[0]["portability"]["goal"]["normalized_sha256"] = "f" * 64
    elif fault == "checkpoint":
        installed["snapshot_sha256"].pop()
    elif fault == "scope":
        observations[0]["portability"]["scope"] = {**cases[0]["result"]["scope"], "project_id": "foreign"}
    else:
        observations[0]["portability"]["manifest_sha256"] = "f" * 64
    with pytest.raises(ValueError):
        acceptance.evaluate(cases, observations, context, installed=installed)


def test_preflight_does_not_request_task_dependent_retained_consent(monkeypatch):
    selections = [{"run_id": "one", "result_revision": 1,
                   "qualification_clients": {"design_task_id": "later", "retained_reuse": True}},
                  {"run_id": "two", "result_revision": 1}]
    cases = [{"result": {"scope": {}}} for _ in selections]
    def qualify(selected, fixture, *, session_factory, **kwargs):
        for case in selected:
            session_factory(case)
        return cases
    monkeypatch.setattr(driver, "qualify_cases", qualify)
    monkeypatch.setattr(driver, "ProductNativeSession", lambda selected: SimpleNamespace(observation={}))
    def client(selected, product, **kwargs):
        assert selected["qualification_clients"] == {}
        return {}
    monkeypatch.setattr(clients, "collect", client)
    driver.observe_release({"cases": selections}, object(), read_only=True, preflight=True)
