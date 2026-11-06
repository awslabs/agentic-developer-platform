"""Use the real workflow declaration to verify bounded dispatch readiness."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from pydantic import ValidationError

from src.orchestration.deployment_workflow_provider import WorkflowDefinition
from src.orchestration.repository_producer import RepositoryScanProvider
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.workflow_evaluation_contract import WorkflowEvaluationSpecification

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def contract():
    target_config = json.loads((ROOT / "tests/e2e/cli_uplift/config.example.json").read_text())
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
                target=dict(account_id=target_config["platform_account"], region=target_config["region"]),
                inputs=dict(
                    environment="dev",
                    expected_revision="b" * 40,
                    mode="start",
                    fixtures_json="{}",
                    suites="knowledge",
                    evaluation_id="",
                    inject_fault="none",
                    # #7187 added a protected-target selector to the live lanes; an accepted
                    # contract must pin it (empty = the lane's own default) or preflight
                    # refuses with producer_unaccepted_default_inputs, by design.
                    target_environment="",
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


@pytest.mark.parametrize("value, accepted", [("", True), ("dev", True), ("prod", False), ("staging", False)])
def test_target_environment_input_must_stay_on_the_accepted_environment(contract, value, accepted):
    """#7187 added a protected-target selector; a contract may pin only the lane default or its own environment."""
    raw = contract.model_dump(mode="json")
    raw["producer"]["inputs"]["target_environment"] = value
    if accepted:
        assert WorkflowEvaluationSpecification.model_validate(raw).producer.inputs["target_environment"] == value
    else:
        with pytest.raises(ValidationError, match="accepted environment"):
            WorkflowEvaluationSpecification.model_validate(raw)


def test_specification_without_target_environment_still_validates(contract):
    """Pre-#7187 specifications load; preflight (not the model) refuses to dispatch them until amended."""
    raw = contract.model_dump(mode="json")
    raw["producer"]["inputs"].pop("target_environment")
    assert "target_environment" not in WorkflowEvaluationSpecification.model_validate(raw).producer.inputs


@pytest.mark.parametrize(
    "name, value, accepted",
    [("region", "us-east-1", True), ("region", "eu-west-1", False), ("smoke_target_digest", "", True), ("smoke_target_digest", "a" * 64, False)],
)
def test_new_lane_inputs_stay_bounded_to_the_accepted_target(contract, name, value, accepted):
    """#7695 added `region` and `smoke_target_digest`; a contract may pin only the accepted
    target's region and the lane's regular bindings (empty digest)."""
    raw = contract.model_dump(mode="json")
    raw["producer"]["inputs"][name] = value
    if accepted:
        assert WorkflowEvaluationSpecification.model_validate(raw).producer.inputs[name] == value
    else:
        with pytest.raises(ValueError):
            WorkflowEvaluationSpecification.model_validate(raw)


@pytest.mark.parametrize("change", ["region_default", "unknown_input", "missing_required"])
async def test_preflight_accepts_only_implied_lane_defaults_for_unpinned_inputs(contract, change):
    """A contract accepted before a lane input existed keeps dispatching while the workflow
    default equals the only value the contract could have pinned; any other default, an
    unknown input, or a pinned input the workflow no longer has, refuses."""

    async def blob(binding, path, revision):
        return "c" * 40, (ROOT / path).read_bytes()

    provider = RepositoryScanProvider(evidence=SimpleNamespace(verify_sources=AsyncMock(return_value=([], {})), definition_blob=blob))
    document = yaml.safe_load((ROOT / contract.workflows[0].path).read_text())
    defaults = {name: str(field.get("default", "")) for name, field in document[True]["workflow_dispatch"]["inputs"].items()}
    # `region` is the lane input #7695 added to workflow_dispatch; everything else the
    # workflow exposes is either pinned by the fixture or engine-owned transport.
    assert "region" in defaults
    assert set(defaults) - set(contract.producer.inputs) - set(contract.producer.implied_inputs()) <= {
        "adp_correlation",
        "adp_source_revision",
        "adp_definition_revision",
    }
    if change == "region_default":
        defaults["region"] = "eu-west-1"
    elif change == "unknown_input":
        defaults["new_lane_knob"] = ""
    else:
        defaults.pop("suites")
    provider.definition = AsyncMock(return_value=WorkflowDefinition("b" * 40, "b" * 40, "c" * 40, defaults, True, "main", "b" * 40))
    with pytest.raises(CycleBlockedError, match="producer_unaccepted_default_inputs"):
        await provider.preflight(SimpleNamespace(repo="o/r"), contract, [])
