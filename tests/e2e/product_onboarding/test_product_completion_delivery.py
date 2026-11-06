"""Offline delivery assembly; controlled reports never constitute live acceptance."""

import json
import os

import pytest

from tests.e2e.product_onboarding import product_completion_delivery as delivery
from tests.e2e.product_onboarding.product_completion_regression import engine_identity
from tests.e2e.product_onboarding.product_v02_record import private_json
from tests.e2e.product_onboarding.test_product_completion_attempt import attempt as controlled_attempt  # noqa: F401

CORRELATION = "d" * 64
WORKFLOW_PATH = ".github/workflows/product-profile-regression.yml"
# The engine supplies the same transport the authenticated receipt is minted
# from; the synthetic three-key environment could not be authorized at all.
ENGINE_ENVIRONMENT = {
    "GITHUB_REPOSITORY": "aws-e/adp",
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_EVENT_NAME": "workflow_dispatch",
    "GITHUB_WORKFLOW_REF": f"aws-e/adp/{WORKFLOW_PATH}@refs/heads/main",
    "GITHUB_SHA": "a" * 40,
    "GITHUB_WORKFLOW_SHA": "b" * 40,
    "ADP_SOURCE_REVISION": "a" * 40,
    "ADP_DEFINITION_REVISION": "b" * 40,
    "ADP_CORRELATION": CORRELATION,
    "PRODUCT_CORRELATION": CORRELATION,
    "PRODUCT_ENVIRONMENT": "dev",
    "PRODUCT_SUITE": "product-profile",
    "PRODUCT_MAX_PARALLEL": "8",
}


@pytest.fixture(params=["accepted_definition", "repaired_definition"])
def delivery_attempt(controlled_attempt, monkeypatch, request):  # noqa: F811 - imported pytest fixture
    attempt = controlled_attempt
    from src.orchestration.delivery_contract import DeliveryContract
    from src.orchestration.deployment_workflow_provider import WorkflowContext

    for key, value in ENGINE_ENVIRONMENT.items():
        monkeypatch.setenv(key, value)
    checks = (*attempt["cases"], "baseline-regression", "repeatability", "cleanup", "evidence-index")
    contract = DeliveryContract(
        schema_version=2, connection_id="fictional-connection", resource_kind="service",
        resource_id="fictional-test/service", baseline_revision="a" * 40,
        definition_revision="e" * 40 if request.param == "repaired_definition" else "b" * 40,
        required_checks=list(checks), cleanup_required=True,
    )
    monkeypatch.setenv("ADP_CONTRACT_DIGEST", contract.digest)
    context = WorkflowContext(
        schema_version=1, repository_id=123, run_id=42, run_attempt=1,
        workflow_path=WORKFLOW_PATH, workflow_revision="b" * 40,
        source_revision="a" * 40, account_id="123456789012", region="us-east-1",
        resource_kind="service", resource_id="fictional-test/service", correlation=CORRELATION,
        inputs=contract.inputs,
    )
    private_json(attempt["root"] / "engine-authorization.json", engine_identity(os.environ))
    monkeypatch.setattr(delivery, "required_cases", lambda: attempt["cases"])
    monkeypatch.setattr(delivery, "required_checks", lambda: checks)
    return attempt, context, contract


def test_assembles_all_checks_from_two_executed_passes(delivery_attempt):
    from src.orchestration.delivery_contract import WorkflowDeliveryEvidence, verify_delivery_evidence

    attempt, context, contract = delivery_attempt
    destination = delivery.publish_inventory(attempt["root"], context=context, contract=contract, component="product")
    artifact = WorkflowDeliveryEvidence.model_validate_json(destination.read_text())
    assert {check.id for check in artifact.checks} == set(contract.required_checks)
    assert artifact.cleanup == "passed" and artifact.run_attempt == 1
    assert attempt["calls"] == ["pass-1", "pass-2"]
    assert destination.stat().st_mode & 0o777 == 0o600
    for check in artifact.checks:
        assert json.loads((attempt["root"] / check.evidence_ref.removeprefix("evidence/")).read_text())
    verify_delivery_evidence(artifact, contract=contract, context=context, component="product",
                             digest="d" * 64, reference="evidence/delivery.json")


@pytest.mark.parametrize("failure", ["wrong_release", "wrong_workflow", "wrong_workflow_path",
                                      "wrong_correlation", "wrong_attempt", "wrong_digest",
                                      "missing_aggregate", "replayed_delivery", "uncorrelated",
                                      "missing_receipt", "stale_receipt",
                                      "unbound_fixtures", "unreachable_release"])
def test_refuses_mismatched_contract_before_execution(delivery_attempt, failure, monkeypatch):
    attempt, context, contract = delivery_attempt
    root = attempt["root"]
    receipt = root / "engine-authorization.json"
    if failure == "missing_receipt":
        receipt.unlink()
    elif failure == "stale_receipt":
        receipt.unlink()
        identity = engine_identity(os.environ)
        private_json(receipt, {**identity, "run_attempt": identity["run_attempt"] + 1})
    elif failure == "unbound_fixtures":
        # Fixtures bound to a revision the named release never published.
        monkeypatch.setenv("PRODUCT_EXPECTED_REVISION", "c" * 40)
    elif failure == "unreachable_release":
        # A release outside this engine source's lineage stays unusable even
        # when the fixtures and the named manifest agree on its revision.
        from tests.e2e.product_onboarding import product_shared_release

        monkeypatch.setattr(product_shared_release, "verify_shared_source_lineage",
                            lambda manifest, environment: False)
    elif failure == "wrong_workflow_path":
        context = context.model_copy(
            update={"workflow_path": ".github/workflows/product-profile-release.yml"}
        )
    elif failure == "wrong_release":
        context = context.model_copy(update={"source_revision": "c" * 40})
    elif failure == "wrong_workflow":
        context = context.model_copy(update={"workflow_revision": "c" * 40})
    elif failure == "wrong_correlation":
        context = context.model_copy(update={"correlation": "different"})
    elif failure == "wrong_attempt":
        context = context.model_copy(update={"run_attempt": 2})
    elif failure == "wrong_digest":
        context = context.model_copy(update={"inputs": {"adp_contract_digest": "0" * 64}})
    elif failure == "missing_aggregate":
        contract = contract.model_copy(update={"required_checks": list(attempt["cases"])})
        context = context.model_copy(update={"inputs": contract.inputs})
    elif failure == "uncorrelated":
        context = context.model_copy(update={"correlation": ""})
    else:
        (root / "delivery.json").write_text("original")
    with pytest.raises(ValueError):
        delivery.publish_inventory(root, context=context, contract=contract, component="product")
    assert attempt["calls"] == []
    if failure == "replayed_delivery":
        assert (root / "delivery.json").read_text() == "original"
    else:
        assert not (root / "delivery.json").exists()


def test_second_pass_failure_keeps_first_attempt_without_publishing(delivery_attempt):
    attempt, context, contract = delivery_attempt
    def fail_second(root, results):
        if root.name == "pass-2":
            next(iter(results.values()))[0]["status"] = "failed"
    attempt["fault"] = fail_second
    with pytest.raises(ValueError, match="failed or did not execute"):
        delivery.publish_inventory(attempt["root"], context=context, contract=contract, component="product")
    assert (attempt["root"] / "pass-1" / "result.json").is_file()
    assert not (attempt["root"] / "delivery.json").exists()
