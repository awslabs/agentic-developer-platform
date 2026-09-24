"""Hosted-SDK handoff and artifact integrity; model reasoning is tested in AWS."""

import json
import subprocess
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import domain_investigation as cli
import hosted_evaluation as hosted
import pytest
from research_case import verify_case

from .test_domain_investigation import live_fixture as browser_fixture
from .test_domain_investigation import review
from .test_live_evaluation import row

live_fixture = browser_fixture


def test_sdk_transcript_does_not_invalidate_case_during_agent_tools(
    live_fixture, tmp_path, monkeypatch
):
    request, _, clients = live_fixture
    monkeypatch.setattr(cli, "start", partial(cli.start, request=request))
    monkeypatch.setattr(cli, "close", partial(cli.close, request=request))
    out = tmp_path / "case"

    def sdk(command, **kwargs):
        assert command[0] == "node"
        task = json.loads(kwargs["input"])
        assert "URL analyst reasoning" in task["instructions"]
        transcript = Path(task["transcript"])
        assert transcript.parent == out.parent
        transcript.write_text('{"type":"synthetic_sdk_start"}\n')
        case = json.loads((out / "case.json").read_text())
        review(case, out)
        transcript.write_text(
            transcript.read_text() + '{"type":"synthetic_sdk_tool"}\n'
        )
        cli.finish(
            out,
            {
                "verdict": "inconclusive",
                "assessor": "synthetic-hosted-model",
                "model_version": "invented-by-model",
            },
            "Synthetic SDK finished",
            request=request,
        )
        return SimpleNamespace(returncode=0, stderr="")

    result = hosted.run_case(out, row(), "test-model", run=sdk)
    assert result["model_completed"] and clients[0].stopped
    assert result["assessment"]["model_version"] == "test-model"
    assert (out / "hosted-transcript.jsonl").exists()
    assert verify_case(out) > 0


def test_hosted_dataset_entrypoint_refuses_local_execution(monkeypatch):
    for name in (
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "CODEBUILD_BUILD_ID",
        "AWS_EXECUTION_ENV",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="local dataset downloads"):
        hosted.main(
            [
                "--manifest",
                "s3://example/manifest",
                "--output-prefix",
                "s3://example/output",
            ]
        )


@pytest.mark.parametrize("failure", ["exit", "timeout", "missing_runtime"])
def test_hosted_failure_closes_browser_and_preserves_valid_findings(
    live_fixture, tmp_path, monkeypatch, failure
):
    request, _, clients = live_fixture
    monkeypatch.setattr(cli, "start", partial(cli.start, request=request))
    monkeypatch.setattr(cli, "close", partial(cli.close, request=request))
    out = tmp_path / "case"

    def sdk(command, **kwargs):
        good = {
            "kind": "other",
            "basis": "observation",
            "statement": "The page offers an account-verification link.",
            "evidence_ids": ["obs-001"],
        }
        with pytest.raises(ValueError):
            cli.finish(
                out,
                {
                    "verdict": "suspicious",
                    "assessor": "synthetic-hosted-model",
                    "findings": [good, {**good, "evidence_ids": ["obs-999"]}],
                },
                "Synthetic rejected assessment",
                request=request,
            )
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 275)
        if failure == "missing_runtime":
            raise FileNotFoundError("Synthetic missing node executable")
        return SimpleNamespace(returncode=1, stderr="Synthetic SDK failure")

    result = hosted.run_case(out, row(), "test-model", run=sdk)
    assert result["error"] and not result["model_completed"]
    assert result["cleanup_confirmed"] and clients[0].stopped
    assert result["assessment"]["verdict"] == "inconclusive"
    assert len(result["assessment"]["findings"]) == 1
    assert verify_case(out) > 0
