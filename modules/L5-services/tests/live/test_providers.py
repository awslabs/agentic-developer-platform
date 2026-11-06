"""Offline, synthetic contract checks; native/provider proof requires a deployed target."""

from copy import deepcopy
from hashlib import sha256

import pytest

from clients import read_assessment, verify_trigger_result
from providers import verify_assessment


@pytest.fixture
def assessment():
    source = {"tenant_id": "tenant-example", "project_id": "project-example",
              "source_connection_id": "source-example", "record_type": "document",
              "record_id": "architecture-example", "record_revision": "revision-example"}
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    job_ref = {"record_type": "job", "record_id": "job-example", "record_revision": "1"}
    content = b"synthetic architecture"
    run = {"run_id": "run-example", "scope": scope, "state": "succeeded", "provider_state": "completed",
           "native_job_ref": job_ref, "input": {"capability": "threat_model", "sources": [source]},
           "admission": {"source_snapshots": [{"source_ref": source, "content": {
               "bytes": len(content), "sha256": sha256(content).hexdigest()}}], "event_id": "event-example"}}
    result = {"result_id": "result-example", "run_id": "run-example", "scope": scope,
              "native_job_ref": job_ref, "finding_count": 1, "coverage": "complete"}
    findings = [{"native_id": "threat-example", "native_job_ref": job_ref, "claim": "candidate claim",
                 "source_refs": [source]}]
    native = {"native_job_ref": job_ref, "status": "COMPLETED", "findings_coverage": "complete",
              "findings": [{"native_id": "threat-example", "claim": "candidate claim"}],
              "review_sample": ["threat-example"], "review_rubric_revision": "rubric-example",
              "known_controls": ["control-example"], "progress_coverage": "unavailable"}
    sources = [{"source_ref": source, "content": content}]
    reviews = {"threat-example": {"judgment": "uncertain", "evidence_ref": "private-review-example",
                                  "source_revision": "revision-example"}}
    return run, result, findings, native, sources, reviews


def test_native_document_result_with_uncertainty(assessment):
    evidence = verify_assessment(*assessment)
    assert evidence["reviewed"] == 1 and evidence["progress_coverage"] == "unavailable"
    assert evidence["source_revisions"] == ["revision-example"]


def test_zero_findings_requires_coverage_and_controls(assessment):
    run, result, findings, native, sources, reviews = deepcopy(assessment)
    result["finding_count"] = 0
    native["findings"] = []
    native["review_sample"] = []
    assert verify_assessment(run, result, [], native, sources, {})["reviewed"] == 0
    native["known_controls"] = []
    with pytest.raises(AssertionError, match="zero findings"):
        verify_assessment(run, result, [], native, sources, {})


def test_code_review_checks_repository_commit(assessment):
    run, result, findings, native, sources, reviews = deepcopy(assessment)
    run["input"]["capability"] = "code_review"
    run["input"]["sources"][0]["record_type"] = "repository_commit"
    assert verify_assessment(run, result, findings, native, sources, reviews)["capability"] == "code_review"


@pytest.mark.parametrize("change,expected", [
    (lambda run, result, findings, native, sources, reviews: run.update(state="running"), "terminal"),
    (lambda run, result, findings, native, sources, reviews: native.update(status="IN_PROGRESS"), "native job"),
    (lambda run, result, findings, native, sources, reviews: native.update(findings_coverage="partial"), "coverage"),
    (lambda run, result, findings, native, sources, reviews: sources[0].update(content=b"different"), "source bytes"),
    (lambda run, result, findings, native, sources, reviews: findings[0].update(claim="false claim"), "finding claim"),
    (lambda run, result, findings, native, sources, reviews: reviews.clear(), "independent review"),
    (lambda run, result, findings, native, sources, reviews: reviews["threat-example"].update(
        source_revision="latest"), "cited revision"),
])
def test_mismatched_evidence_rejected(assessment, change, expected):
    items = deepcopy(assessment)
    change(*items)
    with pytest.raises(AssertionError, match=expected):
        verify_assessment(*items)


def test_result_readback_fetches_all_findings(assessment):
    run, result, findings, native, sources, reviews = assessment
    paths = []

    def read_api(path):
        paths.append(path)
        if path.endswith("/runs/run-example"):
            return run
        if path.endswith("/results/result-example"):
            return result
        if path.endswith("/findings?limit=100"):
            return {"items": [], "next_cursor": "next-example"}
        if path.endswith("/findings?limit=100&cursor=next-example"):
            return {"items": findings}
        raise AssertionError("unexpected readback path")

    assert read_assessment(run["scope"], run["run_id"], result["result_id"], read_api) == (
        run, result, findings)
    assert len(paths) == 4
    assert all("/products/product-example/projects/project-example/services/security/" in path for path in paths)


def test_duplicate_event_must_have_one_job(assessment):
    run, result, findings, native, sources, reviews = assessment
    receipt = {"event_id": "event-example", "run_id": "run-example", "state": "admitted"}
    evidence = verify_trigger_result([receipt, receipt], run, result, [native])
    assert evidence["run_id"] == "run-example"
    with pytest.raises(AssertionError, match="extra native job"):
        verify_trigger_result([receipt, receipt], run, result, [native, native])
    with pytest.raises(AssertionError, match="event deliveries"):
        verify_trigger_result([receipt, {**receipt, "run_id": "other"}], run, result, [native])


def test_review_sample_three_of_four_claims(assessment):
    run, result, findings, native, sources, reviews = deepcopy(assessment)
    for index in range(2, 5):
        finding = {**findings[0], "native_id": f"threat-{index}"}
        findings.append(finding)
        native["findings"].append({"native_id": finding["native_id"], "claim": finding["claim"]})
        if index < 4:
            reviews[finding["native_id"]] = {**reviews["threat-example"]}
            native["review_sample"].append(finding["native_id"])
    result["finding_count"] = 4
    assert verify_assessment(run, result, findings, native, sources, reviews)["reviewed"] == 3
    native["review_sample"].pop()
    with pytest.raises(AssertionError, match="independent review"):
        verify_assessment(run, result, findings, native, sources, reviews)


def test_unpinned_source_revision_rejected(assessment):
    run, result, findings, native, sources, reviews = deepcopy(assessment)
    run["input"]["sources"][0]["record_revision"] = "latest"
    with pytest.raises(AssertionError, match="source revision"):
        verify_assessment(run, result, findings, native, sources, reviews)


def test_result_readback_rejects_repeated_page_cursor(assessment):
    run, result, findings, native, sources, reviews = assessment

    def read_api(path):
        if path.endswith("/runs/run-example"):
            return run
        if path.endswith("/results/result-example"):
            return result
        return {"items": [], "next_cursor": "same-cursor"}

    with pytest.raises(AssertionError, match="pagination"):
        read_assessment(run["scope"], run["run_id"], result["result_id"], read_api)
