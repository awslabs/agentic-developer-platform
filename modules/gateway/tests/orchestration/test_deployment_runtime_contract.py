"""Keep independently deployed components' evidence requirements isolated."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.orchestration.deployment_controller import ADAPTERS
from src.orchestration.deployment_runtime_contract import ReleaseArtifact, RuntimeComponent

DIGEST = "sha256:" + "b" * 64
COMPONENT_EVIDENCE = {
    "gateway-backend": {"image_digest": DIGEST, "pod_uids": ["pod"], "migration_head": "schema-head", "tick_digest": DIGEST},
    "gateway-frontend": {"asset_count": 1},
    "gateway-migrations": {"image_digest": DIGEST, "pod_uids": ["pod"], "migration_head": "schema-head"},
    "tools-facade": {"image_digest": DIGEST, "pod_uids": ["pod"]},
    "tool-adapters": {
        "image_digest": DIGEST,
        "pod_uids": ["adapter-pod-one", "adapter-pod-two"],
        "manifests_sha256": "d" * 64,
        "adapter_contract_sha256": "e" * 64,
        "adapter_probe_path": "api_server_proxy",
    },
    "insights": {"image_digest": DIGEST, "pod_uids": ["pod"], "asset_count": 1},
}


def runtime_fields(component):
    return {
        "component": component,
        "actual_revision": "a" * 40,
        "artifact_hash": "c" * 64,
        "healthy": True,
        "evidence_ref": "github/actions/runs/2/artifacts/3",
        "observed_at": datetime.now(UTC),
        **COMPONENT_EVIDENCE[component],
    }


@pytest.mark.parametrize("component", COMPONENT_EVIDENCE)
def test_runtime_accepts_each_components_own_evidence(component):
    runtime = RuntimeComponent(**runtime_fields(component))
    assert runtime.component == component
    assert runtime.healthy


@pytest.mark.parametrize(
    ("component", "missing_evidence"),
    [
        ("insights", {"image_digest": None}),
        ("insights", {"pod_uids": []}),
        ("insights", {"asset_count": 0}),
        ("insights", {"migration_head": "schema-head"}),
        ("tools-facade", {"image_digest": None}),
        ("tools-facade", {"pod_uids": []}),
        ("tool-adapters", {"image_digest": None}),
        ("tool-adapters", {"pod_uids": ["adapter-pod-one"]}),
        ("tool-adapters", {"pod_uids": ["same", "same"]}),
        ("tool-adapters", {"adapter_probe_path": None}),
        ("tool-adapters", {"adapter_contract_sha256": None}),
        ("tool-adapters", {"manifests_sha256": None}),
        ("tool-adapters", {"migration_head": "unrelated-head"}),
        ("tools-facade", {"adapter_contract_sha256": "e" * 64}),
        ("gateway-backend", {"migration_head": None}),
        ("gateway-backend", {"tick_digest": "sha256:" + "d" * 64}),
        ("gateway-migrations", {"migration_head": None}),
        ("gateway-frontend", {"asset_count": 0}),
    ],
)
def test_runtime_rejects_incomplete_or_cross_component_evidence(component, missing_evidence):
    with pytest.raises(ValidationError):
        RuntimeComponent(**(runtime_fields(component) | missing_evidence))


def test_adapter_reader_is_scoped_to_its_component():
    assert ADAPTERS["tool-adapters-runtime-verification"] == frozenset({"tool-adapters"})


@pytest.mark.parametrize(
    ("component", "evidence", "valid"),
    [
        ("insights", {"assets": {"platform/evidence.tf": "c" * 64}}, True),
        ("insights", {"image_digest": DIGEST}, False),
        ("tools-facade", {"image_digest": DIGEST}, True),
        ("tools-facade", {"image_digest": DIGEST, "compatibility": {}}, False),
        ("tools-facade", {"assets": {"platform/evidence.tf": "c" * 64}}, False),
        (
            "tool-adapters",
            {"image_digest": DIGEST, "assets": {"adapter-manifest.json": "d" * 64, "adapter-contract.json": "e" * 64}},
            False,  # Hashes without the authenticated expected documents are insufficient.
        ),
        ("tool-adapters", {"image_digest": DIGEST, "assets": {"adapter-manifest.json": "d" * 64}}, False),
        (
            "tool-adapters",
            {"image_digest": DIGEST, "assets": {"adapter-manifest.json": "d" * 64, "adapter-contract.json": "e" * 64, "extra.json": "f" * 64}},
            False,
        ),
        (
            "tool-adapters",
            {"assets": {"adapter-manifest.json": "d" * 64, "adapter-contract.json": "e" * 64}},
            False,
        ),
    ],
)
def test_release_preserves_insights_and_tools_artifact_contracts(component, evidence, valid):
    fields = {
        "schema_version": 1,
        "repository_id": 1,
        "run_id": 2,
        "run_attempt": 1,
        "source_revision": "a" * 40,
        "workflow_revision": "b" * 40,
        "workflow_path": ".github/workflows/component-deploy.yml",
        "account_id": "000000000000",
        "region": "us-east-1",
        "resource_id": "cluster/component",
        "component": component,
        "produced_at": datetime.now(UTC),
        **evidence,
    }
    if valid:
        assert ReleaseArtifact(**fields).component == component
    else:
        with pytest.raises(ValidationError):
            ReleaseArtifact(**fields)
