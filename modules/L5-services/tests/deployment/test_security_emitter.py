"""Offline evidence emission never treats supplied success as native qualification."""

import base64
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import test_security_release_readbacks as source_fixtures

from l5_services.selector_receipts import SELECTOR_NODES

SOURCE_CONTENT = source_fixtures.SOURCE_CONTENT
reader = source_fixtures.reader
readbacks = source_fixtures.readbacks

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "modules/L5-services/scripts/emit-security-qualification.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("security_qualification_emitter", SCRIPT)
emitter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(emitter)
SOURCE = "a" * 40
DEFINITION = "b" * 40
DIGEST = "c" * 64
PROVIDER = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/fictional-provider",
            "region": "us-east-1"}


def retain_selector_receipts(environment, native):
    directory = Path(environment["PRIVATE_EVIDENCE_DIR"])
    receipts = {}
    for scenario in ("setup", "assessments", "recovery", "context"):
        name = f"security-456-2-{scenario}"
        log = directory / (name + ".log")
        log.write_bytes(b"1 passed in 0.02s\n")
        log.chmod(0o600)
        receipt = {"scenario": scenario, "run_id": "456", "run_attempt": "2",
                   "selector_node": SELECTOR_NODES[scenario],
                   "source_revision": SOURCE, "fixture_sha256": environment["L5_FIXTURE_DIGEST"],
                   "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                   "result": "passed", "observed_at": native["observed_at"]}
        content = json.dumps(receipt, sort_keys=True) + "\n"
        path = directory / (name + ".json")
        path.write_text(content)
        path.chmod(0o600)
        receipts[scenario] = hashlib.sha256(content.encode()).hexdigest()
    native["selector_receipts"] = receipts


@pytest.fixture
def private_qualification(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    runner = tmp_path / "runner"
    runner.mkdir(mode=0o700)
    environment = {
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "example/project", "GITHUB_REPOSITORY_ID": "123",
        "GITHUB_WORKFLOW_REF": "example/project/.github/workflows/l5-services-release.yml@refs/heads/main",
        "GITHUB_RUN_ID": "456", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_WORKFLOW_SHA": DEFINITION,
        "L5_CHECKOUT_REVISION": SOURCE, "REVIEWED_SOURCE_SHA": SOURCE,
        "L5_SERVICE": "security", "L5_ENVIRONMENT": "dev", "SOURCE_SHA": SOURCE,
        "PRIVATE_EVIDENCE_DIR": str(directory), "RUNNER_TEMP": str(runner),
        "WHEEL_DIGEST": DIGEST, "ARCHIVE_DIGEST": DIGEST, "MANIFEST_VERSION": "version-one",
        "L5_FIXTURE_DIGEST": DIGEST,
    }
    native = {
        "source_revision": SOURCE, "definition_revision": DEFINITION,
        "workflow_path": ".github/workflows/l5-services-release.yml",
        "run_id": "456", "run_attempt": "2", "wheel_digest": DIGEST,
        "archive_digest": DIGEST, "manifest_version": "version-one", "fixture_digest": DIGEST,
        "observed_at": datetime.now(UTC).isoformat(),
        "installed": {"worker_sha256": base64.b64encode(bytes.fromhex(DIGEST)).decode(),
                      "repair_sha256": base64.b64encode(bytes.fromhex(DIGEST)).decode(),
                      "mapping_state": "Enabled", "repair_state": "ENABLED"},
        "products": [
            {"scope": {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one"},
             "assessments": [{"capability": capability, "run_id": capability + "-one",
                               "result_id": capability + "-result-one",
                               "binding_id": capability + "-binding-one", "provider_identity": PROVIDER.copy(),
                               "native_job_ref": {"record_id": capability + "-job-one"},
                               "sources": [{"tenant_id": "tenant-one", "project_id": "project-one",
                                            "record_revision": "one"}], "finding_count": 0,
                               "native_finding_ids": [],
                               "reviewed": 0, "source_sha256": [DIGEST], "snapshot_sha256": [DIGEST]}
                             for capability in ("code_review", "threat_model")]},
            {"scope": {"tenant_id": "tenant-two", "product_id": "product-two", "project_id": "project-two"},
             "assessments": [{"capability": capability, "run_id": capability + "-two",
                               "result_id": capability + "-result-two",
                               "binding_id": capability + "-binding-two", "provider_identity": PROVIDER.copy(),
                               "native_job_ref": {"record_id": capability + "-job-two"},
                               "sources": [{"tenant_id": "tenant-two", "project_id": "project-two",
                                            "record_revision": "two"}], "finding_count": 0,
                               "native_finding_ids": [],
                               "reviewed": 0, "source_sha256": [DIGEST], "snapshot_sha256": [DIGEST]}
                             for capability in ("code_review", "threat_model")]},
        ],
    }
    for product in native["products"]:
        for record in product["assessments"]:
            rubric = {"run_id": record["run_id"], "result_id": record["result_id"],
                      "native_job_ref": record["native_job_ref"], "reviews": {},
                      "rubric_revision": "fictional-reviewed", "review_sample": [], "known_controls": ["fictional-control"]}
            review_path = directory / (record["run_id"] + "-rubric.json")
            review_path.write_text(json.dumps(rubric))
            review_path.chmod(0o600)
            record["rubric_sha256"] = hashlib.sha256(review_path.read_bytes()).hexdigest()
    fixture = {"authorization": {"source_sha": SOURCE}, "release": {"worker": SOURCE},
               "cases": [{"scope": product["scope"], "provider": PROVIDER.copy(), "assessments": [
                   {"capability": record["capability"], "binding_id": record["binding_id"],
                    "input": {"sources": record["sources"], "binding_id": record["binding_id"]},
                    "review_file": str(directory / (record["run_id"] + "-rubric.json"))}
                   for record in product["assessments"]]} for product in native["products"]]}
    fixture_path = directory / "selected-fixture.json"
    fixture_path.write_text(json.dumps(fixture))
    fixture_path.chmod(0o600)
    environment["PRIVATE_FIXTURE"] = str(fixture_path)
    environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    native["fixture_digest"] = environment["L5_FIXTURE_DIGEST"]
    cleanup_packet = {"run_id": "recovery-run", "ownership": {"owned": "scenario_owned",
                      "linked": "customer_linked"}, "resources": {"owned": "absent", "linked": "retained"}}
    cleanup_content = json.dumps(cleanup_packet, sort_keys=True, separators=(",", ":")) + "\n"
    cleanup_file = directory / "security-cleanup-456-2.json"
    cleanup_file.write_text(cleanup_content)
    cleanup_file.chmod(0o600)
    native["cleanup"] = {"run_id": "recovery-run", "owned_absent": 1, "linked_retained": 1,
                         "readback": ["private://security/native-cleanup/" + cleanup_file.name
                                      + "#sha256:" + hashlib.sha256(cleanup_content.encode()).hexdigest()],
                         "observed_at": native["observed_at"]}
    retain_selector_receipts(environment, native)
    path = directory / "security-readback-456-2.json"
    path.write_text(json.dumps(native))
    path.chmod(0o600)
    return environment, path, native


@pytest.mark.parametrize("fault", ["accepted", "rehashed_foreign_finding",
                                     "missing_native_result", "rehashed_wrong_source",
                                     "rehashed_wrong_revision", "rehashed_wrong_definition", "rehashed_wrong_workflow"])
def test_current_product_and_provider_assessment_reaches_emitter(private_qualification, readbacks, fault):
    environment, path, report_input = private_qualification
    case, product_rows, provider_jobs = readbacks
    scope = case["scope"]
    base = f"/v1/products/{scope['product_id']}/projects/{scope['project_id']}/services/security"
    plan = case["assessments"][0]
    source = plan["input"]["sources"][0]
    native_job = provider_jobs["code_review-run"]
    finding = {"native_id": "native-finding", "claim": "fictional cited finding",
               "native_job_ref": native_job["native_job_ref"], "source_refs": [source]}
    native_job["findings"] = [finding]
    result = product_rows[base + "/results/code_review-result"]
    result["finding_count"] = 1
    product_rows[base + "/results/code_review-result/findings?limit=100"]["items"] = [finding]
    review_path = Path(plan["review_file"])
    rubric = json.loads(review_path.read_text())
    rubric["reviews"] = {finding["native_id"]: {"judgment": "supported",
        "evidence_ref": "private://security/review/citation",
        "source_revision": source["record_revision"]}}
    review_path.write_text(json.dumps(rubric))
    observed = reader.assess_case(case, product_rows.__getitem__,
                                  lambda run, provider: provider_jobs[run["run_id"]],
                                  lambda run: [{"source_ref": source, "content": SOURCE_CONTENT} for source in run["input"]["sources"]])
    assert observed[0]["native_finding_ids"] == ["native-finding"]
    fixture_path = Path(environment["PRIVATE_FIXTURE"])
    fixture = json.loads(fixture_path.read_text())
    for assessment in case["assessments"]:
        assessment["input"]["binding_id"] = assessment["binding_id"]
    fixture["cases"][0].update(scope=scope, provider=case["provider"], assessments=case["assessments"])
    fixture_path.write_text(json.dumps(fixture))
    environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    report_input["fixture_digest"] = environment["L5_FIXTURE_DIGEST"]
    report_input["products"][0].update(scope=scope, assessments=observed)
    if fault == "rehashed_foreign_finding":
        report_input["products"][0]["assessments"][0]["native_finding_ids"] = ["foreign-finding"]
    elif fault == "missing_native_result":
        report_input["products"][0]["assessments"].pop()
    elif fault == "rehashed_wrong_source":
        report_input["products"][0]["assessments"][0]["source_sha256"] = ["f" * 64]
    elif fault == "rehashed_wrong_revision":
        report_input["source_revision"] = "f" * 40
    elif fault == "rehashed_wrong_definition":
        report_input["definition_revision"] = "f" * 40
    elif fault == "rehashed_wrong_workflow":
        report_input["workflow_path"] = ".github/workflows/other.yml"
    retain_selector_receipts(environment, report_input)
    path.write_text(json.dumps(report_input))
    if fault == "accepted":
        report = emitter.emit(environment)
        assert report["native_observation"]["assessments"] == 4
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        delivery = json.loads((Path(environment["RUNNER_TEMP"]) /
                               "delivery-evidence/delivery-evidence.json").read_text())
        assert delivery["native_observation"]["sha256"] == digest
        observed = report["native_observation"]
        for field, expected in (("source_revision", SOURCE), ("definition_revision", DEFINITION),
                                ("workflow_path", emitter.WORKFLOW_PATH), ("run_id", "456"), ("run_attempt", "2")):
            assert observed[field] == delivery["native_observation"][field] == expected
        source_check = next(check for check in report["live_checks"] if check["id"] == "source-pinning")
        assert source_check["source_revision"] == SOURCE
        assert source_check["definition_revision"] == DEFINITION
        assert source_check["workflow_path"] == emitter.WORKFLOW_PATH
        assert source_check["evidence_ref"].endswith("#sha256:" + digest)
        assert report["unresolved"]
    else:
        with pytest.raises(ValueError):
            emitter.emit(environment)
        assert not (Path(environment["RUNNER_TEMP"]) / "delivery-evidence/delivery-evidence.json").exists()


def test_native_sampled_review_retains_partial_evidence(private_qualification):
    environment, path, native = private_qualification
    record = native["products"][0]["assessments"][0]
    record["finding_count"] = record["reviewed"] = 3
    record["native_finding_ids"] = [f"finding-{index}" for index in range(3)]
    review_path = path.parent / (record["run_id"] + "-rubric.json")
    rubric = json.loads(review_path.read_text())
    rubric["reviews"] = {f"finding-{index}": {
        "judgment": "supported", "evidence_ref": "private://security/review/example",
        "source_revision": record["sources"][0]["record_revision"]} for index in range(3)}
    rubric["review_sample"] = list(rubric["reviews"])
    review_path.write_text(json.dumps(rubric))
    record["rubric_sha256"] = hashlib.sha256(review_path.read_bytes()).hexdigest()
    path.write_text(json.dumps(native))
    report = emitter.emit(environment)
    assert report["native_observation"]["assessments"] == 4
    assert report["unresolved"]


@pytest.mark.parametrize("fault", ["unchecked_zero_findings", "missing_revision", "uncited_review",
                                   "invalid_judgment", "wrong_review_sample", "missing_native_ids",
                                   "foreign_native_ids", "duplicate_native_ids", "malformed_sample"])
def test_rehashed_native_review_needs_checked_controls_and_cited_judgments(private_qualification, fault):
    environment, path, native = private_qualification
    record = native["products"][0]["assessments"][0]
    review_path = path.parent / (record["run_id"] + "-rubric.json")
    rubric = json.loads(review_path.read_text())
    if fault == "unchecked_zero_findings":
        rubric["known_controls"] = []
    elif fault == "missing_revision":
        rubric["rubric_revision"] = ""
    elif fault == "missing_native_ids":
        record.pop("native_finding_ids")
    else:
        record["finding_count"] = 3 if fault == "wrong_review_sample" else 1
        record["reviewed"] = min(record["finding_count"], 3)
        record["native_finding_ids"] = [f"finding-{index}" for index in range(record["finding_count"])]
        rubric["reviews"] = {f"finding-{index}": {
            "judgment": "supported", "evidence_ref": "private://security/review/example",
            "source_revision": record["sources"][0]["record_revision"]}
            for index in range(record["reviewed"])}
        if fault == "uncited_review":
            rubric["reviews"]["finding-0"]["source_revision"] = "foreign-revision"
        elif fault == "invalid_judgment":
            rubric["reviews"]["finding-0"]["judgment"] = "unreviewed"
        elif fault == "foreign_native_ids":
            record["native_finding_ids"] = ["another-finding"]
        elif fault == "duplicate_native_ids":
            record["native_finding_ids"] = ["finding-0"] * 3
        elif fault == "malformed_sample":
            rubric["review_sample"] = [{"native_id": "finding-0"}]
        else:
            rubric["review_sample"] = ["another-finding"]
    review_path.write_text(json.dumps(rubric))
    record["rubric_sha256"] = hashlib.sha256(review_path.read_bytes()).hexdigest()
    path.write_text(json.dumps(native))
    with pytest.raises(ValueError, match="independent rubric"):
        emitter.emit(environment)
    assert not (Path(environment["RUNNER_TEMP"]) / "delivery-evidence/delivery-evidence.json").exists()


def test_native_partial_readback_emits_both_shapes_but_never_passing_evidence(private_qualification):
    environment, path, native = private_qualification
    (path.parent / "independent-observations.json").write_text(json.dumps({
        "epic_criteria": [{"id": "EC-01", "outcome": "passed", "evidence_verified": True}],
        "story_criteria": [{"id": "SA16/AC-02", "outcome": "passed", "evidence_verified": True}],
        "scenarios": [{"id": "SA16/SCN-02", "outcome": "passed", "evidence_verified": True}],
        "live_checks": [{"id": "same-wheel", "outcome": "passed", "evidence_verified": True},
                        {"id": "ui-cli-mcp-parity", "outcome": "passed", "evidence_verified": True},
                        {"id": "runtime-revisions", "outcome": "passed", "evidence_verified": True}],
        "cleanup": {"outcome": "passed"}}))
    report = emitter.emit(environment)
    root = Path(environment["RUNNER_TEMP"])
    qualification = json.loads((root / "l5-security-qualification/qualification.json").read_text())
    delivery = json.loads((root / "delivery-evidence/delivery-evidence.json").read_text())
    assert qualification == report
    assert report["native_observation"]["products"] == 2
    assert report["native_observation"]["assessments"] == 4
    assert report["native_observation"]["sha256"] and report["source_revision"] == SOURCE
    assert report["native_observation"]["fixture_sha256"] == environment["L5_FIXTURE_DIGEST"]
    assert report["native_observation"]["selector_receipts"] == {
        scenario: hashlib.sha256((path.parent / f"security-456-2-{scenario}.json").read_bytes()).hexdigest()
        for scenario in ("setup", "assessments", "recovery", "context")
    }
    assert report["definition_revision"] == DEFINITION
    assert report["workflow_path"] == native["workflow_path"]
    assert all(record["definition_revision"] == DEFINITION
               and record["workflow_path"] == native["workflow_path"]
               for record in report["live_checks"] if record["outcome"] == "passed")
    assert report["unresolved"] and delivery["unresolved"] == report["unresolved"]
    assert delivery["schema_version"] == 2
    assert delivery["qualification_artifact"] == "l5-security-qualification/qualification.json"
    assert delivery["cleanup"]["outcome"] == "incomplete"
    assert "cleanup:not-observed" in report["unresolved"]
    assert report["native_observation"]["owned_absent"] == 1
    assert all(item["evidence_verified"] is False for item in report["epic_criteria"])
    assert next(item for item in report["story_criteria"] if item["id"] == "SA16/AC-02")[
        "outcome"] == "incomplete"
    assert next(item for item in report["scenarios"] if item["id"] == "SA16/SCN-02")[
        "outcome"] == "incomplete"
    supported = {"real-threat-model", "real-static-review", "source-pinning"}
    passed = {item["id"] for item in report["live_checks"] if item["outcome"] == "passed"}
    assert passed == supported
    assert all(item["evidence_verified"] is (item["id"] in supported) for item in report["live_checks"])
    assert all(item["requirement_revision"] == emitter.rollup_module().proposal_revision()
               for item in report["live_checks"] if item["id"] in supported)
    assert all(item["runtime_revision"] == SOURCE and item["run_attempt"] == "2"
               and item["evidence_ref"].startswith("private://security/")
               for item in report["live_checks"] if item["id"] in supported)
    assert all(report["native_observation"]["sha256"] in item["evidence_ref"]
               for item in report["live_checks"] if item["id"] in supported)
    assert {check["name"] for check in delivery["required_checks"] if check["evidence_verified"]} == supported
    assert "live_checks/same-wheel:not-observed" in report["unresolved"]
    assert "live_checks/ui-cli-mcp-parity:not-observed" in report["unresolved"]
    assert "live_checks/owned-cleanup:not-observed" in report["unresolved"]
    assert "live_checks/source-pinning:not-observed" not in report["unresolved"]
    assert "live_checks/runtime-revisions:not-observed" in report["unresolved"]
    assert "gateway_revision:missing" in report["unresolved"]
    assert native["products"][0]["scope"]["product_id"] not in json.dumps(report)
    with pytest.raises(ValueError, match="existing"):
        emitter.emit(environment)


@pytest.mark.parametrize("fault", ["missing", "wrong_source", "wrong_attempt", "altered_log",
                                   "rehashed_foreign_source", "rehashed_wrong_node", "rehashed_failed_log",
                                   "out_of_order", "missing_receipt"])
def test_native_qualification_requires_executed_selector_receipts(private_qualification, fault):
    environment, path, native = private_qualification
    receipt_path = path.parent / "security-456-2-context.json"
    receipt = json.loads(receipt_path.read_text())
    if fault == "missing":
        native.pop("selector_receipts")
    elif fault == "wrong_source":
        receipt["source_revision"] = "f" * 40
    elif fault == "rehashed_foreign_source":
        receipt["source_revision"] = "f" * 40
    elif fault == "wrong_attempt":
        receipt["run_attempt"] = "3"
    elif fault == "altered_log":
        (path.parent / "security-456-2-context.log").write_bytes(b"unrelated log\n")
    elif fault == "rehashed_wrong_node":
        receipt["selector_node"] = SELECTOR_NODES["setup"]
    elif fault == "rehashed_failed_log":
        log = path.parent / "security-456-2-context.log"
        log.write_bytes(b"1 passed in 0.02s\n1 failed\n")
        receipt["log_sha256"] = hashlib.sha256(log.read_bytes()).hexdigest()
    elif fault == "out_of_order":
        receipt["observed_at"] = (datetime.fromisoformat(receipt["observed_at"]) - timedelta(seconds=1)).isoformat()
    else:
        receipt_path.unlink()
    if fault in {"wrong_source", "wrong_attempt", "rehashed_foreign_source", "rehashed_wrong_node", "rehashed_failed_log", "out_of_order"}:
        receipt_path.write_text(json.dumps(receipt) + "\n")
    if fault in {"rehashed_foreign_source", "rehashed_wrong_node", "rehashed_failed_log", "out_of_order"}:
        native["selector_receipts"]["context"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    path.write_text(json.dumps(native))
    with pytest.raises((ValueError, OSError)):
        emitter.emit(environment)
    assert not (Path(environment["RUNNER_TEMP"]) / "l5-security-qualification/qualification.json").exists()
    assert not (Path(environment["RUNNER_TEMP"]) /
                "delivery-evidence/delivery-evidence.json").exists()


def test_long_running_assessments_retain_earlier_selector_provenance(private_qualification):
    environment, path, native = private_qualification
    receipt_path = path.parent / "security-456-2-setup.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["observed_at"] = (datetime.now(UTC) - timedelta(minutes=90)).isoformat()
    receipt_path.write_text(json.dumps(receipt) + "\n")
    native["selector_receipts"]["setup"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    path.write_text(json.dumps(native))
    report = emitter.emit(environment)
    assert report["native_observation"]["products"] == 2
    assert report["unresolved"]


@pytest.mark.parametrize("fault", ["missing_binding", "wrong_binding", "missing_provider",
                                   "foreign_provider"])
def test_native_assessment_provider_binding_cannot_be_rehashed(private_qualification, fault):
    environment, path, native = private_qualification
    assessment = native["products"][0]["assessments"][0]
    if fault == "missing_binding":
        assessment.pop("binding_id")
    elif fault == "wrong_binding":
        assessment["binding_id"] = "foreign-binding"
    elif fault == "missing_provider":
        assessment.pop("provider_identity")
    else:
        assessment["provider_identity"]["role_arn"] = "foreign-provider"
    path.write_text(json.dumps(native))
    with pytest.raises(ValueError, match="source and provider binding"):
        emitter.emit(environment)
    assert not (Path(environment["RUNNER_TEMP"]) / "delivery-evidence/delivery-evidence.json").exists()


@pytest.mark.parametrize("fault", ["foreign_tenant", "foreign_project", "moving_tip", "missing_revision"])
def test_rehashed_selected_product_sources_cannot_qualify(private_qualification, fault):
    environment, path, native = private_qualification
    fixture_path = Path(environment["PRIVATE_FIXTURE"])
    fixture = json.loads(fixture_path.read_text())
    source = fixture["cases"][0]["assessments"][0]["input"]["sources"][0]
    native["products"][0]["assessments"][0]["sources"][0] = source
    if fault == "foreign_tenant":
        source["tenant_id"] = "tenant-two"
    elif fault == "foreign_project":
        source["project_id"] = "project-two"
    elif fault == "moving_tip":
        source["record_revision"] = "latest"
    else:
        source.pop("record_revision")
    fixture_path.write_text(json.dumps(fixture))
    environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    native["fixture_digest"] = environment["L5_FIXTURE_DIGEST"]
    retain_selector_receipts(environment, native)
    path.write_text(json.dumps(native))
    with pytest.raises(ValueError, match="foreign or not pinned"):
        emitter.emit(environment)
    assert not (Path(environment["RUNNER_TEMP"]) / "delivery-evidence/delivery-evidence.json").exists()


@pytest.mark.parametrize("fault", ["accepted", "missing", "foreign_event", "shared_job",
                                   "shared_result", "wrong_run", "reused_source", "wrong_snapshot",
                                   "altered_journal", "forged_journal", "out_of_order",
                                   "missing_baseline", "extra_native_job", "forged_inventory",
                                   "forged_baseline", "foreign_native_space",
                                   "foreign_rubric", "supplied_success", "forged_submit",
                                   "foreign_provider", "wrong_binding",
                                   "wrong_submit_run", "late_submit", "swapped_submits",
                                   "foreign_event_source", "moving_event_tip", "pre_inventory"])
def test_native_trigger_receipts_require_private_journal_and_independent_review(private_qualification, fault):
    environment, path, native = private_qualification
    fixture_path = Path(environment["PRIVATE_FIXTURE"])
    fixture = json.loads(fixture_path.read_text())
    triggers = []
    for index, (case, product) in enumerate(zip(fixture["cases"], native["products"], strict=True)):
        event_id = f"selected-event-{index}"
        run_id = f"selected-event-run-{index}"
        result_id = f"selected-event-result-{index}"
        job = {"record_id": f"selected-event-job-{index}"}
        source = {**product["assessments"][0]["sources"][0], "record_revision": f"event-revision-{index}"}
        rubric_path = path.parent / f"event-{index}-rubric.json"
        rubric_path.write_text(json.dumps({"run_id": run_id, "result_id": result_id,
                                           "native_job_ref": job, "reviews": {}, "review_sample": [],
                                           "rubric_revision": "fictional-reviewed", "known_controls": ["fictional-control"]}))
        rubric_path.chmod(0o600)
        journal_path = path.parent / f"event-{index}-attempts.jsonl"
        entries = []
        for attempt in range(2):
            receipt = f"assessment-{index}-{attempt}"
            case["assessments"][attempt]["receipt"] = receipt
            for status in ("prepared", "acknowledged"):
                entries.append({"operation": "assessment_submit", "status": status,
                                "attempt_id": receipt, "work_id": receipt, "run_id": None,
                                "effect_id": product["assessments"][attempt]["run_id"]
                                if status == "acknowledged" else None,
                                "idempotency_key": receipt if status == "acknowledged" else None,
                                "native_job_id": None, "observed_at": datetime.now(UTC).isoformat()})
        inventory_before = Path(str(journal_path) + ".event-before.json")
        before = {"event_id": event_id, "scope": case["scope"],
                  "run_id": product["assessments"][0]["run_id"], "space": f"event-space-{index}",
                  "jobs": [product["assessments"][0]["native_job_ref"]],
                  "observed_at": datetime.now(UTC).isoformat()}
        inventory_before.write_text(json.dumps(before) + "\n")
        inventory_before.chmod(0o600)
        for attempt in range(2):
            for status in ("prepared", "acknowledged"):
                entries.append({"operation": "event_delivery", "status": status,
                                "attempt_id": f"{event_id}-{attempt}", "work_id": event_id,
                                "effect_id": run_id if status == "acknowledged" else None,
                                "observed_at": str(datetime.now(UTC).timestamp())})
        journal_path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        journal_path.chmod(0o600)
        inventory_after = Path(str(journal_path) + ".event-after.json")
        after = {**before, "run_id": run_id,
                 "jobs": [product["assessments"][0]["native_job_ref"], job],
                 "observed_at": datetime.now(UTC).isoformat()}
        inventory_after.write_text(json.dumps(after) + "\n")
        inventory_after.chmod(0o600)
        selected_binding = product["assessments"][0]["binding_id"]
        case["trigger_event"] = {"event_id": event_id, "capability": "code_review",
                                 "input": {"binding_id": selected_binding, "sources": [source]},
                                 "review_file": str(rubric_path)}
        case["assessment_journal_file"] = str(journal_path)
        triggers.append({"event_id": event_id, "run_id": run_id, "result_id": result_id,
                         "native_job_ref": job, "binding_id": selected_binding,
                         "provider_identity": PROVIDER.copy(),
                         "source_sha256": [DIGEST], "snapshot_sha256": [DIGEST],
                         "finding_count": 0,
                         "native_finding_ids": [],
                         "reviewed": 0, "rubric_sha256": hashlib.sha256(rubric_path.read_bytes()).hexdigest(),
                         "journal_sha256": hashlib.sha256(journal_path.read_bytes()).hexdigest(),
                         "before_sha256": hashlib.sha256(inventory_before.read_bytes()).hexdigest(),
                         "after_sha256": hashlib.sha256(inventory_after.read_bytes()).hexdigest(),
                         "observed_at": native["observed_at"]})
    native["observed_at"] = datetime.now(UTC).isoformat()
    for trigger in triggers:
        trigger["observed_at"] = native["observed_at"]
    native["triggers"] = triggers
    if fault == "missing":
        native.pop("triggers")
    elif fault == "foreign_event":
        triggers[0]["event_id"] = "another-event"
    elif fault == "shared_job":
        triggers[0]["native_job_ref"] = native["products"][0]["assessments"][0]["native_job_ref"]
    elif fault == "shared_result":
        triggers[0]["result_id"] = native["products"][0]["assessments"][0]["result_id"]
    elif fault == "wrong_run":
        triggers[0]["run_id"] = "another-run"
    elif fault == "reused_source":
        fixture["cases"][0]["trigger_event"]["input"]["sources"] = (
            native["products"][0]["assessments"][0]["sources"])
    elif fault == "foreign_event_source":
        fixture["cases"][0]["trigger_event"]["input"]["sources"][0]["tenant_id"] = "tenant-two"
    elif fault == "moving_event_tip":
        fixture["cases"][0]["trigger_event"]["input"]["sources"][0]["record_revision"] = "HEAD"
    elif fault == "wrong_snapshot":
        triggers[0]["snapshot_sha256"] = ["0" * 64]
    elif fault == "pre_inventory":
        after_path = Path(fixture["cases"][0]["assessment_journal_file"] + ".event-after.json")
        after = json.loads(after_path.read_text())
        triggers[0]["observed_at"] = (datetime.fromisoformat(after["observed_at"])
                                      - timedelta(microseconds=1)).isoformat()
    elif fault == "altered_journal":
        Path(fixture["cases"][0]["assessment_journal_file"]).write_text("{}\n")
    elif fault == "forged_journal":
        journal_path = Path(fixture["cases"][0]["assessment_journal_file"])
        entries = [json.loads(line) for line in journal_path.read_text().splitlines()]
        entries[-1]["effect_id"] = "another-run"
        journal_path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        triggers[0]["journal_sha256"] = hashlib.sha256(journal_path.read_bytes()).hexdigest()
    elif fault == "out_of_order":
        journal_path = Path(fixture["cases"][0]["assessment_journal_file"])
        entries = [json.loads(line) for line in journal_path.read_text().splitlines()]
        entries[-1]["observed_at"] = str(float(entries[4]["observed_at"]) - 1)
        journal_path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        triggers[0]["journal_sha256"] = hashlib.sha256(journal_path.read_bytes()).hexdigest()
    elif fault in {"forged_submit", "wrong_submit_run", "late_submit", "swapped_submits"}:
        journal_path = Path(fixture["cases"][0]["assessment_journal_file"])
        entries = [json.loads(line) for line in journal_path.read_text().splitlines()]
        if fault == "forged_submit":
            entries[1]["attempt_id"] = "foreign-receipt"
        elif fault == "wrong_submit_run":
            entries[1]["effect_id"] = native["products"][1]["assessments"][0]["run_id"]
        elif fault == "late_submit":
            entries[1]["observed_at"] = "2099-01-01T00:00:00+00:00"
        else:
            entries[:4] = entries[2:4] + entries[:2]
        journal_path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        triggers[0]["journal_sha256"] = hashlib.sha256(journal_path.read_bytes()).hexdigest()
    elif fault == "missing_baseline":
        Path(fixture["cases"][0]["assessment_journal_file"] + ".event-before.json").unlink()
    elif fault in {"forged_baseline", "foreign_native_space"}:
        before_path = Path(fixture["cases"][0]["assessment_journal_file"] + ".event-before.json")
        before = json.loads(before_path.read_text())
        if fault == "forged_baseline":
            before["jobs"].append(triggers[0]["native_job_ref"])
        else:
            before["space"] = "another-native-space"
        before_path.write_text(json.dumps(before) + "\n")
        triggers[0]["before_sha256"] = hashlib.sha256(before_path.read_bytes()).hexdigest()
    elif fault in {"extra_native_job", "forged_inventory"}:
        after_path = Path(fixture["cases"][0]["assessment_journal_file"] + ".event-after.json")
        after = json.loads(after_path.read_text())
        after["jobs"].append({"record_id": "unexpected-native-job"})
        after_path.write_text(json.dumps(after) + "\n")
        if fault == "forged_inventory":
            triggers[0]["after_sha256"] = hashlib.sha256(after_path.read_bytes()).hexdigest()
    elif fault == "foreign_rubric":
        Path(fixture["cases"][0]["trigger_event"]["review_file"]).write_text("{}")
    elif fault == "foreign_provider":
        triggers[0]["provider_identity"]["role_arn"] = "foreign-provider"
    elif fault == "wrong_binding":
        triggers[0]["binding_id"] = "foreign-binding"
    elif fault == "supplied_success":
        native.pop("triggers")
        (path.parent / "independent-observations.json").write_text(json.dumps({
            "live_checks": [{"id": "immediate-dispatch", "outcome": "passed", "evidence_verified": True}],
            "story_criteria": [{"id": "SA16/AC-02", "outcome": "passed", "evidence_verified": True}],
            "scenarios": [{"id": "SA16/SCN-02", "outcome": "passed", "evidence_verified": True}]}))
    fixture_path.write_text(json.dumps(fixture))
    environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    native["fixture_digest"] = environment["L5_FIXTURE_DIGEST"]
    retain_selector_receipts(environment, native)
    path.write_text(json.dumps(native))
    if fault == "accepted":
        report = emitter.emit(environment)
        assert report["native_observation"]["triggered_runs"] == 2
        for section, identifier in (("story_criteria", "SA16/AC-02"), ("scenarios", "SA16/SCN-02")):
            verified = next(record for record in report[section] if record["id"] == identifier)
            assert verified["outcome"] == "passed" and verified["evidence_verified"] is True
            assert verified["source_revision"] == verified["runtime_revision"] == SOURCE
            assert report["native_observation"]["sha256"] in verified["evidence_ref"]
        assert all(record["outcome"] != "passed" for record in report["epic_criteria"])
        assert next(record for record in report["story_criteria"] if record["id"] == "SA16/AC-03")[
            "outcome"] == "incomplete"
        assert "live_checks/immediate-dispatch:not-observed" in report["unresolved"]
        assert "live_checks/delayed-polling:not-observed" in report["unresolved"]
        assert "native-readbacks:required-scenarios-not-evaluated" in report["unresolved"]
    else:
        with pytest.raises((ValueError, OSError)):
            emitter.emit(environment)
        assert not (Path(environment["RUNNER_TEMP"]) / "delivery-evidence/delivery-evidence.json").exists()


@pytest.mark.parametrize("fault", ["accepted", "wrong_source", "wrong_definition",
                                     "wrong_workflow", "missing_workflow", "changed_fixture", "changed_readback"])
def test_delivery_provenance_requires_validated_native_receipt(private_qualification, fault):
    environment, path, native = private_qualification
    if fault == "wrong_source":
        native["source_revision"] = DEFINITION
    elif fault == "wrong_definition":
        native["definition_revision"] = SOURCE
    elif fault == "wrong_workflow":
        native["workflow_path"] = ".github/workflows/other.yml"
    elif fault == "missing_workflow":
        native.pop("workflow_path")
    elif fault == "changed_fixture":
        native["fixture_digest"] = DIGEST
    elif fault == "changed_readback":
        native["products"][0]["scope"]["tenant_id"] = "foreign-tenant"
    path.write_text(json.dumps(native))
    if fault != "accepted":
        with pytest.raises(ValueError):
            emitter.emit(environment)
        assert not (Path(environment["RUNNER_TEMP"]) / "delivery-evidence/delivery-evidence.json").exists()
        return
    report = emitter.emit(environment)
    delivery = json.loads((Path(environment["RUNNER_TEMP"]) /
                           "delivery-evidence/delivery-evidence.json").read_text())
    qualification_path = Path(environment["RUNNER_TEMP"]) / "l5-security-qualification/qualification.json"
    assert delivery["qualification_sha256"] == hashlib.sha256(qualification_path.read_bytes()).hexdigest()
    assert delivery["definition_revision"] == DEFINITION
    assert delivery["workflow_path"] == native["workflow_path"]
    assert json.loads(qualification_path.read_text())["native_observation"] == report["native_observation"]
    qualification_path.write_text("{}")
    assert delivery["qualification_sha256"] != hashlib.sha256(qualification_path.read_bytes()).hexdigest()
    assert delivery["native_observation"] == {
        field: report["native_observation"][field] for field in (
            "sha256", "source_revision", "definition_revision", "workflow_path",
            "run_id", "run_attempt", "wheel_sha256", "archive_sha256", "fixture_sha256",
            "installed_scope_sha256", "gateway_revision", "selector_receipts")}
    assert delivery["native_observation"]["selector_receipts"] == native["selector_receipts"]
    assert delivery["native_observation"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "target" not in delivery and "target-identity:missing" in delivery["unresolved"]
    assert "live_checks/same-wheel:not-observed" in delivery["unresolved"]


@pytest.mark.parametrize("fault", ["accepted", "foreign_account", "foreign_region", "unavailable", "missing_gateway"])
def test_delivery_target_comes_only_from_engine_and_selected_gateway(private_qualification, monkeypatch, fault):
    environment, path, native = private_qualification
    environment.update(GITHUB_SHA=DEFINITION, ADP_CORRELATION="orch:12345678-1234-1234-1234-123456789abc",
                       ADP_SOURCE_REVISION=SOURCE, ADP_DEFINITION_REVISION=DEFINITION, ADP_CONTRACT_DIGEST=DIGEST,
                       AWS_REGION="us-east-1", L5_MUTATION_ROLE_ARN="arn:aws:iam::123456789012:role/deployment")
    if fault != "missing_gateway":
        native["gateway"] = {"revision": SOURCE, "account_id": "123456789012", "region": "us-east-1",
                             "cluster": "adp-dev-eks-cluster", "namespace": "adp-gateway",
                             "image_digest": "sha256:" + DIGEST, "pod_uids": ["pod-one"],
                             "migration_head": "head-one", "wheel_sha256": DIGEST,
                             "observed_at": native["observed_at"]}
    path.write_text(json.dumps(native))
    selected = {"connection_id": "selected-connection", "resource_kind": "eks-namespace",
                "resource_id": "selected-cluster/selected-namespace", "account_id": "123456789012",
                "region": "us-east-1"}
    if fault == "foreign_account":
        selected["account_id"] = "999999999999"
    elif fault == "foreign_region":
        selected["region"] = "us-west-2"

    def target_reader(observed_environment, context, *, target=False):
        assert observed_environment is environment and context == emitter.release_identity(environment) and target
        if fault == "unavailable":
            raise ValueError("Gateway authority unavailable")
        return selected

    monkeypatch.setattr(emitter, "authorize_engine", target_reader)
    if fault in {"foreign_account", "foreign_region", "unavailable"}:
        with pytest.raises(ValueError):
            emitter.emit(environment)
        assert not (Path(environment["RUNNER_TEMP"]) / "delivery-evidence/delivery-evidence.json").exists()
        return
    report = emitter.emit(environment)
    delivery = json.loads((Path(environment["RUNNER_TEMP"]) /
                           "delivery-evidence/delivery-evidence.json").read_text())
    if fault == "accepted":
        assert report["target"] == delivery["target"] == {
            "connection_id": "selected-connection", "resource_kind": "eks-namespace",
            "resource_id": "selected-cluster/selected-namespace"}
        assert "target-identity:missing" not in report["unresolved"]
    else:
        assert "target" not in report and "target" not in delivery
        assert "target-identity:missing" in report["unresolved"]
    assert ("live_checks/same-wheel:not-observed" in report["unresolved"]) is (fault != "accepted")


@pytest.mark.parametrize("fault", ["accepted", "missing", "foreign_run", "foreign_scope", "allowed",
                                     "leaked", "duplicate", "stale", "changed_selection", "supplied_success"])
def test_scope_revocation_requires_post_cleanup_native_denials(private_qualification, fault):
    environment, path, native = private_qualification
    fixture_path = Path(environment["PRIVATE_FIXTURE"])
    fixture = json.loads(fixture_path.read_text())
    selected = fixture["cases"][0]
    sources = selected["assessments"][0]["input"]["sources"]
    selected["selected"] = {"sources": sources, "binding_id": "binding-example"}
    selected["recovery"] = {"scope": selected["scope"], "sources": sources,
                            "binding_id": "binding-example", "foreign_product_id": "foreign-product",
                            "revoked_token_env": "REVOKED_TOKEN"}
    fixture_path.write_text(json.dumps(fixture))
    environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    native["fixture_digest"] = environment["L5_FIXTURE_DIGEST"]
    retain_selector_receipts(environment, native)
    denial = {"run_id": native["cleanup"]["run_id"], "scope": selected["scope"],
              "observed_at": native["observed_at"], "probes": [
                  {"case": label, "status": 403, "disclosed_resources": []} for label in (
                      "foreign_product", "foreign_tenant", "revoked_access")]}
    if fault not in {"missing", "supplied_success"}:
        native["scope_denials"] = denial
    if fault == "foreign_run":
        denial["run_id"] = "another-run"
    elif fault == "foreign_scope":
        denial["scope"] = fixture["cases"][1]["scope"]
    elif fault == "allowed":
        denial["probes"][2]["status"] = 200
    elif fault == "leaked":
        denial["probes"][0]["disclosed_resources"] = ["private-result"]
    elif fault == "duplicate":
        denial["probes"][2]["case"] = "foreign_product"
    elif fault == "stale":
        denial["observed_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    elif fault == "changed_selection":
        selected["recovery"]["sources"] = [{"record_revision": "other"}]
        fixture_path.write_text(json.dumps(fixture))
        environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
        native["fixture_digest"] = environment["L5_FIXTURE_DIGEST"]
        retain_selector_receipts(environment, native)
    if fault == "supplied_success":
        (path.parent / "independent-observations.json").write_text(json.dumps({
            "live_checks": [{"id": "scope-revocation", "outcome": "passed", "evidence_verified": True}]}))
    path.write_text(json.dumps(native))
    if fault not in {"accepted", "missing", "supplied_success"}:
        with pytest.raises(ValueError):
            emitter.emit(environment)
        assert not (Path(environment["RUNNER_TEMP"]) / "delivery-evidence/delivery-evidence.json").exists()
        return
    report = emitter.emit(environment)
    delivery = json.loads((Path(environment["RUNNER_TEMP"]) /
                           "delivery-evidence/delivery-evidence.json").read_text())
    verified = next(item for item in report["live_checks"] if item["id"] == "scope-revocation")
    assert report["native_observation"]["scope_denials"] is (fault == "accepted")
    assert verified["evidence_verified"] is False and verified["outcome"] == "incomplete"
    assert any(item["name"] == "scope-revocation" and item["evidence_verified"] is False
               for item in delivery["required_checks"])
    assert "live_checks/scope-revocation:not-observed" in report["unresolved"]
    assert "cleanup:not-observed" in report["unresolved"]


@pytest.mark.parametrize("fault", ["accepted", "foreign_account", "wrong_revision", "wrong_region",
                                     "wrong_cluster", "unread_image", "missing_pods", "duplicate_pods",
                                     "stale", "after_receipt", "missing_wheel", "wrong_wheel"])
def test_gateway_serving_revision_only_follows_independent_readback(private_qualification, fault):
    environment, path, native = private_qualification
    environment.update(AWS_REGION="us-east-1", L5_MUTATION_ROLE_ARN=
                       "arn:aws:iam::123456789012:role/fictional-deployment")
    gateway = {"revision": SOURCE, "account_id": "123456789012", "region": "us-east-1",
               "cluster": "adp-dev-eks-cluster", "namespace": "adp-gateway",
               "image_digest": "sha256:" + DIGEST, "pod_uids": ["pod-one"],
               "migration_head": "head-one", "wheel_sha256": DIGEST,
               "observed_at": native["observed_at"]}
    native["gateway"] = copy.deepcopy(gateway)
    if fault == "foreign_account":
        native["gateway"]["account_id"] = "999999999999"
    elif fault == "wrong_revision":
        native["gateway"]["revision"] = DEFINITION
    elif fault == "wrong_region":
        native["gateway"]["region"] = "us-west-2"
    elif fault == "wrong_cluster":
        native["gateway"]["cluster"] = "other"
    elif fault == "unread_image":
        native["gateway"]["image_digest"] = "latest"
    elif fault == "missing_pods":
        native["gateway"]["pod_uids"] = []
    elif fault == "duplicate_pods":
        native["gateway"]["pod_uids"] = ["pod-one", "pod-one"]
    elif fault == "stale":
        native["gateway"]["observed_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    elif fault == "after_receipt":
        native["gateway"]["observed_at"] = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    elif fault == "missing_wheel":
        native["gateway"].pop("wheel_sha256")
    elif fault == "wrong_wheel":
        native["gateway"]["wheel_sha256"] = "0" * 64
    path.write_text(json.dumps(native))
    if fault == "accepted":
        report = emitter.emit(environment)
        assert report["gateway_revision"] == SOURCE
        assert not any(item.startswith("gateway_revision:") for item in report["unresolved"])
        check = next(item for item in report["live_checks"] if item["id"] == "runtime-revisions")
        assert check["outcome"] == "passed" and check["evidence_verified"] is True
        assert check["source_revision"] == check["runtime_revision"] == SOURCE
        assert check["run_id"] == "456" and check["run_attempt"] == "2"
        assert report["native_observation"]["sha256"] in check["evidence_ref"]
        assert "live_checks/runtime-revisions:not-observed" not in report["unresolved"]
        delivery = json.loads((Path(environment["RUNNER_TEMP"]) /
                               "delivery-evidence/delivery-evidence.json").read_text())
        assert {item["name"] for item in delivery["required_checks"] if item["evidence_verified"]} == {
            "real-threat-model", "real-static-review", "runtime-revisions", "source-pinning", "same-wheel"}
        assert "live_checks/same-wheel:not-observed" not in report["unresolved"]
        assert report["unresolved"]
    else:
        with pytest.raises(ValueError):
            emitter.emit(environment)
        assert not (Path(environment["RUNNER_TEMP"]) /
                    "l5-security-qualification/qualification.json").exists()


@pytest.mark.parametrize("fault", ["accepted", "no_scope", "missing", "public", "changed",
                                     "foreign_trust", "missing_trust", "extra_trust",
                                     "wrong_role", "wrong_queue", "foreign_account", "wrong_action"])
def test_installed_iam_scope_receipt_is_bound_but_does_not_claim_least_privilege(private_qualification, fault):
    environment, path, native = private_qualification
    environment.update(AWS_REGION="us-east-1", L5_MUTATION_ROLE_ARN=
                       "arn:aws:iam::123456789012:role/fictional-deployment")
    account = "123456789012"
    region = "us-east-1"
    target = "dev"
    scope = {"roles": {}, "queues": {}}
    for label in ("worker", "repair"):
        name = f"adp-{target}-l5-{label}"
        scope["roles"][label] = {"role_arn": f"arn:aws:iam::{account}:role/{name}",
                                 "policy_name": name,
                                 "boundary_arn": f"arn:aws:iam::{account}:policy/fictional-boundary",
                                 "trust_policy": {"Version": "2012-10-17", "Statement": [
                                     {"Effect": "Allow", "Action": "sts:AssumeRole",
                                      "Principal": {"Service": "lambda.amazonaws.com"}}]}}
    for label, suffix, retention in (("work", "", 345600), ("dead_letter", "-dlq", 1209600)):
        scope["queues"][label] = {"arn": f"arn:aws:sqs:{region}:{account}:adp-{target}-l5-work{suffix}",
                                   "retention_seconds": retention}
    receipt_path = path.parent / "installed-456-2.json"
    packet = {"action": "status", "result": "passed", "observed": copy.deepcopy(native["installed"]),
              "scope": scope}
    receipt_path.write_text(json.dumps(packet))
    receipt_path.chmod(0o600)
    digest = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    if fault != "no_scope":
        native["installed"]["scope_receipt"] = (
            f"private://security/installed/{receipt_path.name}#sha256:{digest}")
    if fault == "missing":
        receipt_path.unlink()
    elif fault == "public":
        receipt_path.chmod(0o644)
    elif fault == "changed":
        receipt_path.write_text("{}")
    elif fault in {"wrong_role", "wrong_queue", "foreign_account", "wrong_action", "foreign_trust", "missing_trust", "extra_trust"}:
        if fault == "wrong_role":
            scope["roles"]["worker"]["role_arn"] = "arn:aws:iam::123456789012:role/another-worker"
        elif fault == "wrong_queue":
            scope["queues"]["work"]["retention_seconds"] = 1
        elif fault == "foreign_account":
            scope["roles"]["repair"]["boundary_arn"] = (
                "arn:aws:iam::999999999999:policy/fictional-boundary")
        elif fault == "foreign_trust":
            scope["roles"]["worker"]["trust_policy"]["Statement"][0]["Principal"] = (
                {"AWS": "arn:aws:iam::123456789012:root"})
        elif fault == "missing_trust":
            scope["roles"]["repair"].pop("trust_policy")
        elif fault == "extra_trust":
            scope["roles"]["repair"]["trust_policy"]["Statement"].append(
                {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": {"Service": "ec2.amazonaws.com"}})
        else:
            packet["action"] = "rollout"
        receipt_path.write_text(json.dumps(packet))
        native["installed"]["scope_receipt"] = (
            f"private://security/installed/{receipt_path.name}#sha256:"
            f"{hashlib.sha256(receipt_path.read_bytes()).hexdigest()}")
    path.write_text(json.dumps(native))
    if fault in {"accepted", "no_scope"}:
        report = emitter.emit(environment)
        assert report["native_observation"]["installed_scope_sha256"] == (digest if fault == "accepted" else None)
        assert "live_checks/least-privilege:not-observed" in report["unresolved"]
        assert account not in json.dumps(report)
    else:
        with pytest.raises((OSError, ValueError)):
            emitter.emit(environment)
        assert not (Path(environment["RUNNER_TEMP"]) /
                    "l5-security-qualification/qualification.json").exists()


@pytest.mark.parametrize("fault", ["accepted", "missing", "shared_run", "foreign_scope", "changed_source",
                                     "unread_source", "stale", "after_receipt", "wrong_revision"])
def test_setup_parity_requires_two_current_selected_product_readbacks(private_qualification, fault):
    environment, path, native = private_qualification
    fixture_path = Path(environment["PRIVATE_FIXTURE"])
    fixture = json.loads(fixture_path.read_text())
    native["setup"] = []
    for index, case in enumerate(fixture["cases"]):
        sources = case["assessments"][0]["input"]["sources"]
        case["selected"] = {"scope": case["scope"], "sources": sources}
        case["run_input"] = {"sources": sources}
        native["setup"].append({"scope": case["scope"], "sources": sources,
                                "run_id": f"setup-run-{index}", "configuration_revision": 3,
                                "binding_revision": 2, "run_revision": 1,
                                "observed_at": native["observed_at"]})
    if fault == "missing":
        native["setup"].pop()
    elif fault == "shared_run":
        native["setup"][1]["run_id"] = native["setup"][0]["run_id"]
    elif fault == "foreign_scope":
        native["setup"][0]["scope"] = {**native["setup"][0]["scope"], "product_id": "foreign"}
    elif fault == "changed_source":
        native["setup"][0]["sources"] = [{"record_revision": "foreign"}]
    elif fault == "unread_source":
        fixture["cases"][0]["selected"].pop("sources")
    elif fault == "stale":
        native["setup"][0]["observed_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    elif fault == "after_receipt":
        native["setup"][0]["observed_at"] = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    elif fault == "wrong_revision":
        native["setup"][0]["configuration_revision"] = 0
    fixture_path.write_text(json.dumps(fixture))
    environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    native["fixture_digest"] = environment["L5_FIXTURE_DIGEST"]
    retain_selector_receipts(environment, native)
    path.write_text(json.dumps(native))
    if fault == "accepted":
        report = emitter.emit(environment)
        assert report["native_observation"]["setup_products"] == 2
        assert {row["id"] for row in report["live_checks"] if row["outcome"] == "passed"} == {
            "real-threat-model", "real-static-review", "ui-cli-mcp-parity", "source-pinning"}
        assert report["unresolved"] and "live_checks/source-pinning:not-observed" not in report["unresolved"]
        assert "live_checks/owned-cleanup:not-observed" in report["unresolved"]
    else:
        with pytest.raises(ValueError):
            emitter.emit(environment)


@pytest.mark.parametrize("fault", ["accepted", "missing", "wrong_scope", "wrong_result",
                                     "unqualified_result", "changed_source", "changed_capability", "shared_agent",
                                     "stale", "after_receipt", "missing_selection"])
def test_context_snapshot_requires_selected_qualified_result_but_cannot_prove_replay(private_qualification, fault):
    environment, path, native = private_qualification
    fixture_path = Path(environment["PRIVATE_FIXTURE"])
    fixture = json.loads(fixture_path.read_text())
    record = native["products"][0]["assessments"][1]
    selection = {"scope": fixture["cases"][0]["scope"], "run_id": record["run_id"],
                 "result_id": record["result_id"], "publication_id": "publication-example",
                 "sources": record["sources"], "assessment_plan": {
                     "input": {"capability": record["capability"], "sources": record["sources"]}}}
    fixture["cases"][0]["context_reuse"] = selection
    native["context"] = {field: copy.deepcopy(selection[field]) for field in (
        "scope", "run_id", "result_id", "publication_id", "sources")}
    native["context"].update(agent_run_id="later-agent-example", observed_at=native["observed_at"])
    if fault == "missing":
        native.pop("context")
    elif fault == "wrong_scope":
        native["context"]["scope"] = fixture["cases"][1]["scope"]
    elif fault == "wrong_result":
        native["context"]["result_id"] = "foreign-result"
    elif fault == "unqualified_result":
        selection["result_id"] = native["context"]["result_id"] = "foreign-result"
    elif fault == "changed_source":
        selection["sources"] = native["context"]["sources"] = [{"record_revision": "foreign"}]
        selection["assessment_plan"]["input"]["sources"] = selection["sources"]
    elif fault == "changed_capability":
        selection["assessment_plan"]["input"]["capability"] = "code_review"
    elif fault == "shared_agent":
        native["context"]["agent_run_id"] = record["run_id"]
    elif fault == "stale":
        native["context"]["observed_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    elif fault == "after_receipt":
        native["context"]["observed_at"] = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    elif fault == "missing_selection":
        fixture["cases"][0].pop("context_reuse")
    fixture_path.write_text(json.dumps(fixture))
    environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    native["fixture_digest"] = environment["L5_FIXTURE_DIGEST"]
    retain_selector_receipts(environment, native)
    path.write_text(json.dumps(native))
    if fault == "accepted":
        report = emitter.emit(environment)
        assert report["native_observation"]["context_observations"] == 1
        assert "native-readbacks:context-replay-not-independently-evaluated" in report["unresolved"]
        assert all(item["outcome"] != "passed" for item in report["scenarios"])
    else:
        with pytest.raises(ValueError):
            emitter.emit(environment)
        assert not (Path(environment["RUNNER_TEMP"]) / "l5-security-qualification/qualification.json").exists()


@pytest.mark.parametrize("fault", ["accepted", "missing", "public", "changed", "forged_hash",
                                     "wrong_key", "other_run", "foreign_agent", "wrong_count", "no_replay"])
def test_context_replay_emission_requires_selected_private_attempts(private_qualification, fault):
    readback_spec = importlib.util.spec_from_file_location(
        "security_replay_readback", SCRIPT.with_name("qualify-security-readbacks.py"))
    readback = importlib.util.module_from_spec(readback_spec)
    readback_spec.loader.exec_module(readback)
    environment, path, native = private_qualification
    fixture_path = Path(environment["PRIVATE_FIXTURE"])
    fixture = json.loads(fixture_path.read_text())
    record = native["products"][0]["assessments"][1]
    scope = fixture["cases"][0]["scope"]
    journal = path.parent / "context-attempts.jsonl"
    selection = {"scope": scope, "run_id": record["run_id"], "result_id": record["result_id"],
                 "publication_id": "publication-example", "sources": record["sources"],
                 "assessment_plan": {"input": {"capability": record["capability"],
                                               "sources": record["sources"]}},
                 "journal_file": str(journal), "query_receipt": "later-query-example", "max_attempts": 3}
    fixture["cases"][0]["context_reuse"] = selection
    key = f"{scope['tenant_id']}/{record['result_id']}/1/product-context/1.0"
    agent_run_id = "later-agent-example"
    entries = []
    for repeat in range(2):
        for status in ("prepared", "acknowledged"):
            entries.append({"operation": "context_replay", "status": status,
                            "attempt_id": f"replay-{repeat}-0", "work_id": selection["publication_id"],
                            "idempotency_key": key, "run_id": record["run_id"],
                            "native_job_id": None, "effect_id": None,
                            "observed_at": native["observed_at"]})
    for status, work_id in (("prepared", selection["publication_id"]), ("acknowledged", agent_run_id)):
        entries.append({"operation": "later_agent_query", "status": status, "attempt_id": "later-query",
                        "work_id": work_id, "idempotency_key": selection["query_receipt"],
                        "native_job_id": None, "effect_id": None,
                        "run_id": record["run_id"], "observed_at": native["observed_at"]})
    journal.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    journal.chmod(0o600)
    publication = {"publication_id": selection["publication_id"], "result_id": record["result_id"],
                   "result_version": 1, "destination": "product-context", "schema_version": "1.0",
                   "contribution_hash": "a" * 64, "payload": {"claims": []}}
    replay = readback.verify_replay_journal(fixture["cases"][0], publication, agent_run_id)
    native["context"] = {"scope": scope, "run_id": record["run_id"], "result_id": record["result_id"],
                         "publication_id": selection["publication_id"], "sources": record["sources"],
                         "agent_run_id": agent_run_id, "observed_at": native["observed_at"], "replay": replay}
    if fault == "missing":
        journal.unlink()
    elif fault == "public":
        journal.chmod(0o644)
    elif fault == "changed":
        journal.write_text(journal.read_text() + "{}\n")
    elif fault == "wrong_key":
        entries[2]["idempotency_key"] = "other-key"
        journal.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    elif fault == "forged_hash":
        entries[2]["idempotency_key"] = "other-key"
        journal.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        replay["sha256"] = hashlib.sha256(journal.read_bytes()).hexdigest()
    elif fault == "other_run":
        entries[1]["run_id"] = "another-run"
        journal.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    elif fault == "foreign_agent":
        entries[-1]["work_id"] = "another-agent"
        journal.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    elif fault == "wrong_count":
        replay["attempts"] = 4
    elif fault == "no_replay":
        native["context"]["replay"] = None
    fixture_path.write_text(json.dumps(fixture))
    environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    native["fixture_digest"] = environment["L5_FIXTURE_DIGEST"]
    retain_selector_receipts(environment, native)
    path.write_text(json.dumps(native))
    if fault in {"accepted", "no_replay"}:
        report = emitter.emit(environment)
        qualified = {item["id"] for item in report["live_checks"] if item["outcome"] == "passed"}
        assert ("context-reuse" in qualified) is (fault == "accepted")
        assert report["native_observation"]["context_replays"] == (2 if fault == "accepted" else 0)
        assert ("native-readbacks:context-replay-not-independently-evaluated" in report["unresolved"]) is (
            fault == "no_replay")
        assert "native-readbacks:required-scenarios-not-evaluated" in report["unresolved"]
        delivery = json.loads((Path(environment["RUNNER_TEMP"]) /
                               "delivery-evidence/delivery-evidence.json").read_text())
        assert ("context-reuse" in {check["name"] for check in delivery["required_checks"]
                                    if check["evidence_verified"]}) is (fault == "accepted")
    else:
        with pytest.raises((ValueError, OSError)):
            emitter.emit(environment)
        assert not (Path(environment["RUNNER_TEMP"]) / "l5-security-qualification/qualification.json").exists()


@pytest.mark.parametrize("fault", ["missing", "wrong_source", "wrong_run", "expired", "foreign_worker",
                                   "shared_product", "missing_result", "public_file", "symlink",
                                   "missing_cleanup", "stale_cleanup", "unread_cleanup", "fabricated_cleanup",
                                   "foreign_cleanup", "changed_packet", "missing_packet", "public_packet",
                                   "forged_packet", "missing_fixture", "changed_fixture", "foreign_product",
                                   "foreign_source", "duplicate_job", "foreign_selected_release",
                                   "shared_run", "missing_selected_plan", "public_fixture", "linked_fixture",
                                   "malformed_selected_plan", "missing_source_digest", "unread_source",
                                   "missing_snapshot_digest", "wrong_snapshot_digest", "forged_source_digest",
                                   "changed_review", "missing_review", "public_review", "forged_review",
                                   "wrong_review_count"])
def test_native_partial_readback_rejects_missing_wrong_source_or_tampered_receipt(private_qualification, fault):
    environment, path, native = private_qualification
    if fault == "missing":
        path.unlink()
    elif fault == "public_file":
        path.chmod(0o644)
    elif fault == "symlink":
        path.unlink()
        path.symlink_to(path.parent / "missing-file")
    else:
        if fault == "wrong_source":
            native["source_revision"] = "f" * 40
        elif fault == "wrong_run":
            native["run_attempt"] = "9"
        elif fault == "expired":
            native["observed_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        elif fault == "foreign_worker":
            native["installed"]["worker_sha256"] = "foreign"
        elif fault == "shared_product":
            native["products"][1]["scope"]["product_id"] = "product-one"
        elif fault == "missing_result":
            native["products"][0]["assessments"].pop()
        elif fault == "missing_cleanup":
            native.pop("cleanup")
        elif fault == "stale_cleanup":
            native["cleanup"]["observed_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        elif fault == "unread_cleanup":
            native["cleanup"]["readback"] = []
        elif fault == "fabricated_cleanup":
            native["cleanup"]["owned_absent"] = True
        elif fault == "foreign_cleanup":
            native["cleanup"]["readback"][0] = native["cleanup"]["readback"][0].replace("456-2", "456-3")
        elif fault == "changed_packet":
            (path.parent / "security-cleanup-456-2.json").write_text('{"run_id":"other"}\n')
        elif fault == "forged_packet":
            changed = {"run_id": "recovery-run", "ownership": {"owned": "scenario_owned",
                       "linked": "customer_linked"}, "resources": {"owned": "present", "linked": "retained"}}
            content = json.dumps(changed, sort_keys=True, separators=(",", ":")) + "\n"
            (path.parent / "security-cleanup-456-2.json").write_text(content)
            native["cleanup"]["readback"] = [native["cleanup"]["readback"][0].split("#", 1)[0]
                                             + "#sha256:" + hashlib.sha256(content.encode()).hexdigest()]
        elif fault == "missing_fixture":
            Path(environment["PRIVATE_FIXTURE"]).unlink()
        elif fault == "changed_fixture":
            Path(environment["PRIVATE_FIXTURE"]).write_text("{}")
        elif fault == "foreign_product":
            native["products"][0]["scope"]["product_id"] = "other-product"
        elif fault == "foreign_source":
            native["products"][0]["assessments"][0]["sources"] = [{"record_revision": "foreign"}]
        elif fault == "duplicate_job":
            native["products"][0]["assessments"][0]["native_job_ref"] = (
                native["products"][1]["assessments"][0]["native_job_ref"])
        elif fault == "foreign_selected_release":
            fixture = json.loads(Path(environment["PRIVATE_FIXTURE"]).read_text())
            fixture["release"]["worker"] = DEFINITION
            Path(environment["PRIVATE_FIXTURE"]).write_text(json.dumps(fixture))
            environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(
                Path(environment["PRIVATE_FIXTURE"]).read_bytes()).hexdigest()
            native["fixture_digest"] = environment["L5_FIXTURE_DIGEST"]
            retain_selector_receipts(environment, native)
        elif fault == "shared_run":
            native["products"][1]["assessments"][0]["run_id"] = native["products"][0]["assessments"][0]["run_id"]
        elif fault == "missing_selected_plan":
            fixture = json.loads(Path(environment["PRIVATE_FIXTURE"]).read_text())
            fixture["cases"][0]["assessments"].pop()
            Path(environment["PRIVATE_FIXTURE"]).write_text(json.dumps(fixture))
            environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(
                Path(environment["PRIVATE_FIXTURE"]).read_bytes()).hexdigest()
            native["fixture_digest"] = environment["L5_FIXTURE_DIGEST"]
            retain_selector_receipts(environment, native)
        elif fault == "malformed_selected_plan":
            fixture = json.loads(Path(environment["PRIVATE_FIXTURE"]).read_text())
            fixture["cases"][0]["assessments"][0]["input"] = ["untrusted"]
            Path(environment["PRIVATE_FIXTURE"]).write_text(json.dumps(fixture))
            environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(
                Path(environment["PRIVATE_FIXTURE"]).read_bytes()).hexdigest()
            native["fixture_digest"] = environment["L5_FIXTURE_DIGEST"]
            retain_selector_receipts(environment, native)
        elif fault == "missing_source_digest":
            native["products"][0]["assessments"][0].pop("source_sha256")
        elif fault == "unread_source":
            native["products"][0]["assessments"][0]["source_sha256"] = ["not-a-sha256"]
        elif fault == "missing_snapshot_digest":
            native["products"][0]["assessments"][0].pop("snapshot_sha256")
        elif fault == "wrong_snapshot_digest":
            native["products"][0]["assessments"][0]["snapshot_sha256"] = ["f" * 64]
        elif fault == "forged_source_digest":
            native["products"][0]["assessments"][0]["source_sha256"] = ["f" * 64]
        elif fault == "wrong_review_count":
            native["products"][0]["assessments"][0]["reviewed"] = 1
        elif fault in {"changed_review", "missing_review", "public_review", "forged_review"}:
            record = native["products"][0]["assessments"][0]
            review_path = path.parent / (record["run_id"] + "-rubric.json")
            if fault == "missing_review":
                review_path.unlink()
            elif fault == "public_review":
                review_path.chmod(0o644)
            else:
                rubric = json.loads(review_path.read_text())
                rubric["run_id"] = "foreign-run"
                review_path.write_text(json.dumps(rubric))
                if fault == "forged_review":
                    record["rubric_sha256"] = hashlib.sha256(review_path.read_bytes()).hexdigest()
        elif fault == "public_fixture":
            Path(environment["PRIVATE_FIXTURE"]).chmod(0o644)
        elif fault == "linked_fixture":
            Path(environment["PRIVATE_FIXTURE"]).unlink()
            Path(environment["PRIVATE_FIXTURE"]).symlink_to(path.parent / "security-cleanup-456-2.json")
        elif fault == "missing_packet":
            (path.parent / "security-cleanup-456-2.json").unlink()
        elif fault == "public_packet":
            (path.parent / "security-cleanup-456-2.json").chmod(0o644)
        path.write_text(json.dumps(native))
    with pytest.raises((OSError, ValueError)):
        emitter.emit(environment)
    assert not (Path(environment["RUNNER_TEMP"]) / "l5-security-qualification/qualification.json").exists()


@pytest.mark.parametrize("fault", ["accepted", "missing_fixture", "wrong_revision", "public_fixture",
                                     "public_evidence", "foreign_service"])
def test_preflight_checks_selected_release_without_native_effects(private_qualification, fault):
    environment, _, _ = private_qualification
    if fault == "missing_fixture":
        environment["PRIVATE_FIXTURE"] = str(Path(environment["RUNNER_TEMP"]) / "missing.json")
    elif fault == "wrong_revision":
        environment["REVIEWED_SOURCE_SHA"] = "f" * 40
    elif fault == "public_fixture":
        Path(environment["PRIVATE_FIXTURE"]).chmod(0o644)
    elif fault == "public_evidence":
        Path(environment["PRIVATE_EVIDENCE_DIR"]).chmod(0o755)
    elif fault == "foreign_service":
        environment["L5_SERVICE"] = "devops"
    result = subprocess.run([sys.executable, str(SCRIPT)],
                            env={**os.environ, **environment, "L5_QUALIFICATION_PREFLIGHT": "1"},
                            capture_output=True, text=True)
    assert (result.returncode == 0) is (fault == "accepted")
    if fault != "accepted":
        assert "Security qualification preflight unavailable" in result.stderr
    assert not list(Path(environment["RUNNER_TEMP"]).iterdir())


def test_native_emitter_feeds_strict_publication_without_upgrading_outcomes(private_qualification):
    environment, _, _ = private_qualification
    report = emitter.emit(environment)
    publication_script = SCRIPT.with_name("publish-release-reports.py")
    publication_spec = importlib.util.spec_from_file_location("security_publication_contract", publication_script)
    publication = importlib.util.module_from_spec(publication_spec)
    publication_spec.loader.exec_module(publication)
    qualification = Path(environment["RUNNER_TEMP"]) / "l5-security-qualification/qualification.json"
    raw, digest = publication.private_json(qualification)
    assert raw == report
    normalized = publication.normalized_report(raw, emitter.release_identity(environment), digest)
    check = next(row for row in normalized["live_checks"] if row["id"] == "real-threat-model")
    assert check["outcome"] == "passed" and check["evidence_verified"] is True
    assert check["evidence_ref"].startswith("sha256:" + digest)
    assert "native_observation" not in normalized
    assert normalized["unresolved"] and not publication.complete(normalized)


@pytest.mark.parametrize("fault", ["missing_product_run", "missing_native_job", "fabricated_native_finding"])
def test_absent_or_fabricated_assessment_cannot_reach_publication(private_qualification, readbacks,
                                                                    monkeypatch, fault):
    environment, readback_path, _ = private_qualification
    case, product_rows, native_jobs = readbacks
    readback_path.unlink()
    base = "/v1/products/product-example/projects/project-example/services/security"
    if fault == "missing_product_run":
        product_rows.pop(base + "/runs/code_review-run")
    elif fault == "missing_native_job":
        native_jobs.pop("threat_model-run")
    else:
        native_jobs["code_review-run"]["findings"] = [{
            "native_id": "fabricated-finding", "claim": "fictional invented claim"}]
    with pytest.raises((ValueError, KeyError)):
        reader.assess_case(case, product_rows.__getitem__,
                           lambda run, provider: native_jobs[run["run_id"]],
                           lambda run: [{"source_ref": source, "content": SOURCE_CONTENT}
                                        for source in run["input"]["sources"]])
    assert not readback_path.exists()
    with pytest.raises((ValueError, OSError)):
        emitter.emit(environment)
    publication_script = SCRIPT.with_name("publish-release-reports.py")
    publication_spec = importlib.util.spec_from_file_location("security_publication_contract", publication_script)
    publication = importlib.util.module_from_spec(publication_spec)
    publication_spec.loader.exec_module(publication)
    monkeypatch.setattr(publication, "installed_revision", lambda *args: pytest.fail("unobserved native work"))
    with pytest.raises((ValueError, OSError)):
        publication.main(environment)
    assert not (Path(environment["RUNNER_TEMP"]) / "l5-published-reports").exists()


def test_partial_native_observation_stages_diagnostics_but_not_delivery(private_qualification, monkeypatch):
    environment, _, _ = private_qualification
    emitter.emit(environment)
    publication_script = SCRIPT.with_name("publish-release-reports.py")
    publication_spec = importlib.util.spec_from_file_location("security_publication_contract", publication_script)
    publication = importlib.util.module_from_spec(publication_spec)
    publication_spec.loader.exec_module(publication)
    monkeypatch.setattr(publication, "installed_revision", lambda *args: pytest.fail("incomplete native work"))
    with pytest.raises(ValueError, match="incomplete"):
        publication.main(environment)
    stage = Path(environment["RUNNER_TEMP"]) / "l5-published-reports"
    metadata = json.loads((stage / "l5-security-qualification/qualification.json").read_text())
    assert metadata["unresolved"] and "native_observation" not in metadata
    assert not (stage / "delivery-evidence").exists()


@pytest.mark.parametrize("fault", ["wrong_source", "wrong_definition", "wrong_run",
                                     "wrong_check_revision", "incompatible_schema", "invalid_checks"])
def test_strict_publisher_rejects_rehashed_foreign_or_incompatible_security_report(
        private_qualification, monkeypatch, fault):
    environment, _, _ = private_qualification
    emitter.emit(environment)
    qualification = Path(environment["RUNNER_TEMP"]) / "l5-security-qualification/qualification.json"
    report = json.loads(qualification.read_text())
    if fault == "wrong_source":
        report["source_revision"] = "f" * 40
    elif fault == "wrong_definition":
        report["definition_revision"] = "f" * 40
    elif fault == "wrong_run":
        report["run_attempt"] = "3"
    elif fault == "wrong_check_revision":
        report["live_checks"][0]["source_revision"] = "f" * 40
    elif fault == "incompatible_schema":
        report["schema_version"] = 2
    else:
        report["live_checks"] = {"outcome": "passed", "evidence_verified": True}
    qualification.write_text(json.dumps(report))
    publication_script = SCRIPT.with_name("publish-release-reports.py")
    publication_spec = importlib.util.spec_from_file_location("security_publication_contract", publication_script)
    publication = importlib.util.module_from_spec(publication_spec)
    publication_spec.loader.exec_module(publication)
    monkeypatch.setattr(publication, "installed_revision", lambda *args: pytest.fail("invalid native evaluation"))
    with pytest.raises(ValueError):
        publication.main(environment)
    assert not (Path(environment["RUNNER_TEMP"]) / "l5-published-reports").exists()


@pytest.mark.parametrize("fault", ["accepted", "missing", "foreign_run", "foreign_scope", "invalid_attempts",
                                     "stale", "changed_journal", "foreign_source", "public_journal",
                                     "supplied_success"])
def test_recovery_attempt_evidence_requires_private_journal_and_selected_run(private_qualification, fault):
    environment, path, native = private_qualification
    fixture_path = Path(environment["PRIVATE_FIXTURE"])
    fixture = json.loads(fixture_path.read_text())
    selected = fixture["cases"][0]
    sources = selected["assessments"][0]["input"]["sources"]
    selected["selected"] = {"sources": sources, "binding_id": "binding-example"}
    journal = path.parent / "recovery-attempts.jsonl"
    journal.write_text('{"attempt_id":"attempt-example"}\n')
    journal.chmod(0o600)
    selected["recovery"] = {"scope": selected["scope"], "sources": sources,
                            "binding_id": "binding-example", "max_attempts": 3,
                            "journal_file": str(journal)}
    fixture_path.write_text(json.dumps(fixture))
    environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    native["fixture_digest"] = environment["L5_FIXTURE_DIGEST"]
    retain_selector_receipts(environment, native)
    recovery = {"run_id": native["cleanup"]["run_id"], "scope": selected["scope"],
                "native_job_ref": {"record_id": "fictional-job"}, "publication_id": "fictional-publication",
                "attempts": 1, "journal_sha256": hashlib.sha256(journal.read_bytes()).hexdigest(),
                "observed_at": native["observed_at"]}
    if fault != "missing":
        native["recovery"] = recovery
    if fault == "foreign_run":
        recovery["run_id"] = "other-run"
    elif fault == "foreign_scope":
        recovery["scope"] = fixture["cases"][1]["scope"]
    elif fault == "invalid_attempts":
        recovery["attempts"] = 4
    elif fault == "stale":
        recovery["observed_at"] = "2026-01-01T00:00:00Z"
    elif fault == "changed_journal":
        journal.write_text('different private attempt\n')
    elif fault == "foreign_source":
        selected["recovery"]["sources"] = [{"record_revision": "different"}]
        fixture_path.write_text(json.dumps(fixture))
        environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
        native["fixture_digest"] = environment["L5_FIXTURE_DIGEST"]
        retain_selector_receipts(environment, native)
    elif fault == "public_journal":
        journal.chmod(0o644)
    elif fault == "supplied_success":
        (path.parent / "independent-observations.json").write_text(json.dumps({
            "live_checks": [{"id": "durable-recovery", "outcome": "passed", "evidence_verified": True}]}))
    path.write_text(json.dumps(native))
    if fault not in {"accepted", "missing", "supplied_success"}:
        with pytest.raises((ValueError, OSError)):
            emitter.emit(environment)
        assert not (Path(environment["RUNNER_TEMP"]) / "delivery-evidence/delivery-evidence.json").exists()
        return
    report = emitter.emit(environment)
    delivery = json.loads((Path(environment["RUNNER_TEMP"]) /
                           "delivery-evidence/delivery-evidence.json").read_text())
    assert report["native_observation"]["recovery_attempts"] == (0 if fault == "missing" else 1)
    verified = next(item for item in report["live_checks"] if item["id"] == "durable-recovery")
    assert verified["outcome"] == "incomplete" and verified["evidence_verified"] is False
    assert any(item["name"] == "durable-recovery" and item["evidence_verified"] is False
               for item in delivery["required_checks"])
    assert "live_checks/durable-recovery:not-observed" in report["unresolved"]


@pytest.mark.parametrize("fault", ["accepted", "unselected", "foreign_owned", "foreign_linked",
                                     "missing_ack", "foreign_attempt", "missing_journal_run"])
def test_owned_cleanup_requires_selected_assets_and_acknowledged_work(private_qualification, fault):
    environment, path, native = private_qualification
    fixture_path = Path(environment["PRIVATE_FIXTURE"])
    fixture = json.loads(fixture_path.read_text())
    selected = fixture["cases"][0]
    sources = selected["assessments"][0]["input"]["sources"]
    selected["selected"] = {"sources": sources, "binding_id": "binding-example"}
    journal = path.parent / "owned-recovery.jsonl"
    run_id = native["cleanup"]["run_id"]
    entries = [{"operation": "cleanup", "status": status, "attempt_id": "cleanup",
                "run_id": run_id, "work_id": run_id, "observed_at": native["cleanup"]["observed_at"]}
               for status in ("prepared", "acknowledged")]
    if fault == "missing_ack":
        entries.pop()
    elif fault == "foreign_attempt":
        entries[1]["work_id"] = "other-run"
    elif fault == "missing_journal_run":
        entries[1].pop("run_id")
    journal.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    journal.chmod(0o600)
    selected["recovery"] = {"scope": selected["scope"], "sources": sources,
                            "binding_id": "binding-example", "max_attempts": 3,
                            "journal_file": str(journal)}
    if fault != "unselected":
        selected["recovery"].update({"owned": ["owned"], "linked": ["linked"]})
    if fault == "foreign_owned":
        selected["recovery"]["owned"] = ["other-resource"]
    elif fault == "foreign_linked":
        selected["recovery"]["linked"] = ["other-resource"]
    fixture_path.write_text(json.dumps(fixture))
    environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    native["fixture_digest"] = environment["L5_FIXTURE_DIGEST"]
    retain_selector_receipts(environment, native)
    native["recovery"] = {"run_id": run_id, "scope": selected["scope"],
                          "native_job_ref": {"record_id": "fictional-job"},
                          "publication_id": "fictional-publication", "attempts": 1,
                          "journal_sha256": hashlib.sha256(journal.read_bytes()).hexdigest(),
                          "observed_at": native["observed_at"]}
    path.write_text(json.dumps(native))
    if fault in {"foreign_owned", "foreign_linked", "missing_ack", "foreign_attempt",
                 "missing_journal_run"}:
        with pytest.raises(ValueError):
            emitter.emit(environment)
        assert not (Path(environment["RUNNER_TEMP"]) / "l5-security-qualification/qualification.json").exists()
        return
    report = emitter.emit(environment)
    assert report["cleanup"]["outcome"] == ("incomplete" if fault == "unselected" else "passed")
    assert next(item for item in report["live_checks"] if item["id"] == "owned-cleanup")[
        "outcome"] == ("incomplete" if fault == "unselected" else "passed")


@pytest.mark.parametrize("fault", ["accepted", "accepted_with_denials", "claimed_revocation", "foreign_denials",
                                     "missing_snapshot", "modified_snapshot", "rehashed_foreign",
                                     "missing_restart", "foreign_attempt", "late_capture", "public_snapshot"])
def test_durable_recovery_check_requires_fault_time_and_terminal_readbacks(private_qualification, fault):
    environment, path, native = private_qualification
    fixture_path = Path(environment["PRIVATE_FIXTURE"])
    fixture = json.loads(fixture_path.read_text())
    selected = fixture["cases"][0]
    sources = selected["assessments"][0]["input"]["sources"]
    selected_input = {"sources": sources, "binding_id": "binding-example", "capability": "code_review"}
    journal_path = path.parent / "selected-recovery.jsonl"
    before = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    restart = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
    run_id = native["cleanup"]["run_id"]
    entries = [
        {"operation": "fault_arm", "status": "acknowledged", "attempt_id": "fault_arm",
         "run_id": run_id, "work_id": "receipt-example", "observed_at": before},
        {"operation": "submit", "status": "accepted", "attempt_id": "attempt-example",
         "run_id": run_id, "work_id": "work-example", "observed_at": before},
        {"operation": "worker_restart", "status": "acknowledged", "attempt_id": "worker_restart",
         "run_id": run_id, "work_id": "receipt-example", "observed_at": restart},
        {"operation": "cleanup", "status": "acknowledged", "attempt_id": "cleanup",
         "run_id": run_id, "work_id": run_id, "observed_at": native["cleanup"]["observed_at"]},
    ]
    if fault == "missing_restart":
        entries.pop(2)
    elif fault == "foreign_attempt":
        entries[1]["run_id"] = "foreign-run"
    if fault in {"accepted_with_denials", "claimed_revocation"}:
        entries.insert(-1, {**entries[-1], "status": "prepared"})
    journal_path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    journal_path.chmod(0o600)
    selected["selected"] = {"sources": sources, "binding_id": "binding-example"}
    selected["recovery"] = {"scope": selected["scope"], "sources": sources,
                            "input": selected_input, "binding_id": "binding-example",
                            "receipt": "receipt-example", "max_attempts": 3,
                            "journal_file": str(journal_path)}
    if fault in {"accepted_with_denials", "claimed_revocation", "foreign_denials"}:
        selected["recovery"].update({"foreign_product_id": "foreign-product",
                                     "revoked_token_env": "REVOKED_TOKEN"})
    if fault in {"accepted_with_denials", "claimed_revocation"}:
        selected["recovery"].update({"owned": ["owned"], "linked": ["linked"]})
    fixture_path.write_text(json.dumps(fixture))
    environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    native["fixture_digest"] = environment["L5_FIXTURE_DIGEST"]
    retain_selector_receipts(environment, native)
    snapshot = {"receipt": "receipt-example", "run": {"run_id": run_id, "scope": selected["scope"],
                "input": selected_input, "attempts": [{"attempt_id": "attempt-example"}]},
                "publication": {"state": "retry_wait", "publication_id": "publication-example"},
                "observed_at": before}
    snapshot_path = Path(str(journal_path) + ".fault.json")
    snapshot_path.write_text(json.dumps(snapshot))
    snapshot_path.chmod(0o600)
    recovery = {"run_id": run_id, "scope": selected["scope"], "native_job_ref": {"record_id": "job-example"},
                "publication_id": "publication-example", "attempts": 1,
                "journal_sha256": hashlib.sha256(journal_path.read_bytes()).hexdigest(),
                "transition_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
                "observed_at": native["observed_at"]}
    native["recovery"] = recovery
    if fault in {"accepted_with_denials", "claimed_revocation", "foreign_denials"}:
        native["scope_denials"] = {
            "run_id": run_id if fault != "foreign_denials" else "another-run",
            "scope": selected["scope"], "observed_at": native["observed_at"],
            "probes": [{"case": label, "status": 403, "disclosed_resources": []}
                       for label in ("foreign_product", "foreign_tenant", "revoked_access")],
        }
    if fault == "missing_snapshot":
        snapshot_path.unlink()
    if fault == "claimed_revocation":
        native["grant_revocation"] = {"outcome": "passed", "evidence_verified": True}
    elif fault == "modified_snapshot":
        snapshot_path.write_text('different private snapshot\n')
    elif fault == "rehashed_foreign":
        snapshot["run"]["run_id"] = "foreign-run"
        snapshot_path.write_text(json.dumps(snapshot))
        recovery["transition_sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    elif fault == "late_capture":
        snapshot["observed_at"] = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
        snapshot_path.write_text(json.dumps(snapshot))
        recovery["transition_sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    elif fault == "public_snapshot":
        snapshot_path.chmod(0o644)
    path.write_text(json.dumps(native))
    if fault == "claimed_revocation":
        with pytest.raises(ValueError, match="unrecognized evidence"):
            emitter.emit(environment)
        assert not (Path(environment["RUNNER_TEMP"]) /
                    "l5-security-qualification/qualification.json").exists()
        return
    if fault not in {"accepted", "accepted_with_denials", "claimed_revocation"}:
        with pytest.raises((ValueError, OSError)):
            emitter.emit(environment)
        assert not (Path(environment["RUNNER_TEMP"]) / "delivery-evidence/delivery-evidence.json").exists()
        return
    report = emitter.emit(environment)
    delivery = json.loads((Path(environment["RUNNER_TEMP"]) /
                           "delivery-evidence/delivery-evidence.json").read_text())
    assert report["native_observation"]["recovery_transition"] is True
    assert report["native_observation"]["recovery_attempts"] == 1
    verified = next(item for item in report["live_checks"] if item["id"] == "durable-recovery")
    assert verified["outcome"] == "passed" and verified["evidence_verified"] is True
    assert report["native_observation"]["scope_denials"] is (fault != "accepted")
    assert report["native_observation"]["owned_cleanup"] is (fault != "accepted")
    for section, identifier in (("story_criteria", "SA16/AC-03"), ("scenarios", "SA16/SCN-03")):
        criterion = next(item for item in report[section] if item["id"] == identifier)
        assert criterion["outcome"] == "incomplete" and criterion["evidence_verified"] is False
    assert next(item for item in report["live_checks"] if item["id"] == "scope-revocation")[
        "evidence_verified"] is False
    assert any(item["name"] == "durable-recovery" and item["evidence_verified"] is True
               for item in delivery["required_checks"])
    assert "native-readbacks:required-scenarios-not-evaluated" in report["unresolved"]


@pytest.mark.parametrize("fault", ["accepted", "missing_gateway", "missing_context", "missing_schema",
                                     "partial_schema", "missing_migration", "wrong_producer", "wrong_consumer",
                                     "missing_consumer_revision",
                                     "foreign_gateway", "stale_context", "supplied_success"])
def test_schema_compatibility_needs_serving_migration_and_native_consumer_receipt(private_qualification, fault):
    environment, path, native = private_qualification
    fixture_path = Path(environment["PRIVATE_FIXTURE"])
    fixture = json.loads(fixture_path.read_text())
    record = native["products"][0]["assessments"][1]
    selection = {"scope": fixture["cases"][0]["scope"], "run_id": record["run_id"],
                 "result_id": record["result_id"], "publication_id": "publication-example",
                 "sources": record["sources"], "assessment_plan": {
                     "input": {"capability": record["capability"], "sources": record["sources"]}}}
    if fault != "missing_context":
        fixture["cases"][0]["context_reuse"] = selection
        native["context"] = {field: copy.deepcopy(selection[field]) for field in (
            "scope", "run_id", "result_id", "publication_id", "sources")}
        native["context"].update(agent_run_id="later-agent-example", observed_at=native["observed_at"])
        if fault != "missing_schema":
            native["context"].update(schema_version="1.0", admitted_schema="1.0",
                                     consumer_revision="consumer-example")
    environment.update(AWS_REGION="us-east-1", L5_MUTATION_ROLE_ARN=
                       "arn:aws:iam::123456789012:role/fictional-deployment")
    if fault != "missing_gateway":
        native["gateway"] = {"revision": SOURCE, "account_id": "123456789012", "region": "us-east-1",
                             "cluster": "adp-dev-eks-cluster", "namespace": "adp-gateway",
                             "image_digest": "sha256:" + DIGEST, "pod_uids": ["pod-one"],
                             "migration_head": "head-one", "wheel_sha256": DIGEST,
                             "observed_at": native["observed_at"]}
    if fault == "partial_schema":
        native["context"].pop("admitted_schema")
    elif fault == "missing_migration":
        native["gateway"]["migration_head"] = ""
    elif fault == "wrong_producer":
        native["context"]["schema_version"] = "2.0"
    elif fault == "wrong_consumer":
        native["context"]["admitted_schema"] = "2.0"
    elif fault == "missing_consumer_revision":
        native["context"]["consumer_revision"] = ""
    elif fault == "foreign_gateway":
        native["gateway"]["account_id"] = "999999999999"
    elif fault == "stale_context":
        native["context"]["observed_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    elif fault == "supplied_success":
        native.pop("gateway")
        (path.parent / "independent-observations.json").write_text(json.dumps({
            "live_checks": [{"id": "schema-compatible", "outcome": "passed", "evidence_verified": True}]}))
    fixture_path.write_text(json.dumps(fixture))
    environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    native["fixture_digest"] = environment["L5_FIXTURE_DIGEST"]
    retain_selector_receipts(environment, native)
    path.write_text(json.dumps(native))
    if fault in {"partial_schema", "missing_migration", "wrong_producer", "wrong_consumer",
                 "missing_consumer_revision", "foreign_gateway", "stale_context"}:
        with pytest.raises(ValueError):
            emitter.emit(environment)
        assert not (Path(environment["RUNNER_TEMP"]) / "delivery-evidence/delivery-evidence.json").exists()
        return
    report = emitter.emit(environment)
    delivery = json.loads((Path(environment["RUNNER_TEMP"]) /
                           "delivery-evidence/delivery-evidence.json").read_text())
    checked = next(item for item in report["live_checks"] if item["id"] == "schema-compatible")
    assert report["native_observation"]["schema_compatible"] is (fault == "accepted")
    assert checked["evidence_verified"] is (fault == "accepted")
    assert checked["outcome"] == ("passed" if fault == "accepted" else "incomplete")
    assert any(item["name"] == "schema-compatible" and item["evidence_verified"] is (fault == "accepted")
               for item in delivery["required_checks"])
    assert "native-readbacks:required-scenarios-not-evaluated" in report["unresolved"]

@pytest.mark.parametrize("fault", ["accepted", "wrong_source", "fabricated_cleanup",
                                   "wrong_recovery_revision", "absent_native_job"])
def test_source_bound_product_cleanup_flows_into_native_emitter(private_qualification, readbacks, tmp_path, fault):
    import httpx

    environment, path, native = private_qualification
    case, product_rows, provider_jobs = readbacks
    scope = case["scope"]
    base = f"/v1/products/{scope['product_id']}/projects/{scope['project_id']}/services/security"
    for plan in case["assessments"]:
        plan["input"]["binding_id"] = plan["binding_id"]
    if fault == "wrong_source":
        case["assessments"][0]["input"]["sources"] = [{
            **case["assessments"][0]["input"]["sources"][0], "record_revision": "foreign-revision"}]
    if fault == "wrong_source":
        with pytest.raises(ValueError):
            reader.assess_case(case, product_rows.__getitem__,
                               lambda run, provider: provider_jobs[run["run_id"]],
                               lambda run: [{"source_ref": item, "content": SOURCE_CONTENT}
                                            for item in run["input"]["sources"]])
        return
    assessed = reader.assess_case(case, product_rows.__getitem__,
                                  lambda run, provider: provider_jobs[run["run_id"]],
                                  lambda run: [{"source_ref": item, "content": SOURCE_CONTENT}
                                               for item in run["input"]["sources"]])
    selected_plan = case["assessments"][1]
    source = selected_plan["input"]["sources"][0]
    input_record = {**selected_plan["input"], "capability": "threat_model", "configuration_revision": 1}
    run_id = "recovery-run"
    now = datetime.now(UTC)
    fault_time = (now - timedelta(seconds=3)).isoformat()
    restart_time = (now - timedelta(seconds=2)).isoformat()
    inventory = json.dumps({"source_ref": source, "files": []}).encode()
    def artifact(content):
        return {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
    source_snapshot = {"snapshot_id": "snapshot-example", "source_ref": source,
                       "content": artifact(SOURCE_CONTENT), "file_manifest": artifact(inventory)}
    reference = {"record_id": "fictional-recovery-job"}
    run = {"run_id": run_id, "scope": scope, "input": input_record,
           "admission": {"source_snapshots": [source_snapshot]}, "state": "succeeded",
           "provider_state": "completed", "native_job_ref": reference}
    work = {"work_id": "work-example", "attempts": 2, "completed_at": now.isoformat(),
            "last_enqueued_at": restart_time}
    attempt = {"attempt_id": "attempt-example", "operation": "submit", "status": "accepted",
               "first_work_id": "work-example", "started_at": fault_time, "native_job_ref": reference}
    publication = {"publication_id": "publication-example", "state": "published", "attempts": 2,
                   "result_id": "result-example", "result_version": 1, "schema_version": "1.0",
                   "receipt": {"receipt_id": "native-receipt", "contribution_id": "publication-example",
                               "admitted_schema": "1.0", "consumer_revision": "revision-example"}}
    before = {**run, "state": "running", "provider_state": "submitted", "native_job_ref": None,
              "work": {**work, "completed_at": None, "attempts": 1, "last_enqueued_at": fault_time},
              "attempts": [{"attempt_id": "attempt-example", "operation": "submit", "native_job_ref": None}]}
    prior = {**publication, "state": "retry_wait", "attempts": 1, "receipt": None}
    journal_path = tmp_path / "owned-cleanup.jsonl"
    journal = reader.AttemptJournal(journal_path)
    for operation, status, work_id, observed_at in (
            ("fault_arm", "acknowledged", "receipt-example", fault_time),
            ("submit", "accepted", "work-example", fault_time),
            ("worker_restart", "acknowledged", "receipt-example", restart_time),
            ("cleanup", "prepared", run_id, now.isoformat()),
            ("cleanup", "acknowledged", run_id, now.isoformat())):
        journal.append({"attempt_id": "attempt-example" if operation == "submit" else operation,
                        "run_id": run_id, "operation": operation, "status": status,
                        "work_id": work_id, "observed_at": observed_at})
    snapshot_path = Path(str(journal_path) + ".fault.json")
    snapshot_path.write_text(json.dumps({"receipt": "receipt-example", "run": before,
                                         "publication": prior, "observed_at": fault_time}))
    snapshot_path.chmod(0o600)
    case.update(api_url="https://product.invalid", api_token_env="PRODUCT_TOKEN", max_attempts=3,
                selected={"configuration_id": "configuration-example",
                          "sources": [source], "binding_id": selected_plan["binding_id"]},
                recovery={"journal_file": str(journal_path), "owned_binding_id": "owned-binding",
                          "scope": scope, "input": input_record, "sources": [source],
                          "binding_id": selected_plan["binding_id"], "receipt": "receipt-example",
                          "exclusive_agent_space": True, "max_attempts": 3,
                          "owned": ["owned"], "linked": ["linked"]})
    packet = {"run_id": "recovery-run", "ownership": {"owned": "scenario_owned", "linked": "customer_linked"},
              "resources": {"owned": "absent", "linked": "retained"}}
    if fault == "fabricated_cleanup":
        packet["ownership"]["owned"] = "customer_linked"
    def product(request):
        assert request.headers["Authorization"] == "Bearer fictional-product-token"
        if request.url.path == base + "/bindings/owned-binding":
            return httpx.Response(200, json={"binding_id": "owned-binding", "scope": scope,
                "configuration_id": "configuration-example", "ownership": "adp_created",
                "native_resource_ref": {"record_id": "owned"}, "revision": 2, "state": "cleaned"})
        path = request.url.path
        source_path = base + "/runs/" + run_id
        if path == source_path + "/native-readback/spaces/owned-binding":
            return httpx.Response(200, json=packet)
        if path == source_path + "/source-manifest":
            manifest_source = ({**source, "record_revision": "foreign-revision"}
                               if fault == "wrong_recovery_revision" else source)
            return httpx.Response(200, json={"run_id": run_id, "scope": scope,
                                             "sources": [manifest_source],
                                             "source_snapshot_ids": ["snapshot-example"]})
        if path == source_path + "/source-snapshots/snapshot-example":
            return httpx.Response(200, json=source_snapshot)
        if path == source_path + "/source-snapshots/snapshot-example/manifest":
            return httpx.Response(200, content=inventory, headers={"ETag": '"' + artifact(inventory)["sha256"] + '"'})
        if path == source_path + "/source-snapshots/snapshot-example/content":
            return httpx.Response(200, content=SOURCE_CONTENT,
                                  headers={"ETag": '"' + artifact(SOURCE_CONTENT)["sha256"] + '"'})
        if path == base + "/runs/receipts/receipt-example":
            return httpx.Response(200, json={"run_id": run_id})
        if path == source_path:
            return httpx.Response(200, json=run)
        if path == source_path + "/recovery-state":
            return httpx.Response(200, json={"run": run, "work": [work], "attempts": [attempt],
                                             "publications": [publication]})
        if path == base + "/publications/publication-example":
            return httpx.Response(200, json={"publication_id": publication["publication_id"],
                "result_id": publication["result_id"], "state": publication["state"],
                "receipt_id": "native-receipt", "consumer_revision": "revision-example"})
        raise AssertionError("unexpected Product readback path")
    cleanup_file = path.parent / "security-cleanup-456-2.json"
    cleanup_file.unlink()
    def client_factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(product), **kwargs)
    if fault == "fabricated_cleanup":
        with pytest.raises(AssertionError):
            reader.read_cleanup(case, {"PRODUCT_TOKEN": "fictional-product-token"}, path.parent,
                                "456", "2", client_factory=client_factory)
        assert not cleanup_file.exists()
        return
    observed = reader.read_cleanup(case, {"PRODUCT_TOKEN": "fictional-product-token"}, path.parent,
                                   "456", "2", client_factory=client_factory)
    if fault in {"wrong_recovery_revision", "absent_native_job"}:
        with pytest.raises((ValueError, AssertionError)):
            reader.read_recovery(case, {"PRODUCT_TOKEN": "fictional-product-token"},
                                 client_factory=client_factory,
                                 inventory=lambda run, provider: [] if fault == "absent_native_job" else [reference])
        return
    recovered = reader.read_recovery(case, {"PRODUCT_TOKEN": "fictional-product-token"},
                                     client_factory=client_factory, inventory=lambda run, provider: [reference])
    fixture_path = Path(environment["PRIVATE_FIXTURE"])
    fixture = json.loads(fixture_path.read_text())
    fixture["cases"][0].update(scope=scope, provider=case["provider"], assessments=case["assessments"],
        selected={"sources": [source], "binding_id": selected_plan["binding_id"]},
        recovery={"scope": scope, "sources": [source], "input": input_record,
                  "binding_id": selected_plan["binding_id"], "owned": ["owned"],
                  "linked": ["linked"], "journal_file": str(journal_path),
                  "receipt": "receipt-example", "max_attempts": 3})
    fixture_path.write_text(json.dumps(fixture))
    environment["L5_FIXTURE_DIGEST"] = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    native["fixture_digest"] = environment["L5_FIXTURE_DIGEST"]
    native["products"][0].update(scope=scope, assessments=assessed)
    native["recovery"] = recovered
    native["cleanup"] = observed
    native["observed_at"] = datetime.now(UTC).isoformat()
    retain_selector_receipts(environment, native)
    path.write_text(json.dumps(native))
    report = emitter.emit(environment)
    assert next(check for check in report["live_checks"] if check["id"] == "source-pinning")["outcome"] == "passed"
    assert report["native_observation"]["recovery_transition"] is True
    assert {"durable-recovery", "owned-cleanup"} <= {
        check["id"] for check in report["live_checks"] if check["outcome"] == "passed"}
