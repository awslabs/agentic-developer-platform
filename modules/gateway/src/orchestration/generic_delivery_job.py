"""Authenticate a workflow job against its accepted generic delivery reservation.

This checks execution authority, provider identity and target ownership only.
Application deployment and immutable artifact readback remain in the workflow.
An authenticated publication observation is a journal entry, never acceptance.
"""

import re
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import select

from .accepted_node import accepted_node_contract
from .deployment_manifest import PhysicalTarget, TargetEvidence
from .dispatch import graph_address
from .environment_leases import shared_release_held
from .execution_policy import Action, CredentialScope, ResourceRef, authorize_action
from .execution_state import ExecutionIdentity
from .execution_store import _live_authority_conflict
from .models import OrchestrationAction, OrchestrationExecution, OrchestrationFlow, OrchestrationNode
from .operations_continuation_authority import assignment_matches
from .operations_release_job import require
from .operations_shared_release import accepted_release_pins
from .review_cycle import CycleBlockedError
from .run_reports import OrchestrationRunReport


async def snapshot(service, session, body, claims, *, expected_action=None, started_at=None):
    require(
        claims.get("event_name") == "workflow_dispatch"
        and isinstance(claims.get("run_id"), str)
        and re.fullmatch(r"[1-9][0-9]{0,18}", claims["run_id"])
        and isinstance(claims.get("run_attempt"), str)
        and re.fullmatch(r"[1-9][0-9]{0,5}", claims["run_attempt"])
    )
    actions = list(
        await session.scalars(
            select(OrchestrationAction)
            .where(
                OrchestrationAction.kind == "delivery_workflow_dispatch",
                OrchestrationAction.detail["correlation"].as_string() == body.correlation,
                OrchestrationAction.detail["contract_digest"].as_string() == body.contract_digest,
                OrchestrationAction.detail["repo"].as_string() == claims.get("repository"),
            )
            .limit(101)
        )
    )
    require(len(actions) <= 100)
    matches = [
        action
        for action in actions
        if not action.detail.get("terminal_run")
        and (
            action.detail.get("run_id") is None
            and claims["run_attempt"] == "1"
            or action.detail.get("run_id") == int(claims["run_id"])
            and action.detail.get("run_attempt") == int(claims["run_attempt"]) - 1
        )
    ]
    require(len(matches) == 1)
    candidate = matches[0]
    row = await session.get(OrchestrationExecution, candidate.execution_id)
    require(row is not None and row.org_id == candidate.org_id)
    node = await session.get(OrchestrationNode, row.node_id, with_for_update=started_at is not None, populate_existing=True)
    require(node is not None and node.org_id == row.org_id and node.flow_id == row.flow_id)
    identity = ExecutionIdentity(row.org_id, node.id, row.cycle, row.accepted_plan_version, row.claim_id, row.claim_generation)
    if started_at is not None:
        require(await _live_authority_conflict(session, identity, flow_id=row.flow_id, require_unarchived=True) is None)
        row = await session.get(OrchestrationExecution, row.id, with_for_update=True, populate_existing=True)
        candidate = await session.get(OrchestrationAction, candidate.id, with_for_update=True, populate_existing=True)
    require(row is not None and candidate is not None)
    saved = candidate.detail or {}
    require(
        candidate.kind == "delivery_workflow_dispatch"
        and candidate.org_id == row.org_id
        and candidate.execution_id == row.id
        and candidate.status in {"prepared", "succeeded", "unknown"}
        and (expected_action is None or candidate.id == expected_action)
        and not saved.get("terminal_run")
        and saved.get("correlation") == body.correlation
        and saved.get("source_revision") == body.source_revision
        and saved.get("dispatch_revision") == body.dispatch_revision
        and saved.get("contract_digest") == body.contract_digest
        and saved.get("workflow") == f".github/workflows/{body.workflow}"
        and saved.get("repo") == claims.get("repository")
        and str(saved.get("provider_repository_id")) == claims.get("repository_id")
        and saved.get("claim_id") == row.claim_id == identity.claim_id
        and saved.get("claim_generation") == row.claim_generation == identity.claim_generation
        and row.node_id == identity.node_id
        and row.cycle == identity.cycle
        and row.accepted_plan_version == identity.accepted_plan_version
        and saved.get("role_arn") == body.role_arn
        and claims.get("workflow_ref") == f"{saved.get('repo')}/{saved.get('workflow')}@refs/heads/{saved.get('branch')}"
        and claims.get("sha") == claims.get("workflow_sha") == body.dispatch_revision
        and claims.get("ref") == f"refs/heads/{saved.get('branch')}"
        and row.status in {"runnable", "awaiting_external"}
        and node.state == "running"
        and node.attempts == row.cycle
    )
    report = await session.get(OrchestrationRunReport, body.correlation)
    contract = await accepted_node_contract(session, node)
    release = getattr(getattr(contract, "node", None), "operational_release_binding", None)
    delivery = getattr(release, "delivery_contract", None)
    require(
        report is not None
        and contract is not None
        and release is not None
        and delivery is not None
        and contract.plan_version == row.accepted_plan_version
        and delivery.digest == body.contract_digest
        and delivery.job_mutation_role_arn == body.mutation_role_arn
        and release.repository == report.repo == saved["repo"]
        and report.provider_repository_id == saved["provider_repository_id"]
        and report.org_id == row.org_id
        and report.node_id == node.id
        and report.flow_id == row.flow_id
        and report.attempt == row.cycle
        and delivery.connection_id in contract.environment_connection_ids
        and release.workflows == [body.workflow]
        and contract.node.evidence_expectations == [f"runtime_verified:{release.component}"]
    )
    require(await assignment_matches(session, node=node, execution=row, report=report))
    pins = await accepted_release_pins(session, node=node, execution=row, contract=contract, report=report)
    require(
        pins
        == {"source_revision": body.source_revision, "definition_revision": saved.get("definition_revision"), "default_branch": saved.get("branch")}
    )
    flow = await session.get(OrchestrationFlow, row.flow_id, populate_existing=True)
    require(
        flow is not None
        and flow.org_id == row.org_id
        and flow.state in {"running", "pending"}
        and not flow.execution_paused
        and flow.archived_at is None
    )
    binding = SimpleNamespace(
        org_id=row.org_id, repo=report.repo, installation_id=report.installation_id, provider_repository_id=report.provider_repository_id
    )
    try:
        raw = saved["target"]
        physical = PhysicalTarget(**{**raw, "evidence": TargetEvidence(**raw["evidence"])})
    except (KeyError, ValueError, TypeError):
        require(False)
    require(
        (physical.resource_kind, physical.resource_id) == (delivery.resource_kind, delivery.resource_id)
        and body.mutation_role_arn.split(":")[4] == body.role_arn.split(":")[4] == physical.account_id
    )
    binding.target_identity = {
        "connection_id": delivery.connection_id,
        "resource_kind": physical.resource_kind,
        "resource_id": physical.resource_id,
        "account_id": physical.account_id,
        "region": physical.region,
    }
    pin = {"run_id": claims["run_id"], "run_attempt": claims["run_attempt"]}
    require(saved.get("release_job", pin) == pin)
    if started_at is not None:
        context = SimpleNamespace(identity=identity, execution=row)
        _, _, admission, principal, _, auth = await service.authority.authority_context(
            session, context, node, binding, body.correlation, Action.DEPLOY
        )
        require(admission.refusal is None and admission.policy is not None and admission.plan_version == contract.plan_version)
        entry = delivery.manifest(release).entry(release.target)
        scoped = False

        def authorize_scope(credential_id, role_arn):
            nonlocal scoped
            decision = authorize_action(
                replace(auth, credential_scope=CredentialScope.USER_GRANTED),
                Action.DEPLOY,
                ResourceRef(
                    repository_id=binding.repo,
                    environment_connection_id=entry.connection_id,
                    node_address=graph_address(node, flow_slug=flow.slug),
                    org_id=row.org_id,
                    user_credential_id=credential_id,
                    aws_role_arn=role_arn,
                ),
                contract.plan_version,
            )
            require(decision.permitted)
            scoped = True

        target = await service.targets.resolve(
            session, entry=entry, policy=admission.policy, principal_user_id=principal, execution_id=row.id, authorize_scope=authorize_scope
        )
        require(scoped and target.role_arn == body.role_arn and target.physical.canonical_key == physical.canonical_key)
        require(
            await shared_release_held(
                session,
                target=physical,
                org_id=row.org_id,
                action_id=f"delivery:{row.id}",
                release_ref=body.source_revision,
                manifest_entry_id=release.target,
                generation=row.claim_generation,
                started_at=started_at,
            )
        )
        candidate.detail = {**saved, "release_job": pin}
    return candidate, binding


async def authorize_job(service, body, claims, *, publication=None, include_target=False):
    try:
        async with service.factory() as session:
            candidate, binding = await snapshot(service, session, body, claims)
            action_id = candidate.id
        started_at = await service.verify_run(claims, binding, body)
        async with service.factory() as session:
            candidate, binding = await snapshot(service, session, body, claims, expected_action=action_id, started_at=started_at)
            if publication is not None:
                existing = candidate.detail.get("publication")
                require(
                    existing is None
                    or isinstance(existing, dict)
                    and existing.get("digest") == body.image_digest
                    and existing.get("state") in {"pending", "observed"}
                )
                require(publication != "observed" or existing is not None)
                # Replaying an acknowledgement cannot downgrade an observed digest.
                state = "observed" if existing and existing.get("state") == "observed" else publication
                candidate.detail = {
                    **candidate.detail,
                    "publication": {
                        "digest": body.image_digest,
                        "state": state,
                        "observed_at": datetime.now(UTC).isoformat(),
                        "run_id": claims["run_id"],
                        "run_attempt": claims["run_attempt"],
                    },
                }
            await session.commit()
            return binding.target_identity if include_target else None
    except CycleBlockedError:
        require(False)
