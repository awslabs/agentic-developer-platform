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


@pytest.mark.parametrize("persona", ["operations", "agent-codex-operations"])
def test_accepted_operational_story_requires_feature_flag(monkeypatch, persona):
    assignment = document(role="operate", persona=persona)
    monkeypatch.delenv("ADP_ENGINE_OPERATIONS_EXECUTOR", raising=False)
    with pytest.raises(ValueError, match="unsupported_node_executor"):
        selected_executor(assignment, ADDRESS, default="developer", accepted=True)
    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_EXECUTOR", "true")
    assert selected_executor(assignment, ADDRESS, default="developer", accepted=True) == persona
    with pytest.raises(ValueError, match="not_accepted"):
        selected_executor(assignment, ADDRESS, default="developer", accepted=False)


@pytest.mark.parametrize("kind", ["gate", "eval"])
def test_operational_executor_cannot_run_non_story_nodes(monkeypatch, kind):
    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_EXECUTOR", "true")
    assignment = document(role="operate", persona="operations")
    assignment["nodes"][0]["kind"] = kind
    with pytest.raises(ValueError, match="unsupported_node_executor"):
        selected_executor(assignment, ADDRESS, default="developer", accepted=True)


@pytest.mark.parametrize("updates", [{"role": "develop", "persona": "operations"}, {"role": "operate", "persona": "developer"}])
def test_role_persona_mismatch_is_never_admitted(monkeypatch, updates):
    monkeypatch.setenv("ADP_ENGINE_OPERATIONS_EXECUTOR", "true")
    with pytest.raises(ValueError, match="unsupported_node_executor"):
        selected_executor(document(**updates), ADDRESS, default="developer", accepted=True)


def test_disabled_operations_plan_is_readable_but_cannot_dispatch(monkeypatch):
    from src.orchestration.proposal import ProposedNode

    monkeypatch.delenv("ADP_ENGINE_OPERATIONS_EXECUTOR", raising=False)
    value = document(role="operate", persona="agent-codex-operations")
    value["nodes"][0]["title"] = "Deploy the reviewed increment"
    node = ProposedNode.model_validate(value["nodes"][0])
    assert node.executor.role == "operate"
    with pytest.raises(ValueError, match="unsupported_node_executor"):
        selected_executor(value, ADDRESS, default="developer", accepted=True)
