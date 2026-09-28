"""The provider calls a mediated operation is allowed to make (#5223).

This module is the only place the platform talks to GitHub on a policy-bearing
worker's behalf, and it is deliberately small: five operations, each addressing a
fixed set of endpoints, with the repository taken from an authorized assignment
rather than from anything a caller said.

There is no method/URL forwarding here, and that absence is the control. A
"perform this request" helper would make every capability the installation token
carries reachable again — merge, workflow dispatch, branch protection, branch
deletion, force push, token exchange — and the typed enum would then only
describe intent rather than bound it. Each function below names its endpoints
literally, so a capability that is not written here cannot be invoked through
here.

Two properties are load-bearing and easy to lose:

**Every provider mutation is preceded by a fresh authorization.** The caller
passes `reauthorize`, an awaitable the service supplies, and each mutating step
awaits it immediately before the call that has an effect — including each retry,
and again after a bounded upload finishes. A single check at the start of a
multi-step publish would let a grant revoked mid-sequence still land a commit.

**Ref updates are never forced and never blind.** A commit is published by
building a tree, creating a commit whose parent is the branch head we actually
observed, then updating the ref with `force: false` and the expected old head.
GitHub rejects the update if the branch moved, which surfaces as a visible
conflict instead of silently discarding someone else's work.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from src.agentauth.github_operations import (
    GitHubOperation,
    OperationAssignment,
    OperationRefusedError,
)

logger = logging.getLogger("bedrockgateway.agentauth.github_provider")

GITHUB_API_BASE = "https://api.github.com"
_API_VERSION = "2022-11-28"
_TIMEOUT = 30.0


class WorkflowPermissionRequiredError(OperationRefusedError):
    """GitHub refused a merge because its token cannot update workflow files."""


# GitHub's own cap on a blob posted as base64 JSON is well above this; the limit
# here bounds what one mediated call will relay, so a runaway generated file
# cannot turn into an unbounded gateway-side upload.
#
# 5 MiB, and like `ARCHIVE_SLICE_BYTES` the number is set by the transport. A
# publish travels the SAME REST API Gateway as the archive response, and its 10 MB
# payload quota is a hard limit in both directions; base64 costs 4/3, so the former
# 8 MiB permitted a ~11.18 MB request body the edge refuses before the gateway ever
# applies this bound. The failure surfaced as a transport error rather than this
# module's clear refusal, which is the wrong end to learn a size limit from.
# 5 MiB encodes to ~6.99 MB, leaving ~3 MB for the rest of the envelope.
MAX_BLOB_BYTES = 5 * 1024 * 1024
MAX_FILES_PER_COMMIT = 500

# Bound on the DECODED content of one publish, summed across its files. The
# per-file cap above cannot express this: `MAX_FILES_PER_COMMIT` files each just
# inside it still add up to a body the edge will not accept. Enforced on the
# decoded bytes so the check is about what is being published rather than about
# how it happened to be encoded. 6 MiB decoded is ~8.39 MB encoded, that being the
# same headroom the archive slice takes against the same 10 MB quota.
MAX_COMMIT_CONTENT_BYTES = 6 * 1024 * 1024

# Bound on a repository archive relayed to a worker (#5223 startup). Enforced while
# streaming, not from the declared `Content-Length`: a length header is the sender's
# claim about the body, so trusting it would let an oversized or unbounded response
# through on a wrong or absent value.
#
# 32 MiB, and the number is set by the transport rather than by taste. The deployed
# edge is a REST API Gateway, whose 10 MB response payload limit is a hard service
# quota that cannot be raised, so an archive is delivered in slices (see
# `ARCHIVE_SLICE_BYTES` and the route). A stateless slice request re-fetches the
# archive and returns one window of it, which makes the total bytes moved
# quadratic in archive size: an N-byte archive costs N * ceil(N / slice). At 32 MiB
# that is ~6 fetches; at the former 128 MiB it would have been ~22 fetches and
# 2.8 GB moved for one work tree. The bound is therefore concurrent-request memory
# AND aggregate fetch cost. Raising it materially means holding the archive in
# object storage instead of re-fetching it.
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024

# How much of the archive one mediated response carries, before base64. Base64 costs
# 4/3, so 6 MiB becomes ~8.39 MB of JSON — inside the edge's 10 MB response limit
# with room for the envelope. This is a transport constant, not a tuning knob: a
# larger value produces responses the deployed edge silently fails to return.
ARCHIVE_SLICE_BYTES = 6 * 1024 * 1024

# Archives take longer than an API call to produce and stream, but not unboundedly:
# the edge's own integration timeout is 29 s (`infra/modules/api-gateway/main.tf`),
# so a longer budget here only produces a 504 at the edge while the gateway is still
# waiting. Kept separate from `_TIMEOUT` so changing it cannot silently slacken
# every other call.
_ARCHIVE_TIMEOUT = 25.0

# Git's own file modes. A mode outside this set is refused rather than coerced:
# `160000` (gitlink) would attach a submodule pointer, and submodule content is
# not reviewed by anything that reviewed this change.
_BLOB_MODE = "100644"
_EXECUTABLE_MODE = "100755"
_SYMLINK_MODE = "120000"
ALLOWED_FILE_MODES = frozenset({_BLOB_MODE, _EXECUTABLE_MODE, _SYMLINK_MODE})


class ProviderNotFoundError(OperationRefusedError):
    """GitHub returned an explicit 404; other failures do not prove absence."""


class ProviderConflictError(OperationRefusedError):
    """The branch moved under us, or the PR is not in the state we observed.

    Distinct from a refusal so the worker can see a conflict as a conflict and
    reconcile, rather than reading "not authorized" and giving up. It carries no
    provider response body: those quote tokens and internal URLs.
    """


class ProviderUnavailableError(OperationRefusedError):
    """The provider did not give us a usable answer. Safe to reconcile and retry.

    Raised for timeouts and 5xx. A timeout specifically does NOT mean the effect
    did not happen, which is why the caller reconciles by looking the intended
    object up before retrying.

    `prepared_parent`/`prepared_tree` carry the objects we had already constructed
    when the call failed, when we had them. Reconciliation needs them to establish
    that a commit now on the branch is *ours* rather than merely similar, and only
    the code that built them knows their values — so they travel with the error
    instead of being recomputed (which would guess) or matched by message (which
    would be wrong). Both None means the outcome is genuinely unknown.
    """

    def __init__(self, *args: object, prepared_parent: str | None = None, prepared_tree: str | None = None) -> None:
        super().__init__(*args)
        self.prepared_parent = prepared_parent
        self.prepared_tree = prepared_tree


@dataclass(frozen=True)
class FileChange:
    """One file in a proposed commit, as git records it.

    `content` is raw bytes, so binary files survive; base64 happens at the API
    boundary. `deleted` is separate from empty content, because a zero-byte file
    and a removed file are different trees.
    """

    path: str
    content: bytes | None = None
    mode: str = _BLOB_MODE
    deleted: bool = False

    def __post_init__(self) -> None:
        if not self.path or self.path.startswith("/") or ".." in self.path.split("/"):
            raise OperationRefusedError("proposed change carries an unusable path")
        if self.deleted:
            if self.content is not None:
                raise OperationRefusedError("a deletion cannot carry content")
            return
        if self.content is None:
            raise OperationRefusedError("a file change must carry content or be a deletion")
        if self.mode not in ALLOWED_FILE_MODES:
            # Notably excludes 160000 (gitlink): a submodule pointer imports code
            # that nothing reviewing this change has seen.
            raise OperationRefusedError("proposed change carries an unsupported file mode")
        if len(self.content) > MAX_BLOB_BYTES:
            raise OperationRefusedError("proposed change exceeds the permitted file size")


@dataclass(frozen=True)
class ArchiveSlice:
    """One window of a repository archive, plus identity of the whole archive.

    `total_bytes` and `digest` describe the ENTIRE archive, not `content`. They are
    what lets a caller assembling successive windows prove it assembled one
    consistent snapshot: each slice request re-fetches the archive, so a push
    between slices changes the digest and the caller restarts instead of
    materializing a spliced tree.
    """

    commit_sha: str
    total_bytes: int
    digest: str
    content: bytes
    offset: int = 0

    @property
    def complete(self) -> bool:
        """Whether this is the last window of the archive."""
        return self.offset + len(self.content) >= self.total_bytes


@dataclass(frozen=True)
class PublishedCommit:
    sha: str
    branch: str
    parent_sha: str


def _archive_slice(consumed: tuple[bytes, int, str], *, commit_sha: str, offset: int) -> ArchiveSlice:
    """Build a slice, refusing an offset that is not within the archive.

    An offset past the end is refused rather than returning an empty slice: an empty
    tail is indistinguishable from "done" to a caller looping until it has
    `total_bytes`, and silently answering a nonsense offset would let a confused
    worker spin. Offset exactly at the end is only valid for an empty archive.
    """
    content, total, digest = consumed
    if offset and offset >= total:
        raise OperationRefusedError("the archive offset is not within the archive")
    return ArchiveSlice(commit_sha=commit_sha, total_bytes=total, digest=digest, content=content, offset=offset)


@dataclass(frozen=True)
class BoundMergeAssignment:
    """Engine-owned PR identity resolved from the current accepted binding.

    Unlike a worker assignment this carries no invented pod or worker identity.
    Only the engine merge adapter constructs it after current policy/grant checks.
    """

    repository: str
    repository_id: int
    branch: str
    default_branch: str
    pr_number: int
    pr_node_id: str


class GitHubProvider:
    """Performs the five typed operations against one authorized repository.

    Constructed per request with a token minted for that single operation. The
    token is held only for the lifetime of this object and is never logged,
    returned, or placed in an exception message.
    """

    def __init__(self, *, token: str, assignment: OperationAssignment | BoundMergeAssignment, client: httpx.AsyncClient | None = None) -> None:
        self._token = token
        self.assignment = assignment
        self._client = client
        self._owned = client is None

    async def __aenter__(self) -> GitHubProvider:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=GITHUB_API_BASE,
                timeout=_TIMEOUT,
                follow_redirects=False,
                trust_env=False,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": _API_VERSION,
                },
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owned and self._client is not None:
            await self._client.aclose()

    @property
    def repo(self) -> str:
        return self.assignment.repository

    async def _call(self, method: str, path: str, *, json: dict | None = None, expect: tuple[int, ...] = (200,)) -> Any:
        """One provider call, with failures mapped to intent rather than to status.

        The distinction that matters is conflict vs. unavailable vs. refused: the
        caller reconciles a timeout, surfaces a conflict, and stops on a refusal.
        Provider response bodies are never propagated — they echo request content
        and URLs, and this path handles credentials.
        """
        assert self._client is not None, "GitHubProvider must be used as an async context manager"
        try:
            response = await self._client.request(method, path, json=json)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderUnavailableError(f"provider did not respond to {method} {path}") from exc
        if response.status_code in expect:
            return response.json() if response.content else None
        if response.status_code == 404:
            raise ProviderNotFoundError("provider resource was not found")
        if response.status_code in (409, 422):
            # 422 on a ref update is how GitHub reports "not a fast-forward".
            raise ProviderConflictError(f"provider reports a conflict for {method} {path}")
        if response.status_code >= 500 or response.status_code == 429:
            raise ProviderUnavailableError(f"provider is unavailable for {method} {path}")
        if response.status_code in (401, 403):
            if response.status_code == 403 and method == "PUT" and path.endswith("/merge"):
                try:
                    payload = response.json()
                    message = payload.get("message") if isinstance(payload, dict) else None
                except ValueError:
                    message = None
                if isinstance(message, str) and re.fullmatch(
                    r"refusing to allow a GitHub App to create or update workflow `[^`]+` without `workflows` permission",
                    message,
                ):
                    raise WorkflowPermissionRequiredError("GitHub requires workflow permission for this merge")
            # Do not retry: a mediated call is authorized before it is made, so
            # the provider disagreeing means our authority is genuinely absent.
            raise OperationRefusedError("provider refused the mediated operation")
        raise OperationRefusedError(f"provider rejected {method} {path}")

    # --- Read -------------------------------------------------------------

    async def read_repository(self) -> dict:
        """The assigned repository and the head of its assigned branch.

        Keyed on the immutable numeric ID: if the name now resolves to a
        different repository, the assignment does not apply to it. A rename
        followed by a squatter taking the old name is the case this catches.
        """
        repository = await self._call("GET", f"/repos/{self.repo}")
        if repository.get("id") != self.assignment.repository_id:
            raise OperationRefusedError("provider repository identity does not match the assignment")
        branch_head = None
        try:
            ref = await self._call("GET", f"/repos/{self.repo}/git/ref/heads/{self.assignment.branch}")
            branch_head = ref["object"]["sha"]
        except ProviderNotFoundError:
            # Only an explicit 404 establishes that a branch is absent.
            branch_head = None
        default = await self._call("GET", f"/repos/{self.repo}/git/ref/heads/{self.assignment.default_branch}")
        return {
            "repository_id": repository["id"],
            "repository": self.repo,
            "default_branch": self.assignment.default_branch,
            "default_branch_head": default["object"]["sha"],
            "branch": self.assignment.branch,
            "branch_head": branch_head,
            # The assigned branch's own pull request, if it has one. Reported here
            # because worker startup has to answer two questions before it can do
            # anything — "did a previous run already land this work?" and "is there
            # an open PR whose review state I must not reset?" — and it used to
            # answer both with `gh pr list` under a broad token (#5223). Only this
            # assignment's PR is described: `_assigned_pull_requests` filters on the
            # immutable head repository id, so a fork's identically named branch
            # cannot present itself as this run's prior work.
            "pull_request": await self._assigned_pull_request(),
        }

    async def _assigned_pull_request(self) -> dict | None:
        """This assignment's own pull request, open or merged, or `None`.

        Ownership is decided exactly as :meth:`_require_assigned_pull_request`
        decides it, and for the same reason: a branch NAME is not ours to control,
        so a PR is only ours when its head repository's immutable numeric id is the
        assigned one. A fork pushing `agent/issue-<n>` must not be able to convince
        startup that this issue is already complete — that would skip the run.

        Open is preferred over merged when both exist, because the caller's question
        is "what should I extend now", and a currently open PR is the answer.
        """
        owner = self.repo.split("/")[0]
        candidates = await self._call("GET", f"/repos/{self.repo}/pulls?head={owner}:{self.assignment.branch}&state=all&per_page=20")
        best = None
        for pull in candidates or []:
            head = pull.get("head") or {}
            head_repository_id = (head.get("repo") or {}).get("id")
            if isinstance(head_repository_id, bool) or not isinstance(head_repository_id, int):
                continue
            if head_repository_id != self.assignment.repository_id:
                continue
            if head.get("ref") != self.assignment.branch or (pull.get("base") or {}).get("ref") != self.assignment.default_branch:
                continue
            described = {
                "number": pull.get("number"),
                "html_url": pull.get("html_url"),
                "state": pull.get("state"),
                "merged": bool(pull.get("merged_at")),
            }
            if described["state"] == "open":
                return described
            if best is None or described["merged"]:
                best = described
        return best

    async def fetch_repository_archive(self, *, ref: str | None = None, offset: int = 0, length: int | None = None) -> ArchiveSlice:
        """One window of a tarball of one ref of the assigned repository (#5223).

        This exists because a metadata read is not a clone. Startup used to clone
        with the run's installation token, which is exactly the broad, hour-long,
        merge-capable credential mediation removes — so a policy-bearing run could
        not start at all. Transferring the content through an authorized read means
        the worker gets code without getting a credential.

        Returns a *slice*, not the whole archive, because the deployed edge is a
        REST API Gateway with a hard 10 MB response limit: this repository's own
        tarball is ~12.1 MiB, or ~16.9 MB once base64'd for JSON, so a
        whole-archive response would simply not be deliverable. The caller requests
        successive windows and reassembles them.

        What makes reassembly safe is `digest` and `total_bytes`, which describe the
        WHOLE archive and are returned with every slice. Each slice request
        re-fetches the archive from the provider (the gateway holds no cross-request
        state), so a repository that receives a push between slices would otherwise
        yield a work tree spliced from two different archives. The caller compares
        the digest across slices and verifies it over the reassembled bytes, so that
        case is detected and refused rather than silently materialized. `commit_sha`
        alone is NOT sufficient for this: `git archive` output for the same commit is
        not byte-identical across fetches (gzip metadata), which is exactly why the
        digest is authoritative and the caller must restart on a mismatch.

        `ref` is an assertion checked against the assignment, never a free
        selection: it may only name the assigned working branch or the assigned
        default branch. Anything else is refused, so this cannot be used to read a
        ref the assignment does not cover.

        Repository identity is re-established before the transfer, on the immutable
        numeric id, for the same reason `read_repository` does it: a rename followed
        by a squatter taking the old name must not resolve as the assignment's
        repository.

        Args:
            ref: Assigned working branch or assigned default branch only.
            offset: First byte of the archive to return. Must be within the archive.
            length: How many bytes to return, capped at :data:`ARCHIVE_SLICE_BYTES`.

        Returns:
            An :class:`ArchiveSlice` carrying this window plus whole-archive
            identity (`commit_sha`, `total_bytes`, `digest`).

        Raises:
            OperationRefusedError: `ref` is not one this assignment covers, the
                repository identity does not match, `offset` is not within the
                archive, or the archive exceeds :data:`MAX_ARCHIVE_BYTES`.
            ProviderUnavailableError: the provider did not produce the archive.
        """
        assert self._client is not None, "GitHubProvider must be used as an async context manager"
        target = ref or self.assignment.branch
        if target not in (self.assignment.branch, self.assignment.default_branch):
            raise OperationRefusedError("the requested ref is not covered by this assignment")

        state = await self.read_repository()
        # Resolve to a commit before downloading, so the bytes are attributable to a
        # specific commit. A branch that does not exist yet falls back to the default
        # branch: a first run legitimately has no working branch to materialize.
        resolved = state["branch_head"] if target == self.assignment.branch else state["default_branch_head"]
        if resolved is None:
            resolved = state["default_branch_head"]
        if not isinstance(resolved, str) or len(resolved) != 40:
            raise OperationRefusedError("the archive ref could not be resolved to a commit")

        if offset < 0:
            raise OperationRefusedError("the archive offset is not within the archive")
        window = ARCHIVE_SLICE_BYTES if length is None else min(length, ARCHIVE_SLICE_BYTES)
        if window <= 0:
            raise OperationRefusedError("the archive slice length is not usable")

        try:
            # Streamed and counted as it arrives. `follow_redirects=False` is kept
            # from the client: GitHub answers this with a 302 to a signed codeload
            # URL, which is fetched explicitly below rather than followed blindly,
            # so the Authorization header is never replayed to another host.
            response = await self._client.request("GET", f"/repos/{self.repo}/tarball/{resolved}", timeout=_ARCHIVE_TIMEOUT)
            location = response.headers.get("location", "")
            if response.status_code in (301, 302, 307) and location:
                parsed = httpx.URL(location)
                if parsed.scheme != "https" or not parsed.host.endswith(".github.com"):
                    # An off-provider redirect target is refused, not fetched: the
                    # gateway would otherwise be a fetcher for an arbitrary URL.
                    raise OperationRefusedError("the archive redirect target is not a provider host")
                # No Authorization header: the codeload URL is pre-signed, and
                # sending the installation token to it would leak the credential to
                # a host that does not need it.
                async with httpx.AsyncClient(timeout=_ARCHIVE_TIMEOUT, follow_redirects=False, trust_env=False) as fetch:
                    async with fetch.stream("GET", location) as streamed:
                        if streamed.status_code != 200:
                            raise ProviderUnavailableError("the provider did not serve the repository archive")
                        consumed = await self._consume_archive(streamed, offset=offset, window=window)
                        return _archive_slice(consumed, commit_sha=resolved, offset=offset)
            if response.status_code == 200:
                body = response.content
                if len(body) > MAX_ARCHIVE_BYTES:
                    raise OperationRefusedError("the repository archive exceeds the mediated transfer limit")
                consumed = (body[offset : offset + window], len(body), hashlib.sha256(body).hexdigest())
                return _archive_slice(consumed, commit_sha=resolved, offset=offset)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderUnavailableError("provider did not serve the repository archive") from exc
        if response.status_code == 404:
            raise ProviderNotFoundError("provider resource was not found")
        if response.status_code >= 500 or response.status_code == 429:
            raise ProviderUnavailableError("provider is unavailable for the repository archive")
        raise OperationRefusedError("provider refused the repository archive")

    @staticmethod
    async def _consume_archive(streamed, *, offset: int, window: int) -> tuple[bytes, int, str]:
        """Stream the archive, keeping only `window` bytes from `offset`.

        The whole archive is hashed and counted as it passes, but only the requested
        slice is retained — so gateway memory for a slice request is the slice, not
        the archive. The digest covers the WHOLE archive because that is what the
        caller needs to prove its reassembly is of one consistent snapshot.
        """
        wanted_end = offset + window
        collected, total, hasher = bytearray(), 0, hashlib.sha256()
        async for chunk in streamed.aiter_bytes():
            start = total
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                # Refused mid-stream rather than after buffering it all: the point
                # of the bound is to not hold it.
                raise OperationRefusedError("the repository archive exceeds the mediated transfer limit")
            hasher.update(chunk)
            # Retain only the overlap with the requested window.
            if start < wanted_end and total > offset:
                collected += chunk[max(0, offset - start) : max(0, wanted_end - start)]
        return bytes(collected), total, hasher.hexdigest()

    # --- Publish a commit -------------------------------------------------

    # --- Publish a commit -------------------------------------------------

    async def publish_commit(self, *, changes: list[FileChange], message: str, expected_head: str | None, reauthorize) -> PublishedCommit:
        """Publish `changes` to the assigned branch as one commit.

        The sequence is blobs → tree → commit → ref update. `reauthorize` is
        awaited before each step that has an effect, so authority withdrawn
        part-way through stops the rest.

        `expected_head` is the branch head the worker built on. Passing it makes
        the ref update conditional: if the branch moved, this raises
        `ProviderConflictError` rather than overwriting. Passing None is only
        valid when creating the branch.
        """
        if not changes:
            raise OperationRefusedError("a commit must carry at least one change")
        if len(changes) > MAX_FILES_PER_COMMIT:
            raise OperationRefusedError("proposed change touches too many files for one mediated commit")
        # Aggregate size, checked alongside the file count for the same reason: a
        # permitted number of individually permitted files can still exceed what the
        # deployed edge will carry, and this is the last place that can say so with a
        # useful message rather than as a transport failure.
        total_content = sum(len(change.content) for change in changes if change.content is not None)
        if total_content > MAX_COMMIT_CONTENT_BYTES:
            raise OperationRefusedError("proposed change exceeds the permitted total size for one mediated commit")
        if not message.strip():
            raise OperationRefusedError("a commit must carry a message")

        await reauthorize()
        state = await self.read_repository()
        head = state["branch_head"]
        if head is not None and expected_head is None:
            # An existing branch REQUIRES the head the change was prepared against.
            # Treating a missing expectation as "no expectation" is what made this
            # a lost-update: the ref update below would fast-forward over a commit
            # the caller never saw, and the caller would be told it succeeded.
            # Absent evidence of what was built on, refuse rather than guess.
            raise ProviderConflictError("publishing to an existing branch requires the head the change was prepared against")
        if expected_head is not None and head != expected_head:
            # Someone else advanced the branch. Surfaced, never resolved here:
            # silently rebasing would publish a combination nothing validated.
            raise ProviderConflictError("the assigned branch moved since this change was prepared")
        base_sha = head or state["default_branch_head"]
        if head is None:
            # A new branch must start from the assigned repository's default
            # branch, so a commit cannot be parented on unrelated history.
            await self._require_ancestry(base_sha, state["default_branch_head"])
        base_commit = await self._call("GET", f"/repos/{self.repo}/git/commits/{base_sha}")

        tree_entries: list[dict] = []
        for change in changes:
            if change.deleted:
                # A null sha is how the tree API expresses removal.
                tree_entries.append({"path": change.path, "mode": change.mode, "type": "blob", "sha": None})
                continue
            await reauthorize()
            blob = await self._call(
                "POST",
                f"/repos/{self.repo}/git/blobs",
                json={"content": base64.b64encode(change.content or b"").decode(), "encoding": "base64"},
                expect=(201,),
            )
            tree_entries.append({"path": change.path, "mode": change.mode, "type": "blob", "sha": blob["sha"]})

        # The upload above can take a while. Re-authorize before the steps that
        # make it visible, because the window since the last check is exactly
        # where a revocation would otherwise be ignored.
        await reauthorize()
        tree = await self._call(
            "POST",
            f"/repos/{self.repo}/git/trees",
            json={"base_tree": base_commit["tree"]["sha"], "tree": tree_entries},
            expect=(201,),
        )
        # From here on the tree exists at the provider, so a failure is genuinely
        # ambiguous and reconciliation needs to be able to identify OUR commit.
        # Re-raise with the prepared objects attached rather than letting a bare
        # timeout reach a caller that would have to guess.
        try:
            await reauthorize()
            commit = await self._call(
                "POST",
                f"/repos/{self.repo}/git/commits",
                json={"message": message, "tree": tree["sha"], "parents": [base_sha]},
                expect=(201,),
            )
            await reauthorize()
            await self._update_ref(commit["sha"], expected_old=base_sha, create=head is None)
        except ProviderUnavailableError as exc:
            raise ProviderUnavailableError(
                str(exc),
                prepared_parent=base_sha,
                prepared_tree=tree["sha"],
            ) from exc
        return PublishedCommit(sha=commit["sha"], branch=self.assignment.branch, parent_sha=base_sha)

    async def _update_ref(self, sha: str, *, expected_old: str, create: bool) -> None:
        """Advance the assigned branch without forcing and without rewriting.

        `force: false` is what makes this a fast-forward-only update. GitHub
        rejects a non-fast-forward with 422, which surfaces as a conflict. There
        is no code path here that sets force true, deletes a ref, or touches a
        ref other than the assigned branch.
        """
        ref = f"refs/heads/{self.assignment.branch}"
        if self.assignment.branch == self.assignment.default_branch:
            # Unreachable via OperationAssignment, which refuses this at
            # construction. Repeated at the call boundary because this is the
            # function that would actually do it.
            raise OperationRefusedError("the mediated path does not write the default branch")
        if create:
            await self._call("POST", f"/repos/{self.repo}/git/refs", json={"ref": ref, "sha": sha}, expect=(201,))
            return
        await self._call(
            "PATCH",
            f"/repos/{self.repo}/git/{ref}",
            json={"sha": sha, "force": False},
        )

    async def _require_ancestry(self, base_sha: str, default_head: str) -> None:
        """Refuse a base that is not reachable from the assigned repository.

        Without this, a commit could be parented on an object from a fork or an
        unrelated branch, producing a PR whose diff bears no relation to the
        reviewed base.
        """
        # `compare` 404s when either end is not in this repository, which `_call`
        # turns into a refusal. When both ends ARE present, the base must be
        # reachable from the default branch: "identical" or "behind" mean the
        # default branch already contains it. "ahead" and "diverged" mean the base
        # carries commits the default branch does not, which is precisely the
        # unrelated-history case this exists to refuse.
        comparison = await self._call("GET", f"/repos/{self.repo}/compare/{default_head}...{base_sha}")
        if comparison.get("status") not in {"identical", "behind"}:
            raise OperationRefusedError("the proposed base is not part of the assigned repository history")

    # --- Pull request -----------------------------------------------------

    async def upsert_pull_request(self, *, title: str, body: str, reauthorize) -> dict:
        """Create or update the PR for the assigned branch.

        Always `head = assignment.branch` and `base = assignment.default_branch`.
        Neither is a parameter, so this cannot open a PR from or into a branch
        the assignment does not cover. An existing PR is updated rather than
        duplicated.
        """
        if not title.strip():
            raise OperationRefusedError("a pull request must carry a title")
        await reauthorize()
        # Repository identity re-established on the immutable numeric id before any
        # effect, exactly as `publish_commit` and `fetch_repository_archive` do. The
        # query below is keyed on the repository NAME, and a rename followed by a
        # squatter taking the old name would otherwise resolve here as the
        # assignment's repository.
        await self.read_repository()
        owner = self.repo.split("/")[0]
        existing = await self._call("GET", f"/repos/{self.repo}/pulls?head={owner}:{self.assignment.branch}&state=open")
        if existing:
            # The listing filtered on a branch NAME, which is not ownership: a fork
            # can carry a branch of the same name. Verify the PR we are about to
            # mutate is the assignment's own — head repository id, head ref and base
            # ref — before the PATCH, not after. Same check `publish_review` and
            # `merge_pull_request` already apply before their effects.
            self._require_assigned_pull_request(existing[0])
            number = existing[0]["number"]
            await reauthorize()
            updated = await self._call("PATCH", f"/repos/{self.repo}/pulls/{number}", json={"title": title, "body": body})
            return {"number": updated["number"], "html_url": updated["html_url"], "created": False}
        await reauthorize()
        created = await self._call(
            "POST",
            f"/repos/{self.repo}/pulls",
            json={
                "title": title,
                "body": body,
                "head": self.assignment.branch,
                "base": self.assignment.default_branch,
            },
            expect=(201,),
        )
        return {"number": created["number"], "html_url": created["html_url"], "created": True}

    # --- Review / comment -------------------------------------------------

    async def publish_review(self, *, pull_number: int, body: str, event: str, reauthorize) -> dict:
        """Publish a review or comment on a pull request.

        `APPROVE` is refused when the App that would submit it is also the PR's
        author. Approving one's own work is not review, and the merge gate's
        value depends on the approval coming from somewhere else. `COMMENT` and
        `REQUEST_CHANGES` stay available to a PR author, because saying something
        about your own PR is not an authorization event.
        """
        if event not in {"COMMENT", "APPROVE", "REQUEST_CHANGES"}:
            raise OperationRefusedError("unsupported review event")
        if not body.strip():
            raise OperationRefusedError("a review must carry a body")
        await reauthorize()
        pull = await self._call("GET", f"/repos/{self.repo}/pulls/{pull_number}")
        self._require_assigned_pull_request(pull)
        if event == "APPROVE" and await self._is_own_pull_request(pull):
            raise OperationRefusedError("the author of a pull request cannot approve it")
        await reauthorize()
        review = await self._call(
            "POST",
            f"/repos/{self.repo}/pulls/{pull_number}/reviews",
            json={"body": body, "event": event},
            expect=(200, 201),
        )
        return {"id": review.get("id"), "state": review.get("state")}

    def _require_assigned_pull_request(self, pull: dict) -> None:
        """Refuse a pull request that is not this assignment's own.

        `pull_number` is the one provider-object identifier a caller supplies, and
        on its own it selects ANY pull request in the repository — including one
        opened by a human, by another tenant's run against a shared repository, or
        against a protected branch. Validating it after the fetch is what makes it
        an assertion like `repository` and `branch` rather than an input.

        Ownership is decided on the fields the assignment derives: the head ref must
        be our working branch, the head REPOSITORY must be the assigned repository,
        and the base must be the assignment's default branch. All three are checked:

        * Head ref alone would accept a PR that targets somewhere we were never
          authorized to affect, and a review is an authorization event on the
          *merge* it argues for.
        * Head ref plus base ref is still not ownership, because a branch NAME is
          not ours to control. Anyone who can fork this repository can push
          `agent/issue-<n>` to their own fork and open a PR into `main`; that PR's
          `head.ref` and `base.ref` are indistinguishable from ours. Deciding
          ownership on a string a third party chooses would let an outsider's
          branch collect this assignment's review — and, under an accepted
          `Action.MERGE`, its merge. The head repository's immutable numeric id is
          the field they cannot forge, so it is what actually establishes that the
          commits under review are the ones we published.

        A pull request whose refs or head repository we cannot read is refused:
        unestablished ownership is not ownership.
        """
        if isinstance(self.assignment, BoundMergeAssignment) and (
            pull.get("number") != self.assignment.pr_number or pull.get("node_id") != self.assignment.pr_node_id
        ):
            raise OperationRefusedError("the pull request identity changed")
        head = pull.get("head") or {}
        head_ref = head.get("ref")
        base_ref = (pull.get("base") or {}).get("ref")
        if not head_ref or not base_ref:
            raise OperationRefusedError("the pull request's refs could not be established")
        head_repository_id = (head.get("repo") or {}).get("id")
        # Booleans are excluded explicitly: `isinstance(True, int)` is True in
        # Python, so a payload carrying `id: true` must not compare as id 1.
        if isinstance(head_repository_id, bool) or not isinstance(head_repository_id, int):
            raise OperationRefusedError("the pull request's head repository could not be established")
        if head_repository_id != self.assignment.repository_id:
            raise OperationRefusedError("the named pull request is not this assignment's pull request")
        if head_ref != self.assignment.branch or base_ref != self.assignment.default_branch:
            raise OperationRefusedError("the named pull request is not this assignment's pull request")

    async def _is_own_pull_request(self, pull: dict) -> bool:
        """Whether approving `pull` would be approving this platform's own work.

        Decided on the head ref rather than on an actor identity. Every PR this
        path opens has `head = assignment.branch` (see `upsert_pull_request`), so
        a PR on the assigned branch is by construction one we authored. That is a
        stricter test than comparing actor IDs and it does not depend on the
        provider telling us who we are — an identity lookup that failed open
        would turn self-approval back on.

        A missing or unreadable head ref counts as self-authored: when we cannot
        establish that the work is someone else's, we do not approve it.

        Consequence worth stating plainly: `_require_assigned_pull_request` already
        demands `head == assignment.branch`, so every pull request that reaches here
        is self-authored by this test, and **APPROVE is therefore unreachable through
        mediation**. That is the intended composition, not an oversight — the two
        requirements are "a caller cannot review an unrelated pull request" and "an
        author cannot approve its own", and mediated runs only ever hold assignments
        for their own derived branch. COMMENT and REQUEST_CHANGES remain available,
        which is what a reviewer assignment actually needs. If a future assignment
        kind legitimately reviews someone else's branch, it needs its own derived
        ownership rule here rather than a relaxation of either check.
        """
        head_ref = (pull.get("head") or {}).get("ref")
        return not head_ref or head_ref == self.assignment.branch

    # --- Merge (separately authorized) -----------------------------------

    async def merge_pull_request(self, *, pull_number: int, expected_head: str, reauthorize, method: str = "squash") -> dict:
        """Merge a pull request. Reachable only via `GitHubOperation.MERGE_PULL_REQUEST`.

        The service authorizes this operation against a current `Action.MERGE`
        before calling, so a develop/repair assignment never arrives here.
        `expected_head` is passed to the provider as `sha`, so a branch that
        moved after the decision to merge produces a conflict rather than merging
        content nobody approved.
        """
        if method not in {"merge", "squash", "rebase"}:
            raise OperationRefusedError("unsupported merge method")
        # Same reasoning as the review path, and it matters more here: an
        # unvalidated number would let a merge authorization for this assignment
        # merge somebody else's pull request.
        pull = await self._call("GET", f"/repos/{self.repo}/pulls/{pull_number}")
        self._require_assigned_pull_request(pull)
        # One full live check, after the read and immediately before mutation.
        # Credential retries come through this boundary again.
        await reauthorize()
        merged = await self._call(
            "PUT",
            f"/repos/{self.repo}/pulls/{pull_number}/merge",
            json={"sha": expected_head, "merge_method": method},
        )
        return {"merged": bool(merged.get("merged")), "sha": merged.get("sha")}

    async def enqueue_pull_request(self, *, pull_number: int, expected_head: str, operation_key: str, reauthorize) -> dict:
        """Ordinary queue admission under the same separate MERGE authorization."""
        await reauthorize()
        pull = await self._call("GET", f"/repos/{self.repo}/pulls/{pull_number}")
        self._require_assigned_pull_request(pull)
        if (pull.get("head") or {}).get("sha") != expected_head or not pull.get("node_id"):
            raise ProviderConflictError("the reviewed pull request head changed")
        await reauthorize()
        result = await self._call(
            "POST",
            "/graphql",
            json={
                "query": "mutation($input:EnqueuePullRequestInput!) { enqueuePullRequest(input:$input) { mergeQueueEntry { id } } }",
                "variables": {"input": {"pullRequestId": pull["node_id"], "expectedHeadOid": expected_head, "clientMutationId": operation_key}},
            },
        )
        if not isinstance(result, dict) or result.get("errors"):
            # A GraphQL error may accompany a committed effect. Reconcile it.
            raise ProviderUnavailableError("queue admission outcome is unknown")
        entry = ((result.get("data") or {}).get("enqueuePullRequest") or {}).get("mergeQueueEntry") or {}
        if not isinstance(entry.get("id"), str) or not entry["id"]:
            raise ProviderUnavailableError("queue admission receipt is missing")
        return {"queue_entry_id": entry["id"]}


async def reconcile_commit(
    provider: GitHubProvider,
    *,
    message: str,
    expected_parent: str | None = None,
    expected_tree: str | None = None,
) -> PublishedCommit | None:
    """After a timeout, find out whether *our* commit actually landed.

    A provider timeout is not evidence that nothing happened. Retrying blindly
    would publish the same change twice; this looks the intended object up first
    and returns it if it is already there, so the retry becomes a no-op.

    Identity is established by **parent and tree**, not by the commit message. A
    message is not an identifier: two runs of the same assignment, a human commit
    that copied the text, or a retry of a *different* change under a reused message
    all produce an equal string. Adopting one of those as our own effect reports a
    commit we did not publish as success and abandons the change we were actually
    asked to publish — the retry then never happens.

    `expected_tree`/`expected_parent` are the objects we constructed before the call
    that timed out. Both known and both matching is proof. When the caller cannot
    supply them the outcome is genuinely **unknown**, and this returns None so the
    error surfaces rather than being resolved by a guess.
    """
    if expected_parent is None or expected_tree is None:
        # No verifiable identity, so no claim either way. `None` here means
        # "unreconciled", and the caller re-raises the provider error.
        return None
    state = await provider.read_repository()
    head = state["branch_head"]
    if head is None:
        return None
    commit = await provider._call("GET", f"/repos/{provider.repo}/git/commits/{head}")
    parents = [parent.get("sha") for parent in (commit.get("parents") or [])]
    tree_sha = (commit.get("tree") or {}).get("sha")
    if tree_sha == expected_tree and parents == [expected_parent] and commit.get("message") == message:
        return PublishedCommit(sha=head, branch=provider.assignment.branch, parent_sha=expected_parent)
    return None


async def reconcile_pull_request(provider: GitHubProvider, *, title: str, body: str) -> dict | None:
    """After a timeout, find out whether *our* PR update actually landed.

    Same discipline as :func:`reconcile_commit`, and for the same reason: a timeout
    is not evidence about what happened, so reconciliation has to establish the
    intended effect rather than the mere existence of an object.

    The existence of an open PR on the assigned branch is NOT that evidence.
    `upsert_pull_request` PATCHes an already-open PR, so on that path a PR is
    already there before the call and is still there after it times out. Returning
    it would report the requested title/body as published when the PATCH may never
    have been applied — an uncertain outcome resolved as success, and the retry that
    would have applied it never happens. That is the one wrong answer here.

    So identity is the requested `title` and `body`: both matching means this
    update is present, whether the timed-out call created or updated the PR. A
    mismatch — including a PR still carrying its previous title — is genuinely
    **unknown**, and this returns None so the provider error surfaces as 503.

    The head repository id is re-checked because the query filters on a branch
    NAME; see `_require_assigned_pull_request` for why a name is not ownership.
    """
    owner = provider.repo.split("/")[0]
    existing = await provider._call("GET", f"/repos/{provider.repo}/pulls?head={owner}:{provider.assignment.branch}&state=open")
    if not existing:
        return None
    pull = existing[0]
    head_repository_id = ((pull.get("head") or {}).get("repo") or {}).get("id")
    if isinstance(head_repository_id, bool) or head_repository_id != provider.assignment.repository_id:
        return None
    if pull.get("title") != title or (pull.get("body") or "") != (body or ""):
        # The intended update is not observable, so we make no claim about it.
        return None
    return {"number": pull["number"], "html_url": pull["html_url"], "created": False}


OPERATION_HANDLERS: dict[GitHubOperation, str] = {
    GitHubOperation.READ_REPOSITORY: "read_repository",
    GitHubOperation.FETCH_REPOSITORY_ARCHIVE: "fetch_repository_archive",
    GitHubOperation.PUBLISH_COMMIT: "publish_commit",
    GitHubOperation.UPSERT_PULL_REQUEST: "upsert_pull_request",
    GitHubOperation.PUBLISH_REVIEW: "publish_review",
    GitHubOperation.MERGE_PULL_REQUEST: "merge_pull_request",
}


__all__ = [
    "ALLOWED_FILE_MODES",
    "ARCHIVE_SLICE_BYTES",
    "MAX_ARCHIVE_BYTES",
    "MAX_BLOB_BYTES",
    "MAX_COMMIT_CONTENT_BYTES",
    "MAX_FILES_PER_COMMIT",
    "OPERATION_HANDLERS",
    "ArchiveSlice",
    "FileChange",
    "GitHubProvider",
    "ProviderConflictError",
    "ProviderUnavailableError",
    "PublishedCommit",
    "reconcile_commit",
    "reconcile_pull_request",
]
