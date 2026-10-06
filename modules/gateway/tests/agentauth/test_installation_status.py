"""Runtime observations are not the authoritative installed-release ledger."""

from datetime import UTC, datetime, timedelta

import pytest

from src.agentauth.installation_failure import read_installation_failure
from src.agentauth.installation_reader import InstallationReader
from src.agentauth.installation_status import read_installation_status
from src.orchestration.deployment_runtime_contract import DeploymentReceipt
from src.orchestration.merge_controller import MERGE_KIND, MergeReceipt
from src.orchestration.models import (
    OrchestrationAction,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
)
from src.shared.models.organization import Organization

OBSERVED = datetime(2026, 10, 1, tzinfo=UTC)
REVISION = "a" * 40
REVIEWED_REVISION = "d" * 40


def receipt():
    return DeploymentReceipt(
        org_id="tenant-a",
        execution_id="execution-a",
        node_id="node-a",
        flow_id="flow-a",
        cycle=1,
        accepted_plan_version=1,
        claim_id="claim-a",
        claim_generation=1,
        operation_key="verify-a",
        repo="example/project",
        source_revision=REVISION,
        actual_revision=REVISION,
        merge_operation_key="merge-a",
        workflow_operation_keys=["workflow-a"],
        manifest_entry_ids=["entry-a"],
        targets=[{"secret_reference": "canary-secret-123"}],
        components=[
            {
                "component": "gateway-frontend",
                "actual_revision": REVISION,
                "artifact_hash": "b" * 64,
                "healthy": True,
                "evidence_ref": "internal-canary-secret-123",
                "observed_at": OBSERVED,
                "asset_count": 1,
            }
        ],
        observed_at=OBSERVED,
        valid_until=OBSERVED + timedelta(minutes=10),
    )


def merge_receipt():
    return MergeReceipt(
        org_id="tenant-a",
        execution_id="execution-a",
        node_id="node-a",
        flow_id="flow-a",
        cycle=1,
        accepted_plan_version=1,
        claim_id="claim-a",
        claim_generation=1,
        operation_key="merge-a",
        repo="example/project",
        pr_number=1,
        provider_repository_id=1,
        provider_pr_node_id="PR-a",
        reviewed_head_sha=REVIEWED_REVISION,
        reviewed_base_sha="e" * 40,
        merge_sha=REVISION,
        review_ref="review-a",
        method="squash",
        eligibility_digest="f" * 64,
        eligibility_observed_at=OBSERVED - timedelta(minutes=2),
        merged_at=OBSERVED - timedelta(minutes=1),
        observed_at=OBSERVED - timedelta(minutes=1),
        adopted=False,
    )


@pytest.fixture
async def runtime_observation(db_session):
    db_session.add(Organization(id="tenant-a", name="Tenant A"))
    await db_session.flush()
    db_session.add(OrchestrationFlow(id="flow-a", org_id="tenant-a", slug="test", title="Test"))
    await db_session.flush()
    db_session.add(
        OrchestrationNode(
            id="node-a",
            org_id="tenant-a",
            flow_id="flow-a",
            epic_ref="epic",
            wave_ref="wave",
            node_ref="node",
            kind="story",
            title="Deploy",
        )
    )
    await db_session.flush()
    db_session.add(
        OrchestrationPullRequestBinding(
            id="binding-a",
            org_id="tenant-a",
            flow_id="flow-a",
            node_id="node-a",
            attempt=1,
            provider_repository_id=1,
            provider_pr_node_id="PR-a",
            repo="example/project",
            pr_number=1,
            installation_id=1234,
            head_sha=REVIEWED_REVISION,
            registered_by="installer",
            registered_by_kind="human",
        )
    )
    db_session.add(
        OrchestrationExecution(
            id="execution-a",
            org_id="tenant-a",
            flow_id="flow-a",
            node_id="node-a",
            cycle=1,
            phase="concluded",
            status="completed",
            claim_id="claim-a",
            claim_generation=1,
        )
    )
    await db_session.flush()
    db_session.add(
        OrchestrationAction(
            id="merge-action-a",
            org_id="tenant-a",
            execution_id="execution-a",
            operation_key="merge-a",
            kind=MERGE_KIND,
            status="succeeded",
            observed_at=OBSERVED - timedelta(minutes=1),
            detail={"merge_receipt": merge_receipt().model_dump(mode="json")},
        )
    )
    db_session.add(
        OrchestrationAction(
            org_id="tenant-a",
            execution_id="execution-a",
            operation_key="verify-a",
            kind="deployment_verification",
            status="succeeded",
            observed_at=OBSERVED,
            detail={"deployment_receipt": receipt().model_dump(mode="json")},
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_runtime_receipt_is_scoped_partial_and_stales_without_fabricating_versions(db_session, runtime_observation):
    scoped = InstallationReader(tenant_id="tenant-a", installation_id=1234)
    fresh = await read_installation_status(db_session, scoped, now=OBSERVED + timedelta(minutes=1))
    assert fresh["status"] == "partial"
    assert fresh["components"] == [
        {
            "component": "gateway-frontend",
            "version": None,
            "observed_revision": REVISION,
            "health": "observed_healthy",
            "observed_at": OBSERVED.isoformat(),
            "evidence": "runtime_verification",
        }
    ]
    assert fresh["desired_release"]["status"] == fresh["last_verified_release"]["status"] == "unavailable"
    assert fresh["capabilities"]["installed_record"] == {"status": "unavailable", "reason": "installed_record_provider_unavailable"}
    assert fresh["optional_modules"]["status"] == "unknown"
    assert fresh["coverage"] == "runtime_receipt_only"
    assert "canary-secret-123" not in str(fresh)
    stale = await read_installation_status(db_session, scoped, now=OBSERVED + timedelta(minutes=10))
    assert stale["components"][0]["health"] == "stale"
    assert stale["components"][0]["version"] is None

    other = await read_installation_status(db_session, InstallationReader(tenant_id="tenant-a", installation_id=5678), now=OBSERVED)
    assert other["status"] == "unavailable" and other["installation_id"] == 5678
    assert other["capabilities"]["installed_record"]["status"] == "unavailable"
    foreign = await read_installation_status(db_session, InstallationReader(tenant_id="tenant-b", installation_id=1234), now=OBSERVED)
    assert foreign["status"] == "unavailable"

    execution = await db_session.get(OrchestrationExecution, "execution-a")
    execution.block_code = "provider_unavailable"
    execution.block_detail = "credential-ref:canary-secret-123"
    db_session.add(
        OrchestrationAction(
            org_id="tenant-a",
            execution_id="execution-a",
            operation_key="workflow-failed",
            kind="deployment_workflow",
            status="failed",
            created_at=OBSERVED + timedelta(minutes=2),
            observed_at=OBSERVED + timedelta(minutes=3),
            detail={"log": "canary-secret-123", "state_url": "s3://private-state"},
            receipt_ref="internal-canary-secret-123",
        )
    )
    await db_session.commit()
    failure = await read_installation_failure(db_session, scoped)
    assert failure["status"] == "partial"
    assert failure["failure"] == {
        "stage": "workflow_dispatch",
        "outcome": "failed",
        "started_at": (OBSERVED + timedelta(minutes=2)).isoformat(),
        "observed_at": (OBSERVED + timedelta(minutes=3)).isoformat(),
        "reason": "provider_unavailable",
    }
    assert "canary-secret-123" not in str(failure)
    assert "private-state" not in str(failure)
    assert (await read_installation_failure(db_session, InstallationReader(tenant_id="tenant-a", installation_id=5678)))["status"] == "unavailable"

    db_session.add(
        OrchestrationAction(
            org_id="tenant-a",
            execution_id="execution-a",
            operation_key="workflow-unknown",
            kind="deployment_workflow",
            status="unknown",
            created_at=OBSERVED + timedelta(minutes=4),
            detail={"log": "canary-secret-123"},
        )
    )
    await db_session.commit()
    unknown = await read_installation_failure(db_session, scoped)
    assert unknown["failure"]["outcome"] == "unknown"
    assert unknown["failure"]["observed_at"] is None

    db_session.add(
        OrchestrationPullRequestBinding(
            id="binding-new",
            org_id="tenant-a",
            flow_id="flow-a",
            node_id="node-a",
            attempt=2,
            provider_repository_id=2,
            provider_pr_node_id="PR-new",
            repo="example/project",
            pr_number=2,
            installation_id=1234,
            head_sha="c" * 40,
            registered_by="installer",
            registered_by_kind="human",
        )
    )
    db_session.add(
        OrchestrationExecution(
            id="execution-new",
            org_id="tenant-a",
            flow_id="flow-a",
            node_id="node-a",
            cycle=2,
            phase="deployment_pending",
            status="blocked",
            claim_id="claim-new",
            claim_generation=1,
        )
    )
    await db_session.flush()
    db_session.add(
        OrchestrationAction(
            org_id="tenant-a",
            execution_id="execution-new",
            operation_key="new-workflow-failed",
            kind="deployment_workflow",
            status="failed",
            created_at=OBSERVED + timedelta(days=1),
            observed_at=OBSERVED + timedelta(days=1, minutes=1),
            detail={"desired_revision": "c" * 40, "log": "canary-secret-123"},
        )
    )
    await db_session.commit()
    mixed = await read_installation_status(db_session, scoped, now=OBSERVED + timedelta(days=1))
    newest_failure = await read_installation_failure(db_session, scoped)
    assert mixed["components"][0]["observed_revision"] == REVISION
    assert mixed["components"][0]["health"] == "stale"
    assert mixed["desired_release"]["status"] == mixed["last_verified_release"]["status"] == "unavailable"
    assert newest_failure["failure"]["stage"] == "workflow_dispatch"
    assert newest_failure["failure"]["observed_at"] == (OBSERVED + timedelta(days=1, minutes=1)).isoformat()
    assert "canary-secret-123" not in str(newest_failure)

    new_binding = await db_session.get(OrchestrationPullRequestBinding, "binding-new")
    new_binding.installation_id = 5678
    new_binding.revision += 1
    await db_session.commit()
    moved_scope = InstallationReader(tenant_id="tenant-a", installation_id=5678)
    assert (await read_installation_failure(db_session, moved_scope))["status"] == "unavailable"
    new_binding.installation_id = 1234
    new_binding.revision = 1
    old_binding = await db_session.get(OrchestrationPullRequestBinding, "binding-a")
    old_binding.installation_id = 5678
    old_binding.revision += 1
    await db_session.commit()
    assert (await read_installation_status(db_session, moved_scope))["status"] == "unavailable"
    old_binding.installation_id = 1234
    old_binding.revision = 1
    await db_session.commit()

    db_session.add(
        OrchestrationPullRequestBinding(
            id="binding-b",
            org_id="tenant-a",
            flow_id="flow-a",
            node_id="node-a",
            attempt=1,
            provider_repository_id=3,
            provider_pr_node_id="PR-b",
            repo="example/project",
            pr_number=3,
            installation_id=5678,
            head_sha=REVIEWED_REVISION,
            registered_by="other",
            registered_by_kind="human",
        )
    )
    await db_session.commit()
    for installation_id in (1234, 5678):
        ambiguous = await read_installation_status(db_session, InstallationReader(tenant_id="tenant-a", installation_id=installation_id))
        assert ambiguous["status"] == "unavailable"
    assert (await read_installation_failure(db_session, InstallationReader(tenant_id="tenant-a", installation_id=5678)))["status"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("revise_prior", [False, True])
async def test_later_installation_binding_cannot_inherit_an_earlier_attempt_receipt(db_session, runtime_observation, revise_prior):
    if revise_prior:
        old_binding = await db_session.get(OrchestrationPullRequestBinding, "binding-a")
        old_binding.head_sha = "c" * 40
        old_binding.revision += 1
    db_session.add(
        OrchestrationPullRequestBinding(
            id="binding-later",
            org_id="tenant-a",
            flow_id="flow-a",
            node_id="node-a",
            attempt=2,
            provider_repository_id=2,
            provider_pr_node_id="PR-later",
            repo="example/project",
            pr_number=2,
            installation_id=5678,
            head_sha=REVIEWED_REVISION,
            registered_by="other-installer",
            registered_by_kind="human",
        )
    )
    await db_session.commit()

    result = await read_installation_status(
        db_session, InstallationReader(tenant_id="tenant-a", installation_id=5678), now=OBSERVED + timedelta(minutes=1)
    )

    assert result["status"] == "unavailable"
    assert "components" not in result

    original = await read_installation_status(db_session, InstallationReader(tenant_id="tenant-a", installation_id=1234))
    assert original["status"] == ("unavailable" if revise_prior else "partial")


@pytest.mark.asyncio
async def test_receipt_cycle_must_match_its_execution(db_session, runtime_observation):
    execution = await db_session.get(OrchestrationExecution, "execution-a")
    binding = await db_session.get(OrchestrationPullRequestBinding, "binding-a")
    execution.cycle = binding.attempt = 2
    await db_session.commit()

    result = await read_installation_status(db_session, InstallationReader(tenant_id="tenant-a", installation_id=1234))

    assert result["status"] == "unavailable"
    assert "components" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("org_id", "tenant-b"),
        ("execution_id", "execution-b"),
        ("flow_id", "flow-b"),
        ("node_id", "node-b"),
        ("cycle", 2),
        ("accepted_plan_version", 2),
        ("claim_id", "claim-b"),
        ("claim_generation", 2),
        ("repo", "example/other"),
        ("operation_key", "merge-b"),
        ("merge_sha", "f" * 40),
        ("reviewed_head_sha", "f" * 40),
        ("pr_number", 2),
        ("provider_repository_id", 2),
        ("provider_pr_node_id", "PR-b"),
    ],
)
async def test_merge_evidence_must_match_runtime_receipt_and_binding(db_session, runtime_observation, field, value):
    action = await db_session.get(OrchestrationAction, "merge-action-a")
    evidence = merge_receipt().model_dump(mode="json")
    evidence[field] = value
    action.detail = {"merge_receipt": evidence}
    await db_session.commit()

    result = await read_installation_status(db_session, InstallationReader(tenant_id="tenant-a", installation_id=1234))

    assert result["status"] == "unavailable"
    assert "components" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_key", "unrelated-merge"),
        ("kind", "deployment_workflow"),
        ("status", "failed"),
        ("status", "unknown"),
        ("detail", None),
        ("detail", ["internal-canary-secret-123"]),
        ("detail", {"merge_receipt": {"secret": "internal-canary-secret-123"}}),
    ],
)
async def test_missing_or_unverified_merge_evidence_is_unavailable(db_session, runtime_observation, field, value):
    action = await db_session.get(OrchestrationAction, "merge-action-a")
    setattr(action, field, value)
    await db_session.commit()

    result = await read_installation_status(db_session, InstallationReader(tenant_id="tenant-a", installation_id=1234))

    assert result["status"] == "unavailable"
    assert "components" not in result
    assert "canary-secret-123" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_id", ["tenant-a", "tenant-b"])
async def test_merge_action_cannot_be_borrowed_from_another_execution(db_session, runtime_observation, tenant_id):
    if tenant_id != "tenant-a":
        db_session.add(Organization(id=tenant_id, name="Other tenant"))
        await db_session.flush()
    db_session.add(OrchestrationFlow(id="flow-other", org_id=tenant_id, slug="other", title="Other"))
    await db_session.flush()
    db_session.add(
        OrchestrationNode(
            id="node-other",
            org_id=tenant_id,
            flow_id="flow-other",
            epic_ref="epic",
            wave_ref="wave",
            node_ref="other",
            kind="story",
            title="Other deployment",
        )
    )
    await db_session.flush()
    db_session.add(
        OrchestrationExecution(
            id="execution-other",
            org_id=tenant_id,
            flow_id="flow-other",
            node_id="node-other",
            cycle=1,
            phase="concluded",
            status="completed",
            claim_id="claim-other",
            claim_generation=1,
        )
    )
    await db_session.flush()
    action = await db_session.get(OrchestrationAction, "merge-action-a")
    action.org_id = tenant_id
    action.execution_id = "execution-other"
    await db_session.commit()

    result = await read_installation_status(db_session, InstallationReader(tenant_id="tenant-a", installation_id=1234))

    assert result["status"] == "unavailable"
    assert "components" not in result


@pytest.mark.asyncio
async def test_missing_legacy_receipt_explicitly_unknown(db_session):
    result = await read_installation_status(db_session, InstallationReader(tenant_id="tenant-a", installation_id=1234))
    assert result["status"] == "unavailable"
    assert result["capabilities"]["installed_record"]["reason"] == "installed_record_provider_unavailable"
    assert result["last_verified_release"]["status"] == "unavailable"
    assert "version" not in result
