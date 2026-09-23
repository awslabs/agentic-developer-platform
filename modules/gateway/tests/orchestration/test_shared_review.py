"""Authenticated shared reviewer bytes become the existing R1 ledger evidence."""

import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.models import OrchestrationAction, OrchestrationWorkClaim
from src.orchestration.run_reports import OrchestrationRunReport, RunReportError
from src.orchestration.shared_review import record_shared_review
from tests.orchestration.test_review_cycle import state, tick
from tests.orchestration.test_review_evidence import APPROVE
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


def document_for(ctx, envelope):
    doc = json.loads(json.dumps(copy.deepcopy(APPROVE)).replace(APPROVE["subject"]["reviewed_head_sha"], ctx.head))
    doc["scope"].update(org_id=ctx.node.org_id, flow_id=ctx.flow.id, node_id=ctx.node.id, cycle=ctx.identity.cycle, execution_id=ctx.execution.id)
    doc["authority"].update(
        accepted_plan_version=ctx.identity.accepted_plan_version, claim_id=ctx.identity.claim_id, claim_generation=ctx.identity.claim_generation
    )
    doc["repository"].update(repo=ctx.binding.repo, provider_repository_id=ctx.binding.provider_repository_id)
    doc["subject"].update(pr_number=ctx.binding.pr_number, provider_pr_node_id=ctx.binding.provider_pr_node_id)
    doc["lineage"].update(author_run_id=ctx.root, reviewer_run_id=envelope["message_id"])
    return doc


@pytest.fixture
async def upload(shared, monkeypatch):  # noqa: F811
    assert (await tick(shared)).effects_succeeded == 1
    envelope = shared.calls[-1]
    monkeypatch.setenv("AGENT_RUN_LOGS_BUCKET", "test-review")
    monkeypatch.setattr("src.orchestration.pr_identity.resolve_head_check_runs", AsyncMock(return_value=frozenset({"check-run:105036077448"})))
    stored = {}

    def put_object(**kwargs):
        stored[kwargs["Key"]] = kwargs["Body"]

    return SimpleNamespace(
        ctx=shared, envelope=envelope, document=document_for(shared, envelope), storage=SimpleNamespace(put_object=put_object), stored=stored
    )


async def record(upload, session):
    return await record_shared_review(
        session, credential=upload.envelope["run_report"]["credential"], content=json.dumps(upload.document, indent=2), storage=upload.storage
    )


async def test_shared_review_exact_bytes_acknowledged_and_replay_converges(upload):
    async with upload.ctx.factory() as db:
        receipt = await record(upload, db)
        assert receipt["recorded"], receipt
        await db.commit()
    async with upload.ctx.factory() as db:
        again = await record(upload, db)
        assert receipt == again
        row = await db.get(OrchestrationRunReport, upload.envelope["message_id"])
        assert row.review_receipt == receipt
        assert receipt["sha256"] == hashlib.sha256(json.dumps(upload.document, indent=2).encode()).hexdigest()
        assert len(list((await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == "review_evidence"))).all())) == 1
    await upload.ctx.finish(upload.envelope["message_id"])
    await tick(upload.ctx)
    assert (await state(upload.ctx))[0].phase == "merge_ready"


async def test_accepted_review_enters_merge_observation_before_worker_terminal(upload):
    async with upload.ctx.factory() as db:
        assert (await record(upload, db))["recorded"]
        await db.commit()
    await tick(upload.ctx)
    assert (await state(upload.ctx))[0].phase == "merge_ready"
    async with upload.ctx.factory() as db:
        assert (await db.get(OrchestrationRunReport, upload.envelope["message_id"])).terminal_receipt is None
    assert len(upload.ctx.calls) == 1


async def test_codex_review_repair_evidence_advances_without_developer_handoff(upload):
    assert upload.envelope["persona"] == "agent-codex-reviewer"
    assert upload.envelope["review_cycle_input"]["allow_story_repairs"] is True
    original_head = upload.ctx.head
    upload.ctx.head = "b" * 40
    upload.document = json.loads(json.dumps(upload.document).replace(original_head, upload.ctx.head))
    async with upload.ctx.factory() as db:
        receipt = await record(upload, db)
        assert receipt["recorded"], receipt
        await db.commit()
    await upload.ctx.finish(upload.envelope["message_id"])
    await tick(upload.ctx)
    assert (await state(upload.ctx))[0].phase == "merge_ready"
    assert len(upload.ctx.calls) == 1


@pytest.mark.parametrize("mutation", ["head", "author", "reviewer", "checks"])
async def test_shared_review_rejects_stale_or_substituted_evidence(upload, mutation):
    if mutation == "head":
        upload.document["subject"]["reviewed_head_sha"] = "b" * 40
    elif mutation == "checks":
        upload.document["evidence_refs"][0]["ref"] = "check-run:unverified"
    else:
        upload.document["lineage"][f"{mutation}_run_id"] = "another-run"
    async with upload.ctx.factory() as db:
        receipt = await record(upload, db)
        assert not receipt["recorded"] and receipt["refusal"], receipt
        assert (await db.get(OrchestrationRunReport, upload.envelope["message_id"])).review_receipt is None


async def test_old_reviewer_cannot_report_after_claim_moves(upload):
    async with upload.ctx.factory() as db:
        claim = await db.get(OrchestrationWorkClaim, upload.ctx.identity.claim_id)
        claim.active_run_id = "replacement-run"
        await db.commit()
    async with upload.ctx.factory() as db:
        with pytest.raises(RunReportError, match="superseded"):
            await record(upload, db)
    assert not upload.stored


async def test_same_reviewer_repairs_keeps_author_and_advances_directly_to_merge(upload):
    from src.orchestration.review_cycle_dispatch import current_author_run

    repair = upload.ctx.calls[-1]
    assert repair["review_cycle_input"]["action"] == "review"
    assert repair["review_cycle_input"]["reviewer_owned_delivery"] is True
    assert repair["review_expect"]["author_run_id"] == upload.ctx.root
    upload.envelope = repair
    upload.ctx.head = "b" * 40
    upload.document = document_for(upload.ctx, repair)
    async with upload.ctx.factory() as db:
        assert await current_author_run(db, node=upload.ctx.node, default=upload.ctx.binding.run_id) == upload.ctx.root
        receipt = await record(upload, db)
        assert receipt["recorded"], receipt
        await db.commit()
    assert repair["pr_binding_required"] is False
    from src.orchestration.models import OrchestrationPullRequestBinding
    from src.orchestration.run_reports import record_terminal

    async with upload.ctx.factory() as db:
        row = await db.get(OrchestrationRunReport, repair["message_id"])
        record_terminal(row, "complete")
        await db.commit()
    await tick(upload.ctx)
    assert (await state(upload.ctx))[0].phase == "merge_ready"
    async with upload.ctx.factory() as db:
        binding = await db.get(OrchestrationPullRequestBinding, upload.ctx.binding.id)
        assert binding.role == "implementation" and binding.run_id == upload.ctx.root
    assert len(upload.ctx.calls) == 1  # same reviewer inspects and repairs


async def test_owned_repair_cannot_substitute_author_or_claim_self_review(upload):
    repair = upload.ctx.calls[-1]
    upload.envelope = repair
    upload.document = document_for(upload.ctx, repair)
    upload.document["lineage"]["author_run_id"] = repair["message_id"]
    async with upload.ctx.factory() as db:
        receipt = await record(upload, db)
        assert not receipt["recorded"]
