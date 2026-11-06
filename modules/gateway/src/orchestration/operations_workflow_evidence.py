"""Gateway-verified correlated workflow context as operational story evidence."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from .accepted_node import accepted_node_contract, operational_revision_inputs
from .models import OrchestrationAction, OrchestrationExecution, OrchestrationNode
from .review_cycle import CycleBlockedError
from .run_reports import OrchestrationRunReport


class CorrelatedWorkflowReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: str
    flow_id: str
    node_id: str
    execution_id: str
    cycle: int = Field(ge=1)
    accepted_plan_version: int = Field(ge=1)
    claim_id: str
    claim_generation: int = Field(ge=1)
    operation_key: str
    repo: str
    component: str
    workflow: str
    run_id: int = Field(gt=0)
    run_attempt: int = Field(gt=0)
    event: Literal["push", "workflow_dispatch"] = "workflow_dispatch"
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    definition_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    # Exactly one of these names the verified context document: the GitHub
    # Actions artifact id (github-artifact transport) or the immutable S3 object
    # reference (s3-oidc-v1 transport, bucket/key/versionId).
    context_artifact_id: int | None = Field(default=None, gt=0)
    context_reference: str | None = Field(default=None, min_length=1, max_length=1024)
    context_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The head the run actually executed at: the pinned definition revision or a
    # descendant with identical workflow content (#7662).
    head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    observed_at: datetime
    valid_until: datetime

    @model_validator(mode="after")
    def _one_context_locator(self):
        if (self.context_artifact_id is None) == (self.context_reference is None):
            raise ValueError("exactly one of context_artifact_id or context_reference is required")
        return self


def context_receipt_ref(run_id, artifact_id, reference):
    """The receipt_ref an observation action carries for its verified context document."""
    if artifact_id is not None:
        return f"github/actions/runs/{run_id}/artifacts/{artifact_id}"
    if not reference:
        raise ValueError("context reference required")
    return reference


async def current_push_head(provider, binding, branch):
    repository = (await provider.request(binding, "GET", f"/repos/{binding.repo}")).json()
    if not isinstance(repository, dict) or repository.get("id") != binding.provider_repository_id or repository.get("default_branch") != branch:
        raise CycleBlockedError("operations_push_repository_changed")
    result = (await provider.request(binding, "GET", f"/repos/{binding.repo}/git/ref/heads/{quote(branch, safe='')}")).json()
    head = (result.get("object") or {}).get("sha") if isinstance(result, dict) and isinstance(result.get("object"), dict) else None
    if not isinstance(head, str) or len(head) != 40 or any(char not in "0123456789abcdef" for char in head):
        raise CycleBlockedError("operations_push_head_unavailable")
    return head


async def record_correlated_workflow(session, *, execution, binding, issued, raw_run, context, artifact_id, digest, reference=None):
    invocation = execution["invocation_id"]["S"]
    workflow = raw_run["path"].removeprefix(".github/workflows/")
    pins = {name: execution.get(name, {}).get("S") for name in ("source_revision", "definition_revision", "default_branch")}
    node = await session.get(OrchestrationNode, issued.node_id)
    contract = await accepted_node_contract(session, node) if node is not None else None
    if (
        context.workflow_path != raw_run["path"]
        or context.run_id != raw_run["id"]
        or context.run_attempt != raw_run["run_attempt"]
        or context.repository_id != binding.provider_repository_id
        or (raw_run.get("event") != "push" and context.correlation != invocation)
        or (raw_run.get("event") == "push" and (context.correlation == invocation or context.inputs.get("adp_correlation")))
        or context.source_revision != pins["source_revision"]
        # The run's head may be a descendant of the pinned definition (#7662); the
        # caller verified containment and byte-identical workflow content.
        or context.workflow_revision != (raw_run.get("head_sha") or pins["definition_revision"])
    ):
        return None
    if (
        node is None
        or node.org_id != binding.org_id
        or contract is None
        or contract.plan_version != issued.plan_version
        or contract.node.repository_identity not in (None, binding.repo)
        or contract.node.executor is None
        or contract.node.executor.role != "operate"
        or contract.node.executor.persona != execution["persona"]["S"]
    ):
        return None
    workflows = [
        item.source.removeprefix("deployment_workflow:") for item in contract.node.required_inputs if item.source.startswith("deployment_workflow:")
    ]
    components = [item.removeprefix("runtime_verified:") for item in contract.node.evidence_expectations if item.startswith("runtime_verified:")]
    if workflows != [workflow] or len(components) != 1 or len(contract.node.evidence_expectations) != 1 or workflow not in issued.workflow_allowlist:
        return None
    assignment = await session.get(OrchestrationRunReport, invocation)
    if (
        assignment is None
        or assignment.org_id != node.org_id
        or assignment.node_id != node.id
        or assignment.attempt != node.attempts
        or assignment.persona != execution["persona"]["S"]
        or assignment.repo != binding.repo
        or assignment.provider_repository_id != binding.provider_repository_id
    ):
        return None
    from .operations_continuation_authority import assignment_matches

    row = await session.scalar(
        select(OrchestrationExecution)
        .where(
            OrchestrationExecution.org_id == node.org_id,
            OrchestrationExecution.node_id == node.id,
            OrchestrationExecution.cycle == node.attempts,
            OrchestrationExecution.accepted_plan_version == contract.plan_version,
        )
        .with_for_update()
    )
    if (
        row is None
        or row.status in {"concluded", "superseded"}
        or not await assignment_matches(session, node=node, execution=row, report=assignment)
        or row.flow_id != node.flow_id
    ):
        return None
    metadata = assignment.dispatch_metadata.get("orchestration") or {}
    if getattr(contract.node, "operational_release_binding", None) is not None:
        from .operations_release_resolution import recorded_operations_release

        accepted_pins = await recorded_operations_release(session, node=node, execution=row, contract=contract, metadata=metadata)
        if not isinstance(metadata.get("release_binding"), dict) or metadata["release_binding"].get("workflow") != raw_run["path"]:
            return None
    else:
        accepted_pins = operational_revision_inputs(contract)
    if accepted_pins is None or metadata.get("deployment_revisions") != accepted_pins:
        return None
    from .operations_repair import repair_revisions

    effective_pins = await repair_revisions(
        session, node_id=node.id, org_id=binding.org_id, plan_version=issued.plan_version, cycle=node.attempts, pins=accepted_pins
    )
    if effective_pins != pins:
        return None
    key = f"correlated-workflow:{raw_run['id']}:{raw_run['run_attempt']}"
    previous = await session.scalar(
        select(OrchestrationAction).where(
            OrchestrationAction.org_id == node.org_id,
            OrchestrationAction.execution_id == row.id,
            OrchestrationAction.operation_key == key,
        )
    )
    now = datetime.now(UTC)
    receipt = CorrelatedWorkflowReceipt(
        org_id=node.org_id,
        flow_id=node.flow_id,
        node_id=node.id,
        execution_id=row.id,
        cycle=node.attempts,
        accepted_plan_version=contract.plan_version,
        claim_id=row.claim_id,
        claim_generation=row.claim_generation,
        operation_key=key,
        repo=binding.repo,
        component=components[0],
        workflow=workflow,
        run_id=raw_run["id"],
        run_attempt=raw_run["run_attempt"],
        event=raw_run.get("event", "workflow_dispatch"),
        source_revision=pins["source_revision"],
        definition_revision=pins["definition_revision"],
        context_artifact_id=artifact_id,
        context_reference=reference,
        context_digest=digest,
        head_sha=raw_run.get("head_sha"),
        observed_at=now,
        valid_until=now + timedelta(hours=1),
    )
    detail = {"correlated_workflow_receipt": receipt.model_dump(mode="json")}
    if previous is not None:
        prior = (previous.detail or {}).get("correlated_workflow_receipt") or {}
        fields = (
            "org_id",
            "flow_id",
            "node_id",
            "execution_id",
            "cycle",
            "accepted_plan_version",
            "claim_id",
            "claim_generation",
            "operation_key",
            "repo",
            "component",
            "workflow",
            "run_id",
            "run_attempt",
            "event",
            "source_revision",
            "definition_revision",
            "context_artifact_id",
            "context_reference",
            "context_digest",
        )
        if previous.status == "succeeded" and all(
            prior.get(key, "workflow_dispatch" if key == "event" else None) == detail["correlated_workflow_receipt"][key] for key in fields
        ):
            return previous.id
        return None
    action = OrchestrationAction(
        org_id=node.org_id,
        execution_id=row.id,
        operation_key=key,
        kind="correlated_workflow_observation",
        status="succeeded",
        attempt=1,
        detail=detail,
        receipt_ref=context_receipt_ref(raw_run["id"], artifact_id, reference),
    )
    session.add(action)
    await session.flush()
    return action.id
