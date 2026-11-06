"""An independently cancelled workflow must not strand its exited assignment."""

import json
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration import operations_cancelled_release as recovery
from src.orchestration.accepted_node import operational_revision_inputs
from src.orchestration.deployment_manifest import EntryStatus, PhysicalTarget, TargetEvidence
from src.orchestration.developer_recovery import recover_developer
from src.orchestration.environment_leases import LeaseHolder, acquire_lease, foundation_release_scope
from src.orchestration.execution_policy import Action
from src.orchestration.models import OrchestrationAction, OrchestrationDecision, OrchestrationEnvironmentLease
from src.orchestration.results import observe_results
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.run_reports import OrchestrationRunReport
from tests.orchestration.test_operations_recovery import engine, exited, reporting, session, session_factory, work  # noqa: F401

ACCOUNT = "000000000000"
PROJECT = "adp-tools-dev-adapters-build"
SOURCE, DEFINITION, DISPATCH = "a" * 40, "b" * 40, "c" * 40


def build(*, start, end=None, status="SUCCEEDED", identifier="11111111-1111-1111-1111-111111111111"):
    build_id = PROJECT + ":" + identifier
    return {
        "id": build_id,
        "arn": f"arn:aws:codebuild:us-east-1:{ACCOUNT}:build/{build_id}",
        "projectName": PROJECT,
        "startTime": start,
        "endTime": end or start + timedelta(seconds=30),
        "buildStatus": status,
        "buildComplete": status != "IN_PROGRESS",
    }


class Builds:
    def __init__(self, rows):
        self.rows, self.calls = rows, []
        self.next_token = None
        self.missing = False

    def client(self, name, **kwargs):
        assert name == "codebuild"
        return self

    def list_builds_for_project(self, **kwargs):
        self.calls.append(("list", kwargs))
        assert kwargs["projectName"] == PROJECT and kwargs["sortOrder"] == "DESCENDING"
        return {"ids": [b["id"] for b in self.rows], **({"nextToken": self.next_token} if self.next_token else {})}

    def batch_get_builds(self, **kwargs):
        self.calls.append(("get", kwargs))
        assert kwargs["ids"] == [b["id"] for b in self.rows]
        return {"builds": [] if self.missing else self.rows, "buildsNotFound": kwargs["ids"] if self.missing else []}


@pytest.fixture
async def cancelled(session, work, monkeypatch):  # noqa: F811
    claim, execution = await exited(session, work, 1)
    observed = await observe_results(session)
    assert observed.advanced == 1 and work.node.state == "failed" and execution.status == "concluded"
    report = await session.get(OrchestrationRunReport, claim.claim_event_id)
    work.policy.allowed_actions.add(Action.DEPLOY)
    work.policy.org_id, work.policy.principal_id = work.node.org_id, "human"
    work.policy.environment_connection_ids = {"connection"}
    work.contract.environment_connection_ids = {"connection"}
    work.contract.node.evidence_expectations = ["runtime_verified:tool-adapters"]
    physical = PhysicalTarget("aws", ACCOUNT, "us-east-1", "eks-namespace", "cluster/adapters", TargetEvidence("test", datetime.now(UTC).isoformat()))
    entry = SimpleNamespace(
        entry_id="adapter-foundation",
        status=EntryStatus.ENABLED,
        docs_only=False,
        covers=lambda component: component == "tool-adapters",
        release_group="atlassian-foundation",
        connection_id="connection",
        mutation_role_arn=f"arn:aws:iam::{ACCOUNT}:role/adp-dev-trusted-deployment",
        resource_kind=physical.resource_kind,
        resource_id=physical.resource_id,
        artifact_revision=SOURCE,
        workflow=SimpleNamespace(path=".github/workflows/tools-platform-deploy.yml", definition_revision=DEFINITION),
    )
    accepted = SimpleNamespace(target=entry.entry_id, component="tool-adapters", repository=report.repo, workflows=["tools-platform-deploy.yml"])
    work.contract.node.operational_release_binding = accepted
    metadata = deepcopy(report.dispatch_metadata)
    metadata["orchestration"]["release_binding"] = {
        "target": entry.entry_id,
        "connection_id": entry.connection_id,
        "mutation_role_arn": entry.mutation_role_arn,
        "resource_kind": entry.resource_kind,
        "resource_id": entry.resource_id,
        "manifest_artifact_revision": SOURCE,
        "manifest_definition_revision": DEFINITION,
        "workflow": entry.workflow.path,
    }
    report.dispatch_metadata = metadata
    shared, owner = foundation_release_scope(
        node=work.node, flow=work.flow, entry=entry, component="tool-adapters", target=physical, environment="dev", source_revision=SOURCE
    )
    action = OrchestrationAction(
        org_id=work.node.org_id,
        execution_id=execution.id,
        kind="operations_native_release_dispatch",
        operation_key="native-release:test",
        status="prepared",
        attempt=1,
        detail={
            "dispatch_started": True,
            "correlation": report.run_id,
            "source_revision": SOURCE,
            "definition_revision": DEFINITION,
            "dispatch_revision": DISPATCH,
            "workflow": "tools-platform-deploy.yml",
            "repo": report.repo,
            "provider_repository_id": report.provider_repository_id,
            "branch": "main",
            "release_job": {"run_id": "123", "run_attempt": "1"},
            "shared_release": {
                "target": asdict(physical),
                "target_key": shared.canonical_key,
                "owner": owner,
                "generation": claim.generation,
                "manifest_entry_id": entry.entry_id,
                "environment": "dev",
                "source_revision": SOURCE,
                "mutation_role_arn": entry.mutation_role_arn,
            },
        },
    )
    session.add(action)
    lease_result = await acquire_lease(
        session, target=shared, holder=LeaseHolder(work.node.org_id, owner, claim.generation), manifest_entry_id=entry.entry_id, release_ref=SOURCE
    )
    assert lease_result.applied
    lease = await session.scalar(
        select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == shared.canonical_key)
    )
    started = datetime.now(UTC) - timedelta(minutes=5)
    run = {"id": 123, "conclusion": "cancelled", "updated_at": (started + timedelta(minutes=1)).isoformat()}
    jobs = [
        {"id": index, "name": name, "run_id": 123, "run_attempt": 1, "head_sha": DISPATCH, "status": "completed", "conclusion": conclusion}
        for index, (name, conclusion) in enumerate(
            (
                ("guard", "success"),
                ("adapter-foundation", "cancelled"),
                ("release", "skipped"),
                ("drift", "skipped"),
                ("adapter-handoff", "skipped"),
                ("adapter-identity", "skipped"),
            ),
            start=1,
        )
    ]
    jobs[1]["steps"] = [
        {"number": number, "name": name, "status": "completed", "conclusion": conclusion}
        for number, name, conclusion in (
            (1, "Verify source belongs to reviewed main history", "success"),
            (2, "Publish qualified immutable image under current native lease", "cancelled"),
            (3, "Apply standalone adapter in its isolated namespace", "skipped"),
            (4, "Independently observe deployed adapter runtime", "skipped"),
            (5, "Remove private stage inputs", "success"),
        )
    ]
    guard = SimpleNamespace(
        verify_terminal_run=AsyncMock(return_value=(run, started)),
        provider=SimpleNamespace(pages=AsyncMock(return_value=jobs)),
    )
    builds = Builds([build(start=started - timedelta(hours=1))])

    async def resolve(database, **kwargs):
        assert kwargs["entry"] == entry and kwargs["policy"] == work.policy
        proof = kwargs["inspect_target"](builds, {}, "adapters")
        return SimpleNamespace(
            physical=physical, credential_id=entry.connection_id, role_arn=f"arn:aws:iam::{ACCOUNT}:role/verifier", observation=proof
        )

    resolver = SimpleNamespace(resolve=AsyncMock(side_effect=resolve))
    monkeypatch.setattr(recovery, "NativeReleaseJob", lambda _: guard)
    monkeypatch.setattr(recovery, "DeploymentTargetResolver", lambda: resolver)
    monkeypatch.setattr(recovery, "load_packaged_manifest", lambda: SimpleNamespace(entry=lambda name: entry if name == entry.entry_id else None))
    monkeypatch.setattr(recovery, "accepted_release_pins", AsyncMock(return_value=operational_revision_inputs(work.contract)))
    monkeypatch.setattr(recovery, "resolve_root_user_entity_id", AsyncMock(return_value="human"))
    monkeypatch.setattr(recovery, "is_workflow_member", AsyncMock(return_value=True))
    await session.commit()
    return SimpleNamespace(
        work=work,
        claim=claim,
        execution=execution,
        report=report,
        action=action,
        lease=lease,
        guard=guard,
        builds=builds,
        jobs=jobs,
        run=run,
        resolver=resolver,
        started=started,
    )


async def recover(database, fixture):
    return await recover_developer(database, fixture.work.node, now=datetime.now(UTC) + timedelta(minutes=40))


async def test_cancelled_workflow_releases_only_failed_effect_and_normal_retry_preserves_attempt(session, cancelled):  # noqa: F811
    old_detail, old_report = deepcopy(cancelled.action.detail), deepcopy(cancelled.report.terminal_receipt)
    assert await recover(session, cancelled) == "scheduled"
    assert cancelled.work.node.state == "ready" and cancelled.work.node.attempts == 1
    assert cancelled.execution.status == "concluded" and cancelled.claim.state == "released"
    assert cancelled.action.status == "failed" and cancelled.lease.state == "free"
    assert cancelled.lease.release_reason == "failed"
    assert cancelled.action.detail["cancellation_recovery"]["runtime_acceptance"] is False
    restriction = json.loads(cancelled.resolver.resolve.await_args.kwargs["session_policy"])
    assert restriction == {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "sts:GetCallerIdentity", "Resource": "*"},
            {"Effect": "Allow", "Action": "eks:DescribeCluster", "Resource": f"arn:aws:eks:us-east-1:{ACCOUNT}:cluster/cluster"},
            {
                "Effect": "Allow",
                "Action": ["codebuild:ListBuildsForProject", "codebuild:BatchGetBuilds"],
                "Resource": f"arn:aws:codebuild:us-east-1:{ACCOUNT}:project/{PROJECT}",
            },
        ],
    }
    assert {k: cancelled.action.detail[k] for k in old_detail} == old_detail
    assert cancelled.report.terminal_receipt == old_report
    await session.commit()
    assert await recover(session, cancelled) == "not_a_failed_developer"
    decisions = (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == recovery.KIND))).all()
    assert len(decisions) == 1 and json.loads(decisions[0].reason)["outcome"] == "cancelled"
    assert cancelled.action.detail["cancellation_recovery"]["workflow_revision"] == DISPATCH


async def test_cancellation_before_publisher_starts_can_retry(session, cancelled):  # noqa: F811
    cancelled.jobs[1]["steps"][1]["conclusion"] = "skipped"
    cancelled.builds.rows = []
    assert await recover(session, cancelled) == "scheduled"
    assert cancelled.action.status == "failed" and cancelled.lease.state == "free"


def unstarted_dispatch(fixture):
    fixture.action.detail = {key: value for key, value in fixture.action.detail.items() if key != "release_job"}
    fixture.action.created_at = fixture.started - timedelta(seconds=1)
    fixture.jobs[1]["steps"] = []
    listing = [{"id": 123, "run_attempt": 1, "display_title": "ADP deployment " + fixture.report.run_id}]

    async def pages(binding, path, key, **params):
        if key == "workflow_runs":
            assert path.endswith("/actions/workflows/tools-platform-deploy.yml/runs")
            assert params["event"] == "workflow_dispatch" and params["created"].startswith(">=")
            return listing
        assert key == "jobs"
        return fixture.jobs

    fixture.guard.provider.pages.side_effect = pages
    return listing


async def test_cancelled_before_job_attestation_discovers_and_verifies_original_dispatch(session, cancelled):  # noqa: F811
    unstarted_dispatch(cancelled)
    original = deepcopy(cancelled.action.detail)
    assert await recover(session, cancelled) == "scheduled"
    assert cancelled.action.status == "failed" and cancelled.lease.state == "free"
    assert "release_job" not in cancelled.action.detail
    assert {key: cancelled.action.detail[key] for key in original} == original
    assert cancelled.action.receipt_ref.endswith("/runs/123/attempts/1:cancelled")
    assert cancelled.guard.verify_terminal_run.await_count == 2
    for call in cancelled.guard.verify_terminal_run.await_args_list:
        assert call.args[1]["release_job"] == {"run_id": "123", "run_attempt": "1"}
    assert cancelled.action.detail["cancellation_recovery"]["runtime_acceptance"] is False


@pytest.mark.parametrize("case", ["missing", "duplicate", "rerun", "invalid_id", "wrong_correlation", "old_run", "unverified_actor"])
async def test_unattested_dispatch_requires_unique_verified_first_attempt(session, cancelled, case):  # noqa: F811
    listing = unstarted_dispatch(cancelled)
    if case == "missing":
        listing.clear()
    elif case == "duplicate":
        listing.append({**listing[0], "id": 456})
    elif case == "rerun":
        listing[0]["run_attempt"] = 2
    elif case == "invalid_id":
        listing[0]["id"] = True
    elif case == "wrong_correlation":
        listing[0]["display_title"] = "ADP deployment unrelated"
    elif case == "old_run":
        cancelled.action.created_at = cancelled.started + timedelta(seconds=2)
    else:
        from fastapi import HTTPException

        cancelled.guard.verify_terminal_run.side_effect = HTTPException(403, "operations_release_job_refused")
    assert await recover(session, cancelled) != "scheduled"
    assert cancelled.action.status == "prepared" and cancelled.lease.state == "held"
    assert cancelled.work.node.state == "failed" and cancelled.action.receipt_ref is None


def failed_apply(fixture):
    fixture.run["conclusion"] = fixture.jobs[1]["conclusion"] = "failure"
    fixture.jobs[1]["steps"] = [
        {"number": number, "name": name, "status": "completed", "conclusion": conclusion}
        for number, name, conclusion in (
            (1, "Verify source belongs to reviewed main history", "success"),
            (2, "Publish qualified immutable image under current native lease", "success"),
            (3, "Bind published image to reviewed foundation generations", "success"),
            (4, "Apply standalone adapter in its isolated namespace", "failure"),
            (5, "Independently observe deployed adapter runtime", "skipped"),
            (6, "Qualify exact deployed images with isolated generated foundation data", "skipped"),
            (7, "Remove private stage inputs", "success"),
        )
    ]
    fixture.builds.rows = [build(start=fixture.started + timedelta(seconds=10))]


async def test_failed_apply_retries_without_claiming_runtime_acceptance(session, cancelled):  # noqa: F811
    failed_apply(cancelled)
    original = deepcopy(cancelled.action.detail)
    assert await recover(session, cancelled) == "scheduled"
    assert cancelled.work.node.state == "ready" and cancelled.work.node.attempts == 1
    assert cancelled.execution.status == "concluded" and cancelled.claim.state == "released"
    assert cancelled.action.status == "failed" and cancelled.lease.state == "free"
    assert {key: cancelled.action.detail[key] for key in original} == original
    assert cancelled.action.detail["failure_recovery"]["runtime_acceptance"] is False
    receipts = (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == recovery.FAILURE_KIND))).all()
    assert len(receipts) == 1 and json.loads(receipts[0].reason)["outcome"] == "failure"
    assert len(recovery.FAILURE_KIND) <= OrchestrationDecision.__table__.c.kind.type.length


@pytest.mark.parametrize("block", [None, "no_resume", "active_job", "active_build", "pending_publication", "revoked_member"])
async def test_resumed_failure_uses_terminal_reconciliation_without_changing_attempt(session, cancelled, monkeypatch, block):  # noqa: F811
    from src.orchestration.controls import ResumeRequest, resume_node

    failed_apply(cancelled)
    if block == "no_resume":
        cancelled.work.node.state = "ready"
    else:
        resumed = await resume_node(
            cancelled.work.node.id,
            ResumeRequest(reason="Retry the observed terminal deployment failure."),
            SimpleNamespace(org_id=cancelled.work.node.org_id, user_id="human"),
            SimpleNamespace(check_permission=AsyncMock(), get_user_role=AsyncMock(return_value=[SimpleNamespace(value="org_admin")])),
            session,
            None,
        )
        assert resumed.state == "ready"
    if block == "active_job":
        cancelled.jobs[1]["status"] = "in_progress"
    elif block == "active_build":
        cancelled.builds.rows = [build(start=cancelled.started + timedelta(seconds=10), status="IN_PROGRESS")]
    elif block == "pending_publication":
        cancelled.lease.publication_pending_digest = "sha256:" + "d" * 64
    elif block == "revoked_member":
        monkeypatch.setattr(recovery, "is_workflow_member", AsyncMock(return_value=False))
    await session.commit()
    original, terminal = deepcopy(cancelled.action.detail), deepcopy(cancelled.report.terminal_receipt)
    outcome = await recovery.reconcile_cancelled_release(
        session,
        node=cancelled.work.node,
        flow=cancelled.work.flow,
        execution=cancelled.execution,
        report=cancelled.report,
        contract=cancelled.work.contract,
        policy=cancelled.work.policy,
    )
    assert cancelled.work.node.state == "ready" and cancelled.work.node.attempts == 1
    assert cancelled.execution.status == "concluded" and cancelled.claim.state == "released"
    assert cancelled.report.terminal_receipt == terminal
    if block is None:
        assert outcome == "reconciled"
        assert cancelled.action.status == "failed" and cancelled.lease.state == "free"
        assert {key: cancelled.action.detail[key] for key in original} == original
        assert cancelled.action.detail["failure_recovery"]["runtime_acceptance"] is False
    else:
        assert outcome != "reconciled"
        assert cancelled.action.status == "prepared" and cancelled.lease.state == "held"
        assert cancelled.action.detail == original and cancelled.action.receipt_ref is None


@pytest.mark.parametrize("authority", ["human_gate", "not_granted"])
async def test_terminal_failure_settles_before_repair_approval_without_retrying(session, cancelled, authority):  # noqa: F811
    from src.orchestration.shared_amendment import history, quiescence_failures

    failed_apply(cancelled)
    if authority == "human_gate":
        cancelled.work.policy.human_gates.add(Action.REPAIR)
    else:
        cancelled.work.policy.allowed_actions.remove(Action.REPAIR)
    original = deepcopy(cancelled.action.detail)
    terminal = deepcopy(cancelled.report.terminal_receipt)
    assert "unsettled_action" in quiescence_failures(await history(session, flow=cancelled.work.flow))

    assert await recover(session, cancelled) == "repair_requires_approval"
    await session.commit()
    assert cancelled.work.node.state == "failed" and cancelled.work.node.attempts == 1
    assert cancelled.execution.status == "concluded" and cancelled.claim.state == "released"
    assert cancelled.action.status == "failed" and cancelled.lease.state == "free"
    assert cancelled.action.detail["failure_recovery"]["runtime_acceptance"] is False
    assert {key: cancelled.action.detail[key] for key in original} == original
    assert cancelled.report.terminal_receipt == terminal
    assert "unsettled_action" not in quiescence_failures(await history(session, flow=cancelled.work.flow))
    assert await recover(session, cancelled) == "repair_requires_approval"
    assert cancelled.work.node.state == "failed"
    assert await session.scalar(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == "operations_retry_scheduled")) is None

    # The owner can approve repair after the old effect has settled. Only then
    # does ordinary retry admission make this node eligible, preserving history.
    cancelled.work.policy.human_gates.clear()
    cancelled.work.policy.allowed_actions.add(Action.REPAIR)
    assert await recover(session, cancelled) == "scheduled"
    assert cancelled.work.node.state == "ready" and cancelled.work.node.attempts == 1
    receipts = (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == recovery.FAILURE_KIND))).all()
    assert len(receipts) == 1


@pytest.mark.parametrize("stage", [1, 2, 3, 5])
async def test_terminal_failure_at_any_stage_can_retry_without_runtime_acceptance(session, cancelled, stage):  # noqa: F811
    failed_apply(cancelled)
    for index, step in enumerate(cancelled.jobs[1]["steps"]):
        step["conclusion"] = "success" if index < stage else "failure" if index == stage else "skipped"
    assert await recover(session, cancelled) == "scheduled"
    assert cancelled.action.status == "failed" and cancelled.lease.state == "free"
    assert cancelled.action.detail["failure_recovery"]["runtime_acceptance"] is False


@pytest.mark.parametrize("case", ["unsettled_publication", "active_build", "outcome_changed", "successful_run"])
@pytest.mark.parametrize("repair_gated", [False, True])
async def test_failed_release_with_uncertain_effects_keeps_lease(session, cancelled, case, repair_gated):  # noqa: F811
    failed_apply(cancelled)
    if repair_gated:
        cancelled.work.policy.human_gates.add(Action.REPAIR)
    if case == "unsettled_publication":
        cancelled.lease.publication_pending_digest = "sha256:" + "d" * 64
    elif case == "active_build":
        cancelled.builds.rows[0].update(buildStatus="IN_PROGRESS", buildComplete=False)
    elif case == "outcome_changed":
        cancelled.guard.verify_terminal_run.side_effect = [
            (cancelled.run, cancelled.started),
            ({**cancelled.run, "conclusion": "success"}, cancelled.started),
        ]
    else:
        cancelled.run["conclusion"] = "success"
    await session.commit()
    assert await recover(session, cancelled) != "scheduled"
    assert cancelled.action.status == "prepared" and cancelled.action.receipt_ref is None
    assert cancelled.lease.state == "held" and cancelled.lease.released_at is None
    assert cancelled.work.node.state == "failed" and cancelled.work.node.attempts == 1


@pytest.mark.parametrize(
    "case", ["empty", "duplicate", "active", "foreign_run", "rerun", "wrong_revision", "missing_outcome", "malformed", "invalid_id"]
)
async def test_unverified_jobs_keep_reservation(session, cancelled, case):  # noqa: F811
    if case == "empty":
        cancelled.jobs.clear()
    elif case == "duplicate":
        cancelled.jobs.append(deepcopy(cancelled.jobs[0]))
    elif case == "malformed":
        cancelled.jobs.append(None)
    else:
        key, value = {
            "active": ("status", "in_progress"),
            "foreign_run": ("run_id", 124),
            "rerun": ("run_attempt", 2),
            "wrong_revision": ("head_sha", "d" * 40),
            "missing_outcome": ("conclusion", None),
            "invalid_id": ("id", True),
        }[case]
        cancelled.jobs[0][key] = value
    assert await recover(session, cancelled) == "native_cancel_recovery_jobs_unverified"
    assert cancelled.action.status == "prepared" and cancelled.lease.state == "held"


async def test_job_names_and_steps_are_not_engine_acceptance_rules(session, cancelled):  # noqa: F811
    failed_apply(cancelled)
    for index, job in enumerate(cancelled.jobs):
        job["name"] = f"deployment matrix / target {index}"
        job.pop("steps", None)
    assert await recover(session, cancelled) == "scheduled"
    assert cancelled.action.detail["failure_recovery"]["runtime_acceptance"] is False


async def test_missing_remote_evidence_never_releases_a_lock(session, cancelled):  # noqa: F811
    cancelled.resolver.resolve.side_effect = RuntimeError("private provider failure")
    assert await recover(session, cancelled) == "native_cancel_recovery_evidence_unavailable"
    assert cancelled.action.status == "prepared" and cancelled.lease.state == "held"


async def test_failure_after_lease_update_rolls_back_the_whole_reconciliation(session, cancelled, monkeypatch):  # noqa: F811
    action_id, lease_id = cancelled.action.id, cancelled.lease.id
    original = recovery.release_lease

    async def interrupted(*args, **kwargs):
        result = await original(*args, **kwargs)
        assert result.applied
        await session.flush()
        raise RuntimeError("interrupted before recording external outcome")

    monkeypatch.setattr(recovery, "release_lease", interrupted)
    assert await recover(session, cancelled) == "native_cancel_recovery_evidence_unavailable"
    await session.commit()
    action = await session.get(OrchestrationAction, action_id, populate_existing=True)
    lease = await session.get(OrchestrationEnvironmentLease, lease_id, populate_existing=True)
    assert action.status == "prepared" and action.receipt_ref is None
    assert lease.state == "held" and lease.released_at is None


@pytest.mark.parametrize(
    "case",
    [
        "active_build",
        "missing_build",
        "pending_publication",
        "other_owner",
        "new_generation",
        "foreign_target",
        "changed_run",
        "revoked_member",
        "duplicate_action",
        "paused",
        "changed_contract",
    ],
)
async def test_uncertain_or_changed_evidence_preserves_action_and_lock(session, cancelled, monkeypatch, case):  # noqa: F811
    if case == "active_build":
        cancelled.builds.rows = [build(start=cancelled.started + timedelta(seconds=10), status="IN_PROGRESS")]
    elif case == "missing_build":
        cancelled.builds.missing = True
    elif case == "pending_publication":
        cancelled.lease.publication_pending_digest = "sha256:" + "d" * 64
    elif case == "other_owner":
        cancelled.lease.owner_action_id = "another-flow"
    elif case == "new_generation":
        cancelled.lease.owner_generation += 1
    elif case == "foreign_target":
        cancelled.lease.release_ref = "e" * 40
    elif case == "changed_run":
        cancelled.guard.verify_terminal_run.side_effect = [
            (cancelled.run, cancelled.started),
            ({**cancelled.run, "updated_at": "different"}, cancelled.started),
        ]
    elif case == "revoked_member":
        monkeypatch.setattr(recovery, "is_workflow_member", AsyncMock(return_value=False))
    elif case == "duplicate_action":
        session.add(
            OrchestrationAction(
                org_id=cancelled.work.node.org_id,
                execution_id=cancelled.execution.id,
                kind="operations_continuation_deploy",
                operation_key="another-dispatch",
                status="prepared",
                attempt=1,
                detail={},
            )
        )
    elif case == "paused":
        cancelled.work.flow.execution_paused = True
    else:
        cancelled.work.contract.node.operational_release_binding.component = "gateway-backend"
    await session.commit()
    outcome = await recover(session, cancelled)
    assert outcome != "scheduled", outcome
    assert cancelled.work.node.state == "failed" and cancelled.work.node.attempts == 1
    assert cancelled.action.status == "prepared" and cancelled.action.receipt_ref is None
    assert cancelled.lease.state == "held" and cancelled.lease.released_at is None
    assert not (await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == recovery.KIND))).all()


@pytest.mark.parametrize("case", ["active", "missing", "duplicate", "foreign_arn", "unordered", "empty_page", "repeated_page"])
def test_incomplete_build_history_does_not_prove_quiescence(case):
    started = datetime.now(UTC) - timedelta(minutes=10)
    client = Builds([build(start=started + timedelta(minutes=1))])
    if case == "active":
        client.rows[0].update(buildStatus="IN_PROGRESS", buildComplete=False)
    elif case == "missing":
        client.missing = True
    elif case == "duplicate":
        client.rows *= 2
    elif case == "foreign_arn":
        client.rows[0]["arn"] = "another-account"
    elif case == "unordered":
        client.rows.append(build(start=started + timedelta(minutes=2), identifier="22222222-2222-2222-2222-222222222222"))
    elif case == "empty_page":
        client.rows, client.next_token = [], "more"
    else:
        client.next_token = "more"
    with pytest.raises(CycleBlockedError):
        recovery.read_builds(client, account=ACCOUNT, region="us-east-1", environment="dev", started_at=started)


def test_cancelled_actions_with_terminal_remote_build_can_be_reconciled():
    started = datetime.now(UTC) - timedelta(minutes=10)
    client = Builds([build(start=started + timedelta(minutes=1), status="STOPPED")])
    proof = recovery.read_builds(client, account=ACCOUNT, region="us-east-1", environment="dev", started_at=started)
    assert proof[0]["status"] == "STOPPED"
    assert [c[0] for c in client.calls] == ["list", "get"]
