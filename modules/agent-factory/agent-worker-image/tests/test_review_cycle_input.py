"""Actual bootstrap input and bound-PR checkout, without live provider calls."""
import json
from types import SimpleNamespace

import pytest

from lib.review_cycle_input import ENV, checkout_cycle_input, prepare_cycle_input


def envelope():
    return {
        "intent": {"trigger": "engine_review_cycle"}, "persona": "developer", "source_ref": {"repo": "org/repo"},
        "review_cycle_input": {"action": "repair", "repo": "org/repo", "pr_number": 77, "head_sha": "a" * 40,
            "accepted_scope": '{"node":{"title":"Accepted story"}}', "operation_key": "dispatch:1",
            "findings": [{"finding_id": "F1", "summary": "Boundary fails", "evidence_refs": []}],
            "remaining_attempts": 2, "remaining_spend_usd": "4.00"},
    }


def test_input_reaches_actual_child_environment(monkeypatch):
    monkeypatch.setenv(ENV, "stale-other-run")
    value = prepare_cycle_input(envelope())
    import os
    assert json.loads(os.environ[ENV]) == value
    assert value["findings"][0]["finding_id"] == "F1"
    assert value["remaining_attempts"] == 2
    prepare_cycle_input({})
    assert ENV not in os.environ


@pytest.mark.parametrize("field,value", [("repo", "other/repo"), ("head_sha", "bad"), ("action", "merge"),
    ("pr_number", True), ("accepted_scope", ""), ("findings", "not-a-list")])
def test_malformed_or_cross_scope_input_refuses(monkeypatch, field, value):
    monkeypatch.delenv(ENV, raising=False)
    message = envelope()
    message["review_cycle_input"][field] = value
    with pytest.raises(RuntimeError):
        prepare_cycle_input(message)


def test_checkout_uses_bound_pr_without_creating_or_resetting_branch():
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="a" * 40 if command == ["git", "rev-parse", "HEAD"] else "existing-pr-branch")
    branch, head = checkout_cycle_input(envelope()["review_cycle_input"], run=run, cwd="/isolated")
    assert branch == "existing-pr-branch" and head == "a" * 40
    assert calls == [["gh", "pr", "checkout", "77", "--repo", "org/repo"], ["git", "rev-parse", "HEAD"],
        ["git", "branch", "--show-current"]]


def test_moved_head_refuses_before_model_exec():
    with pytest.raises(RuntimeError, match="head changed"):
        checkout_cycle_input(envelope()["review_cycle_input"], run=lambda *a, **k: SimpleNamespace(stdout="b" * 40), cwd="/isolated")
