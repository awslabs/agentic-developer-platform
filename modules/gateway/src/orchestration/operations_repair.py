"""Record repair merges in the existing execution journal and derive current pins."""

import re

from sqlalchemy import select

from .accepted_node import accepted_node_contract, operational_revision_inputs
from .models import OrchestrationAction, OrchestrationExecution, OrchestrationNode
from .policy_admission import load_in_force_policy


def _workflows(contract):
    return sorted(
        item.source.removeprefix("deployment_workflow:") for item in contract.node.required_inputs if item.source.startswith("deployment_workflow:")
    )


class RepairMergeRequiredError(ValueError):
    """A published repair has no qualified merge receipt yet."""


async def repair_revisions(session, *, node_id, org_id, plan_version, cycle, pins, contract=None, require_merged=False):
    """Advance the pins through verified repair merges recorded under `plan_version`.

    `contract` may be supplied by a caller that has already verified, through the
    plan lineage, that the in-force contract still describes the node the run was
    fenced to (#7821); otherwise it is read here and must be at `plan_version`.
    """
    if pins is None:
        return None
    rows = (
        await session.execute(
            select(OrchestrationAction, OrchestrationExecution)
            .join(OrchestrationExecution, OrchestrationAction.execution_id == OrchestrationExecution.id)
            .where(
                OrchestrationAction.org_id == org_id,
                OrchestrationExecution.org_id == org_id,
                OrchestrationExecution.node_id == node_id,
                OrchestrationExecution.accepted_plan_version <= plan_version,
                OrchestrationExecution.cycle <= cycle,
                OrchestrationAction.kind.in_(("operations_repair_merge", "operations_repair_pr") if require_merged else ("operations_repair_merge",)),
                OrchestrationAction.status == "succeeded",
            )
            .order_by(OrchestrationExecution.cycle, OrchestrationAction.created_at, OrchestrationAction.id)
        )
    ).all()
    # An owner adoption reconciles (rather than overwrites) earlier publication
    # and merge history. Every consumer validates that immutable checkpoint
    # before advancing through repairs belonging to the new release.
    node = await session.get(OrchestrationNode, node_id)
    if node is None or node.org_id != org_id:
        return None
    if contract is None:
        contract = await accepted_node_contract(session, node)
    release = contract.node.operational_release_binding if contract is not None else None
    if release is not None and release.maintenance_adoption_id is not None:
        from .maintenance_adoption_resolution import verified_adoption
        from .maintenance_repair_coverage import covered_publication
        from .review_cycle import CycleBlockedError

        try:
            adoption = await verified_adoption(session, node, contract)
        except CycleBlockedError:
            return None
        if adoption is None or cycle <= adoption["checkpoint_cycle"] or pins != adoption["pins"]:
            return None
        rows = [
            (action, execution)
            for action, execution in rows
            if execution.cycle > adoption["checkpoint_cycle"]
            and not (
                action.kind == "operations_repair_pr"
                and covered_publication(
                    adoption,
                    repository=(action.detail or {}).get("repository", release.repository),
                    receipt_ref=action.receipt_ref,
                    pull_number=(action.detail or {}).get("pull_number"),
                    head=(action.detail or {}).get("head"),
                )
            )
        ]
    # A PR can have several recorded heads as CI repairs are published. Only
    # its latest published head needs a merge receipt; progress reports and
    # checks_verified never substitute for the protected merge journal.
    published = {}
    for action, _execution in rows:
        if action.kind == "operations_repair_pr":
            published[action.receipt_ref] = (action.detail or {}).get("head")
    rows = [(action, execution) for action, execution in rows if action.kind == "operations_repair_merge"]
    result = dict(pins)
    if not rows:
        if published:
            raise RepairMergeRequiredError("operations_repair_merge_required")
        return result
    node = await session.get(OrchestrationNode, node_id)
    if node is None or node.org_id != org_id:
        return None
    if contract is None:
        contract = await accepted_node_contract(session, node)
        if contract is None or contract.plan_version != plan_version:
            return None
    if getattr(contract.node, "operational_release_binding", None) is not None:
        from .dispatch_pass import attempt_run_id
        from .operations_release_resolution import operational_execution_pins

        current = await session.scalar(
            select(OrchestrationExecution).where(
                OrchestrationExecution.org_id == org_id,
                OrchestrationExecution.node_id == node_id,
                OrchestrationExecution.accepted_plan_version == plan_version,
                OrchestrationExecution.cycle == cycle,
            )
        )
        if (
            current is None
            or await operational_execution_pins(session, node=node, execution=current, contract=contract, run_id=attempt_run_id(node_id, cycle))
            != pins
        ):
            return None
    elif operational_revision_inputs(contract) != pins:
        return None
    admission = await load_in_force_policy(session, org_id=org_id, flow_id=node.flow_id)
    policy = admission.policy
    if (
        admission.refusal is not None
        or admission.plan_version not in {plan_version, contract.plan_version}
        or policy is None
        or not policy.policy_hash
    ):
        return None
    from .operations_agent_recovery import maintenance_pins
    from .operations_repair_history import historical_repair_policy

    merged = set()
    previous_execution = None
    previous_policy = policy
    for row, execution in rows:
        if previous_execution is not None and previous_execution.id != execution.id:
            result = await maintenance_pins(session, node=node, execution=previous_execution, pins=result, policy=previous_policy)
            if result is None:
                return None
        receipt_policy = policy
        # A historical query may use the original execution version while its
        # verified contract carries the current plan's authority context.
        if execution.accepted_plan_version != plan_version or execution.accepted_plan_version != contract.plan_version:
            receipt_policy = await historical_repair_policy(session, node=node, execution=execution, action=row, contract=contract, current=policy)
            if receipt_policy is None:
                return None
        previous_execution = execution
        detail = row.detail or {}
        policy_matches = detail.get("policy_hash") == receipt_policy.policy_hash
        if not policy_matches and (execution.handoff_receipt_ref or "").startswith("operations-agent-return:"):
            from .dispatch_pass import attempt_run_id
            from .operations_agent_recovery import closed_assignment
            from .run_reports import OrchestrationRunReport
            from .shared_retry import verified_limits_increased

            report = await session.get(OrchestrationRunReport, attempt_run_id(node.id, execution.cycle))
            closure = await closed_assignment(session, node=node, execution=execution, report=report) if report is not None else None
            policy_matches = bool(
                closure
                and detail.get("policy_hash") == closure["policy"]["policy_hash"]
                and verified_limits_increased(closure["policy"], receipt_policy)
            )
        if not policy_matches and execution.accepted_plan_version == plan_version and execution.cycle < cycle:
            # Limit supplements can change policy identity without a new plan.
            # A completed native repair retains only the authority verified at
            # its receipt time; current-cycle effects still need current policy.
            receipt_policy = await historical_repair_policy(session, node=node, execution=execution, action=row, contract=contract, current=policy)
            if receipt_policy is None:
                return None
            policy_matches = detail.get("policy_hash") == receipt_policy.policy_hash
        previous_policy = receipt_policy
        sha = detail.get("merge_sha")
        pull = detail.get("pull_number")
        head = detail.get("head")
        if (
            not isinstance(sha, str)
            or re.fullmatch(r"[0-9a-f]{40}", sha) is None
            or not isinstance(head, str)
            or re.fullmatch(r"[0-9a-f]{40}", head) is None
            or type(pull) is not int
            or pull < 1
            or row.execution_id != execution.id
            or execution.flow_id != node.flow_id
            or row.operation_key != f"operations-repair:{pull}:{head}"
            or row.receipt_ref != f"github/{detail.get('repository')}/pull/{pull}"
            or detail.get("repository") not in policy.repository_ids
            or contract.node.repository_identity not in (None, detail.get("repository"))
            or detail.get("default_branch") != result["default_branch"]
            or not policy_matches
            or detail.get("workflows") != _workflows(contract)
            or detail.get("repair_paths") != sorted(receipt_policy.repair_paths)
            or detail.get("repair_scope") != getattr(receipt_policy, "repair_scope", None)
            or detail.get("before_source_revision") != result["source_revision"]
            or detail.get("before_definition_revision") != result["definition_revision"]
            or type(detail.get("claim_generation")) is not int
            or not 1 <= detail["claim_generation"] <= execution.claim_generation
            or not detail.get("run_id")
        ):
            return None
        result.update(source_revision=sha, definition_revision=sha)
        merged.add((row.receipt_ref, head))
    result = await maintenance_pins(session, node=node, execution=previous_execution, pins=result, policy=previous_policy)
    if result is None:
        return None
    if any((receipt, head) not in merged for receipt, head in published.items()):
        raise RepairMergeRequiredError("operations_repair_merge_required")
    return result


async def record_repair_merge(session, assignment, *, pull_number, head, merged, authorized):
    from src.agentauth.github_operations import OperationRefusedError

    if merged.get("merged") is not True:
        return
    sha = merged.get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise OperationRefusedError("repair merge receipt unavailable")
    execution = await session.scalar(
        select(OrchestrationExecution)
        .where(
            OrchestrationExecution.org_id == assignment.tenant_id,
            OrchestrationExecution.node_id == assignment.node_id,
            OrchestrationExecution.accepted_plan_version == assignment.accepted_plan_version,
            OrchestrationExecution.cycle == assignment.attempt,
        )
        .with_for_update()
    )
    if execution is None or execution.status in {"concluded", "superseded"} or execution.claim_generation != assignment.claim_generation:
        raise OperationRefusedError("repair execution changed")
    node = await session.get(OrchestrationNode, assignment.node_id)
    contract = await accepted_node_contract(session, node) if node is not None and node.org_id == assignment.tenant_id else None
    if contract is not None:
        from .operations_release_resolution import operational_execution_pins

        pins = await operational_execution_pins(session, node=node, execution=execution, contract=contract, run_id=assignment.invocation_id)
    else:
        pins = None
    policy = authorized.policy
    if (
        contract is None
        or contract.plan_version != assignment.accepted_plan_version
        or contract.node.repository_identity not in (None, assignment.repository)
        or assignment.repository not in policy.repository_ids
        or pins is None
        or pins["default_branch"] != assignment.default_branch
        or not policy.policy_hash
        or not (policy.repair_paths or getattr(policy, "repair_scope", None) == "repository")
        or _workflows(contract) != sorted(authorized.workflow_allowlist)
    ):
        raise OperationRefusedError("repair scope changed")
    key = f"operations-repair:{pull_number}:{head}"
    prior = await session.scalar(
        select(OrchestrationAction).where(
            OrchestrationAction.org_id == assignment.tenant_id,
            OrchestrationAction.execution_id == execution.id,
            OrchestrationAction.operation_key == key,
        )
    )
    before = await repair_revisions(
        session,
        node_id=node.id,
        org_id=assignment.tenant_id,
        plan_version=assignment.accepted_plan_version,
        cycle=assignment.attempt,
        pins=pins,
    )
    if before is None:
        raise OperationRefusedError("repair revision chain changed")
    if prior is not None:
        if (prior.detail or {}).get("merge_sha") != sha or (prior.detail or {}).get("policy_hash") != policy.policy_hash:
            raise OperationRefusedError("repair merge identity changed")
        return
    session.add(
        OrchestrationAction(
            org_id=assignment.tenant_id,
            execution_id=execution.id,
            operation_key=key,
            kind="operations_repair_merge",
            status="succeeded",
            attempt=1,
            detail={
                "pull_number": pull_number,
                "head": head,
                "merge_sha": sha,
                "run_id": assignment.invocation_id,
                "claim_generation": assignment.claim_generation,
                "repository": assignment.repository,
                "default_branch": assignment.default_branch,
                "policy_hash": policy.policy_hash,
                "repair_paths": sorted(policy.repair_paths),
                **({"repair_scope": policy.repair_scope} if getattr(policy, "repair_scope", None) is not None else {}),
                "workflows": _workflows(contract),
                "before_source_revision": before["source_revision"],
                "before_definition_revision": before["definition_revision"],
            },
            receipt_ref=f"github/{assignment.repository}/pull/{pull_number}",
        )
    )
    await session.flush()
