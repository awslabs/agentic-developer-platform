"""Durable engine-owned merge actions; code completion and deployment stay distinct."""

from __future__ import annotations

import hashlib
import json
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from src.agentauth.github_provider import ProviderConflictError, ProviderUnavailableError

from .execution_policy import Action, CredentialScope
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
from .execution_state import ActionIntent, BlockCode, ExecutionPhase, Observation, ObservedOutcome, OutcomeKind
from .execution_store import load_execution, prepare_action, record_observation
from .merge_evidence import (
    EligibilityReason,
    EligibilityState,
    GitHubEvidenceSource,
    GitHubMergeObserver,
    evaluate_required_checks,
    observe_merge_eligibility,
)
from .merge_provider import MergeProvider
from .merge_review import load_merge_review
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from .pr_bindings import active_binding_for_node, binding_scope_matches
from .review_cycle import CycleBlockedError, block
from .state import ActorKind, NodeState, transition

MERGE_KIND = "merge_pull_request"
PHASES = (ExecutionPhase.MERGE_READY,)


async def code_only_delivery(session, context, node):
    """End code delivery at merge when the accepted policy contains only code work.

    Deployment permissions or gates retain the full delivery pipeline. Evaluation
    nodes retain their own acceptance requirements in either mode.
    """
    plan = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == node.org_id,
            OrchestrationAcceptedPlan.flow_id == node.flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
    )
    marker = (plan.plan_document or {}).get("execution_continuation") if plan else None
    if marker is None:
        from .policy_admission import load_in_force_policy

        admission = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
        policy = admission.policy
        if admission.refusal is not None or admission.plan_version != context.identity.accepted_plan_version:
            raise CycleBlockedError("code_delivery_contract_changed")
        return bool(
            policy is not None
            and Action.MERGE in policy.allowed_actions
            and not policy.environment_connection_ids
            and set(policy.allowed_actions).union(policy.human_gates) <= {Action.DEVELOP, Action.REVIEW, Action.REPAIR, Action.MERGE}
        )
    if not isinstance(marker, dict) or marker.get("delivery_mode") is None:
        return False
    from .shared_cycle import shared_marker

    accepted, _ = await shared_marker(session, org_id=node.org_id, flow_id=node.flow_id)
    from .plan_lineage import ancestor_plan

    if (
        await ancestor_plan(session, accepted, context.identity.accepted_plan_version, node_id=node.id) is None
        or marker["delivery_mode"] != "code_only"
    ):
        raise CycleBlockedError("code_delivery_contract_changed")
    return True


class MergeReceipt(BaseModel):
    """Published M2 receipt consumed by deployment observers, never issue state."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    org_id: str = Field(min_length=1, max_length=255)
    execution_id: str = Field(min_length=1, max_length=255)
    flow_id: str = Field(min_length=1, max_length=255)
    node_id: str = Field(min_length=1, max_length=255)
    cycle: int = Field(ge=1)
    accepted_plan_version: int = Field(ge=1)
    claim_id: str = Field(min_length=1, max_length=255)
    claim_generation: int = Field(ge=1)
    repo: str = Field(min_length=3, max_length=255)
    pr_number: int = Field(gt=0)
    provider_repository_id: int = Field(gt=0)
    provider_pr_node_id: str = Field(min_length=1, max_length=255)
    reviewed_head_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    reviewed_base_sha: str | None = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    merge_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    review_ref: str = Field(min_length=1, max_length=512)
    operation_key: str = Field(min_length=1, max_length=255)
    method: str = Field(pattern=r"^(merge|squash|rebase|queue|external)$")
    verification_kind: Literal["pre_merge_authorization", "post_merge_verification"] = "pre_merge_authorization"
    eligibility_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    eligibility_observed_at: datetime
    merged_at: datetime
    observed_at: datetime
    adopted: bool

    @model_validator(mode="after")
    def chronological(self):
        if any(moment.tzinfo is None for moment in (self.eligibility_observed_at, self.merged_at, self.observed_at)):
            raise ValueError("merge receipt timestamps must be timezone-aware")
        if self.verification_kind == "post_merge_verification":
            if not self.adopted or self.method != "external" or self.reviewed_base_sha is not None:
                raise ValueError("Observed external merges cannot claim a pre-merge method or base authorization")
            if not self.merged_at <= self.eligibility_observed_at <= self.observed_at:
                raise ValueError("Post-merge verification must retain its actual observation time")
        elif self.method == "external" or self.reviewed_base_sha is None or not evidence_precedes_merge(self.eligibility_observed_at, self.merged_at):
            raise ValueError("merge receipt evidence is not chronological")
        if self.merged_at > self.observed_at:
            raise ValueError("merge receipt evidence is not chronological")
        return self


def evidence_precedes_merge(observed, merged):
    # GitHub reports merged_at at second precision. A subsecond eligibility
    # observation in that same second still precedes an engine merge. Retain
    # the provider timestamp verbatim and reject evidence in any later second.
    return observed <= merged or (merged.microsecond == 0 and observed < merged + timedelta(seconds=1))


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class MergeObservation(HandlerObservation):
    snapshot: dict | None = None
    receipt: MergeReceipt | None = None


class MergeServices:
    def __init__(self, factory, *, authority=None, provider=None, storage=None):
        self.factory = factory
        self.authority = authority
        self.provider = provider or MergeProvider()
        self.storage = storage

    async def authority_for(self, context):
        from .review_cycle_dispatch import cycle_services

        return self.authority or await cycle_services(self.factory, context)

    async def state(self, session, context):
        loaded = await load_execution(session, identity=context.identity)
        if loaded is None or loaded.kind is not OutcomeKind.APPLIED or loaded.record is None:
            raise CycleBlockedError("merge_execution_authority_changed", BlockCode.AUTHORITY_UNVERIFIABLE)
        node = await session.scalar(
            select(OrchestrationNode)
            .where(
                OrchestrationNode.org_id == context.identity.org_id,
                OrchestrationNode.id == context.identity.node_id,
            )
            .execution_options(populate_existing=True)
        )
        if node is None or node.kind != "story" or node.attempts != context.identity.cycle or node.state not in {"running", "awaiting_merge"}:
            raise CycleBlockedError("merge_outer_gate_not_running", BlockCode.HUMAN_INPUT_REQUIRED)
        # Container state is derived from nodes. The stored default remains
        # pending even while an accepted protected execution is running.
        flow = await session.get(OrchestrationFlow, node.flow_id)
        if flow is None or flow.state not in {"pending", "running"}:
            raise CycleBlockedError("merge_flow_not_running", BlockCode.HUMAN_INPUT_REQUIRED)
        binding = await active_binding_for_node(session, org_id=node.org_id, node_id=node.id, attempt=node.attempts)
        if binding is None or binding.role != "implementation" or not binding_scope_matches(binding, node):
            raise CycleBlockedError("merge_binding_or_scope_changed", BlockCode.HUMAN_INPUT_REQUIRED)
        claim = await session.scalar(
            select(OrchestrationWorkClaim)
            .where(
                OrchestrationWorkClaim.org_id == node.org_id,
                OrchestrationWorkClaim.id == context.identity.claim_id,
            )
            .execution_options(populate_existing=True)
        )
        if claim is None or not claim.active_run_id:
            raise CycleBlockedError("merge_claim_unavailable", BlockCode.OWNERSHIP_LOST)
        return node, binding, claim.active_run_id

    async def review(self, session, context, node, binding, run_id, provider_state):
        authority = await self.authority_for(context)
        raw = await authority.protected(node.org_id, run_id)
        if not raw or raw.get("status", {}).get("S") in {"revoked", "cancelled"}:
            raise CycleBlockedError("reviewer_authority_revoked", BlockCode.AUTHORITY_UNVERIFIABLE)
        if raw.get("status") != {"S": "completed"} or raw.get("terminal_outcome") != {"S": "complete"}:
            from .review_assignment import reviewer_owns_delivery

            if not (provider_state.merged and await reviewer_owns_delivery(session, org_id=node.org_id, node_id=node.id, run_id=run_id)):
                raise CycleBlockedError("reviewer_still_active")
        return await load_merge_review(
            session,
            context=context,
            node=node,
            binding=binding,
            reviewer_run_id=run_id,
            raw_execution=raw,
            head_sha=provider_state.head_sha,
            storage=self.storage,
        )

    async def eligibility(self, session, context, node, binding, run_id, provider_state):
        review = await self.review(session, context, node, binding, run_id, provider_state)

        async def current_authority():
            current_node, current_binding, current_run = await self.state(session, context)
            authority = await self.authority_for(context)
            facts = await authority.authority_context(session, context, current_node, current_binding, current_run, Action.MERGE)
            # The engine uses the typed, single-repository merge adapter. The
            # worker-token selector intentionally exposes no MERGE capability.
            # Actual scoped token minting must still succeed in M1/provider I/O.
            return replace(facts[-1], credential_scope=CredentialScope.SCOPED)

        result = await observe_merge_eligibility(
            session=session,
            identity=context.identity,
            review=review,
            authorization_reader=current_authority,
            client=self.provider.client,
        )
        return result, review

    async def completed_elsewhere(self, session, context, node, binding, run_id, state):
        """Reconcile delivered code using current review/check evidence, without a merge effect.

        A human or another authorized actor can merge before this controller runs.
        Record what is verified now; never invent an earlier engine authorization.
        """
        before = (binding.id, binding.revision, binding.head_sha, run_id)
        review = await self.review(session, context, node, binding, run_id, state)
        token = await self.provider.token(binding, evidence=True)
        evidence = await GitHubEvidenceSource().bound_pull_request(
            org_id=node.org_id,
            installation_id=binding.installation_id,
            repo=binding.repo,
            pr_number=binding.pr_number,
            read_token=token,
            client=self.provider.client,
        )
        if (
            evidence is None
            or not evidence.merged
            or evidence.head_sha != binding.head_sha
            or evidence.head_sha != state.head_sha
            or evidence.merge_commit_sha != state.merge_sha
            or evidence.provider_repository_id != binding.provider_repository_id
            or evidence.provider_pr_node_id != binding.provider_pr_node_id
            or evidence.merged_at != state.merged_at
        ):
            raise CycleBlockedError("merged_pr_identity_changed")
        # The reviewer and merge controller use repository requirements to
        # select applicable checks. A merged PR must use that same policy:
        # GitHub's aggregate is null when no checks apply, not SUCCESS.
        async with AsyncExitStack() as stack:
            client = self.provider.client or await stack.enter_async_context(httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False))
            observation = await GitHubMergeObserver(client, token, lambda: datetime.now(UTC)).observe(binding)
        if not observation.merged or observation.head_sha != evidence.head_sha or observation.merge_commit_sha != evidence.merge_commit_sha:
            raise CycleBlockedError("merged_pr_identity_changed")
        if len(observation.requirements.checks) > 100 or len(observation.checks) > 200 or len(observation.sources) > 64:
            raise CycleBlockedError("merge_evidence_bound_exceeded")
        check_reasons, checks = evaluate_required_checks(observation)
        if check_reasons:
            raise CycleBlockedError("merged_pr_checks_not_successful")
        if not evidence.review_approved:
            raise CycleBlockedError("merged_pr_review_not_approved")
        _, current, current_run = await self.state(session, context)
        if before != (current.id, current.revision, current.head_sha, current_run):
            raise CycleBlockedError("merged_pr_binding_changed")
        now = datetime.now(UTC)
        verification = {
            "verification_kind": "post_merge_verification",
            "observed_at": now.isoformat(),
            "head_sha": evidence.head_sha,
            "merge_sha": evidence.merge_commit_sha,
            "merged_at": evidence.merged_at,
            "checks_state": "SATISFIED" if checks else "NOT_REQUIRED",
            "check_requirements": asdict(observation.requirements),
            "checks": [asdict(check) for check in checks],
            "check_sources": [{"kind": source.kind, "digest": source.payload_sha256} for source in observation.sources],
            "review_state": evidence.review_state,
            "review_ref": review.artifact_ref,
        }
        serialized = encode(verification)
        if len(serialized) > 65536:
            raise CycleBlockedError("merge_evidence_bound_exceeded")
        key = OperationIdentity.from_context(context, "observe_existing_merge", binding.id, str(binding.revision), state.merge_sha).key
        receipt = MergeReceipt(
            **asdict(context.identity),
            execution_id=context.execution.id,
            flow_id=context.execution.flow_id,
            repo=binding.repo,
            pr_number=binding.pr_number,
            provider_repository_id=binding.provider_repository_id,
            provider_pr_node_id=binding.provider_pr_node_id,
            reviewed_head_sha=state.head_sha,
            reviewed_base_sha=None,
            merge_sha=state.merge_sha,
            review_ref=review.artifact_ref,
            operation_key=key,
            method="external",
            verification_kind="post_merge_verification",
            eligibility_digest=hashlib.sha256(serialized.encode()).hexdigest(),
            eligibility_observed_at=now,
            merged_at=datetime.fromisoformat(state.merged_at.replace("Z", "+00:00")),
            observed_at=now,
            adopted=True,
        )
        return receipt, verification

    async def actions(self, session, context):
        rows = list(
            (
                await session.scalars(
                    select(OrchestrationAction)
                    .where(
                        OrchestrationAction.org_id == context.identity.org_id,
                        OrchestrationAction.execution_id == context.execution.id,
                        OrchestrationAction.kind == MERGE_KIND,
                    )
                    .order_by(OrchestrationAction.created_at, OrchestrationAction.id)
                    .limit(101)
                )
            ).all()
        )
        if len(rows) > 100:
            raise CycleBlockedError("merge_history_limit", BlockCode.ATTEMPTS_EXHAUSTED)
        return rows

    @staticmethod
    def snapshot(binding, state, eligibility, review, sequence):
        observed = eligibility.observation
        if observed is None or not eligibility.eligible:
            raise CycleBlockedError("merge_eligibility_missing")
        if len(observed.requirements.checks) > 100 or len(observed.checks) > 200 or len(observed.sources) > 64:
            raise CycleBlockedError("merge_evidence_bound_exceeded")
        evidence = {
            **eligibility.summary(),
            "requirements": asdict(observed.requirements),
            "checks": [asdict(check) for check in observed.checks],
            "review_ids": [opinion.provider_id for opinion in observed.reviews],
            "sources": [{"kind": source.kind, "digest": source.payload_sha256} for source in observed.sources],
        }
        serialized = encode(evidence)
        if len(serialized) > 65536:
            raise CycleBlockedError("merge_evidence_bound_exceeded")
        methods = observed.requirements.allowed_merge_methods
        method = "queue" if observed.requirements.queue_required else next((m for m in ("squash", "merge", "rebase") if m in methods), None)
        if method is None:
            raise CycleBlockedError("merge_method_unavailable")
        return {
            "binding_id": binding.id,
            "binding_revision": binding.revision,
            "accepted_scope": binding.accepted_scope,
            "repo": binding.repo,
            "pr_number": binding.pr_number,
            "provider_repository_id": binding.provider_repository_id,
            "provider_pr_node_id": binding.provider_pr_node_id,
            "head_sha": state.head_sha,
            "base_sha": observed.base_sha,
            "method": method,
            "review_ref": review.artifact_ref,
            "eligibility": evidence,
            "eligibility_digest": hashlib.sha256(serialized.encode()).hexdigest(),
            "sequence": sequence,
        }

    @staticmethod
    def matches(detail, binding, state):
        return (
            detail.get("binding_id"),
            detail.get("binding_revision"),
            detail.get("accepted_scope"),
            detail.get("repo"),
            detail.get("pr_number"),
            detail.get("head_sha"),
            detail.get("provider_repository_id"),
            detail.get("provider_pr_node_id"),
        ) == (
            binding.id,
            binding.revision,
            binding.accepted_scope,
            binding.repo,
            binding.pr_number,
            state.head_sha,
            binding.provider_repository_id,
            binding.provider_pr_node_id,
        )

    @staticmethod
    def receipt(context, binding, state, action):
        data = action.detail or {}
        eligibility = data.get("eligibility") or {}
        before = datetime.fromisoformat(eligibility["observed_at"])
        merged = datetime.fromisoformat(state.merged_at.replace("Z", "+00:00"))
        if (
            action.status not in {"prepared", "unknown", "succeeded"}
            or eligibility.get("state") != "eligible"
            or eligibility.get("reasons") != []
            or before.tzinfo is None
            or not evidence_precedes_merge(before, merged)
            or hashlib.sha256(encode(eligibility).encode()).hexdigest() != data.get("eligibility_digest")
        ):
            raise CycleBlockedError("historical_merge_requirements_unverifiable")
        return MergeReceipt(
            **asdict(context.identity),
            execution_id=context.execution.id,
            flow_id=context.execution.flow_id,
            repo=binding.repo,
            pr_number=binding.pr_number,
            provider_repository_id=binding.provider_repository_id,
            provider_pr_node_id=binding.provider_pr_node_id,
            reviewed_head_sha=state.head_sha,
            reviewed_base_sha=data["base_sha"],
            merge_sha=state.merge_sha,
            review_ref=data["review_ref"],
            operation_key=action.operation_key,
            method=data["method"],
            eligibility_digest=data["eligibility_digest"],
            eligibility_observed_at=before,
            merged_at=merged,
            observed_at=datetime.fromisoformat(state.observed_at),
            adopted=action.status != "succeeded",
        )

    async def persist_authorization(self, context, operation_key, fresh):
        """Commit the final pre-merge decisions, releasing all locks before I/O."""
        async with self.factory() as session:
            loaded = await load_execution(session, identity=context.identity, for_update=True)
            if (
                loaded is None
                or loaded.kind is not OutcomeKind.APPLIED
                or loaded.record is None
                or loaded.record.revision != context.execution.revision
                or loaded.record.pending_action_key != operation_key
            ):
                raise CycleBlockedError("merge_intent_superseded", BlockCode.OWNERSHIP_LOST)
            node, binding, _ = await self.state(session, context)
            if (
                binding.id != fresh["binding_id"]
                or binding.revision != fresh["binding_revision"]
                or binding.accepted_scope != fresh["accepted_scope"]
            ):
                raise CycleBlockedError("merge_scope_changed_before_mutation")
            action = await session.scalar(
                select(OrchestrationAction)
                .where(
                    OrchestrationAction.org_id == node.org_id,
                    OrchestrationAction.execution_id == context.execution.id,
                    OrchestrationAction.operation_key == operation_key,
                    OrchestrationAction.kind == MERGE_KIND,
                )
                .with_for_update()
            )
            if action is None or action.status not in {"prepared", "unknown"}:
                raise CycleBlockedError("merge_intent_already_settled")
            action.detail = {**(action.detail or {}), **fresh}
            await session.commit()

    async def perform(self, context, effect):
        expected = effect.intent.detail
        try:
            async with self.factory() as session:
                node, binding, run_id = await self.state(session, context)
                state = await self.provider.read(binding)
                if state.merged or state.queue_id:
                    return EffectResult(EffectOutcome.UNCERTAIN, detail="Provider changed before mutation; reconcile the existing action.")

                async def reauthorize():
                    current_node, current_binding, current_run = await self.state(session, context)
                    current = await self.provider.read(current_binding)
                    if not self.matches(expected, current_binding, current) or current.base_sha != expected["base_sha"]:
                        raise CycleBlockedError("merge_revision_changed")
                    eligibility, review = await self.eligibility(session, context, current_node, current_binding, current_run, current)
                    if not eligibility.eligible:
                        raise CycleBlockedError("merge_eligibility_withdrawn", BlockCode.AUTHORITY_UNVERIFIABLE)
                    fresh = self.snapshot(current_binding, current, eligibility, review, expected["sequence"])
                    if any(fresh[name] != expected[name] for name in ("method", "review_ref", "head_sha", "base_sha")):
                        raise CycleBlockedError("merge_requirements_changed")
                    await self.persist_authorization(context, effect.intent.operation_key, fresh)

                result = await self.provider.perform(
                    binding, state, method=expected["method"], operation_key=effect.intent.operation_key, reauthorize=reauthorize
                )
                if expected["method"] == "queue" and result.get("queue_entry_id"):
                    return EffectResult(
                        EffectOutcome.SUCCEEDED,
                        receipt_ref=f"github/merge-queue/{result['queue_entry_id']}",
                        detail="Queue admission only; actual merge remains unobserved.",
                    )
                if result.get("merged") is True and result.get("sha"):
                    return EffectResult(
                        EffectOutcome.SUCCEEDED,
                        receipt_ref=f"github/merge-response/{result['sha']}",
                        detail="Merge response received; verify provider state before code completion.",
                    )
                return EffectResult(EffectOutcome.UNCERTAIN, detail="Merge response did not establish the result.")
        except ProviderConflictError:
            return EffectResult(EffectOutcome.FAILED, detail="merge_conflict")
        except CycleBlockedError as exc:
            return EffectResult(EffectOutcome.FAILED, detail=exc.reason)
        except (ProviderUnavailableError, TimeoutError):
            return EffectResult(EffectOutcome.UNCERTAIN, detail="Merge outcome unknown; reconcile before retry.")


class MergeController:
    def __init__(self, factory, services=None):
        self.factory, self.services = factory, services or MergeServices(factory)

    async def observe(self, context):
        try:
            return await self._observe(context)
        except CycleBlockedError as exc:
            return MergeObservation(ObservationKind.BLOCKED, block=block(exc.reason, exc.code))
        except Exception:
            if context.execution.pending_action_key:
                return MergeObservation(ObservationKind.UNCERTAIN, detail="Merge provider outcome remains unknown.")
            return MergeObservation(ObservationKind.BLOCKED, block=block("merge_evidence_unavailable", BlockCode.PROVIDER_UNAVAILABLE))

    async def _observe(self, context):
        async with self.factory() as session:
            node, binding, run = await self.services.state(session, context)
            state = await self.services.provider.read(binding)
            rows = await self.services.actions(session, context)
            matching = [row for row in rows if self.services.matches(row.detail or {}, binding, state)]
            latest = matching[-1] if matching else None
            if state.merged:
                verification = None
                if latest is None:
                    receipt, verification = await self.services.completed_elsewhere(session, context, node, binding, run, state)
                else:
                    receipt = self.services.receipt(context, binding, state, latest)
                return MergeObservation(
                    ObservationKind.SUCCEEDED,
                    operation_key=receipt.operation_key,
                    receipt_ref=f"github/verified-merge/{receipt.merge_sha}",
                    receipt=receipt,
                    snapshot={
                        "binding_id": binding.id,
                        "binding_revision": binding.revision,
                        "accepted_scope": binding.accepted_scope,
                        "code_only": await code_only_delivery(session, context, node),
                        "post_merge_verification": verification,
                    },
                )
            if not state.open:
                raise CycleBlockedError("implementation_pr_closed_without_merge", BlockCode.HUMAN_INPUT_REQUIRED)
            if state.head_sha != binding.head_sha:
                return MergeObservation(ObservationKind.FAILED, snapshot={"return_to_review": True}, detail="Reviewed head changed.")
            from .run_reports import OrchestrationRunReport

            report = await session.get(OrchestrationRunReport, run)
            from .review_assignment import reviewer_owns_delivery

            if await reviewer_owns_delivery(session, org_id=node.org_id, node_id=node.id, run_id=run):
                # Reviewer delivery owns mutation; the engine only adopts the
                # independently verified merged state above.
                raw = await (await self.services.authority_for(context)).protected(node.org_id, run)
                if (report and not report.terminal_receipt) or (not report and raw and raw.get("status") != {"S": "completed"}):
                    return MergeObservation(ObservationKind.WAITING, detail="Reviewer is delivering the merge.")
                raise CycleBlockedError("reviewer_merge_not_delivered", BlockCode.HUMAN_INPUT_REQUIRED)
            if state.queue_id:
                if latest is None or latest.detail.get("method") != "queue":
                    raise CycleBlockedError("merge_queue_admission_unattributed")
                return MergeObservation(ObservationKind.WAITING, detail="Admitted to the merge queue; awaiting the actual merge.")
            if latest is not None and (latest.detail or {}).get("observation") == "merge_conflict":
                return MergeObservation(ObservationKind.READY, snapshot={"repair_conflict": latest.operation_key})
            eligibility, review = await self.services.eligibility(session, context, node, binding, run, state)
            if not eligibility.eligible:
                if EligibilityReason.HEAD_CHANGED in eligibility.reasons:
                    return MergeObservation(ObservationKind.FAILED, snapshot={"return_to_review": True}, detail="Fresh review required.")
                if EligibilityReason.CONFLICT in eligibility.reasons or EligibilityReason.BASE_OUTDATED in eligibility.reasons:
                    return MergeObservation(ObservationKind.READY, snapshot={"repair_conflict": f"provider:{state.head_sha}:{state.base_sha}"})
                if eligibility.state is EligibilityState.WAITING:
                    return MergeObservation(ObservationKind.WAITING, detail="Waiting for current repository checks and mergeability.")
                if set(eligibility.reasons) == {EligibilityReason.REQUIRED_CHECK_FAILED} and eligibility.observation is not None:
                    failures = [
                        f"{check.name}: {check.state}"
                        for check in eligibility.observation.checks
                        if check.state not in {"success", "neutral", "skipped", "pending"}
                    ]
                    return MergeObservation(
                        ObservationKind.READY,
                        snapshot={
                            "repair_conflict": f"checks:{state.head_sha}:{hashlib.sha256(review.artifact_ref.encode()).hexdigest()}",
                            "repair_reason": "Repair the failing CI checks for this story and rerun validation: " + "; ".join(failures)[:2000],
                        },
                    )
                reason = eligibility.authority_reason or ""
                code = BlockCode.DEPENDENCY_UNSATISFIED
                if "attempt" in reason or "wall_clock" in reason:
                    code = BlockCode.ATTEMPTS_EXHAUSTED
                elif "spend" in reason or "budget" in reason:
                    code = BlockCode.BUDGET_EXHAUSTED
                elif "human_gate" in reason:
                    code = BlockCode.HUMAN_GATE_REQUIRED
                elif any(value in eligibility.reasons for value in (EligibilityReason.AUTHORITY_DENIED, EligibilityReason.AUTHORITY_UNAVAILABLE)):
                    code = BlockCode.AUTHORITY_UNVERIFIABLE
                raise CycleBlockedError("merge_ineligible:" + ",".join(value.value for value in eligibility.reasons), code)
            snapshot = self.services.snapshot(binding, state, eligibility, review, len(rows) + 1)
            if latest is not None and latest.status in {"prepared", "unknown"} and latest.detail.get("base_sha") == state.base_sha:
                snapshot = dict(latest.detail)
                snapshot["replay_key"] = latest.operation_key
            return MergeObservation(ObservationKind.READY, snapshot=snapshot)

    def decide(self, context, observation):
        if observation.kind is ObservationKind.BLOCKED:
            return HandlerDecision(DecisionKind.BLOCK, block=observation.block)
        data = dict(getattr(observation, "snapshot", None) or {})
        if getattr(observation, "receipt", None) is not None:

            async def settle(session, current):
                if data.get("post_merge_verification") is not None:
                    key = observation.receipt.operation_key
                    prepared = await prepare_action(
                        session,
                        identity=current.identity,
                        intent=ActionIntent(
                            key, MERGE_KIND, detail={"post_merge_verification": data["post_merge_verification"], "merge_mutation_performed": False}
                        ),
                    )
                    if prepared.kind is not OutcomeKind.APPLIED:
                        raise CycleBlockedError("observed_merge_settlement_changed")
                    recorded = await record_observation(
                        session,
                        identity=current.identity,
                        observation=Observation(key, ObservedOutcome.SUCCEEDED, receipt_ref=observation.receipt_ref),
                    )
                    if recorded.kind is not OutcomeKind.APPLIED:
                        raise CycleBlockedError("observed_merge_settlement_changed")
                await settle_merge(session, current, observation.receipt, data)

            if data.get("code_only") is True:
                return HandlerDecision(
                    DecisionKind.CONCLUDE,
                    settlement=settle,
                    progress_note="Verified code delivery complete. Dependent evaluations retain their separate acceptance requirements.",
                )
            return HandlerDecision(
                DecisionKind.ADVANCE,
                phase=ExecutionPhase.DEPLOYMENT_PENDING,
                settlement=settle,
                progress_note="Code merged; deployment and evaluation remain separate requirements.",
            )
        if data.get("return_to_review"):
            return HandlerDecision(DecisionKind.ADVANCE, phase=ExecutionPhase.AWAITING_REVIEW, progress_note="Head changed; obtaining fresh review.")
        if data.get("repair_conflict"):

            async def settle(session, current):
                key = OperationIdentity.from_context(current, "merge_repair_request", data["repair_conflict"]).key
                prepared = await prepare_action(
                    session,
                    identity=current.identity,
                    intent=ActionIntent(
                        key,
                        "merge_repair_request",
                        detail={
                            "reason": data.get("repair_reason")
                            or "Resolve the current merge conflict or update the out-of-date base within accepted scope."
                        },
                    ),
                )
                if prepared.kind is not OutcomeKind.APPLIED:
                    raise CycleBlockedError("merge_repair_handoff_changed")
                recorded = await record_observation(
                    session,
                    identity=current.identity,
                    observation=Observation(
                        key,
                        ObservedOutcome.SUCCEEDED,
                        receipt_ref="merge-repair/" + hashlib.sha256(key.encode()).hexdigest(),
                    ),
                )
                if recorded.kind is not OutcomeKind.APPLIED:
                    raise CycleBlockedError("merge_repair_handoff_changed")

            return HandlerDecision(
                DecisionKind.ADVANCE,
                phase=ExecutionPhase.REPAIRING,
                settlement=settle,
                progress_note=data.get("repair_reason") or "Merge conflict requires repair within the existing allowance.",
            )
        if observation.kind is not ObservationKind.READY:
            return HandlerDecision(DecisionKind.WAIT, next_check_at=context.now + timedelta(seconds=60), progress_note=observation.detail)
        replay = data.pop("replay_key", None)
        key = (
            replay
            or OperationIdentity.from_context(
                context, MERGE_KIND, data["binding_id"], str(data["binding_revision"]), data["head_sha"], data["base_sha"], str(data["sequence"])
            ).key
        )
        return HandlerDecision(
            DecisionKind.EFFECT,
            effect=EffectRequest(ActionIntent(key, MERGE_KIND, detail=data), Action.MERGE),
            progress_note="Merging the reviewed revision through repository requirements.",
        )

    async def perform(self, context, effect):
        return await self.services.perform(context, effect)


async def settle_merge(session, context, receipt, snapshot):
    """No provider I/O: atomically commit code completion and continuation."""
    node = await session.scalar(
        select(OrchestrationNode)
        .where(
            OrchestrationNode.org_id == context.identity.org_id,
            OrchestrationNode.id == context.identity.node_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    binding = await active_binding_for_node(session, org_id=context.identity.org_id, node_id=context.identity.node_id, attempt=context.identity.cycle)
    if (
        node is None
        or node.kind != "story"
        or node.attempts != context.identity.cycle
        or node.state not in {"running", "awaiting_merge"}
        or binding is None
        or binding.id != snapshot["binding_id"]
        or binding.revision != snapshot["binding_revision"]
        or binding.accepted_scope != snapshot["accepted_scope"]
        or binding.head_sha != receipt.reviewed_head_sha
        or not binding_scope_matches(binding, node)
    ):
        raise CycleBlockedError("merge_settlement_scope_changed")
    flow = await session.get(OrchestrationFlow, node.flow_id)
    if flow is None or flow.state not in {"pending", "running"}:
        raise CycleBlockedError("merge_flow_gate_changed")
    if bool(snapshot.get("code_only")) != await code_only_delivery(session, context, node):
        raise CycleBlockedError("code_delivery_contract_changed")
    move = transition(NodeState(node.state), NodeState.PASSED, actor_kind=ActorKind.SERVICE, reason="Verified bound PR merge")
    if not move.allowed:
        raise CycleBlockedError("merge_settlement_gate_denied")
    action = await session.scalar(
        select(OrchestrationAction).where(
            OrchestrationAction.org_id == node.org_id,
            OrchestrationAction.execution_id == context.execution.id,
            OrchestrationAction.operation_key == receipt.operation_key,
        )
    )
    if action is None:
        raise CycleBlockedError("merge_intent_missing")
    action.detail = {**(action.detail or {}), "merge_receipt": receipt.model_dump(mode="json")}
    session.add(
        OrchestrationDecision(
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind="result_observed",
            actor_id="system:merge-controller",
            actor_kind=ActorKind.SERVICE.value,
            actor_role="engine",
            from_state=node.state,
            to_state=NodeState.PASSED.value,
            reason=encode({"attempt": node.attempts, "merge_receipt": receipt.model_dump(mode="json")}),
        )
    )
    node.state = NodeState.PASSED.value
    node.updated_at = datetime.now(UTC)
    await session.flush()


def handlers(factory):
    return dict.fromkeys(PHASES, MergeController(factory))


def bounded_receipt_summary(detail):
    try:
        receipt = MergeReceipt.model_validate((detail or {})["merge_receipt"])
    except (KeyError, TypeError, ValueError):
        return None
    return receipt.model_dump(
        mode="json",
        include={
            "reviewed_head_sha",
            "reviewed_base_sha",
            "merge_sha",
            "method",
            "merged_at",
            "observed_at",
            "adopted",
            "verification_kind",
        },
    )
