"""Assignment identity, compatibility and acceptance fences."""

import pytest

from src.orchestration.executor_assignment import selected_executor

ADDRESS = "flow/epic/wave/story"


def document(**updates):
    return {
        "nodes": [
            {
                "address": ADDRESS,
                "kind": "story",
                "executor": {
                    "kind": "agent",
                    "role": "develop",
                    "persona": "agent-codex-developer",
                    **updates,
                },
            }
        ]
    }


def test_assignment_does_not_leak_to_another_story():
    assert selected_executor(document(), "flow/epic/wave/other", default="developer", accepted=True) == "developer"


@pytest.mark.parametrize("updates", [{"persona": "operations"}, {"role": "review"}, {"kind": "workflow"}, {"schema_version": 2}])
def test_unsupported_execution_never_falls_back(updates):
    with pytest.raises(ValueError):
        selected_executor(document(**updates), ADDRESS, default="developer", accepted=True)


def test_draft_assignment_cannot_dispatch_even_when_it_matches_the_default():
    with pytest.raises(ValueError, match="not_accepted"):
        selected_executor(document(persona="developer"), ADDRESS, default="developer", accepted=False)


def test_duplicate_assignment_is_not_arbitrarily_selected():
    value = document()
    value["nodes"] *= 2
    with pytest.raises(ValueError, match="ambiguous"):
        selected_executor(value, ADDRESS, default="developer", accepted=True)


def test_gate_cannot_become_an_agent_by_assignment():
    value = document()
    value["nodes"][0]["kind"] = "gate"
    with pytest.raises(ValueError, match="unsupported_node_executor"):
        selected_executor(value, ADDRESS, default="developer", accepted=True)
