"""Durable check -> merge -> deploy -> verify continuation for retained repairs."""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import quote

from sqlalchemy import select

from src.agentauth.github_operation_service import installation_token, operation_permissions
from src.agentauth.github_operations import GitHubOperation
from src.agentauth.github_provider import GitHubProvider
from src.agentauth.operations_repair import merge_repair, qualify_repair_readiness, repair_status

from .deployment_workflow_provider import WorkflowProvider
from .execution_policy import Action
from .execution_runner import (
    DecisionKind,
    EffectOutcome,
    EffectRequest,
    EffectResult,
    HandlerDecision,
    HandlerObservation,
    ObservationKind,
    OperationIdentity,
)
from .execution_state import ActionIntent, BlockCode, BlockRecord
from .models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationFlow, OrchestrationNode
from .operations_continuation_authority import ACTOR, OperationsContinuationServices, require
from .operations_publication_recovery import ImageAbsence
from .operations_repair import record_repair_merge
from .review_cycle import CycleBlockedError
from .state import ActorKind, NodeState, transition

logger = logging.getLogger(__name__)
MERGE = "operations_continuation_merge"
DEPLOY = "operations_continuation_deploy"


@dataclass(frozen=True)
class OperationsObservation(HandlerObservation):
    stage: str | None = None
    data: dict | None = None


class OperationsContinuationHandler:
    def __init__(self, factory, services=None):
        self.factory = factory
        self.services = services or OperationsContinuationServices(factory)

    async def state(self, context, action=Action.OPERATE):
        async with self.factory() as session:
            state = await self.services.authorize(session, context, action)
            await session.commit()
            return state

    async def actions_authority(self, context):
        state = await self.state(context, Action.DEPLOY)
        return state.issued, state.binding, state.execution

    async def runtime_authority(self, session, context):
        return (await self.services.authorize(session, context, Action.DEPLOY)).values

    async def merge(self, context, data, *, mutate):
        state = await self.state(context, Action.MERGE)
        token = await installation_token(
            org_id=state.binding.org_id,
            installation_id=state.binding.installation_id,
            repository=state.binding.repo,
            permissions={
                **operation_permissions(GitHubOperation.MERGE_PULL_REQUEST if mutate else GitHubOperation.READ_REPOSITORY),
                "statuses": "read",
                **({"administration": "read", "workflows": "write"} if mutate else {}),
            },
        )
        async with GitHubProvider(token=token, assignment=state.authorized.assignment) as provider:
            if not mutate:
                return await repair_status(
                    provider, data["pull_number"], expected_head=data["head"], required_checks=state.authorized.policy.repair_required_checks
                )
            await qualify_repair_readiness(provider, state.authorized.assignment.default_branch)

            async def reauthorize():
                current = await self.state(context, Action.MERGE)
                require(current.authorized == state.authorized, "operations_merge_authority_changed")
                return current.authorized

            result = await merge_repair(
                provider, SimpleNamespace(pull_number=data["pull_number"], expected_head=data["head"]), state.authorized, reauthorize
            )
        if result.get("merged") is True:
            async with self.factory() as session:
                current = await self.services.authorize(session, context, Action.MERGE)
                require(current.authorized == state.authorized, "operations_merge_authority_changed")
                await record_repair_merge(
                    session,
                    state.authorized.assignment,
                    pull_number=data["pull_number"],
                    head=data["head"],
                    merged=result,
                    authorized=state.authorized,
                )
                await session.commit()
        return result

    async def runs(self, state, action):
        token = await installation_token(
            org_id=state.binding.org_id,
            installation_id=state.binding.installation_id,
            repository=state.binding.repo,
            permissions={"metadata": "read", "actions": "read"},
        )
        workflow = action.detail["workflow"]
        since = action.created_at.replace(tzinfo=UTC) if action.created_at.tzinfo is None else action.created_at
        # GitHub timestamps have second precision; retain a run created in the
        # same second as the committed dispatch intent.
        since = since.replace(microsecond=0)
        matches = []
        provider = WorkflowProvider()
        native = action.kind == "operations_native_release_dispatch"
        pin = (action.detail or {}).get("release_job") or {}
        retry_started_at = (action.detail or {}).get("retry_started_at") if native else None
        if retry_started_at is not None:
            try:
                retry_after = datetime.fromisoformat(retry_started_at)
            except (TypeError, ValueError):
                raise CycleBlockedError("operations_release_retry_intent_changed") from None
            require(retry_after.tzinfo is not None and retry_after <= datetime.now(UTC), "operations_release_retry_intent_changed")
            since = retry_after
        for page in range(1, 6):
            response = await provider.request(
                state.binding,
                "GET",
                f"/repos/{state.binding.repo}/actions/workflows/{quote(workflow)}/runs",
                token=token,
                params={"event": "workflow_dispatch", "per_page": 10, "page": page},
                max_bytes=512 * 1024,
            )
            rows = response.json().get("workflow_runs")
            require(isinstance(rows, list), "operations_workflow_runs_unavailable")
            for raw in rows:
                created = datetime.fromisoformat(raw["created_at"].replace("Z", "+00:00"))
                if (
                    created >= since
                    and raw.get("display_title") == "ADP deployment " + state.report.run_id
                    and raw.get("event") == "workflow_dispatch"
                    and raw.get("path") == ".github/workflows/" + workflow
                    and (raw.get("repository") or {}).get("id") == state.binding.provider_repository_id
                    and (
                        not native
                        or (
                            raw.get("run_attempt") == 1
                            and raw.get("head_sha") == action.detail.get("dispatch_revision")
                            and (not pin or str(raw.get("id")) == pin.get("run_id"))
                        )
                    )
                ):
                    matches.append(raw)
            if len(rows) < 10 or rows and datetime.fromisoformat(rows[-1]["created_at"].replace("Z", "+00:00")) < since:
                break
        else:
            raise CycleBlockedError("operations_workflow_history_limit", BlockCode.PROVIDER_UNAVAILABLE)
        require(len(matches) <= 1, "operations_workflow_correlation_ambiguous")
        return matches[0] if matches else None

    async def observe(self, context):
        try:
            return await self._observe(context)
        except CycleBlockedError as error:
            return OperationsObservation(
                ObservationKind.BLOCKED,
                block=BlockRecord(
                    code=error.code,
                    owner="engine",
                    detail=error.reason,
                    required_input="The engine will recheck this condition under the accepted policy.",
                ),
            )
        except Exception:
            logger.exception("Operations continuation observation failed for %s", context.execution.id)
            return OperationsObservation(ObservationKind.WAITING, detail="Operations provider observation unavailable; automatic recheck scheduled.")

    async def _observe(self, context):
        state = await self.state(context)
        async with self.factory() as session:
            actions = list(
                await session.scalars(
                    select(OrchestrationAction)
                    .where(OrchestrationAction.org_id == state.node.org_id, OrchestrationAction.execution_id == state.row.id)
                    .order_by(OrchestrationAction.created_at, OrchestrationAction.id)
                    .limit(201)
                )
            )
        require(len(actions) <= 200, "operations_continuation_history_limit")
        pending = next((a for a in actions if a.operation_key == context.execution.pending_action_key), None)
        repairs = {}
        merged = set()
        for action in actions:
            if action.kind == "operations_repair_pr" and action.status == "succeeded":
                repairs[action.receipt_ref] = action.detail
            if action.kind == "operations_repair_merge" and action.status == "succeeded":
                merged.add((action.receipt_ref, action.detail.get("head")))
        for ref, data in repairs.items():
            if (ref, data.get("head")) in merged:
                if pending is not None and pending.kind == MERGE and pending.detail.get("head") == data.get("head"):
                    return OperationsObservation(
                        ObservationKind.SUCCEEDED, operation_key=pending.operation_key, receipt_ref=ref, detail="Qualified repair merge recorded."
                    )
                continue
            status = await self.merge(context, data, mutate=False)
            if status["merged"]:
                # Reconcile a merge which landed before the process recorded its
                # receipt. merge_repair re-verifies checks, parents and paths.
                await self.merge(context, data, mutate=True)
                return OperationsObservation(
                    ObservationKind.SUCCEEDED if pending else ObservationKind.WAITING,
                    operation_key=pending.operation_key if pending else None,
                    receipt_ref=ref,
                    detail="Qualified repair merge recovered; deployment will continue.",
                )
            if not status["ready"]:
                return OperationsObservation(
                    ObservationKind.WAITING,
                    detail="Waiting for qualified repair checks and branch state; automatic recheck scheduled.",
                    data={"repair_status": status},
                )
            return OperationsObservation(
                ObservationKind.READY,
                stage=MERGE,
                data=pending.detail
                if pending
                else {"pull_number": data["pull_number"], "head": data["head"], "sequence": sum(a.kind == MERGE for a in actions) + 1},
                operation_key=pending.operation_key if pending else None,
            )
        require(repairs, "operations_retained_repair_missing")
        state = await self.state(context, Action.DEPLOY)
        from .operations_release_job import NativeReleaseJob

        recovered = await NativeReleaseJob(self.factory).recover_terminal_publication(context, self.services)
        if isinstance(recovered, ImageAbsence):
            native = [action for action in actions if action.kind == "operations_native_release_dispatch"]
            require(len(native) == 1, "operations_release_retry_intent_changed")
            if (native[0].detail or {}).get("retry_started_at") is not None:
                raise CycleBlockedError("operations_release_retry_exhausted")
            workflow = (native[0].detail or {}).get("workflow")
            require(workflow in state.issued.workflow_allowlist, "operations_release_retry_intent_changed")
            if pending is not None:
                require(
                    pending.kind == DEPLOY
                    and pending.status in {"prepared", "unknown"}
                    and (pending.detail or {}).get("native_retry") is True
                    and (pending.detail or {}).get("workflow") == workflow
                    and "shared_release" not in (pending.detail or {}),
                    "operations_release_retry_intent_changed",
                )
            return OperationsObservation(
                ObservationKind.READY,
                stage=DEPLOY,
                data={"workflow": workflow, "native_retry": True},
                operation_key=pending.operation_key if pending is not None else None,
            )
        deployments = [a for a in actions if a.kind in {DEPLOY, "operations_native_release_dispatch"}]
        if not deployments:
            return OperationsObservation(ObservationKind.READY, stage=DEPLOY, data={"workflow": next(iter(state.issued.workflow_allowlist))})
        latest = deployments[-1]
        if (
            pending is not None
            and latest.id == pending.id
            and pending.kind == DEPLOY
            and (pending.detail or {}).get("native_retry") is not True
            and "mutation_role_arn" in ((state.report.dispatch_metadata.get("orchestration") or {}).get("release_binding") or {})
            and getattr(getattr(state.contract.node, "operational_release_binding", None), "delivery_contract", None) is None
        ):
            from .deployment_manifest import EntryStatus, load_packaged_manifest

            release = getattr(state.contract.node, "operational_release_binding", None)
            entry = load_packaged_manifest().entry(release.target) if release is not None else None
            require(
                release is not None
                and entry is not None
                and entry.status is EntryStatus.ENABLED
                and entry.release_group is not None
                and state.row.pending_action_key == pending.operation_key
                and pending.status in {"prepared", "unknown"}
                and (pending.detail or {}).get("workflow") in state.issued.workflow_allowlist
                and not (pending.detail or {}).get("native_retry")
                and not (pending.detail or {}).get("dispatch_started")
                and not (pending.detail or {}).get("native_release_action_id"),
                "operations_deployment_dispatch_changed",
            )
            return OperationsObservation(
                ObservationKind.READY,
                stage=DEPLOY,
                data={"workflow": pending.detail["workflow"]},
                operation_key=pending.operation_key,
            )
        if latest.kind == DEPLOY and (latest.detail or {}).get("native_retry") is True:
            native = [action for action in deployments if action.kind == "operations_native_release_dispatch"]
            require(len(native) == 1, "operations_release_retry_intent_changed")
            latest = native[0]
            if (latest.detail or {}).get("retry_started_at") is None:
                return OperationsObservation(ObservationKind.WAITING, detail="Retry reservation is not committed; no repeat dispatch.")
        elif pending is not None and pending.kind == DEPLOY and pending.id != latest.id:
            require(
                latest.kind == "operations_native_release_dispatch"
                and (pending.detail or {}).get("native_release_action_id") == latest.id
                and (pending.detail or {}).get("dispatch_started") is True,
                "operations_deployment_dispatch_changed",
            )
        raw = await self.runs(state, latest)
        if raw is None:
            return OperationsObservation(
                ObservationKind.WAITING, detail="Waiting for GitHub to publish the correlated deployment run; no duplicate dispatch."
            )
        if pending is not None:
            return OperationsObservation(
                ObservationKind.SUCCEEDED,
                operation_key=pending.operation_key,
                receipt_ref=f"github/{state.binding.repo}/actions/runs/{raw['id']}",
                detail="Correlated deployment run observed.",
            )
        if raw.get("status") != "completed":
            return OperationsObservation(
                ObservationKind.WAITING, detail=f"Deployment {raw['id']} is {raw.get('status')}; automatic recheck scheduled."
            )
        if raw.get("conclusion") != "success":
            return OperationsObservation(
                ObservationKind.BLOCKED,
                block=BlockRecord(
                    BlockCode.DEPENDENCY_UNSATISFIED,
                    owner="operations-repair",
                    detail=f"Deployment {raw['id']} concluded {raw.get('conclusion')}.",
                    required_input="Repair the recorded deployment failure within the accepted repair allowance.",
                ),
            )
        from src.agentauth.actions_routes import _capture_correlated_run

        evidence = await _capture_correlated_run(
            None,
            None,
            execution=state.execution,
            binding=state.binding,
            raw=raw,
            provider=WorkflowProvider(),
            authority=lambda: self.actions_authority(context),
            runtime_authority=self.runtime_authority,
        )
        if not evidence or not evidence.get("runtime_evidence_id"):
            return OperationsObservation(
                ObservationKind.WAITING, detail="Deployment succeeded; waiting for independent signed runtime verification.", data=evidence
            )
        return OperationsObservation(ObservationKind.READY, stage="settle", data=evidence)

    def decide(self, context, observation):
        if observation.kind is ObservationKind.BLOCKED:
            return HandlerDecision(DecisionKind.BLOCK, block=observation.block)
        if observation.kind is ObservationKind.SUCCEEDED:
            # Reconciliation settled the pending effect. Move to runnable so
            # the runner clears its pointer before observing the next stage.
            return HandlerDecision(DecisionKind.ADVANCE, progress_note=observation.detail)
        if observation.stage == "settle":

            async def settle(session, current):
                state = await self.services.authorize(session, current, Action.DEPLOY, settling=True)
                from .operations_evidence import observed_operations_evidence

                dispatch = {"run_id": state.report.run_id, "attempt": state.node.attempts, "conclusion_authority": "evidence"}
                verified = {}
                evidence, hold = await observed_operations_evidence(session, state.node, dispatch, controller=True, observation=verified)
                require(evidence is not None, hold or "operations_runtime_evidence_missing")
                if verified.get("native_release"):
                    from .operations_result_settlement import settle_native_release

                    try:
                        await settle_native_release(session, execution=state.row, run_id=state.report.run_id, verified=verified["native_release"])
                    except ValueError as exc:
                        raise CycleBlockedError("operations_release_handoff_changed") from exc
                move = transition(
                    NodeState(state.node.state), NodeState.PASSED, actor_kind=ActorKind.SERVICE, reason="Verified operations continuation"
                )
                require(move.allowed, "operations_completion_not_authorized")
                session.add(
                    OrchestrationDecision(
                        org_id=state.node.org_id,
                        flow_id=state.node.flow_id,
                        node_id=state.node.id,
                        kind="result_observed",
                        actor_id=ACTOR,
                        actor_kind="service",
                        actor_role="engine",
                        from_state=state.node.state,
                        to_state="passed",
                        reason=json.dumps(
                            dict(
                                attempt=state.node.attempts,
                                source_run_id=state.report.run_id,
                                continuation_receipt=state.row.handoff_receipt_ref,
                                **observation.data,
                            )
                        ),
                    )
                )
                state.node.state = "passed"
                from .work_claims import Disposition, ReleaseReason, release_work

                released = await release_work(
                    session,
                    org_id=state.node.org_id,
                    claim_id=current.identity.claim_id,
                    generation=current.identity.claim_generation,
                    reason=ReleaseReason.COMPLETED,
                    terminal_evidence=observation.data["runtime_evidence_id"],
                )
                require(released.disposition is Disposition.ADMITTED, "operations_completion_claim_changed")

            return HandlerDecision(
                DecisionKind.CONCLUDE, settlement=settle, progress_note="Repair merged, deployment and runtime independently verified."
            )
        if observation.kind is not ObservationKind.READY:
            return HandlerDecision(DecisionKind.WAIT, next_check_at=context.now + timedelta(seconds=60), progress_note=observation.detail)
        key = (
            observation.operation_key or OperationIdentity.from_context(context, observation.stage, json.dumps(observation.data, sort_keys=True)).key
        )
        return HandlerDecision(
            DecisionKind.EFFECT,
            effect=EffectRequest(
                ActionIntent(key, observation.stage, detail=observation.data), Action.MERGE if observation.stage == MERGE else Action.DEPLOY
            ),
        )

    async def perform(self, context, effect):
        try:
            if effect.intent.kind == MERGE:
                result = await self.merge(context, effect.intent.detail, mutate=True)
                return EffectResult(
                    EffectOutcome.SUCCEEDED if result.get("merged") else EffectOutcome.FAILED,
                    receipt_ref=result.get("sha"),
                    detail="Repair merge recorded." if result.get("merged") else "Repair readiness changed; recheck scheduled.",
                )
            from src.agentauth.actions_routes import _perform

            await _perform(
                None,
                None,
                workflow=effect.intent.detail["workflow"],
                run_id=None,
                inputs={},
                authority=lambda: self.actions_authority(context),
                factory=self.factory,
                native_retry=(context, self.services) if effect.intent.detail.get("native_retry") is True else None,
                continuation_context=(context, self.services) if effect.intent.detail.get("native_retry") is not True else None,
            )
            return EffectResult(EffectOutcome.SUCCEEDED, detail="Deployment accepted; waiting for its correlated run.")
        except Exception:
            logger.exception("Operations continuation effect requires reconciliation for %s", context.execution.id)
            return EffectResult(EffectOutcome.UNCERTAIN, detail="Provider outcome uncertain; reconcile before any retry.")


async def recover_pending_operations(factory, *, limit=10):
    """Bounded scheduled recovery; inactive flows and changed authority stay closed."""
    from .developer_personas import operations_executor_enabled

    if not operations_executor_enabled() or os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() != "true":
        return 0
    started = time.monotonic()
    services = OperationsContinuationServices(factory)
    async with factory() as session:
        published_repair = (
            select(OrchestrationAction.id)
            .join(OrchestrationExecution, OrchestrationAction.execution_id == OrchestrationExecution.id)
            .where(
                OrchestrationExecution.node_id == OrchestrationNode.id,
                OrchestrationExecution.org_id == OrchestrationNode.org_id,
                OrchestrationExecution.cycle == OrchestrationNode.attempts,
                OrchestrationAction.kind == "operations_repair_pr",
                OrchestrationAction.status == "succeeded",
            )
            .exists()
        )
        candidates = list(
            await session.scalars(
                select(OrchestrationNode)
                .join(OrchestrationFlow, OrchestrationNode.flow_id == OrchestrationFlow.id)
                .where(
                    OrchestrationNode.state == "failed",
                    OrchestrationNode.kind == "story",
                    OrchestrationFlow.execution_paused.is_(False),
                    OrchestrationFlow.state.in_(["pending", "running"]),
                    OrchestrationFlow.archived_at.is_(None),
                    published_repair,
                )
                .order_by(OrchestrationNode.updated_at, OrchestrationNode.id)
                .limit(limit)
            )
        )
    recovered = 0
    for candidate in candidates:
        if time.monotonic() - started >= 10:
            break
        try:
            async with factory() as session:
                node = await session.scalar(
                    select(OrchestrationNode)
                    .where(OrchestrationNode.id == candidate.id, OrchestrationNode.org_id == candidate.org_id)
                    .with_for_update()
                )
                await asyncio.wait_for(services.recover(session, node), timeout=10)
                await session.commit()
                recovered += 1
        except CycleBlockedError as exc:
            logger.info("Operations continuation recovery held node=%s reason=%s", candidate.id, exc.reason)
        except Exception:
            logger.exception("Operations continuation recovery unavailable node=%s", candidate.id)
    return recovered
