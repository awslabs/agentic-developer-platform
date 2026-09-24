"""A story's display stage follows its current attempt, not its original worker."""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import EndpointConnectionError

from src.activity.schemas import InvocationChainItem, InvocationChainResponse
from src.orchestration.node_activity import current_activity, load_story_activity, story_execution


def run(invocation_id, persona, timestamp, *, status="in_progress", liveness="live", children=None):
    return InvocationChainItem(
        invocation_id=invocation_id,
        persona=persona,
        invoked_at=timestamp,
        status=status,
        liveness=liveness,
        children=children or [],
    )


def chain(*items, depth_capped=False):
    return InvocationChainResponse(correlation_id="attempt-1", items=list(items), total_count=len(items), depth_capped=depth_capped)


@pytest.mark.parametrize("persona", ["reviewer", "agent-codex-reviewer"])
def test_review_is_visible_after_developer_exits(persona):
    reviewer = run("review-1", persona, "2026-09-15T14:54:00Z")
    developer = run("attempt-1", "developer", "2026-09-15T14:18:00Z", status="complete", liveness="exited", children=[reviewer])
    activity = current_activity(chain(developer))
    assert activity.invocation_id == "review-1"
    assert activity.persona == persona
    assert activity.liveness == "live"


def test_completed_codex_review_remains_in_story_history():
    reviewer = run("codex-review", "agent-codex-reviewer", "2026-09-15T14:54:00Z", status="complete", liveness="exited")
    developer = run("attempt-1", "developer", "2026-09-15T14:18:00Z", status="complete", liveness="exited", children=[reviewer])
    execution = story_execution(chain(developer))
    assert [(item.invocation_id, item.persona, item.status) for item in execution.runs] == [
        ("attempt-1", "developer", "complete"),
        ("codex-review", "agent-codex-reviewer", "complete"),
    ]
    assert execution.activity is None and execution.history_complete


@pytest.mark.parametrize("status", ["complete", "failed", "aborted", "budget_stopped"])
def test_finished_latest_review_does_not_resurrect_an_older_active_run(status):
    assert (
        current_activity(
            chain(
                run("old-review", "reviewer", "2026-09-15T14:54:00Z"),
                run("latest-review", "reviewer", "2026-09-15T15:00:00Z", status=status, liveness="exited"),
            )
        )
        is None
    )


def test_repair_supersedes_review_and_timestamp_offsets_are_respected():
    activity = current_activity(
        chain(
            run("review", "reviewer", "2026-09-15T16:00:00+01:00"),
            run("repair", "developer", "2026-09-15T15:05:00Z"),
        )
    )
    assert activity.invocation_id == "repair"


def test_lost_contact_is_not_presented_as_live_or_finished():
    activity = current_activity(chain(run("review", "reviewer", "2026-09-15T14:54:00Z", liveness="unverifiable")))
    assert activity.liveness == "unverifiable"


def test_capped_history_cannot_establish_the_current_stage():
    assert current_activity(chain(run("old-review", "reviewer", "2026-09-15T14:54:00Z"), depth_capped=True)) is None


def test_history_preserves_finished_runs_in_chronological_order_without_approval():
    development = run("attempt-1", "developer", "2026-09-15T15:00:00+01:00", status="complete", liveness="exited")
    review = run("review", "reviewer", "2026-09-15T14:10:00Z", status="complete", liveness="exited")
    repair = run("repair", "developer", "2026-09-15T14:20:00Z", status="complete", liveness="exited")
    rereview = run("review-again", "reviewer", "2026-09-15T14:30:00Z", status="complete", liveness="exited")
    development.children = [review]
    review.children = [repair]
    repair.children = [rereview]
    execution = story_execution(chain(development, repair))
    assert execution.run_id == "attempt-1"
    assert execution.history_complete is True
    assert [item.invocation_id for item in execution.runs] == ["attempt-1", "review", "repair", "review-again"]
    assert execution.activity is None
    assert "approved" not in execution.model_dump_json()


def test_capped_history_retains_observations_without_current_activity():
    execution = story_execution(chain(run("review", "reviewer", "2026-09-15T14:54:00Z"), depth_capped=True))
    assert len(execution.runs) == 1
    assert execution.history_complete is False
    assert execution.activity is None


@pytest.mark.parametrize("timestamp", ["invalid", "2026-09-15T15:00:00"])
def test_invalid_ordering_cannot_resurrect_stale_runs(timestamp):
    execution = story_execution(chain(run("old", "reviewer", "2026-09-15T14:54:00Z"), run("new", "developer", timestamp)))
    assert execution.runs == []
    assert execution.history_complete is False
    assert execution.activity is None


@pytest.mark.asyncio
async def test_reads_only_the_requested_tenant_and_deduplicates_attempts(monkeypatch):
    service = MagicMock()
    service.get_chain.return_value = chain(run("review", "reviewer", "2026-09-15T14:54:00Z"))
    monkeypatch.setattr("src.orchestration.node_activity._activity_service", lambda: service)
    result = await load_story_activity(org_id="org-a", run_ids=["attempt-1", "attempt-1"])
    service.get_chain.assert_called_once_with(correlation_id="attempt-1", tenant_id="org-a")
    assert result["attempt-1"].invocation_id == "review"


@pytest.mark.asyncio
async def test_unavailable_activity_does_not_break_the_graph(monkeypatch):
    service = MagicMock()
    service.get_chain.side_effect = EndpointConnectionError(endpoint_url="https://dynamodb.example")
    monkeypatch.setattr("src.orchestration.node_activity._activity_service", lambda: service)
    assert await load_story_activity(org_id="org-a", run_ids=["attempt-1"]) == {"attempt-1": None}
