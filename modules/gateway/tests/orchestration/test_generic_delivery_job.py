"""Real target leases fence authenticated generic workflow jobs and publication."""

import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src.orchestration import generic_delivery_dispatch as dispatch
from src.orchestration import generic_delivery_job as generic
from src.orchestration import operations_release_job as jobs
from src.orchestration.delivery_contract import DeliveryContract
from src.orchestration.deployment_manifest import PhysicalTarget, TargetEvidence
from src.orchestration.models import OrchestrationAction, OrchestrationEnvironmentLease, OrchestrationFlow, OrchestrationWorkClaim
from src.orchestration.proposal import OperationalReleaseBinding
from src.orchestration.run_reports import OrchestrationRunReport
from tests.orchestration.test_operations_release_job import job  # noqa: F401
from tests.orchestration.test_operations_release_job_postgres import retained_job  # noqa: F401
from tests.orchestration.test_review_cycle import cycle, pg_server, pg_url, store  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("boundary", [None, "repository", "attempt", "run", "conclusion", "boolean", "shape"])
async def test_recorded_terminal_receipt_retains_exact_dispatch_identity(boundary):
    from src.orchestration.generic_delivery_recovery import _terminal_run

    binding = SimpleNamespace(repo="example/repository", provider_repository_id=17)
    terminal = {"run_id": 42, "run_attempt": 2, "conclusion": "failure"}
    saved = {
        "repo": binding.repo,
        "provider_repository_id": 17,
        "dispatch_revision": "a" * 40,
        "workflow": ".github/workflows/release.yml",
        "correlation": "selected-dispatch",
        "run_id": 42,
        "run_attempt": 1,
        "terminal_run": terminal,
    }
    if boundary == "repository":
        saved["provider_repository_id"] = 18
    elif boundary == "attempt":
        terminal["run_attempt"] = 1
    elif boundary == "run":
        terminal["run_id"] = 43
    elif boundary == "conclusion":
        terminal["conclusion"] = "in_progress"
    elif boundary == "boolean":
        terminal["run_id"] = True
    elif boundary == "shape":
        saved["terminal_run"] = []
    provider = SimpleNamespace(request=AsyncMock(side_effect=AssertionError("stored receipt must not use the latest attempt")))
    assert await _terminal_run(provider, binding, saved, "test-token", allow_success=True, reuse_terminal=True) == (None if boundary else terminal)
    provider.request.assert_not_awaited()


async def test_target_readback_returns_only_second_authorized_snapshot(monkeypatch):
    from contextlib import asynccontextmanager

    from src.orchestration.review_cycle import CycleBlockedError

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(commit=AsyncMock())

    candidate = SimpleNamespace(id="selected-action")
    selected = {
        "connection_id": "authorized-connection",
        "resource_kind": "service",
        "resource_id": "preview/payments",
        "account_id": "123456789012",
        "region": "us-east-1",
    }

    async def snapshot(service, session, body, claims, *, expected_action=None, started_at=None):
        assert expected_action == (candidate.id if started_at else None)
        return candidate, SimpleNamespace(target_identity=selected if started_at else {"connection_id": "old"})

    monkeypatch.setattr(generic, "snapshot", snapshot)
    service = SimpleNamespace(factory=factory, verify_run=AsyncMock(return_value=datetime.now(UTC)))
    body, claims = SimpleNamespace(), {"run_id": "42", "run_attempt": "1"}
    assert await generic.authorize_job(service, body, claims, include_target=True) == selected
    assert service.verify_run.await_count == 1
    assert await generic.authorize_job(service, body, claims) is None

    async def refused(service, session, body, claims, *, expected_action=None, started_at=None):
        if started_at:
            raise CycleBlockedError("selected_scope_revoked")
        return candidate, SimpleNamespace(target_identity=selected)

    monkeypatch.setattr(generic, "snapshot", refused)
    with pytest.raises(HTTPException) as error:
        await generic.authorize_job(service, body, claims, include_target=True)
    assert error.value.status_code == 403


@pytest.fixture
async def generic_job(retained_job, monkeypatch):  # noqa: F811
    ctx, prepared, body, guard = retained_job
    delivery = DeliveryContract(
        schema_version=2,
        connection_id="delivery-connection",
        resource_kind="service",
        resource_id="preview/payments",
        definition_revision=prepared.pins.return_value["definition_revision"],
        baseline_revision=body.source_revision,
        workflow_inputs={"environment": "test"},
        required_checks=["checkout"],
        cleanup_required=True,
        job_mutation_role_arn="arn:aws:iam::123456789012:role/payments-workflow",
    )
    release = OperationalReleaseBinding(
        repository=ctx.binding.repo,
        target="payments-test",
        component="payments-api",
        workflows=[body.workflow],
        default_branch="main",
        predecessors=[{"address": "flow/epic/build/implementation", "receipt_kind": "verified_merge"}],
        build_prerequisites=["flow/epic/build/implementation"],
        delivery_contract=delivery,
    )
    prepared.contract.node.operational_release_binding = release
    prepared.contract.node.evidence_expectations = ["runtime_verified:payments-api"]
    monkeypatch.setattr(generic, "accepted_node_contract", AsyncMock(return_value=prepared.contract))
    physical = PhysicalTarget(
        "aws",
        "123456789012",
        "us-east-1",
        "service",
        "preview/payments",
        TargetEvidence("registered-aws-role:delivery-connection", datetime.now(UTC).isoformat()),
    )

    async def resolve(session, *, authorize_scope, **kwargs):
        # Generic job authorization never requests an application-specific probe.
        assert "inspect_target" not in kwargs
        assert kwargs["entry"].verification_adapter == "workflow-evidence-v2"
        authorize_scope("delivery-connection", body.role_arn)
        return SimpleNamespace(physical=physical, role_arn=body.role_arn)

    prepared.targets.resolve.side_effect = resolve
    monkeypatch.setattr(dispatch, "DeploymentTargetResolver", lambda: prepared.targets)
    monkeypatch.setattr(dispatch, "ReviewCycleServices", lambda _: ctx.service)
    guard.manifest_loader = lambda: pytest.fail("generic job cannot read the legacy manifest")
    async with ctx.factory() as session:
        report = await session.get(OrchestrationRunReport, ctx.root)
        metadata = deepcopy(report.dispatch_metadata)
        record = metadata["orchestration"]["release_binding"]
        record.update(
            target=release.target,
            component=release.component,
            delivery_contract_digest=delivery.digest,
            resource_kind=delivery.resource_kind,
            resource_id=delivery.resource_id,
            receipts=[{"address": "flow/epic/build/implementation"}],
        )
        metadata["handoff_expect"] = {
            "execution_id": ctx.execution.id,
            "accepted_plan_version": 1,
            "claim_id": ctx.identity.claim_id,
            "claim_generation": ctx.identity.claim_generation,
        }
        report.dispatch_metadata = metadata
        recorded = await session.scalar(select(OrchestrationAction).where(OrchestrationAction.operation_key == "operations-release:1"))
        recorded.detail = deepcopy(record)
        await session.commit()
        protected = {"invocation_id": {"S": ctx.root}, **{key: {"S": value} for key, value in record["pins"].items()}}
        reservation = await dispatch.reserve_dispatch(
            session,
            factory=ctx.factory,
            node=ctx.node,
            flow=ctx.flow,
            contract=prepared.contract,
            entry=delivery.manifest(release).entry(release.target),
            binding=ctx.binding,
            execution=protected,
            dispatch_revision=body.dispatch_revision,
        )
        await session.commit()
    prepared.run["created_at"] = datetime.now(UTC).isoformat()
    body = body.model_copy(update={"contract_digest": delivery.digest, "mutation_role_arn": delivery.job_mutation_role_arn})
    return SimpleNamespace(ctx=ctx, prepared=prepared, body=body, guard=guard, reservation=reservation, delivery=delivery, protected=protected)


def test_optional_job_authority_preserves_existing_delivery_digest():
    document = dict(
        schema_version=2,
        connection_id="connection",
        resource_kind="service",
        resource_id="test/api",
        definition_revision="a" * 40,
        baseline_revision="b" * 40,
        workflow_inputs={},
        required_checks=["api"],
        cleanup_required=False,
        evidence_artifact="delivery-evidence",
    )
    contract = DeliveryContract.model_validate(document)
    assert contract.model_dump(mode="json") == document
    assert contract.digest == hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert contract.model_copy(update={"job_mutation_role_arn": "arn:aws:iam::123456789012:role/build"}).digest != contract.digest


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
async def test_generic_job_replays_authority_and_publication_without_accepting_deployment(generic_job):
    scenario = generic_job
    target = await scenario.guard.target_identity(scenario.body, scenario.prepared.claims)
    assert target == {
        "connection_id": "delivery-connection",
        "resource_kind": "service",
        "resource_id": "preview/payments",
        "account_id": "123456789012",
        "region": "us-east-1",
    }
    await scenario.guard.authorize(scenario.body, scenario.prepared.claims)
    await scenario.guard.authorize(scenario.body, scenario.prepared.claims)
    publication = jobs.NativePublicationRequest(**scenario.body.model_dump(), image_digest="sha256:" + "f" * 64)
    with pytest.raises(HTTPException):
        await scenario.guard.settle_publication(publication, scenario.prepared.claims)
    await scenario.guard.mark_publication(publication, scenario.prepared.claims)
    await scenario.guard.mark_publication(publication, scenario.prepared.claims)
    await scenario.guard.settle_publication(publication, scenario.prepared.claims)
    await scenario.guard.settle_publication(publication, scenario.prepared.claims)
    await scenario.guard.mark_publication(publication, scenario.prepared.claims)
    with pytest.raises(HTTPException):
        await scenario.guard.mark_publication(publication.model_copy(update={"image_digest": "sha256:" + "e" * 64}), scenario.prepared.claims)
    async with scenario.ctx.factory() as session:
        action = await session.get(OrchestrationAction, scenario.reservation["id"])
        assert action.detail["release_job"] == {"run_id": "42", "run_attempt": "1"}
        assert action.detail["publication"]["state"] == "observed"
        assert action.status == "prepared"
        assert "deployment_receipt" not in action.detail
        lease = await session.scalar(
            select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.owner_action_id == f"delivery:{scenario.ctx.execution.id}")
        )
        assert lease.state == "held"


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "role",
        "mutation_role",
        "source",
        "definition",
        "actor",
        "repository",
        "workflow",
        "attempt",
        "paused",
        "generation",
        "lease",
        "record",
        "terminal",
    ],
)
async def test_generic_job_refuses_changed_authority(generic_job, change):
    scenario = generic_job
    body, claims = scenario.body, dict(scenario.prepared.claims)
    if change in {"digest", "role", "mutation_role", "source", "definition"}:
        field, value = {
            "digest": ("contract_digest", "0" * 64),
            "role": ("role_arn", "arn:aws:iam::123456789012:role/other"),
            "mutation_role": ("mutation_role_arn", "arn:aws:iam::123456789012:role/other"),
            "source": ("source_revision", "0" * 40),
            "definition": ("dispatch_revision", "0" * 40),
        }[change]
        body = body.model_copy(update={field: value})
    elif change in {"actor", "repository", "workflow", "attempt"}:
        field, value = {
            "actor": ("actor_id", "99"),
            "repository": ("repository", "foreign/project"),
            "workflow": ("workflow_ref", "foreign/ship.yml@refs/heads/main"),
            "attempt": ("run_attempt", "2"),
        }[change]
        claims[field] = value
    else:
        async with scenario.ctx.factory() as session:
            if change == "paused":
                flow = await session.get(OrchestrationFlow, scenario.ctx.flow.id)
                flow.execution_paused = True
            elif change == "generation":
                claim = await session.get(OrchestrationWorkClaim, scenario.ctx.identity.claim_id)
                claim.generation += 1
            elif change == "lease":
                lease = await session.scalar(
                    select(OrchestrationEnvironmentLease).where(
                        OrchestrationEnvironmentLease.owner_action_id == f"delivery:{scenario.ctx.execution.id}"
                    )
                )
                lease.owner_generation += 1
            elif change == "record":
                report = await session.get(OrchestrationRunReport, scenario.ctx.root)
                report.dispatch_metadata = {}
            else:
                action = await session.get(OrchestrationAction, scenario.reservation["id"])
                action.detail = {**action.detail, "terminal_run": {"run_id": 42, "run_attempt": 1, "conclusion": "failure"}}
            await session.commit()
    with pytest.raises(HTTPException) as error:
        await scenario.guard.authorize(body, claims)
    assert error.value.status_code == 403


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
async def test_competing_generic_jobs_pin_only_one_provider_run(generic_job):
    scenario = generic_job
    arrived, ready = [], asyncio.Event()

    async def verify(claims, binding, body):
        arrived.append(claims["run_id"])
        if len(arrived) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=10)
        return datetime.fromisoformat(scenario.prepared.run["created_at"])

    scenario.guard.verify_run = verify
    results = await asyncio.gather(
        scenario.guard.authorize(scenario.body, scenario.prepared.claims),
        scenario.guard.authorize(scenario.body, {**scenario.prepared.claims, "run_id": "43"}),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, HTTPException) and result.status_code == 403 for result in results) == 1


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
async def test_generic_job_rerun_requires_reconciled_reservation_and_attempt_start(generic_job):
    from src.orchestration.environment_leases import LeaseHolder, ReleaseReason, release_lease
    from src.orchestration.review_cycle import CycleBlockedError

    scenario = generic_job
    await scenario.guard.authorize(scenario.body, scenario.prepared.claims)
    await dispatch.settle_dispatch(scenario.ctx.factory, scenario.reservation, org_id=scenario.ctx.node.org_id, succeeded=True)
    arguments = dict(
        factory=scenario.ctx.factory,
        node=scenario.ctx.node,
        flow=scenario.ctx.flow,
        contract=scenario.prepared.contract,
        entry=scenario.delivery.manifest(scenario.prepared.contract.node.operational_release_binding).entries[0],
        binding=scenario.ctx.binding,
        execution=scenario.protected,
        dispatch_revision=scenario.body.dispatch_revision,
        run_id=42,
        run_attempt=1,
    )
    async with scenario.ctx.factory() as session:
        with pytest.raises(CycleBlockedError, match="delivery_dispatch_reconciliation_required"):
            await dispatch.reserve_dispatch(session, **arguments)
        await session.rollback()
        # Reproduce the durable result of authenticated terminal reconciliation.
        action = await session.get(OrchestrationAction, scenario.reservation["id"])
        action.detail = {**action.detail, "terminal_run": {"run_id": 42, "run_attempt": 1, "conclusion": "failure"}}
        lease = await session.scalar(
            select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.owner_action_id == f"delivery:{scenario.ctx.execution.id}")
        )
        assert (
            await release_lease(
                session,
                canonical_target_key=lease.canonical_target_key,
                holder=LeaseHolder(scenario.ctx.node.org_id, lease.owner_action_id, scenario.ctx.identity.claim_generation),
                reason=ReleaseReason.FAILED,
                terminal_evidence="github/actions/runs/42/attempts/1",
            )
        ).applied
        await session.commit()
        reservation = await dispatch.reserve_dispatch(session, **arguments)
        assert reservation["already_dispatched"] is False
        await session.commit()
    with pytest.raises(HTTPException):
        await scenario.guard.authorize(scenario.body, scenario.prepared.claims)
    scenario.prepared.run.update(run_attempt=2, run_started_at=datetime.now(UTC).isoformat())
    claims = {**scenario.prepared.claims, "run_attempt": "2"}
    await scenario.guard.authorize(scenario.body, claims)
    async with scenario.ctx.factory() as session:
        action = await session.get(OrchestrationAction, reservation["id"])
        assert action.detail["release_job"] == {"run_id": "42", "run_attempt": "2"}


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
@pytest.mark.parametrize("obstacle", [None, "running", "publication", "generation", "second-workflow", "revoked"])
async def test_later_attempt_dispatch_closes_failed_predecessor_and_takes_target(generic_job, monkeypatch, obstacle):
    """Real PostgreSQL: distinct executions/correlations, no artifact or old worker."""
    from src.orchestration.execution_policy import Action
    from src.orchestration.models import OrchestrationExecution, OrchestrationNode
    from src.orchestration.review_cycle import CycleBlockedError

    ctx, prepared = generic_job.ctx, generic_job.prepared
    async with ctx.factory() as session:
        old = await session.get(OrchestrationExecution, ctx.execution.id)
        authority = await ctx.service.authority_context(
            session, SimpleNamespace(identity=ctx.identity, execution=old), ctx.node, ctx.binding, ctx.root, Action.DEPLOY
        )
        old.status = "concluded"
        node = await session.get(OrchestrationNode, ctx.node.id)
        node.attempts = 2
        current = OrchestrationExecution(
            org_id=old.org_id,
            flow_id=old.flow_id,
            node_id=old.node_id,
            cycle=2,
            phase="delivering",
            status="runnable",
            accepted_plan_version=old.accepted_plan_version,
            claim_id=old.claim_id,
            claim_generation=old.claim_generation + 1,
        )
        session.add(current)
        previous = await session.get(OrchestrationAction, generic_job.reservation["id"])
        saved = dict(previous.detail)
        lease = await session.scalar(
            select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.owner_action_id == f"delivery:{old.id}")
        )
        if obstacle == "publication":
            lease.publication_pending_digest = "sha256:" + "f" * 64
        elif obstacle == "generation":
            lease.owner_generation += 1
        elif obstacle == "second-workflow":
            session.add(
                OrchestrationAction(
                    org_id=old.org_id,
                    execution_id=old.id,
                    operation_key="second-dispatch",
                    kind="delivery_workflow_dispatch",
                    status="prepared",
                    attempt=1,
                    detail={**saved, "dispatch_revision": "f" * 40},
                )
            )
        await session.commit()
        # Keep authorization separate from the lifecycle being tested. The
        # denied-authority case proves no provider read or lock release occurs.
        authorization = AsyncMock(return_value=authority)
        if obstacle == "revoked":
            authorization.side_effect = CycleBlockedError("grant_revoked_or_unverifiable")
        monkeypatch.setattr(ctx.service, "authority_context", authorization)
        requests = []

        async def request(binding, method, path, **kwargs):
            requests.append((method, path))
            head = kwargs["params"]["head_sha"]
            assert binding == ctx.binding
            assert "status" not in kwargs["params"]  # Include running duplicates in ambiguity detection.
            run = {
                "id": 42,
                "run_attempt": 1,
                "status": "in_progress" if obstacle == "running" or head == "f" * 40 else "completed",
                "conclusion": "failure",
                "display_title": "ADP deployment " + saved["correlation"],
                "path": saved["workflow"],
                "head_sha": head,
                "head_branch": saved["branch"],
                "event": "workflow_dispatch",
                "repository": {"id": ctx.binding.provider_repository_id},
            }
            return SimpleNamespace(json=lambda: {"total_count": 1, "workflow_runs": [run]})

        arguments = dict(
            factory=ctx.factory,
            node=node,
            flow=ctx.flow,
            contract=prepared.contract,
            entry=generic_job.delivery.manifest(prepared.contract.node.operational_release_binding).entries[0],
            binding=ctx.binding,
            execution={**generic_job.protected, "invocation_id": {"S": "later-attempt"}, "source_revision": {"S": "e" * 40}},
            dispatch_revision="e" * 40,
            provider=SimpleNamespace(request=request),
            token="test-token",
        )
        if obstacle:
            with pytest.raises(CycleBlockedError):
                await dispatch.reserve_dispatch(session, **arguments)
            assert lease.state == "held"
            assert lease.owner_action_id == f"delivery:{old.id}"
            assert previous.detail.get("terminal_run") is None
            if obstacle == "revoked":
                assert requests == []
        else:
            reservation = await dispatch.reserve_dispatch(session, **arguments)
            await session.commit()
            assert reservation["already_dispatched"] is False
            assert previous.detail["terminal_run"] == {"run_id": 42, "run_attempt": 1, "conclusion": "failure"}
            assert old.status == "concluded"
            assert lease.state == "held" and lease.owner_action_id == f"delivery:{current.id}"
            assert lease.owner_generation == current.claim_generation
            await dispatch.settle_dispatch(ctx.factory, reservation, org_id=ctx.binding.org_id, succeeded=True)
            duplicate = await dispatch.reserve_dispatch(session, **arguments)
            assert duplicate == {"id": reservation["id"], "already_dispatched": True}
            assert len(requests) == 1
        assert await session.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "deployment_verification")) is None


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
async def test_concurrent_failure_observers_release_once(generic_job):
    from src.orchestration.generic_delivery_recovery import reconcile_failed_dispatches

    ctx = generic_job.ctx
    async with ctx.factory() as session:
        action = await session.get(OrchestrationAction, generic_job.reservation["id"])
        saved = dict(action.detail)
    run = {
        "id": 42,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "failure",
        "event": "workflow_dispatch",
        "display_title": "ADP deployment " + saved["correlation"],
        "path": saved["workflow"],
        "head_sha": saved["dispatch_revision"],
        "head_branch": saved["branch"],
        "repository": {"id": ctx.binding.provider_repository_id},
    }
    observed, finish = asyncio.Event(), asyncio.Event()

    async def request(*args, **kwargs):
        observed.set()
        await finish.wait()
        return SimpleNamespace(json=lambda: {"total_count": 1, "workflow_runs": [run]})

    async def reconcile():
        async with ctx.factory() as session:
            result = await reconcile_failed_dispatches(
                session, binding=ctx.binding, node_id=ctx.node.id, provider=SimpleNamespace(request=request), token="test-token"
            )
            await session.commit()
            return result

    first = asyncio.create_task(reconcile())
    await asyncio.wait_for(observed.wait(), 5)
    second = asyncio.create_task(reconcile())
    await asyncio.sleep(0.05)
    assert not second.done()
    finish.set()
    assert sorted(await asyncio.wait_for(asyncio.gather(first, second), 5)) == [0, 1]
    async with ctx.factory() as session:
        lease = await session.scalar(
            select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.owner_action_id == f"delivery:{ctx.execution.id}")
        )
        assert lease.state == "free" and lease.release_reason == "failed"


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
@pytest.mark.parametrize(
    "obstacle",
    [
        None,
        "active",
        "foreign_tenant",
        "foreign_repo",
        "pending_effect",
        "pending_action",
        "running",
        "ambiguous",
        "generation",
        "publication",
        "rerun",
        "job_identity",
    ],
)
@pytest.mark.parametrize("rerun", [False, True])
async def test_retired_successful_dispatch_releases_target_without_passing_runtime(generic_job, obstacle, rerun):
    from src.orchestration.environment_leases import LeaseHolder, acquire_lease
    from src.orchestration.generic_delivery_recovery import reconcile_retired_target
    from src.orchestration.models import OrchestrationExecution, OrchestrationNode
    from src.orchestration.review_cycle import CycleBlockedError

    ctx = generic_job.ctx
    async with ctx.factory() as session:
        old = await session.get(OrchestrationExecution, ctx.execution.id)
        old.phase = old.status = "concluded"
        old.pending_action_key = None
        node = await session.get(OrchestrationNode, ctx.node.id)
        node.state = "superseded"
        actions = list(await session.scalars(select(OrchestrationAction).where(OrchestrationAction.execution_id == old.id)))
        for action in actions:
            action.status = "succeeded"
        prior = await session.get(OrchestrationAction, generic_job.reservation["id"])
        saved = deepcopy(prior.detail)
        prior.detail = {**saved, "release_job": {"run_id": "42", "run_attempt": "1"}}
        if rerun:
            prior.detail = {**prior.detail, "terminal_run": {"run_id": 42, "run_attempt": 1, "conclusion": "failure"}}
            latest = OrchestrationAction(
                id="retired-rerun",
                org_id=prior.org_id,
                execution_id=prior.execution_id,
                operation_key="retired-rerun",
                kind=prior.kind,
                status="succeeded",
                detail={**saved, "run_id": 42, "run_attempt": 1, "release_job": {"run_id": "42", "run_attempt": "2"}},
            )
            session.add(latest)
        lease = await session.scalar(
            select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.owner_action_id == f"delivery:{old.id}")
        )
        binding = SimpleNamespace(org_id=ctx.binding.org_id, repo=ctx.binding.repo, provider_repository_id=ctx.binding.provider_repository_id)
        if obstacle == "active":
            old.phase, old.status = "delivering", "runnable"
        elif obstacle == "foreign_tenant":
            binding.org_id = "other-tenant"
        elif obstacle == "foreign_repo":
            binding.provider_repository_id += 1
        elif obstacle == "pending_effect":
            next(a for a in actions if a.id != prior.id).status = "unknown"
        elif obstacle == "pending_action":
            old.pending_action_key = "unresolved-effect"
        elif obstacle == "generation":
            lease.owner_generation += 1
        elif obstacle == "publication":
            lease.publication_pending_digest = "sha256:" + "f" * 64
        elif obstacle == "job_identity":
            prior.detail = {**prior.detail, "release_job": {"run_id": "43", "run_attempt": "1"}}
        await session.commit()
        run = {
            "id": 42,
            "run_attempt": (2 if rerun else 1) + (1 if obstacle == "rerun" else 0),
            "status": "in_progress" if obstacle == "running" else "completed",
            "conclusion": "success",
            "event": "workflow_dispatch",
            "display_title": "ADP deployment " + saved["correlation"],
            "path": saved["workflow"],
            "head_sha": saved["dispatch_revision"],
            "head_branch": saved["branch"],
            "repository": {"id": ctx.binding.provider_repository_id},
        }
        runs = [run, {**run, "id": 43}] if obstacle == "ambiguous" else [run]
        provider = SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(json=lambda: {"total_count": len(runs), "workflow_runs": runs})))
        arguments = dict(binding=binding, target_key=lease.canonical_target_key, provider=provider, token="test-token")
        if obstacle == "publication":
            with pytest.raises(CycleBlockedError, match="delivery_target_lease_changed"):
                await reconcile_retired_target(session, **arguments)
        else:
            assert await reconcile_retired_target(session, **arguments) == (0 if obstacle else (2 if rerun else 1))
        if obstacle:
            assert lease.state == "held"
            assert prior.detail.get("terminal_run") == ({"run_id": 42, "run_attempt": 1, "conclusion": "failure"} if rerun else None)
            if obstacle in {"active", "foreign_tenant", "foreign_repo", "pending_effect", "pending_action", "generation"}:
                provider.request.assert_not_awaited()
        else:
            assert lease.state == "free" and lease.release_reason == "abandoned"
            assert prior.detail["terminal_run"] == {"run_id": 42, "run_attempt": 1, "conclusion": "failure" if rerun else "success"}
            if rerun:
                assert latest.detail["terminal_run"] == {"run_id": 42, "run_attempt": 2, "conclusion": "success"}
                assert provider.request.await_count == 1
            assert old.status == old.phase == "concluded" and node.state == "superseded"
            assert await session.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "deployment_verification")) is None
            # A different flow/execution can now acquire this authorized target.
            physical = saved["target"]
            target = PhysicalTarget(**{**physical, "evidence": TargetEvidence(**physical["evidence"])})
            assert (
                await acquire_lease(
                    session,
                    target=target,
                    holder=LeaseHolder(binding.org_id, "delivery:next-flow", 1),
                    manifest_entry_id="next-service",
                    release_ref="e" * 40,
                )
            ).applied
            assert lease.owner_action_id == "delivery:next-flow"


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
@pytest.mark.parametrize("revoke_lease", [False, True])
async def test_observation_export_runs_real_job_authority_before_reading_receipt(generic_job, revoke_lease):
    """Real job snapshot, provider verifier, target authority and PostgreSQL lease."""
    from src.orchestration.l5_observation_export import ExportObservation, export_observation
    from src.orchestration.l5_observation_scope import ObservationError

    c = generic_job
    # A live export is a reader of an already authorized dispatch, never the
    # operation that silently establishes its workflow identity.
    await c.guard.authorize(c.body, c.prepared.claims)
    async with c.ctx.factory() as session:
        action = await session.get(OrchestrationAction, c.reservation["id"])
        action.status = "succeeded"
        if revoke_lease:
            lease = await session.scalar(
                select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.owner_action_id == f"delivery:{c.ctx.execution.id}")
            )
            lease.owner_generation += 1
        await session.commit()
    body = ExportObservation.model_validate(
        {
            "job": c.body.model_dump(),
            "challenge_id": "00000000-0000-4000-8000-000000000001",
            "snapshot_sha256": "a" * 64,
            "selection": {
                "product_id": "product",
                "project_id": "project",
                "service_id": "devops",
                "binding_id": "binding",
                "configuration_id": "configuration",
                "configuration_revision": 1,
                "check_id": "ui-cli-mcp-parity",
                "subject": "configuration",
            },
        }
    )
    if revoke_lease:
        with pytest.raises(HTTPException) as error:
            await export_observation(c.guard, body, c.prepared.claims)
        assert error.value.status_code == 403
    else:
        # With real job authority accepted, receipt selection still fails closed.
        with pytest.raises(ObservationError, match="not_found") as error:
            await export_observation(c.guard, body, c.prepared.claims)
        assert error.value.status == 404
