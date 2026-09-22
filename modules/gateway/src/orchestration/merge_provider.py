"""Scoped provider observations and ordinary expected-head merge/queue effects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from src.agentauth.github_provider import BoundMergeAssignment, GitHubProvider, ProviderUnavailableError
from src.knowledge.github_app_service import mint_installation_token_with_expiry, resolve_tenant_app_credentials

from .merge_evidence import READ_PERMISSIONS, _boolean, _integer, _object, _sha, _text
from .review_cycle import CycleBlockedError


@dataclass(frozen=True)
class MergeState:
    head_sha: str
    base_sha: str
    head_ref: str
    base_ref: str
    merged: bool
    open: bool
    merge_sha: str | None
    merged_at: str | None
    queue_id: str | None
    observed_at: str


class MergeProvider:
    def __init__(self, *, client=None, clock=lambda: datetime.now(UTC)):
        self.client, self.clock = client, clock

    async def token(self, binding, *, write=False, evidence=False):
        app, key = await resolve_tenant_app_credentials(binding.org_id)
        permissions = {"contents": "write" if write else "read", "pull_requests": "write" if write else "read", "metadata": "read"}
        if evidence:
            if write:
                raise ValueError("Evidence observations require read-only credentials")
            permissions = dict(READ_PERMISSIONS)
        token, expires = await mint_installation_token_with_expiry(
            app,
            key,
            binding.installation_id,
            repositories=[binding.repo.split("/", 1)[1]],
            permissions=permissions,
        )
        deadline = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        if deadline.tzinfo is None or deadline <= self.clock():
            raise CycleBlockedError("scoped_merge_credential_expired")
        return token

    async def read(self, binding):
        token = await self.token(binding)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False)
        try:
            response = await client.get(f"https://api.github.com/repos/{binding.repo}/pulls/{binding.pr_number}", headers=headers)
            response.raise_for_status()
            pull = response.json()
            self.identity(binding, pull)
            head, base = pull["head"], pull["base"]
            owner, name = binding.repo.split("/", 1)
            response = await client.post(
                "https://api.github.com/graphql",
                headers=headers,
                json={
                    "query": (
                        "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
                        "databaseId pullRequest(number:$number){id headRefOid baseRefOid baseRef{name target{oid}} merged mergeQueueEntry{id}}}}"
                    ),
                    "variables": {"owner": owner, "name": name, "number": binding.pr_number},
                },
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("errors"):
                raise ProviderUnavailableError("merge queue state unavailable")
            repo = (payload.get("data") or {}).get("repository") or {}
            current = repo.get("pullRequest") or {}
            if (repo.get("databaseId"), current.get("id"), current.get("headRefOid"), current.get("baseRefOid"), current.get("merged")) != (
                binding.provider_repository_id,
                binding.provider_pr_node_id,
                head["sha"],
                base["sha"],
                pull["merged"],
            ):
                raise ProviderUnavailableError("PR changed during merge observation")
            queue = current.get("mergeQueueEntry")
            if "mergeQueueEntry" not in current or (queue is not None and not isinstance(queue, dict)):
                raise ProviderUnavailableError("merge queue state incomplete")
            merged = _boolean(pull["merged"])
            # baseRefOid agrees with the REST PR snapshot, but can lag main.
            # Open-PR mutation fences must use the current branch target instead.
            live_base = current.get("baseRef")
            if merged and live_base is None:
                base_sha = _sha(base["sha"])
            else:
                live_base = _object(live_base)
                if live_base.get("name") != base["ref"]:
                    raise ProviderUnavailableError("PR base branch changed during merge observation")
                base_sha = _sha(_object(live_base.get("target")).get("oid"))
            merged_at = pull.get("merged_at")
            if merged:
                when = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
                if when.tzinfo is None or when > self.clock():
                    raise ProviderUnavailableError("merge timestamp unverifiable")
            if pull.get("state") not in {"open", "closed"}:
                raise ProviderUnavailableError("PR state unavailable")
            return MergeState(
                _sha(head["sha"]),
                base_sha,
                _text(head["ref"]),
                _text(base["ref"]),
                merged,
                pull["state"] == "open",
                _sha(pull["merge_commit_sha"]) if merged else None,
                merged_at if merged else None,
                _text(queue["id"]) if queue else None,
                self.clock().isoformat(),
            )
        finally:
            if owned:
                await client.aclose()

    @staticmethod
    def identity(binding, pull):
        if (
            _integer(pull.get("number"), minimum=1) != binding.pr_number
            or pull.get("node_id") != binding.provider_pr_node_id
            or _integer(((pull.get("base") or {}).get("repo") or {}).get("id"), minimum=1) != binding.provider_repository_id
            or _integer(((pull.get("head") or {}).get("repo") or {}).get("id"), minimum=1) != binding.provider_repository_id
        ):
            raise CycleBlockedError("merge_provider_identity_changed")

    async def perform(self, binding, state, *, method, operation_key, reauthorize):
        await reauthorize()
        token = await self.token(binding, write=True)
        assignment = BoundMergeAssignment(
            binding.repo, binding.provider_repository_id, state.head_ref, state.base_ref, binding.pr_number, binding.provider_pr_node_id
        )
        owned = self.client is None
        client = self.client or httpx.AsyncClient(
            base_url="https://api.github.com",
            timeout=15,
            follow_redirects=False,
            trust_env=False,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        client.headers["Authorization"] = f"Bearer {token}"
        try:
            async with GitHubProvider(token=token, assignment=assignment, client=client) as provider:
                if method == "queue":
                    return await provider.enqueue_pull_request(
                        pull_number=binding.pr_number, expected_head=state.head_sha, operation_key=operation_key, reauthorize=reauthorize
                    )
                return await provider.merge_pull_request(
                    pull_number=binding.pr_number, expected_head=state.head_sha, method=method, reauthorize=reauthorize
                )
        finally:
            if owned:
                await client.aclose()
