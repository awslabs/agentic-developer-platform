"""A reconciled controller closes without erasing failures or granting a retry."""

# ruff: noqa: F811
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.orchestration import operations_agent_recovery as recovery
from src.orchestration.developer_recovery import retry_context
from src.orchestration.models import OrchestrationAction, OrchestrationDecision, OrchestrationFlow
from src.orchestration.operations_continuation_authority import assignment_matches, report_digest
from src.orchestration.policy_admission import AdmissionInputs
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.state import ActorKind
from tests.orchestration.test_operations_continuation import engine, recover, reporting, retained, session, session_factory, work  # noqa: F401


@pytest.fixture
async def pending(session, retained, monkeypatch):
    _, context = await recover(session, retained)
    action = OrchestrationAction(
        org_id=retained.node.org_id,
        execution_id=retained.row.id,
        operation_key="legacy-deploy",
        kind="operations_continuation_deploy",
        status="unknown",
        attempt=1,
        detail={"workflow": "deploy.yml", "observation": "Provider outcome uncertain"},
    )
    session.add(action)
    retained.row.pending_action_key = action.operation_key
    flow = await session.get(OrchestrationFlow, retained.node.flow_id)
    flow.execution_paused = True
    await session.commit()
    provider = AsyncMock(return_value=None)
    monkeypatch.setattr(recovery, "provider_run", provider)
    monkeypatch.setattr(recovery, "load_in_force_policy", AsyncMock(return_value=AdmissionInputs(retained.policy, 1)))
    actor = SimpleNamespace(org_id=retained.node.org_id, actor_id="owner", actor_kind=ActorKind.HUMAN, actor_role="platform_admin")
    request = recovery.OperationsAgentRecoveryRequest(
        expected_plan_version=1,
        expected_execution_id=retained.row.id,
        expected_attempt=3,
        expected_report_digest=report_digest(retained.report),
        no_dispatch_evidence="cloudwatch/verified-import-failure-before-provider-call",
        reason="Return deployment to the operations agent.",
    )
    return SimpleNamespace(ctx=retained, action=action, actor=actor, request=request, provider=provider, flow=flow)


async def preview(session, pending):
    return await recovery.recover(session, node_id=pending.ctx.node.id, actor=pending.actor, request=pending.request)


async def accept(session, pending):
    result = await preview(session, pending)
    request = pending.request.model_copy(update={"expected_snapshot": result["snapshot"]})
    result = await recovery.recover(session, node_id=pending.ctx.node.id, actor=pending.actor, request=request, accept=True)
    await session.commit()
    return request, result


async def test_close_retains_history_and_does_not_admit_or_reset_attempt(session, pending):
    ctx = pending.ctx
    before = copy.deepcopy(ctx.report.terminal_receipt)
    result = await preview(session, pending)
    assert result["retry_limit"] == 3
    assert ctx.node.state == "running" and pending.action.status == "unknown"
    request, result = await accept(session, pending)
    assert result["closed"]
    assert ctx.node.state == "failed" and ctx.node.attempts == 3
    assert ctx.row.phase == ctx.row.status == "concluded" and ctx.row.pending_action_key is None
    assert ctx.row.next_check_at is None
    assert ctx.claim.state == "released" and ctx.claim.active_run_id is None
    assert ctx.claim.generation == 4 and ctx.row.claim_generation == 4
    assert ctx.report.terminal_receipt == before
    assert ctx.policy.limits.max_attempts_per_node == 3 and ctx.policy.limits.max_spend_usd == 2000
    assert pending.flow.execution_paused
    assert pending.action.status == "failed" and pending.action.detail["workflow"] == "deploy.yml"
    decision = await session.get(OrchestrationDecision, result["decision_id"])
    history = json.loads(decision.reason)
    assert history["pending_action_status"] == "unknown"
    assert history["pending_action_detail"]["observation"] == "Provider outcome uncertain"
    assert history["controller"].startswith("operations-continuation:")
    assert decision.actor_id == "owner" and decision.actor_kind == "human"
    assert await assignment_matches(session, node=ctx.node, execution=ctx.row, report=ctx.report)
    context = await retry_context(session, ctx.node, 3)
    assert context["reason"] == "operations_failed" and context["previous_attempt"] == 3
    assert context["controller_closure"] == ctx.row.handoff_receipt_ref
    replay = await recovery.recover(session, node_id=ctx.node.id, actor=pending.actor, request=request, accept=True)
    assert replay == {"closed": True, "created": False, "decision_id": result["decision_id"]}


@pytest.mark.parametrize("change", ["service", "role", "tenant", "unpaused", "attempt", "report", "claim", "provider", "extra_action", "receipt"])
async def test_close_refuses_unreconciled_or_changed_assignment(session, pending, change):
    if change == "service":
        pending.actor.actor_kind = ActorKind.SERVICE
    elif change == "role":
        pending.actor.actor_role = "org_admin"
    elif change == "tenant":
        pending.actor.org_id = "another-tenant"
    elif change == "unpaused":
        pending.flow.execution_paused = False
    elif change == "attempt":
        pending.request = pending.request.model_copy(update={"expected_attempt": 2})
    elif change == "report":
        pending.request = pending.request.model_copy(update={"expected_report_digest": "f" * 64})
    elif change == "claim":
        pending.ctx.claim.active_run_id = "someone-else"
    elif change == "provider":
        pending.provider.return_value = {"id": 123, "status": "completed", "conclusion": "failure"}
    elif change == "extra_action":
        session.add(
            OrchestrationAction(
                org_id=pending.ctx.node.org_id, execution_id=pending.ctx.row.id, operation_key="another", kind="unknown", status="prepared", attempt=1
            )
        )
    elif change == "receipt":
        pending.action.receipt_ref = "github/workflow/123"
    await session.commit()
    with pytest.raises(CycleBlockedError):
        await preview(session, pending)
    assert pending.ctx.node.state == "running" and pending.action.status == "unknown"


async def test_accept_rechecks_provider_and_snapshot(session, pending):
    before = await preview(session, pending)
    request = pending.request.model_copy(update={"expected_snapshot": before["snapshot"]})
    pending.provider.return_value = {"id": 123}
    with pytest.raises(CycleBlockedError, match="provider_dispatch_exists"):
        await recovery.recover(session, node_id=pending.ctx.node.id, actor=pending.actor, request=request, accept=True)
    pending.provider.return_value = None
    pending.action.detail = {**pending.action.detail, "changed": True}
    await session.commit()
    with pytest.raises(CycleBlockedError, match="recovery_preview_changed"):
        await recovery.recover(session, node_id=pending.ctx.node.id, actor=pending.actor, request=request, accept=True)
    assert pending.action.status == "unknown"


async def test_history_is_not_authority_for_altered_report(session, pending):
    await accept(session, pending)
    pending.ctx.report.terminal_receipt = {"outcome": "complete"}
    assert not await assignment_matches(session, node=pending.ctx.node, execution=pending.ctx.row, report=pending.ctx.report)
    assert await retry_context(session, pending.ctx.node, 3) is None


async def test_no_new_automatic_controller_takeover(monkeypatch, session_factory):
    from src.orchestration.execution_runner import RunnerConfig, run_execution_runner

    takeover = AsyncMock(side_effect=AssertionError("New operations recovery must stay with the worker"))
    monkeypatch.setattr("src.orchestration.operations_continuation.recover_pending_operations", takeover)
    await run_execution_runner(session_factory, config=RunnerConfig(enabled=True))
    takeover.assert_not_awaited()


@pytest.mark.parametrize("retain_maintenance", [False, True])
async def test_retained_repair_survives_only_verified_limit_increase(session, pending, work, monkeypatch, retain_maintenance):
    from src.orchestration.operations_repair import repair_revisions
    from src.orchestration.shared_retry import apply_retry_limit

    ctx = pending.ctx
    session.add(
        OrchestrationAction(
            org_id=ctx.node.org_id,
            execution_id=ctx.row.id,
            kind="operations_repair_merge",
            status="succeeded",
            attempt=3,
            operation_key="operations-repair:77:" + "c" * 40,
            receipt_ref="github/aws-e/adp/pull/77",
            detail={
                "pull_number": 77,
                "head": "c" * 40,
                "merge_sha": "d" * 40,
                "repository": "aws-e/adp",
                "default_branch": "main",
                "policy_hash": ctx.policy.policy_hash,
                "workflows": ["deploy.yml"],
                "repair_paths": sorted(ctx.policy.repair_paths),
                "before_source_revision": "a" * 40,
                "before_definition_revision": "b" * 40,
                "claim_generation": ctx.row.claim_generation,
                "run_id": ctx.report.run_id,
            },
        )
    )
    await session.commit()
    if retain_maintenance:
        monkeypatch.setattr(
            recovery,
            "maintenance_source",
            AsyncMock(
                return_value={
                    "before": {"source_revision": "d" * 40, "definition_revision": "d" * 40, "default_branch": "main"},
                    "pins": {"source_revision": "e" * 40, "definition_revision": "e" * 40, "default_branch": "main"},
                    "deployment_run_id": 123,
                    "workflow": ".github/workflows/deploy.yml",
                }
            ),
        )
    await accept(session, pending)
    policy = apply_retry_limit(ctx.policy, 4, "verified-retry-increase")
    monkeypatch.setattr("src.orchestration.operations_repair.load_in_force_policy", AsyncMock(return_value=AdmissionInputs(policy, 1)))
    args = dict(
        node_id=ctx.node.id,
        org_id=ctx.node.org_id,
        plan_version=1,
        cycle=3,
        contract=work.contract,
        pins={"source_revision": "a" * 40, "definition_revision": "b" * 40, "default_branch": "main"},
        require_merged=True,
    )
    result = await repair_revisions(session, **args)
    expected = ("e" if retain_maintenance else "d") * 40
    assert result == {"source_revision": expected, "definition_revision": expected, "default_branch": "main"}
    if retain_maintenance:
        from src.orchestration.models import OrchestrationExecution

        successor = OrchestrationExecution(
            org_id=ctx.node.org_id,
            flow_id=ctx.node.flow_id,
            node_id=ctx.node.id,
            cycle=4,
            accepted_plan_version=1,
            claim_id=ctx.row.claim_id,
            claim_generation=5,
            phase="delivering",
            status="runnable",
        )
        session.add(successor)
        await session.flush()
        session.add(
            OrchestrationAction(
                org_id=ctx.node.org_id,
                execution_id=successor.id,
                kind="operations_repair_merge",
                status="succeeded",
                attempt=4,
                operation_key="operations-repair:78:" + "8" * 40,
                receipt_ref="github/aws-e/adp/pull/78",
                detail={
                    "pull_number": 78,
                    "head": "8" * 40,
                    "merge_sha": "9" * 40,
                    "repository": "aws-e/adp",
                    "default_branch": "main",
                    "policy_hash": policy.policy_hash,
                    "workflows": ["deploy.yml"],
                    "repair_paths": sorted(policy.repair_paths),
                    "before_source_revision": expected,
                    "before_definition_revision": expected,
                    "claim_generation": 5,
                    "run_id": "next-worker",
                },
            )
        )
        await session.flush()
        extended = await repair_revisions(session, **{**args, "cycle": 4})
        assert extended == {"source_revision": "9" * 40, "definition_revision": "9" * 40, "default_branch": "main"}
    policy._shared_retry_decision_id = None
    assert await repair_revisions(session, **args) is None


@pytest.mark.parametrize("invalid", [None, "sha", "failed", "repository", "definition", "ancestry", "incomplete_pair"])
async def test_maintenance_source_requires_deployment_and_ancestry_proof(session, pending, monkeypatch, invalid):
    import base64

    from src.orchestration.deployment_release_provider import ReleaseProvider

    ctx = pending.ctx
    pins = {"source_revision": "d" * 40, "definition_revision": "d" * 40, "default_branch": "main"}
    request = pending.request.model_copy(update={"maintenance_source_revision": "e" * 40, "maintenance_deployment_run_id": 123})
    run = {
        "id": 123,
        "repository": {"id": 123},
        "head_sha": "e" * 40,
        "status": "completed",
        "conclusion": "success",
        "path": ".github/workflows/deploy.yml",
    }
    if invalid == "sha":
        run["head_sha"] = "f" * 40
    elif invalid == "failed":
        run["conclusion"] = "failure"
    elif invalid == "repository":
        run["repository"] = {"id": 999}
    elif invalid == "incomplete_pair":
        request = request.model_copy(update={"maintenance_deployment_run_id": None})

    async def read(self, binding, method, path, **kwargs):
        assert method == "GET"
        if "/actions/runs/" in path:
            body = run
        elif "/git/ref/" in path:
            body = {"object": {"sha": "e" * 40}}
        else:
            content = b"workflow"
            if invalid == "definition" and kwargs["params"]["ref"] == "e" * 40:
                content = b"changed-workflow"
            body = {"type": "file", "encoding": "base64", "size": len(content), "content": base64.b64encode(content).decode()}
        return SimpleNamespace(json=lambda: body)

    contains = AsyncMock(side_effect=CycleBlockedError("not-descendant") if invalid == "ancestry" else None)
    monkeypatch.setattr(ReleaseProvider, "request", read)
    monkeypatch.setattr(ReleaseProvider, "contains", contains)
    monkeypatch.setattr(recovery, "maintenance_context", AsyncMock(return_value={"target": "dev", "digest": "f" * 64, "reference": "s3/context"}))
    monkeypatch.setattr("src.orchestration.operations_release_resolution.operational_execution_pins", AsyncMock(return_value=pins))
    monkeypatch.setattr("src.orchestration.operations_repair.repair_revisions", AsyncMock(return_value=pins))
    args = dict(
        node=ctx.node,
        row=ctx.row,
        report=ctx.report,
        binding=SimpleNamespace(repo="aws-e/adp", provider_repository_id=123),
        request=request,
        workflow="deploy.yml",
    )
    if invalid:
        with pytest.raises(CycleBlockedError):
            await recovery.maintenance_source(session, **args)
    else:
        result = await recovery.maintenance_source(session, **args)
        assert result["before"] == pins and result["pins"]["source_revision"] == "e" * 40
        contains.assert_awaited_once()


@pytest.mark.parametrize("account_type,auth_source", [("service", "jwt"), ("human", "iam")])
async def test_recovery_route_rejects_nonhuman_session(account_type, auth_source):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import Mock

    from fastapi import HTTPException

    from src.orchestration.controls import _operations_agent_recovery
    from src.shared.schemas.auth import TokenContext

    context = TokenContext(
        user_id="operator",
        org_id="org",
        team_id="",
        department_id="",
        account_type=account_type,
        auth_source=auth_source,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    access = SimpleNamespace(require_platform_admin=Mock(), check_permission=AsyncMock())
    db = AsyncMock()
    with pytest.raises(HTTPException) as error:
        await _operations_agent_recovery("node", SimpleNamespace(), context, access, db, accept=True)
    assert error.value.status_code == 403
    db.commit.assert_not_called()


@pytest.mark.parametrize("invalid", [None, "account", "resource", "source", "missing"])
async def test_maintenance_context_is_bound_to_the_accepted_target(monkeypatch, invalid):
    from src.orchestration.deployment_manifest import EntryStatus

    source = "e" * 40
    entry = SimpleNamespace(
        status=EntryStatus.ENABLED,
        connection_id="connection",
        covers=lambda component: component == "gateway-backend",
        workflow=SimpleNamespace(path=".github/workflows/deploy.yml", allowed_inputs={"environment": frozenset({"dev"})}),
        resource_kind="eks-namespace",
        resource_id="cluster/namespace",
    )
    contract = SimpleNamespace(
        environment_connection_ids=["connection"],
        node=SimpleNamespace(operational_release_binding=SimpleNamespace(target="dev", component="gateway-backend")),
    )
    context = SimpleNamespace(
        repository_id=123,
        run_id=45,
        run_attempt=1,
        workflow_path=entry.workflow.path,
        workflow_revision=source,
        source_revision=source,
        account_id="123456789012",
        region="us-east-1",
        resource_kind=entry.resource_kind,
        resource_id=entry.resource_id,
    )
    if invalid == "account":
        context.account_id = "999999999999"
    elif invalid == "resource":
        context.resource_id = "unrelated/namespace"
    elif invalid == "source":
        context.source_revision = "f" * 40
    monkeypatch.setattr("src.orchestration.deployment_manifest.load_packaged_manifest", lambda: SimpleNamespace(entry=lambda name: entry))
    monkeypatch.setattr("src.agentauth.actions_routes._evidence_account", lambda: "123456789012")
    monkeypatch.setattr("src.shared.config.get_settings", lambda: SimpleNamespace(aws_region="us-east-1"))
    monkeypatch.setattr(
        "src.agentauth.actions_routes._s3_context", AsyncMock(return_value=None if invalid == "missing" else (context, "f" * 64, "s3/context"))
    )
    args = (object(), SimpleNamespace(provider_repository_id=123), {"id": 45, "run_attempt": 1}, contract, "deploy.yml", source)
    if invalid:
        with pytest.raises(CycleBlockedError):
            await recovery.maintenance_context(*args)
    else:
        assert await recovery.maintenance_context(*args) == {"reference": "s3/context", "digest": "f" * 64, "target": "dev"}
