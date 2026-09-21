"""Current SQL receipts distinguish finished work from unresolved worker activity."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationNode, OrchestrationWorkClaim
from src.orchestration.run_reports import OrchestrationRunReport, prepare_run_report
from src.orchestration.shared_policy import _active_count
from tests.orchestration import test_review_cycle as protocol
from tests.orchestration.test_shared_attempt_boundaries import envelope_for
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


async def assigned_report(ctx, db):
    envelope = await envelope_for(ctx, db, ctx.node)
    envelope["handoff_expect"] = {
        "execution_id": ctx.execution.id,
        "accepted_plan_version": 1,
        "claim_id": ctx.identity.claim_id,
        "claim_generation": ctx.identity.claim_generation,
    }
    return await prepare_run_report(db, envelope)


async def count(ctx, db, **kwargs):
    await db.flush()
    return await _active_count(db, org_id=ctx.node.org_id, flow_id=ctx.flow.id, **kwargs)


async def test_completed_report_releases_slot_but_new_unreported_attempt_occupies_one(shared):  # noqa: F811
    async with shared.factory() as db:
        report = await assigned_report(shared, db)
        assert await count(shared, db) == 1
        report.terminal_receipt = {"outcome": "complete"}
        assert await count(shared, db) == 0
        node = await db.get(OrchestrationNode, shared.node.id)
        node.attempts += 1
        assert await count(shared, db) == 1


async def test_json_null_terminal_receipt_remains_an_active_worker(shared):  # noqa: F811
    async with shared.factory() as db:
        report = await assigned_report(shared, db)
        report.terminal_receipt = {"outcome": "complete"}
        await db.flush()
        report.terminal_receipt = None
        node = await db.get(OrchestrationNode, shared.node.id)
        node.state = "awaiting_merge"
        await db.flush()
        # JSON's default None encoding is JSON null, distinct from SQL NULL.
        assert (
            await db.scalar(select(OrchestrationRunReport.terminal_receipt.is_(None)).where(OrchestrationRunReport.run_id == report.run_id)) is False
        )
        assert await count(shared, db) == 1


@pytest.mark.parametrize("change", [None, "run_id", "attempt", "evidence_origin"])
async def test_only_exact_reconciled_initial_assignment_releases_missing_terminal_slot(shared, change):  # noqa: F811
    async with shared.factory() as db:
        report = await assigned_report(shared, db)
        initial = {"run_id": report.run_id, "attempt": report.attempt, "evidence_origin": "owner_reconciled_legacy_delivery"}
        if change:
            initial[change] = 99 if change == "attempt" else "different"
        assert await count(shared, db, initial_runs={report.node_id: initial}) == (1 if change else 0)


@pytest.mark.parametrize(
    "case,expected",
    [
        ("evidenced_release", 0),
        ("advanced_generation", 0),
        ("release_without_time", 1),
        ("release_without_reason", 1),
        ("release_unknown_reason", 1),
        ("another_same_generation_run", 1),
        ("expired_credential", 1),
        ("expired_lease", 1),
        ("terminal_node", 1),
        ("new_node_attempt", 2),
        ("missing_claim", 1),
        ("wrong_repository", 1),
        ("wrong_tenant", 1),
        ("malformed_generation", 1),
        ("missing_fence", 1),
    ],
)
async def test_only_evidenced_claim_reconciliation_releases_orphaned_report_slot(shared, case, expected):  # noqa: F811
    async with shared.factory() as db:
        report = await assigned_report(shared, db)
        claim = await db.get(OrchestrationWorkClaim, shared.identity.claim_id)
        node = await db.get(OrchestrationNode, shared.node.id)
        if case.startswith("release_") or case == "evidenced_release":
            claim.state, claim.active_run_id = "released", None
            claim.released_at, claim.release_reason = datetime.now(UTC), "completed"
            if case == "release_without_time":
                claim.released_at = None
            elif case == "release_without_reason":
                claim.release_reason = None
            elif case == "release_unknown_reason":
                claim.release_reason = "unknown"
        elif case in {"advanced_generation", "wrong_repository", "wrong_tenant"}:
            claim.generation += 1
            if case == "wrong_repository":
                claim.provider_repository_id += 1
            elif case == "wrong_tenant":
                claim.org_id = "other-tenant"
        elif case == "another_same_generation_run":
            claim.active_run_id = "replacement-without-completion-proof"
        elif case == "expired_credential":
            report.expires_at = datetime.now(UTC) - timedelta(days=1)
        elif case == "expired_lease":
            claim.lease_expires_at = datetime.now(UTC) - timedelta(days=1)
        elif case == "terminal_node":
            node.state = "passed"
        elif case == "new_node_attempt":
            node.attempts += 1
        else:
            metadata = dict(report.dispatch_metadata)
            fence = dict(metadata["handoff_expect"])
            if case == "missing_claim":
                fence["claim_id"] = "not-stored"
            elif case == "malformed_generation":
                fence["claim_generation"] = str(claim.generation)
            metadata["handoff_expect"] = fence
            if case == "missing_fence":
                del metadata["handoff_expect"]
            report.dispatch_metadata = metadata
        assert await count(shared, db) == expected


async def test_shared_develop_review_repair_and_merge_ready_with_real_slot_accounting(shared, monkeypatch):  # noqa: F811
    monkeypatch.setattr("src.orchestration.shared_policy._active_count", _active_count)
    async with shared.factory() as db:
        # This is a newly reported developer, not an owner-reconciled legacy run.
        plan = await db.get(OrchestrationAcceptedPlan, shared.plan.id)
        marker = {**plan.plan_document["execution_continuation"], "initial_runs": {}}
        plan.plan_document = {**plan.plan_document, "execution_continuation": marker}
        report = await assigned_report(shared, db)
        report.binding_receipt = {"binding_id": shared.binding.id, "revision": 1}
        report.terminal_receipt = {"outcome": "complete"}
        await db.commit()
    await protocol.test_develop_review_repair_fresh_review_merge_ready(shared)
    async with shared.factory() as db:
        assert await count(shared, db) == 0
