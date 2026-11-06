"""Tick-owned repository evaluation without workers, issue claims, or deployment claims.

The decision ledger is the authority for this read-only observer. Source story
actions retain their real execution/claim identities; an external evidence read
does not manufacture an execution or take ownership of the referenced issue.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal

from pydantic import Field, StrictBool, model_serializer, model_validator
from sqlalchemy import select

from .dispatch import graph_address
from .evaluation_plan import accepted_evaluation
from .execution_policy import Action, CredentialScope, ResourceRef, authorize_action
from .merge_controller import MERGE_KIND, MergeReceipt
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
)
from .pr_bindings import active_binding_for_node, binding_scope_matches
from .repository_evaluation_contract import (
    Contract,
    DeploymentSourceReceipt,
    Digest,
    Name,
    PullReceipt,
    WorkflowReceipt,
    canonical,
    harness_digest,
    native_harness_compatible,
)
from .repository_evaluation_diagnostics import ACTOR, evaluation_scope, scoped_refusal
from .repository_evaluation_provider import RepositoryEvidenceProvider, require
from .review_cycle import CycleBlockedError
from .state import ActorKind, NodeState, transition

logger = logging.getLogger(__name__)
OBSERVATION_TIMEOUT_SECONDS = 15


def repository_mode_supported(spec):
    return (
        spec.evidence_schema == "multi-repository-evaluation/v1"
        and (
            (spec.mode == "repository" and spec.harness.adapter == "engine-repository-evidence-v1" and spec.harness.operation == "collect")
            or (
                spec.mode == "deployed"
                and spec.probe is not None
                and spec.harness.adapter == "engine-github-actions-probe-v1"
                and spec.harness.operation == "probe"
            )
        )
        and spec.harness.harness_digest == harness_digest()
    )


def scoped_binding(org_id, verified):
    return SimpleNamespace(
        org_id=org_id,
        installation_id=verified.installation_id,
        repo=verified.full_name,
        provider_repository_id=verified.repository_id,
    )


def deployed_target_contract(sources, environment):
    """Compare proven physical targets with the one accepted runtime contract."""
    require(environment is not None, "predecessor_target_contract_missing")
    targets = {}
    for source in sources:
        for target in source.get("deployment_targets", []):
            require(
                target.get("evidence", {}).get("source") == "registered-aws-role:" + environment.connection_id,
                f"predecessor_target_unverified:{source['address']}",
            )
            identity = {key: target.get(key) for key in ("provider", "account_id", "region", "resource_kind", "resource_id")}
            require(all(identity.values()), f"predecessor_target_incomplete:{source['address']}")
            targets[canonical(identity)] = identity
        require(source.get("deployment_targets"), f"predecessor_target_missing:{source['address']}")
    document = {"runtime_contract": environment.runtime_contract, "targets": [targets[key] for key in sorted(targets)]}
    require(hashlib.sha256(canonical(document).encode()).hexdigest() == environment.target_contract_hash, "predecessor_target_contract_mismatch")


class RepositoryEvaluationReceipt(Contract):
    evidence_schema: Literal["repository-evaluation-receipt/v1"] = "repository-evaluation-receipt/v1"
    org_id: Name
    flow_id: Name
    node_id: Name
    cycle: int = Field(gt=0)
    accepted_plan_version: int = Field(gt=0)
    accepted_plan_hash: Digest
    acceptance_decision_id: Name | None = None
    evaluation_policy_hash: Digest
    specification_hash: Digest
    harness_sha256: Digest
    collector_harness_sha256: Digest | None = None
    source_snapshot_hash: Digest
    observed_at: datetime
    # Provider-specific records retain exact PR/check/run/attempt/archive identities.
    pull_requests: list[PullReceipt] = Field(max_length=160)
    # Operations predecessors, proven by their accepted deployment receipts (#7880).
    deployments: list[DeploymentSourceReceipt] = Field(default_factory=list, max_length=128)
    workflows: list[WorkflowReceipt] = Field(max_length=16)
    mandatory_passed: StrictBool
    live_attestation: Literal[False] = False

    @model_serializer(mode="wrap")
    def preserve_existing_receipts(self, handler):
        document = handler(self)
        if not self.deployments:
            document.pop("deployments", None)
        return document

    @model_validator(mode="after")
    def proof(self):
        if not self.pull_requests and not self.workflows and not self.deployments:
            raise ValueError("evidence receipt cannot be empty")
        if self.mandatory_passed != all(item.passed for row in self.workflows for item in row.criteria):
            raise ValueError("verdict must follow mandatory criterion outcomes")
        if self.observed_at.tzinfo is None:
            raise ValueError("observation timestamp must be aware")
        return self


def verify_refreshed_deployment(original, renewed, data):
    from .repository_producer import digest

    old = original.model_dump(mode="json")
    fresh = renewed.model_dump(mode="json")
    for key in ("operation_key", "components", "observed_at", "valid_until"):
        old.pop(key)
        fresh.pop(key)

    def component_identity(component):
        return component.model_dump(exclude={"observed_at", "evidence_ref", "pod_uids"})

    require(
        fresh == old
        and data.get("original_operation_key") == original.operation_key
        and data.get("original_digest") == digest(original.model_dump(mode="json"))
        and sorted(map(component_identity, renewed.components), key=str) == sorted(map(component_identity, original.components), key=str),
        "evaluation_source_refresh_scope_changed",
    )


async def correction_source_refresh(session, node, plan, parent, address, correction_key=None, *, receipt_keys=()):
    from .deployment_runtime_contract import DeploymentReceipt
    from .evaluation_correction_state import CORRECTION_KIND

    rows = list(
        await session.execute(
            select(OrchestrationAction, OrchestrationExecution)
            .join(OrchestrationExecution, OrchestrationExecution.id == OrchestrationAction.execution_id)
            .where(
                OrchestrationAction.org_id == node.org_id,
                OrchestrationAction.kind == "multi_repository_source_refresh",
                OrchestrationExecution.org_id == node.org_id,
                OrchestrationExecution.flow_id == node.flow_id,
                OrchestrationExecution.node_id == node.id,
            )
            .order_by(OrchestrationAction.created_at.desc())
            .limit(101)
        )
    )
    require(len(rows) <= 100, "evaluation_source_refresh_history_limit")
    for action, execution in rows:
        data = action.detail or {}
        if data.get("source_node_id") != parent.id:
            continue
        if correction_key is not None and data.get("correction_operation_key") != correction_key:
            continue
        if receipt_keys and action.operation_key not in receipt_keys:
            continue
        require(
            action.status == "succeeded"
            and action.receipt_ref == action.operation_key
            and execution.cycle <= node.attempts
            and execution.accepted_plan_version == plan.version
            and data.get("source_address") == address
            and data.get("product_id") == plan.plan_document.get("product_id")
            and data.get("flow_id") == node.flow_id
            and data.get("accepted_plan_hash") == plan.plan_hash,
            "evaluation_source_refresh_scope_changed",
        )
        try:
            receipt = DeploymentReceipt.model_validate(data["deployment_receipt"])
        except (ValueError, KeyError, TypeError):
            raise CycleBlockedError("repository_evaluation_source_refresh_unverifiable") from None
        require(receipt.operation_key == action.operation_key, "evaluation_source_refresh_scope_changed")
        owner = await session.scalar(
            select(OrchestrationAction)
            .join(OrchestrationExecution, OrchestrationExecution.id == OrchestrationAction.execution_id)
            .where(
                OrchestrationAction.org_id == node.org_id,
                OrchestrationAction.kind == CORRECTION_KIND,
                OrchestrationAction.status == "succeeded",
                OrchestrationAction.operation_key == data["correction_operation_key"],
                OrchestrationExecution.node_id == node.id,
            )
        )
        require(
            owner is not None
            and owner.execution_id == execution.id
            and owner.detail["source_authorization"]["product_id"] == data["product_id"]
            and owner.detail["source_authorization"]["flow_id"] == data["flow_id"]
            and owner.detail["source_authorization"]["address"] != address,
            "evaluation_source_refresh_correction_changed",
        )
        return receipt, data
    return None


async def verify_original_after_refresh(session, node, plan, parent, address, failed_source, failed_at, original):
    require(
        failed_source["node_id"] == parent.id and failed_source["deployed_revision"] == original.actual_revision,
        "evaluation_source_refresh_original_changed",
    )
    if failed_source["deployment_receipt_key"] == original.operation_key:
        return
    previous = await correction_source_refresh(session, node, plan, parent, address, receipt_keys=(failed_source["deployment_receipt_key"],))
    require(previous is not None, "evaluation_source_refresh_original_changed")
    proof, data = previous
    verify_refreshed_deployment(original, proof, data)
    require(proof.observed_at <= failed_at < proof.valid_until, "evaluation_source_refresh_original_changed")


async def sources_for(session, node, plan, spec, *, lock=False, preview_gate_id=None, observed_at=None, correction=None):
    multi = spec.evidence_schema == "multi-repository-evaluation/v1"
    query = (
        select(OrchestrationNode)
        .join(OrchestrationEdge, OrchestrationEdge.from_node_id == OrchestrationNode.id)
        .where(
            OrchestrationEdge.org_id == node.org_id,
            OrchestrationEdge.to_node_id == node.id,
            OrchestrationNode.org_id == node.org_id,
            OrchestrationNode.flow_id == node.flow_id,
        )
        .order_by(OrchestrationNode.id)
        .limit(129)
        .execution_options(populate_existing=True)
    )
    parents = list(await session.scalars(query.with_for_update(of=OrchestrationNode) if lock else query))
    require(
        len(parents) <= 128
        and all(
            parent.state == "passed" or (parent.id == preview_gate_id and parent.kind == "gate" and parent.state == "awaiting_gate")
            for parent in parents
        ),
        "predecessors_not_complete",
    )
    flow = await session.get(OrchestrationFlow, node.flow_id)
    stories = {graph_address(parent, flow_slug=flow.slug): parent for parent in parents if parent.kind == "story"}
    if multi:
        missing = next((item.address for item in spec.predecessors if item.address not in stories), None)
        require(missing is None, f"repository_evaluation_source_missing:{missing}")
    require(set(stories) == {item.address for item in spec.predecessors}, "predecessor_set_changed")
    sources = []
    for expected in spec.predecessors:
        parent = stories[expected.address]
        if correction is not None and expected.address == correction[0]:
            require(multi and spec.mode == "deployed", "evaluation_correction_source_mode_changed")
            parent = correction[1]
        if multi and spec.mode == "deployed":
            require(expected.evidence_kind == "deployment", f"predecessor_deployment_required:{expected.address}")
            require(parent.provider_repository_id == expected.repository_id, "predecessor_repository_changed")
            source = await deployment_source(
                session,
                node=node,
                parent=parent,
                plan=plan,
                spec=spec,
                expected=expected,
                observed_at=observed_at,
                allow_updated_receipts=correction is not None,
                refresh=(
                    await correction_source_refresh(
                        session,
                        node,
                        plan,
                        parent,
                        expected.address,
                        correction[2] if correction is not None and len(correction) > 2 else None,
                        receipt_keys=spec.environment.deployment_receipt_keys if observed_at is not None else (),
                    )
                    if (
                        (correction is not None and len(correction) > 2 and expected.address != correction[0])
                        or (node.attempts > 1 and observed_at is not None)
                    )
                    else None
                ),
            )
            source["repository_id"] = expected.repository_id
            sources.append(source)
            continue
        binding = await active_binding_for_node(session, org_id=node.org_id, node_id=parent.id, attempt=parent.attempts)
        if binding is None and await _operations_story(session, parent):
            require(not multi or expected.evidence_kind == "deployment", "predecessor_evidence_kind_changed")
            # An operate-role story delivers a deployment, not a pull request: its
            # accepted deployment receipt is the source (#7880, TLS-X02 live-2).
            if multi:
                require(parent.provider_repository_id == expected.repository_id, "predecessor_repository_changed")
            source = await deployment_source(session, node=node, parent=parent, plan=plan, spec=spec, expected=expected)
            if multi:
                source["repository_id"] = expected.repository_id
            sources.append(source)
            continue
        require(binding is not None and binding_scope_matches(binding, parent), "predecessor_binding_missing_or_changed")
        require(not multi or expected.evidence_kind == "merge", "predecessor_evidence_kind_changed")
        require(multi or expected.required_checks, "predecessor_checks_missing")
        if multi:
            require(
                binding.provider_repository_id == expected.repository_id
                and parent.provider_repository_id == expected.repository_id
                and parent.repository_identity == binding.repo
                and type(parent.issue_ref) is str
                and parent.issue_ref.isdecimal(),
                "predecessor_repository_changed",
            )
        else:
            require(
                binding.repo == spec.runner.repository and binding.provider_repository_id == spec.runner.repository_id,
                "predecessor_repository_changed",
            )
        if getattr(expected, "accepted_merge_decision_id", None) is not None:
            from .accepted_merge_evidence import accepted_merge_source

            source = await accepted_merge_source(session, parent=parent, plan=plan, binding=binding, decision_id=expected.accepted_merge_decision_id)
            source.update(address=expected.address, required_checks=[item.model_dump() for item in expected.required_checks])
            if multi:
                source["repository_id"] = expected.repository_id
            if getattr(spec, "qualification", None) is not None and str(parent.issue_ref).isdigit():
                source["issue_number"] = int(parent.issue_ref)
            sources.append(source)
            continue
        # A later evaluation-only amendment may consume earlier delivered code.
        # Its exact original plan/execution stays on the source receipt; the
        # currently accepted node scope must still match the immutable binding.
        rows = list(
            await session.execute(
                select(OrchestrationAction, OrchestrationExecution)
                .join(OrchestrationExecution, OrchestrationExecution.id == OrchestrationAction.execution_id)
                .where(
                    OrchestrationExecution.org_id == node.org_id,
                    OrchestrationExecution.flow_id == node.flow_id,
                    OrchestrationExecution.node_id == parent.id,
                    OrchestrationExecution.cycle == parent.attempts,
                    OrchestrationExecution.accepted_plan_version <= plan.version,
                    OrchestrationAction.org_id == node.org_id,
                    OrchestrationAction.kind == MERGE_KIND,
                    OrchestrationAction.status == "succeeded",
                )
                .limit(101)
            )
        )
        require(len(rows) <= 100, "merge_history_limit")
        matches = []
        for action, execution in rows:
            raw = (action.detail or {}).get("merge_receipt")
            if raw:
                receipt = MergeReceipt.model_validate(raw)
                require(
                    receipt.org_id == node.org_id
                    and receipt.flow_id == node.flow_id
                    and receipt.node_id == parent.id
                    and receipt.execution_id == execution.id
                    and receipt.operation_key == action.operation_key
                    and receipt.accepted_plan_version == execution.accepted_plan_version
                    and receipt.cycle == parent.attempts
                    and receipt.claim_id == execution.claim_id,
                    "merge_receipt_scope_changed",
                )
                if receipt.claim_generation != execution.claim_generation:
                    from .delivery_recovery import verified_merge_history
                    from .execution_state import ExecutionIdentity

                    await verified_merge_history(
                        session,
                        identity=ExecutionIdentity(
                            execution.org_id,
                            execution.node_id,
                            execution.cycle,
                            execution.accepted_plan_version,
                            execution.claim_id,
                            execution.claim_generation,
                        ),
                        node=parent,
                        binding=binding,
                        receipt=receipt,
                    )
                matches.append(receipt)
        require(len(matches) == 1, "merge_receipt_missing_or_ambiguous")
        receipt = matches[0]
        require(
            receipt.repo == binding.repo
            and receipt.provider_repository_id == binding.provider_repository_id
            and receipt.pr_number == binding.pr_number
            and receipt.provider_pr_node_id == binding.provider_pr_node_id
            and receipt.reviewed_head_sha == binding.head_sha,
            f"repository_evaluation_source_stale:{expected.address}" if multi else "merge_binding_changed",
        )
        sources.append(
            dict(
                **(
                    {"issue_number": int(parent.issue_ref)}
                    if getattr(spec, "qualification", None) is not None and str(parent.issue_ref).isdigit()
                    else {}
                ),
                **({"repository_id": expected.repository_id} if multi else {}),
                address=expected.address,
                node_id=parent.id,
                attempt=parent.attempts,
                accepted_plan_version=receipt.accepted_plan_version,
                execution_id=receipt.execution_id,
                binding_id=binding.id,
                binding_revision=binding.revision,
                pr_number=receipt.pr_number,
                head_sha=receipt.reviewed_head_sha,
                merge_sha=receipt.merge_sha,
                provider_pr_node_id=receipt.provider_pr_node_id,
                review_ref=receipt.review_ref,
                merge_operation_key=receipt.operation_key,
                required_checks=[item.model_dump() for item in expected.required_checks],
            )
        )
    if not multi:
        sources.extend(item.model_dump() for item in spec.external_pull_requests)
    return sources


async def multi_repository_sources(session, node, plan, spec, *, lock=False, observed_at=None, correction=None):
    from .repository_binding import RepositoryBindingError, resolve_story_repository

    require(spec.mode in {"repository", "deployed"}, "evaluation_mode_unavailable")
    flow = await session.get(OrchestrationFlow, node.flow_id)
    require(
        flow is not None and flow.org_id == node.org_id and flow.product_id is not None and flow.product_id == plan.plan_document.get("product_id"),
        "source_flow_changed",
    )
    require(
        node.repository_identity == spec.harness.repository
        and node.provider_repository_id == spec.harness.repository_id
        and (node.repository_connection or {}).get("installation_id") == spec.harness.installation_id,
        "harness_binding_changed",
    )
    if correction is None and spec.mode == "deployed" and node.attempts > 1:
        from .evaluation_correction_state import retest_deployment

        retests = list(
            await session.scalars(
                select(OrchestrationAction)
                .join(OrchestrationExecution, OrchestrationExecution.id == OrchestrationAction.execution_id)
                .where(
                    OrchestrationAction.org_id == node.org_id,
                    OrchestrationAction.kind == "multi_repository_context",
                    OrchestrationAction.status == "succeeded",
                    OrchestrationExecution.org_id == node.org_id,
                    OrchestrationExecution.node_id == node.id,
                    OrchestrationExecution.cycle == node.attempts,
                )
                .limit(2)
            )
        )
        require(len(retests) <= 1, "evaluation_retest_context_ambiguous")
        if retests and retests[0].detail.get("correction_operation_key"):
            deployed = await retest_deployment(session, node, retests[0], plan.version)
            correction = (
                retests[0].detail["affected_source_address"],
                deployed[1],
                retests[0].detail["correction_operation_key"],
            )
    sources = await sources_for(session, node, plan, spec, lock=lock, observed_at=observed_at, correction=correction)
    if spec.mode == "deployed":
        require(
            spec.environment is not None
            and sorted(source["deployment_receipt_key"] for source in sources) == sorted(spec.environment.deployment_receipt_keys),
            "predecessor_deployment_set_mismatch",
        )
        deployed_target_contract(sources, spec.environment)
        if spec.probe is not None:
            require(
                all(
                    (target.get("account_id"), target.get("region")) == (spec.probe.account_id, spec.probe.region)
                    for source in sources
                    for target in source["deployment_targets"]
                ),
                "predecessor_probe_target_mismatch",
            )
    missing = next((item.address for item in spec.predecessors if item.address not in {source["address"] for source in sources}), None)
    require(missing is None, f"repository_evaluation_source_missing:{missing}")
    require(len(sources) == len(spec.predecessors), "predecessor_set_changed")
    bindings = {}
    for source, expected in zip(sources, spec.predecessors):
        require(source.get("repository_id") == expected.repository_id, "predecessor_repository_changed")
        parent = (
            correction[1] if correction is not None and correction[0] == expected.address else await session.get(OrchestrationNode, source["node_id"])
        )
        require(parent is not None and parent.org_id == node.org_id and parent.flow_id == node.flow_id, "source_flow_changed")
        if correction is not None and correction[0] == expected.address:
            require(parent.id == source["node_id"], "evaluation_correction_source_changed")
        require(parent.issue_ref is not None and parent.issue_ref.isdecimal(), "predecessor_issue_missing")
        try:
            binding = await resolve_story_repository(
                session,
                tenant_id=node.org_id,
                name=parent.repository_identity,
                issue=int(parent.issue_ref),
                operation="evaluate",
                repository_id=expected.repository_id,
                installation_id=(parent.repository_connection or {}).get("installation_id"),
            )
        except RepositoryBindingError as error:
            raise CycleBlockedError(f"repository_evaluation_source_{error.code}:{expected.address}") from None
        require(
            binding.repository_id == expected.repository_id
            and binding.installation_id == (parent.repository_connection or {}).get("installation_id"),
            "predecessor_repository_changed",
        )
        bindings[expected.address] = binding
    try:
        harness = await resolve_story_repository(
            session,
            tenant_id=node.org_id,
            name=spec.harness.repository,
            issue=int(node.issue_ref),
            operation="evaluate",
            repository_id=spec.harness.repository_id,
            installation_id=spec.harness.installation_id,
        )
    except RepositoryBindingError as error:
        raise CycleBlockedError(f"repository_evaluation_harness_{error.code}") from None
    return sources, bindings, harness


async def multi_source_receipts(session, node, plan, spec, sources, observations):
    from .evaluation_contract import multi_repository_models
    from .plan_lineage import ancestor_plan

    models = multi_repository_models()
    require(len(sources) == len(observations) == len(spec.predecessors), "predecessor_set_changed")
    resolved = []
    for expected, source, observed in zip(spec.predecessors, sources, observations):
        require(source["address"] == expected.address and source.get("repository_id") == expected.repository_id, "predecessor_set_changed")
        execution = await session.get(OrchestrationExecution, source["execution_id"])
        require(
            execution is not None
            and execution.org_id == node.org_id
            and execution.flow_id == node.flow_id
            and execution.node_id == source["node_id"]
            and execution.cycle == source["attempt"]
            and execution.accepted_plan_version == source["accepted_plan_version"]
            and execution.claim_generation > 0,
            "predecessor_execution_changed",
        )
        ancestor = await ancestor_plan(session, plan, execution.accepted_plan_version, node_id=source["node_id"])
        require(ancestor is not None, "predecessor_plan_changed")
        require(
            len(observed["receipts"]) == len(expected.required_checks)
            and all(
                receipt["name"] == check.name
                and receipt["app_id"] == check.app_id
                and receipt["head_sha"] == source["head_sha"]
                and receipt["conclusion"] == "success"
                for receipt, check in zip(observed["receipts"], expected.required_checks)
            ),
            "predecessor_checks_changed",
        )
        data = dict(
            address=expected.address,
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=source["node_id"],
            accepted_plan_hash=ancestor.plan_hash,
            accepted_plan_version=execution.accepted_plan_version,
            execution_id=execution.id,
            cycle=execution.cycle,
            claim_generation=execution.claim_generation,
            source_binding_revision=hashlib.sha256(
                canonical(
                    {
                        key: source[key]
                        for key in (
                            ("binding_id", "binding_revision") if expected.evidence_kind == "merge" else ("deployment_receipt_key", "execution_id")
                        )
                    }
                ).encode()
            ).hexdigest(),
            repository_id=expected.repository_id,
            checks=observed["receipts"],
        )
        if expected.evidence_kind == "merge":
            data.update(
                evidence_kind="merge",
                pull_request_node_id=source["provider_pr_node_id"],
                pull_request_number=source["pr_number"],
                reviewed_head_sha=source["head_sha"],
                merge_sha=source["merge_sha"],
            )
            resolved.append(models.MergeInput.model_validate(data))
        else:
            data.update(
                evidence_kind="deployment",
                deployment_receipt_key=source["deployment_receipt_key"],
                operation_key=source["deployment_receipt_key"],
                deployed_revision=source["head_sha"],
            )
            resolved.append(models.DeploymentInput.model_validate(data))
    return resolved


async def _operations_story(session, parent):
    from .accepted_node import accepted_node_contract

    contract = await accepted_node_contract(session, parent)
    executor = contract.node.executor if contract is not None else None
    return executor is not None and executor.role == "operate"


async def deployment_source(session, *, node, parent, plan, spec, expected, observed_at=None, allow_updated_receipts=False, refresh=None):
    """The source an operations predecessor contributes: its runtime-verified deployment.

    Mirrors the merge-receipt path: the parent's current cycle must hold exactly one
    accepted deployment (``deployment_verification`` with a scope-matching
    ``DeploymentReceipt`` marked delivery-complete); the deployed revision stands where
    a pull request's merge commit stands, so workflow revisions declared against this
    predecessor and the ancestry checks keep working unchanged. Several receipts for
    one cycle (a worker re-verified the same release) must agree on the revision.
    """
    from pydantic import ValidationError

    from .deployment_controller import RECEIPT_LIFETIME
    from .deployment_runtime_contract import DeploymentReceipt

    rows = list(
        await session.execute(
            select(OrchestrationAction, OrchestrationExecution)
            .join(OrchestrationExecution, OrchestrationExecution.id == OrchestrationAction.execution_id)
            .where(
                OrchestrationExecution.org_id == node.org_id,
                OrchestrationExecution.flow_id == node.flow_id,
                OrchestrationExecution.node_id == parent.id,
                OrchestrationExecution.cycle == parent.attempts,
                OrchestrationExecution.accepted_plan_version <= plan.version,
                OrchestrationAction.org_id == node.org_id,
                OrchestrationAction.kind == "deployment_verification",
                OrchestrationAction.status == "succeeded",
            )
            .limit(101)
        )
    )
    require(len(rows) <= 100, "deployment_history_limit")
    receipts = []
    for action, execution in rows:
        raw = (action.detail or {}).get("deployment_receipt")
        if not isinstance(raw, dict):
            continue
        try:
            receipt = DeploymentReceipt.model_validate(raw)
        except ValidationError:
            continue
        if not receipt.delivery_complete:
            continue
        require(
            action.receipt_ref == action.operation_key
            and receipt.org_id == node.org_id
            and receipt.flow_id == node.flow_id
            and receipt.node_id == parent.id
            and receipt.execution_id == execution.id
            and receipt.operation_key == action.operation_key
            and receipt.accepted_plan_version == execution.accepted_plan_version
            and receipt.cycle == parent.attempts
            and receipt.claim_id == execution.claim_id
            and receipt.claim_generation == execution.claim_generation
            and receipt.delivery_complete is True
            and not receipt.docs_only
            and receipt.components,
            "deployment_receipt_scope_changed",
        )
        receipts.append(receipt)
    require(receipts, "predecessor_deployment_receipt_missing")
    if observed_at is not None:
        keys = {refresh[1]["original_operation_key"]} if refresh is not None else set(spec.environment.deployment_receipt_keys)
        receipts = [receipt for receipt in receipts if receipt.operation_key in keys]
        require(len(receipts) == 1, "predecessor_deployment_set_mismatch")
    require(len({(item.repo, item.actual_revision) for item in receipts}) == 1, "predecessor_deployment_receipt_ambiguous")
    receipt = max(receipts, key=lambda item: item.observed_at)
    if refresh is not None:
        renewed, data = refresh
        verify_refreshed_deployment(receipt, renewed, data)
        receipt = renewed
    if spec.evidence_schema == "multi-repository-evaluation/v1" and spec.mode == "deployed":
        require(
            receipt.observed_at <= (observed_at or datetime.now(UTC)) < receipt.valid_until
            and receipt.valid_until - receipt.observed_at <= RECEIPT_LIFETIME,
            "predecessor_deployment_expired",
        )
        if not allow_updated_receipts:
            require(receipt.operation_key in spec.environment.deployment_receipt_keys, "predecessor_deployment_set_mismatch")
    expected_repo = parent.repository_identity if spec.evidence_schema == "multi-repository-evaluation/v1" else spec.runner.repository
    require(receipt.repo == expected_repo, "predecessor_repository_changed")
    artifact_target = {}
    from .accepted_node import accepted_node_contract
    from .repository_evaluation_contract import IdentityProofArtifactTarget

    contract = await accepted_node_contract(session, parent)
    release = contract.node.operational_release_binding if contract is not None else None
    if release is not None and release.proof_phase == "stage.identity-proof":
        # This phase is restricted to dev by the maintained workflow. The account
        # and region come from its runtime-verified target, never from artifact
        # content or a caller-supplied bucket/location.
        require(
            release.workflows == ["gateway-deploy.yml"]
            and len(receipt.targets) == 1
            and {item.component for item in receipt.components} == {"gateway-backend"},
            "identity_proof_artifact_target_changed",
        )
        target = receipt.targets[0]
        artifact_target["identity_proof_artifact_target"] = IdentityProofArtifactTarget(
            account_id=target.get("account_id"), region=target.get("region"), environment="dev"
        ).model_dump()
    if any(
        workflow.source.predecessor == expected.address
        and workflow.path == ".github/workflows/tools-platform-deploy.yml"
        and any(artifact.name == "tools-live-evaluation-{run_id}-{run_attempt}" for artifact in workflow.artifacts)
        for workflow in getattr(spec, "workflows", ())
    ):
        require(
            release is not None
            and release.component == "tool-adapters"
            and release.workflows == ["tools-platform-deploy.yml"]
            and len(receipt.targets) == 1
            and {item.component for item in receipt.components} == {"tool-adapters"},
            "foundation_artifact_target_changed",
        )
        target = receipt.targets[0]
        artifact_target["foundation_artifact_target"] = IdentityProofArtifactTarget(
            account_id=target.get("account_id"), region=target.get("region"), environment="dev"
        ).model_dump()
    return dict(
        **artifact_target,
        **(
            {
                "deployment_targets": receipt.targets,
                "runtime_artifacts": [
                    {
                        "component": component.component,
                        "actual_revision": component.actual_revision,
                        "artifact_hash": component.artifact_hash,
                        "image_digest": component.image_digest,
                        "tick_digest": component.tick_digest,
                        "migration_head": component.migration_head,
                        "asset_count": component.asset_count,
                    }
                    for component in receipt.components
                ],
            }
            if spec.evidence_schema == "multi-repository-evaluation/v1" and spec.mode == "deployed"
            else {}
        ),
        **({"issue_number": int(parent.issue_ref)} if getattr(spec, "qualification", None) is not None and str(parent.issue_ref).isdigit() else {}),
        address=expected.address,
        node_id=parent.id,
        attempt=parent.attempts,
        accepted_plan_version=receipt.accepted_plan_version,
        execution_id=receipt.execution_id,
        deployment_receipt_key=receipt.operation_key,
        workflow_operation_keys=sorted(receipt.workflow_operation_keys),
        components=sorted(item.component for item in receipt.components),
        head_sha=receipt.actual_revision,
        merge_sha=receipt.actual_revision,
        required_checks=[item.model_dump() for item in expected.required_checks],
    )


async def authorize(session, node, plan, spec, binding, provider, *, exclude_current_evaluation=False, source_repositories=()):
    from . import shared_policy
    from .policy_admission import SpendObservation

    # This observer uses its own short-lived repository-read token, never an
    # agent's broad role. Minting must really succeed before claiming SCOPED.
    require(getattr(spec, "qualification", None) is None or node.issue_ref == str(spec.qualification.owner_issue), "cli_qualification_owner_changed")
    async with _ProviderBudget():
        await provider.token(binding)
    from .evaluation_authority import evaluation_inputs

    inputs, marker = await evaluation_inputs(session, org_id=node.org_id, flow_id=node.flow_id)
    repository = spec.harness.repository if spec.evidence_schema == "multi-repository-evaluation/v1" else spec.runner.repository
    require(
        inputs.plan_version == plan.version
        and repository in inputs.policy.repository_ids
        and set(source_repositories) <= set(inputs.policy.repository_ids),
        "policy_changed",
    )
    from .evaluation_acceptance import accepted_contract

    attached = await accepted_contract(session, node=node, plan=plan)
    require(attached is None or attached[1] == spec, "accepted_specification_changed")
    policy = attached[2] if attached is not None else inputs.policy
    policy._budget_enforcement_enabled = inputs.policy._budget_enforcement_enabled
    policy._budget_accounting_incomplete = inputs.policy._budget_accounting_incomplete
    meter = await shared_policy.read_flow_meter(org_id=node.org_id, flow_id=node.flow_id, policy=inputs.policy)
    require(
        not policy._budget_enforcement_enabled or (meter is not None and meter.total_usd >= Decimal(marker["prior_spend_usd"])), "budget_unavailable"
    )
    context = await shared_policy.resolve_authorization_context(
        session,
        policy=policy,
        plan_version=plan.version,
        node=node,
        # The canonical users.id resolved by evaluation_inputs, never the raw sub.
        principal_user_id=marker.get("principal_user_id") or policy.principal_id,
        credential_scope=CredentialScope.SCOPED,
        spend=SpendObservation(total_usd=meter.total_usd if meter else None),
    )
    flow = await session.get(OrchestrationFlow, node.flow_id)
    address = graph_address(node, flow_slug=flow.slug)
    rows = [row for row in (plan.plan_document or {}).get("nodes", []) if row.get("address") == address]
    require(
        len(rows) == 1 and all(rows[0].get(key) == getattr(node, key) for key in ("kind", "title", "issue_ref")),
        "accepted_node_changed",
    )
    # Ownership here is the accepted flow's evaluation node. Reading an external
    # issue does not acquire the issue's coding lane and cannot dispatch changes.
    context = replace(
        context,
        work_owned_by_policy_flow=True,
        evaluation_operation="collect" if spec.evidence_schema == "workflow-evaluation/v1" or spec.acceptance_mode == "human" else "accept",
        observed_attempts=max(0, context.observed_attempts - int(exclude_current_evaluation and node.state == "running")),
        observed_concurrency=max(
            0,
            await shared_policy._active_count(session, org_id=node.org_id, flow_id=node.flow_id, initial_runs=marker.get("initial_runs"))
            - int(exclude_current_evaluation and node.kind == "eval" and node.state == "running"),
        ),
    )
    decision = authorize_action(
        context, Action.EVALUATE, ResourceRef(repository_id=binding.repo, node_address=address, org_id=node.org_id), plan.version
    )
    require(decision.permitted, "authority_" + (decision.reason.value if decision.reason else "denied"))
    return (attached[0].id if attached is not None else None), policy.policy_hash


async def record(session, node, payload, *, rejection=False, before=None, refusal_scope=None):
    if rejection:
        payload = scoped_refusal(payload, refusal_scope)
    text = canonical(payload)
    require(len(text) <= 512 * 1024, "receipt_size_limit")
    # A repeated blocker still advances the observation cursor so the next tick
    # gives another ready evaluation its bounded turn without growing the ledger.
    node.updated_at = datetime.now(UTC)
    previous = await session.scalar(
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == node.org_id,
            OrchestrationDecision.flow_id == node.flow_id,
            OrchestrationDecision.node_id == node.id,
            OrchestrationDecision.actor_id == ACTOR,
            OrchestrationDecision.actor_kind == "service",
        )
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(1)
    )
    if previous is not None and previous.reason == text:
        return
    session.add(
        OrchestrationDecision(
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind="transition_rejected" if rejection else "result_observed",
            actor_id=ACTOR,
            actor_role="engine",
            actor_kind="service",
            from_state=before or node.state,
            to_state=None if rejection else node.state,
            reason=text,
            rejection_reason=text if rejection else None,
        )
    )
    await session.flush()


async def observe_repository_evaluation(session, node, *, provider=None):
    """One bounded observer after story admission; no slow read owns the tick.

    The observation budget bounds the provider's reads (`_ProviderBudget`), never the
    database work around them. Cancelling an in-flight statement leaves the savepoint
    invalid, and every later pass of the same tick then dies with
    ``PendingRollbackError`` — the tick went fatal once a minute for half an hour when
    one evaluation's reads outlived the budget (#7889).
    """
    async with session.begin_nested():
        return await _observe_repository_evaluation(session, node, provider=provider)


# One observation's provider-I/O deadline (monotonic seconds), shared by every
# `_ProviderBudget` scope opened while it is set: the token mint inside `authorize`
# and the evidence reads in `provider.observe` draw on the same budget, as the
# whole-observation timeout did, without that timeout ever reaching a statement.
_OBSERVATION_DEADLINE: ContextVar[float | None] = ContextVar("repository_evaluation_observation_deadline", default=None)


class _ProviderBudget:
    """``async with`` scope for provider I/O.

    Over budget is a named refusal (``repository_evaluation_time_budget``), never a
    cancelled database statement.
    """

    def __init__(self, seconds=None):
        if seconds is None:
            deadline = _OBSERVATION_DEADLINE.get()
            seconds = OBSERVATION_TIMEOUT_SECONDS if deadline is None else max(0.0, deadline - time.monotonic())
        self._timeout = asyncio.timeout(seconds)

    async def __aenter__(self):
        await self._timeout.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            return await self._timeout.__aexit__(exc_type, exc, tb)
        except TimeoutError:
            raise CycleBlockedError("repository_evaluation_time_budget") from None


async def block_on_required_inputs(session, node):
    from .accepted_node import unresolved_required_inputs

    unresolved = await unresolved_required_inputs(session, node)
    if not unresolved:
        return False
    await record(
        session,
        node,
        dict(
            action="repository_evaluation_blocked",
            block_code="required_inputs_unavailable",
            owner="flow-owner",
            required_input="; ".join(f"{item.name}: {item.source}" for item in unresolved),
            next_action="Satisfy the named prerequisites under the accepted plan before retrying this evaluation.",
        ),
        rejection=True,
        refusal_scope=await evaluation_scope(session, node),
    )
    return True


async def _observe_repository_evaluation(session, node, *, provider=None):
    accepted = await accepted_evaluation(session, node)
    if accepted is None or accepted[1].evidence_schema not in {
        "repository-evaluation/v1",
        "cli-live-evaluation/v1",
        "workflow-evaluation/v1",
        "multi-repository-evaluation/v1",
    }:
        return False
    budget = _OBSERVATION_DEADLINE.set(time.monotonic() + OBSERVATION_TIMEOUT_SECONDS)
    try:
        return await _observe_within_budget(session, node, accepted, provider=provider)
    finally:
        _OBSERVATION_DEADLINE.reset(budget)


async def _observe_within_budget(session, node, accepted, *, provider=None):
    plan, spec, _ = accepted
    provider = provider or RepositoryEvidenceProvider()
    settlement = None
    expected_plan = plan.id, plan.version, plan.plan_hash
    expected_attempt = node.attempts
    # Capture before provider I/O. Refusal after an amendment must retain the
    # scope that was actually observed, not acquire the replacement's identity.
    refusal_scope = await evaluation_scope(session, node, accepted=accepted)
    try:
        require(node.state == "ready", "node_not_ready")
        flow = await session.get(OrchestrationFlow, node.flow_id)
        require(flow is not None and flow.org_id == node.org_id and flow.state in {"pending", "running"}, "flow_not_running")
        if await block_on_required_inputs(session, node):
            return True
        if spec.evidence_schema == "multi-repository-evaluation/v1":
            from .repository_producer import admit_producer

            async with session.begin_nested():
                return await admit_producer(session, node, provider=provider)
        if spec.producer is not None:
            from .repository_producer import admit_producer

            async with session.begin_nested():
                return await admit_producer(session, node, provider=provider)
        collector_digest = harness_digest()
        require(native_harness_compatible(spec, collector_digest), "harness_changed")
        from .dispatch_pass import resolve_installation_id

        installation = await resolve_installation_id(session, org_id=node.org_id)
        require(installation is not None, "installation_missing")
        binding = SimpleNamespace(
            org_id=node.org_id, installation_id=installation, repo=spec.runner.repository, provider_repository_id=spec.runner.repository_id
        )
        authority = await authorize(session, node, plan, spec, binding, provider)
        sources = await sources_for(session, node, plan, spec)
        spec_hash = hashlib.sha256(canonical(spec.model_dump(mode="json")).encode()).hexdigest()
        snapshot_hash = hashlib.sha256(canonical(sources).encode()).hexdigest()
        async with _ProviderBudget():
            observed = await provider.observe(binding, spec, sources)
        settlement = await session.begin_nested()
        # Lock/re-read accepted state at the write boundary, including all direct
        # predecessors. An amendment or binding replacement invalidates the read.
        await session.scalar(
            select(OrchestrationFlow)
            .where(OrchestrationFlow.id == node.flow_id, OrchestrationFlow.org_id == node.org_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        current = await session.scalar(
            select(OrchestrationAcceptedPlan)
            .where(
                OrchestrationAcceptedPlan.org_id == node.org_id,
                OrchestrationAcceptedPlan.flow_id == node.flow_id,
                OrchestrationAcceptedPlan.superseded_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        require(current is not None and (current.id, current.version, current.plan_hash) == expected_plan, "plan_changed")
        node = await session.scalar(
            select(OrchestrationNode)
            .where(OrchestrationNode.id == node.id, OrchestrationNode.org_id == node.org_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        require(node.attempts == expected_attempt, "attempt_changed")
        fresh = await accepted_evaluation(session, node)
        require(fresh is not None and fresh[1] == spec and node.state == "ready" and flow.state in {"pending", "running"}, "scope_changed")
        require(await sources_for(session, node, current, spec, lock=True) == sources, "source_changed")
        require(await authorize(session, node, current, spec, binding, provider) == authority, "evaluation_authority_changed")
        if await block_on_required_inputs(session, node):
            await settlement.commit()
            return True
        receipt = receipt_model(spec)(
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            cycle=node.attempts + 1,
            accepted_plan_version=current.version,
            accepted_plan_hash=current.plan_hash,
            acceptance_decision_id=authority[0],
            evaluation_policy_hash=authority[1],
            specification_hash=spec_hash,
            harness_sha256=spec.runner.harness_sha256,
            collector_harness_sha256=collector_digest,
            source_snapshot_hash=snapshot_hash,
            observed_at=datetime.now(UTC),
            **observed,
        )
        before = node.state
        if receipt.mandatory_passed:
            for target in (NodeState.RUNNING, NodeState.PASSED):
                require(
                    transition(node.state, target, actor_kind=ActorKind.SERVICE, reason="Verified accepted repository evidence").allowed,
                    "transition_refused",
                )
                node.state = target.value
            node.attempts += 1
        await record(session, node, dict(action="repository_evaluation", receipt=receipt.model_dump(mode="json")), before=before)
        if receipt.mandatory_passed:
            from .tick import release_satisfied_successors

            await release_satisfied_successors(session, node)
        await settlement.commit()
        return True
    except Exception as error:
        if settlement is not None:
            await settlement.rollback()
            await session.refresh(node)
        reason = error.reason if isinstance(error, CycleBlockedError) else "repository_evaluation_provider_unverifiable"
        logger.warning("Repository evaluation refused node=%s reason=%s", node.id, reason, exc_info=not isinstance(error, CycleBlockedError))
        await record(
            session,
            node,
            dict(
                action="repository_evaluation_blocked",
                block_code=reason,
                owner="evidence-producer",
                required_input="Provide successful evidence matching the accepted repository evaluation specification.",
                next_action="Recheck authenticated repository evidence on the next engine tick.",
            ),
            rejection=True,
            refusal_scope=refusal_scope,
        )
        return True


def receipt_model(spec):
    if spec.evidence_schema == "cli-live-evaluation/v1":
        from .cli_live_contract import CliWorkflowReceipt

        class CliLiveEvaluationReceipt(RepositoryEvaluationReceipt):
            evidence_schema: Literal["cli-live-evaluation-receipt/v1"] = "cli-live-evaluation-receipt/v1"
            live_attestation: Literal[True] = True
            workflows: list[CliWorkflowReceipt]

        return CliLiveEvaluationReceipt
    return RepositoryEvaluationReceipt
