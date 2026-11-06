"""Bounded story timing from immutable, successful state transitions.

Node updated_at includes metadata edits and is deliberately never used as an
execution boundary. Times describe wall time (including review and queue waits),
not worker compute time. Older stories may have no recorded start or finish.
"""

from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import OrchestrationDecision as Decision
from .models import OrchestrationNode as Node

MAX_TIMELINE_NODES = 100
MAX_TRANSITIONS = 50


class TimelineTransition(BaseModel):
    id: str
    from_state: str | None
    to_state: str
    at: datetime


class StoryTiming(BaseModel):
    node_id: str
    state: str
    attempts: int
    started_at: datetime | None
    finished_at: datetime | None
    transitions: list[TimelineTransition]
    transitions_truncated: bool


class FlowTimelineResponse(BaseModel):
    flow_id: str
    server_time: datetime
    nodes: list[StoryTiming]
    total: int
    limit: int
    offset: int


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


async def load_flow_timeline(db: AsyncSession, *, org_id: str, flow_id: str, limit: int, offset: int) -> FlowTimelineResponse:
    scope = (
        Node.org_id == org_id,
        Node.flow_id == flow_id,
        Node.state != "superseded",
        (Node.kind == "story") | ((Node.kind == "eval") & (func.length(func.trim(Node.issue_ref)) > 0)),
    )
    total = (await db.execute(select(func.count()).select_from(Node).where(*scope))).scalar_one()
    nodes = (await db.execute(select(Node).where(*scope).order_by(Node.created_at, Node.id).limit(limit).offset(offset))).scalars().all()
    by_node: dict[str, list[TimelineTransition]] = {node.id: [] for node in nodes}
    milestones = {}
    if nodes:
        conditions = (
            Decision.org_id == org_id,
            Decision.flow_id == flow_id,
            Decision.node_id.in_(by_node),
            Decision.rejection_reason.is_(None),
            Decision.to_state.is_not(None),
            (Decision.from_state.is_(None)) | (Decision.from_state != Decision.to_state),
        )
        # Aggregates use ALL transitions, even if event details are truncated.
        rows = (
            await db.execute(
                select(
                    Decision.node_id,
                    func.min(case((Decision.to_state == "running", Decision.created_at))).label("started"),
                    func.count().label("count"),
                )
                .where(*conditions)
                .group_by(Decision.node_id)
            )
        ).all()
        milestones = {row.node_id: row for row in rows}
        ranked = (
            select(
                Decision.id,
                Decision.node_id,
                Decision.from_state,
                Decision.to_state,
                Decision.created_at,
                func.row_number().over(partition_by=Decision.node_id, order_by=(Decision.created_at.desc(), Decision.id.desc())).label("rank"),
            )
            .where(*conditions)
            .subquery()
        )
        events = (await db.execute(select(ranked).where(ranked.c.rank <= MAX_TRANSITIONS).order_by(ranked.c.created_at, ranked.c.id))).all()
        for event in events:
            by_node[event.node_id].append(
                TimelineTransition(
                    id=event.id,
                    from_state=event.from_state,
                    to_state=event.to_state,
                    at=_aware(event.created_at),
                )
            )
    result = []
    for node in nodes:
        events = by_node[node.id]
        milestone = milestones.get(node.id)
        latest = events[-1] if events else None
        # A historic pass must not finish a reopened story. A metadata timestamp
        # or execution-ledger conclusion is never proof of acceptance either.
        terminal = node.state in {"passed", "failed", "halted", "waived", "rejected_at_gate"}
        finished = latest.at if latest and latest.to_state == node.state and terminal else None
        result.append(
            StoryTiming(
                node_id=node.id,
                state=node.state,
                attempts=node.attempts,
                started_at=_aware(milestone.started) if milestone else None,
                finished_at=finished,
                transitions=events,
                transitions_truncated=bool(milestone and milestone.count > MAX_TRANSITIONS),
            )
        )
    return FlowTimelineResponse(flow_id=flow_id, server_time=datetime.now(UTC), nodes=result, total=total, limit=limit, offset=offset)
