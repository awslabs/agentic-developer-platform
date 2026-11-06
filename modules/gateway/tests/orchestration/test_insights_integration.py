"""Offline check that Insights changes reach a fail-closed D3 adapter."""

from datetime import UTC, datetime

import pytest

from src.orchestration.deployment_controller import ADAPTERS
from src.orchestration.deployment_manifest import EntryStatus, load_packaged_manifest
from src.orchestration.deployment_runtime_contract import ReleaseArtifact, RuntimeComponent
from src.orchestration.deployment_workflows import components_for_paths
from src.orchestration.review_cycle import CycleBlockedError


@pytest.mark.parametrize(
    "path", ["modules/insights/emitter/service.py", "modules/insights/platform/evidence.tf", ".github/workflows/insights-deploy.yml"]
)
def test_insights_source_selects_only_its_own_release(path):
    assert components_for_paths([path]) == ("insights",)
    assert components_for_paths([path, "modules/gateway/src/api.py"]) == ("gateway-backend", "insights")


def test_manifest_reserves_insights_adapter_without_approving_unknown_target():
    manifest = load_packaged_manifest()
    entry = next(entry for entry in manifest.entries if entry.entry_id == "adp-dev-insights")
    assert entry.status is EntryStatus.UNRESOLVED
    assert entry.workflow.path == ".github/workflows/insights-deploy.yml"
    assert entry.verification_adapter == "insights-health-verification"
    assert ADAPTERS[entry.verification_adapter] == frozenset({"insights"})
    assert entry.connection_id is None and entry.resource_id is None


def test_insights_release_accepts_hashed_infrastructure_but_not_false_health():
    fields = dict(
        schema_version=1,
        repository_id=1,
        run_id=2,
        run_attempt=1,
        source_revision="a" * 40,
        workflow_revision="b" * 40,
        workflow_path=".github/workflows/insights-deploy.yml",
        account_id="000000000000",
        region="us-east-1",
        resource_id="cluster/adp-insights",
        component="insights",
        assets={"platform/evidence.tf": "c" * 64},
        produced_at=datetime.now(UTC),
    )
    assert ReleaseArtifact(**fields).image_digest is None
    with pytest.raises(ValueError, match="bounded infrastructure"):
        ReleaseArtifact(**(fields | {"assets": {"../secrets": "c" * 64}}))
    runtime = dict(
        component="insights",
        actual_revision="a" * 40,
        artifact_hash="d" * 64,
        healthy=True,
        evidence_ref="github/actions/runs/2/artifacts/3",
        observed_at=datetime.now(UTC),
        pod_uids=["pod"],
        asset_count=1,
        image_digest=None,
    )
    with pytest.raises(ValueError, match="serving pods"):
        RuntimeComponent(**runtime)
    assert RuntimeComponent(**(runtime | {"image_digest": "sha256:" + "e" * 64})).healthy


def test_unmapped_path_still_blocks_dispatch():
    with pytest.raises(CycleBlockedError, match="deployment_component_unmapped"):
        components_for_paths(["unexpected/provider.py"])
