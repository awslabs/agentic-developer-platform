"""Real PostgreSQL, protected grants, R1 artifact reload and expected-head HTTP effects."""

from __future__ import annotations

import hashlib
import io
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from src.agentauth.artifact_keys import artifact_prefix
from src.orchestration.execution_runner import RunnerConfig, run_execution_runner
from src.orchestration.execution_state import PhaseAdvance
from src.orchestration.execution_store import advance_execution, load_execution
from src.orchestration.merge_controller import MERGE_KIND, PHASES, MergeController, MergeReceipt, MergeServices, bounded_receipt_summary
from src.orchestration.merge_provider import MergeProvider
from src.orchestration.models import OrchestrationAction, OrchestrationNode, OrchestrationPullRequestBinding
from src.orchestration.review_evidence import record_review_evidence, validate_review_result
from tests.orchestration.test_merge_evidence import provider_data
from tests.orchestration.test_review_cycle import cycle, pg_server, pg_url, state, store  # noqa: F401
from tests.orchestration.test_review_cycle import tick as review_tick
from tests.orchestration.test_review_evidence import APPROVE, _all_refs


@pytest.fixture
async def merge(cycle, monkeypatch):  # noqa: F811
    ctx = cycle
    assert (await review_tick(ctx)).effects_succeeded == 1
    execution, claim, node, actions = await state(ctx)
    reviewer = claim.active_run_id
    await ctx.finish(reviewer)
    doc = json.loads(json.dumps(deepcopy(APPROVE)).replace(APPROVE["subject"]["reviewed_head_sha"], ctx.head))
    doc["scope"].update(org_id=node.org_id, flow_id=node.flow_id, node_id=node.id, execution_id=execution.id, cycle=1)
    doc["authority"].update(accepted_plan_version=1, claim_id=ctx.identity.claim_id, claim_generation=5)
    doc["repository"].update(repo=ctx.binding.repo, provider_repository_id=123)
    doc["subject"].update(pr_number=77, provider_pr_node_id="PR_cycle", reviewed_head_sha=ctx.head)
    doc["lineage"].update(author_run_id=ctx.root, reviewer_run_id=reviewer)
    doc["observed_at"] = datetime.now(UTC).isoformat()
    payload = json.dumps(doc).encode()
    digest = hashlib.sha256(payload).hexdigest()
    prefix = artifact_prefix(SimpleNamespace(tenant_id=node.org_id, invocation_id=reviewer, current_attempt=1))
    key = f"{prefix}review-result/{digest}.json"
    reference = f"s3://review-test/{key}#sha256={digest}"
    objects = {key: payload}

    def get_object(**kwargs):
        assert kwargs["Bucket"] == "review-test"
        return {"Body": io.BytesIO(objects[kwargs["Key"]]), "ContentType": "application/json"}

    monkeypatch.setenv("AGENT_RUN_LOGS_BUCKET", "review-test")
    async with ctx.factory() as db:
        binding = await db.get(OrchestrationPullRequestBinding, ctx.binding.id)
        evidence = validate_review_result(
            doc,
            identity=ctx.identity,
            binding=binding,
            flow_id=node.flow_id,
            author_run_id=ctx.root,
            reviewer_run_id=reviewer,
            execution_id=execution.id,
            actual_head_sha=ctx.head,
            trusted_artifact_refs=_all_refs(doc),
        )
        recorded = await record_review_evidence(db, identity=ctx.identity, evidence=replace(evidence, artifact_ref=reference))
        assert recorded.kind.value == "applied", recorded
        await db.commit()
    await review_tick(ctx)
    assert (await state(ctx))[0].phase == "merge_ready"
    data = provider_data()
    data["pr"].update(number=77, node_id="PR_cycle", merged=False, merged_at=None)
    data["pr"]["head"].update(sha=ctx.head, ref="agent/issue-43", repo={"id": 123, "full_name": ctx.binding.repo})
    data["pr"]["base"].update(sha="b" * 40, ref="main", repo={"id": 123, "full_name": ctx.binding.repo})
    data["repository"].update(id=123, full_name=ctx.binding.repo)
    data["branch"]["commit"]["sha"] = "b" * 40
    data["checks"]["check_runs"][0]["head_sha"] = ctx.head
    data["reviews"][0]["commit_id"] = ctx.head
    data["graphql"]["data"]["repository"].update(databaseId=123)
    data["graphql"]["data"]["repository"]["pullRequest"].update(
        id="PR_cycle", headRefOid=ctx.head, baseRefOid="b" * 40, merged=False, mergeQueueEntry=None
    )
    calls, mutations = [], []
    ctx.remote, ctx.mutations, ctx.http_calls = data, mutations, calls
    ctx.timeout_after_merge = False
    ctx.unavailable = False
    ctx.conflict = False

    def merge_remote():
        data["pr"].update(merged=True, state="closed", merge_commit_sha="c" * 40, merged_at=datetime.now(UTC).isoformat())
        data["graphql"]["data"]["repository"]["pullRequest"].update(merged=True, mergeQueueEntry=None)

    ctx.merge_remote = merge_remote

    def respond(request):
        assert request.headers["Authorization"] == "Bearer scoped-test-token"
        calls.append((request.method, request.url.path))
        if ctx.unavailable:
            raise httpx.ReadTimeout("test outage")
        path = request.url.path
        if path.endswith("/merge"):
            mutations.append(json.loads(request.content))
            assert json.loads(request.content)["sha"] == ctx.head
            if ctx.conflict:
                return httpx.Response(409, json={"message": "merge conflict"})
            merge_remote()
            if ctx.timeout_after_merge:
                raise httpx.ReadTimeout("response lost after merge")
            return httpx.Response(200, json={"merged": True, "sha": "c" * 40})
        if path == "/graphql":
            body = json.loads(request.content)
            if body["query"].startswith("mutation"):
                mutations.append(body)
                assert body["variables"]["input"]["expectedHeadOid"] == ctx.head
                data["graphql"]["data"]["repository"]["pullRequest"]["mergeQueueEntry"] = {"id": "MQ_one"}
                return httpx.Response(200, json={"data": {"enqueuePullRequest": {"mergeQueueEntry": {"id": "MQ_one"}}}})
            return httpx.Response(200, json=data["graphql"])
        if path.endswith("/rules/branches/main"):
            key = "rules"
        elif path.endswith("/protection"):
            key = "protection"
        elif path.endswith("/branches/main"):
            key = "branch"
        elif path.endswith("/check-runs"):
            key = "checks"
        elif path.endswith("/statuses"):
            key = "statuses"
        elif path.endswith("/reviews"):
            key = "reviews"
        elif path.endswith("/pulls/77"):
            key = "pr"
        elif path == f"/repos/{ctx.binding.repo}":
            key = "repository"
        else:
            pytest.fail(f"Unexpected request {request.method} {path}")
        return httpx.Response(404 if data[key] is None else 200, json=data[key])

    credentials = AsyncMock(return_value=("app", "test-key"))
    mint = AsyncMock(return_value=("scoped-test-token", (datetime.now(UTC) + timedelta(hours=1)).isoformat()))
    monkeypatch.setattr("src.knowledge.github_app_service.resolve_tenant_app_credentials", credentials)
    monkeypatch.setattr("src.knowledge.github_app_service.mint_installation_token_with_expiry", mint)
    monkeypatch.setattr("src.orchestration.merge_provider.resolve_tenant_app_credentials", credentials)
    monkeypatch.setattr("src.orchestration.merge_provider.mint_installation_token_with_expiry", mint)
    monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", AsyncMock(return_value=42))
    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(respond)) as client:
        ctx.merge_services = MergeServices(
            ctx.factory, authority=ctx.service, provider=MergeProvider(client=client), storage=SimpleNamespace(get_object=get_object)
        )
        ctx.objects, ctx.artifact_key, ctx.mint = objects, key, mint
        yield ctx


async def tick(ctx, checkpoint=None):
    async with ctx.factory() as db:
        record = (await load_execution(db, identity=ctx.identity)).record
        await advance_execution(
            db,
            identity=ctx.identity,
            advance=PhaseAdvance(
                phase=record.phase,
                status=record.status,
                expected_revision=record.revision,
                next_check_at=datetime.now(UTC),
            ),
        )
        await db.commit()
    return await run_execution_runner(
        ctx.factory,
        handlers=dict.fromkeys(PHASES, MergeController(ctx.factory, ctx.merge_services)),
        config=RunnerConfig(enabled=True, max_attempts=8, io_timeout_seconds=10),
        notifier=AsyncMock(return_value="test-notice"),
        checkpoint=checkpoint,
    )


async def merge_actions(ctx):
    async with ctx.factory() as db:
        return list((await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == MERGE_KIND))).all())


async def test_engine_expected_head_merge_then_verified_code_completion(merge):
    ctx = merge
    first = await tick(ctx)
    assert first.effects_succeeded == 1, first
    assert (await state(ctx))[2].state == "running"
    assert len(ctx.mutations) == 1
    second = await tick(ctx)
    execution, claim, node, _ = await state(ctx)
    assert node.state == "passed", second
    assert execution.phase == "deployment_pending" and execution.status == "runnable"
    assert claim.state == "held" and claim.generation == 5
    receipt = MergeReceipt.model_validate((await merge_actions(ctx))[0].detail["merge_receipt"])
    assert receipt.merge_sha == "c" * 40 and not receipt.adopted
    assert receipt.reviewed_head_sha == ctx.head and receipt.review_ref.startswith("s3://review-test/")
    assert bounded_receipt_summary({"merge_receipt": receipt.model_dump(mode="json")})["merge_sha"] == receipt.merge_sha
    await tick(ctx)
    assert len(ctx.mutations) == 1
    assert any(call.kwargs["permissions"].get("contents") == "write" for call in ctx.mint.await_args_list)
    assert all(call.kwargs["repositories"] == ["repo"] for call in ctx.mint.await_args_list)


async def test_timeout_after_merge_reconciles_without_second_effect(merge):
    merge.timeout_after_merge = True
    first = await tick(merge)
    assert first.effects_uncertain == 1, first
    second = await tick(merge)
    assert (await state(merge))[2].state == "passed", second
    assert len(merge.mutations) == 1


async def test_queue_admission_waits_for_actual_merge(merge):
    merge.remote["rules"].append({"type": "merge_queue", "parameters": {}})
    first = await tick(merge)
    assert first.effects_succeeded == 1, first
    await tick(merge)
    assert (await state(merge))[2].state == "running"
    assert len(merge.mutations) == 1
    merge.merge_remote()
    await tick(merge)
    assert (await state(merge))[2].state == "passed"
    assert len(merge.mutations) == 1


async def test_manual_merge_without_historical_requirements_blocks(merge):
    merge.merge_remote()
    result = await tick(merge)
    assert result.blocked == 1
    execution, _, node, _ = await state(merge)
    assert node.state == "running" and "historical" in execution.block_detail
    assert merge.mutations == []


async def test_head_change_returns_to_review_without_merge(merge):
    merge.remote["pr"]["head"]["sha"] = "d" * 40
    merge.remote["graphql"]["data"]["repository"]["pullRequest"]["headRefOid"] = "d" * 40
    result = await tick(merge)
    assert (await state(merge))[0].phase == "awaiting_review", result
    assert merge.mutations == []


async def test_conflict_hands_off_to_real_repair_without_resetting_allowance(merge):
    merge.conflict = True
    await tick(merge)
    await tick(merge)
    before = (await state(merge))[0].attempts
    result = await review_tick(merge)
    assert result.effects_succeeded == 1, result
    assert merge.calls[-1]["persona"] == "developer"
    assert "merge conflict" in merge.calls[-1]["review_cycle_input"]["findings"][0]["summary"]
    execution, claim, node, _ = await state(merge)
    assert execution.attempts == before + 1 and claim.generation == 5 and node.attempts == 1


@pytest.mark.parametrize("gate", ["failed", "halted", "rejected", "awaiting_gate"])
async def test_outer_node_gates_do_not_merge(merge, gate):
    async with merge.factory() as db:
        node = await db.get(OrchestrationNode, merge.node.id)
        node.state = gate
        await db.commit()
    result = await tick(merge)
    assert result.blocked == 1 and merge.mutations == []


async def test_revocation_after_intent_prevents_effect(merge):
    async def revoke(stage, context):
        if stage == "after_intent":
            _, claim, _, _ = await state(merge)
            raw = merge.store._read(f"TENANT#{merge.node.org_id}", f"EXEC#{claim.active_run_id}")
            raw["status"] = {"S": "revoked"}
            merge.store.client.put_item(TableName=merge.store.table, Item=raw)

    await tick(merge, revoke)
    assert merge.mutations == []
    assert (await state(merge))[2].state == "running"


async def test_artifact_integrity_failure_cannot_authorize_merge(merge):
    merge.objects[merge.artifact_key] += b" "
    result = await tick(merge)
    assert result.blocked == 1 and merge.mutations == []


async def test_merge_observer_resolves_authenticated_evidence(merge):
    from src.orchestration.execution_runner import RunnerContext

    async with merge.factory() as db:
        record = (await load_execution(db, identity=merge.identity)).record
    observed = await MergeController(merge.factory, merge.merge_services)._observe(RunnerContext(merge.identity, record, datetime.now(UTC)))
    assert observed.kind.value == "ready", observed
