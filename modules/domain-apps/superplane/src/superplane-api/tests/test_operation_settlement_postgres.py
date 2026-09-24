"""Actual receipt acceptance, restart and atomic budget effects on PostgreSQL."""

import asyncio
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from harness_jobs import (
    REQUIRED_PERMISSION,
    OperationRequest,
    OperationStore,
    ResolvedPrincipal,
)
from harness_jobs.admission import BudgetDenied, BudgetUnavailable, admit_operation
from harness_jobs.approval import (
    APPROVAL_PERMISSION,
    ApprovalBinding,
    ApprovalRecord,
    ApprovalResult,
    ApproverStatus,
)
from harness_jobs.schema import apply
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.operation_approval import OperationSettlementReceipt
from tests.test_operation_budget_ledger_postgres import (
    envelope,
    installation_postgres_url as installation_postgres_url,
    ledger as ledger,
    postgres_available,
    row,
)

pytestmark = [] if os.environ.get("CI") else postgres_available


@pytest.fixture
async def settlement(ledger, installation_postgres_url):  # noqa: F811
    budget, connections, restart = ledger
    async with connections.connect() as connection:
        await apply(connection)
        schema = await connection.fetchval("SELECT current_schema()")
    engine = create_async_engine(
        installation_postgres_url,
        connect_args={"server_settings": {"search_path": schema}},
    )
    async with engine.begin() as connection:
        await connection.run_sync(OperationSettlementReceipt.__table__.create)
    await engine.dispose()
    actor = ResolvedPrincipal(
        org_id=str(uuid.uuid4()),
        workspace_id=str(uuid.uuid4()),
        subject="requester",
        permissions=frozenset({REQUIRED_PERMISSION}),
    )
    request = OperationRequest(
        action="provision",
        idempotency_key="request",
        parameters={
            "allocation_id": "original-allocation",
            "lifecycle_phase": "apply-infrastructure",
            "plan_revision": "b" * 64,
        },
    )
    now = datetime.now(UTC)
    approval = ApprovalRecord(
        approval_id=str(uuid.uuid4()),
        binding=ApprovalBinding.for_request(actor, request),
        envelope=envelope(),
        result=ApprovalResult.ALLOWED_ONCE,
        approvers=frozenset({"approver"}),
        decided_by="approver",
        decided_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
    )
    async with connections.connect() as connection:
        admitted = await admit_operation(
            connection,
            OperationStore(),
            budget,
            principal=actor,
            request=request,
            approval=approval,
            requested_envelope=envelope(),
            approver_statuses={
                "approver": ApproverStatus(
                    subject="approver",
                    is_member=True,
                    permissions=frozenset({APPROVAL_PERMISSION}),
                )
            },
            now=now,
        )
        identity = {
            "operation_id": admitted.operation.record.operation_id,
            "job_id": admitted.operation.record.job_id,
            "attempt_id": admitted.operation.record.attempt_id,
            "org_id": actor.org_id,
            "workspace_id": actor.workspace_id,
        }
        await connection.execute("UPDATE harness_operations SET state='failed'")
        await connection.execute(
            "INSERT INTO harness_operation_leases(operation_id,org_id,workspace_id,fence_token,"
            "closed_at,closed_holder,closed_attempt_id) VALUES($1,$2,$3,3,now(),'recovery#1','recovery-attempt')",
            identity["operation_id"],
            identity["org_id"],
            identity["workspace_id"],
        )

    async def issue(*, _identity=None, **overrides):
        receipt_identity = _identity or identity
        accounting = {
            "version": 1,
            **receipt_identity,
            "claim": {
                "holder": "recovery#1",
                "attempt_id": "recovery-attempt",
                "fence_token": 3,
            },
            "action": "failed",
            "allocation_id": "original-allocation",
            "budget": "release",
            "call_dispositions": {"failed-call": "release"},
            "resource_dispositions": {"original-volume": "release"},
            "inventory_complete": True,
            "release_permitted": True,
            "may_mark_released": True,
            "exposure": "none",
            "unresolved_resources": [],
            **overrides,
        }
        encoded = json.dumps(accounting, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        receipt = {
            "receipt_id": "recovery:" + digest,
            "payload_digest": digest,
            **receipt_identity,
            "accounting": accounting,
        }
        async with connections.connect() as connection:
            await connection.execute(
                "INSERT INTO harness_recovery_settlements(receipt_id,operation_id,org_id,workspace_id,job_id,attempt_id,"
                "claim_holder,claim_attempt_id,claim_fence_token,payload_digest,accounting) "
                "VALUES($1,$2,$3,$4,$5,$6,'recovery#1','recovery-attempt',3,$7,$8::jsonb)",
                receipt["receipt_id"],
                receipt_identity["operation_id"],
                receipt_identity["org_id"],
                receipt_identity["workspace_id"],
                receipt_identity["job_id"],
                receipt_identity["attempt_id"],
                digest,
                encoded,
            )
        return receipt

    return budget, connections, restart, identity, issue


async def test_lost_ack_restarts_and_concurrent_redelivery_have_one_effect(settlement):
    budget, connections, restart, identity, issue = settlement
    receipt = await issue()
    assert await budget.deliver_settlement(**receipt) == receipt["receipt_id"]
    budget, connections = await restart()
    results = await asyncio.gather(
        *(budget.deliver_settlement(**receipt) for _ in range(8))
    )
    assert results == [receipt["receipt_id"]] * 8
    assert (await row(connections, identity["job_id"], identity["attempt_id"]))[
        "state"
    ] == "released"
    async with connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_settlement_receipts"
            )
            == 1
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"release_permitted": False},
        {"inventory_complete": False},
        {"may_mark_released": False},
        {"exposure": "active"},
        {"unresolved_resources": ["volume"]},
        {"resource_dispositions": {"volume": "retain"}},
        {"budget": "settle"},
    ],
)
async def test_call_release_never_overrides_resource_exposure(settlement, overrides):
    budget, connections, _, identity, issue = settlement
    await budget.deliver_settlement(**await issue(**overrides))
    assert (await row(connections, identity["job_id"], identity["attempt_id"]))[
        "state"
    ] == "retained"


@pytest.mark.parametrize(
    "column,value",
    [
        ("closed_holder", "different#1"),
        ("closed_attempt_id", "other"),
        ("fence_token", 4),
    ],
)
async def test_receipt_cannot_settle_another_closed_claim(settlement, column, value):
    budget, connections, _, identity, issue = settlement
    receipt = await issue()
    async with connections.connect() as connection:
        await connection.execute(
            f"UPDATE harness_operation_leases SET {column}=$1", value
        )
    with pytest.raises(BudgetDenied):
        await budget.deliver_settlement(**receipt)
    assert (await row(connections, identity["job_id"], identity["attempt_id"]))[
        "state"
    ] == "confirmed"


async def test_recomputed_forgery_does_not_match_shared_receipt(settlement):
    budget, _, _, _, issue = settlement
    receipt = await issue(release_permitted=False)
    receipt["accounting"]["release_permitted"] = True
    receipt["payload_digest"] = hashlib.sha256(
        json.dumps(
            receipt["accounting"], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    receipt["receipt_id"] = "recovery:" + receipt["payload_digest"]
    with pytest.raises(BudgetDenied):
        await budget.deliver_settlement(**receipt)


async def test_receiver_insert_failure_rolls_back_budget_update(settlement):
    budget, connections, _, identity, issue = settlement
    receipt = await issue()
    async with connections.connect() as connection:
        await connection.execute(
            "ALTER TABLE operation_settlement_receipts ADD CHECK (disposition='never')"
        )
    with pytest.raises(BudgetUnavailable):
        await budget.deliver_settlement(**receipt)
    assert (await row(connections, identity["job_id"], identity["attempt_id"]))[
        "state"
    ] == "confirmed"
    async with connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_settlement_receipts"
            )
            == 0
        )


@pytest.fixture
async def retirement_settlement(settlement, installation_postgres_url):  # noqa: F811
    from harness_jobs.identity import decode_payload
    from workspace_provisioning.artifacts import canonical, digest
    from app.models.lifecycle import WorkspaceLifecycleArtifact

    budget, connections, restart, source_identity, issue = settlement
    async with connections.connect() as connection:
        schema = await connection.fetchval("SELECT current_schema()")
        await connection.execute("UPDATE harness_operations SET state='succeeded'")
        source = await connection.fetchrow("SELECT * FROM harness_operations")
    engine = create_async_engine(
        installation_postgres_url,
        connect_args={"server_settings": {"search_path": schema}},
    )
    async with engine.begin() as connection:
        await connection.run_sync(WorkspaceLifecycleArtifact.__table__.create)
    await engine.dispose()
    source_request = decode_payload(source["request_payload"])
    values = {
        "org_id": source["org_id"],
        "workspace_id": source["workspace_id"],
        "source_operation_id": source["operation_id"],
        "source_job_id": source["job_id"],
        "source_attempt_id": source["attempt_id"],
        "source_payload_digest": source["plan_digest"],
        "source_request_payload": source["request_payload"],
        "producer_holder": "apply-worker#1",
        "producer_attempt_id": "apply-worker-attempt",
        "producer_fence_token": 1,
        "request_revision": source_request.parameters["plan_revision"],
        "account_id": "000000000002",
        "target_json": canonical({"account_id": "000000000002"}),
        "parameters_json": canonical(dict(source_request.parameters)),
        "artifact_metadata_json": canonical(
            {
                "next_phase": "bootstrap-workspace",
                "allocation_source_operation_id": source["operation_id"],
            }
        ),
    }
    artifact_id = digest(values)
    async with connections.connect() as connection:
        await connection.execute(
            "INSERT INTO workspace_lifecycle_artifacts (artifact_id,"
            + ",".join(values)
            + ") VALUES ($1,"
            + ",".join("$" + str(i) for i in range(2, len(values) + 2))
            + ")",
            artifact_id,
            *values.values(),
        )
    actor = ResolvedPrincipal(
        source["org_id"],
        source["workspace_id"],
        "requester",
        frozenset({REQUIRED_PERMISSION}),
    )
    parameters = {
        "allocation_source_operation_id": source["operation_id"],
        "lifecycle_artifact_id": artifact_id,
        "allocation_id": "original-allocation",
    }
    request = OperationRequest("teardown", "retirement", parameters)
    now = datetime.now(UTC)
    approval = ApprovalRecord(
        approval_id=str(uuid.uuid4()),
        binding=ApprovalBinding.for_request(actor, request),
        envelope=envelope(units=0, micros=0),
        result=ApprovalResult.ALLOWED_ONCE,
        approvers=frozenset({"approver"}),
        decided_by="approver",
        decided_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
    )
    async with connections.connect() as connection:
        admitted = await admit_operation(
            connection,
            OperationStore(),
            budget,
            principal=actor,
            request=request,
            approval=approval,
            requested_envelope=approval.envelope,
            approver_statuses={
                "approver": ApproverStatus(
                    "approver", True, frozenset({APPROVAL_PERMISSION})
                )
            },
            now=now,
        )
        identity = {
            "operation_id": admitted.operation.record.operation_id,
            "job_id": admitted.operation.record.job_id,
            "attempt_id": admitted.operation.record.attempt_id,
            "org_id": actor.org_id,
            "workspace_id": actor.workspace_id,
        }
        await connection.execute(
            "UPDATE harness_operations SET state='succeeded' WHERE operation_id=$1",
            identity["operation_id"],
        )
        await connection.execute(
            "INSERT INTO harness_operation_leases(operation_id,org_id,workspace_id,fence_token,closed_at,closed_holder,closed_attempt_id) VALUES($1,$2,$3,3,now(),'recovery#1','recovery-attempt')",
            identity["operation_id"],
            actor.org_id,
            actor.workspace_id,
        )

    async def receipt(**overrides):
        return await issue(
            _identity=identity, action="succeeded", **(parameters | overrides)
        )

    return budget, connections, restart, source_identity, identity, receipt


async def test_retirement_releases_original_apply_and_control_reservations_once(
    retirement_settlement,
):
    budget, connections, restart, source, retirement, issue = retirement_settlement
    receipt = await issue()
    await budget.deliver_settlement(**receipt)
    budget, connections = await restart()
    await asyncio.gather(*(budget.deliver_settlement(**receipt) for _ in range(5)))
    for identity in (source, retirement):
        assert (await row(connections, identity["job_id"], identity["attempt_id"]))[
            "state"
        ] == "released"
    async with connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_settlement_receipts"
            )
            == 1
        )


async def test_unresolved_retirement_keeps_original_allocation_committed(
    retirement_settlement,
):
    budget, connections, _, source, retirement, issue = retirement_settlement
    await budget.deliver_settlement(
        **await issue(exposure="unknown", inventory_complete=False)
    )
    for identity in (source, retirement):
        assert (await row(connections, identity["job_id"], identity["attempt_id"]))[
            "state"
        ] == "retained"


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "artifact",
        "source_attempt",
        "source_workspace",
        "source_state",
        "receiver_insert",
    ],
)
async def test_retirement_linkage_substitution_and_receiver_failure_release_nothing(
    retirement_settlement, change
):
    budget, connections, _, source, retirement, issue = retirement_settlement
    overrides = {}
    if change == "source":
        overrides["allocation_source_operation_id"] = "other-operation"
    elif change == "artifact":
        overrides["lifecycle_artifact_id"] = "f" * 64
    else:
        async with connections.connect() as connection:
            if change == "source_attempt":
                await connection.execute(
                    "UPDATE harness_operations SET attempt_id='changed' WHERE operation_id=$1",
                    source["operation_id"],
                )
            elif change == "source_workspace":
                await connection.execute(
                    "UPDATE harness_operations SET workspace_id='other-workspace' WHERE operation_id=$1",
                    source["operation_id"],
                )
            elif change == "source_state":
                await connection.execute(
                    "UPDATE harness_operations SET state='unknown' WHERE operation_id=$1",
                    source["operation_id"],
                )
            else:
                await connection.execute(
                    "ALTER TABLE operation_settlement_receipts ADD CHECK (disposition='never')"
                )
    receipt = await issue(**overrides)
    with pytest.raises(
        BudgetUnavailable if change == "receiver_insert" else BudgetDenied
    ):
        await budget.deliver_settlement(**receipt)
    for identity in (source, retirement):
        assert (await row(connections, identity["job_id"], identity["attempt_id"]))[
            "state"
        ] == "confirmed"
