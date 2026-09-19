"""Exercise the actual producer through its own-run transport boundary."""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import review_delivery, review_result, status_gateway_client

HEAD = "a" * 40
EXPECT = {
    "org_id": "tenant",
    "flow_id": "flow",
    "node_id": "node",
    "cycle": 1,
    "accepted_plan_version": 1,
    "claim_id": "claim",
    "claim_generation": 1,
    "author_run_id": "author",
    "execution_id": "execution",
    "expected_head_sha": HEAD,
    "repo": "org/repo",
    "pr_number": 77,
    "provider_repository_id": 1234,
    "provider_pr_node_id": "PR_bound",
}


def receipt(data):
    digest = hashlib.sha256(data).hexdigest()
    tenant = hashlib.sha256(b"tenant").hexdigest()
    run = hashlib.sha256(b"reviewer").hexdigest()
    return {
        "key": f"runs/{tenant}/{run}/attempt-1/review-result/{digest}.json",
        "sha256": digest,
        "recorded": True,
    }


@pytest.fixture
def delivery(monkeypatch, tmp_path):
    # Restore direct env writes by prepare_review_delivery, too.
    for name in (
        review_result.REVIEW_EXPECT_ENV,
        review_result.AGENT_REPORT_PATH_ENV,
        review_result.RESULT_PATH_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ADP_TENANT_ID", "tenant")
    monkeypatch.setenv("ADP_MESSAGE_ID", "reviewer")
    monkeypatch.setenv("ADP_RUN_ATTEMPT", "1")
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    return review_delivery.prepare_review_delivery({"persona": "reviewer", "review_expect": EXPECT})


def test_missing_report_is_uploaded_as_incomplete_evidence(delivery, monkeypatch):
    sent = []

    def post(path, data, **kwargs):
        sent.append((path, data))
        return receipt(data)

    monkeypatch.setattr(status_gateway_client, "_post_bytes", post)
    note = delivery.finish(reviewed_head_sha=HEAD)
    assert "Review evidence recorded" in note
    assert sent[0][0] == "/artifacts/review-result"
    body = json.loads(sent[0][1])
    review_result.contract_models().ReviewResult.model_validate(body)
    assert body["verdict"] == "incomplete"
    assert body["publication"]["outcome"] == "not-attempted"
    assert body["lineage"]["reviewer_run_id"] == "reviewer"
    assert body["subject"]["reviewed_head_sha"] == HEAD


def test_report_findings_reach_the_stored_bytes(delivery, monkeypatch):
    delivery.report_path.write_text(
        json.dumps(
            {
                "stages": {"functional": "completed"},
                "verdict": "request-changes",
                "findings": [
                    {
                        "finding_id": "F1",
                        "stage": "functional",
                        "severity": "blocking",
                        "disposition": "open",
                        "summary": "Changed head invalidates review",
                    }
                ],
            }
        )
    )
    sent = []

    def post(path, data, **kwargs):
        sent.append(json.loads(data))
        return receipt(data)

    monkeypatch.setattr(status_gateway_client, "_post_bytes", post)
    assert "Review evidence recorded" in delivery.finish(reviewed_head_sha=HEAD)
    assert sent[0]["findings"][0]["finding_id"] == "F1"
    assert sent[0]["verdict"] == "request-changes"


@pytest.mark.parametrize("head", ["", "b" * 40])
def test_unknown_or_wrong_observed_head_never_uploads(delivery, monkeypatch, head):
    def unexpected(*args, **kwargs):
        pytest.fail("unbound review was uploaded")

    monkeypatch.setattr(status_gateway_client, "_post_bytes", unexpected)
    assert "not produced" in delivery.finish(reviewed_head_sha=head)
    assert not delivery.result_path.exists()


@pytest.mark.parametrize(
    "change",
    [
        {"recorded": False},
        {"recorded": "true"},
        {"sha256": "0" * 64},
        {"key": "runs/another/run/attempt-1/review-result/fake.json"},
    ],
)
def test_refused_or_wrong_receipt_preserves_local_result(delivery, monkeypatch, change):
    def post(path, data, **kwargs):
        return {**receipt(data), **change}

    monkeypatch.setattr(status_gateway_client, "_post_bytes", post)
    assert "not recorded" in delivery.finish(reviewed_head_sha=HEAD)
    assert delivery.result_path.exists()


def test_disabled_authority_does_not_fall_back(delivery, monkeypatch):
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "false")

    def unexpected(*args, **kwargs):
        pytest.fail("disabled transport must not send")

    monkeypatch.setattr(status_gateway_client, "_post_bytes", unexpected)
    assert "not recorded" in delivery.finish(reviewed_head_sha=HEAD)
    assert delivery.result_path.exists()


def test_fresh_paths_and_legacy_cleanup(delivery):
    delivery.report_path.write_text("stale report")
    another = review_delivery.prepare_review_delivery(
        {"persona": "reviewer", "review_expect": EXPECT}
    )
    assert another.report_path != delivery.report_path
    assert not another.report_path.exists()
    assert json.loads(os.environ[review_result.REVIEW_EXPECT_ENV]) == EXPECT
    assert review_delivery.prepare_review_delivery({"persona": "developer"}) is None
    for name in (
        review_result.REVIEW_EXPECT_ENV,
        review_result.AGENT_REPORT_PATH_ENV,
        review_result.RESULT_PATH_ENV,
    ):
        assert name not in os.environ
