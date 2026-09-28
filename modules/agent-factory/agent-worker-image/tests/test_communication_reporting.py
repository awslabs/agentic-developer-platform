"""Report observed runtime facts without inferring task completion."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import entrypoint


@pytest.mark.parametrize("self_pr", ["123", ""])
def test_self_pushed_run_reports_facts_and_links_outcome(self_pr):
    url = "https://github.com/org/repo/issues/42#issuecomment-456"
    with (
        patch.object(entrypoint, "run_cmd", return_value=MagicMock(stdout="")),
        patch.object(entrypoint, "_find_open_pr", return_value=self_pr),
        patch.object(entrypoint, "_ensure_pr_body_marker") as marker,
        patch.object(entrypoint, "_register_authored_draft", return_value=""),
        patch.object(entrypoint, "_read_result_metadata", return_value={"outcome_comment_url": url}),
        patch.object(entrypoint, "_post_comment") as post,
        patch.object(entrypoint, "update_invocation_status") as status,
    ):
        assert entrypoint._handle_success("org/repo", 42, "agent/issue-42", "developer", "msg", "now") == 0
        body = post.call_args.args[4]
        assert url in body
        assert "no changes needed" not in body
        assert status.call_args.args[2] == "complete"  # machine execution contract unchanged
        if self_pr:
            assert "PR #123 is open" in body
            marker.assert_called_once()
        else:
            assert "task completion is not verified" in body
            marker.assert_not_called()


def test_transcript_only_push_does_not_claim_a_pr_was_opened():
    def run(cmd, **kwargs):
        return MagicMock(stdout="review.md" if cmd[:2] == ["git", "log"] else "")

    with (
        patch.object(entrypoint, "run_cmd", side_effect=run) as commands,
        patch.object(entrypoint, "_branch_changes_are_transcript_only", return_value=True),
        patch.object(entrypoint, "_ensure_pr_body_marker") as marker,
        patch.object(entrypoint, "_register_authored_draft", return_value=""),
        patch.object(entrypoint, "_read_result_metadata", return_value={}),
        patch.object(entrypoint, "_post_comment") as post,
        patch.object(entrypoint, "update_invocation_status") as status,
    ):
        assert entrypoint._handle_success("org/repo", 42, "agent/issue-42", "reviewer", "msg", "now") == 0
        body = post.call_args.args[4]
        assert "no PR was created" in body
        assert "PR opened" not in body
        assert "review transcripts pushed" in status.call_args.kwargs["summary"]
        marker.assert_not_called()
        assert not any(call.args[0][:3] == ["gh", "pr", "create"] for call in commands.call_args_list)


@pytest.mark.parametrize("url", ["https://evil.invalid/", "https://github.com/other/repo/issues/42#issuecomment-123", "https://github.com/org/repo/issues/42#issuecomment-1)\nBAD"])
def test_outcome_link_rejects_other_targets_or_markdown(url):
    assert entrypoint._outcome_report_link({"outcome_comment_url": url}, "org/repo", 42) == ""
