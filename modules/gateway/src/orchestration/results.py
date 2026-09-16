"""Observe completed engine attempts without equating worker exit with success.

Stories require merged code and successful checks. Evaluations present their
finished run for human evidence review; only the existing approval boundary may
accept that result. Missing observations leave the node unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.base import utcnow

from .dispatch_pass import attempt_run_id, resolve_installation_id
from .models import DecisionKind, NodeKind, OrchestrationDecision, OrchestrationNode
from .run_store import EngineRunStore
from .state import ActorKind, NodeState, transition

logger = logging.getLogger(__name__)


@dataclass
class ResultReport:
    examined: int = 0
    advanced: int = 0
    errors: int = 0
    waiting: int = 0
    reasons: dict[str, str] = field(default_factory=dict)


class GitHubEvidenceSource:
    async def merged_story(self, *, org_id: str, installation_id: int, repo: str, issue: int, since: str) -> str | None:
        from src.admin.connections.github_client import GitHubAppClient
        from src.knowledge.github_app_service import resolve_tenant_app_credentials

        owner, name = repo.split("/", 1)
        app_id, private_key = await resolve_tenant_app_credentials(org_id)
        app = GitHubAppClient(app_id, private_key)
        try:
            token = await app.get_installation_token(installation_id)
            query = """query($owner:String!,$name:String!,$issue:Int!) {
              repository(owner:$owner,name:$name) { issue(number:$issue) {
                state stateReason timelineItems(last:10,itemTypes:[CLOSED_EVENT]) { nodes {
                  ... on ClosedEvent { closer { __typename ... on PullRequest {
                    url merged mergedAt commits(last:1) { nodes { commit { statusCheckRollup { state } } } }
                  } } }
                } }
              } }
            }"""
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    "https://api.github.com/graphql",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"query": query, "variables": {"owner": owner, "name": name, "issue": issue}},
                )
                response.raise_for_status()
                payload = response.json()
            if payload.get("errors"):
                raise RuntimeError("GitHub could not verify merged-story evidence")
            record = (payload.get("data", {}).get("repository") or {}).get("issue") or {}
            if record.get("state") != "CLOSED" or record.get("stateReason") != "COMPLETED":
                return None
            events = (record.get("timelineItems") or {}).get("nodes", [])
            # The last closure is decisive; an older merged PR cannot justify a
            # later manual close or a retry whose work never landed.
            closer = (events[-1].get("closer") or {}) if events else {}
            if closer.get("__typename") != "PullRequest" or not closer.get("merged"):
                return None
            if datetime.fromisoformat(closer["mergedAt"].replace("Z", "+00:00")) < datetime.fromisoformat(since.replace("Z", "+00:00")):
                return None
            commits = (closer.get("commits") or {}).get("nodes", [])
            rollup = ((commits[-1].get("commit") or {}).get("statusCheckRollup") or {}) if commits else {}
            return closer["url"] if rollup.get("state") == "SUCCESS" else None
        finally:
            await app.aclose()


async def observe_results(session: AsyncSession, *, run_store: Any | None = None, evidence: Any | None = None) -> ResultReport:
    report = ResultReport()
    deadline = time.monotonic() + 45
    # Oldest observation first: a long-running node cannot starve later work.
    # Persist the cursor as an audit row, so cold starts and overlapping ticks
    # have the same bounded sweep. Twenty external reads fit the tick's budget.
    last_check = (
        select(OrchestrationDecision.node_id, func.max(OrchestrationDecision.created_at).label("checked_at"))
        .where(
            OrchestrationDecision.kind == DecisionKind.RESULT_CHECKED.value,
        )
        .group_by(OrchestrationDecision.node_id)
        .subquery()
    )
    nodes = (
        (
            await session.execute(
                select(OrchestrationNode)
                .outerjoin(
                    last_check,
                    last_check.c.node_id == OrchestrationNode.id,
                )
                .where(
                    OrchestrationNode.state.in_([NodeState.RUNNING.value, NodeState.AWAITING_MERGE.value]),
                    OrchestrationNode.kind.in_([NodeKind.STORY.value, NodeKind.EVAL.value]),
                )
                .order_by(last_check.c.checked_at.asc().nullsfirst(), OrchestrationNode.id)
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    for node in nodes:
        if time.monotonic() >= deadline:
            break
        node_id, org_id, flow_id, attempt = node.id, node.org_id, node.flow_id, node.attempts
        report.examined += 1
        try:
            async with session.begin_nested():
                decision = (
                    await session.execute(
                        select(OrchestrationDecision)
                        .where(
                            OrchestrationDecision.org_id == node.org_id,
                            OrchestrationDecision.node_id == node.id,
                            OrchestrationDecision.kind == DecisionKind.NODE_DISPATCHED.value,
                        )
                        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if decision is None:
                    report.waiting += 1
                    continue
                dispatch = json.loads(decision.reason or "{}")
                if dispatch.get("attempt") != node.attempts or dispatch.get("run_id") != attempt_run_id(node.id, node.attempts):
                    raise ValueError("dispatch record does not match the current attempt")
                store = run_store if run_store is not None else EngineRunStore.from_env()
                row = await asyncio.to_thread(store.get, dispatch["run_id"], dispatch["arrived_at"])
                if row is None:
                    report.waiting += 1
                    continue
                if row.get("tenant_id") != node.org_id or row.get("engine_node_id") != node.id or row.get("engine_attempt") != node.attempts:
                    raise ValueError("run record does not match node/tenant/attempt")
                status = row.get("status")
                if status in {"failed", "budget_stopped", "aborted", "cancelled"}:
                    target, detail = NodeState.FAILED, f"Worker reported {status}; inspect the run before retrying."
                elif status == "complete":
                    if node.kind == NodeKind.EVAL.value:
                        # A green worker exit is not a test verdict. Require a real
                        # report and human review, including all deferred criteria.
                        if not row.get("transcript_key"):
                            report.waiting += 1
                            continue
                        target, detail = (
                            NodeState.AWAITING_GATE,
                            "Evaluation run finished. Review its test evidence and deferred criteria before accepting.",
                        )
                    else:
                        installation_id = await resolve_installation_id(session, org_id=node.org_id)
                        if installation_id is None:
                            raise ValueError("GitHub installation is unresolved")
                        source = evidence if evidence is not None else GitHubEvidenceSource()
                        url = await asyncio.wait_for(
                            source.merged_story(
                                org_id=node.org_id,
                                installation_id=installation_id,
                                repo=dispatch["repo"],
                                issue=dispatch["issue"],
                                since=dispatch["arrived_at"],
                            ),
                            timeout=15,
                        )
                        if not url:
                            target, detail = (
                                NodeState.AWAITING_MERGE,
                                "Agent finished. Waiting for the issue to be completed by a merged pull request with successful checks.",
                            )
                        else:
                            target, detail = NodeState.PASSED, f"Issue completed by merged pull request with successful checks: {url}"
                else:
                    report.waiting += 1
                    continue
                if target.value == node.state:
                    report.waiting += 1
                    continue
                observed_state = node.state
                result = transition(observed_state, target, actor_kind=ActorKind.SERVICE, reason=detail)
                if not result.allowed:
                    raise ValueError(result.rejection_reason)
                rows = (
                    await session.execute(
                        update(OrchestrationNode)
                        .where(
                            OrchestrationNode.id == node.id,
                            OrchestrationNode.org_id == node.org_id,
                            OrchestrationNode.state == observed_state,
                            OrchestrationNode.attempts == dispatch["attempt"],
                        )
                        .values(state=target.value, updated_at=utcnow())
                    )
                ).rowcount
                if rows:
                    session.add(
                        OrchestrationDecision(
                            org_id=node.org_id,
                            flow_id=node.flow_id,
                            node_id=node.id,
                            kind=DecisionKind.RESULT_OBSERVED.value,
                            actor_id="system:orchestration-results",
                            actor_role="engine",
                            actor_kind=ActorKind.SERVICE.value,
                            from_state=observed_state,
                            to_state=target.value,
                            reason=json.dumps({"attempt": dispatch["attempt"], "run_id": dispatch["run_id"], "evidence": detail}),
                        )
                    )
                    await session.flush()
                    report.advanced += 1
                    report.reasons[node.id] = detail
        except Exception:
            report.errors += 1
            logger.exception("Could not observe result for engine node %s", node_id)
        finally:
            session.add(
                OrchestrationDecision(
                    org_id=org_id,
                    flow_id=flow_id,
                    node_id=node_id,
                    kind=DecisionKind.RESULT_CHECKED.value,
                    actor_id="system:orchestration-results",
                    actor_role="engine",
                    actor_kind=ActorKind.SERVICE.value,
                    reason=f"Checked attempt {attempt}",
                )
            )
            await session.flush()
    return report
