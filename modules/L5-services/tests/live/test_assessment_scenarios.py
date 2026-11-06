"""Offline scenario contract checks: no claim about deployed provider access."""

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from assessment_scenarios import real_security_assessments
from l5_services.adapters.security.native_readback import native_job, native_jobs
from providers import verify_assessment


@pytest.fixture
def assessment_case():
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    plans = []
    for capability, kind in (("threat_model", "document"), ("code_review", "repository_commit")):
        source = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                  "record_type": kind, "record_revision": "revision-example"}
        plans.append({"capability": capability, "receipt": capability + "-unique-receipt", "binding_id": "binding-example",
                      "input": {"capability": capability, "binding_id": "binding-example", "sources": [source]}})
    event = {"kind": "pull_request", "event_id": "event-example", "capability": "code_review",
             "input": deepcopy(plans[1]["input"])}
    event["input"]["sources"][0]["record_revision"] = "new-revision-example"
    return {"scope": scope, "assessments": plans, "trigger_event": event, "max_attempts": 4}


class RecordingAssessments:
    def __init__(self, case):
        self.case = case
        self.calls = []
        self.reads = {}
        for index, plan in enumerate([*case["assessments"], case["trigger_event"]]):
            run_id = f"run-{index}"
            source = plan["input"]["sources"][0]
            content = b"independently fetched example source"
            native_ref = {"record_type": "native_job", "record_id": f"job-{index}"}
            run = {"run_id": run_id, "scope": case["scope"], "state": "succeeded", "provider_state": "completed",
                   "native_job_ref": native_ref, "input": deepcopy(plan["input"]),
                   "admission": {"source_snapshots": [{"source_ref": source, "content": {
                       "bytes": len(content), "sha256": sha256(content).hexdigest()}}],
                       "event_id": "event-example" if index == 2 else None}}
            result = {"run_id": run_id, "result_id": f"result-{index}", "scope": case["scope"],
                      "native_job_ref": native_ref, "coverage": "complete", "finding_count": 1}
            finding = {"native_id": f"claim-{index}", "native_job_ref": native_ref,
                       "claim": "candidate example", "source_refs": [source]}
            native = {"native_job_ref": native_ref, "status": "COMPLETED", "findings_coverage": "complete",
                      "findings": [{"native_id": finding["native_id"], "claim": finding["claim"]}],
                      "review_sample": [finding["native_id"]], "review_rubric_revision": "rubric-example",
                      "known_controls": ["example control"]}
            reviews = {finding["native_id"]: {"judgment": "uncertain", "evidence_ref": "private-example",
                                                   "source_revision": source["record_revision"]}}
            self.reads[run_id] = (run, result, [finding], native,
                                  [{"source_ref": source, "content": content}], reviews)
        self.receipt = {"event_id": "event-example", "state": "admitted", "run_id": "run-2"}

    def submit(self, plan):
        self.calls.append("submit")
        return {"run_id": "run-0" if plan["capability"] == "threat_model" else "run-1"}

    def await_result(self, run_id):
        self.calls.append("result")
        return self.reads[run_id][:3]

    def native_job(self, run):
        self.calls.append("native")
        return self.reads[run["run_id"]][3]

    def read_sources(self, run, plan):
        self.calls.append("source")
        return self.reads[run["run_id"]][4]

    def reviews(self, run, result, findings, native, plan):
        self.calls.append("review")
        return self.reads[run["run_id"]][5]

    def capture_inventory(self, event):
        self.calls.append("inventory")
        return ("space-example", [])

    def deliver_event(self, event):
        self.calls.append("event")
        return self.receipt

    def read_event_receipt(self, event):
        self.calls.append("receipt")
        return self.receipt

    def native_jobs_for_event(self, run, baseline):
        self.calls.append("jobs")
        return [self.reads[run["run_id"]][3]]

    def verify_triggered_evidence(self, run, result, findings, event):
        self.calls.append("verify_event")
        verify_assessment(*self.reads[run["run_id"]])


def test_two_capabilities_and_duplicate_event_need_independent_evidence(assessment_case):
    driver = RecordingAssessments(assessment_case)
    summary = real_security_assessments(assessment_case, driver)
    assert [row["capability"] for row in summary["assessments"]] == ["threat_model", "code_review"]
    assert summary["event"]["run_id"] == "run-2"
    assert driver.calls.count("submit") == 2 and driver.calls.count("event") == 2
    assert driver.calls.index("native") < driver.calls.index("review") < driver.calls.index("event")
    assert driver.calls[-2:] == ["verify_event", "jobs"]


@pytest.mark.parametrize("invalid", ["wrong_scope", "unbounded", "missing_capability", "unpinned"])
def test_invalid_assessment_selection_never_submits(assessment_case, invalid):
    driver = RecordingAssessments(assessment_case)
    if invalid == "wrong_scope":
        assessment_case["assessments"][0]["input"]["sources"][0]["tenant_id"] = "other-tenant"
    elif invalid == "unbounded":
        assessment_case["max_attempts"] = 3
    elif invalid == "missing_capability":
        assessment_case["assessments"][1]["capability"] = "threat_model"
    else:
        assessment_case["assessments"][0]["input"]["sources"][0]["record_revision"] = "latest"
    with pytest.raises(AssertionError):
        real_security_assessments(assessment_case, driver)
    assert driver.calls == []


def test_missing_independent_claim_review_refuses_completion(assessment_case):
    driver = RecordingAssessments(assessment_case)
    driver.reads["run-0"] = (*driver.reads["run-0"][:-1], {})
    with pytest.raises(AssertionError, match="independent review"):
        real_security_assessments(assessment_case, driver)
    assert "event" not in driver.calls


def test_native_duplicate_paid_job_refuses_completion(assessment_case):
    driver = RecordingAssessments(assessment_case)
    driver.native_jobs_for_event = lambda run, baseline: [driver.reads[run["run_id"]][3]] * 2
    with pytest.raises(AssertionError, match="extra native job"):
        real_security_assessments(assessment_case, driver)


def test_duplicate_event_receipt_must_match_independent_read(assessment_case):
    driver = RecordingAssessments(assessment_case)
    driver.read_event_receipt = lambda event: {**driver.receipt, "run_id": "other"}
    with pytest.raises(AssertionError, match="independently read receipt"):
        real_security_assessments(assessment_case, driver)


def test_event_baseline_is_private_and_cannot_replace_an_existing_observation(tmp_path, assessment_case):
    from assessment_live import LiveAssessmentDriver

    driver = object.__new__(LiveAssessmentDriver)
    driver.case = {**assessment_case, "assessment_journal_file": str(tmp_path / "attempts.jsonl")}
    driver.runs_by_capability = {"code_review": {
        "run_id": "selected-run", "input": {"capability": "code_review"},
        "admission": {"native_space_ref": {"record_id": "selected-space"}}}}
    driver._native = lambda action, run: ([{"native_job_ref": "previous-job"}]
                                          if action == "inventory" and run["run_id"] == "selected-run" else [])
    assert driver.capture_inventory(assessment_case["trigger_event"]) == (
        "selected-space", [{"native_job_ref": "previous-job"}])
    baseline = Path(str(tmp_path / "attempts.jsonl") + ".event-before.json")
    assert baseline.stat().st_mode & 0o077 == 0
    assert json.loads(baseline.read_text())["event_id"] == "event-example"
    with pytest.raises(FileExistsError):
        driver.capture_inventory(assessment_case["trigger_event"])


def test_native_threat_probe_reads_job_and_claims_without_cached_product_claims():
    from types import SimpleNamespace

    ref = {"agent_space_id": "space-example", "threat_model_id": "model-example",
           "threat_model_job_id": "job-example", "artifact_id": "artifact-example"}
    run = {"native_job_ref": ref, "input": {"capability": "threat_model"},
           "admission": {"native_space_ref": {"record_id": "space-example"}}}
    job = {"agentSpaceId": "space-example", "threatModelId": "model-example", "threatModelJobId": "job-example",
           "scopeDocs": [{"artifactId": "artifact-example"}], "status": "COMPLETED"}
    client = SimpleNamespace(
        batch_get_threat_model_jobs=lambda **kwargs: {"threatModelJobs": [job]},
        list_threats=lambda **kwargs: {"threats": [{"threatId": "claim-example", "threatJobId": "job-example"}]},
        batch_get_threats=lambda **kwargs: {"threats": [{"threatId": "claim-example",
                            "threatJobId": "job-example", "statement": "native claim"}]},
        list_threat_model_job_tasks=lambda **kwargs: {"threatModelJobTaskSummaries": []},
        list_threat_model_jobs=lambda **kwargs: {"threatModelJobSummaries": [
            {"agentSpaceId": "space-example", "threatModelId": "model-example", "threatModelJobId": "job-example"}]})
    assert native_job(client, run)["findings"] == [{"native_id": "claim-example", "claim": "native claim"}]
    assert native_jobs(client, run) == [ref]
    client.list_threat_model_jobs = lambda **kwargs: {"threatModelJobSummaries": [
        {"agentSpaceId": "space-example", "threatModelId": "model-example", "threatModelJobId": "job-example"},
        {"agentSpaceId": "space-example", "threatModelId": "model-example", "threatModelJobId": "other-job"}]}
    client.batch_get_threat_model_jobs = lambda **kwargs: {"threatModelJobs": [{
        **job, "threatModelJobId": kwargs["threatModelJobIds"][0],
        "scopeDocs": [{"artifactId": ("artifact-example" if kwargs["threatModelJobIds"][0] == "job-example"
                                       else "other-artifact")}]}]}
    jobs = native_jobs(client, run)
    assert len(jobs) == 2 and jobs[0]["artifact_id"] != jobs[1]["artifact_id"]


def test_native_code_review_reads_phase_task_and_independent_claim():
    from types import SimpleNamespace

    ref = {"record_id": "job-example", "record_revision": "review-example", "record_type": "native_job"}
    run = {"native_job_ref": ref, "input": {"capability": "code_review"},
           "admission": {"native_space_ref": {"record_id": "space-example"}}}
    client = SimpleNamespace(
        batch_get_code_review_jobs=lambda **kwargs: {"codeReviewJobs": [{
            "agentSpaceId": "space-example", "codeReviewJobId": "job-example", "codeReviewId": "review-example",
            "status": "COMPLETED", "steps": [{"name": "STATIC_ANALYSIS", "status": "COMPLETED"}]}]},
        list_findings=lambda **kwargs: {"findingsSummaries": [{"findingId": "finding-example",
                                                         "codeReviewJobId": "job-example"}]},
        batch_get_findings=lambda **kwargs: {"findings": [{"findingId": "finding-example",
                             "codeReviewJobId": "job-example", "description": "native claim"}]},
        list_code_review_job_tasks=lambda **kwargs: {"codeReviewJobTaskSummaries": [
            {"codeReviewJobId": "job-example", "taskId": "task-example"}]},
        list_code_reviews=lambda **kwargs: {"codeReviewSummaries": [{
            "agentSpaceId": "space-example", "codeReviewId": "review-example"}]},
        list_code_review_jobs_for_code_review=lambda **kwargs: {"codeReviewJobSummaries": [{
            "agentSpaceId": "space-example", "codeReviewId": "review-example", "codeReviewJobId": "job-example"}]})
    evidence = native_job(client, run)
    assert evidence["findings"] == [{"native_id": "finding-example", "claim": "native claim"}]
    assert evidence["progress_coverage"] == "complete" and len(evidence["tasks"]) == 1
    assert native_jobs(client, run) == [ref]
    client.list_code_reviews = lambda **kwargs: {"codeReviewSummaries": [
        {"agentSpaceId": "space-example", "codeReviewId": "review-example"},
        {"agentSpaceId": "space-example", "codeReviewId": "orphan-review"}]}
    client.list_code_review_jobs_for_code_review = lambda **kwargs: {"codeReviewJobSummaries": [{
        "agentSpaceId": "space-example", "codeReviewId": kwargs["codeReviewId"],
        "codeReviewJobId": "job-example" if kwargs["codeReviewId"] == "review-example" else "orphan-job"}]}
    client.batch_get_code_review_jobs = lambda **kwargs: {"codeReviewJobs": [{
        "agentSpaceId": "space-example", "codeReviewJobId": kwargs["codeReviewJobIds"][0],
        "codeReviewId": "review-example" if kwargs["codeReviewJobIds"][0] == "job-example" else "orphan-review"}]}
    assert len(native_jobs(client, run)) == 2


@pytest.mark.parametrize("capability", ["threat_model", "code_review"])
@pytest.mark.parametrize("fault", [None, "foreign_space", "foreign_parent", "foreign_job", "missing_detail"])
def test_native_inventory_accepts_aws_summaries_but_requires_scoped_details(capability, fault):
    from types import SimpleNamespace

    threat = capability == "threat_model"
    parent_key = "threatModelId" if threat else "codeReviewId"
    job_key = "threatModelJobId" if threat else "codeReviewJobId"
    detail_key = "threatModelJobs" if threat else "codeReviewJobs"
    summary = {parent_key: "parent-example", job_key: "job-example"}
    detail = {**summary, "agentSpaceId": "space-example", "scopeDocs": [{"artifactId": "artifact-example"}]}
    if not threat:
        detail.pop("agentSpaceId")  # AWS CodeReviewJob omits this field too.
    ref = ({"threat_model_id": "parent-example", "threat_model_job_id": "job-example", "artifact_id": "artifact-example"}
           if threat else {"record_id": "job-example", "record_revision": "parent-example"})
    run = {"native_job_ref": ref, "input": {"capability": capability},
           "admission": {"native_space_ref": {"record_id": "space-example"}}}
    if fault == "foreign_space":
        detail["agentSpaceId"] = "foreign-space"
    elif fault == "foreign_parent":
        detail[parent_key] = "foreign-parent"
    elif fault == "foreign_job":
        detail[job_key] = "foreign-job"

    def read_detail(**kwargs):
        assert kwargs["agentSpaceId"] == "space-example"
        return {detail_key: [] if fault == "missing_detail" else [detail]}

    client = SimpleNamespace(
        list_threat_model_jobs=lambda **kwargs: {"threatModelJobSummaries": [summary]},
        batch_get_threat_model_jobs=read_detail,
        list_code_reviews=lambda **kwargs: {"codeReviewSummaries": [{"codeReviewId": "parent-example"}]},
        list_code_review_jobs_for_code_review=lambda **kwargs: {"codeReviewJobSummaries": [summary]},
        batch_get_code_review_jobs=read_detail,
    )
    if fault:
        with pytest.raises(ValueError, match="inventory is incomplete"):
            native_jobs(client, run)
    else:
        assert native_jobs(client, run) == [ref]


@pytest.mark.parametrize("scope_docs", [[], [{"artifactId": "foreign"}], [{"artifactId": "artifact-example"}] * 2])
def test_threat_readback_never_substitutes_legacy_documents_for_scope(scope_docs):
    from types import SimpleNamespace

    ref = {"threat_model_id": "model-example", "threat_model_job_id": "job-example", "artifact_id": "artifact-example"}
    run = {"native_job_ref": ref, "input": {"capability": "threat_model"},
           "admission": {"native_space_ref": {"record_id": "space-example"}}}
    client = SimpleNamespace(batch_get_threat_model_jobs=lambda **kwargs: {"threatModelJobs": [{
        "agentSpaceId": "space-example", "threatModelId": "model-example", "threatModelJobId": "job-example",
        "documents": [{"artifactId": "artifact-example"}], "scopeDocs": scope_docs}]})
    with pytest.raises(ValueError, match="differs from scoped run"):
        native_job(client, run)


def test_assessment_fixture_guards_private_journal_and_exact_source(tmp_path, monkeypatch):
    from assessment_live import validate_assessment_fixture

    journal = tmp_path / "journal.jsonl"
    review = tmp_path / "review.json"
    review.write_text("{}")
    review.chmod(0o600)
    scope = {"tenant_id": "tenant-example", "project_id": "project-example"}
    source = {**scope, "record_type": "document", "record_revision": "revision-example"}
    entry = {"source_ref": source, "url": "https://source.example/revision-example", "token_env": "TEST_SOURCE"}
    plan = {"input": {"sources": [source]}, "source_readback": [entry], "review_file": str(review)}
    event = {**deepcopy(plan), "event_id": "event-example", "url": "https://event.example/hook",
             "receipt_url": "https://event.example/receipts/event-example", "token_env": "TEST_EVENT",
             "exclusive_agent_space": True}
    case = {"assessments": [plan, deepcopy(plan)], "trigger_event": event,
            "max_attempts": 4, "assessment_journal_file": str(journal), "provider": {"region": "us-east-1"}}
    monkeypatch.setenv("TEST_SOURCE", "fictional")
    monkeypatch.setenv("TEST_EVENT", "fictional")
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError, match="journal"):
        validate_assessment_fixture(case)
    tmp_path.chmod(0o700)
    validate_assessment_fixture(case)
    plan["input"]["sources"][0]["record_revision"] = "latest"
    with pytest.raises(ValueError, match="exact revision"):
        validate_assessment_fixture(case)


def test_trigger_must_pin_updated_revision_before_paid_assessment(assessment_case):
    driver = RecordingAssessments(assessment_case)
    assessment_case["trigger_event"]["input"] = deepcopy(assessment_case["assessments"][1]["input"])
    with pytest.raises(AssertionError, match="new authorized"):
        real_security_assessments(assessment_case, driver)
    assert driver.calls == []
