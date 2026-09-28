"""GitHub publication of a gateway-validated Task workspace manifest.

Caller must validate durable check evidence and journal the operation before
invoking this adapter. No HTTP route is exposed by this module. Provider commits
are explicitly mapped to the validated local tree, never equated with local SHA.
"""

from __future__ import annotations

import base64
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.agentauth.github_operation_service import installation_token
from src.agentauth.github_operations import OperationRefusedError
from src.agentauth.github_provider import FileChange, GitHubProvider, ProviderConflictError
from src.agentauth.task_repository_policy import validate_frozen_repository
from src.agentauth.task_repository_source import SourceAssignment, authorize_source_connection

SHA = r"^[a-f0-9]{40}$"


class PublicationFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1, max_length=4096)
    mode: Literal["100644", "100755"]
    deleted: bool
    content_base64: str | None = Field(max_length=262144)

    @field_validator("path")
    @classmethod
    def path_scope(cls, value):
        if (
            len(value.split("/")) > 32
            or any(p in {"", ".", ".."} or p.casefold() == ".git" for p in value.split("/"))
            or re.search(r"[\x00-\x1f\x7f\\:]", value)
        ):
            raise ValueError("invalid publication path")
        # Workflow write authority is not part of this Task adapter's grant.
        if value.casefold().startswith(".github/workflows/"):
            raise ValueError("workflow publication is not admitted")
        return value

    def change(self):
        if self.deleted:
            if self.content_base64 is not None:
                raise ValueError("deleted file cannot contain data")
            return FileChange(path=self.path, mode=self.mode, deleted=True)
        if self.content_base64 is None:
            raise ValueError("publication content missing")
        return FileChange(path=self.path, mode=self.mode, content=base64.b64decode(self.content_base64, validate=True))


class TaskChangeManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1.0"]
    provider: Literal["github", "gitlab"]
    repository_id: str = Field(pattern=r"^[1-9][0-9]{0,31}$")
    repository: str = Field(min_length=3, max_length=512)
    source_revision: str = Field(pattern=SHA)
    local_head: str = Field(pattern=SHA)
    base_tree: str = Field(pattern=SHA)
    tree: str = Field(pattern=SHA)
    changes: list[PublicationFile] = Field(min_length=1, max_length=100)

    def file_changes(self):
        if len({entry.path for entry in self.changes}) != len(self.changes):
            raise ValueError("duplicate publication path")
        changes = [entry.change() for entry in self.changes]
        if sum(len(change.content or b"") for change in changes) > 192 * 1024:
            raise ValueError("publication content exceeds bound")
        return changes


def task_publication_branch(task_id):
    if not isinstance(task_id, str) or not re.fullmatch(r"tsk_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", task_id):
        raise OperationRefusedError("Task publication identity invalid")
    # ADP memory uses refs/heads/adp. Git cannot also store refs/heads/adp/...
    return "adp-task-" + task_id.removeprefix("tsk_")


class TaskGitHubPublication(GitHubProvider):
    def __init__(self, *, token, binding, task_id, manifest, reauthorize, client=None):
        self.manifest, self.reauthorize = manifest, reauthorize
        self.task_id = task_id
        branch = task_publication_branch(task_id)
        if branch == binding["base_branch"]:
            raise OperationRefusedError("Task branch must differ from base")
        super().__init__(
            token=token,
            assignment=SourceAssignment(binding["repository"], int(binding["repository_id"]), branch, binding["base_branch"]),
            client=client,
        )

    async def _call(self, method, path, **kwargs):
        await self.reauthorize()
        result = await super()._call(method, path, **kwargs)
        if method == "GET" and path == f"/repos/{self.repo}/git/commits/{self.manifest.source_revision}":
            if result.get("tree", {}).get("sha") != self.manifest.base_tree:
                raise ProviderConflictError("Materialized source tree differs from provider source")
        if method == "POST" and path == f"/repos/{self.repo}/git/trees":
            # Stop before commit/ref publication if remote content is not exactly
            # the tree named by trusted final-commit validation evidence.
            if result.get("sha") != self.manifest.tree:
                raise ProviderConflictError("Provider tree differs from validated workspace")
        if method == "POST" and path == f"/repos/{self.repo}/git/commits":
            if (
                not re.fullmatch(SHA, str(result.get("sha", "")))
                or result.get("tree", {}).get("sha") != self.manifest.tree
                or [p.get("sha") for p in result.get("parents", [])] != [self.manifest.source_revision]
            ):
                raise ProviderConflictError("Provider commit differs from validated publication")
        return result

    async def publish(self, *, title, body, changes):
        if not isinstance(title, str) or not title.strip() or len(title) > 255 or not isinstance(body, str) or len(body.encode()) > 16384:
            raise OperationRefusedError("Task change description exceeds bound")
        state = await self.read_repository()
        await self._call("GET", f"/repos/{self.repo}/git/commits/{self.manifest.source_revision}")
        published = state["branch_head"]
        marker = f"ADP-Task: {self.task_id}\nADP-Local-Commit: {self.manifest.local_head}"
        if published is None:
            if state["default_branch_head"] != self.manifest.source_revision:
                raise ProviderConflictError("Task base moved before publication")
            result = await self.publish_commit(changes=changes, message=title + "\n\n" + marker, expected_head=None, reauthorize=self.reauthorize)
            published = result.sha
        commit = await self._call("GET", f"/repos/{self.repo}/git/commits/{published}")
        if (
            commit.get("tree", {}).get("sha") != self.manifest.tree
            or [p.get("sha") for p in commit.get("parents", [])] != [self.manifest.source_revision]
            or not commit.get("message", "").endswith(marker)
        ):
            raise ProviderConflictError("Task branch does not contain this validated publication")
        change = await self.upsert_pull_request(title=title, body=body + f"\n\n<!-- adp-task:{self.task_id} -->", reauthorize=self.reauthorize)
        pull = await self._call("GET", f"/repos/{self.repo}/pulls/{change['number']}")
        self._require_assigned_pull_request(pull)
        if pull.get("head", {}).get("sha") != published or pull.get("state") != "open" or pull.get("draft") is not False:
            raise ProviderConflictError("Published Task change identity or state differs")
        await self.reauthorize()
        return {
            "schema_version": "1.0",
            "task_id": self.task_id,
            "provider": "github",
            "repository_id": self.manifest.repository_id,
            "source_revision": self.manifest.source_revision,
            "local_head": self.manifest.local_head,
            "tree": self.manifest.tree,
            "provider_head": published,
            "branch": self.assignment.branch,
            "number": change["number"],
            "url": change["html_url"],
            "state": "open",
            "draft": False,
        }


async def publish_task_change(*, db, tenant, task_id, frozen, manifest, title, body, reauthorize, provider_client=None):
    validate_frozen_repository(frozen)
    binding = frozen["binding"]
    proposal = TaskChangeManifest.model_validate(manifest)
    changes = proposal.file_changes()
    if any(getattr(proposal, key) != binding[key] for key in ("provider", "repository", "repository_id")):
        raise OperationRefusedError("Task change differs from repository authority")

    async def authorize():
        await reauthorize()
        return await authorize_source_connection(db=db, tenant=tenant, binding=binding)

    installation_id = await authorize()
    token = await installation_token(
        org_id=tenant,
        installation_id=installation_id,
        repository=binding["repository"],
        permissions={"contents": "write", "pull_requests": "write", "metadata": "read"},
    )
    await authorize()
    async with TaskGitHubPublication(
        token=token, binding=binding, task_id=task_id, manifest=proposal, reauthorize=authorize, client=provider_client
    ) as provider:
        return await provider.publish(title=title, body=body, changes=changes)


async def observe_task_change(*, db, tenant, task_id, frozen, receipt, reauthorize, provider_client=None):
    """Fresh read-only completion observation, using no content-write credential."""
    validate_frozen_repository(frozen)
    binding = frozen["binding"]
    branch = task_publication_branch(task_id)
    if (
        receipt.get("task_id") != task_id
        or receipt.get("repository_id") != binding["repository_id"]
        or receipt.get("provider") != "github"
        or receipt.get("branch") != branch
        or type(receipt.get("number")) is not int
        or receipt["number"] < 1
    ):
        raise OperationRefusedError("Completion publication identity differs")

    async def authorize():
        await reauthorize()
        return await authorize_source_connection(db=db, tenant=tenant, binding=binding)

    installation = await authorize()
    token = await installation_token(
        org_id=tenant,
        installation_id=installation,
        repository=binding["repository"],
        permissions={"contents": "read", "pull_requests": "read", "metadata": "read"},
    )

    class Observer(GitHubProvider):
        async def _call(self, method, path, **kwargs):
            if method != "GET":
                raise OperationRefusedError("Completion cannot mutate provider state")
            await authorize()
            return await super()._call(method, path, **kwargs)

    assignment = SourceAssignment(binding["repository"], int(binding["repository_id"]), branch, binding["base_branch"])
    async with Observer(token=token, assignment=assignment, client=provider_client) as provider:
        state = await provider.read_repository()
        pull = await provider._call("GET", f"/repos/{provider.repo}/pulls/{receipt['number']}")
        provider._require_assigned_pull_request(pull)
        commit = await provider._call("GET", f"/repos/{provider.repo}/git/commits/{receipt['provider_head']}")
        if (
            state["branch_head"] != receipt["provider_head"]
            or pull.get("head", {}).get("sha") != receipt["provider_head"]
            or pull.get("state") != "open"
            or pull.get("draft") is not False
            or pull.get("html_url") != receipt["url"]
            or commit.get("tree", {}).get("sha") != receipt["tree"]
            or [parent.get("sha") for parent in commit.get("parents", [])] != [receipt["source_revision"]]
        ):
            raise ProviderConflictError("Published change no longer matches validated completion")
        await authorize()
        return dict(receipt)
