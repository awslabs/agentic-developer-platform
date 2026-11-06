"""Reconcile a terminal native adapter run before retrying its exited worker.

Called under the normal retry path's node/flow/claim locks, after its policy,
lineage and terminal-report checks. This records a failed external effect; it
never grants runtime acceptance, revives a worker or dispatches a workflow.
"""

import asyncio
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from botocore.config import Config
from fastapi import HTTPException
from sqlalchemy import select

from src.shared.identity.resolver import resolve_root_user_entity_id
from src.shared.identity.workflow_access import is_workflow_member

from .deployment_manifest import EntryStatus, PhysicalTarget, TargetEvidence, load_packaged_manifest
from .deployment_target import DeploymentTargetResolver
from .environment_leases import LeaseHolder, ReleaseReason, foundation_release_scope, release_lease
from .execution_policy import Action
from .models import OrchestrationAction, OrchestrationDecision, OrchestrationEnvironmentLease, OrchestrationExecution
from .operations_release_job import NativeReleaseJob
from .operations_shared_release import accepted_release_pins
from .review_cycle import CycleBlockedError

KIND = "native_release_cancel_reconciled"
FAILURE_KIND = "native_release_fail_reconciled"
UNRESOLVED = {"prepared", "dispatched", "unknown"}


def require(value, code="scope_changed"):
    if not value:
        raise CycleBlockedError("native_cancel_recovery_" + code)


def read_builds(scoped, *, account, region, environment, started_at):
    """Prove the complete relevant project history contains no running build."""
    project = f"adp-tools-{environment}-adapters-build"
    client = scoped.client("codebuild", config=Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1}))
    token, seen, observed = None, set(), []
    previous = None
    for _ in range(5):
        page = client.list_builds_for_project(projectName=project, sortOrder="DESCENDING", **({"nextToken": token} if token else {}))
        ids = page.get("ids")
        require(isinstance(ids, list) and len(ids) <= 100 and all(isinstance(i, str) for i in ids), "build_history_unverified")
        require(len(set(ids)) == len(ids) and not seen.intersection(ids), "build_history_unverified")
        seen.update(ids)
        for build_id in ids:
            require(re.fullmatch(re.escape(project) + r":[0-9a-f-]{36}", build_id), "build_history_unverified")
        rows = client.batch_get_builds(ids=ids) if ids else {"builds": [], "buildsNotFound": []}
        builds = rows.get("builds")
        require(isinstance(builds, list) and all(isinstance(b, dict) for b in builds) and not rows.get("buildsNotFound"), "build_history_unverified")
        require(len(builds) == len(ids) and {b.get("id") for b in builds} == set(ids), "build_history_unverified")
        by_id = {b["id"]: b for b in builds}
        before_run = False
        for build_id in ids:
            build = by_id[build_id]
            start, end = build.get("startTime"), build.get("endTime")
            require(
                build.get("projectName") == project
                and build.get("arn") == f"arn:aws:codebuild:{region}:{account}:build/{build_id}"
                and isinstance(start, datetime)
                and start.tzinfo is not None
                and start <= datetime.now(UTC)
                and (previous is None or start <= previous),
                "build_history_unverified",
            )
            # A terminal Actions run does not stop an already dispatched build.
            require(
                build.get("buildComplete") is True and build.get("buildStatus") in {"SUCCEEDED", "FAILED", "FAULT", "STOPPED", "TIMED_OUT"},
                "build_still_active",
            )
            require(isinstance(end, datetime) and end.tzinfo is not None and start <= end <= datetime.now(UTC), "build_history_unverified")
            observed.append({"id": build_id, "status": build["buildStatus"], "ended_at": end.isoformat()})
            previous = start
            before_run |= start < started_at
        next_token = page.get("nextToken")
        require(next_token is None or isinstance(next_token, str) and next_token and next_token != token, "build_history_unverified")
        if not next_token or before_run:
            return observed
        require(ids, "build_history_unverified")
        token = next_token
    raise CycleBlockedError("native_cancel_recovery_build_history_limit")


async def verify_terminal_native_run(guard, binding, saved):
    run, started = await guard.verify_terminal_run(binding, saved)
    require(run["conclusion"] in {"cancelled", "failure"}, "run_not_recoverable")
    rows = await guard.provider.pages(binding, f"/repos/{binding.repo}/actions/runs/{run['id']}/attempts/1/jobs", "jobs")
    # The accepted dispatch pins the workflow implementation. Recovery records a
    # failed effect, not successful deployment: job names, step order and source
    # fingerprints are not acceptance criteria. Verify that every job in this
    # exact attempt has stopped; publication and remote-build checks below still
    # fence work that can outlive the workflow.
    require(isinstance(rows, list) and 0 < len(rows) <= 1000, "jobs_unverified")
    require(
        all(
            isinstance(job, dict)
            and type(job.get("id")) is int
            and job["id"] > 0
            and job.get("run_id") == run["id"]
            and job.get("run_attempt") == 1
            and job.get("status") == "completed"
            and job.get("head_sha") == saved["dispatch_revision"]
            and job.get("conclusion")
            in {"success", "failure", "cancelled", "skipped", "neutral", "timed_out", "action_required", "startup_failure", "stale"}
            for job in rows
        ),
        "jobs_unverified",
    )
    require(len({job["id"] for job in rows}) == len(rows), "jobs_unverified")
    return run, started


async def recovery_run_pins(guard, binding, action):
    """Find a dispatch cancelled before the job could save its attested run ID.

    Discovery is only a candidate for the normal terminal-run verification,
    including the dispatching App, repository, revision and first attempt.
    It neither changes the original intent nor supplies deployment acceptance.
    """
    saved = deepcopy(action.detail or {})
    if "release_job" in saved:
        return saved
    since = action.created_at
    require(isinstance(since, datetime), "dispatch_time_unverified")
    since = since.replace(tzinfo=UTC) if since.tzinfo is None else since
    require(since <= datetime.now(UTC), "dispatch_time_unverified")
    rows = await guard.provider.pages(
        binding,
        f"/repos/{binding.repo}/actions/workflows/{quote(saved['workflow'], safe='')}/runs",
        "workflow_runs",
        event="workflow_dispatch",
        created=">=" + since.replace(microsecond=0).isoformat(),
    )
    require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), "dispatch_unverified")
    matches = [row for row in rows if row.get("display_title") == "ADP deployment " + saved["correlation"]]
    require(len(matches) == 1, "dispatch_ambiguous")
    run = matches[0]
    require(type(run.get("id")) is int and run["id"] > 0 and run.get("run_attempt") == 1, "dispatch_unverified")
    return {**saved, "release_job": {"run_id": str(run["id"]), "run_attempt": "1"}}


async def reconcile_cancelled_release(session, *, node, flow, execution, report, contract, policy, guard=None, resolver=None):
    """Settle only a verified terminal failure, inside the retry transaction."""
    try:
        async with asyncio.timeout(40), session.begin_nested():
            return await _reconcile(
                session,
                node=node,
                flow=flow,
                execution=execution,
                report=report,
                contract=contract,
                policy=policy,
                guard=guard or NativeReleaseJob(None),
                resolver=resolver or DeploymentTargetResolver(),
            )
    except CycleBlockedError as error:
        return error.reason
    except (HTTPException, ValueError, KeyError, TypeError):
        return "native_cancel_recovery_evidence_unverified"
    except Exception:
        # Provider errors cannot release a reservation or leak credential details
        # into the public retry decision. The savepoint rolls back partial work.
        return "native_cancel_recovery_evidence_unavailable"


async def _reconcile(session, *, node, flow, execution, report, contract, policy, guard, resolver):
    require(node.state in {"failed", "ready"} and execution.status == execution.phase == "concluded" and execution.pending_action_key is None)
    if node.state == "ready":
        from .operations_resume_retry import retry_context

        resumed = await retry_context(session, node, node.attempts)
        require(
            resumed is not None and resumed["reason"] == "operations_runtime_unverified" and resumed["execution_id"] == execution.id,
            "resume_unverified",
        )
    require(flow.org_id == node.org_id == execution.org_id == report.org_id and flow.id == node.flow_id == execution.flow_id == report.flow_id)
    require(node.id == execution.node_id == report.node_id and node.attempts == execution.cycle == report.attempt)
    require(flow.state in {"pending", "running"} and not flow.execution_paused and flow.archived_at is None)
    require(policy.org_id == node.org_id and policy.expires_at > datetime.now(UTC))
    require({Action.OPERATE, Action.DEPLOY} <= set(policy.allowed_actions) and not {Action.OPERATE, Action.DEPLOY}.intersection(policy.human_gates))
    require((report.terminal_receipt or {}).get("outcome") == "complete" and (report.terminal_receipt or {}).get("run_id") == report.run_id)
    await session.get(OrchestrationExecution, execution.id, with_for_update=True, populate_existing=True)
    actions = (
        await session.scalars(
            select(OrchestrationAction)
            .where(OrchestrationAction.org_id == node.org_id, OrchestrationAction.execution_id == execution.id)
            .with_for_update()
            .limit(201)
        )
    ).all()
    outstanding = [a for a in actions if a.status in UNRESOLVED]
    require(len(actions) <= 200 and len(outstanding) == 1 and not any(a.kind == "review_cycle_dispatch" for a in actions))
    action = outstanding[0]
    require(action.kind == "operations_native_release_dispatch" and action.status in {"prepared", "unknown"} and action.receipt_ref is None)
    saved = deepcopy(action.detail or {})
    release = saved.get("shared_release") or {}
    accepted = getattr(contract.node, "operational_release_binding", None)
    entry = load_packaged_manifest().entry(release.get("manifest_entry_id"))
    recorded = ((report.dispatch_metadata or {}).get("orchestration") or {}).get("release_binding") or {}
    require(accepted is not None and entry is not None and entry.status is EntryStatus.ENABLED and not entry.docs_only)
    require(accepted.component == "tool-adapters" and accepted.target == entry.entry_id and accepted.repository == report.repo)
    require(saved.get("workflow") == "tools-platform-deploy.yml" and saved["workflow"] in accepted.workflows and release.get("environment") == "dev")
    require(
        saved.get("correlation") == report.run_id
        and saved.get("repo") == report.repo
        and saved.get("provider_repository_id") == report.provider_repository_id
    )
    require(
        not saved.get("retry_started_at")
        and not saved.get("controller_dispatch")
        and contract.node.evidence_expectations == ["runtime_verified:tool-adapters"]
    )
    require(entry.connection_id in contract.environment_connection_ids and entry.connection_id in policy.environment_connection_ids)
    require(
        entry.entry_id == recorded.get("target")
        and entry.connection_id == recorded.get("connection_id")
        and entry.mutation_role_arn == recorded.get("mutation_role_arn")
    )
    require(
        entry.resource_kind == recorded.get("resource_kind")
        and entry.resource_id == recorded.get("resource_id")
        and entry.artifact_revision == recorded.get("manifest_artifact_revision")
    )
    require(
        entry.workflow.path == recorded.get("workflow") == ".github/workflows/" + saved["workflow"]
        and entry.workflow.definition_revision == recorded.get("manifest_definition_revision")
    )
    pins = await accepted_release_pins(session, node=node, execution=execution, contract=contract, report=report)
    require(
        pins is not None
        and pins
        == {
            "default_branch": saved.get("branch"),
            "source_revision": saved.get("source_revision"),
            "definition_revision": saved.get("definition_revision"),
        }
    )
    raw = release["target"]
    physical = PhysicalTarget(**{**raw, "evidence": TargetEvidence(**raw["evidence"])})
    shared, owner = foundation_release_scope(
        node=node,
        flow=flow,
        entry=entry,
        component=accepted.component,
        target=physical,
        environment=release["environment"],
        source_revision=saved["source_revision"],
    )
    require((physical.resource_kind, physical.resource_id) == (entry.resource_kind, entry.resource_id))
    require(
        release.get("target_key") == shared.canonical_key
        and release.get("owner") == owner
        and release.get("source_revision") == saved["source_revision"]
        and release.get("mutation_role_arn") == entry.mutation_role_arn
    )
    require(type(release.get("generation")) is int and release["generation"] == execution.claim_generation)
    lease = await session.scalar(
        select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == shared.canonical_key).with_for_update()
    )
    require(
        lease is not None
        and lease.state == "held"
        and lease.owner_org_id == node.org_id
        and lease.owner_action_id == owner
        and lease.owner_generation == release["generation"],
        "owner_changed",
    )
    require(lease.manifest_entry_id == entry.entry_id and lease.release_ref == saved["source_revision"])
    require(lease.publication_pending_digest is None, "publication_unsettled")
    binding = SimpleNamespace(
        org_id=node.org_id, repo=report.repo, installation_id=report.installation_id, provider_repository_id=report.provider_repository_id
    )
    verification = await recovery_run_pins(guard, binding, action)
    run, started = await verify_terminal_native_run(guard, binding, verification)
    if "release_job" not in saved:
        dispatch_time = action.created_at.replace(tzinfo=UTC) if action.created_at.tzinfo is None else action.created_at
        require(started >= dispatch_time.replace(microsecond=0), "dispatch_time_unverified")
    principal = await resolve_root_user_entity_id(session, node.org_id, policy.principal_id)
    require(await is_workflow_member(session, user_id=principal, org_id=node.org_id), "membership_revoked")

    def inspect(scoped, description, namespace):
        return read_builds(scoped, account=physical.account_id, region=physical.region, environment=release["environment"], started_at=started)

    project_arn = f"arn:aws:codebuild:{physical.region}:{physical.account_id}:project/adp-tools-{release['environment']}-adapters-build"
    target = await resolver.resolve(
        session,
        entry=entry,
        policy=policy,
        principal_user_id=principal,
        execution_id=execution.id,
        inspect_target=inspect,
        session_policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {"Effect": "Allow", "Action": "sts:GetCallerIdentity", "Resource": "*"},
                    {
                        "Effect": "Allow",
                        "Action": "eks:DescribeCluster",
                        "Resource": f"arn:aws:eks:{physical.region}:{physical.account_id}:cluster/{entry.resource_id.split('/')[0]}",
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["codebuild:ListBuildsForProject", "codebuild:BatchGetBuilds"],
                        "Resource": project_arn,
                    },
                ],
            }
        ),
    )
    require(
        target.physical.canonical_key == physical.canonical_key
        and target.credential_id == entry.connection_id
        and target.role_arn != entry.mutation_role_arn,
        "target_changed",
    )
    require(isinstance(target.observation, list), "build_history_unverified")
    # A GitHub rerun between observations invalidates the old terminal outcome.
    current, _ = await guard.verify_terminal_run(binding, verification)
    require(current["conclusion"] == run["conclusion"] and current.get("updated_at") == run.get("updated_at"), "run_changed")
    evidence = f"github/{binding.repo}/actions/runs/{run['id']}/attempts/1:{run['conclusion']}"
    released = await release_lease(
        session,
        canonical_target_key=shared.canonical_key,
        holder=LeaseHolder(node.org_id, owner, release["generation"]),
        reason=ReleaseReason.FAILED,
        terminal_evidence=evidence,
    )
    require(released.applied, "owner_changed")
    observation = {
        "run_id": run["id"],
        "run_attempt": 1,
        "outcome": run["conclusion"],
        "runtime_acceptance": False,
        "builds": target.observation,
        "workflow_revision": saved["dispatch_revision"],
    }
    kind = KIND if run["conclusion"] == "cancelled" else FAILURE_KIND
    action.status, action.observed_at, action.receipt_ref = "failed", datetime.now(UTC), evidence
    action.detail = {**saved, "cancellation_recovery" if run["conclusion"] == "cancelled" else "failure_recovery": observation}
    session.add(
        OrchestrationDecision(
            id=str(uuid5(NAMESPACE_URL, kind + ":" + action.id)),
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind=kind,
            actor_id="system:operations-recovery",
            actor_role="engine",
            actor_kind="service",
            reason=json.dumps({"action_id": action.id, "attempt": node.attempts, "generation": release["generation"], **observation}),
        )
    )
    await session.flush()
    return "reconciled"
