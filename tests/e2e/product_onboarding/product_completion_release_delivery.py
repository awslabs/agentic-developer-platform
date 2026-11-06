"""Publish generic delivery evidence for the observed shared Product release."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from tests.e2e.product_onboarding.product_completion_context import verify_contract_pins
from tests.e2e.product_onboarding.product_completion_regression import (
    RELEASE_CHECKS,
    guard_engine,
)
from tests.e2e.product_onboarding.product_completion_release import (
    DEFERRED_INTEGRATIONS,
    _private,
    _write,
    build_manifest,
    release_identity,
)


def publish_delivery(root, environment=os.environ):
    from src.orchestration.delivery_contract import (
        DeliveryContract,
        WorkflowDeliveryEvidence,
        verify_delivery_evidence,
    )
    from src.orchestration.deployment_workflow_provider import WorkflowContext

    root = Path(root)
    guard_engine(environment, root / "engine-authorization.json")
    identity = release_identity(environment)
    contract = DeliveryContract.model_validate(
        _private(root / "delivery-contract.json")
    )
    context = WorkflowContext.model_validate(_private(root / "workflow-context.json"))
    manifest = _private(root / "manifest.json")
    expected = build_manifest(
        _private(root / "component-receipts.json"),
        environment,
        _private(root / "desired.json"),
        _private(root / "current.json"),
    )
    if (
        manifest != expected
        or manifest.get("deferred_integrations") != DEFERRED_INTEGRATIONS
        or set(contract.required_checks) != set(RELEASE_CHECKS)
        or contract.cleanup_required
        or context.inputs != contract.inputs
        or contract.digest != identity["contract_digest"]
    ):
        raise ValueError("Shared release differs from the authorized contract")
    verify_contract_pins(
        contract, environment, authorization=root / "engine-authorization.json"
    )
    for field in (
        "repository_id",
        "run_id",
        "run_attempt",
        "workflow_path",
        "workflow_revision",
        "source_revision",
        "correlation",
    ):
        if getattr(context, field) != identity[field]:
            raise ValueError("Shared release context belongs to another attempt")
    for component, receipt in manifest["components"].items():
        observation = _private(root / receipt["evidence_ref"])
        if component.startswith("gateway-"):
            runtime = observation["runtime"]
            if component == "gateway-backend" and runtime.get("healthy") is not True:
                raise ValueError("Gateway health was not observed")
            if component == "gateway-frontend" and runtime.get("asset_count", 0) < 1:
                raise ValueError("Served frontend was not observed")
        elif component == "installed-cli" and not observation.get("commands"):
            raise ValueError("Installed CLI was not executed")
        elif component == "schema" and not observation.get("migration_head"):
            raise ValueError("Live schema was not observed")
    references = {
        "release-manifest": "manifest.json",
        "gateway-runtime": "evidence/gateway-backend.json",
        "frontend-runtime": "evidence/gateway-frontend.json",
        "installed-cli": "evidence/installed-cli.json",
        "release-health": "evidence/schema.json",
    }
    artifact = WorkflowDeliveryEvidence(
        **context.model_dump(exclude={"schema_version", "inputs"}),
        schema_version=2,
        contract_digest=contract.digest,
        actual_revision=context.source_revision,
        component="product-profile-release",
        checks=[
            {"id": name, "outcome": "passed", "evidence_ref": references[name]}
            for name in RELEASE_CHECKS
        ],
        cleanup="not_required",
        produced_at=datetime.now(UTC),
    )
    verify_delivery_evidence(
        artifact,
        contract=contract,
        context=context,
        component="product-profile-release",
        digest=hashlib.sha256(artifact.model_dump_json().encode()).hexdigest(),
        reference="delivery.json",
    )
    _write(artifact.model_dump(mode="json"), root / "delivery.json")


if __name__ == "__main__":
    publish_delivery(Path(os.environ["RUNNER_TEMP"]) / "product-profile-release")
