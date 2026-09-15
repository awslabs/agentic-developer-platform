"""Production ownership boundaries: competing producers and worker lifecycle."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.orchestration.models import ClaimState, OrchestrationWorkClaim
from src.orchestration.work_admission import admit, maintain_worker_claim, recover_exited_claims
from src.orchestration.work_claims import ClaimOwner, OwnerKind, WorkClaimError
from tests.orchestration import test_work_claims as fixtures

engine = fixtures.engine
session = fixtures.session
session_factory = fixtures.session_factory


async def start(session, *, run="run-1", owner="flow-1", kind=OwnerKind.ENGINE_FLOW, org="org-alpha"):
    return await admit(session, org_id=org, repository_id=1234, issue=5161, owner=ClaimOwner(kind, owner), invocation_id=run)


async def test_engine_and_webhook_cannot_admit_two_runs(session):
    receipt = await start(session)
    with pytest.raises(WorkClaimError, match="already owned"):
        await start(session, run="webhook", owner="human-event", kind=OwnerKind.DIRECT_DISPATCH)
    row = await session.get(OrchestrationWorkClaim, receipt["claim_id"])
    assert row.active_run_id == "run-1"


async def test_lost_producer_ack_reuses_same_pending_receipt(session):
    first = await start(session)
    retry = await start(session)
    assert retry["claim_id"] == first["claim_id"]
    assert retry["generation"] == first["generation"]
    assert retry["disposition"] == "duplicate"


async def test_sequential_personas_share_owner_and_advance_generation(session, monkeypatch):
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
    first = await start(session, run="developer")
    await maintain_worker_claim(session, org_id="org-alpha", invocation_id="developer", terminal=True)
    second = await start(session, run="reviewer")
    await maintain_worker_claim(session, org_id="org-alpha", invocation_id="reviewer", terminal=True)
    third = await start(session, run="repair")
    assert first["claim_id"] == second["claim_id"] == third["claim_id"]
    assert [first["generation"], second["generation"], third["generation"]] == [1, 2, 3]
    with pytest.raises(WorkClaimError):
        await maintain_worker_claim(session, org_id="org-alpha", invocation_id="developer", terminal=True)
    row = await session.get(OrchestrationWorkClaim, first["claim_id"])
    assert row.active_run_id == "repair"


async def test_worker_missing_claim_is_refused_before_work(session, monkeypatch):
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
    with pytest.raises(WorkClaimError, match="no admitted"):
        await maintain_worker_claim(session, org_id="org-alpha", invocation_id="unadmitted")


@pytest.mark.parametrize("exited", [False, True])
async def test_crash_recovery_requires_positive_workload_exit(session, exited):
    receipt = await start(session)
    store = SimpleNamespace(
        _read=Mock(
            return_value={
                "tenant_id": {"S": "org-alpha"},
                "status": {"S": "active"},
                "pod_name": {"S": "worker-1"},
                "workload_binding": {"S": "uid-1"},
            }
        )
    )
    workloads = SimpleNamespace(has_exited=Mock(return_value=exited))
    assert await recover_exited_claims(session, store=store, workloads=workloads) == int(exited)
    row = await session.get(OrchestrationWorkClaim, receipt["claim_id"])
    assert row.state == (ClaimState.RELEASED.value if exited else ClaimState.HELD.value)
    workloads.has_exited.assert_called_once_with(name="worker-1", uid="uid-1")


async def test_other_tenants_execution_cannot_release_claim(session):
    receipt = await start(session)
    store = SimpleNamespace(_read=Mock(return_value={"tenant_id": {"S": "other"}, "status": {"S": "completed"}}))
    assert await recover_exited_claims(session, store=store, workloads=None) == 0
    assert (await session.get(OrchestrationWorkClaim, receipt["claim_id"])).state == ClaimState.HELD.value
