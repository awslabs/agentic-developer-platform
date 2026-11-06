"""Reject façade receipts that cannot pass the engine's actual release schema."""

import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.orchestration.deployment_runtime_contract import ReleaseArtifact

ROOT = Path(__file__).resolve().parents[4]
SPEC = importlib.util.spec_from_file_location("facade_receipt", ROOT / "modules/tools/scripts/facade_receipt.py")
RECEIPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECEIPT)


def test_facade_release_matches_engine_schema():
    digest = "sha256:" + "a" * 64
    environment = {
        "ADP_SOURCE_REVISION": "b" * 40,
        "ADP_WORKFLOW_REVISION": "c" * 40,
        "ADP_ACCOUNT_ID": "123456789012",
        "AWS_REGION": "us-east-1",
        "ADP_ENVIRONMENT": "dev",
        "ADP_TOOLS_EKS_CLUSTER_NAME": "adp-dev-eks-cluster",
        "ADP_TOOLS_NAMESPACE": "adp-tools",
        "DEPLOYED_IMAGE": f"123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-tools-facade@{digest}",
        "OBSERVED_ECR_DIGEST": digest,
        "GITHUB_REPOSITORY_ID": "10",
        "GITHUB_RUN_ID": "20",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    context, receipt = RECEIPT.evidence(environment)
    parsed = ReleaseArtifact.model_validate(receipt)
    assert parsed.component == "tools-facade"
    assert parsed.image_digest == digest
    assert parsed.source_revision == context["source_revision"]


def test_engine_rejects_unrecognized_or_missing_release_fields():
    digest = "sha256:" + "a" * 64
    release = {
        "schema_version": 1,
        "repository_id": 10,
        "run_id": 20,
        "run_attempt": 1,
        "source_revision": "b" * 40,
        "workflow_revision": "c" * 40,
        "workflow_path": ".github/workflows/tools-facade-deploy.yml",
        "account_id": "123456789012",
        "region": "us-east-1",
        "resource_id": "adp-dev-eks-cluster/adp-tools/tools-facade",
        "component": "tools-facade",
        "image_digest": digest,
        "assets": {},
        "produced_at": "2026-01-01T00:00:00+00:00",
    }
    for invalid in (release | {"image": "unrecognized"}, {key: value for key, value in release.items() if key != "produced_at"}):
        with pytest.raises(ValidationError):
            ReleaseArtifact.model_validate(invalid)
