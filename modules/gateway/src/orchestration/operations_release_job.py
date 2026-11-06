"""Attest a native operations deployment job against its durable group owner."""

import asyncio
import re
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from src.knowledge.github_app_service import _mint_app_jwt, resolve_tenant_app_credentials
from src.shared.database import get_session_factory

from .accepted_node import accepted_node_contract
from .deployment_manifest import EntryStatus, PhysicalTarget, TargetEvidence, load_packaged_manifest
from .deployment_target import DeploymentTargetResolver
from .deployment_workflow_provider import WorkflowProvider
from .dispatch import graph_address
from .environment_leases import (
    LeaseError,
    LeaseHolder,
    begin_shared_publication,
    foundation_release_scope,
    record_shared_publication_absence,
    recover_shared_publication,
    rollforward_shared_publication,
    settle_shared_publication,
    shared_release_held,
)
from .execution_policy import Action, CredentialScope, ResourceRef, authorize_action
from .execution_state import ExecutionIdentity
from .execution_store import _live_authority_conflict
from .models import (
    OrchestrationAction,
    OrchestrationEnvironmentLease,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from .operations_continuation_authority import transfer
from .operations_publication_recovery import ImageAbsence, ImageReadback, read_pending_image
from .operations_shared_release import accepted_release_pins
from .policy_admission import load_in_force_policy
from .review_cycle import CycleBlockedError
from .review_cycle_dispatch import ReviewCycleServices
from .run_reports import OrchestrationRunReport

AUDIENCE = "adp-operations-release-job-v1"
router = APIRouter(prefix="/orchestration/operations-release-job", tags=["orchestration"])


async def github_claims(token: str, *, audience: str) -> dict:
    # Only HTTP callers need OIDC authentication. Scheduled recovery imports this
    # service without the web-login signing key or its middleware initialization.
    from src.agentauth.arc_model import github_claims as verify

    return await verify(token, audience=audience)


class NativeJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    correlation: str = Field(pattern=r"^orch:[0-9a-f-]{36}$")
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    dispatch_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    workflow: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml$")
    role_arn: str = Field(pattern=r"^arn:(?:aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$")
    mutation_role_arn: str = Field(pattern=r"^arn:(?:aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$")
    contract_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class NativePublicationRequest(NativeJobRequest):
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


def require(condition):
    if not condition:
        raise HTTPException(403, "operations_release_job_refused")


class NativeReleaseJob:
    def __init__(self, factory, provider=None, manifest_loader=load_packaged_manifest, authority=None, targets=None):
        self.factory = factory
        self.provider = provider or WorkflowProvider()
        self.manifest_loader = manifest_loader
        self.authority = authority or ReviewCycleServices(factory)
        self.targets = targets or DeploymentTargetResolver()

    async def snapshot(self, body, claims, *, action_id=None, started_at=None, publication_digest=None):
        for attempt in range(2):
            try:
                return await self._snapshot(body, claims, action_id=action_id, started_at=started_at, publication_digest=publication_digest)
            except DBAPIError as error:
                # PostgreSQL guarantees that a deadlock victim's transaction was
                # aborted. Reopen it once and recheck all current authority; an
                # unknown connection/commit outcome must never be replayed here.
                if attempt or getattr(error.orig, "sqlstate", None) != "40P01":
                    raise
                await asyncio.sleep(0.1)

    async def _snapshot(self, body, claims, *, action_id=None, started_at=None, publication_digest=None):
        require(claims.get("event_name") == "workflow_dispatch" and claims.get("run_attempt") == "1")
        async with self.factory() as session:
            query = (
                select(OrchestrationAction)
                .where(
                    OrchestrationAction.kind == "operations_native_release_dispatch",
                    OrchestrationAction.detail["correlation"].as_string() == body.correlation,
                    OrchestrationAction.detail["repo"].as_string() == claims.get("repository"),
                )
                .limit(2)
            )
            actions = (await session.scalars(query)).all()
            require(len(actions) == 1)
            action = actions[0]
            if started_at is not None:
                candidate = await session.get(OrchestrationExecution, action.execution_id)
                # Canonical lock order is node -> flow/claim/plan -> execution -> action, as
                # the result observer and the worker write path take it. Anchoring the node
                # FOR UPDATE before the authority read keeps this guard from inverting
                # against that observer; every binding below is still revalidated under the
                # same locks.
                node_anchor = (
                    await session.get(OrchestrationNode, candidate.node_id, with_for_update=True, populate_existing=True)
                    if candidate is not None
                    else None
                )
                require(
                    candidate is not None
                    and node_anchor is not None
                    and action.org_id == candidate.org_id == node_anchor.org_id
                    and candidate.node_id == node_anchor.id
                    and candidate.flow_id == node_anchor.flow_id
                )
                identity = ExecutionIdentity(
                    action.org_id, node_anchor.id, candidate.cycle, candidate.accepted_plan_version, candidate.claim_id, candidate.claim_generation
                )
                require(await _live_authority_conflict(session, identity, flow_id=candidate.flow_id) is None)
                execution = await session.get(OrchestrationExecution, candidate.id, with_for_update=True, populate_existing=True)
                require(
                    execution is not None
                    and execution.org_id == identity.org_id
                    and execution.node_id == identity.node_id
                    and execution.flow_id == candidate.flow_id
                    and execution.cycle == identity.cycle
                    and execution.accepted_plan_version == identity.accepted_plan_version
                    and execution.claim_id == identity.claim_id
                    and execution.claim_generation == identity.claim_generation
                )
                node = await session.get(OrchestrationNode, execution.node_id, with_for_update=True, populate_existing=True)
                action = await session.get(OrchestrationAction, action.id, with_for_update=True, populate_existing=True)
                require(
                    action is not None
                    and action.org_id == execution.org_id
                    and action.execution_id == execution.id
                    and action.kind == "operations_native_release_dispatch"
                )
            else:
                execution = await session.get(OrchestrationExecution, action.execution_id, populate_existing=True)
                node = await session.get(OrchestrationNode, execution.node_id, populate_existing=True) if execution is not None else None
            saved = action.detail or {}
            job_pin = {"run_id": claims.get("run_id"), "run_attempt": claims.get("run_attempt")}
            retry_started_at = saved.get("retry_started_at")
            service_dispatch = retry_started_at is not None or saved.get("controller_dispatch") is True
            require(
                saved.get("release_job", job_pin) == job_pin
                and (retry_started_at is None or saved.get("retry_previous_run_id") != claims.get("run_id"))
                and (action_id is None or action.id == action_id)
                and action.status in {"prepared", "succeeded", "unknown"}
                and saved.get("dispatch_started") is True
                and saved.get("correlation") == body.correlation
                and saved.get("source_revision") == body.source_revision
                and saved.get("dispatch_revision") == body.dispatch_revision
                and saved.get("workflow") == body.workflow
                and type(saved.get("provider_repository_id")) is int
                and str(saved["provider_repository_id"]) == claims.get("repository_id")
                and isinstance(saved.get("branch"), str)
                and claims.get("workflow_ref") == f"{saved['repo']}/.github/workflows/{body.workflow}@refs/heads/{saved['branch']}"
                and claims.get("sha") == body.dispatch_revision
                and claims.get("workflow_sha") == body.dispatch_revision
                and claims.get("ref") == f"refs/heads/{saved['branch']}",
            )
            require(
                execution is not None
                and execution.org_id == action.org_id
                and execution.status in {"runnable", "awaiting_external"}
                and (bool(execution.handoff_receipt_ref) if service_dispatch else execution.handoff_receipt_ref is None)
            )
            claim = await session.get(OrchestrationWorkClaim, execution.claim_id, populate_existing=True)
            report = await session.get(OrchestrationRunReport, body.correlation)
            require(
                node is not None
                and node.org_id == action.org_id
                and node.flow_id == execution.flow_id
                and node.state == "running"
                and report is not None
                and node.attempts == execution.cycle == report.attempt
            )
            require(
                claim is not None
                and claim.org_id == action.org_id
                and claim.id == execution.claim_id
                and claim.provider_repository_id == report.provider_repository_id
                and claim.state == "held"
                and claim.generation == execution.claim_generation == saved.get("shared_release", {}).get("generation")
                and claim.active_run_id == claim.claim_event_id == (execution.handoff_receipt_ref if service_dispatch else body.correlation)
                and claim.lease_expires_at is not None
                and claim.lease_expires_at.tzinfo is not None
                and claim.lease_expires_at > datetime.now(UTC)
                and report.org_id == action.org_id
                and report.flow_id == execution.flow_id
                and report.node_id == node.id
                and report.repo == saved["repo"]
                and report.provider_repository_id == saved["provider_repository_id"],
            )
            if service_dispatch:
                require(await transfer(session, node=node, execution=execution, report=report) is not None)
                if retry_started_at is not None:
                    try:
                        retry_at = datetime.fromisoformat(retry_started_at)
                    except (TypeError, ValueError):
                        require(False)
                    require(retry_at.tzinfo is not None and (started_at is None or started_at >= retry_at))
            contract = await accepted_node_contract(session, node)
            admission = await load_in_force_policy(session, org_id=action.org_id, flow_id=node.flow_id)
            recorded = ((report.dispatch_metadata or {}).get("orchestration") or {}).get("release_binding") or {}
            release = getattr(contract.node, "operational_release_binding", None) if contract else None
            entry = self.manifest_loader().entry(saved.get("shared_release", {}).get("manifest_entry_id"))
            pins = await accepted_release_pins(session, node=node, execution=execution, contract=contract, report=report)
            require(
                pins is not None
                and contract is not None
                and contract.plan_version == execution.accepted_plan_version == admission.plan_version
                and admission.refusal is None
                and admission.policy is not None
                and admission.policy.permits(Action.DEPLOY)
                and release is not None
                and entry is not None
                and entry.status is EntryStatus.ENABLED
                and not entry.docs_only
                and entry.release_group is not None
                and entry.entry_id == release.target == recorded.get("target")
                and entry.connection_id == recorded.get("connection_id")
                and entry.mutation_role_arn == body.mutation_role_arn
                and entry.mutation_role_arn == recorded.get("mutation_role_arn")
                and entry.mutation_role_arn == saved["shared_release"].get("mutation_role_arn")
                and entry.connection_id in contract.environment_connection_ids
                and entry.resource_kind == recorded.get("resource_kind")
                and entry.resource_id == recorded.get("resource_id")
                and entry.artifact_revision == recorded.get("manifest_artifact_revision")
                and entry.workflow is not None
                and entry.workflow.definition_revision == recorded.get("manifest_definition_revision")
                and entry.workflow.path == recorded.get("workflow")
                and entry.covers(release.component)
                and release.repository == saved["repo"]
                and body.workflow in release.workflows
                and entry.workflow.path == f".github/workflows/{body.workflow}"
                and pins["definition_revision"] == saved["definition_revision"]
                and pins["source_revision"] == body.source_revision
                and pins["default_branch"] == saved["branch"]
                and contract.node.evidence_expectations == [f"runtime_verified:{release.component}"],
            )
            try:
                raw = saved["shared_release"]["target"]
                physical = PhysicalTarget(**{**raw, "evidence": TargetEvidence(**raw["evidence"])})
                flow = await session.get(OrchestrationFlow, node.flow_id)
                require(flow is not None and flow.org_id == action.org_id)
                shared, owner = foundation_release_scope(
                    node=node,
                    flow=flow,
                    entry=entry,
                    component=release.component,
                    target=physical,
                    environment=saved["shared_release"]["environment"],
                    source_revision=body.source_revision,
                )
            except (KeyError, ValueError, TypeError, LeaseError):
                require(False)
            require(
                flow is not None
                and flow.org_id == action.org_id
                and flow.state in {"pending", "running"}
                and not flow.execution_paused
                and flow.archived_at is None
                and (physical.resource_kind, physical.resource_id) == (entry.resource_kind, entry.resource_id)
                and body.mutation_role_arn.split(":")[4] == physical.account_id
                and body.role_arn.split(":")[4] == physical.account_id
                and (shared.canonical_key, owner) == (saved["shared_release"]["target_key"], saved["shared_release"]["owner"]),
            )
            if started_at is not None:
                identity = ExecutionIdentity(action.org_id, node.id, node.attempts, execution.accepted_plan_version, claim.id, claim.generation)
                source = SimpleNamespace(identity=identity, execution=execution)
                binding = SimpleNamespace(
                    org_id=action.org_id,
                    repo=report.repo,
                    installation_id=report.installation_id,
                    provider_repository_id=report.provider_repository_id,
                )
                try:
                    _, _, current, principal, _, authorization = await self.authority.authority_context(
                        session, source, node, binding, body.correlation, Action.DEPLOY
                    )
                    require(current.policy is not None and current.plan_version == execution.accepted_plan_version)
                    scope_checked = False

                    def authorize_scope(credential_id, role_arn):
                        nonlocal scope_checked
                        require(bool(credential_id) and bool(role_arn))
                        decision = authorize_action(
                            replace(authorization, credential_scope=CredentialScope.USER_GRANTED),
                            Action.DEPLOY,
                            ResourceRef(
                                repository_id=binding.repo,
                                environment_connection_id=entry.connection_id,
                                node_address=graph_address(node, flow_slug=flow.slug),
                                org_id=action.org_id,
                                user_credential_id=credential_id,
                                aws_role_arn=role_arn,
                            ),
                            execution.accepted_plan_version,
                        )
                        require(decision.permitted)
                        scope_checked = True

                    def inspect_publication(scoped, description, scope):
                        return read_pending_image(
                            scoped,
                            account_id=physical.account_id,
                            region=physical.region,
                            environment=saved["shared_release"]["environment"],
                            source_revision=body.source_revision,
                            image_digest=publication_digest,
                        )

                    target = await self.targets.resolve(
                        session,
                        entry=entry,
                        policy=current.policy,
                        principal_user_id=principal,
                        execution_id=execution.id,
                        authorize_scope=authorize_scope,
                        **({"inspect_target": inspect_publication} if publication_digest is not None else {}),
                    )
                    require(scope_checked and target.role_arn == body.role_arn and target.physical.canonical_key == physical.canonical_key)
                    if publication_digest is not None:
                        require(
                            isinstance(getattr(target, "observation", None), ImageReadback)
                            and target.observation.account_id == physical.account_id
                            and target.observation.region == physical.region
                            and target.observation.source_revision == body.source_revision
                            and target.observation.image_digest == publication_digest
                        )
                    require(body.role_arn != body.mutation_role_arn)
                except CycleBlockedError:
                    require(False)
                require(
                    await shared_release_held(
                        session,
                        target=shared,
                        org_id=action.org_id,
                        action_id=owner,
                        release_ref=body.source_revision,
                        manifest_entry_id=entry.entry_id,
                        generation=execution.claim_generation,
                        started_at=started_at,
                        allow_publication_pending=True,
                    )
                )
            if started_at is not None and "release_job" not in saved:
                action.detail = {**saved, "release_job": job_pin}
                await session.commit()
            return action.id, SimpleNamespace(
                org_id=action.org_id,
                repo=report.repo,
                installation_id=report.installation_id,
                provider_repository_id=report.provider_repository_id,
            )

    async def verify_run(self, claims, binding, body, *, terminal=False, allow_in_progress=False):
        app_id, key = await resolve_tenant_app_credentials(binding.org_id)
        app = (await self.provider.request(binding, "GET", "/app", token=_mint_app_jwt(app_id, key))).json()
        require(isinstance(app, dict) and isinstance(app.get("slug"), str) and app["slug"])
        bot = (await self.provider.request(binding, "GET", f"/users/{app['slug']}[bot]")).json()
        require(isinstance(bot, dict) and bot.get("type") == "Bot" and type(bot.get("id")) is int and bot["id"] > 0)
        require(terminal or str(bot["id"]) == claims["actor_id"])
        run = (await self.provider.request(binding, "GET", f"/repos/{binding.repo}/actions/runs/{claims['run_id']}")).json()
        require(isinstance(run, dict))
        require(
            run.get("id") == int(claims["run_id"])
            and run.get("run_attempt") == (int(claims["run_attempt"]) if getattr(body, "contract_digest", None) else 1)
            and run.get("event") == "workflow_dispatch"
            and (
                (
                    (run.get("status") == "completed" and run.get("conclusion") in {"success", "failure", "cancelled", "timed_out"})
                    or (
                        allow_in_progress
                        and run.get("status") in {"requested", "waiting", "queued", "pending", "in_progress"}
                        and run.get("conclusion") is None
                    )
                )
                if terminal
                else run.get("status") == "in_progress"
            )
            and isinstance(run.get("repository"), dict)
            and run["repository"].get("id") == binding.provider_repository_id
            and run.get("head_sha") == body.dispatch_revision
            and run.get("path") == f".github/workflows/{body.workflow}"
            and run.get("display_title") == "ADP deployment " + body.correlation
            and isinstance(run.get("actor"), dict)
            and isinstance(run.get("triggering_actor"), dict)
            and str(run["actor"].get("id")) == (str(bot["id"]) if terminal else claims["actor_id"])
            and str(run["triggering_actor"].get("id")) == (str(bot["id"]) if terminal else claims["actor_id"]),
        )
        try:
            timestamp = "run_started_at" if getattr(body, "contract_digest", None) and int(claims["run_attempt"]) > 1 else "created_at"
            started_at = datetime.fromisoformat(run[timestamp].replace("Z", "+00:00"))
        except (KeyError, ValueError, AttributeError):
            require(False)
        require(started_at.tzinfo is not None and started_at <= datetime.now(UTC))
        return (run, started_at) if terminal else started_at

    async def verify_terminal_run(self, binding, saved, *, allow_in_progress=False):
        require(isinstance(saved, dict))
        pin = saved.get("release_job") or {}
        release = saved.get("shared_release") or {}
        require(
            isinstance(pin, dict)
            and isinstance(pin.get("run_id"), str)
            and re.fullmatch(r"[1-9][0-9]{0,18}", pin["run_id"])
            and pin.get("run_attempt") == "1"
            and saved.get("dispatch_started") is True
            and saved.get("repo") == binding.repo
            and type(saved.get("provider_repository_id")) is int
            and saved["provider_repository_id"] == binding.provider_repository_id
            and isinstance(saved.get("dispatch_revision"), str)
            and re.fullmatch(r"[0-9a-f]{40}", saved["dispatch_revision"])
            and isinstance(saved.get("workflow"), str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml", saved["workflow"])
            and isinstance(saved.get("correlation"), str)
            and re.fullmatch(r"orch:[0-9a-f-]{36}", saved["correlation"])
            and isinstance(saved.get("source_revision"), str)
            and re.fullmatch(r"[0-9a-f]{40}", saved["source_revision"])
            and isinstance(release, dict)
            and release.get("source_revision") == saved.get("source_revision")
        )
        body = SimpleNamespace(correlation=saved["correlation"], dispatch_revision=saved["dispatch_revision"], workflow=saved["workflow"])
        return await self.verify_run({"run_id": pin["run_id"], "actor_id": None}, binding, body, terminal=True, allow_in_progress=allow_in_progress)

    async def recover_terminal_publication(self, context, services, *, reserve_retry=False, expected_workflow=None, expected_dispatch_revision=None):
        async with self.factory() as session:
            state = await services.authorize(session, context, Action.DEPLOY)
            actions = (
                await session.scalars(
                    select(OrchestrationAction)
                    .where(
                        OrchestrationAction.org_id == state.node.org_id,
                        OrchestrationAction.execution_id == state.row.id,
                        OrchestrationAction.kind == "operations_native_release_dispatch",
                        OrchestrationAction.detail["correlation"].as_string() == state.report.run_id,
                    )
                    .limit(2)
                )
            ).all()
            if not actions:
                return False
            if len(actions) != 1:
                raise CycleBlockedError("operations_release_recovery_ambiguous")
            action = actions[0]
            saved = deepcopy(action.detail or {})
            release = saved.get("shared_release") or {}
            entry = self.manifest_loader().entry(release.get("manifest_entry_id"))
            flow = await session.get(OrchestrationFlow, state.node.flow_id)
            accepted = getattr(state.contract.node, "operational_release_binding", None)
            recorded = ((state.report.dispatch_metadata or {}).get("orchestration") or {}).get("release_binding") or {}
            pins = await accepted_release_pins(session, node=state.node, execution=state.row, contract=state.contract, report=state.report)
            try:
                raw = release["target"]
                physical = PhysicalTarget(**{**raw, "evidence": TargetEvidence(**raw["evidence"])})
                shared, owner = foundation_release_scope(
                    node=state.node,
                    flow=flow,
                    entry=entry,
                    component=accepted.component,
                    target=physical,
                    environment=release["environment"],
                    source_revision=saved["source_revision"],
                )
            except (KeyError, TypeError, AttributeError, ValueError, LeaseError):
                raise CycleBlockedError("operations_release_recovery_scope_changed") from None
            if (
                pins is None
                or action.status not in {"prepared", "unknown", "succeeded"}
                or action.org_id != state.node.org_id
                or entry.status is not EntryStatus.ENABLED
                or entry.docs_only
                or flow.state not in {"pending", "running"}
                or flow.execution_paused
                or flow.archived_at is not None
                or accepted.target != entry.entry_id
                or accepted.repository != state.binding.repo
                or state.contract.node.evidence_expectations != [f"runtime_verified:{accepted.component}"]
                or entry.connection_id not in state.contract.environment_connection_ids
                or entry.entry_id != recorded.get("target")
                or entry.connection_id != recorded.get("connection_id")
                or entry.mutation_role_arn != recorded.get("mutation_role_arn")
                or entry.resource_kind != recorded.get("resource_kind")
                or entry.resource_id != recorded.get("resource_id")
                or entry.artifact_revision != recorded.get("manifest_artifact_revision")
                or (physical.resource_kind, physical.resource_id) != (entry.resource_kind, entry.resource_id)
                or pins["source_revision"] != saved.get("source_revision")
                or pins["default_branch"] != saved.get("branch")
                or entry.workflow is None
                or entry.workflow.path != recorded.get("workflow")
                or entry.workflow.definition_revision != recorded.get("manifest_definition_revision")
                or entry.workflow.path != f".github/workflows/{saved.get('workflow')}"
                or pins["definition_revision"] != saved.get("definition_revision")
                or release.get("owner") != owner
                or release.get("target_key") != shared.canonical_key
                or release.get("source_revision") != saved.get("source_revision")
                or release.get("mutation_role_arn") != entry.mutation_role_arn
                or type(release.get("generation")) is not int
                or not 0 < release["generation"] <= state.row.claim_generation
                or saved.get("repo") != state.binding.repo
                or saved.get("provider_repository_id") != state.binding.provider_repository_id
            ):
                raise CycleBlockedError("operations_release_recovery_scope_changed")
            lease = await session.scalar(
                select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == shared.canonical_key)
            )
            if lease is None or lease.publication_pending_digest is None:
                return False
            if reserve_retry and (
                saved.get("retry_started_at") is not None
                or saved.get("workflow") != expected_workflow
                or saved.get("dispatch_revision") != expected_dispatch_revision
            ):
                raise CycleBlockedError("operations_release_retry_intent_changed")
            digest = lease.publication_pending_digest
            binding = state.binding
            values = state.values
            execution_id = state.row.id
            action_id = action.id
        try:
            run, _ = await self.verify_terminal_run(binding, saved, allow_in_progress=True)
        except HTTPException:
            raise CycleBlockedError("operations_release_terminal_unverified") from None
        if run["status"] != "completed":
            return False

        async with self.factory() as session:
            scope_checked = False

            def authorize_scope(credential_id, role_arn):
                nonlocal scope_checked
                decision = authorize_action(
                    replace(values[-1], credential_scope=CredentialScope.USER_GRANTED),
                    Action.DEPLOY,
                    ResourceRef(
                        repository_id=binding.repo,
                        environment_connection_id=entry.connection_id,
                        node_address=graph_address(state.node, flow_slug=flow.slug),
                        org_id=state.node.org_id,
                        user_credential_id=credential_id,
                        aws_role_arn=role_arn,
                    ),
                    state.contract.plan_version,
                )
                if not decision.permitted:
                    raise CycleBlockedError("operations_release_recovery_connection_refused")
                scope_checked = True

            target = await self.targets.resolve(
                session,
                entry=entry,
                policy=values[2].policy,
                principal_user_id=values[3],
                execution_id=execution_id,
                authorize_scope=authorize_scope,
                inspect_target=lambda scoped, description, scope: read_pending_image(
                    scoped,
                    account_id=physical.account_id,
                    region=physical.region,
                    environment=release["environment"],
                    source_revision=saved["source_revision"],
                    image_digest=digest,
                    report_absence=True,
                ),
            )
            if (
                not scope_checked
                or target.physical.canonical_key != physical.canonical_key
                or target.role_arn == entry.mutation_role_arn
                or not isinstance(target.observation, ImageReadback | ImageAbsence)
            ):
                raise CycleBlockedError("operations_release_recovery_image_unverified")
            image = target.observation

        async with self.factory() as session:
            current = await services.authorize(session, context, Action.DEPLOY)
            if (
                current.row.id != execution_id
                or current.row.claim_generation != state.row.claim_generation
                or current.contract != state.contract
                or current.values[2].policy != values[2].policy
            ):
                raise CycleBlockedError("operations_release_recovery_authority_changed")
            identity = ExecutionIdentity(
                current.node.org_id,
                current.node.id,
                current.row.cycle,
                current.contract.plan_version,
                current.row.claim_id,
                current.row.claim_generation,
            )
            # Canonical lock order is node -> flow/claim/plan -> execution, as the result
            # observer takes it. Anchor the node FOR UPDATE before the authority read and
            # the execution lock so this recovery cannot invert against that observer; the
            # anchored owner is compared against the immutable identity captured above and
            # every later binding is still revalidated under the same locks.
            anchor = await session.get(OrchestrationNode, current.node.id, with_for_update=True, populate_existing=True)
            if anchor is None or anchor.org_id != identity.org_id or anchor.flow_id != current.row.flow_id:
                raise CycleBlockedError("operations_release_recovery_authority_changed")
            if await _live_authority_conflict(session, identity, flow_id=current.node.flow_id) is not None:
                raise CycleBlockedError("operations_release_recovery_authority_changed")
            row = await session.get(OrchestrationExecution, execution_id, with_for_update=True, populate_existing=True)
            node = await session.get(OrchestrationNode, current.node.id, with_for_update=True, populate_existing=True)
            if (
                row is None
                or node is None
                or row.claim_generation != identity.claim_generation
                or row.claim_id != identity.claim_id
                or row.org_id != node.org_id
                or node.attempts != identity.cycle
            ):
                raise CycleBlockedError("operations_release_recovery_authority_changed")
            action = await session.get(OrchestrationAction, action_id, with_for_update=True, populate_existing=True)
            if action is None or action.detail != saved or action.status not in {"prepared", "unknown", "succeeded"}:
                raise CycleBlockedError("operations_release_recovery_action_changed")
            evidence = dict(
                target=shared,
                holder=LeaseHolder(state.node.org_id, owner, release["generation"]),
                manifest_entry_id=entry.entry_id,
                release_ref=saved["source_revision"],
                digest=digest,
                terminal_evidence=f"github/{binding.repo}/actions/runs/{run['id']}:{run['conclusion']}",
            )
            if reserve_retry and not isinstance(image, ImageAbsence):
                raise CycleBlockedError("operations_release_retry_image_present")
            recovered = (
                await record_shared_publication_absence(session, **evidence, image_absence=image)
                if isinstance(image, ImageAbsence)
                else await recover_shared_publication(session, **evidence, image_readback=image)
            )
            if not recovered:
                raise CycleBlockedError("operations_release_recovery_lease_changed")
            if reserve_retry:
                successor = LeaseHolder(current.node.org_id, owner, current.row.claim_generation)
                if not await rollforward_shared_publication(session, **evidence, successor=successor, image_absence=image):
                    raise CycleBlockedError("operations_release_retry_unverified")
                action.detail = {
                    **{key: value for key, value in saved.items() if key != "release_job"},
                    "shared_release": {**release, "generation": successor.generation},
                    "retry_previous_run_id": str(run["id"]),
                    "retry_started_at": datetime.now(UTC).isoformat(),
                }
            await session.commit()
            return image if isinstance(image, ImageAbsence) else True

    async def _authorized_action(self, body, claims, *, publication_digest=None):
        action_id, binding = await self.snapshot(body, claims)
        started_at = await self.verify_run(claims, binding, body)
        await self.snapshot(body, claims, action_id=action_id, started_at=started_at, publication_digest=publication_digest)
        return action_id

    async def authorize(self, body, claims):
        if body.contract_digest is not None:
            from .generic_delivery_job import authorize_job

            await authorize_job(self, body, claims)
            return
        await self._authorized_action(body, claims)

    async def target_identity(self, body, claims):
        require(body.contract_digest is not None)
        from .generic_delivery_job import authorize_job

        return await authorize_job(self, body, claims, include_target=True)

    async def _publication(self, body, claims, *, settled):
        if body.contract_digest is not None:
            from .generic_delivery_job import authorize_job

            await authorize_job(self, body, claims, publication="observed" if settled else "pending")
            return
        action_id = await self._authorized_action(body, claims, publication_digest=body.image_digest if settled else None)
        async with self.factory() as session:
            action = await session.get(OrchestrationAction, action_id, with_for_update=True)
            require(action is not None and action.kind == "operations_native_release_dispatch")
            release = (action.detail or {}).get("shared_release") or {}
            require(
                (action.detail or {}).get("release_job") == {"run_id": claims.get("run_id"), "run_attempt": "1"}
                and release.get("source_revision") == body.source_revision
                and release.get("mutation_role_arn") == body.mutation_role_arn
            )
            try:
                raw = release["target"]
                target = PhysicalTarget(**{**raw, "evidence": TargetEvidence(**raw["evidence"])})
                execution = await session.get(OrchestrationExecution, action.execution_id)
                require(execution is not None and execution.org_id == action.org_id)
                node = await session.get(OrchestrationNode, execution.node_id)
                flow = await session.get(OrchestrationFlow, execution.flow_id)
                contract = await accepted_node_contract(session, node)
                entry = self.manifest_loader().entry(release["manifest_entry_id"])
                shared, owner = foundation_release_scope(
                    node=node,
                    flow=flow,
                    entry=entry,
                    component=contract.node.operational_release_binding.component,
                    target=target,
                    environment=release["environment"],
                    source_revision=body.source_revision,
                )
                require(shared.canonical_key == release["target_key"] and owner == release["owner"] and node.org_id == action.org_id)
                operation = settle_shared_publication if settled else begin_shared_publication
                require(
                    await operation(
                        session,
                        target=shared,
                        holder=LeaseHolder(action.org_id, owner, release["generation"]),
                        manifest_entry_id=release["manifest_entry_id"],
                        release_ref=body.source_revision,
                        digest=body.image_digest,
                    )
                )
            except (KeyError, TypeError, AttributeError, LeaseError):
                require(False)
            await session.commit()

    async def mark_publication(self, body, claims):
        await self._publication(body, claims, settled=False)

    async def settle_publication(self, body, claims):
        await self._publication(body, claims, settled=True)


def service():
    return NativeReleaseJob(get_session_factory())


@router.post("/check")
async def check_native_job(body: NativeJobRequest, request: Request, response: Response, guard=Depends(service)):
    response.headers["Cache-Control"] = "no-store"
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    require(scheme.lower() == "bearer" and 0 < len(token) <= 8192 and not any(character.isspace() for character in token))
    claims = await github_claims(token, audience=AUDIENCE)
    await guard.authorize(body, claims)
    return {"authorized": True}


@router.post("/target")
async def read_native_target(body: NativeJobRequest, request: Request, response: Response, guard=Depends(service)):
    response.headers["Cache-Control"] = "no-store"
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    require(scheme.lower() == "bearer" and 0 < len(token) <= 8192 and not any(character.isspace() for character in token))
    claims = await github_claims(token, audience=AUDIENCE)
    target = await guard.target_identity(body, claims)
    return {
        "authorized": True,
        "target": target,
        "source_revision": body.source_revision,
        "contract_digest": body.contract_digest,
        "run_id": claims["run_id"],
        "run_attempt": claims["run_attempt"],
    }


@router.post("/publication-intent")
async def publication_intent(body: NativePublicationRequest, request: Request, response: Response, guard=Depends(service)):
    response.headers["Cache-Control"] = "no-store"
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    require(scheme.lower() == "bearer" and 0 < len(token) <= 8192 and not any(character.isspace() for character in token))
    claims = await github_claims(token, audience=AUDIENCE)
    await guard.mark_publication(body, claims)
    return {"recorded": True}


@router.post("/publication-observed")
async def publication_observed(body: NativePublicationRequest, request: Request, response: Response, guard=Depends(service)):
    response.headers["Cache-Control"] = "no-store"
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    require(scheme.lower() == "bearer" and 0 < len(token) <= 8192 and not any(character.isspace() for character in token))
    claims = await github_claims(token, audience=AUDIENCE)
    await guard.settle_publication(body, claims)
    return {"settled": True}
