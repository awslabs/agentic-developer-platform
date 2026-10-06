"""Read-only presentation of authorized Activity and Task invocation records."""

from datetime import datetime
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, Field

from src.activity.external_events import ActorKind, ClassifiedEvent, EventKind, Provider
from src.activity.schemas import InvocationItem, TriggerKind


class WorkRun(BaseModel):
    invocation_id: str
    record_url: str
    source_type: str
    task_id: str | None
    correlation_id: str | None
    parent_invocation_id: str | None
    trigger_kind: TriggerKind
    persona: str | None
    invoked_at: str
    completed_at: str | None
    status: str | None
    summary: str | None
    error: str | None
    task_result: dict | None
    task_error: dict | None
    repo: str | None
    issue_number: int | None
    evidence: dict[str, str] = Field(default_factory=dict)


class WorkIssue(BaseModel):
    repo: str
    issue_number: int
    url: str
    invocation_ids: list[str]


class WorkExternalEvent(BaseModel):
    provider: Provider
    source_id: str
    event_kind: EventKind
    repository: str
    source_url: str
    occurred_at: str
    actor_id: str
    attribution: ActorKind
    human_work: bool


class WorkTimelineEntry(BaseModel):
    source_type: Literal["adp", "github", "gitlab"]
    source_id: str
    occurred_at: str


class WorkSummary(BaseModel):
    runs: list[WorkRun]
    issues: list[WorkIssue]
    external_events: list[WorkExternalEvent] = Field(default_factory=list)
    timeline: list[WorkTimelineEntry] = Field(default_factory=list)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def summarize_work(items: list[InvocationItem], external_events: list[ClassifiedEvent] | None = None) -> WorkSummary:
    """Group issue presentation without discarding provenance from its runs."""
    runs: list[WorkRun] = []
    issues: dict[tuple[str, int], WorkIssue] = {}
    external: list[WorkExternalEvent] = []
    timeline: list[WorkTimelineEntry] = []
    seen: set[tuple[str, str]] = set()
    for item in sorted(items, key=lambda record: (record.invoked_at, record.invocation_id)):
        identity = item.source_type, item.invocation_id
        if identity in seen:
            continue
        seen.add(identity)
        transcript_url = f"/me/agent-invocations/{quote(item.invocation_id, safe='')}/transcript"
        evidence = {}
        if item.source_url:
            evidence["source"] = item.source_url
        if item.run_log_url:
            evidence["run_log"] = item.run_log_url
        if item.source_type == "task" and item.transcript_status == "available":
            evidence["task_report"] = transcript_url
        elif item.source_type == "activity" and item.transcript_key:
            evidence["transcript"] = transcript_url
        timeline.append(WorkTimelineEntry(source_type="adp", source_id=item.invocation_id, occurred_at=item.invoked_at))
        runs.append(
            WorkRun(
                invocation_id=item.invocation_id,
                record_url=f"/me/agent-invocations/{quote(item.invocation_id, safe='')}",
                source_type=item.source_type,
                task_id=item.task_id,
                correlation_id=item.correlation_id,
                parent_invocation_id=item.triggered_by_invocation_id,
                trigger_kind=item.trigger_kind,
                persona=item.persona,
                invoked_at=item.invoked_at,
                completed_at=item.completed_at,
                status=item.status,
                summary=item.summary,
                error=item.error_message,
                task_result=(item.task_snapshot or {}).get("result"),
                task_error=(item.task_snapshot or {}).get("error"),
                repo=item.repo,
                issue_number=item.issue_number,
                evidence=evidence,
            )
        )
        if item.repo and item.issue_number is not None:
            key = item.repo, item.issue_number
            if key not in issues:
                issues[key] = WorkIssue(
                    repo=item.repo,
                    issue_number=item.issue_number,
                    url=f"https://github.com/{quote(item.repo, safe='/')}/issues/{item.issue_number}",
                    invocation_ids=[],
                )
            issues[key].invocation_ids.append(item.invocation_id)
    seen_external: set[tuple[str, str, str, str]] = set()
    for classified in external_events or []:
        event = classified.event
        identity = (event.provider, event.repository, event.kind, event.event_id)
        if event.kind == "assignment" or identity in seen_external:
            continue
        seen_external.add(identity)
        source_id = ":".join(identity)
        external.append(
            WorkExternalEvent(
                provider=event.provider,
                source_id=source_id,
                event_kind=event.kind,
                repository=event.repository,
                source_url=event.source_url,
                occurred_at=event.occurred_at,
                actor_id=event.actor_id,
                attribution=classified.attribution,
                human_work=classified.human_work,
            )
        )
        timeline.append(WorkTimelineEntry(source_type=event.provider, source_id=source_id, occurred_at=event.occurred_at))
    timeline.sort(key=lambda entry: (_time(entry.occurred_at), entry.source_type, entry.source_id))
    return WorkSummary(runs=runs, issues=list(issues.values()), external_events=external, timeline=timeline)
