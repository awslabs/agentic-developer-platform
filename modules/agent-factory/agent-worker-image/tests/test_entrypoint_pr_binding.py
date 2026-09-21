"""Both PR delivery paths and retries register their artifact before reporting success."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.mark.parametrize("path", ["self_created", "entrypoint_created", "existing_pr"])
@pytest.mark.parametrize("persona", ["developer", "reviewer"])
def test_delivery_and_retry_register_pr(path, persona, monkeypatch):
    import entrypoint

    def run(cmd, **_kwargs):
        stdout = ""
        if cmd[:3] == ["git", "diff", "--stat"] and path != "self_created":
            stdout = "code.py"
        if cmd[:3] == ["gh", "pr", "list"] and path == "existing_pr":
            stdout = "5293"
        return MagicMock(stdout=stdout, returncode=0)

    monkeypatch.setattr(entrypoint, "run_cmd", run)
    monkeypatch.setattr(entrypoint, "_find_open_pr", lambda *_: "5293")
    monkeypatch.setattr(entrypoint, "_branch_changes_are_transcript_only", lambda *_: False)
    monkeypatch.setattr(entrypoint, "_read_result_metadata", lambda: None)
    monkeypatch.setattr(entrypoint, "_register_authored_draft", lambda *_: "")
    monkeypatch.setattr(entrypoint, "_ensure_pr_body_marker", MagicMock())
    monkeypatch.setattr(entrypoint, "_write_outbound_correlation", MagicMock())
    monkeypatch.setattr(entrypoint, "prepend_correlation_marker", lambda body: body)
    monkeypatch.setattr(entrypoint, "update_invocation_status", MagicMock())
    comment = MagicMock()
    monkeypatch.setattr(entrypoint, "_post_comment", comment)
    binding = MagicMock(return_value="registered-artifact")
    monkeypatch.setattr(entrypoint, "pr_binding_note", binding)
    for _ in range(2):
        assert entrypoint._handle_success("aws-e/adp", 5301, "agent/issue-5301", persona, "run", "arrival") == 0
        binding.assert_called_with(repo="aws-e/adp", pr_number="5293", reviewer_artifact=persona == "reviewer")
        assert "registered-artifact" in comment.call_args.args[4]
    assert binding.call_count == 2


@pytest.mark.parametrize("path", ["self_created", "entrypoint_created"])
def test_unacknowledged_handoff_keeps_delivered_work_pending(path, monkeypatch):
    import entrypoint

    def run(cmd, **kwargs):
        stdout = "code.py" if path == "entrypoint_created" and cmd[:3] == ["git", "diff", "--stat"] else ""
        return MagicMock(stdout=stdout, returncode=0)

    monkeypatch.setattr(entrypoint, "run_cmd", run)
    monkeypatch.setattr(entrypoint, "_find_open_pr", lambda *_: "5293")
    monkeypatch.setattr(entrypoint, "_branch_changes_are_transcript_only", lambda *_: False)
    monkeypatch.setattr(entrypoint, "_read_result_metadata", lambda: None)
    monkeypatch.setattr(entrypoint, "_register_authored_draft", lambda *_: "")
    monkeypatch.setattr(entrypoint, "_ensure_pr_body_marker", MagicMock())
    monkeypatch.setattr(entrypoint, "_write_outbound_correlation", MagicMock())
    monkeypatch.setattr(entrypoint, "prepend_correlation_marker", lambda body: body)
    status = MagicMock()
    monkeypatch.setattr(entrypoint, "update_invocation_status", status)
    monkeypatch.setattr(entrypoint, "_post_comment", MagicMock())
    monkeypatch.setattr(entrypoint, "pr_binding_note", lambda **_: "pending")
    monkeypatch.setattr(entrypoint, "pr_handoff_pending", lambda: True)
    code = entrypoint._handle_success("aws-e/adp", 5301, "agent/issue-5301", "developer", "run", "arrival")
    assert code == entrypoint.AGENT_EXIT_RETRYABLE
    assert not entrypoint._should_ack_message(code)
    assert all(call.args[2] != "complete" for call in status.call_args_list)
