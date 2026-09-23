import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib import codex_review_delivery as finalizer, review_delivery, review_result
from tests.test_review_delivery import EXPECT, HEAD, receipt


@pytest.fixture
def setup(monkeypatch):
    for key in (review_result.REVIEW_EXPECT_ENV, review_result.AGENT_REPORT_PATH_ENV, review_result.RESULT_PATH_ENV):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ADP_MESSAGE_ID", "reviewer")
    monkeypatch.setenv("ADP_TENANT_ID", "tenant")
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
    monkeypatch.setenv("ADP_RUN_ATTEMPT", "1")
    envelope = {"persona": "agent-codex-reviewer", "review_expect": {**EXPECT, "allow_story_repairs": True},
                "review_cycle_input": {"action": "review", "repo": "org/repo", "pr_number": 77,
                                       "head_sha": HEAD, "allow_story_repairs": True}}
    delivery = review_delivery.prepare_review_delivery(envelope)
    uploaded = []

    def upload(path, data, **kwargs):
        uploaded.append(json.loads(data))
        return receipt(data)

    monkeypatch.setattr(review_delivery.status_gateway_client, "_post_bytes", upload)
    monkeypatch.setattr(finalizer, "mint_review_token", lambda **kwargs: ("token", "review"))
    submit = Mock(return_value={"outcome": "submitted", "verdict_recorded": True, "commit_id": HEAD, "review_id": 123})
    monkeypatch.setattr(finalizer, "submit_review", submit)
    result = {"status": "engine_reviewed", "sha": HEAD, "repair_base_sha": None, "body": "Actual Codex verdict",
              "report": {"verdict": "approve", "stages": {"functional": "completed", "security": "completed"}, "findings": []}}
    return SimpleNamespace(envelope=envelope, delivery=delivery, uploaded=uploaded, submit=submit, result=result)


def finish(setup, *, parent=HEAD):
    return finalizer.finish_engine_review(json.dumps(setup.result), envelope=setup.envelope,
        delivery=setup.delivery, cwd="/workspace",
        run=lambda args, **kwargs: SimpleNamespace(stdout=parent if args[-1] == "HEAD^" else setup.result["sha"]))


def test_codex_exact_head_review_is_uploaded_before_completion(setup):
    assert "Review evidence recorded" in finish(setup)
    doc = setup.uploaded[0]
    assert doc["subject"]["reviewed_head_sha"] == HEAD
    assert doc["publication"]["outcome"] == "published"
    assert doc["lineage"]["reviewer_run_id"] != doc["lineage"]["author_run_id"]
    assert setup.submit.call_args.kwargs["commit_id"] == HEAD


def test_verified_story_repair_is_reviewed_at_final_child_commit(setup):
    setup.result.update(sha="b" * 40, repair_base_sha=HEAD)
    setup.submit.return_value["commit_id"] = "b" * 40
    finish(setup)
    assert setup.uploaded[0]["subject"]["reviewed_head_sha"] == "b" * 40


def test_repaired_commit_keeps_blocking_findings_in_formal_review(setup):
    setup.result.update(sha="b" * 40, repair_base_sha=HEAD)
    setup.result["report"].update(verdict="request-changes", findings=[{
        "finding_id": "scan", "stage": "security", "severity": "blocking",
        "disposition": "open", "summary": "Image scan still required",
    }])
    setup.submit.return_value["commit_id"] = "b" * 40
    finish(setup)
    assert setup.submit.call_args.kwargs["event"] == "REQUEST_CHANGES"
    assert setup.uploaded[0]["verdict"] == "request-changes"
    assert setup.uploaded[0]["subject"]["reviewed_head_sha"] == "b" * 40


def test_unchanged_unresolved_repair_does_not_claim_delivery(setup, monkeypatch):
    setup.envelope["review_cycle_input"]["action"] = "repair"
    setup.result["report"]["verdict"] = "request-changes"
    register = Mock()
    monkeypatch.setattr(finalizer.pr_binding, "register_pull_request", register)
    with pytest.raises(RuntimeError, match="no commit was published"):
        finish(setup)
    register.assert_not_called()
    setup.submit.assert_not_called()


def test_published_partial_repair_retains_distinct_followup_review(setup, monkeypatch):
    setup.envelope["review_cycle_input"]["action"] = "repair"
    setup.result.update(sha="b" * 40, repair_base_sha=HEAD)
    setup.result["report"]["verdict"] = "request-changes"
    register = Mock()
    monkeypatch.setattr(finalizer.pr_binding, "register_pull_request", register)
    assert "repair delivered" in finish(setup)
    register.assert_called_once_with(repo="org/repo", pr_number=77)
    setup.submit.assert_not_called()


def test_unchanged_clean_repair_reports_no_repair_needed(setup, monkeypatch):
    setup.envelope["review_cycle_input"]["action"] = "repair"
    monkeypatch.setattr(finalizer.pr_binding, "register_pull_request", Mock())
    assert "no repair was needed" in finish(setup)


@pytest.mark.parametrize("change", ["parent", "authorization", "lineage"])
def test_unassigned_head_cannot_be_published_or_uploaded(setup, change):
    setup.result.update(sha="b" * 40, repair_base_sha=HEAD)
    if change == "authorization":
        setup.envelope["review_cycle_input"]["allow_story_repairs"] = False
    if change == "lineage":
        setup.result["repair_base_sha"] = "c" * 40
    with pytest.raises(RuntimeError):
        finish(setup, parent="c" * 40 if change == "parent" else HEAD)
    assert not setup.uploaded
    setup.submit.assert_not_called()


def test_formal_self_review_refusal_remains_a_refusal(setup):
    setup.submit.return_value = {"outcome": "pending_approval", "verdict_recorded": False,
                                "refusal_reason": "self_review", "review_id": 123}
    finish(setup)
    doc = setup.uploaded[0]
    result = review_result.contract_models().ReviewResult.model_validate(doc)
    assert doc["verdict"] == "approve"
    assert doc["publication"]["outcome"] == "refused"
    assert result.publication_blockers()


def test_review_report_finalizer_never_commits_or_pushes(monkeypatch):
    import entrypoint
    run = Mock(side_effect=AssertionError("review finalizer must not run Git"))
    monkeypatch.setattr(entrypoint, "run_cmd", run)
    monkeypatch.setattr(entrypoint.run_report, "enabled", lambda: False)
    monkeypatch.setattr(entrypoint, "update_invocation_status", Mock())
    assert entrypoint._handle_success("org/repo", 1, "story", "reviewer", "run", "now", review_only=True) == 0
    run.assert_not_called()


def test_owned_repair_publishes_evidence_without_new_reviewer(setup, monkeypatch):
    setup.envelope["review_cycle_input"].update(action="repair", reviewer_owned_delivery=True)
    setup.result.update(sha="b" * 40, repair_base_sha=HEAD)
    setup.submit.return_value["commit_id"] = "b" * 40
    register = Mock()
    monkeypatch.setattr(finalizer.pr_binding, "register_pull_request", register)
    assert "Review evidence recorded" in finish(setup, parent="c" * 40)
    register.assert_not_called()
    assert setup.uploaded[0]["lineage"]["author_run_id"] == EXPECT["author_run_id"]


def test_owned_no_progress_repair_keeps_real_findings_instead_of_runtime_error(setup, monkeypatch):
    setup.envelope["review_cycle_input"].update(action="repair", reviewer_owned_delivery=True)
    setup.result["report"].update(verdict="request-changes", findings=[{
        "finding_id": "external", "stage": "functional", "severity": "blocking",
        "disposition": "open", "summary": "External dependency unavailable"}])
    monkeypatch.setattr(finalizer.pr_binding, "register_pull_request", Mock())
    finish(setup)
    assert setup.uploaded[0]["verdict"] == "request-changes"


def test_merge_poll_and_parent_finalization_reuse_the_exact_review(setup):
    finish(setup)
    setup.result["merged"] = True
    finish(setup)
    setup.submit.assert_called_once()
    assert setup.uploaded[0] == setup.uploaded[1]


def test_lost_upload_response_replays_evidence_without_repeating_formal_review(setup, monkeypatch):
    original = finalizer.status_gateway_client.upload_review_result
    calls = 0

    def upload(data):
        nonlocal calls
        calls += 1
        result = original(data)
        if calls == 1:
            raise finalizer.run_report.RunReportError("response_lost")
        return result

    monkeypatch.setattr(finalizer.status_gateway_client, "upload_review_result", upload)
    with pytest.raises(finalizer.run_report.RunReportError):
        finish(setup)
    finish(setup)
    setup.submit.assert_called_once()
    assert setup.uploaded[0] == setup.uploaded[1]
