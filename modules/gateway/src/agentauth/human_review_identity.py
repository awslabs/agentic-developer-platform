"""Reviewer identity for an authenticated human's standalone PR mention.

The delivery journal supplies the original webhook envelope, bound by the
protected execution's digest. Neither a worker body nor its hydrated PR context
can select a persona or supply authority. GitHub scopes the token to a repository,
not a PR/head; the review publisher separately verifies its pinned source head.
"""

from __future__ import annotations

import json
from datetime import timedelta

from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.grants import AUTHORITY_GITHUB_EVENT, DelegatedGrant
from src.orchestration.execution_policy import Action, Decision, DenyReason
from src.shared.models.base import utcnow


def original_review_dispatch(store, *, pod_uid: str, invocation_id: str, execution: dict) -> dict:
    """Read only the authenticated pod's original assignment, never a body key."""
    journal = store._read(f"PODTASK#{pod_uid}", "DELIVERY")
    if not journal or journal.get("invocation_id") != {"S": invocation_id}:
        raise BootstrapRefusedError("review assignment unavailable")
    body = journal.get("body", {}).get("S")
    if not isinstance(body, str) or len(body.encode()) > 200 * 1024:
        raise BootstrapRefusedError("review assignment unavailable")
    try:
        envelope = json.loads(body)
    except (ValueError, TypeError):
        raise BootstrapRefusedError("review assignment unavailable") from None
    if not isinstance(envelope, dict):
        raise BootstrapRefusedError("review assignment unavailable")
    digest = {"S": envelope_digest(envelope)}
    if execution.get("envelope_digest") != digest or journal.get("envelope_digest") != digest:
        raise BootstrapRefusedError("review assignment unavailable")
    return envelope


def authorize_human_review(*, execution: dict, grant: DelegatedGrant, dispatch: dict | None) -> Decision:
    """Resolve REVIEW from the original human assignment and its live grant."""
    from src.orchestration.runtime_policy import WorkerCredentialDecision

    refusal = Decision.block(DenyReason.ACTION_NOT_PERMITTED, "no authenticated human PR review assignment")
    if not isinstance(dispatch, dict):
        return refusal
    source, actor, intent, payload = (dispatch.get(key) for key in ("source_ref", "actor", "intent", "payload"))
    if not all(isinstance(value, dict) for value in (source, actor, intent, payload)):
        return refusal
    sender, comment, issue = (payload.get(key) for key in ("sender", "comment", "issue"))
    if not all(isinstance(value, dict) for value in (sender, comment, issue)):
        return refusal
    author = comment.get("user")
    if not isinstance(author, dict):
        return refusal
    invocation = execution.get("invocation_id", {}).get("S")
    attempt = execution.get("current_attempt", {}).get("N")
    repo, number, installation = (source.get(key) for key in ("repo", "issue", "installation_id"))
    if (
        grant.authority.kind != AUTHORITY_GITHUB_EVENT
        or not grant.authority.human_id
        or grant.authority.org_id != grant.tenant_id
        or grant.flow_id != grant.authority.reference_id
        or not invocation
        or not attempt
        or grant.principal != f"{invocation}#{attempt}"
        or execution.get("tenant_id") != {"S": grant.tenant_id}
        or execution.get("flow_id") != {"S": grant.flow_id}
        or execution.get("status") != {"S": "active"}
        or execution.get("chain_depth") != {"N": "0"}
        or execution.get("parent_principal")
        or execution.get("persona") != {"S": "agent-codex-reviewer"}
        or dispatch.get("persona") != "agent-codex-reviewer"
        or dispatch.get("message_id") != invocation
        or dispatch.get("tenant_id") != grant.tenant_id
        or dispatch.get("channel") != "github"
        or actor.get("user_id") != grant.authority.human_id
        or actor.get("org_id") != grant.tenant_id
        or intent.get("trigger") != "mentioned"
        or intent.get("persona") != "agent-codex-reviewer"
        or not isinstance(repo, str)
        or repo not in grant.repo_scope
        or execution.get("repo") != {"S": repo}
        or type(number) is not int
        or number <= 0
        or execution.get("issue_number") != {"N": str(number)}
        or type(installation) is not int
        or installation <= 0
        or execution.get("installation_id") != {"N": str(installation)}
        or type(issue.get("number")) is not int
        or issue.get("number") != number
        or not isinstance(issue.get("pull_request"), dict)
        or sender.get("type") != "User"
        or author.get("type") != "User"
        or type(sender.get("id")) is not int
        or sender["id"] <= 0
        or sender["id"] != author.get("id")
        or sender["id"] != actor.get("github_id")
        or not isinstance(comment.get("body"), str)
        or not comment["body"]
    ):
        return refusal
    now = utcnow()
    if not grant.is_live(now):
        return Decision.block(DenyReason.GRANT_REVOKED, "human review grant is no longer live")
    # Installation tokens last one hour; the provider cannot shorten them to a
    # nearly expired grant. The route also checks actual expiry after minting.
    if grant.expires_at is None or grant.expires_at <= now + timedelta(hours=1, seconds=30):
        return Decision.block(DenyReason.CREDENTIAL_SCOPE_UNAVAILABLE, "human review grant cannot cover the provider token lifetime")
    return WorkerCredentialDecision(
        permitted=True,
        action=Action.REVIEW,
        permissions={"contents": "read", "pull_requests": "write", "metadata": "read"},
        not_after=grant.expires_at,
    )
