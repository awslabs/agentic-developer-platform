"""Retire exited, actionless old admissions after a later attempt has completed.

An operator resume can replace a stalled developer before the result observer
concludes its old admission. That row has no valid claim and cannot run again,
but it still blocks plan amendments. Never infer exit from its age or lease.
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import aliased

from .dispatch_pass import attempt_run_id
from .execution_state import ExecutionIdentity
from .execution_store import supersede_replaced_admission
from .models import OrchestrationDecision, OrchestrationExecution, OrchestrationFlow, OrchestrationNode, OrchestrationWorkClaim

logger = logging.getLogger(__name__)


async def _settle_one(session, *, flow_id, identity, revision, successor_id, successor_revision):
    flow = await session.scalar(
        select(OrchestrationFlow)
        .where(OrchestrationFlow.org_id == identity.org_id, OrchestrationFlow.id == flow_id, OrchestrationFlow.archived_at.is_(None))
        .with_for_update(skip_locked=True)
    )
    if flow is None:
        return False
    # Dispatch can hold the node before the flow. Skip its lock so recovery never
    # waits for a node while holding the flow. The existing sweep retries it.
    node = await session.scalar(
        select(OrchestrationNode)
        .where(OrchestrationNode.org_id == identity.org_id, OrchestrationNode.id == identity.node_id, OrchestrationNode.flow_id == flow_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if node is None or node.kind != "story" or node.state != "passed" or node.attempts <= identity.cycle:
        return False
    issue = str(node.issue_ref).lstrip("#")
    if not issue.isdecimal():
        return False
    return await supersede_replaced_admission(
        session,
        identity=identity,
        expected_revision=revision,
        successor_id=successor_id,
        successor_revision=successor_revision,
        successor_cycle=node.attempts,
        issue_number=int(issue),
    )


async def recover_replaced_admissions(factory, *, store, after_id="", limit=25):
    """Return (settled count, scan cursor); no external call holds SQL locks."""
    successor = aliased(OrchestrationExecution)
    async with factory() as session:
        candidates = (
            await session.execute(
                select(OrchestrationExecution, successor.id, successor.revision)
                .join(
                    OrchestrationNode,
                    (OrchestrationNode.org_id == OrchestrationExecution.org_id) & (OrchestrationNode.id == OrchestrationExecution.node_id),
                )
                .join(
                    successor,
                    (successor.org_id == OrchestrationExecution.org_id)
                    & (successor.flow_id == OrchestrationExecution.flow_id)
                    & (successor.node_id == OrchestrationExecution.node_id)
                    & (successor.cycle == OrchestrationNode.attempts),
                )
                .join(
                    OrchestrationWorkClaim,
                    (OrchestrationWorkClaim.org_id == OrchestrationExecution.org_id) & (OrchestrationWorkClaim.id == OrchestrationExecution.claim_id),
                )
                .where(
                    OrchestrationExecution.id > after_id,
                    OrchestrationExecution.phase == "admitted",
                    OrchestrationExecution.status.in_(("runnable", "awaiting_external")),
                    OrchestrationExecution.cycle < OrchestrationNode.attempts,
                    OrchestrationNode.kind == "story",
                    OrchestrationNode.state == "passed",
                    successor.status == "concluded",
                    OrchestrationWorkClaim.state == "released",
                    OrchestrationWorkClaim.generation > OrchestrationExecution.claim_generation,
                )
                .order_by(OrchestrationExecution.id)
                .limit(limit)
            )
        ).all()
        snapshots = [
            (
                row.id,
                row.flow_id,
                ExecutionIdentity(row.org_id, row.node_id, row.cycle, row.accepted_plan_version, row.claim_id, row.claim_generation),
                row.revision,
                successor_id,
                successor_revision,
            )
            for row, successor_id, successor_revision in candidates
        ]
    settled = 0
    for execution_id, flow_id, identity, revision, successor_id, successor_revision in snapshots:
        run_id = attempt_run_id(identity.node_id, identity.cycle)
        try:
            raw = await asyncio.to_thread(store._read, f"TENANT#{identity.org_id}", f"EXEC#{run_id}")
            expected = {
                "tenant_id": {"S": identity.org_id},
                "invocation_id": {"S": run_id},
                "flow_id": {"S": flow_id},
                "orchestration_node_id": {"S": identity.node_id},
                "orchestration_node_attempt": {"N": str(identity.cycle)},
                "status": {"S": "completed"},
            }
            if (
                not raw
                or any(raw.get(key) != value for key, value in expected.items())
                or raw.get("terminal_outcome")
                not in (
                    {"S": "complete"},
                    {"S": "failed"},
                )
            ):
                continue
            async with factory() as session:
                changed = await _settle_one(
                    session,
                    flow_id=flow_id,
                    identity=identity,
                    revision=revision,
                    successor_id=successor_id,
                    successor_revision=successor_revision,
                )
                if changed:
                    session.add(
                        OrchestrationDecision(
                            id=str(uuid5(NAMESPACE_URL, f"replaced-admission:{execution_id}")),
                            org_id=identity.org_id,
                            flow_id=flow_id,
                            node_id=identity.node_id,
                            kind="replaced_admission_settled",
                            actor_id="system:replaced-admission-recovery",
                            actor_role="engine",
                            actor_kind="service",
                            reason=json.dumps(
                                {
                                    "execution_id": execution_id,
                                    "previous_revision": revision,
                                    "run_id": run_id,
                                    "exit_evidence": "protected_terminal_record",
                                    "successor_execution_id": successor_id,
                                    "successor_revision": successor_revision,
                                    "claim_id": identity.claim_id,
                                    "claim_generation": identity.claim_generation,
                                }
                            ),
                        )
                    )
                    await session.commit()
                    settled += 1
        except Exception:
            logger.exception("replaced admission recovery failed execution=%s", execution_id)
    return settled, snapshots[-1][0] if len(snapshots) == limit else ""
