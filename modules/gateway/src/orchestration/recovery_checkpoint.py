"""A verified work branch for recovery before an implementation PR exists."""

import json
import re
from types import SimpleNamespace

import httpx

from .review_cycle import CycleBlockedError


async def provider_checkpoint(*, org_id, installation_id, repo, issue):
    from src.knowledge.github_app_service import mint_installation_token_with_expiry, resolve_tenant_app_credentials

    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not str(issue).isdigit():
        raise CycleBlockedError("recovery_repository_invalid")
    branch = f"agent/issue-{issue}"
    app, key = await resolve_tenant_app_credentials(org_id)
    token, _ = await mint_installation_token_with_expiry(
        app,
        key,
        installation_id,
        repositories=[repo.split("/")[1]],
        permissions={"metadata": "read", "contents": "read"},
    )
    async with httpx.AsyncClient(base_url="https://api.github.com", timeout=10, trust_env=False, follow_redirects=False) as client:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        repository = await client.get(f"/repos/{repo}", headers=headers)
        repository.raise_for_status()
        repository = repository.json()
        response = await client.get(f"/repos/{repo}/git/ref/heads/{branch}", headers=headers)
        response.raise_for_status()
        ref = response.json()
    sha = ref.get("object", {}).get("sha", "")
    if (
        repository.get("full_name", "").lower() != repo.lower()
        or type(repository.get("id")) is not int
        or ref.get("ref") != f"refs/heads/{branch}"
        or ref.get("object", {}).get("type") != "commit"
        or not re.fullmatch(r"[a-f0-9]{40}", sha)
    ):
        raise CycleBlockedError("recovery_checkpoint_unverified")
    return {
        "repo": repository["full_name"],
        "provider_repository_id": repository["id"],
        "installation_id": installation_id,
        "branch": branch,
        "head_sha": sha,
    }


def checkpoint_target(data, node):
    checkpoint = data.get("checkpoint")
    if not isinstance(checkpoint, dict):
        raise CycleBlockedError("bound_delivery_missing")
    if checkpoint.get("branch") != f"agent/issue-{str(node.issue_ref).lstrip('#')}" or not re.fullmatch(
        r"[a-f0-9]{40}", checkpoint.get("head_sha", "")
    ):
        raise CycleBlockedError("recovery_checkpoint_unverified")
    return SimpleNamespace(
        **checkpoint,
        org_id=node.org_id,
        node_id=node.id,
        id="checkpoint:" + checkpoint["head_sha"],
        revision=1,
        accepted_scope=data["accepted_scope"],
        pr_number=None,
        provider_pr_node_id=None,
    )


async def ensure_checkpoint_pr(session, *, node, report, execution, identity):
    """Publish a draft recovery target behind a durable, policy-scoped intent.

    GitHub's same-head/base open-PR uniqueness and discovery make lost replies
    recoverable. Never create a replacement for an existing story binding.
    """
    from dataclasses import replace
    from datetime import UTC, datetime
    from uuid import NAMESPACE_URL, uuid5

    from sqlalchemy import select

    from src.knowledge.github_app_service import mint_installation_token_with_expiry, resolve_tenant_app_credentials

    from .execution_policy import Action
    from .execution_runner import RunnerContext
    from .models import OrchestrationDecision, OrchestrationFlow, OrchestrationNode
    from .pr_bindings import active_binding_for_node, register_binding, resolve_registration_target
    from .pr_identity import resolve_pr_identity
    from .review_recovery import RECOVERY_ACTOR, exited_run, report_exit_resolver, require_autonomous_recovery
    from .shared_policy import authorize_shared_action, shared_inputs
    from .state import ActorKind

    target = await resolve_registration_target(session, run_id=report.run_id, expected_org_id=node.org_id)
    if target.node_id != node.id or target.attempt != node.attempts:
        raise CycleBlockedError("recovery_assignment_changed")
    checkpoint = await provider_checkpoint(org_id=node.org_id, installation_id=target.installation_id, repo=target.repo, issue=target.issue)
    scope = target.accepted_scope
    data = {"checkpoint": checkpoint, "accepted_scope": scope}
    candidate = checkpoint_target(data, node)
    key = str(uuid5(NAMESPACE_URL, f"stalled-pr:{node.org_id}:{node.id}:{report.run_id}"))

    async def authorize():
        # Flow/node locks serialize with pause and competing recovery controls.
        await session.scalar(select(OrchestrationFlow).where(OrchestrationFlow.id == node.flow_id).with_for_update())
        await session.scalar(
            select(OrchestrationNode).where(OrchestrationNode.id == node.id).with_for_update().execution_options(populate_existing=True)
        )
        inputs, _ = await shared_inputs(session, org_id=node.org_id, flow_id=node.flow_id, lock=True)
        await require_autonomous_recovery(session, node, report, execution, inputs)
        proposed = SimpleNamespace(**{k: v for k, v in node.__dict__.items() if not k.startswith("_")})
        proposed.state = "running"
        context = RunnerContext(identity, execution, datetime.now(UTC))
        for action in (Action.REVIEW, Action.REPAIR):
            await authorize_shared_action(session, context, proposed, candidate, report.run_id, action, observation=True)
        await exited_run(report.run_id, node.org_id, report_exit_resolver(report))
        existing = await active_binding_for_node(session, org_id=node.org_id, node_id=node.id, attempt=node.attempts)
        if existing is not None:
            raise CycleBlockedError("recovery_binding_changed")

    await authorize()
    intent = await session.get(OrchestrationDecision, key)
    document = {
        **data,
        "prior_run_id": report.run_id,
        "attempt": node.attempts,
        "plan_version": identity.accepted_plan_version,
        "execution_id": execution.id,
    }
    if intent is None:
        session.add(
            OrchestrationDecision(
                id=key,
                org_id=node.org_id,
                flow_id=node.flow_id,
                node_id=node.id,
                kind="recovery_pr_prepared",
                actor_kind="service",
                actor_id=RECOVERY_ACTOR,
                actor_role="engine",
                reason=json.dumps(document),
            )
        )
        await session.commit()  # No GitHub write without a durable recovery intent.
    elif json.loads(intent.reason) != document or intent.actor_id != RECOVERY_ACTOR:
        raise CycleBlockedError("recovery_checkpoint_changed")
    await authorize()
    current = await provider_checkpoint(org_id=node.org_id, installation_id=target.installation_id, repo=target.repo, issue=target.issue)
    if current != checkpoint:
        raise CycleBlockedError("recovery_checkpoint_changed")
    app, secret = await resolve_tenant_app_credentials(node.org_id)
    token, _ = await mint_installation_token_with_expiry(
        app,
        secret,
        target.installation_id,
        repositories=[target.repo.split("/")[1]],
        permissions={"metadata": "read", "contents": "read", "pull_requests": "write"},
    )
    async with httpx.AsyncClient(base_url="https://api.github.com", timeout=10, trust_env=False, follow_redirects=False) as client:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        repository = await client.get(f"/repos/{target.repo}", headers=headers)
        repository.raise_for_status()
        repository = repository.json()
        if repository["id"] != checkpoint["provider_repository_id"]:
            raise CycleBlockedError("recovery_repository_changed")
        response = await client.get(
            f"/repos/{target.repo}/pulls",
            headers=headers,
            params={"state": "open", "head": target.repo.split("/")[0] + ":" + checkpoint["branch"], "per_page": 2},
        )
        response.raise_for_status()
        pulls = response.json()
        if len(pulls) > 1:
            raise CycleBlockedError("recovery_pr_ambiguous")
        if not pulls:
            response = await client.post(
                f"/repos/{target.repo}/pulls",
                headers=headers,
                json={
                    "title": ("Recovery: " + node.title)[:240],
                    "head": checkpoint["branch"],
                    "base": repository["default_branch"],
                    "draft": True,
                    "body": f"Part of #{target.issue}.\n\nRecovery of committed work after the worker stopped. "
                    "Implementation and acceptance are not yet complete. The assigned reviewer must read the current story, "
                    f"finish required work, and qualify the final head.\n\nCheckpoint: `{checkpoint['head_sha']}`. "
                    f"Recovery intent: `{key}`.",
                },
            )
            response.raise_for_status()
            pull = response.json()
        else:
            pull = pulls[0]
    if (
        pull["head"]["sha"] != checkpoint["head_sha"]
        or pull["head"]["ref"] != checkpoint["branch"]
        or pull["head"]["repo"]["id"] != checkpoint["provider_repository_id"]
        or pull["base"]["repo"]["id"] != checkpoint["provider_repository_id"]
    ):
        raise CycleBlockedError("recovery_pr_head_changed")
    remote = await resolve_pr_identity(org_id=node.org_id, installation_id=target.installation_id, repo=target.repo, pr_number=pull["number"])
    if remote.head_sha != checkpoint["head_sha"]:
        raise CycleBlockedError("recovery_pr_head_changed")
    binding, _ = await register_binding(
        session, target=replace(target, accepted_scope=scope), pr=remote, actor_id=RECOVERY_ACTOR, actor_kind=ActorKind.SERVICE
    )
    await session.commit()
    return binding
