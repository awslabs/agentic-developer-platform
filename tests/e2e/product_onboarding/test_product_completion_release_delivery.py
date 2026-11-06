"""Synthetic release publication tests; they do not constitute live acceptance."""

import json

import pytest

from tests.e2e.product_onboarding import product_completion_release as release
from tests.e2e.product_onboarding.product_completion_regression import (
    RELEASE_CHECKS,
    engine_identity,
)
from tests.e2e.product_onboarding.product_completion_release_delivery import (
    publish_delivery,
)
from tests.e2e.product_onboarding.test_product_completion_release import attempt  # noqa: F401


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "repaired_definition",
        "stale_receipt",
        "unhealthy",
        "missing_observation",
        "changed_manifest",
        "wrong_checks",
    ],
)
def test_delivery_needs_complete_matching_observed_manifest(attempt, failure):  # noqa: F811
    from src.orchestration.delivery_contract import (
        DeliveryContract,
        WorkflowDeliveryEvidence,
    )
    from src.orchestration.deployment_workflow_provider import WorkflowContext

    root, receipt_path, _, environment, _ = attempt
    contract = DeliveryContract(
        schema_version=2,
        connection_id="test-connection",
        resource_kind="service",
        resource_id="test-product",
        definition_revision="d" * 40
        if failure == "repaired_definition"
        else environment["ADP_DEFINITION_REVISION"],
        baseline_revision=environment["ADP_SOURCE_REVISION"],
        workflow_inputs={
            "environment": "dev",
            "suite": "product-profile",
            "max_parallel": "8",
        },
        required_checks=["invented"]
        if failure == "wrong_checks"
        else list(RELEASE_CHECKS),
    )
    environment.update(
        ADP_CONTRACT_DIGEST=contract.digest,
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_WORKFLOW_REF="aws-e/adp/.github/workflows/product-profile-release.yml@refs/heads/main",
    )
    (root / "plan.json").unlink()
    release.write_plan(
        root / "desired.json",
        root / "current.json",
        root / "plan.json",
        environment=environment,
    )
    release.publish(
        receipt_path,
        root / "manifest.json",
        desired_path=root / "desired.json",
        current_path=root / "current.json",
        plan_path=root / "plan.json",
        environment=environment,
    )
    identity = release.release_identity(environment)
    context = WorkflowContext(
        schema_version=1,
        **{
            key: identity[key]
            for key in (
                "repository_id",
                "run_id",
                "run_attempt",
                "workflow_path",
                "workflow_revision",
                "source_revision",
                "correlation",
            )
        },
        account_id="000000000000",
        region="us-east-1",
        resource_kind=contract.resource_kind,
        resource_id=contract.resource_id,
        inputs=contract.inputs,
    )
    receipt_identity = engine_identity(environment)
    if failure == "stale_receipt":
        receipt_identity = {
            **receipt_identity,
            "run_attempt": receipt_identity["run_attempt"] + 1,
        }
    release._write(receipt_identity, root / "engine-authorization.json")
    release._write(context.model_dump(mode="json"), root / "workflow-context.json")
    release._write(contract.model_dump(mode="json"), root / "delivery-contract.json")
    evidence = root / "evidence"
    evidence.mkdir(mode=0o700)
    for name, observation in {
        "gateway-backend": {"runtime": {"healthy": failure != "unhealthy"}},
        "gateway-frontend": {"runtime": {"asset_count": 1}},
        "installed-cli": {"commands": [{"exit_code": 0}]},
        "schema": {"migration_head": "test_head"},
    }.items():
        release._write(observation, evidence / (name + ".json"))
    if failure == "missing_observation":
        (evidence / "schema.json").unlink()
    if failure == "changed_manifest":
        (root / "manifest.json").write_text("{}")
    if failure and failure != "repaired_definition":
        with pytest.raises(ValueError):
            publish_delivery(root, environment)
        assert not (root / "delivery.json").exists()
    else:
        # An accepted repair merged after acceptance advances the definition;
        # the authenticated receipt already approved these effective pins.
        assert (failure == "repaired_definition") == (
            contract.definition_revision != context.workflow_revision
        )
        publish_delivery(root, environment)
        assert (
            WorkflowDeliveryEvidence.model_validate_json(
                (root / "delivery.json").read_text()
            ).workflow_revision
            == environment["ADP_DEFINITION_REVISION"]
        )
        artifact = WorkflowDeliveryEvidence.model_validate_json(
            (root / "delivery.json").read_text()
        )
        assert {check.id for check in artifact.checks} == set(RELEASE_CHECKS)
        assert artifact.contract_digest == contract.digest
        assert artifact.cleanup == "not_required"
        assert (
            json.loads((root / "manifest.json").read_text())["deferred_integrations"]
            == release.DEFERRED_INTEGRATIONS
        )
        assert set(release.DEFERRED_INTEGRATIONS) == {
            "engineering-insights",
            "context-l5",
        }
        assert all(
            item["status"] == "deferred" and item["live_acceptance"] is False
            for item in release.DEFERRED_INTEGRATIONS.values()
        )
