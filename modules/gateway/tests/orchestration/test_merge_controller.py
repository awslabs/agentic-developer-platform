"""Real PostgreSQL, protected grants, R1 artifact reload and expected-head HTTP effects."""

from __future__ import annotations

import hashlib
import io
import json
from contextlib import asynccontextmanager
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
from tests.orchestration.test_merge_evidence import provider_data, unavailable_rules_data
from tests.orchestration.test_review_cycle import cycle, pg_server, pg_url, state, store  # noqa: F401
from tests.orchestration.test_review_cycle import tick as review_tick
from tests.orchestration.test_review_evidence import APPROVE, _all_refs


@pytest.fixture
async def merge(cycle, monkeypatch):  # noqa: F811
    async with prepared_merge(cycle, monkeypatch) as ctx:
        yield ctx


@asynccontextmanager
async def prepared_merge(ctx, monkeypatch, *, merge_sha="c" * 40):
    result = await review_tick(ctx)
    assert result.effects_succeeded == 1, ((await state(ctx))[0].block_detail, result)
    execution, claim, node, actions = await state(ctx)
    reviewer = claim.active_run_id
    await ctx.finish(reviewer)
    doc = json.loads(json.dumps(deepcopy(APPROVE)).replace(APPROVE["subject"]["reviewed_head_sha"], ctx.head))
    doc["scope"].update(org_id=node.org_id, flow_id=node.flow_id, node_id=node.id, execution_id=execution.id, cycle=ctx.identity.cycle)
    doc["authority"].update(accepted_plan_version=1, claim_id=ctx.identity.claim_id, claim_generation=ctx.identity.claim_generation)
    doc["repository"].update(repo=ctx.binding.repo, provider_repository_id=123)
    doc["subject"].update(pr_number=ctx.binding.pr_number, provider_pr_node_id=ctx.binding.provider_pr_node_id, reviewed_head_sha=ctx.head)
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
    data["pr"].update(number=ctx.binding.pr_number, node_id=ctx.binding.provider_pr_node_id, merged=False, merged_at=None)
    data["pr"]["head"].update(sha=ctx.head, ref="agent/issue-43", repo={"id": 123, "full_name": ctx.binding.repo})
    data["pr"]["base"].update(sha="b" * 40, ref="main", repo={"id": 123, "full_name": ctx.binding.repo})
    data["repository"].update(id=123, full_name=ctx.binding.repo)
    data["branch"]["commit"]["sha"] = "b" * 40
    data["checks"]["check_runs"][0]["head_sha"] = ctx.head
    data["reviews"][0]["commit_id"] = ctx.head
    data["graphql"]["data"]["repository"].update(databaseId=123)
    data["graphql"]["data"]["repository"]["pullRequest"].update(
        id=ctx.binding.provider_pr_node_id,
        headRefOid=ctx.head,
        baseRefOid="b" * 40,
        merged=False,
        mergeQueueEntry=None,
        author={"login": "developer"},
        commits={"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
        reviews={
            "pageInfo": {"hasPreviousPage": False},
            "nodes": [
                {"author": {"login": "reviewer"}, "state": "APPROVED", "submittedAt": datetime.now(UTC).isoformat(), "commit": {"oid": ctx.head}}
            ],
        },
    )
    calls, mutations = [], []
    ctx.remote, ctx.mutations, ctx.http_calls = data, mutations, calls
    ctx.timeout_after_merge = False
    ctx.unavailable = False
    ctx.conflict = False

    def merge_remote():
        data["pr"].update(merged=True, state="closed", merge_commit_sha=merge_sha, merged_at=datetime.now(UTC).isoformat())
        data["graphql"]["data"]["repository"]["pullRequest"].update(
            merged=True, mergeQueueEntry=None, mergedAt=data["pr"]["merged_at"], mergeCommit={"oid": merge_sha}
        )

    ctx.merge_remote = merge_remote

    async def respond(request):
        assert request.headers["Authorization"] == "Bearer scoped-test-token"
        calls.append((request.method, request.url.path))
        if ctx.unavailable:
            raise httpx.ReadTimeout("test outage")
        path = request.url.path
        if path.endswith("/merge"):
            if getattr(ctx, "on_mutation", None):
                await ctx.on_mutation()
            mutations.append(json.loads(request.content))
            assert json.loads(request.content)["sha"] == ctx.head
            if ctx.conflict:
                return httpx.Response(409, json={"message": "merge conflict"})
            merge_remote()
            if ctx.timeout_after_merge:
                raise httpx.ReadTimeout("response lost after merge")
            return httpx.Response(200, json={"merged": True, "sha": merge_sha})
        if path.endswith("/comments"):
            return httpx.Response(200, json=[])
        if path == "/graphql":
            body = json.loads(request.content)
            if "rulesets(first:100,includeParents:true)" in body["query"] and "capability" in data:
                return httpx.Response(200, json=data["capability"])
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
        elif path.endswith(f"/pulls/{ctx.binding.pr_number}"):
            key = "pr"
        elif path == f"/repos/{ctx.binding.repo}":
            key = "repository"
        else:
            pytest.fail(f"Unexpected request {request.method} {path}")
        return data[key] if isinstance(data[key], httpx.Response) else httpx.Response(404 if data[key] is None else 200, json=data[key])

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


async def test_read_only_repository_projection_merges_and_completes(merge):
    for key in ("allow_merge_commit", "allow_squash_merge", "allow_rebase_merge"):
        del merge.remote["repository"][key]
    await test_engine_expected_head_merge_then_verified_code_completion(merge, True, False)


@pytest.mark.parametrize("saved_base_is_behind", [False, True])
@pytest.mark.parametrize("rest_rules_available", [False, True])
async def test_engine_expected_head_merge_then_verified_code_completion(merge, saved_base_is_behind, rest_rules_available):
    ctx = merge
    if not rest_rules_available:
        unavailable = unavailable_rules_data()
        ctx.remote.update(rules=unavailable["rules"], capability=unavailable["capability"])
        ctx.remote["capability"]["data"]["repository"]["databaseId"] = ctx.binding.provider_repository_id
    if saved_base_is_behind:
        ctx.remote["pr"]["base"]["sha"] = "d" * 40
        ctx.remote["graphql"]["data"]["repository"]["pullRequest"]["baseRefOid"] = "d" * 40
    first = await tick(ctx)
    assert first.effects_succeeded == 1, (first, [(row.detail or {}).get("observation") for row in await merge_actions(ctx)])
    assert (await state(ctx))[2].state == "running"
    assert len(ctx.mutations) == 1
    second = await tick(ctx)
    execution, claim, node, _ = await state(ctx)
    assert node.state == "passed", second
    assert execution.phase == "deployment_pending" and execution.status == "runnable"
    assert claim.state == "held" and claim.generation == 5
    receipt = MergeReceipt.model_validate((await merge_actions(ctx))[0].detail["merge_receipt"])
    assert receipt.merge_sha == "c" * 40 and not receipt.adopted
    assert receipt.reviewed_base_sha == "b" * 40
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


async def test_external_merge_completes_with_current_verified_evidence_without_mutation(merge):
    merge.merge_remote()
    result = await tick(merge)
    execution, _, node, _ = await state(merge)
    assert node.state == "passed", result
    assert execution.phase == "deployment_pending"
    assert merge.mutations == []
    actions = await merge_actions(merge)
    assert len(actions) == 1 and actions[0].status == "succeeded"
    receipt = MergeReceipt.model_validate(actions[0].detail["merge_receipt"])
    assert receipt.verification_kind == "post_merge_verification"
    assert receipt.adopted and receipt.method == "external" and receipt.reviewed_base_sha is None
    assert receipt.merged_at <= receipt.eligibility_observed_at <= receipt.observed_at
    assert actions[0].detail["merge_mutation_performed"] is False
    assert (
        receipt.eligibility_digest
        == hashlib.sha256(json.dumps(actions[0].detail["post_merge_verification"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    assert all("write" not in call.kwargs.get("permissions", {}).values() for call in merge.mint.call_args_list)
    for change in (
        {"verification_kind": "pre_merge_authorization"},
        {"adopted": False},
        {"method": "squash"},
        {"reviewed_base_sha": "b" * 40},
        {"eligibility_observed_at": receipt.merged_at - timedelta(seconds=1)},
    ):
        with pytest.raises(ValueError):
            MergeReceipt.model_validate({**receipt.model_dump(), **change})
    from src.orchestration.deployment_authority import load_delivery_merge

    async with merge.factory() as db:
        _, _, delivery_receipt = await load_delivery_merge(db, identity=merge.identity, node=node)
        assert delivery_receipt == receipt


@pytest.mark.parametrize("defect", ["checks", "review", "stale_review", "head", "repository", "merge_sha", "review_artifact", "timestamp"])
async def test_external_merge_requires_real_matching_review_checks_and_provider_evidence(merge, defect):
    merge.merge_remote()
    record = merge.remote["graphql"]["data"]["repository"]["pullRequest"]
    if defect == "checks":
        record["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["state"] = "FAILURE"
    elif defect == "review":
        record["reviewDecision"] = "CHANGES_REQUESTED"
    elif defect == "stale_review":
        record["reviews"]["nodes"][0]["commit"]["oid"] = "e" * 40
    elif defect == "head":
        merge.remote["pr"]["head"]["sha"] = record["headRefOid"] = "e" * 40
    elif defect == "repository":
        merge.remote["graphql"]["data"]["repository"]["databaseId"] = 999
    elif defect == "merge_sha":
        record["mergeCommit"]["oid"] = "e" * 40
    elif defect == "timestamp":
        record["mergedAt"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    else:
        merge.objects[merge.artifact_key] = b"{}"
    result = await tick(merge)
    assert (await state(merge))[2].state == "running", result
    assert merge.mutations == []
    assert not await merge_actions(merge)


async def test_review_after_external_merge_is_recorded_as_post_merge_verification(merge):
    merge.merge_remote()
    earlier = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    merge.remote["pr"]["merged_at"] = earlier
    merge.remote["graphql"]["data"]["repository"]["pullRequest"]["mergedAt"] = earlier
    result = await tick(merge)
    assert (await state(merge))[2].state == "passed", result
    receipt = MergeReceipt.model_validate((await merge_actions(merge))[0].detail["merge_receipt"])
    assert receipt.merged_at < receipt.eligibility_observed_at and receipt.verification_kind == "post_merge_verification"


async def test_failed_ci_returns_to_codex_repair_without_developer_handoff(merge):
    merge.remote["checks"]["check_runs"][0]["conclusion"] = "failure"
    result = await tick(merge)
    assert (await state(merge))[0].phase == "repairing", result
    assert merge.mutations == []
    result = await review_tick(merge)
    assert result.effects_succeeded == 1, result
    assert merge.calls[-1]["persona"] == "agent-codex-reviewer"
    finding = merge.calls[-1]["review_cycle_input"]["findings"][0]["summary"]
    assert "CI checks" in finding and "test: failure" in finding


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
    assert merge.calls[-1]["persona"] == "agent-codex-reviewer"
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


async def test_crash_after_intent_reuses_one_merge_identity(merge):
    async def crash(stage, context):
        if stage == "after_intent":
            raise RuntimeError("simulated controller loss")

    first = await tick(merge, crash)
    assert first.errors == 1 and merge.mutations == []
    before = (await merge_actions(merge))[0].operation_key
    await tick(merge)
    await tick(merge)
    assert (await state(merge))[2].state == "passed"
    assert [row.operation_key for row in await merge_actions(merge)] == [before]
    assert len(merge.mutations) == 1


async def test_crash_after_remote_success_adopts_only_verified_historical_evidence(merge):
    async def crash(stage, context):
        if stage == "after_effect":
            raise RuntimeError("simulated lost local receipt")

    await tick(merge, crash)
    await tick(merge)
    receipt = MergeReceipt.model_validate((await merge_actions(merge))[0].detail["merge_receipt"])
    assert receipt.adopted and (await state(merge))[2].state == "passed"
    assert len(merge.mutations) == 1


async def test_unknown_outcome_keeps_claim_and_pending_action(merge):
    async def crash(stage, context):
        if stage == "after_intent":
            raise RuntimeError("stop before provider")

    await tick(merge, crash)
    before = (await merge_actions(merge))[0].operation_key
    merge.unavailable = True
    await tick(merge)
    execution, claim, node, _ = await state(merge)
    assert execution.status == "awaiting_external" and execution.pending_action_key == before
    assert claim.state == "held" and node.state == "running"
    assert merge.mutations == []


async def test_head_change_after_intent_prevents_mutation(merge):
    async def change(stage, context):
        if stage == "after_intent":
            merge.remote["pr"]["head"]["sha"] = "d" * 40
            merge.remote["graphql"]["data"]["repository"]["pullRequest"]["headRefOid"] = "d" * 40

    await tick(merge, change)
    assert merge.mutations == []
    await tick(merge)
    assert (await state(merge))[0].phase == "awaiting_review"


async def test_concurrent_ticks_mutate_once(merge):
    import asyncio

    results = await asyncio.gather(tick(merge), tick(merge))
    assert sum(report.effects_succeeded for report in results) <= 1
    assert len(merge.mutations) == 1
    await tick(merge)
    assert (await state(merge))[2].state == "passed"


async def test_code_dependencies_release_but_human_and_deployment_gates_remain(merge):
    from src.orchestration.models import OrchestrationEdge
    from src.orchestration.tick import run_tick
    from src.shared.models.base import Base

    async with merge.factory() as db:
        conn = await db.connection()
        await conn.run_sync(lambda connection: Base.metadata.create_all(connection, tables=[OrchestrationEdge.__table__]))
        children = {}
        for name, kind, status in (
            ("code", "story", "pending"),
            ("human", "gate", "pending"),
            ("deploy", "gate", "awaiting_gate"),
            ("after-deploy", "story", "pending"),
        ):
            child = OrchestrationNode(
                org_id=merge.node.org_id, flow_id=merge.node.flow_id, epic_ref="E1", wave_ref="W1", node_ref=name, kind=kind, state=status, title=name
            )
            db.add(child)
            children[name] = child
        await db.flush()
        for name in ("code", "human"):
            db.add(OrchestrationEdge(org_id=merge.node.org_id, flow_id=merge.node.flow_id, from_node_id=merge.node.id, to_node_id=children[name].id))
        db.add(
            OrchestrationEdge(
                org_id=merge.node.org_id, flow_id=merge.node.flow_id, from_node_id=children["deploy"].id, to_node_id=children["after-deploy"].id
            )
        )
        await db.commit()
    await tick(merge)
    await tick(merge)
    async with merge.factory() as db:
        report = await run_tick(db)
        assert report.success, report
        await db.commit()
        states = {name: (await db.get(OrchestrationNode, child.id)).state for name, child in children.items()}
    assert states == {"code": "ready", "human": "awaiting_gate", "deploy": "awaiting_gate", "after-deploy": "pending"}


async def test_expanded_scope_or_halted_flow_after_intent_cannot_merge(merge):
    from src.orchestration.models import OrchestrationFlow

    async def halt(stage, context):
        if stage == "after_intent":
            async with merge.factory() as db:
                flow = await db.get(OrchestrationFlow, merge.node.flow_id)
                flow.state = "halted"
                await db.commit()

    await tick(merge, halt)
    assert merge.mutations == []
    assert (await state(merge))[2].state == "running"


async def test_live_grant_refusal_is_preserved_as_a_block(merge):
    _, claim, _, _ = await state(merge)
    raw = merge.store._read(f"TENANT#{merge.node.org_id}", f"EXEC#{claim.active_run_id}")
    raw["status"] = {"S": "revoked"}
    merge.store.client.put_item(TableName=merge.store.table, Item=raw)
    result = await tick(merge)
    execution, _, _, _ = await state(merge)
    assert result.blocked == 1 and execution.block_code == "authority_unverifiable"
    assert merge.mutations == []


@pytest.mark.parametrize("attempts,allowed", [(7, True), (8, False)])
async def test_last_remaining_attempt_is_usable_but_never_reset(merge, attempts, allowed):
    from src.orchestration.models import OrchestrationExecution

    async with merge.factory() as db:
        execution = await db.get(OrchestrationExecution, merge.execution.id)
        execution.attempts = attempts
        await db.commit()
    result = await tick(merge)
    assert len(merge.mutations) == int(allowed), result
    execution, claim, _, _ = await state(merge)
    if allowed:
        assert execution.attempts == 8
        await tick(merge)
        assert (await state(merge))[2].state == "passed"
    else:
        assert execution.block_code == "attempts_exhausted"
    assert claim.generation == 5


async def test_current_authorization_is_committed_and_locks_released_before_provider_write(merge):
    from src.orchestration.models import OrchestrationExecution, OrchestrationFlow, OrchestrationWorkClaim

    seen = []

    async def inspect():
        async with merge.factory() as db:
            for model, identity in (
                (OrchestrationFlow, merge.node.flow_id),
                (OrchestrationExecution, merge.execution.id),
                (OrchestrationWorkClaim, merge.identity.claim_id),
            ):
                await db.scalar(select(model).where(model.id == identity).with_for_update(nowait=True))
            actions = list((await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == MERGE_KIND))).all())
            assert len(actions) == 1 and actions[0].status == "prepared"
            assert actions[0].detail["eligibility"]["state"] == "eligible"
            assert actions[0].detail["eligibility"]["requirements"]["required_approvals"] == 1
            seen.append(actions[0].operation_key)

    merge.on_mutation = inspect
    result = await tick(merge)
    assert result.effects_succeeded == 1 and len(seen) == 1, result


async def test_failed_intent_cannot_justify_later_manual_merge(merge):
    merge.conflict = True
    await tick(merge)
    merge.merge_remote()
    result = await tick(merge)
    assert result.blocked == 1
    assert (await state(merge))[2].state == "running"


async def test_non_story_cannot_receive_code_acceptance(merge):
    async with merge.factory() as db:
        node = await db.get(OrchestrationNode, merge.node.id)
        node.kind = "eval"
        await db.commit()
    result = await tick(merge)
    assert result.blocked == 1 and merge.mutations == []


@pytest.mark.parametrize("failure", ["provider_identity", "write_credential", "rules_withdrawn", "base_moved", "base_deleted", "base_retargeted"])
async def test_final_provider_preconditions_refuse_mutation(merge, failure):
    original = merge.mint.return_value

    async def mint(*args, **kwargs):
        if kwargs["permissions"]["contents"] == "write":
            if failure == "write_credential":
                raise RuntimeError("scoped credential unavailable")
            if failure == "rules_withdrawn":
                merge.remote["rules"][1]["parameters"]["required_approving_review_count"] = 2
            if failure == "base_moved":
                merge.remote["branch"]["commit"]["sha"] = "d" * 40
                merge.remote["graphql"]["data"]["repository"]["pullRequest"]["baseRef"]["target"]["oid"] = "d" * 40
            if failure == "base_deleted":
                merge.remote["graphql"]["data"]["repository"]["pullRequest"]["baseRef"] = None
            if failure == "base_retargeted":
                merge.remote["graphql"]["data"]["repository"]["pullRequest"]["baseRef"]["name"] = "other"
        return original

    merge.mint.side_effect = mint

    async def change_identity(stage, context):
        if stage == "after_intent" and failure == "provider_identity":
            merge.remote["pr"]["base"]["repo"]["id"] = 999

    await tick(merge, change_identity)
    assert merge.mutations == []
    execution, claim, node, _ = await state(merge)
    assert node.state == "running" and claim.state == "held"
    assert execution.phase == "merge_ready"


async def test_artifact_from_another_run_namespace_cannot_authorize_merge(merge):
    async with merge.factory() as db:
        row = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "review_evidence"))
        row.artifact_ref = row.artifact_ref.replace("/review-result/", "/another-run/review-result/")
        await db.commit()
    result = await tick(merge)
    assert result.blocked == 1 and merge.mutations == []
    assert "namespace" in (await state(merge))[0].block_detail


async def test_eligibility_conflict_dispatches_repair_without_merge(merge):
    merge.remote["pr"].update(mergeable=False, mergeable_state="dirty")
    await tick(merge)
    assert (await state(merge))[0].phase == "repairing"
    result = await review_tick(merge)
    assert result.effects_succeeded == 1 and merge.mutations == []
    assert merge.calls[-1]["persona"] == "agent-codex-reviewer"


async def test_github_second_precision_merge_timestamp_remains_verifiable(merge):
    await tick(merge)
    action = (await merge_actions(merge))[0]
    when = datetime.fromisoformat(action.detail["eligibility"]["observed_at"]).replace(microsecond=0)
    merge.remote["pr"]["merged_at"] = when.isoformat()
    result = await tick(merge)
    assert (await state(merge))[2].state == "passed", result
    receipt = MergeReceipt.model_validate((await merge_actions(merge))[0].detail["merge_receipt"])
    assert receipt.merged_at == when
    with pytest.raises(ValueError, match="chronological"):
        MergeReceipt.model_validate({**receipt.model_dump(), "eligibility_observed_at": when + timedelta(seconds=1)})
