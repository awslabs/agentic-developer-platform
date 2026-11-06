"""Retry-safe publication of newly proposed amendment work as GitHub issues (#5329).

The database, not the provider, decides whether an issue for a piece of proposed work
already exists. `orchestration_amendment_issue_bindings` is unique on
`(org_id, repository, proposal_identity)`:

1. The first publisher inserts a `pending` row carrying a fresh claim token and commits
   it **before** asking the provider to create anything. A concurrent publisher loses the
   unique index and never reaches the provider.
2. Immediately before POST/PATCH, the provider atomically changes its live reservation
   to `writing`. This one-shot intent is durable before sending and cannot be reassigned
   by lease expiry. The issue carries an opaque attempt marker, never the claim token.
3. A retry that finds a `created` row with matching content reuses its issue with no
   listing and no time window. Content drift claims that same row as `pending` before
   updating the issue, and token-checked completion records the new content hash.
   Neither acceptance nor another publisher trusts an unfinished update.
4. An expired `pending` reservation can be replaced: its old owner cannot cross the
   provider write boundary afterwards. An expired `writing` intent can only be resolved
   by observing that exact attempt on the app-owned issue. Until then, neither another
   write nor acceptance is allowed, even if the request timed out or the process died.
   Known issue numbers are read directly; creation recovery uses the bounded identity
   marker listing. A confirmed attempt records observed content before a new revision
   may update it. An absent or unverifiable attempt is never permission to repost.
5. A definitive provider rejection of POST/PATCH invalidates only that attempt's token
   and restores an expired `pending` reservation. Authorization, missing-target,
   validation and rate-limit rejections permit retry; other errors remain uncertain.

Only human-authorized acceptance calls `publish_unbound_work`, and only after the exact
revision, base and every other acceptance blocker have been checked (see
`amendment_review_routes.accept_draft`). Every target repository must be listed in the
flow's in-force execution policy `repository_ids` (and in the proposal's own policy when
it carries one), and one request may create at most `MAX_ISSUES_PER_REQUEST` issues.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from src.admin.installations.resolver import OwnerState, resolve_installation_owner

from .evaluation_issue_provider import EvaluationIssueProvider
from .execution_policy import ExecutionPolicy
from .models import AmendmentIssueBindingState, NodeKind, OrchestrationAmendmentIssueBinding
from .proposal import LoopProposal, PlanningIssueReference, ProposedNode, Violation
from .review_cycle import CycleBlockedError

#: Upper bound on issues one request may create. Small and explicit: an amendment that
#: needs more new tracked work than this should be split, and a runaway document must not
#: be able to spam a repository.
MAX_ISSUES_PER_REQUEST = 20
#: Pre-write reservation lifetime and delay before reconciling an unfinished write.
#: Expiry never grants permission to repeat a write whose result is unproven.
CLAIM_LEASE = timedelta(minutes=5)
#: After a claimant's own provider call fails, its claim becomes recoverable this soon.
SETTLE_AFTER_FAILURE = timedelta(seconds=30)
#: Marker reconciliation pages (100 issues each) scanned before refusing.
LISTING_PAGES = 20

REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
PUBLISHED_KINDS = frozenset({NodeKind.STORY.value, NodeKind.EVAL.value})
ISSUE_WRITE_REJECTIONS = frozenset({401, 403, 404, 410, 422, 429})


@dataclass(frozen=True)
class PublishedAmendmentIssue:
    number: int
    repository_id: int
    url: str
    node_id: str | None = None
    content_hash: str | None = None
    write_id: str | None = None
    issue_id: int | None = None


@dataclass(frozen=True)
class PlannedPublication:
    """What acceptance will do for one unbound new node.

    `action` is `create` (no binding yet), `pending` (a publication was started and is
    not recorded yet; acceptance reconciles it), `reuse` (an issue is recorded) or
    `update` (an issue is recorded and its public content will be refreshed).
    """

    address: str
    proposal_identity: str
    repository: str
    action: str
    issue_number: int | None = None
    provider_repository_id: int | None = None
    url: str | None = None
    parent_repository: str | None = None
    parent_issue_number: int | None = None

    @property
    def creates_issue(self) -> bool:
        return self.action in {"create", "pending"}


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def repository_key(name: str) -> str:
    return name.lower()


def publishable_repository(name: str | None) -> bool:
    return bool(name) and REPOSITORY_PATTERN.fullmatch(name) is not None and not any(part in {".", ".."} for part in name.split("/"))


def issue_content(node: ProposedNode) -> dict[str, str]:
    if not node.proposal_identity:
        raise CycleBlockedError("amendment_issue_identity_missing")
    correlation = hashlib.sha256(node.proposal_identity.encode()).hexdigest()
    marker = f"<!-- adp-amendment-work:v1:{correlation} -->"
    purpose = "Development" if node.kind == "story" else "Validation"
    criteria = "\n".join(f"- `{item}`" for item in node.criteria) or "- Follow the accepted amendment criteria."
    evidence = "\n".join(f"- `{item}`" for item in node.evidence_expectations) or "- Follow the engine completion contract."
    body = (
        f"{marker}\n\n{purpose} work proposed as `{node.address}`. This issue does not authorize execution; "
        "execution starts only after the exact amendment revision is accepted.\n\n"
        f"## Criteria\n{criteria}\n\n## Evidence\n{evidence}\n\n"
        "Required inputs and credentials are intentionally omitted. They remain named prerequisites in the protected plan document.\n"
    )
    if len(body.encode()) > 32768:
        raise CycleBlockedError("amendment_issue_content_limit")
    return {"title": node.title, "body": body, "correlation": correlation, "marker": marker}


def content_hash(content: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps({"title": content["title"], "body": content["body"]}, sort_keys=True).encode()).hexdigest()


def write_identity(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def observed_content(data: dict) -> tuple[dict[str, str], str | None]:
    body = data.get("body") or ""
    if not isinstance(body, str) or not isinstance(data.get("title"), str):
        raise CycleBlockedError("amendment_issue_mismatch")
    logical_body, _, marker = body.rpartition("\n")
    match = re.fullmatch(r"<!-- adp-amendment-write:v1:([0-9a-f]{64}) -->", marker)
    return {"title": data.get("title") or "", "body": logical_body if match else body}, match[1] if match else None


def permitted_repositories(in_force_document: dict | None, proposal: LoopProposal) -> frozenset[str]:
    """Repositories new work may be published to: the in-force policy's, narrowed by the proposal's.

    No in-force policy (or an unreadable one) permits nothing. A proposal carrying its own
    policy narrows the set, because that policy is what will dispatch the work.
    """
    raw = (in_force_document or {}).get("execution_policy")
    if raw is None:
        return frozenset()
    try:
        allowed = set(ExecutionPolicy.model_validate(raw).repository_ids)
    except ValueError:
        return frozenset()
    proposed = proposal.execution_policy or proposal.proposed_execution_policy
    if proposed is not None:
        allowed &= set(proposed.repository_ids)
    return frozenset(allowed)


def unbound_new_work(proposal: LoopProposal, existing_addresses: set[str]) -> list[ProposedNode]:
    return [node for node in proposal.nodes if node.address not in existing_addresses and node.kind in PUBLISHED_KINDS and not node.issue_ref]


def native_parent(proposal: LoopProposal, node: ProposedNode) -> PlanningIssueReference | None:
    epic_address = "/".join(node.address.split("/")[:2])
    for reference in proposal.tracker_refs or []:
        if reference.scope == "epic" and reference.address == epic_address:
            return reference.issue
    return None


async def verified_parent_issue_id(session, *, org_id: str, parent: PlanningIssueReference) -> int:
    from .repository_binding import RepositoryBindingError, resolve_story_repository

    resolved = await resolve_story_repository(
        session,
        tenant_id=org_id,
        name=parent.repository,
        repository_id=parent.repository_id,
        issue=parent.issue_number,
        installation_id=parent.installation_id,
        operation="preview",
    )
    if (
        resolved.repository_id != parent.repository_id
        or resolved.installation_id != parent.installation_id
        or resolved.issue_number != parent.issue_number
        or type(resolved.issue_id) is not int
        or resolved.issue_id <= 0
    ):
        raise RepositoryBindingError("amendment_issue_parent_unverifiable")
    return resolved.issue_id


async def verify_adopted_story_issues(session, *, org_id: str, proposal: LoopProposal, base_document: dict | None) -> list[Violation]:
    """Refuse unverified adopted story issues before any publication or acceptance."""
    if proposal.product_id is None or proposal.origin_ref is None or not proposal.tracker_refs:
        return []
    from .repository_binding import RepositoryBindingError, resolve_story_repository

    previous = {node.get("address"): node for node in (base_document or {}).get("nodes", []) if isinstance(node, dict)}
    violations: list[Violation] = []
    checked: set[tuple[str, int, int]] = set()
    for node in proposal.nodes:
        if node.kind != "story" or not node.issue_ref:
            continue
        before = previous.get(node.address) or {}
        identity = (
            node.issue_ref,
            node.repository_identity,
            node.provider_repository_id,
            node.repository_connection.model_dump(mode="json") if node.repository_connection else None,
        )
        if (
            identity
            == tuple(
                before.get(key)
                for key in (
                    "issue_ref",
                    "repository_identity",
                    "provider_repository_id",
                    "repository_connection",
                )
            )
            and (base_document or {}).get("origin_ref") == proposal.origin_ref.model_dump(mode="json")
            and (base_document or {}).get("tracker_refs") == [ref.model_dump(mode="json") for ref in proposal.tracker_refs]
        ):
            continue
        if not node.repository_identity or not node.provider_repository_id or not node.repository_connection:
            violations.append(
                Violation(
                    rule="story_issue_target_unverified",
                    message="Adopted story issues require a verified repository and connection.",
                    where=node.address,
                )
            )
            continue
        try:
            number = int(node.issue_ref.lstrip("#"))
            if number <= 0:
                raise ValueError
        except ValueError:
            violations.append(Violation(rule="story_issue_invalid", message="Story issue number is invalid.", where=node.address))
            continue
        if node.proposal_identity:
            row = await _binding_row(session, org_id=org_id, repository=node.repository_identity, proposal_identity=node.proposal_identity)
            if row is not None:
                if (row.provider_repository_id, row.issue_number, row.state) != (
                    node.provider_repository_id,
                    number,
                    AmendmentIssueBindingState.CREATED.value,
                ):
                    violations.append(
                        Violation(
                            rule="amendment_issue_binding_changed",
                            message="The saved publication does not match the adopted issue.",
                            where=node.address,
                        )
                    )
                    continue
                parent = native_parent(proposal, node)
                if parent is not None:
                    if row.parent_repository_id != parent.repository_id or row.link_confirmed_at is None:
                        violations.append(
                            Violation(
                                rule="amendment_issue_link_unconfirmed",
                                message="The saved issue has no confirmed native epic link.",
                                where=node.address,
                            )
                        )
                    else:
                        try:
                            parent_issue_id = await verified_parent_issue_id(session, org_id=org_id, parent=parent)
                        except RepositoryBindingError:
                            violations.append(
                                Violation(
                                    rule="amendment_issue_parent_unavailable",
                                    message="The native epic could not be verified.",
                                    where=node.address,
                                )
                            )
                        else:
                            if row.parent_issue_id != parent_issue_id:
                                violations.append(
                                    Violation(
                                        rule="amendment_issue_parent_changed",
                                        message="The saved issue is linked to another epic.",
                                        where=node.address,
                                    )
                                )
                continue
        key = (node.repository_identity, node.repository_connection.installation_id, number)
        if key in checked:
            continue
        if len(checked) >= MAX_ISSUES_PER_REQUEST:
            violations.append(
                Violation(
                    rule="story_issue_verification_limit",
                    message="Verify fewer adopted issues per amendment.",
                    where=node.address,
                )
            )
            continue
        checked.add(key)
        try:
            verified = await resolve_story_repository(
                session,
                tenant_id=org_id,
                name=node.repository_identity,
                repository_id=node.provider_repository_id,
                issue=number,
                installation_id=node.repository_connection.installation_id,
                operation="preview",
            )
        except RepositoryBindingError as error:
            violations.append(Violation(rule=error.code, message="Adopted story issue could not be verified.", where=node.address))
            continue
        if (verified.repository_id, verified.installation_id, verified.issue_number) != (
            node.provider_repository_id,
            node.repository_connection.installation_id,
            number,
        ):
            violations.append(
                Violation(
                    rule="story_issue_target_mismatch",
                    message="The issue belongs to a different repository or connection.",
                    where=node.address,
                )
            )
    return violations


async def _binding_row(
    session,
    *,
    org_id: str,
    repository: str,
    proposal_identity: str,
    provider_repository_id: int | None = None,
    lock: bool = False,
) -> OrchestrationAmendmentIssueBinding | None:
    query = (
        select(OrchestrationAmendmentIssueBinding)
        .where(
            OrchestrationAmendmentIssueBinding.org_id == org_id,
            OrchestrationAmendmentIssueBinding.proposal_identity == proposal_identity,
        )
        .execution_options(populate_existing=True)
    )
    if provider_repository_id is not None:
        by_id = query.where(OrchestrationAmendmentIssueBinding.provider_repository_id == provider_repository_id)
        row = await session.scalar(by_id.with_for_update() if lock else by_id)
        if row is not None:
            return row
    by_name = query.where(OrchestrationAmendmentIssueBinding.repository == repository_key(repository))
    return await session.scalar(by_name.with_for_update() if lock else by_name)


async def plan_publications(
    session,
    *,
    org_id: str,
    proposal: LoopProposal,
    existing_addresses: set[str],
    permitted: frozenset[str],
    lock: bool = False,
) -> tuple[list[PlannedPublication], list[Violation]]:
    """What acceptance would publish, and every reason it could not. Reads only."""
    from .repository_binding import RepositoryBindingError

    plans: list[PlannedPublication] = []
    violations: list[Violation] = []
    verified_parents: dict[PlanningIssueReference, int | None] = {}
    for node in unbound_new_work(proposal, existing_addresses):
        repository = node.repository_identity or ""
        parent = native_parent(proposal, node)
        if not node.proposal_identity or not publishable_repository(repository):
            violations.append(
                Violation(
                    rule="missing_issue_binding",
                    message=(
                        "New development and validation work must either adopt an existing issue (`issue_ref`) or name a "
                        "`proposal_identity` and an `owner/name` `repository_identity` so acceptance can publish one; "
                        "otherwise the normal scheduler has no durable dispatch identity."
                    ),
                    where=node.address,
                )
            )
            continue
        if repository not in permitted:
            violations.append(
                Violation(
                    rule="amendment_issue_repository_not_permitted",
                    message=(
                        f"Repository {repository!r} is not in the flow's in-force execution policy `repository_ids`"
                        f"{' (or the proposal narrows it out)' if proposal.execution_policy is not None else ''}; "
                        "no issue may be published there. Target a permitted repository or amend the policy first."
                    ),
                    where=node.address,
                )
            )
            # Still listed: the reviewer must see where this work wanted to go.
        row = await _binding_row(
            session,
            org_id=org_id,
            repository=repository,
            proposal_identity=node.proposal_identity,
            provider_repository_id=node.provider_repository_id,
            lock=lock,
        )
        if row is not None and row.repository != repository_key(repository) and row.issue_number is not None:
            from .repository_binding import resolve_story_repository

            try:
                verified = await resolve_story_repository(
                    session,
                    tenant_id=org_id,
                    name=repository,
                    issue=row.issue_number,
                    repository_id=row.provider_repository_id,
                    installation_id=node.repository_connection.installation_id if node.repository_connection else None,
                    operation="preview",
                )
            except RepositoryBindingError:
                verified = None
            if verified is None or verified.repository_id != node.provider_repository_id or verified.issue_number != row.issue_number:
                violations.append(
                    Violation(
                        rule="amendment_issue_repository_changed",
                        message="The saved issue cannot be verified in the renamed target repository. Review the target before publication.",
                        where=node.address,
                    )
                )
        if (
            row is not None
            and row.provider_repository_id is not None
            and node.provider_repository_id is not None
            and row.provider_repository_id != node.provider_repository_id
        ):
            violations.append(
                Violation(
                    rule="amendment_issue_repository_changed",
                    message="The saved issue belongs to a different provider repository. Review the target before publication.",
                    where=node.address,
                )
            )
        if row is not None and row.parent_repository_id != (parent.repository_id if parent else None):
            violations.append(
                Violation(
                    rule="amendment_issue_parent_changed",
                    message="The saved publication has a different planning parent. Review the requested change before continuing.",
                    where=node.address,
                )
            )
        if row is not None and parent is not None and row.parent_repository_id == parent.repository_id:
            if parent not in verified_parents:
                try:
                    verified_parents[parent] = await verified_parent_issue_id(session, org_id=org_id, parent=parent)
                except RepositoryBindingError:
                    verified_parents[parent] = None
            if verified_parents[parent] is None:
                violations.append(
                    Violation(
                        rule="amendment_issue_parent_unavailable",
                        message="The native epic could not be verified.",
                        where=node.address,
                    )
                )
            elif row.parent_issue_id != verified_parents[parent]:
                violations.append(
                    Violation(
                        rule="amendment_issue_parent_changed",
                        message="The saved issue is linked to another epic.",
                        where=node.address,
                    )
                )
        if row is None:
            action = "create"
        elif row.state != AmendmentIssueBindingState.CREATED.value or row.issue_number is None:
            action = "pending"
        elif parent is not None and row.link_confirmed_at is None:
            action = "pending_link"
        else:
            action = "reuse" if row.content_hash == content_hash(issue_content(node)) else "update"
        plans.append(
            PlannedPublication(
                address=node.address,
                proposal_identity=node.proposal_identity,
                repository=repository,
                action=action,
                issue_number=row.issue_number if row is not None and action in {"reuse", "update", "pending_link"} else None,
                provider_repository_id=row.provider_repository_id if row is not None else node.provider_repository_id,
                url=row.issue_url if row is not None and action in {"reuse", "update", "pending_link"} else None,
                parent_repository=parent.repository if parent else None,
                parent_issue_number=parent.issue_number if parent else None,
            )
        )
    creating = [plan for plan in plans if plan.creates_issue]
    if len(creating) > MAX_ISSUES_PER_REQUEST:
        violations.append(
            Violation(
                rule="amendment_issue_limit_exceeded",
                message=(
                    f"This amendment would create {len(creating)} new issues; at most {MAX_ISSUES_PER_REQUEST} may be "
                    "created by one request. Split the amendment or adopt existing issues."
                ),
                where=None,
            )
        )
    return plans, violations


def apply_publications(proposal: LoopProposal, plans: list[PlannedPublication]) -> LoopProposal:
    """Bind recorded issues onto their nodes. Deterministic: same rows, same document."""
    recorded = {plan.address: plan for plan in plans if plan.issue_number is not None and plan.action != "pending_link"}
    if not recorded:
        return proposal
    nodes = [
        node.model_copy(
            update={
                "issue_ref": str(recorded[node.address].issue_number),
                "provider_repository_id": recorded[node.address].provider_repository_id or node.provider_repository_id,
            }
        )
        if node.address in recorded
        else node
        for node in proposal.nodes
    ]
    return proposal.model_copy(update={"nodes": nodes})


class AmendmentIssuePublisher(EvaluationIssueProvider):
    """The provider HTTP layer. Holds no publication state; the binding table does."""

    async def validated(self, binding, data, app_id, *, token, number=None) -> PublishedAmendmentIssue:
        found = data.get("number") if isinstance(data, dict) else None
        if (
            type(found) is not int
            or found <= 0
            or (number is not None and found != number)
            or data.get("pull_request") is not None
            or not await self.app_owns_issue(binding, data, app_id, token=token)
            or data.get("html_url") != f"https://github.com/{binding.repo}/issues/{found}"
        ):
            raise CycleBlockedError("amendment_issue_mismatch")
        node_id = data.get("node_id") if isinstance(data.get("node_id"), str) else None
        content, write_id = observed_content(data)
        return PublishedAmendmentIssue(
            number=found,
            repository_id=binding.provider_repository_id,
            url=data["html_url"],
            node_id=node_id,
            content_hash=content_hash(content),
            write_id=write_id,
            issue_id=data.get("id") if type(data.get("id")) is int and data["id"] > 0 else None,
        )

    async def find_issue(self, binding, content, *, since: datetime) -> PublishedAmendmentIssue | None:
        """Reconcile by marker and app identity only. Title and body are not compared."""
        token, app_id = await self.credentials(binding)
        await self.repository(binding, token)
        found: dict[int, PublishedAmendmentIssue] = {}
        for page in range(1, LISTING_PAGES + 1):
            rows = (
                await self.request(
                    binding,
                    "GET",
                    f"/repos/{binding.repo}/issues",
                    token=token,
                    params={"state": "all", "since": (since - timedelta(minutes=1)).isoformat(), "per_page": 100, "page": page},
                )
            ).json()
            if not isinstance(rows, list) or len(rows) > 100 or any(not isinstance(row, dict) for row in rows):
                raise CycleBlockedError("amendment_issue_listing_invalid")
            for row in rows:
                # Another actor can paste the marker; only this app's issues count.
                if content["marker"] in str(row.get("body") or "") and await self.app_owns_issue(binding, row, app_id, token=token):
                    issue = await self.validated(binding, row, app_id, token=token)
                    found[issue.number] = issue
            if len(found) > 1:
                raise CycleBlockedError("amendment_issue_ambiguous")
            if len(rows) < 100:
                return next(iter(found.values()), None)
        # Absence was not proven, so creating could duplicate. Refuse.
        raise CycleBlockedError("amendment_issue_listing_incomplete")

    async def create_issue(self, binding, content) -> PublishedAmendmentIssue:
        return await self._write_issue(binding, content)

    async def update_issue(self, binding, number: int, content) -> PublishedAmendmentIssue:
        return await self._write_issue(binding, content, number=number)

    async def _write_issue(self, binding, content, *, number=None) -> PublishedAmendmentIssue:
        token, app_id = await self.credentials(binding, write=True)
        await self.repository(binding, token)
        await binding.begin_write()
        try:
            response = await self.request(
                binding,
                "PATCH" if number is not None else "POST",
                f"/repos/{binding.repo}/issues" + (f"/{number}" if number is not None else ""),
                token=token,
                json={"title": content["title"], "body": content["body"] + f"\n<!-- adp-amendment-write:v1:{binding.write_id} -->"},
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code in ISSUE_WRITE_REJECTIONS:
                await binding.reject_write()
            raise
        issue = await self.validated(binding, response.json(), app_id, token=token, number=number)
        if issue.write_id != binding.write_id or issue.content_hash != content_hash(content):
            raise CycleBlockedError("amendment_issue_write_uncertain")
        return issue

    async def read_issue(self, binding, number: int) -> PublishedAmendmentIssue:
        token, app_id = await self.credentials(binding)
        await self.repository(binding, token)
        response = await self.request(binding, "GET", f"/repos/{binding.repo}/issues/{number}", token=token)
        return await self.validated(binding, response.json(), app_id, token=token, number=number)

    async def read_native_parent(self, binding, number: int, *, parent_binding) -> dict | None:
        # A private parent outside the token's repository scope looks absent to
        # GitHub. Both already-verified relationship endpoints must be readable.
        token, _ = await self.credentials(binding, related_binding=parent_binding)
        await self.repository(binding, token)
        await self.repository(parent_binding, token)
        try:
            response = await self.request(binding, "GET", f"/repos/{binding.repo}/issues/{number}/parent", token=token)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return None
            raise CycleBlockedError("amendment_issue_link_unavailable") from None
        except httpx.HTTPError:
            raise CycleBlockedError("amendment_issue_link_unavailable") from None
        parent = response.json()
        if not isinstance(parent, dict) or type(parent.get("id")) is not int or not isinstance(parent.get("repository_url"), str):
            raise CycleBlockedError("amendment_issue_parent_unverifiable")
        return parent

    async def confirm_native_parent(self, binding, parent_binding, issue: PublishedAmendmentIssue) -> None:
        actual = await self.read_native_parent(binding, issue.number, parent_binding=parent_binding)
        expected_url = f"https://api.github.com/repos/{parent_binding.repo}"
        if actual is None:
            child = issue if issue.issue_id is not None else await self.read_issue(binding, issue.number)
            if type(child.issue_id) is not int or child.issue_id <= 0:
                raise CycleBlockedError("amendment_issue_identity_missing")
            token, _ = await self.credentials(parent_binding, write=True, related_binding=binding)
            await self.repository(parent_binding, token)
            await self.repository(binding, token)
            try:
                await self.request(
                    parent_binding,
                    "POST",
                    f"/repos/{parent_binding.repo}/issues/{parent_binding.issue_number}/sub_issues",
                    token=token,
                    json={"sub_issue_id": child.issue_id},
                )
            except httpx.HTTPError:
                raise CycleBlockedError("amendment_issue_link_unconfirmed") from None
            actual = await self.read_native_parent(binding, issue.number, parent_binding=parent_binding)
        if actual is None:
            raise CycleBlockedError("amendment_issue_link_unconfirmed")
        if actual["id"] != parent_binding.issue_id or actual["repository_url"].casefold() != expected_url.casefold():
            raise CycleBlockedError("amendment_issue_parent_conflict")


async def _release_claim(session, row_id: str, token: str) -> None:
    """Permit an earlier retry; a writing intent still requires observed completion."""
    await session.rollback()
    await session.execute(
        update(OrchestrationAmendmentIssueBinding)
        .where(
            OrchestrationAmendmentIssueBinding.id == row_id,
            OrchestrationAmendmentIssueBinding.claim_token == token,
            OrchestrationAmendmentIssueBinding.state.in_((AmendmentIssueBindingState.PENDING.value, AmendmentIssueBindingState.WRITING.value)),
        )
        .values(claimed_at=_now() - CLAIM_LEASE + SETTLE_AFTER_FAILURE)
    )
    await session.commit()


async def _record_write(session, row_id: str, token: str, issue: PublishedAmendmentIssue) -> None:
    if issue.write_id != write_identity(token) or issue.content_hash is None:
        raise CycleBlockedError("amendment_issue_write_uncertain")
    recorded = await session.execute(
        update(OrchestrationAmendmentIssueBinding)
        .where(
            OrchestrationAmendmentIssueBinding.id == row_id,
            OrchestrationAmendmentIssueBinding.claim_token == token,
            OrchestrationAmendmentIssueBinding.state == AmendmentIssueBindingState.WRITING.value,
        )
        .values(
            state=AmendmentIssueBindingState.CREATED.value,
            issue_number=issue.number,
            issue_node_id=issue.node_id,
            issue_url=issue.url,
            provider_repository_id=issue.repository_id,
            content_hash=issue.content_hash,
            completed_at=_now(),
        )
    )
    if recorded.rowcount != 1:
        await session.rollback()
        raise CycleBlockedError("amendment_issue_publication_conflict")
    await session.commit()


async def _publish_one(
    session,
    provider,
    binding,
    node: ProposedNode,
    *,
    before_claim: Callable[[], Awaitable[None]] | None = None,
    parent_repository_id: int | None = None,
    parent_issue_id: int | None = None,
) -> PublishedAmendmentIssue:
    content = issue_content(node)
    digest = content_hash(content)
    identity = {
        "org_id": binding.org_id,
        "repository": binding.repo,
        "proposal_identity": node.proposal_identity,
        "provider_repository_id": binding.provider_repository_id,
    }

    token = str(uuid4())
    fresh = False
    if before_claim is not None:
        await before_claim()
    row = await _binding_row(session, **identity)
    if row is not None and (row.parent_repository_id, row.parent_issue_id) != (parent_repository_id, parent_issue_id):
        raise CycleBlockedError("amendment_issue_parent_changed")
    if row is not None and row.provider_repository_id is not None and row.provider_repository_id != binding.provider_repository_id:
        raise CycleBlockedError("amendment_issue_repository_changed")
    if row is None:
        session.add(
            OrchestrationAmendmentIssueBinding(
                org_id=binding.org_id,
                repository=repository_key(binding.repo),
                proposal_identity=node.proposal_identity,
                provider_repository_id=binding.provider_repository_id,
                parent_repository_id=parent_repository_id,
                parent_issue_id=parent_issue_id,
                state=AmendmentIssueBindingState.PENDING.value,
                claim_token=token,
                claimed_at=_now(),
                content_hash=digest,
                created_at=_now(),
            )
        )
        try:
            # Committed BEFORE the provider call: from here on a crash leaves a row
            # that recovery reconciles, never an untracked issue.
            await session.commit()
            fresh = True
        except IntegrityError:
            await session.rollback()
            if before_claim is not None:
                await before_claim()
        row = await _binding_row(session, **identity)
        if row is None:
            raise CycleBlockedError("amendment_issue_binding_unavailable")

    if row.state == AmendmentIssueBindingState.WRITING.value:
        if _aware(row.claimed_at) > _now() - CLAIM_LEASE:
            await session.rollback()
            raise CycleBlockedError("amendment_issue_publication_in_progress")
        row_id, prior_token, issue_number, created_at = row.id, row.claim_token, row.issue_number, _aware(row.created_at)
        await session.commit()
        try:
            issue = (
                await provider.read_issue(binding, issue_number)
                if issue_number is not None
                else await provider.find_issue(binding, content, since=created_at)
            )
        except httpx.HTTPError as error:
            raise CycleBlockedError("amendment_issue_provider_unavailable") from error
        if issue is None:
            raise CycleBlockedError("amendment_issue_write_uncertain")
        await _record_write(session, row_id, prior_token, issue)
        return await _publish_one(
            session,
            provider,
            binding,
            node,
            before_claim=before_claim,
            parent_repository_id=parent_repository_id,
            parent_issue_id=parent_issue_id,
        )

    if row.state == AmendmentIssueBindingState.CREATED.value and row.issue_number is not None and row.content_hash == digest:
        recorded_issue = PublishedAmendmentIssue(
            row.issue_number, row.provider_repository_id or binding.provider_repository_id, row.issue_url or "", row.issue_node_id
        )
        await session.commit()
        return recorded_issue

    if not fresh:
        if row.state == AmendmentIssueBindingState.PENDING.value and _aware(row.claimed_at) > _now() - CLAIM_LEASE:
            await session.rollback()
            raise CycleBlockedError("amendment_issue_publication_in_progress")
        taken = await session.execute(
            update(OrchestrationAmendmentIssueBinding)
            .where(
                OrchestrationAmendmentIssueBinding.id == row.id,
                OrchestrationAmendmentIssueBinding.claim_token == row.claim_token,
                OrchestrationAmendmentIssueBinding.state == row.state,
                OrchestrationAmendmentIssueBinding.content_hash == row.content_hash,
            )
            .values(state=AmendmentIssueBindingState.PENDING.value, claim_token=token, claimed_at=_now(), completed_at=None)
        )
        if taken.rowcount != 1:
            await session.rollback()
            raise CycleBlockedError("amendment_issue_publication_in_progress")
        row = await _binding_row(session, **identity)

    if row is None or row.claim_token != token or row.state != AmendmentIssueBindingState.PENDING.value:
        await session.rollback()
        raise CycleBlockedError("amendment_issue_publication_conflict")
    row_id, row_created_at, issue_number = row.id, _aware(row.created_at), row.issue_number
    await session.commit()

    async def begin_write() -> None:
        started = await session.execute(
            update(OrchestrationAmendmentIssueBinding)
            .where(
                OrchestrationAmendmentIssueBinding.id == row_id,
                OrchestrationAmendmentIssueBinding.claim_token == token,
                OrchestrationAmendmentIssueBinding.state == AmendmentIssueBindingState.PENDING.value,
                OrchestrationAmendmentIssueBinding.claimed_at >= _now() - CLAIM_LEASE,
            )
            .values(state=AmendmentIssueBindingState.WRITING.value, content_hash=digest)
            .execution_options(synchronize_session=False)
        )
        if started.rowcount != 1:
            await session.rollback()
            raise CycleBlockedError("amendment_issue_publication_conflict")
        await session.commit()

    async def reject_write() -> None:
        rejected = await session.execute(
            update(OrchestrationAmendmentIssueBinding)
            .where(
                OrchestrationAmendmentIssueBinding.id == row_id,
                OrchestrationAmendmentIssueBinding.claim_token == token,
                OrchestrationAmendmentIssueBinding.state == AmendmentIssueBindingState.WRITING.value,
            )
            .values(state=AmendmentIssueBindingState.PENDING.value, claim_token=str(uuid4()), claimed_at=_now() - CLAIM_LEASE)
            .execution_options(synchronize_session=False)
        )
        if rejected.rowcount != 1:
            await session.rollback()
            raise CycleBlockedError("amendment_issue_publication_conflict")
        await session.commit()

    binding.begin_write = begin_write
    binding.reject_write = reject_write
    binding.write_id = write_identity(token)

    try:
        if issue_number is not None:
            issue = await provider.update_issue(binding, issue_number, content)
        else:
            issue = None if fresh else await provider.find_issue(binding, content, since=row_created_at)
            if issue is None:
                issue = await provider.create_issue(binding, content)
            else:
                issue = await provider.update_issue(binding, issue.number, content)
    except CycleBlockedError:
        await _release_claim(session, row_id, token)
        raise
    except httpx.HTTPError as error:
        await _release_claim(session, row_id, token)
        raise CycleBlockedError("amendment_issue_provider_unavailable") from error

    await _record_write(session, row_id, token, issue)
    return issue


async def publish_unbound_work(
    session,
    *,
    org_id: str,
    proposal: LoopProposal,
    existing_addresses: set[str],
    permitted: frozenset[str],
    publisher=None,
    before_claim: Callable[[], Awaitable[None]] | None = None,
) -> list[PlannedPublication]:
    """Publish (or reconcile) every unbound new node and record it. COMMITS `session`.

    Callers must have already verified that a human is accepting this exact revision and
    that nothing else blocks acceptance; this function re-checks repository permission
    and the issue cap itself so it cannot be misused to publish more.
    `before_claim` revalidates the accepting draft under its flow lock in the same
    transaction that claims each publication. No lock spans a provider call.
    """
    from .dispatch_pass import resolve_installation_id
    from .work_admission import resolve_repository_id

    plans, violations = await plan_publications(session, org_id=org_id, proposal=proposal, existing_addresses=existing_addresses, permitted=permitted)
    if violations:
        raise CycleBlockedError(violations[0].rule)
    if not plans:
        return []
    provider = publisher or AmendmentIssuePublisher()
    nodes = {node.address: node for node in proposal.nodes}
    verified: dict[tuple[str, int], int] = {}
    parents: dict[PlanningIssueReference, SimpleNamespace] = {}
    published: list[PlannedPublication] = []
    for plan in plans:
        node = nodes[plan.address]
        parent = native_parent(proposal, node)
        if parent is not None and parent not in parents:
            from .repository_binding import RepositoryBindingError, resolve_story_repository

            try:
                resolved = await resolve_story_repository(
                    session,
                    tenant_id=org_id,
                    name=parent.repository,
                    issue=parent.issue_number,
                    repository_id=parent.repository_id,
                    installation_id=parent.installation_id,
                    operation="preview",
                )
            except RepositoryBindingError as error:
                raise CycleBlockedError("amendment_issue_parent_unavailable") from error
            if type(resolved.issue_id) is not int or resolved.issue_id <= 0:
                raise CycleBlockedError("amendment_issue_parent_unverifiable")
            parents[parent] = SimpleNamespace(
                org_id=org_id,
                repo=resolved.full_name,
                provider_repository_id=resolved.repository_id,
                installation_id=resolved.installation_id,
                issue_number=parent.issue_number,
                issue_id=resolved.issue_id,
            )
        if proposal.product_id is not None:
            connection = node.repository_connection
            if connection is None or type(node.provider_repository_id) is not int or node.provider_repository_id <= 0:
                raise CycleBlockedError("amendment_issue_binding_incomplete")
            installation_id = connection.installation_id
            owner, state = await resolve_installation_owner(installation_id, db=session)
            if state is not OwnerState.RESOLVED or owner is None or owner.tenant_id != org_id:
                raise CycleBlockedError("amendment_issue_connection_unavailable")
        else:
            installation_id = await resolve_installation_id(session, org_id=org_id)
            if installation_id is None:
                raise CycleBlockedError("amendment_issue_installation_unavailable")
        key = (plan.repository, installation_id)
        if key not in verified:
            repository_id = await resolve_repository_id(org_id=org_id, installation_id=installation_id, repo=plan.repository)
            if type(repository_id) is not int or repository_id <= 0:
                raise CycleBlockedError("amendment_issue_binding_incomplete")
            verified[key] = repository_id
        if node.provider_repository_id is not None and verified[key] != node.provider_repository_id:
            raise CycleBlockedError("amendment_issue_repository_changed")
        binding = SimpleNamespace(org_id=org_id, repo=plan.repository, provider_repository_id=verified[key], installation_id=installation_id)
        parent_binding = parents.get(parent)
        issue = await _publish_one(
            session,
            provider,
            binding,
            node,
            before_claim=before_claim,
            parent_repository_id=parent.repository_id if parent else None,
            parent_issue_id=parent_binding.issue_id if parent_binding else None,
        )
        if parent_binding is not None:
            if before_claim is not None:
                await before_claim()
                await session.commit()
            await provider.confirm_native_parent(binding, parent_binding, issue)
            row = await _binding_row(
                session,
                org_id=org_id,
                repository=plan.repository,
                proposal_identity=plan.proposal_identity,
                provider_repository_id=binding.provider_repository_id,
            )
            confirmed = await session.execute(
                update(OrchestrationAmendmentIssueBinding)
                .where(
                    OrchestrationAmendmentIssueBinding.id == row.id,
                    OrchestrationAmendmentIssueBinding.state == AmendmentIssueBindingState.CREATED.value,
                    OrchestrationAmendmentIssueBinding.issue_number == issue.number,
                    OrchestrationAmendmentIssueBinding.parent_repository_id == parent.repository_id,
                    OrchestrationAmendmentIssueBinding.parent_issue_id == parent_binding.issue_id,
                )
                .values(link_confirmed_at=_now())
            )
            if confirmed.rowcount != 1:
                await session.rollback()
                raise CycleBlockedError("amendment_issue_link_conflict")
            await session.commit()
        published.append(
            replace(plan, action="reuse", issue_number=issue.number, provider_repository_id=binding.provider_repository_id, url=issue.url)
        )
    return published
