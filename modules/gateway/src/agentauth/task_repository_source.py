"""Read-only Task source adapter using canonical tenant installation ownership.

This reuses the bounded mediated GitHub archive transport, not GitHub-event
execution grants. Task authorization is supplied by the run-bound route.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from src.agentauth.github_operation_service import installation_token
from src.agentauth.github_operations import OperationRefusedError
from src.agentauth.github_provider import MAX_ARCHIVE_BYTES, ArchiveSlice, GitHubProvider, ProviderUnavailableError
from src.agentauth.task_repository_policy import validate_frozen_repository


@dataclass(frozen=True)
class SourceAssignment:
    repository: str
    repository_id: int
    branch: str
    default_branch: str


class TaskGitHubSource(GitHubProvider):
    def __init__(self, *, binding, token, reauthorize, client=None):
        branch = binding["base_branch"]
        super().__init__(
            token=token, assignment=SourceAssignment(binding["repository"], int(binding["repository_id"]), branch, branch), client=client
        )
        self.reauthorize = reauthorize

    async def _call(self, method, path, **kwargs):
        if method != "GET":
            raise OperationRefusedError("Task source adapter permits reads only")
        await self.reauthorize()
        return await super()._call(method, path, **kwargs)

    async def read_repository(self):
        repository = await self._call("GET", f"/repos/{self.repo}")
        if not isinstance(repository, dict) or type(repository.get("id")) is not int or repository["id"] != self.assignment.repository_id:
            raise OperationRefusedError("Task repository identity differs from policy")
        ref = await self._call("GET", f"/repos/{self.repo}/git/ref/heads/{quote(self.assignment.branch, safe='')}")
        head = ref.get("object", {}).get("sha") if isinstance(ref, dict) else None
        if not isinstance(head, str) or not re.fullmatch(r"[a-f0-9]{40}", head):
            raise OperationRefusedError("Task source ref did not resolve to a commit")
        await self.reauthorize()
        return {"branch_head": head, "default_branch_head": head}

    async def download(self):
        """One bounded provider download, ready for trusted staged transfer."""
        state = await self.read_repository()
        head = state["branch_head"]
        await self.reauthorize()
        try:
            async with self._client.stream("GET", f"/repos/{self.repo}/tarball/{head}", timeout=120) as response:
                if response.status_code == 200:
                    content, total, digest = await self._consume_archive(response, offset=0, window=MAX_ARCHIVE_BYTES)
                elif response.status_code in (301, 302, 307):
                    location = response.headers.get("location", "")
                    target = httpx.URL(location)
                    if target.scheme != "https" or target.host != "codeload.github.com" or target.port not in (None, 443) or target.userinfo:
                        raise OperationRefusedError("Task archive redirect is not the provider download host")
                    await self.reauthorize()
                    # Signed download URL receives no installation token.
                    async with httpx.AsyncClient(timeout=120, follow_redirects=False, trust_env=False) as download:
                        async with download.stream("GET", location) as archive:
                            if archive.status_code != 200:
                                raise ProviderUnavailableError("Task archive download unavailable")
                            content, total, digest = await self._consume_archive(archive, offset=0, window=MAX_ARCHIVE_BYTES)
                else:
                    raise ProviderUnavailableError("Task archive download unavailable")
        except (httpx.TransportError, httpx.InvalidURL):
            raise ProviderUnavailableError("Task archive download unavailable") from None
        await self.reauthorize()
        return ArchiveSlice(commit_sha=head, total_bytes=total, digest=digest, content=content)


async def authorize_source_connection(*, db, tenant, binding):
    if binding["provider"] != "github":
        raise OperationRefusedError("Task source provider adapter is unavailable")
    matched = re.fullmatch(r"installation:([1-9][0-9]{0,19})", binding["connection_id"])
    if matched is None or binding["repository"].count("/") != 1:
        raise OperationRefusedError("Task GitHub connection binding is invalid")
    installation_id = int(matched[1])
    from src.admin.installations.resolver import OwnerState, resolve_installation_owner

    owner, state = await resolve_installation_owner(installation_id, db=db)
    if state is not OwnerState.RESOLVED or owner is None or owner.tenant_id != tenant:
        raise OperationRefusedError("Task provider connection is not owned by the tenant")
    return installation_id


async def fetch_task_source(*, db, tenant, frozen, reauthorize, provider_client=None):
    validate_frozen_repository(frozen)
    binding = frozen["binding"]

    async def authorize():
        await reauthorize()
        return await authorize_source_connection(db=db, tenant=tenant, binding=binding)

    installation_id = await authorize()
    token = await installation_token(
        org_id=tenant, installation_id=installation_id, repository=binding["repository"], permissions={"contents": "read", "metadata": "read"}
    )
    await authorize()
    async with TaskGitHubSource(binding=binding, token=token, reauthorize=authorize, client=provider_client) as provider:
        result = await provider.download()
    await authorize()
    return result
