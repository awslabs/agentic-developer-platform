"""Use the real workflow declaration to verify bounded dispatch readiness."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from src.orchestration.deployment_workflow_provider import WorkflowDefinition
from src.orchestration.repository_producer import RepositoryScanProvider
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.workflow_evaluation_contract import WorkflowEvaluationSpecification

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def contract():
    return WorkflowEvaluationSpecification.model_validate(
        dict(
            evidence_schema="workflow-evaluation/v1",
            acceptance_mode="human",
            runner=dict(adapter="engine-repository-evidence-v1", repository="o/r", repository_id=1, harness_sha256="a" * 64),
            workflows=[
                dict(
                    criterion_id="qualification",
                    path=".github/workflows/eval-cli-uplift.yml",
                    source=dict(revision="b" * 40),
                    definition=dict(revision="b" * 40),
                    required_jobs=["Live evaluation (dev)", "Recovery sweep (this run, plus anything expired)"],
                    artifacts=[
                        dict(
                            name="e32-qualification-result",
                            path="report.json",
                            predicates=[dict(criterion_id="passed", pointer="/status", operation="equals", expected="passed")],
                        )
                    ],
                )
            ],
            producer=dict(
                workflow_criterion_id="qualification",
                target=dict(account_id="879318057152", region="us-east-1"),
                inputs=dict(
                    environment="dev",
                    expected_revision="b" * 40,
                    mode="start",
                    fixtures_json="{}",
                    suites="knowledge",
                    evaluation_id="",
                    inject_fault="none",
                ),
                receipt_artifact="e32-qualification-result",
                receipt_path="report.json",
            ),
        )
    )


@pytest.mark.parametrize("wrong_target", [False, True])
async def test_real_workflow_preflight_checks_target_and_all_inputs(contract, wrong_target):
    async def blob(binding, path, revision):
        assert revision == "b" * 40
        return "c" * 40, (ROOT / path).read_bytes()

    provider = RepositoryScanProvider(evidence=SimpleNamespace(verify_sources=AsyncMock(return_value=([], {})), definition_blob=blob))
    document = yaml.safe_load((ROOT / contract.workflows[0].path).read_text())
    defaults = {name: str(field.get("default", "")) for name, field in document[True]["workflow_dispatch"]["inputs"].items()}
    provider.definition = AsyncMock(return_value=WorkflowDefinition("b" * 40, "b" * 40, "c" * 40, defaults, True, "main", "b" * 40))
    if wrong_target:
        raw = contract.model_dump(mode="json")
        raw["producer"]["target"]["account_id"] = "999999999999"
        contract = WorkflowEvaluationSpecification.model_validate(raw)
        with pytest.raises(CycleBlockedError, match="qualification_target_configuration_changed"):
            await provider.preflight(SimpleNamespace(repo="o/r"), contract, [])
        provider.definition.assert_not_awaited()
    else:
        result = await provider.preflight(SimpleNamespace(repo="o/r"), contract, [])
        assert result["source_revision"] == "b" * 40


@pytest.mark.parametrize("change", ["full", "revision", "cleanup", "extra_input"])
def test_contract_refuses_wider_or_unverifiable_qualification(contract, change):
    raw = contract.model_dump(mode="json")
    if change == "full":
        raw["producer"]["inputs"]["suites"] = "full"
    elif change == "revision":
        raw["producer"]["inputs"]["expected_revision"] = "d" * 40
    elif change == "cleanup":
        raw["workflows"][0]["required_jobs"].pop()
    else:
        raw["producer"]["inputs"]["extra"] = "ignored"
    with pytest.raises(ValueError):
        WorkflowEvaluationSpecification.model_validate(raw)
