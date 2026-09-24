"""Model protocol regressions, not real-model adaptive acceptance claims."""

import copy
import json

import live_evaluation as live
import pytest
from browser_guard import DestinationRefused
from denylist import DenylistResult

from . import test_domain_investigation as fixtures

live_fixture = fixtures.live_fixture


def tool_use(name, data, index=1):
    return {"toolUse": {"toolUseId": str(index), "name": name, "input": data}}


def review(obs, outcome="unresolved"):
    return {
        "hypothesis": "The claimed affiliation requires examination",
        "outcome": outcome,
        "explanation": "Review the observed form and operator disclosure",
        "next_question": "Who operates this flow?",
        "evidence_ids": [obs],
    }


def advance(view, text):
    obs = view["observation"]["id"]
    choice = next(c for c in view["browser"]["choices"] if text in c["text"])
    return tool_use(
        "advance",
        {
            "action": "follow",
            "candidate_id": choice["id"],
            "review": review(obs),
            "decision": {
                "question": "Who operates the verification flow?",
                "reason": "The observed link answers the current question",
                "expected_signal": "Form or operator disclosure",
                "evidence_ids": [obs],
            },
        },
    )


def finish(obs, verdict="inconclusive"):
    return tool_use(
        "finish",
        {
            "review": review(obs, "revised"),
            "reason": "Operator disclosure examined; no form submitted",
            "assessment": {"verdict": verdict, "assessor": "test", "findings": []},
        },
    )


class ProtocolModel:
    def __init__(self, choose):
        self.choose, self.calls = choose, []

    def converse(self, **kwargs):
        for message in kwargs["messages"]:
            for block in message["content"]:
                result = block.get("toolResult", {})
                if result.get("status") == "error":
                    assert all(set(item) == {"text"} for item in result["content"])
        self.calls.append(copy.deepcopy(kwargs))
        blocks = self.choose(len(self.calls), kwargs)
        return {
            "output": {"message": {"role": "assistant", "content": blocks}},
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }


def last_view(kwargs):
    blocks = kwargs["messages"][-1]["content"]
    if "toolResult" in blocks[0]:
        first = blocks[0]["toolResult"]["content"][0]
        return first["json"] if "json" in first else json.loads(first["text"])
    return json.loads(blocks[1]["text"])


def row():
    return {
        "id": "synthetic",
        "url": "https://public.test/seed",
        "objective": "Investigate the verification flow and operator",
    }


def test_live_feedback_preserves_context_and_updates_model_evidence(
    live_fixture, tmp_path
):
    request, transport, clients = live_fixture

    def choose(turn, kwargs):
        view = last_view(kwargs)
        assert view["browser"]["session_open"]
        assert not clients[0].stopped
        assert "assessment" not in view
        if turn == 1:
            assert not view["observation"]["forms"]
            return [advance(view, "verification")]
        if turn == 2:
            assert view["observation"]["forms"]
            assert "Missing session context" not in view["observation"]["visible_text"]
            return [advance(view, "operates")]
        assert "independently of Example" in view["observation"]["visible_text"]
        return [finish(view["observation"]["id"])]

    model = ProtocolModel(choose)
    result = live.investigate(tmp_path / "case", row(), model, request=request)
    assert result["model_completed"] and result["evidence_valid"]
    assert result["adaptive"]["model_browser_actions"] == 2
    assert result["adaptive"]["hypothesis_revisions"] == 1
    assert result["adaptive"]["multiple_observations_in_one_context"]
    assert len(clients) == 1 and clients[0].stopped
    assert all(method in {"GET", "HEAD", "OPTIONS"} for method, _ in transport.requests)
    assert not any("image" in b for b in model.calls[0]["messages"][0]["content"])  # screenshots are model-selected


def test_batched_actions_are_all_rejected_without_navigation(live_fixture, tmp_path):
    request, _, clients = live_fixture

    def choose(turn, kwargs):
        view = last_view(kwargs)
        if turn == 1:
            first = advance(view, "verification")
            second = copy.deepcopy(first)
            second["toolUse"]["toolUseId"] = "2"
            return [first, second]
        assert "Exactly one tool" in view["error"]["message"]
        assert view["observation"]["id"] == "obs-001"
        return [finish("obs-001")]

    result = live.investigate(
        tmp_path / "case", row(), ProtocolModel(choose), request=request
    )
    assert result["model_completed"]
    assert result["adaptive"]["model_browser_actions"] == 0
    assert result["adaptive"]["observations"] == 1
    assert clients[0].stopped


def test_invalid_assessment_can_be_corrected_while_browser_stays_open(
    live_fixture, tmp_path
):
    request, _, clients = live_fixture

    def choose(turn, kwargs):
        view = last_view(kwargs)
        assert not clients[0].stopped
        assert view["browser"]["session_open"]
        call = finish("obs-001")
        if turn == 1:
            call["toolUse"]["input"]["assessment"]["findings"] = [
                {
                    "kind": "other",
                    "basis": "observation",
                    "statement": "Unknown evidence",
                    "evidence_ids": ["obs-999"],
                }
            ]
        else:
            assert "unknown observation" in view["error"]["message"]
        return [call]

    result = live.investigate(
        tmp_path / "case", row(), ProtocolModel(choose), request=request
    )
    assert result["model_completed"] and result["model_turns"] == 2
    assert clients[0].stopped


def test_model_failure_closes_and_leaves_assessment_pending(live_fixture, tmp_path):
    request, _, clients = live_fixture

    def fail(*args):
        raise RuntimeError("Synthetic model outage")

    result = live.investigate(
        tmp_path / "case", row(), ProtocolModel(fail), request=request
    )
    assert not result["model_completed"] and result["verdict"] is None
    assert clients[0].stopped
    assert (
        live.load_case(tmp_path / "case")["assessment"]["assessor"]
        == "collection-system"
    )


def test_browser_only_mode_skips_model_without_observations(tmp_path):
    def unavailable(*args):
        raise DestinationRefused(
            "https://unavailable.test",
            DenylistResult(
                allowed=False,
                reason="Synthetic DNS failure",
                reason_code="resolution_failed",
            ),
        )

    model = ProtocolModel(lambda *args: pytest.fail("No model invocation is allowed"))
    result = live.investigate(
        tmp_path / "case", row(), model, request=unavailable, browser_only=True
    )
    assert result["model_turns"] == 0 and result["verdict"] is None
    assert result["adaptive"]["model_browser_actions"] == 0


def test_finish_schema_resolves_all_nested_references():
    schema = live.tool_contracts()[-1]["toolSpec"]["inputSchema"]["json"]

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                current = schema
                for part in node["$ref"].removeprefix("#/").split("/"):
                    current = current[part]
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(schema)
    assert "verdict" in schema["properties"]["assessment"]["properties"]


def test_local_cli_refuses_dataset_operations(monkeypatch):
    for key in (
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "CODEBUILD_BUILD_ID",
        "AWS_EXECUTION_ENV",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="local dataset downloads are disabled"):
        live.main(
            [
                "--manifest",
                "s3://example/manifest.json",
                "--output-prefix",
                "s3://example/results",
            ]
        )


def test_manifest_rejects_path_traversal_and_duplicate_cases():
    for cases in ([{**row(), "id": "../escape"}], [row(), row()]):
        with pytest.raises(ValueError, match="unique opaque"):
            live.validate_manifest(
                {"schema_version": "cyber-live-evaluation/1", "cases": cases}
            )
