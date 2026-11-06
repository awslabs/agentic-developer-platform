import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import operations_repair as repair
from lib import operations_task_board as boards


@pytest.fixture
def story(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-b", "agent/issue-12")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    path = tmp_path / ".adp/tasks/12.json"
    path.parent.mkdir(parents=True)
    value = {
        "version": 2,
        "issue": 12,
        "updated_at": "2026-10-31T09:00:00Z",
        "tasks": [
            {
                "id": "AC-c1",
                "kind": "code",
                "title": "Existing code",
                "status": "done",
                "covers": [],
                "note": "Tests passed at original revision",
            },
            {
                "id": "AC-t1",
                "kind": "test",
                "title": "Manual acceptance",
                "status": "blocked",
                "covers": ["AC-c1"],
                "note": "Needs an authorized human",
            },
        ],
    }
    path.write_text(json.dumps(value))
    git("add", ".")
    git("commit", "-m", "fixture")
    repair.github._record_publication(
        repair.github._publication_state_path(str(tmp_path)),
        head="a" * 40,
        tree=git("rev-parse", "HEAD^{tree}"),
    )
    state = {
        "repository_id": 7,
        "repository": "example/service",
        "branch": "agent/issue-12",
        "branch_head": "b" * 40,
        "default_branch_head": "c" * 40,
    }
    monkeypatch.setenv("WORK_DIR", str(tmp_path))
    monkeypatch.setenv("ISSUE_NUMBER", "12")
    monkeypatch.setenv("TARGET_REPO", "example/service")
    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_STORY", "true")
    monkeypatch.setattr(repair.github, "read_repository", lambda: {"repository": state})
    history = {"cycles": [{"cycle": 1, "tasks": [{"state": "done"}]}]}
    monkeypatch.setattr(repair.github, "_request", lambda body: {"operations_tasks": history})
    return path, value, state, history


@pytest.mark.parametrize("persona", ["operations", "agent-codex-operations"])
@pytest.mark.parametrize("version", [1, 2])
def test_both_stacks_import_every_status_and_retained_evidence(
    story, monkeypatch, persona, version
):
    path, value, state, history = story
    monkeypatch.setenv("AGENT_TYPE", persona)
    value["version"] = version
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    result = repair.invoke("repair_tasks", {})
    assert result["operations_tasks"] == history
    board = result["story_board"]
    assert board["state"] == "available" and board["board"] == value
    assert board["sha256"] == hashlib.sha256(before).hexdigest()
    assert board["confirmed_source_revision"] == "a" * 40
    assert board["current_branch_revision"] == "b" * 40
    assert board["modified_since_confirmed_source"] is (version == 1)
    assert board["counts"] == {"done": 1, "blocked": 1, "in_progress": 0, "open": 0}
    assert board["progress_only"] is True and path.read_bytes() == before


@pytest.mark.parametrize(
    "fault", ["foreign_issue", "duplicate", "bad_status", "dangling", "bad_parent", "invalid_json"]
)
def test_invalid_board_does_not_erase_history_or_claim_absence(story, monkeypatch, fault):
    path, value, _, history = story
    monkeypatch.setenv("AGENT_TYPE", "operations")
    if fault == "foreign_issue":
        value["issue"] = 13
    elif fault == "duplicate":
        value["tasks"].append(value["tasks"][0])
    elif fault == "bad_status":
        value["tasks"][0]["status"] = "accepted"
    elif fault == "dangling":
        value["tasks"][1]["covers"] = ["missing"]
    elif fault == "bad_parent":
        value["tasks"][0]["planStep"] = {"id": "../wrong", "title": "Wrong"}
    path.write_text("{" if fault == "invalid_json" else json.dumps(value))
    result = repair.invoke("repair_tasks", {})
    assert result["operations_tasks"] == history
    assert result["story_board"]["state"] == "invalid"
    assert (
        "board" not in result["story_board"]
        and "expected_sha256" in result["story_board"]["next_action"]
    )


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_branch",
        "wrong_repository",
        "missing_identity",
        "provider_down",
        "missing_receipt",
        "symlink",
        "hardlink",
    ],
)
def test_unavailable_provenance_does_not_expose_board_or_erase_history(story, monkeypatch, fault):
    path, _, state, history = story
    monkeypatch.setenv("AGENT_TYPE", "agent-codex-operations")
    if fault == "wrong_branch":
        state["branch"] = "agent/issue-13"
    elif fault == "wrong_repository":
        state["repository"] = "foreign/service"
    elif fault == "missing_identity":
        state.pop("repository_id")
    elif fault == "provider_down":

        def unavailable():
            raise repair.github.MediatedError("provider down")

        monkeypatch.setattr(repair.github, "read_repository", unavailable)
    elif fault == "missing_receipt":
        repair.github._publication_state_path(str(path.parents[2])).unlink()
    else:
        outside = path.parents[2] / "external.json"
        outside.write_text(path.read_text())
        if fault == "symlink":
            path.unlink()
            path.symlink_to(outside)
        else:
            os.link(path, outside.with_name("hardlink.json"))
    result = repair.invoke("repair_tasks", {})
    assert result["operations_tasks"] == history
    assert result["story_board"]["state"] == "unavailable"
    assert "board" not in result["story_board"]


def test_absence_is_explicit_and_large_valid_board_requires_complete_paged_read(story):
    path, value, _, _ = story
    path.unlink()
    assert boards.read_story_board()["state"] == "absent"
    value["tasks"][0]["note"] = "x" * (boards.MAX_CONTEXT_BYTES + 1)
    path.write_text(json.dumps(value))
    result = boards.read_story_board()
    assert result["state"] == "oversized" and result["task_count"] == 2
    assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "board" not in result and "every page" in result["next_action"]


def test_gateway_refusal_stops_before_workspace_read(story, monkeypatch):
    monkeypatch.setenv("AGENT_TYPE", "operations")

    def refused(_):
        raise repair.github.MediatedRefused("revoked")

    monkeypatch.setattr(repair.github, "_request", refused)
    monkeypatch.setattr(
        repair.github, "read_repository", lambda: pytest.fail("read before authorization")
    )
    with pytest.raises(repair.github.MediatedRefused):
        repair.invoke("repair_tasks", {})


def test_board_over_protected_file_bound_has_actionable_retained_history(story, monkeypatch):
    monkeypatch.setenv("AGENT_TYPE", "operations")
    path, value, _, history = story
    value["tasks"][0]["note"] = "x" * (256 * 1024)
    path.write_text(json.dumps(value))
    result = repair.invoke("repair_tasks", {})
    assert result["operations_tasks"] == history
    assert result["story_board"]["reason"] == "story_board_exceeds_file_bound"
    assert "256 KiB" in result["story_board"]["next_action"]
    assert "board" not in result["story_board"]


def test_deeply_nested_untrusted_json_keeps_repair_history(story, monkeypatch):
    monkeypatch.setenv("AGENT_TYPE", "operations")
    path, _, _, history = story
    path.write_text("[" * 2000 + "]" * 2000)
    result = repair.invoke("repair_tasks", {})
    assert result["operations_tasks"] == history
    assert result["story_board"]["state"] == "invalid"
