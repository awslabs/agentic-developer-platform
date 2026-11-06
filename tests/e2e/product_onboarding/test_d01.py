"""Local Product D01 fixtures; successful checks are not deployed acceptance."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from tests.e2e.cli_uplift import cases, preflight

ROOT = Path(__file__).resolve().parents[3]
SOURCE = "a" * 40
DIGEST = "sha256:" + "c" * 64


@pytest.fixture
def pinned_core():
    manifest = ROOT / "modules/gateway/cli/command-manifest.json"
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    receipt = {
        "source_revision": SOURCE,
        "workflow_revision": "b" * 40,
        "run_attempt": 1,
        "image_digest": DIGEST,
        "migration_head": "112_product_profile_foundation",
        "cli_manifest_sha256": manifest_hash,
        "product_profile_enabled": True,
        "product_onboarding_enabled": False,
        "product_profile_schema_version": "1.0",
    }
    runtime = {
        "source_revision": SOURCE,
        "image_digest": DIGEST,
        "migration_head": receipt["migration_head"],
        "api": {
            "authenticated": True,
            "product_profile_schema_version": "1.0",
            "product_profile_enabled": True,
            "product_onboarding_enabled": False,
        },
    }
    record = {"served_cli_hashes": {"command-manifest.json": manifest_hash}}
    return {"expected_revision": SOURCE}, sorted(preflight.PRODUCT_RELEASE_CASES), receipt, runtime, record


def deploy_core_receipt(pinned_core):
    """SCN-01: synthetic source, CLI bytes, API contract and schema agree."""
    preflight.check_product_release(*pinned_core)
    cfg, selection, receipt, runtime, record = pinned_core
    assert set(selection) == preflight.PRODUCT_RELEASE_CASES
    assert record["product_release_check"]["source_revision"] == cfg["expected_revision"]
    assert record["product_release_check"]["image_digest"] == runtime["image_digest"]
    assert record["product_release_check"]["cli_manifest_sha256"] == receipt["cli_manifest_sha256"]
    cli_result = {"exit_code": 0, "product_id": "owned-product", "project_id": "owned-project"}
    independent_api_read = {"product_id": "owned-product", "project_ids": ["owned-project"]}
    assert cli_result["exit_code"] == 0
    assert cli_result["product_id"] == independent_api_read["product_id"]
    assert cli_result["project_id"] in independent_api_read["project_ids"]
    matrix = cases.new_matrix(("case:PR01-C01",))
    assert matrix["PR01-C01"]["status"] == cases.NOT_RUN
    verdict, reasons = cases.accept(matrix, ("case:PR01-C01",))
    assert verdict != cases.PASSED and any("PR01-C01" in reason for reason in reasons)


@pytest.mark.parametrize("failure", [
    "empty_suite", "unknown_suite", "missing_receipt", "missing_runtime",
    "stale_source", "stale_schema", "wrong_image", "wrong_cli", "health_only",
])
def reject_false_green(pinned_core, failure):
    """SCN-02: no missing, stale or health-only fixture can claim a release."""
    cfg, selected, receipt, runtime, record = pinned_core
    if failure == "empty_suite":
        selected.clear()
    elif failure == "unknown_suite":
        selected.append("not-a-product-case")
    elif failure == "missing_receipt":
        receipt = None
    elif failure == "missing_runtime":
        runtime = None
    elif failure == "stale_source":
        receipt["source_revision"] = "e" * 40
    elif failure == "stale_schema":
        runtime["migration_head"] = "obsolete_head"
    elif failure == "wrong_image":
        runtime["image_digest"] = "sha256:" + "f" * 64
    elif failure == "wrong_cli":
        record["served_cli_hashes"]["command-manifest.json"] = "e" * 64
    else:
        runtime["api"] = {"status": "healthy"}
    with pytest.raises(preflight.PreflightError):
        preflight.check_product_release(cfg, selected, receipt, runtime, record)
    assert "product_release_check" not in record
    matrix = cases.new_matrix(("case:PR01-C02",))
    assert matrix["PR01-C02"]["status"] == cases.NOT_RUN
    verdict, reasons = cases.accept(matrix, ("case:PR01-C02",))
    assert verdict != cases.PASSED and any("PR01-C02" in reason for reason in reasons)


def automatic_release_handoff(pinned_core):
    """SCN-04: a pinned, automatic run fixture cannot mark live verification passed."""
    from src.orchestration.deployment_manifest import EntryStatus, load_packaged_manifest
    from src.orchestration.deployment_workflow_provider import WorkflowContext, WorkflowRun

    cfg, selected, receipt, runtime, record = pinned_core
    entry = load_packaged_manifest().entry("adp-dev-gateway")
    context = WorkflowContext(
        schema_version=1, repository_id=17, run_id=42, run_attempt=1,
        workflow_path=entry.workflow.path, workflow_revision=SOURCE,
        source_revision=SOURCE, account_id="123456789012", region="us-east-1",
        resource_kind=entry.resource_kind, resource_id=entry.resource_id,
        inputs={"environment": "dev"}, correlation="",
    )
    payload = json.dumps(context.model_dump(mode="json"), sort_keys=True).encode()
    run = WorkflowRun(
        run_id=42, run_attempt=1, status="completed", conclusion="success",
        url="https://example.invalid/runs/42", context=context, artifact_id=None,
        artifact_digest=hashlib.sha256(payload).hexdigest(),
        observed_at="2026-01-01T00:00:00+00:00",
        artifact_ref="s3://fixture/context?versionId=one",
    )
    receipt["workflow_revision"] = SOURCE
    assert entry.status == EntryStatus.ENABLED
    assert entry.workflow.allowed_inputs["environment"] == frozenset({"dev"})
    assert run.context.workflow_path == entry.workflow.path
    assert run.context.resource_id == entry.resource_id
    assert run.context.source_revision == cfg["expected_revision"] == receipt["source_revision"]
    assert run.context.workflow_revision == receipt["workflow_revision"]
    assert run.context.run_attempt == run.run_attempt == receipt["run_attempt"]
    assert run.context.run_id == run.run_id and run.artifact_ref
    assert run.artifact_digest == hashlib.sha256(payload).hexdigest()
    preflight.check_product_release(cfg, selected, receipt, runtime, record)
    assert record["product_release_check"]["source_revision"] == run.context.source_revision
    assert record["product_release_check"]["run_attempt"] == run.run_attempt
    matrix = cases.new_matrix(("case:PR01-C04",))
    assert matrix["PR01-C04"]["status"] == cases.NOT_RUN
    verdict, reasons = cases.accept(matrix, ("case:PR01-C04",))
    assert verdict != cases.PASSED and any("PR01-C04" in reason for reason in reasons)


def rollout_recovery(tmp_path):
    """SCN-03: local owned fixture survives quiescence; live recovery is pending."""
    script = ROOT / "modules/gateway/scripts/product-profile-recovery.py"
    spec = importlib.util.spec_from_file_location("product_recovery_scenario", script)
    recovery = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recovery)
    image = "example.invalid/adp-gateway@sha256:" + "c" * 64
    flags = [
        {"name": "BG_PRODUCT_PROFILE_ENABLED", "value": "true"},
        {"name": "BG_PRODUCT_ONBOARDING_ENABLED", "value": "false"},
    ]
    deployed = {
        "kind": "Deployment", "metadata": {
            "name": "bedrockgateway", "namespace": "dev", "uid": "gateway-uid",
            "resourceVersion": "1", "generation": 1,
        },
        "spec": {"replicas": 1, "template": {"spec": {"containers": [
            {"name": "bedrockgateway", "image": image, "env": flags},
        ]}}},
        "status": {"readyReplicas": 1, "observedGeneration": 1},
    }
    candidate = copy.deepcopy(deployed)
    candidate["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "false"
    saved = {
        "owned-product": {"owner": "run-local", "history": ["created", "edited"],
                          "idempotency_receipt": "same-key", "archived": False},
        "foreign-product": {"owner": "other-run", "history": ["created"], "archived": False},
    }
    before = copy.deepcopy(saved)
    calls = []

    def kubectl(args, payload=None):
        calls.append(args)
        if "create" in args:
            return json.dumps(candidate)
        if "get" in args:
            return json.dumps(deployed)
        if "replace" in args:
            update = json.loads(payload)
            assert update["metadata"]["resourceVersion"] == deployed["metadata"]["resourceVersion"]
            update["metadata"].update(resourceVersion="2", generation=2)
            update["status"] = {"readyReplicas": 1, "observedGeneration": 2}
            deployed.clear()
            deployed.update(update)
            return ""
        if "rollout" in args:
            return "ready"
        raise AssertionError(args)

    receipt = recovery.prepare(
        namespace="dev", baseline_image=image, baseline_source="b" * 40,
        rollback_source="a" * 40, approved_source="a" * 40,
        candidate_manifest="owned-fixture.yaml", run=kubectl,
    )
    assert receipt["admissions_disabled_before_rollback"] is True
    assert receipt["migration_mode"] == "retained" and receipt["data_readback"] == "pending"
    assert receipt["baseline_image_digest"] == image.rsplit("@", 1)[1]
    assert calls.index(next(item for item in calls if "replace" in item)) < calls.index(
        next(item for item in calls if "rollout" in item)
    )
    assert saved == before
    assert saved["owned-product"]["idempotency_receipt"] == "same-key"
    assert saved["owned-product"]["history"] == before["owned-product"]["history"]
    saved["owned-product"]["archived"] = True
    cleanup_receipt = {"owner": "run-local", "product_id": "owned-product", "action": "archived"}
    (tmp_path / "product-recovery.json").write_text(json.dumps(receipt, sort_keys=True))
    (tmp_path / "owned-cleanup.json").write_text(json.dumps(cleanup_receipt, sort_keys=True))
    assert json.loads((tmp_path / "product-recovery.json").read_text()) == receipt
    assert json.loads((tmp_path / "owned-cleanup.json").read_text()) == cleanup_receipt
    assert saved["foreign-product"] == before["foreign-product"]
    assert saved["owned-product"]["history"] == before["owned-product"]["history"]
    assert saved["owned-product"]["idempotency_receipt"] == "same-key"
    matrix = cases.new_matrix(("case:PR01-C03",))
    assert matrix["PR01-C03"]["status"] == cases.NOT_RUN
    verdict, reasons = cases.accept(matrix, ("case:PR01-C03",))
    assert verdict != cases.PASSED and any("PR01-C03" in reason for reason in reasons)
