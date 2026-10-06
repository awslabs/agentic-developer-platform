"""Mixed-source, three-day timeline presentation fixtures for ACT01."""

from src.activity.schemas import InvocationItem
from src.activity.work_summary import summarize_work


def test_mixed_three_day_timeline_keeps_runs_and_deduplicates_issues():
    records = [
        InvocationItem(
            invocation_id="root-1",
            invoked_at="2026-10-01T23:15:00Z",
            completed_at="2026-10-02T00:00:00Z",
            correlation_id="chain-a",
            repo="sample/widgets",
            issue_number=14,
            persona="developer",
            status="complete",
            summary="Submitted a patch",
            source_url="https://github.com/sample/widgets/issues/14",
            transcript_key="transcripts/root-1",
            run_log_url="https://github.com/sample/widgets/actions/runs/5",
        ),
        InvocationItem(
            invocation_id="child-2",
            invoked_at="2026-10-02T11:00:00Z",
            correlation_id="chain-a",
            triggered_by_invocation_id="root-1",
            trigger_kind="agent",
            repo="sample/widgets",
            issue_number=14,
            persona="reviewer",
            status="failed",
            error_message="Review failed",
        ),
        InvocationItem(
            invocation_id="task-3",
            invoked_at="2026-10-03T19:00:00Z",
            source_type="task",
            task_id="tsk_3",
            repo="sample/widgets",
            issue_number=15,
            persona="developer",
            status="complete",
            transcript_kind="task_report",
            transcript_status="available",
            task_snapshot={"result": {"report": {"summary": "Patch proposed"}}, "error": None},
        ),
        InvocationItem(
            invocation_id="legacy-4",
            invoked_at="2026-10-04T01:00:00Z",
            repo="sample/widgets",
            issue_number=15,
            persona="developer",
            status="in_progress",
        ),
    ]
    summary = summarize_work([records[3], records[1], records[2], records[0], records[0]])
    assert [run.invocation_id for run in summary.runs] == ["root-1", "child-2", "task-3", "legacy-4"]
    assert [(run.invocation_id, run.correlation_id, run.parent_invocation_id, run.trigger_kind) for run in summary.runs[:2]] == [
        ("root-1", "chain-a", None, "human"),
        ("child-2", "chain-a", "root-1", "agent"),
    ]
    assert [(issue.url, issue.invocation_ids) for issue in summary.issues] == [
        ("https://github.com/sample/widgets/issues/14", ["root-1", "child-2"]),
        ("https://github.com/sample/widgets/issues/15", ["task-3", "legacy-4"]),
    ]
    assert summary.runs[0].evidence == {
        "source": "https://github.com/sample/widgets/issues/14",
        "run_log": "https://github.com/sample/widgets/actions/runs/5",
        "transcript": "/me/agent-invocations/root-1/transcript",
    }
    assert summary.runs[0].record_url == "/me/agent-invocations/root-1"
    assert summary.runs[2].record_url == "/me/agent-invocations/task-3"
    assert summary.runs[1].error == "Review failed"
    assert summary.runs[1].evidence == {}
    assert summary.runs[2].evidence == {"task_report": "/me/agent-invocations/task-3/transcript"}
    assert summary.runs[2].task_result == {"report": {"summary": "Patch proposed"}}
    assert summary.runs[3].evidence == {}


def test_unlinked_run_is_kept_without_an_invented_issue_or_transcript():
    summary = summarize_work([InvocationItem(invocation_id="unlinked", invoked_at="2026-10-03T10:00:00Z")])
    assert summary.issues == []
    assert summary.runs[0].evidence == {}
