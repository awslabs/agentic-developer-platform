"""Read-only GitHub merge evidence and eligibility (#5148).

Legacy completion accepts the configured App's commit-specific reviewer verdict.
The opt-in adapter observes current repository requirements and consumes a freshly
resolved A1 context; neither a worker exit nor an old approval authorizes a merge.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import quote

import httpx

from .execution_policy import Action, AuthorizationContext, ResourceRef, authorize_action
from .execution_state import TERMINAL_EXECUTION_STATUSES, ExecutionIdentity, OutcomeKind
from .pr_bindings import BindingError, MergeEvidence, active_binding_for_node, binding_scope_matches, completion_candidate
from .review_evidence import ReviewEvidence


async def _app_review_opinion(
    client: httpx.AsyncClient, *, token: str, repo: str, pr_number: int, app_id: int, head: str
) -> tuple[bool | None, str | None]:
    """Latest Codex verdict published by the tenant's App, including a shared author.

    GitHub disallows formal self-approval. Authenticate the existing reviewer
    comment using provider metadata, not its display name or body alone. None
    means no App verdict; False means its latest verdict cannot approve this head.
    Read all pages within a fixed bound so an unseen withdrawal cannot pass.
    """
    latest = None
    latest_state = None
    latest_key = ("", 0)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    for page in range(1, 11):
        response = await client.get(
            f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments",
            headers=headers,
            params={"per_page": 100, "page": page},
        )
        response.raise_for_status()
        comments = response.json()
        if not isinstance(comments, list):
            raise RuntimeError("GitHub returned incomplete reviewer comment evidence")
        for comment in comments:
            if str((comment.get("performed_via_github_app") or {}).get("id")) != str(app_id) or (comment.get("user") or {}).get("type") != "Bot":
                continue
            body = (comment.get("body") or "").replace("\r\n", "\n")
            if not body.startswith("## agent-codex-reviewer — "):
                continue
            # Issue-readiness verdicts are unrelated to PR review.
            if body.split("\n", 1)[0] in {"## agent-codex-reviewer — ISSUE READY", "## agent-codex-reviewer — ISSUE CHANGES REQUESTED"}:
                continue
            key = (comment.get("updated_at") or comment.get("created_at") or "", comment.get("id") or 0)
            if not key[0] or not key[1]:
                raise RuntimeError("GitHub returned undated reviewer comment evidence")
            if key <= latest_key:
                continue
            latest_key = key
            match = re.match(
                r"\A## agent-codex-reviewer — (APPROVE|FIXES PUSHED AND APPROVED)\n\n"
                r"\*\*Reviewed head:\*\* `([0-9a-f]{40})`\n\*\*Blockers:\*\* 0\n\*\*Engine:\*\* [^\n]+\n",
                body,
            )
            latest = bool(match and match[2] == head)
            latest_state = (
                "approved"
                if latest
                else "stale"
                if match
                else "changes_requested"
                if body.startswith("## agent-codex-reviewer — REQUEST CHANGES\n")
                else "unverified"
            )
        if "next" not in response.links:
            return latest, latest_state
    raise RuntimeError("GitHub reviewer comment pagination exceeded the evidence bound")


class GitHubEvidenceSource:
    async def merge_eligibility(
        self,
        *,
        session,
        identity: ExecutionIdentity,
        review: ReviewEvidence,
        authorization_reader: AuthorizationReader,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> MergeEligibility:
        return await observe_merge_eligibility(
            session=session, identity=identity, review=review, authorization_reader=authorization_reader, client=client, clock=clock
        )

    async def bound_pull_request(
        self,
        *,
        org_id: str,
        installation_id: int,
        repo: str,
        pr_number: int,
        read_token: str | None = None,
        client: httpx.AsyncClient | None = None,
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
        * each reviewer's latest opinion, matched to the current head. A formal
          approval or a verified Codex verdict from the configured App suffices;
          the App may also be the PR author. Withdrawn approvals and unmet GitHub
          review requirements still hold completion.
        """
        from src.admin.connections.github_client import GitHubAppClient
        from src.knowledge.github_app_service import resolve_tenant_app_credentials

        owner, name = repo.split("/", 1)
        app_id, private_key = await resolve_tenant_app_credentials(org_id)
        app = GitHubAppClient(app_id, private_key)
        try:
            token = read_token if read_token is not None else await app.get_installation_token(installation_id)
            query = """query($owner:String!,$name:String!,$pr:Int!) {
              repository(owner:$owner,name:$name) { databaseId pullRequest(number:$pr) {
                id url merged mergedAt headRefOid reviewDecision isDraft mergeable mergeStateStatus mergeCommit { oid }
                author { login }
                commits(last:1) { nodes { commit { statusCheckRollup { state } } } }
                reviews(last:100) { pageInfo { hasPreviousPage } nodes { state submittedAt commit { oid } author { login } } }
              } }
            }"""
            async with AsyncExitStack() as stack:
                client = client or await stack.enter_async_context(httpx.AsyncClient(timeout=15))
                response = await client.post(
                    "https://api.github.com/graphql",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"query": query, "variables": {"owner": owner, "name": name, "pr": pr_number}},
                )
                response.raise_for_status()
                payload = response.json()
                record = (payload.get("data", {}).get("repository") or {}).get("pullRequest") or {}
                app_approval = None
                app_review_state = None
                if record and not payload.get("errors"):
                    app_approval, app_review_state = await _app_review_opinion(
                        client, token=token, repo=repo, pr_number=pr_number, app_id=app_id, head=record.get("headRefOid") or ""
                    )
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
            review_approved = (
                not (reviews.get("pageInfo") or {}).get("hasPreviousPage", False)
                and (app_approval is True or "APPROVED" in latest.values())
                and app_approval is not False
                and "CHANGES_REQUESTED" not in latest.values()
                and record.get("reviewDecision") not in {"CHANGES_REQUESTED", "REVIEW_REQUIRED"}
            )
            if review_approved:
                review_state = "approved"
            elif (
                record.get("reviewDecision") == "CHANGES_REQUESTED"
                or "CHANGES_REQUESTED" in latest.values()
                or app_review_state == "changes_requested"
            ):
                review_state = "changes_requested"
            elif (reviews.get("pageInfo") or {}).get("hasPreviousPage", False):
                review_state = "unverified"
            elif record.get("reviewDecision") == "REVIEW_REQUIRED":
                review_state = "required"
            elif app_review_state in {"stale", "unverified"}:
                review_state = app_review_state
            elif "STALE" in latest.values():
                review_state = "stale"
            else:
                review_state = "missing"
            return MergeEvidence(
                merged=bool(record.get("merged")),
                head_sha=head,
                checks_successful=rollup.get("state") == "SUCCESS",
                review_approved=review_approved,
                merge_commit_sha=(record.get("mergeCommit") or {}).get("oid"),
                merged_at=record.get("mergedAt"),
                url=record.get("url"),
                provider_repository_id=(payload.get("data", {}).get("repository") or {}).get("databaseId"),
                provider_pr_node_id=record.get("id"),
                checks_state=rollup.get("state") or "MISSING",
                review_state=review_state,
                draft=bool(record.get("isDraft")),
                mergeable=record.get("mergeable"),
                merge_state=record.get("mergeStateStatus"),
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


class EligibilityState(StrEnum):
    ELIGIBLE = "eligible"
    WAITING = "waiting"
    BLOCKED = "blocked"


class EligibilityReason(StrEnum):
    AUTHORITY_UNAVAILABLE = "authority_unavailable"
    AUTHORITY_DENIED = "authority_denied"
    SCOPE_CHANGED = "scope_changed"
    REVIEW_INCOMPLETE = "review_incomplete"
    HEAD_CHANGED = "head_changed"
    BASE_CHANGED = "base_changed"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INCOMPLETE_OBSERVATION = "incomplete_observation"
    RULES_UNAVAILABLE = "rules_unavailable"
    UNSUPPORTED_RULE = "unsupported_rule"
    PR_CLOSED = "pr_closed"
    DRAFT = "draft"
    CONFLICT = "conflict"
    MERGEABILITY_UNKNOWN = "mergeability_unknown"
    PROVIDER_BLOCKED = "provider_blocked"
    REVIEW_REQUIRED = "review_required"
    CHANGES_REQUESTED = "changes_requested"
    REQUIRED_CHECK_MISSING = "required_check_missing"
    REQUIRED_CHECK_PENDING = "required_check_pending"
    REQUIRED_CHECK_FAILED = "required_check_failed"
    BASE_OUTDATED = "base_outdated"


class EvidenceUnavailableError(Exception):
    def __init__(self, reason: EligibilityReason):
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class CheckRequirement:
    name: str
    app_id: int | None = None


@dataclass(frozen=True)
class CheckEvidence:
    name: str
    app_id: int | None
    state: str
    provider_id: int
    source: str


@dataclass(frozen=True)
class ReviewOpinion:
    actor_id: int
    state: str
    head_sha: str
    provider_id: int
    submitted_at: datetime


@dataclass(frozen=True)
class RepositoryRequirements:
    checks: tuple[CheckRequirement, ...]
    required_approvals: int
    code_owner_review: bool
    last_push_review: bool
    strict_checks: bool
    queue_required: bool
    # Once both effective branch rules AND legacy protection were observed,
    # their explicit required-context list determines which other checks are optional.
    complete: bool = True
    checks_declared: bool = False
    allowed_merge_methods: tuple[str, ...] = ("merge", "rebase", "squash")


@dataclass(frozen=True)
class SourceEvidence:
    kind: str
    url: str
    observed_at: datetime
    payload_sha256: str


@dataclass(frozen=True)
class PullRequestObservation:
    repository_id: int
    repo: str
    pr_number: int
    pr_node_id: str
    head_sha: str
    base_sha: str
    base_ref: str
    author_id: int
    open: bool
    merged: bool
    merge_commit_sha: str | None
    draft: bool
    mergeable: bool | None
    mergeable_state: str
    review_decision: str | None
    requirements: RepositoryRequirements
    checks: tuple[CheckEvidence, ...]
    reviews: tuple[ReviewOpinion, ...]
    observed_at: datetime
    sources: tuple[SourceEvidence, ...]


@dataclass(frozen=True)
class MergeEligibility:
    state: EligibilityState
    reasons: tuple[EligibilityReason, ...]
    observed_at: datetime
    observation: PullRequestObservation | None = None
    authority_reason: str | None = None

    @property
    def eligible(self) -> bool:
        return self.state is EligibilityState.ELIGIBLE

    def ledger_detail(self) -> dict[str, str]:
        return {"merge_eligibility": json.dumps(self.summary(), separators=(",", ":"))}

    def summary(self) -> dict[str, Any]:
        """Bounded ledger/presentation data, never credentials or provider payloads."""
        value: dict[str, Any] = {
            "state": self.state.value,
            "reasons": [r.value for r in self.reasons[:12]],
            "observed_at": self.observed_at.isoformat(),
        }
        if self.observation:
            value.update(
                head_sha=self.observation.head_sha,
                base_sha=self.observation.base_sha,
                queue_required=self.observation.requirements.queue_required,
                required_checks=len(self.observation.requirements.checks),
                required_approvals=self.observation.requirements.required_approvals,
            )
        return value


_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
MAX_PAGES = 10
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
READ_PERMISSIONS = {"contents": "read", "pull_requests": "read", "checks": "read", "statuses": "read", "administration": "read", "metadata": "read"}


def _refuse(reason=EligibilityReason.INCOMPLETE_OBSERVATION):
    raise EvidenceUnavailableError(reason)


def _integer(value, *, minimum=0):
    if type(value) is not int or value < minimum:
        _refuse()
    return value


def _boolean(value):
    if type(value) is not bool:
        _refuse()
    return value


def _text(value, *, limit=255):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _refuse()
    return value


def _sha(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        _refuse()
    return value


def _object(value):
    if not isinstance(value, dict):
        _refuse()
    return value


def _list(value):
    if not isinstance(value, list):
        _refuse()
    return value


def _required_check(raw, *, legacy=False):
    raw = _object(raw)
    name = _text(raw.get("context"))
    app_id = raw.get("app_id" if legacy else "integration_id")
    if app_id not in (None, -1):
        app_id = _integer(app_id, minimum=1)
    else:
        app_id = None
    return CheckRequirement(name, app_id)


def parse_requirements(rules: Any, protection: Any) -> RepositoryRequirements:
    """Unknown requirements deny; only positively observed policy makes checks optional."""
    required: set[CheckRequirement] = set()
    approvals = 0  # The engine validates reviewer runs; GitHub identities follow repository policy.
    codeowners = last_push = strict = queue = checks_declared = False
    methods = {"merge", "rebase", "squash"}
    for rule in _list(rules):
        kind = _object(rule).get("type")
        params = _object(rule.get("parameters", {}))
        allowed_parameters = {
            "required_status_checks": {"required_status_checks", "strict_required_status_checks_policy", "do_not_enforce_on_create"},
            "pull_request": {
                "required_approving_review_count",
                "require_code_owner_review",
                "require_last_push_approval",
                "dismiss_stale_reviews_on_push",
                "required_review_thread_resolution",
                "allowed_merge_methods",
            },
            "merge_queue": {
                "check_response_timeout_minutes",
                "grouping_strategy",
                "max_entries_to_build",
                "max_entries_to_merge",
                "merge_method",
                "min_entries_to_merge",
                "min_entries_to_merge_wait_minutes",
            },
            "deletion": set(),
            "non_fast_forward": set(),
        }
        if kind not in allowed_parameters or set(params) - allowed_parameters[kind]:
            _refuse(EligibilityReason.UNSUPPORTED_RULE)
        if kind == "required_status_checks":
            checks_declared = True
            required.update(_required_check(c) for c in _list(params.get("required_status_checks")))
            strict |= _boolean(params.get("strict_required_status_checks_policy"))
        elif kind == "pull_request":
            approvals = max(approvals, _integer(params.get("required_approving_review_count")))
            codeowners |= _boolean(params.get("require_code_owner_review"))
            last_push |= _boolean(params.get("require_last_push_approval"))
            _boolean(params.get("dismiss_stale_reviews_on_push"))
            _boolean(params.get("required_review_thread_resolution"))
            if "allowed_merge_methods" in params:
                allowed = {_text(m) for m in _list(params["allowed_merge_methods"])}
                if not allowed or not allowed <= {"merge", "rebase", "squash"}:
                    _refuse(EligibilityReason.UNSUPPORTED_RULE)
                methods &= allowed
            # Thread resolution is independently reflected in GitHub's mergeable_state.
        elif kind == "merge_queue":
            queue = True
            if "merge_method" in params:
                method = _text(params["merge_method"]).lower()
                if method not in {"merge", "rebase", "squash"}:
                    _refuse(EligibilityReason.UNSUPPORTED_RULE)
                methods &= {method}
        elif kind not in {"deletion", "non_fast_forward"}:
            _refuse(EligibilityReason.UNSUPPORTED_RULE)
    if protection is not None:
        protection = _object(protection)
        known = {
            "url",
            "required_status_checks",
            "required_pull_request_reviews",
            "restrictions",
            "enforce_admins",
            "required_linear_history",
            "allow_force_pushes",
            "allow_deletions",
            "block_creations",
            "required_conversation_resolution",
            "lock_branch",
            "allow_fork_syncing",
            "required_signatures",
        }
        if set(protection) - known or protection.get("restrictions") is not None:
            _refuse(EligibilityReason.UNSUPPORTED_RULE)
        checks = protection.get("required_status_checks")
        if checks is not None:
            checks_declared = True
            checks = _object(checks)
            strict |= _boolean(checks.get("strict"))
            detailed = [_required_check(c, legacy=True) for c in _list(checks.get("checks"))]
            contexts = {_text(c) for c in _list(checks.get("contexts"))}
            if not {c.name for c in detailed} <= contexts:
                _refuse()
            required.update(detailed)
            required.update(CheckRequirement(name) for name in contexts - {c.name for c in detailed})
        reviews = protection.get("required_pull_request_reviews")
        if reviews is not None:
            reviews = _object(reviews)
            approvals = max(approvals, _integer(reviews.get("required_approving_review_count")))
            codeowners |= _boolean(reviews.get("require_code_owner_reviews"))
            last_push |= _boolean(reviews.get("require_last_push_approval"))
            _boolean(reviews.get("dismiss_stale_reviews"))
        for name in ("required_signatures", "required_linear_history", "lock_branch"):
            setting = protection.get(name)
            if setting is not None and _boolean(_object(setting).get("enabled")):
                _refuse(EligibilityReason.UNSUPPORTED_RULE)
    return RepositoryRequirements(
        tuple(sorted(required, key=lambda c: (c.name, c.app_id or 0))),
        approvals,
        codeowners,
        last_push,
        strict,
        queue,
        checks_declared=checks_declared,
        allowed_merge_methods=tuple(sorted(methods)),
    )


def evaluate_required_checks(observation: PullRequestObservation):
    """Canonical CI selection shared by merge and the retained reviewer."""
    req = observation.requirements
    blocked: list[EligibilityReason] = []
    waiting: list[EligibilityReason] = []
    selected: list[CheckEvidence] = []
    if not req.complete:
        blocked.append(EligibilityReason.RULES_UNAVAILABLE)
    checks_to_require = req.checks if req.checks_declared else tuple(CheckRequirement(c.name, c.app_id) for c in observation.checks)
    for check in checks_to_require:
        matches = [c for c in observation.checks if c.name == check.name and (check.app_id is None or c.app_id == check.app_id)]
        # GitHub requires both when a check-run and a legacy status share a context.
        if matches and check.app_id is not None:
            matches += [c for c in observation.checks if c.name == check.name and c.source == "status"]
        selected.extend(c for c in matches if c not in selected)
        if not matches:
            blocked.append(EligibilityReason.REQUIRED_CHECK_MISSING)
        elif any(
            c.state not in ({"success", "skipped", "neutral", "pending"} if c.source == "check_run" else {"success", "pending"}) for c in matches
        ):
            blocked.append(EligibilityReason.REQUIRED_CHECK_FAILED)
        elif any(c.state == "pending" for c in matches):
            waiting.append(EligibilityReason.REQUIRED_CHECK_PENDING)
    return tuple(dict.fromkeys(blocked + waiting)), tuple(selected)


def evaluate_observation(observation: PullRequestObservation) -> MergeEligibility:
    """Repository eligibility only; the public adapter also checks R1 and current A1."""
    blocked: list[EligibilityReason] = []
    waiting: list[EligibilityReason] = []
    req = observation.requirements
    if not req.complete:
        blocked.append(EligibilityReason.RULES_UNAVAILABLE)
    if not observation.open or observation.merged:
        blocked.append(EligibilityReason.PR_CLOSED)
    if observation.draft:
        blocked.append(EligibilityReason.DRAFT)
    if observation.mergeable is False:
        blocked.append(EligibilityReason.CONFLICT)
    elif observation.mergeable is None:
        waiting.append(EligibilityReason.MERGEABILITY_UNKNOWN)
    if req.strict_checks and observation.mergeable_state == "behind":
        blocked.append(EligibilityReason.BASE_OUTDATED)
    latest: dict[int, ReviewOpinion] = {}
    for opinion in sorted(observation.reviews, key=lambda r: (r.submitted_at, r.provider_id)):
        if opinion.state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[opinion.actor_id] = opinion
    independent = [r for actor, r in latest.items() if actor != observation.author_id]
    if any(r.state == "CHANGES_REQUESTED" for r in independent) or observation.review_decision == "CHANGES_REQUESTED":
        blocked.append(EligibilityReason.CHANGES_REQUESTED)
    approvals = sum(r.state == "APPROVED" and r.head_sha == observation.head_sha for r in independent)
    if approvals < req.required_approvals or observation.review_decision == "REVIEW_REQUIRED":
        blocked.append(EligibilityReason.REVIEW_REQUIRED)
    if (req.code_owner_review or req.last_push_review) and observation.review_decision != "APPROVED":
        blocked.append(EligibilityReason.REVIEW_REQUIRED)
    # An empty, complete provider response is valid for paths with no applicable
    # CI and no required checks. Only a named requirement can be missing; inventing
    # one here strands reviewed documentation/tooling changes indefinitely.
    if not req.allowed_merge_methods:
        blocked.append(EligibilityReason.UNSUPPORTED_RULE)
    check_reasons, _ = evaluate_required_checks(observation)
    blocked.extend(r for r in check_reasons if r is not EligibilityReason.REQUIRED_CHECK_PENDING)
    waiting.extend(r for r in check_reasons if r is EligibilityReason.REQUIRED_CHECK_PENDING)
    if observation.mergeable_state == "blocked" and not blocked and not waiting:
        blocked.append(EligibilityReason.PROVIDER_BLOCKED)
    if observation.mergeable_state not in {"clean", "unstable", "has_hooks", "behind", "blocked", "dirty", "unknown"}:
        blocked.append(EligibilityReason.INCOMPLETE_OBSERVATION)
    if observation.mergeable_state in {"dirty", "unknown"} and not blocked:
        waiting.append(EligibilityReason.MERGEABILITY_UNKNOWN)
    reasons = tuple(dict.fromkeys(blocked + waiting))
    state = EligibilityState.BLOCKED if blocked else EligibilityState.WAITING if waiting else EligibilityState.ELIGIBLE
    return MergeEligibility(state, reasons, observation.observed_at, observation)


class GitHubMergeObserver:
    """Bounded read-only provider client. Its token covers one repository only."""

    def __init__(self, client: httpx.AsyncClient, token: str, clock: Callable[[], datetime]):
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        self.clock = clock
        self.sources: list[SourceEvidence] = []
        self.has_next_page = False
        self.rules_capability_unavailable = False

    def _decode(self, response, *, kind: str, url: str, rules=False):
        if response.status_code != 200:
            if rules and response.status_code == 403 and len(response.content) <= MAX_RESPONSE_BYTES:
                try:
                    message = response.json().get("message")
                except (ValueError, AttributeError):
                    message = None
                if message in {
                    "Upgrade to GitHub Pro or make this repository public to enable this feature.",
                    "Upgrade to GitHub Team or make this repository public to enable this feature.",
                }:
                    self.rules_capability_unavailable = True
                    self.sources.append(SourceEvidence(kind, url, self.clock(), hashlib.sha256(response.content).hexdigest()))
            _refuse(EligibilityReason.RULES_UNAVAILABLE if rules else EligibilityReason.PROVIDER_UNAVAILABLE)
        if len(response.content) > MAX_RESPONSE_BYTES:
            _refuse()
        try:
            body = response.json()
        except ValueError:
            _refuse()
        self.sources.append(SourceEvidence(kind, url, self.clock(), hashlib.sha256(response.content).hexdigest()))
        return body

    async def get(self, path, *, kind, params=None, rules=False, absent=False):
        url = "https://api.github.com" + path
        response = await self.client.get(url, headers=self.headers, params=params, follow_redirects=False)
        self.has_next_page = "next" in response.links
        if absent and response.status_code == 404:
            self.sources.append(SourceEvidence(kind, url, self.clock(), hashlib.sha256(response.content).hexdigest()))
            return None
        return self._decode(response, kind=kind, url=url, rules=rules)

    async def pages(self, path, *, kind, key=None, rules=False, params=None):
        items = []
        expected = None
        for page in range(1, MAX_PAGES + 1):
            body = await self.get(path, kind=kind, params={**(params or {}), "per_page": 100, "page": page}, rules=rules)
            if key:
                body = _object(body)
                total = _integer(body.get("total_count"))
                if expected is not None and expected != total:
                    _refuse()
                expected = total
                values = _list(body.get(key))
            else:
                values = _list(body)
            items.extend(values)
            if self.has_next_page and not values:
                _refuse()
            if len(values) < 100 and not self.has_next_page:
                if expected is not None and len(items) != expected:
                    _refuse()
                return items
        _refuse()

    async def verify_no_configured_rules(self, binding, base_ref, base_sha):
        """Positive alternate evidence when REST rules require a paid plan."""
        owner, name = binding.repo.split("/", 1)
        query = """query($owner:String!,$name:String!,$base:String!) {
          repository(owner:$owner,name:$name) { databaseId
            rulesets(first:100,includeParents:true) { totalCount nodes { id } pageInfo { hasNextPage } }
            ref(qualifiedName:$base) { name target { oid } branchProtectionRule { id } }
          }
        }"""
        response = await self.client.post(
            "https://api.github.com/graphql",
            headers=self.headers,
            follow_redirects=False,
            json={"query": query, "variables": {"owner": owner, "name": name, "base": "refs/heads/" + base_ref}},
        )
        try:
            payload = _object(self._decode(response, kind="rules_capability_verification", url="https://api.github.com/graphql", rules=True))
            if payload.get("errors"):
                _refuse()
            repository = _object(_object(payload.get("data")).get("repository"))
            rulesets = _object(repository.get("rulesets"))
            ref = _object(repository.get("ref"))
            if (
                repository.get("databaseId") != binding.provider_repository_id
                or ref.get("name") != base_ref
                or _sha(_object(ref.get("target")).get("oid")) != base_sha
                or "branchProtectionRule" not in ref
                or ref["branchProtectionRule"] is not None
                or _integer(rulesets.get("totalCount")) != 0
                or _list(rulesets.get("nodes")) != []
                or _boolean(_object(rulesets.get("pageInfo")).get("hasNextPage"))
            ):
                _refuse()
        except EvidenceUnavailableError:
            _refuse(EligibilityReason.RULES_UNAVAILABLE)

    async def observe(self, binding) -> PullRequestObservation:
        repo = binding.repo
        if not isinstance(repo, str) or not _REPO.fullmatch(repo) or any(p in {".", ".."} for p in repo.split("/")):
            _refuse()
        root = f"/repos/{repo}"
        path = f"{root}/pulls/{binding.pr_number}"
        pr = _object(await self.get(path, kind="pull_request"))
        head, pull_base, base_ref, repository_id, pr_node_id = self._identity(pr, binding)
        branch = _object(await self.get(f"{root}/branches/{quote(base_ref, safe='')}", kind="base_branch", rules=True))
        protected = _boolean(branch.get("protected"))
        # GitHub keeps the PR's base.sha/baseRefOid as a saved PR snapshot.
        # The branch and GraphQL baseRef.target identify the current merge target.
        base = _sha(_object(branch.get("commit")).get("sha"))
        rules = None
        try:
            rules = await self.pages(f"{root}/rules/branches/{quote(base_ref, safe='')}", kind="branch_rules", rules=True)
            protection = await self.get(
                f"{root}/branches/{quote(base_ref, safe='')}/protection", kind="branch_protection", rules=True, absent=not protected
            )
        except EvidenceUnavailableError:
            if protected or not self.rules_capability_unavailable or rules not in (None, []):
                raise
            await self.verify_no_configured_rules(binding, base_ref, base)
            rules, protection = [], None
        requirements = parse_requirements(rules, protection)
        repository_settings = _object(await self.get(root, kind="repository_settings"))
        if repository_settings.get("id") != repository_id or _text(repository_settings.get("full_name")).lower() != repo.lower():
            _refuse(EligibilityReason.SCOPE_CHANGED)
        check_runs = await self.pages(f"{root}/commits/{head}/check-runs", kind="check_runs", key="check_runs", params={"filter": "latest"})
        statuses = await self.pages(f"{root}/commits/{head}/statuses", kind="statuses")
        reviews = await self.pages(f"{path}/reviews", kind="reviews")
        checks = self._checks(check_runs, statuses, head)
        opinions = self._reviews(reviews)
        owner, name = repo.split("/", 1)
        query = """query($owner:String!,$name:String!,$pr:Int!) {
          repository(owner:$owner,name:$name) { databaseId mergeCommitAllowed squashMergeAllowed rebaseMergeAllowed pullRequest(number:$pr) {
            id headRefOid baseRefOid baseRef { name target { oid } } reviewDecision
          } }
        }"""
        response = await self.client.post(
            "https://api.github.com/graphql",
            headers=self.headers,
            follow_redirects=False,
            json={"query": query, "variables": {"owner": owner, "name": name, "pr": binding.pr_number}},
        )
        payload = _object(self._decode(response, kind="review_requirements", url="https://api.github.com/graphql"))
        if payload.get("errors"):
            _refuse()
        repository = _object(_object(payload.get("data")).get("repository"))
        record = _object(repository.get("pullRequest"))
        if repository.get("databaseId") != repository_id or record.get("id") != pr_node_id:
            _refuse(EligibilityReason.SCOPE_CHANGED)
        # REST omits allow_* settings for read-only installation tokens. GraphQL
        # explicitly exposes the same repository settings without write access.
        # Cross-check REST when present; absence never implies permission to merge.
        permitted_methods = set()
        for method, field, rest_field in (
            ("merge", "mergeCommitAllowed", "allow_merge_commit"),
            ("squash", "squashMergeAllowed", "allow_squash_merge"),
            ("rebase", "rebaseMergeAllowed", "allow_rebase_merge"),
        ):
            allowed = _boolean(repository.get(field))
            if rest_field in repository_settings and _boolean(repository_settings[rest_field]) != allowed:
                _refuse()
            if allowed:
                permitted_methods.add(method)
        requirements = replace(requirements, allowed_merge_methods=tuple(sorted(set(requirements.allowed_merge_methods) & permitted_methods)))
        if record.get("headRefOid") != head:
            _refuse(EligibilityReason.HEAD_CHANGED)
        live_base = _object(record.get("baseRef"))
        if record.get("baseRefOid") != pull_base or live_base.get("name") != base_ref or _sha(_object(live_base.get("target")).get("oid")) != base:
            _refuse(EligibilityReason.BASE_CHANGED)
        if "reviewDecision" not in record or record["reviewDecision"] not in {None, "APPROVED", "REVIEW_REQUIRED", "CHANGES_REQUESTED"}:
            _refuse()
        after = _object(await self.get(path, kind="pull_request_recheck"))
        after_head, after_base, after_ref, _, _ = self._identity(after, binding)
        if after_head != head:
            _refuse(EligibilityReason.HEAD_CHANGED)
        if after_base != pull_base or after_ref != base_ref:
            _refuse(EligibilityReason.BASE_CHANGED)
        branch_after = _object(await self.get(f"{root}/branches/{quote(base_ref, safe='')}", kind="base_branch_recheck", rules=True))
        if _sha(_object(branch_after.get("commit")).get("sha")) != base or _boolean(branch_after.get("protected")) != protected:
            _refuse(EligibilityReason.BASE_CHANGED)
        if after.get("state") not in {"open", "closed"}:
            _refuse()
        mergeable = after.get("mergeable")
        if "mergeable" not in after or mergeable is not None and type(mergeable) is not bool:
            _refuse()
        merged = _boolean(after.get("merged"))
        merge_sha = _sha(after.get("merge_commit_sha")) if merged else None
        return PullRequestObservation(
            repository_id=repository_id,
            repo=repo,
            pr_number=binding.pr_number,
            pr_node_id=pr_node_id,
            head_sha=head,
            base_sha=base,
            base_ref=base_ref,
            author_id=_integer(_object(after.get("user")).get("id"), minimum=1),
            open=after["state"] == "open",
            merged=merged,
            merge_commit_sha=merge_sha,
            draft=_boolean(after.get("draft")),
            mergeable=mergeable,
            mergeable_state=_text(after.get("mergeable_state")),
            review_decision=record["reviewDecision"],
            requirements=requirements,
            checks=checks,
            reviews=opinions,
            observed_at=self.clock(),
            sources=tuple(self.sources),
        )

    @staticmethod
    def _identity(pr, binding):
        base = _object(pr.get("base"))
        repository = _object(base.get("repo"))
        repository_id = _integer(repository.get("id"), minimum=1)
        node_id = _text(pr.get("node_id"))
        if (
            repository_id != binding.provider_repository_id
            or node_id != binding.provider_pr_node_id
            or _integer(pr.get("number"), minimum=1) != binding.pr_number
            or _text(repository.get("full_name")).lower() != binding.repo.lower()
        ):
            _refuse(EligibilityReason.SCOPE_CHANGED)
        return _sha(_object(pr.get("head")).get("sha")), _sha(base.get("sha")), _text(base.get("ref")), repository_id, node_id

    @staticmethod
    def _checks(check_runs, statuses, head):
        latest: dict[tuple[str, int | None, str], CheckEvidence] = {}
        seen_checks = set()
        seen_statuses = set()
        for raw in check_runs:
            raw = _object(raw)
            if _sha(raw.get("head_sha")) != head:
                _refuse(EligibilityReason.HEAD_CHANGED)
            name = _text(raw.get("name"))
            app_id = _integer(_object(raw.get("app")).get("id"), minimum=1)
            provider_id = _integer(raw.get("id"), minimum=1)
            if provider_id in seen_checks:
                _refuse()
            seen_checks.add(provider_id)
            status = raw.get("status")
            if status not in {"queued", "in_progress", "completed", "waiting", "pending", "requested"}:
                _refuse()
            state = _text(raw.get("conclusion")) if status == "completed" else "pending"
            item = CheckEvidence(name, app_id, state, provider_id, "check_run")
            key = (name, app_id, "check_run")
            if key not in latest or latest[key].provider_id < provider_id:
                latest[key] = item
        for raw in statuses:
            raw = _object(raw)
            name = _text(raw.get("context"))
            state = _text(raw.get("state"))
            if state not in {"pending", "success", "failure", "error"}:
                _refuse()
            provider_id = _integer(raw.get("id"), minimum=1)
            if provider_id in seen_statuses:
                _refuse()
            seen_statuses.add(provider_id)
            key = (name, None, "status")
            if key not in latest or latest[key].provider_id < provider_id:
                latest[key] = CheckEvidence(name, None, state, provider_id, "status")
        return tuple(latest.values())

    @staticmethod
    def _reviews(reviews):
        result = []
        seen = set()
        for raw in reviews:
            raw = _object(raw)
            provider_id = _integer(raw.get("id"), minimum=1)
            if provider_id in seen:
                _refuse()
            seen.add(provider_id)
            state = _text(raw.get("state"))
            if state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED", "COMMENTED", "PENDING"}:
                _refuse()
            if state in {"COMMENTED", "PENDING"}:
                continue
            submitted_at = datetime.fromisoformat(_text(raw.get("submitted_at")).replace("Z", "+00:00"))
            if submitted_at.tzinfo is None:
                _refuse()
            result.append(
                ReviewOpinion(_integer(_object(raw.get("user")).get("id"), minimum=1), state, _sha(raw.get("commit_id")), provider_id, submitted_at)
            )
        return tuple(result)


AuthorizationReader = Callable[[], Awaitable[AuthorizationContext | None]]


async def observe_merge_eligibility(
    *,
    session,
    identity: ExecutionIdentity,
    review: ReviewEvidence,
    authorization_reader: AuthorizationReader,
    client: httpx.AsyncClient | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> MergeEligibility:
    """Public M1 seam for the M2 handler, with no persistence or provider writes.

    The internal caller supplies R1-validated evidence and a resolver for CURRENT
    A1 facts, including actual grant revocation and credential scope. A pre-dispatch
    context with assumed grant_revoked=False is not a runtime resolver. Resolve
    again after I/O; no old decision or missing context can admit a merge.
    """
    from sqlalchemy import select

    from src.knowledge.github_app_service import mint_installation_token_with_expiry, resolve_tenant_app_credentials

    from .dispatch import graph_address
    from .dispatch_pass import resolve_installation_id
    from .execution_store import load_execution
    from .models import OrchestrationFlow, OrchestrationNode
    from .policy_admission import load_in_force_policy
    from .state import NodeState

    def blocked(reason, authority_reason=None):
        return MergeEligibility(EligibilityState.BLOCKED, (reason,), clock(), authority_reason=authority_reason)

    async def protected_state():
        loaded = await load_execution(session, identity=identity)
        if loaded is None or loaded.kind is not OutcomeKind.APPLIED or loaded.record is None:
            _refuse(EligibilityReason.SCOPE_CHANGED)
        execution = loaded.record
        if execution.status in TERMINAL_EXECUTION_STATUSES:
            _refuse(EligibilityReason.SCOPE_CHANGED)
        node = await session.scalar(
            select(OrchestrationNode)
            .where(OrchestrationNode.org_id == identity.org_id, OrchestrationNode.id == identity.node_id)
            .execution_options(populate_existing=True)
        )
        flow = await session.scalar(
            select(OrchestrationFlow)
            .where(OrchestrationFlow.org_id == identity.org_id, OrchestrationFlow.id == execution.flow_id)
            .execution_options(populate_existing=True)
        )
        if (
            node is None
            or flow is None
            or node.attempts != identity.cycle
            or node.state not in {NodeState.RUNNING.value, NodeState.AWAITING_MERGE.value}
        ):
            _refuse(EligibilityReason.SCOPE_CHANGED)
        binding = await active_binding_for_node(session, org_id=identity.org_id, node_id=identity.node_id, attempt=node.attempts)
        if completion_candidate(binding) is not None or not binding_scope_matches(binding, node):
            _refuse(EligibilityReason.SCOPE_CHANGED)
        inputs = await load_in_force_policy(session, org_id=identity.org_id, flow_id=execution.flow_id)
        if inputs.refusal or inputs.policy is None or inputs.plan_version != identity.accepted_plan_version:
            _refuse(EligibilityReason.AUTHORITY_UNAVAILABLE)
        return execution, node, flow, binding, inputs

    async def authorized(execution, node, flow, binding, inputs):
        try:
            context = await authorization_reader()
        except Exception:
            _refuse(EligibilityReason.AUTHORITY_UNAVAILABLE)
        now = clock()
        if context is None or context.now is None or context.now.tzinfo is None or not 0 <= (now - context.now).total_seconds() <= 30:
            _refuse(EligibilityReason.AUTHORITY_UNAVAILABLE)
        if context.policy != inputs.policy or context.accepted_plan_version != identity.accepted_plan_version:
            _refuse(EligibilityReason.AUTHORITY_UNAVAILABLE)
        return authorize_action(
            context,
            Action.MERGE,
            ResourceRef(repository_id=binding.repo, org_id=identity.org_id, node_address=graph_address(node, flow_slug=flow.slug)),
            identity.accepted_plan_version,
        )

    try:
        execution, node, flow, binding, inputs = await protected_state()
        result = review.result
        if (
            result.scope.org_id != identity.org_id
            or result.scope.node_id != identity.node_id
            or result.scope.flow_id != execution.flow_id
            or result.scope.execution_id != execution.id
            or result.scope.cycle != identity.cycle
            or result.authority.claim_id != identity.claim_id
            or result.authority.claim_generation != identity.claim_generation
            or result.authority.accepted_plan_version != identity.accepted_plan_version
            or result.repository.provider_repository_id != binding.provider_repository_id
            or result.subject.provider_pr_node_id != binding.provider_pr_node_id
            or review.repo != binding.repo
            or review.pr_number != binding.pr_number
        ):
            _refuse(EligibilityReason.SCOPE_CHANGED)
        if not review.is_complete_review or review.publication_blockers:
            _refuse(EligibilityReason.REVIEW_INCOMPLETE)
        if review.reviewed_head_sha != binding.head_sha:
            _refuse(EligibilityReason.HEAD_CHANGED)
        decision = await authorized(execution, node, flow, binding, inputs)
        if not decision.permitted:
            return blocked(EligibilityReason.AUTHORITY_DENIED, decision.reason.value)
        installation = await resolve_installation_id(session, org_id=identity.org_id)
        if not installation or binding.installation_id != installation:
            _refuse(EligibilityReason.SCOPE_CHANGED)
        try:
            app_id, key = await resolve_tenant_app_credentials(identity.org_id)
            token, expires_at = await mint_installation_token_with_expiry(
                app_id, key, installation, repositories=[binding.repo.split("/", 1)[1]], permissions=dict(READ_PERMISSIONS)
            )
        except Exception:
            _refuse(EligibilityReason.AUTHORITY_UNAVAILABLE)
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expires.tzinfo is None or expires <= clock():
            _refuse(EligibilityReason.AUTHORITY_UNAVAILABLE)
        snapshot = (binding.id, binding.revision, binding.head_sha, binding.repo, binding.provider_repository_id, binding.provider_pr_node_id)
        if client is None:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as own_client:
                observation = await asyncio.wait_for(GitHubMergeObserver(own_client, token, clock).observe(binding), timeout=30)
        else:
            observation = await asyncio.wait_for(GitHubMergeObserver(client, token, clock).observe(binding), timeout=30)
        if observation.head_sha != review.reviewed_head_sha:
            _refuse(EligibilityReason.HEAD_CHANGED)
        execution, node, flow, current, inputs = await protected_state()
        if snapshot != (current.id, current.revision, current.head_sha, current.repo, current.provider_repository_id, current.provider_pr_node_id):
            _refuse(EligibilityReason.SCOPE_CHANGED)
        decision = await authorized(execution, node, flow, current, inputs)
        if not decision.permitted:
            return blocked(EligibilityReason.AUTHORITY_DENIED, decision.reason.value)
        return evaluate_observation(observation)
    except EvidenceUnavailableError as exc:
        return blocked(exc.reason)
    except (httpx.HTTPError, TimeoutError):
        return blocked(EligibilityReason.PROVIDER_UNAVAILABLE)
    except BindingError:
        return blocked(EligibilityReason.SCOPE_CHANGED)
    except (ValueError, TypeError, KeyError, AttributeError):
        return blocked(EligibilityReason.INCOMPLETE_OBSERVATION)


def bounded_merge_summary(detail: Any) -> dict[str, Any] | None:
    """Read only the published summary vocabulary from an action's JSON detail."""
    if not isinstance(detail, dict):
        return None
    raw = detail.get("merge_eligibility")
    if not isinstance(raw, str) or len(raw) > 2048:
        return None
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            return None
        state = EligibilityState(value["state"])
        reasons = value["reasons"]
        if not isinstance(reasons, list) or len(reasons) > 12:
            return None
        reasons = [EligibilityReason(r).value for r in reasons]
        moment = datetime.fromisoformat(value["observed_at"])
        if moment.tzinfo is None or (state is EligibilityState.ELIGIBLE) != (not reasons):
            return None
        output = {"state": state.value, "reasons": reasons, "observed_at": moment.isoformat()}
        for key in ("head_sha", "base_sha"):
            if key in value:
                output[key] = _sha(value[key])
        if "queue_required" in value:
            output["queue_required"] = _boolean(value["queue_required"])
        for key in ("required_checks", "required_approvals"):
            if key in value:
                output[key] = _integer(value[key])
        return output
    except (EvidenceUnavailableError, KeyError, ValueError, TypeError):
        return None
