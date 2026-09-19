"""Observe completed engine attempts without equating worker exit with success.

Stories require merged code and successful checks. Evaluations present their
finished run for human evidence review; only the existing approval boundary may
accept that result. Missing observations leave the node unchanged.

## Two evidence paths, and the boundary between them (#5301)

`GitHubEvidenceSource.merged_story` asks whether the *issue* was closed by a merged
PR with green checks. That question is unanswerable for a PR whose body says
`Issue #5049` rather than `Closes #5049`: no closing event is ever created, the
issue stays open, and the story waits in `awaiting_merge` forever while its
implementation PR sits merged. That is the observed U11 shape, and it is a missing
*association* rather than polling latency.

So a dispatch that carries a durable PR binding is reconciled from that binding —
`bound_pull_request` reads provider truth about the specific PR, and
`pr_bindings.evidence_for_binding` decides completion from it. GitHub issue closure
becomes a projection of delivery rather than its sole authority.

The boundary between the two paths is deliberate and is **not** "no binding falls
back to the issue-closure path". Applied unconditionally that would let newly
unregistered work complete through issue closure anyway, which is exactly the
unregistered-PR case the issue requires to be refused. Instead the dispatch record
itself says which contract it was dispatched under: `dispatch_pass` stamps
`pr_binding_required` on the `NODE_DISPATCHED` decision it writes, so

* a **binding-required** dispatch with no valid association **holds**, with the
  specific reason surfaced, and
* a **legacy** dispatch with no recovered binding keeps the issue-closure path;
  an attributed recovered binding takes precedence over that fallback.

Reading the marker off the decision rather than comparing timestamps is what makes
the boundary deterministic and testable: a dispatch's own record states its
contract, so no wall-clock or deploy-time reasoning is involved.

## A merged PR is not the end of delivery (#5144)

Merge evidence answers "did this code land". It says nothing about the review,
deployment and evaluation a worker's clean exit can leave outstanding — the worker
process exits 0, the PR is merged, and nothing durable records what is still owed.

So a dispatch marked `handoff_required` must ALSO carry a durable continuation
receipt before it may pass, and a missing or unattributable one holds the node
rather than completing it. The check sits after the merge evidence deliberately: an
unmarked legacy dispatch reaches exactly the code it reached before, and the marked
case pays one extra read only on the path that was about to pass anyway.

The receipt is read, never created. Manufacturing an execution row to hang a receipt
on would fabricate the evidence being checked for, so an absent receipt is a hold and
an unverifiable one is treated as absent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.base import utcnow

from .dispatch_pass import attempt_run_id, resolve_installation_id
from .handoff import current_identity, handoff_required, missing_receipt_hold, receipt_for
from .models import DecisionKind, NodeKind, OrchestrationDecision, OrchestrationNode
from .pr_bindings import (
    BindingError,
    BindingRefusal,
    MergeEvidence,
    active_binding_for_node,
    binding_scope_matches,
    binding_snapshot,
    completion_candidate,
    evidence_for_binding,
    hold_explanation,
)
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
    async def bound_pull_request(
        self,
        *,
        org_id: str,
        installation_id: int,
        repo: str,
        pr_number: int,
    ) -> MergeEvidence | None:
        """Provider truth about one specific pull request (#5301).

        Asks about the *pull request*, not the issue's closure timeline, which is
        what makes the U11 shape observable: a merged PR is merged whether or not it
        ever closed an issue.

        Returns `None` when the provider cannot answer (the PR is absent, or the
        query failed), which reconciliation treats as "no evidence yet" rather than
        as a pass — `evidence_for_binding` has no arm that turns an absent answer
        into completion.

        Three things are read beyond merge state, each because a binding must be
        *falsifiable*:

        * `headRefOid` — the PR's current head. Compared against the bound head, so
          commits pushed after registration invalidate the eligibility the previous
          head earned.
        * `statusCheckRollup` on the head commit — required checks.
        * each reviewer's latest opinion, matched to the current head and excluding
          the PR author. A withdrawn approval, incomplete review requirement, or
          missing required check holds completion. This is the arm U11 fails: its PR was
          merged by the reviewer bot after GitHub refused its formal self-approval,
          so it carries no independent approval of any head.
        """
        from src.admin.connections.github_client import GitHubAppClient
        from src.knowledge.github_app_service import resolve_tenant_app_credentials

        owner, name = repo.split("/", 1)
        app_id, private_key = await resolve_tenant_app_credentials(org_id)
        app = GitHubAppClient(app_id, private_key)
        try:
            token = await app.get_installation_token(installation_id)
            query = """query($owner:String!,$name:String!,$pr:Int!) {
              repository(owner:$owner,name:$name) { databaseId pullRequest(number:$pr) {
                id url merged mergedAt headRefOid reviewDecision mergeCommit { oid }
                author { login }
                commits(last:1) { nodes { commit { statusCheckRollup { state } } } }
                reviews(last:100) { pageInfo { hasPreviousPage } nodes { state submittedAt commit { oid } author { login } } }
              } }
            }"""
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    "https://api.github.com/graphql",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"query": query, "variables": {"owner": owner, "name": name, "pr": pr_number}},
                )
                response.raise_for_status()
                payload = response.json()
            if payload.get("errors"):
                raise RuntimeError("GitHub could not verify bound pull-request evidence")
            record = (payload.get("data", {}).get("repository") or {}).get("pullRequest") or {}
            if not record:
                return None
            head = record.get("headRefOid") or ""
            commits = (record.get("commits") or {}).get("nodes", [])
            rollup = ((commits[-1].get("commit") or {}).get("statusCheckRollup") or {}) if commits else {}
            pr_author = ((record.get("author") or {}).get("login") or "").lower()
            reviews = record.get("reviews") or {}
            latest: dict[str, str] = {}
            for review in sorted(reviews.get("nodes", []), key=lambda item: item.get("submittedAt") or ""):
                author = ((review.get("author") or {}).get("login") or "").lower()
                state = review.get("state")
                if author not in {"", pr_author} and state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                    # A later contrary opinion withdraws an earlier approval,
                    # including when the later review describes another head.
                    latest[author] = state if ((review.get("commit") or {}).get("oid") or "") == head else "STALE"
            approved_by_non_author = (
                not (reviews.get("pageInfo") or {}).get("hasPreviousPage", False)
                and "APPROVED" in latest.values()
                and "CHANGES_REQUESTED" not in latest.values()
                and record.get("reviewDecision") not in {"CHANGES_REQUESTED", "REVIEW_REQUIRED"}
            )
            return MergeEvidence(
                merged=bool(record.get("merged")),
                head_sha=head,
                checks_successful=rollup.get("state") == "SUCCESS",
                approved_by_non_author=approved_by_non_author,
                merge_commit_sha=(record.get("mergeCommit") or {}).get("oid"),
                merged_at=record.get("mergedAt"),
                url=record.get("url"),
                provider_repository_id=(payload.get("data", {}).get("repository") or {}).get("databaseId"),
                provider_pr_node_id=record.get("id"),
            )
        finally:
            await app.aclose()

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


# The legacy hold text, kept verbatim for dispatches that predate PR binding so
# their observable behaviour does not change.
_LEGACY_WAIT = "Agent finished. Waiting for the issue to be completed by a merged pull request with successful checks."


def binding_required(dispatch: dict[str, Any]) -> bool:
    """Whether this dispatch must be reconciled from a durable PR binding (#5301).

    Read from the dispatch record's own marker rather than from a date, so the
    contract a run was dispatched under is a property of that run. A dispatch
    written before this contract existed carries no marker and keeps the legacy
    issue-closure path; everything dispatched after it must produce a binding, and
    holds rather than falling back if it has none.

    The fallback is deliberately not universal. "No binding, so use issue closure"
    applied to every dispatch would let newly unregistered work complete on a
    closing keyword, which is the unregistered-PR case #5301 requires to be
    refused.
    """
    return bool(dispatch.get("pr_binding_required"))


async def _story_evidence(
    session: AsyncSession,
    *,
    node: OrchestrationNode,
    dispatch: dict[str, Any],
    source: Any,
    installation_id: int,
    observation: dict | None = None,
) -> tuple[str | None, str]:
    """Completion evidence for one finished story attempt.

    Returns `(url, hold_reason)`. A non-None url means the story may pass; otherwise
    the hold reason is specific enough for an operator to act on, which the original
    generic "waiting for the issue to be completed" was not — it described, in U11's
    case, something that could never happen.
    """
    try:
        binding = await active_binding_for_node(session, org_id=node.org_id, node_id=node.id, attempt=node.attempts)
    except BindingError as exc:
        return None, hold_explanation(exc.code)
    if binding is None and not binding_required(dispatch):
        # Legacy dispatch: unchanged issue-closure evidence.
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
        return url, _LEGACY_WAIT

    if binding is not None and observation is not None:
        observation["binding"] = binding_snapshot(binding)
    refusal = completion_candidate(binding)
    if refusal is not None:
        # No binding, superseded, or a reviewer artifact — refused before any
        # provider call, since none of those can be fixed by asking GitHub.
        return None, hold_explanation(refusal)
    assert binding is not None  # completion_candidate returns NO_BINDING otherwise
    if not binding_scope_matches(binding, node):
        return None, hold_explanation(BindingRefusal.SCOPE_CHANGED)

    if binding.installation_id and binding.installation_id != installation_id:
        # The binding was registered against a different GitHub installation than
        # this tenant now resolves to. Reading the PR through the current one could
        # answer about a different repository of the same name.
        return None, hold_explanation(BindingRefusal.REPOSITORY_MISMATCH)

    provider_evidence: MergeEvidence | None = await asyncio.wait_for(
        source.bound_pull_request(
            org_id=node.org_id,
            installation_id=binding.installation_id or installation_id,
            repo=binding.repo,
            pr_number=binding.pr_number,
        ),
        timeout=15,
    )
    url, refusal = evidence_for_binding(binding, provider_evidence)
    if url:
        if observation is not None:
            observation["merge_receipt"] = asdict(provider_evidence)
        # #5144: provider evidence about the PR is necessary but not sufficient. A run
        # dispatched under the handoff contract must also have committed a durable
        # continuation receipt, because a merged PR says nothing about the review,
        # deployment and evaluation the worker's clean exit left outstanding.
        #
        # Checked AFTER the merge evidence rather than before it, so an unmarked legacy
        # dispatch reaches exactly the code it reached before and the marked case pays
        # one extra read only on the path that was about to pass.
        if handoff_required(dispatch):
            receipt = await _delivery_receipt(session, node=node)
            if receipt is None:
                # Absent OR unattributable. Fail closed: a receipt this attempt cannot
                # be shown to own is not evidence about this attempt.
                return None, missing_receipt_hold()
            if observation is not None:
                observation["handoff_receipt_ref"] = receipt
        return url, ""
    return None, hold_explanation(refusal) if refusal else _LEGACY_WAIT


async def _delivery_receipt(session: AsyncSession, *, node: OrchestrationNode, lock: bool = False) -> str | None:
    """This node's durable continuation receipt, or ``None`` when there is none.

    A read only. The absence of a receipt leaves the node held by the caller — nothing
    is created, advanced or repaired here, because inventing an execution row to hang a
    receipt on would manufacture the very evidence being checked for.

    ``lock=True`` for the commit-time revalidation (#5144 F2): the receipt's authority
    is re-derived under the execution store's lock order rather than trusted from the
    earlier lock-free snapshot. Both the identity and the receipt are resolved again,
    because a handover advances the claim generation on the *identity*, and re-checking
    only the stored string would compare a fresh receipt against stale fences.
    """
    identity = await current_identity(session, org_id=node.org_id, node_id=node.id)
    if identity is None:
        # Marked as requiring a receipt but carrying no execution row. Fail closed: the
        # caller holds, rather than passing work whose continuation cannot be verified.
        return None
    return await receipt_for(session, identity=identity, lock=lock)


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
                recovered_without_run_record = False
                recovered_from_skipped_run = False
                recoverable_skipped_run = row is not None and row.get("status") == "skipped" and row.get("skip_reason") == "idempotency_merged_pr"
                if row is None or recoverable_skipped_run:
                    # An attributed recovery is specifically the operator attesting that
                    # historical work predates (or escaped) the normal run/binding seam.
                    # Requiring the missing run row before inspecting that binding makes
                    # the recovery circular: the exact absence it exists to repair keeps
                    # the story running forever (#5358).  Only a human-established,
                    # current-attempt recovery gets this path. Ordinary worker bindings
                    # still require their completion receipt below.
                    if node.kind != NodeKind.STORY.value:
                        report.waiting += 1
                        continue
                    recovered = await active_binding_for_node(
                        session,
                        org_id=node.org_id,
                        node_id=node.id,
                        attempt=node.attempts,
                    )
                    if recovered is None or recovered.registered_by_kind != ActorKind.HUMAN.value or not recovered.recovery_reason:
                        report.waiting += 1
                        continue
                    if row is None:
                        row = {
                            "tenant_id": node.org_id,
                            "engine_node_id": node.id,
                            "engine_attempt": node.attempts,
                            "status": "complete",
                        }
                        recovered_without_run_record = True
                    else:
                        # The worker can positively suppress a duplicate attempt
                        # after finding an already-merged PR. Preserve and validate
                        # that run's stored identity below, but let the attributed
                        # binding prove which exact PR completed the accepted scope.
                        row = {**row, "status": "complete"}
                        recovered_from_skipped_run = True
                if row.get("tenant_id") != node.org_id or row.get("engine_node_id") != node.id or row.get("engine_attempt") != node.attempts:
                    raise ValueError("run record does not match node/tenant/attempt")
                status = row.get("status")
                observation: dict = {}
                if recovered_without_run_record:
                    observation["recovered_without_run_record"] = True
                if recovered_from_skipped_run:
                    observation.update(
                        recovered_from_skipped_run=True,
                        recovered_run_status="skipped",
                        recovered_skip_reason="idempotency_merged_pr",
                    )
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
                        url, hold = await _story_evidence(
                            session,
                            node=node,
                            dispatch=dispatch,
                            source=source,
                            installation_id=installation_id,
                            observation=observation,
                        )
                        if not url:
                            target, detail = NodeState.AWAITING_MERGE, hold
                        else:
                            target, detail = NodeState.PASSED, f"Story completed by merged pull request with successful checks: {url}"
                else:
                    report.waiting += 1
                    continue
                observed_state = node.state
                # Provider reads run without locks. Before writing either a hold
                # or completion, serialize with registration/replacement and retry,
                # then revalidate the exact revision the provider evidence names.
                locked = (
                    await session.execute(
                        select(OrchestrationNode)
                        .where(
                            OrchestrationNode.id == node.id,
                            OrchestrationNode.org_id == node.org_id,
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                if locked.state != observed_state or locked.attempts != dispatch["attempt"]:
                    report.waiting += 1
                    continue
                if node.kind == NodeKind.STORY.value and status == "complete":
                    try:
                        current = await active_binding_for_node(session, org_id=node.org_id, node_id=node.id, attempt=node.attempts)
                    except BindingError as exc:
                        current = None
                        target, detail = NodeState.AWAITING_MERGE, hold_explanation(exc.code)
                        observation.clear()
                    snapshot = observation.get("binding")
                    changed = (
                        snapshot is not None
                        and (
                            current is None
                            or current.id != snapshot["id"]
                            or current.revision != snapshot["revision"]
                            or (target == NodeState.PASSED and not binding_scope_matches(current, node))
                        )
                    ) or (snapshot is None and current is not None)
                    # #5144 F2: the receipt read in `_story_evidence` was taken without
                    # locks, alongside the provider call. Between that snapshot and this
                    # write a handover can advance the claim generation, and the receipt
                    # this attempt was about to pass on becomes unattributable — so the
                    # authority backing a PASSED transition is revalidated here, under
                    # the node lock, rather than trusted from the earlier read.
                    #
                    # Only on the passing path. A hold needs no receipt authority, and
                    # re-reading for it would add a locked query to the common case that
                    # changes no outcome.
                    #
                    # Locked, and after the node lock, deliberately: `receipt_for` takes
                    # the store's flow → claim → accepted-plan → execution order, which
                    # is the same order the worker write path takes behind the same node
                    # lock, so the result sweep and a concurrent commit serialize instead
                    # of deadlocking.
                    if target == NodeState.PASSED and handoff_required(dispatch):
                        revalidated = await _delivery_receipt(session, node=locked, lock=True)
                        if revalidated != observation.get("handoff_receipt_ref"):
                            # Fail closed, and held rather than failed: the work is
                            # genuinely still outstanding, and the next sweep re-reads
                            # whatever superseded this.
                            target = NodeState.AWAITING_MERGE
                            detail = missing_receipt_hold("the continuation receipt's authority changed during verification")
                            observation.pop("merge_receipt", None)
                            observation.pop("handoff_receipt_ref", None)
                    if changed:
                        target = NodeState.AWAITING_MERGE
                        detail = "The implementation binding changed during verification; its current head and scope will be verified again."
                        observation.pop("merge_receipt", None)
                        observation["binding"] = binding_snapshot(current) if current else None
                payload = {"attempt": dispatch["attempt"], "run_id": dispatch["run_id"], "evidence": detail, **observation}
                if target.value == node.state:
                    previous = (
                        await session.execute(
                            select(OrchestrationDecision)
                            .where(
                                OrchestrationDecision.org_id == node.org_id,
                                OrchestrationDecision.node_id == node.id,
                                OrchestrationDecision.kind == DecisionKind.RESULT_OBSERVED.value,
                            )
                            .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if previous is None or json.loads(previous.reason or "{}") != payload:
                        session.add(
                            OrchestrationDecision(
                                org_id=node.org_id,
                                flow_id=node.flow_id,
                                node_id=node.id,
                                kind=DecisionKind.RESULT_OBSERVED.value,
                                actor_id="system:orchestration-results",
                                actor_role="engine",
                                actor_kind=ActorKind.SERVICE.value,
                                from_state=node.state,
                                to_state=node.state,
                                reason=json.dumps(payload),
                            )
                        )
                        await session.flush()
                    report.waiting += 1
                    report.reasons[node.id] = detail
                    continue
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
                            reason=json.dumps(payload),
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
