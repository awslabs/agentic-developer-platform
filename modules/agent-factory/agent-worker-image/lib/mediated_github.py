"""Publish local git work through the gateway's mediated operations (#5223).

The worker side of the mediated path. Policy-bearing develop/repair work used to
be refused outright, because a GitHub `contents: write` token also authorizes
`PUT /repos/{o}/{r}/pulls/{n}/merge` — so no token can express "may push this
branch, may not merge it", and the work was blocked rather than the gate weakened.
This module asks the gateway to perform a *typed operation* instead of asking it
for a token.

What that changes for the worker:

- **No `contents: write` token is ever held here.** There is no token to leak, no
  token to reuse after the grant ends, and no merge capability riding along with
  the push capability.
- **The worker cannot name the target.** Repository, branch, installation and
  deadline are derived by the gateway from protected records. This module sends
  the repository and branch it *believes* it is on purely so a mismatch is
  refused rather than silently written somewhere else.
- **Merge is not reachable from here.** There is no merge function in this module.
  That absence is deliberate: the merge operation exists on the gateway and
  requires separately accepted authority, so a develop or repair run cannot reach
  it even by constructing its own request.

## Publishing real local changes

`collect_changes` reads what git actually recorded — including binary files,
deletions, and file-mode changes such as a script becoming executable or a path
becoming a symlink. It uses `git diff --raw -z` rather than a textual diff,
because a textual patch cannot represent a binary file and silently loses mode
transitions.

Content is sent base64-encoded so bytes that are not valid UTF-8 survive
transport; the gateway decodes and publishes them through the GitHub tree/commit/
ref APIs with `force: false` and an expected old head.

## Conflicts and timeouts

A 409 is a `MediatedConflict`, which is a *visible* outcome the caller is expected
to reconcile (fetch, rebase, retry) — not a failure to swallow. A 503 or a network
timeout is a `MediatedUnavailable`, and the gateway has already reconciled by
looking up whether the intended commit landed before reporting it, so a retry
cannot publish the same change twice.

## What this module deliberately does not do

There is no fallback. If mediation is unavailable, `publish_commit` raises. It
must never quietly reach for a long-lived installation token instead: that
fallback would restore exactly the broad, hour-long, merge-capable credential the
mediated path exists to avoid, and it would do so precisely when authorization was
in doubt.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

logger = logging.getLogger(__name__)

MEDIATED_OPERATION_PATH = "/internal/v1/agent/self/github-operation"

# Kept at or below the gateway's own bounds so an oversized change is reported
# here, before an upload starts, rather than refused half-way through one. These
# mirror `src/agentauth/github_provider.py`; both are set by the deployed edge's
# hard 10 MB payload quota, which applies to a publish REQUEST just as it does to
# an archive response (base64 costs 4/3). Refusing here is what turns "the edge
# dropped your request" into a message naming the file and the limit.
MAX_BLOB_BYTES = 5 * 1024 * 1024
# Summed decoded content for one publish. The per-file cap cannot express this:
# `MAX_FILES_PER_COMMIT` files each just inside it still exceed the edge.
MAX_COMMIT_CONTENT_BYTES = 6 * 1024 * 1024
MAX_FILES_PER_COMMIT = 500
# REST API Gateway's request quota is 10,000,000 bytes. Content bounds alone
# cannot prove the JSON fits: permitted paths and a permitted message can add
# several megabytes after JSON escaping (one non-BMP character becomes a 12-byte
# surrogate pair). Keep margin for the edge and refuse the serialized body before
# signing or connecting, so the caller sees its actual size instead of an opaque
# edge-level rejection.
MAX_REQUEST_BYTES = 9 * 1024 * 1024
_MAX_RESPONSE_BYTES = 1 * 1024 * 1024
_TIMEOUT = 60

# The archive operation's own bounds. One response carries one SLICE, not the whole
# archive, because the deployed edge is a REST API Gateway with a hard 10 MB response
# limit; see `_fetch_archive`. This ceiling is per response and sits above the
# gateway's slice size after base64 (4/3) plus envelope, so a slice the gateway was
# willing to send is not then refused here for being exactly as large as we asked for.
_MAX_ARCHIVE_RESPONSE_BYTES = 12 * 1024 * 1024
# Total reassembled archive accepted, matching the gateway's own `MAX_ARCHIVE_BYTES`.
_MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
# Slice ceiling for the loop bound: enough responses to carry the largest archive at
# the smallest slice the gateway is expected to serve, plus headroom for a short
# final window. Bounds the loop so a gateway that stops advancing fails instead of
# spinning.
_MAX_ARCHIVE_SLICES = 64
# Fields describing the transfer itself rather than the repository. Stripped from the
# envelope returned to callers so a slice's bookkeeping is not mistaken for state.
_ARCHIVE_PAYLOAD_FIELDS = frozenset(
    {
        "archive_base64",
        "archive_offset",
        "archive_slice_bytes",
        "archive_complete",
    }
)
# The edge's integration timeout is 29 s, so a longer per-call budget only waits for
# a 504 the edge has already returned. Each slice is one call.
_ARCHIVE_TIMEOUT = 28

# Git's mode for a gitlink (submodule pointer). Excluded rather than translated: a
# submodule imports code that nothing reviewing this change has seen.
_GITLINK_MODE = "160000"
_SUPPORTED_MODES = frozenset({"100644", "100755", "120000"})


class MediatedError(Exception):
    """Mediation could not be performed. Never fall back to a broader credential."""


class MediatedConflict(MediatedError):
    """The assigned branch or pull request moved. Reconcile and retry."""


class MediatedUnavailable(MediatedError):
    """The gateway or provider was unreachable. Retrying is reasonable."""


class MediatedRefused(MediatedError):
    """Authorization was refused. Retrying without a change is not reasonable."""


@dataclass(frozen=True)
class LocalChange:
    """One file's contribution to a commit, as git recorded it."""

    path: str
    content: bytes | None = None
    mode: str = "100644"
    deleted: bool = False

    def payload(self) -> dict:
        return {
            "path": self.path,
            "content_base64": None if self.content is None else base64.b64encode(self.content).decode("ascii"),
            "mode": self.mode,
            "deleted": self.deleted,
        }


@dataclass(frozen=True)
class PublishedCommit:
    sha: str
    branch: str
    parent_sha: str


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _identity_headers() -> dict[str, str]:
    """Both proofs, read from the pod's own projected files.

    Same shape as `gateway_credential_client._worker_identity_headers`: the run
    credential says which invocation and attempt, the workload token says which
    pod. A missing or malformed either is an error rather than an unauthenticated
    request that would be refused less informatively at the gateway.
    """
    headers = {}
    for variable, header in (
        ("ADP_RUN_CREDENTIAL_FILE", "X-Adp-Run-Credential"),
        ("ADP_WORKLOAD_TOKEN_FILE", "X-Adp-Workload-Token"),
    ):
        try:
            fd = os.open(os.environ[variable], os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("not a file")
                raw = source.read(16387)
            token = raw.decode("ascii").rstrip("\r\n")
            if not token or len(token) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise ValueError("invalid token")
            headers[header] = token
        except (OSError, KeyError, ValueError):
            raise MediatedError("worker identity unavailable") from None
    return headers


def _sign(method: str, url: str, headers: dict, data: bytes) -> dict:
    import botocore.auth
    import botocore.awsrequest
    import botocore.session

    from adp_trigger.transport_identity import gateway_signing_region, worker_credentials

    session = botocore.session.get_session()
    credentials = worker_credentials(session)
    if credentials is None:
        raise MediatedError("no AWS credentials available for SigV4 signing")
    request = botocore.awsrequest.AWSRequest(method=method, url=url, headers=headers, data=data)
    signer = botocore.auth.SigV4Auth(credentials.get_frozen_credentials(), "execute-api", gateway_signing_region(url))
    signer.add_auth(request)
    return dict(request.headers)


def _request(payload: dict, *, timeout: int = _TIMEOUT, max_response_bytes: int = _MAX_RESPONSE_BYTES) -> dict:
    """One mediated call: SigV4 plus both worker proofs, no redirects.

    HTTPS and SigV4 are required rather than preferred. The two identity headers
    are bearer-shaped, and following a redirect or speaking plaintext would hand
    them to whatever answered.

    `max_response_bytes` is a parameter because one operation — the repository
    archive — legitimately returns megabytes while every other returns a small JSON
    object. The default stays small so a runaway response on the ordinary paths is
    still refused; the archive path raises it explicitly rather than removing it.
    """
    endpoint = os.environ.get("ADP_GATEWAY_ENDPOINT", "").rstrip("/")
    if not endpoint:
        raise MediatedError("mediated GitHub operations require ADP_GATEWAY_ENDPOINT")
    url = f"{endpoint}{MEDIATED_OPERATION_PATH}"
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise MediatedError("mediated GitHub operations require a clean HTTPS endpoint")

    # This is the exact wire serializer. Keep its ASCII and separator choices
    # explicit: changing either changes the byte count enforced below.
    data = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_REQUEST_BYTES:
        raise MediatedError(
            f"mediated request is {len(data)} bytes; the accepted limit is "
            f"{MAX_REQUEST_BYTES} bytes"
        )
    headers = {"Content-Type": "application/json", **_identity_headers()}
    headers = _sign("POST", url, headers, data)

    try:
        opener = build_opener(_NoRedirect()).open
        with opener(Request(url, data=data, headers=headers, method="POST"), timeout=timeout) as response:
            body = response.read(max_response_bytes + 1)
        if len(body) > max_response_bytes:
            raise MediatedError("mediated response exceeded the accepted size")
        return json.loads(body.decode("utf-8"))
    except HTTPError as exc:
        # The gateway's status codes carry the only distinction that matters here:
        # 409 means reconcile, 503 means retry, everything else means stop. The
        # body is deliberately not read or logged — it is not needed to decide,
        # and this path handles content that may be sensitive.
        if exc.code == 409:
            raise MediatedConflict("the assigned branch or pull request moved") from None
        if exc.code == 503:
            raise MediatedUnavailable("the mediated operation service is unavailable") from None
        raise MediatedRefused(f"mediated operation refused (HTTP {exc.code})") from None
    except URLError:
        raise MediatedUnavailable("cannot reach the mediated operation service") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise MediatedError("mediated operation returned invalid JSON") from None


def _git(repo: str, *args: str, binary: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise MediatedError(f"git {args[0]} failed in the work tree")
    return result.stdout if binary else result.stdout.decode("utf-8", "replace")


def collect_changes(repo: str, *, base: str = "HEAD") -> list[LocalChange]:
    """Read the work tree's changes as git recorded them.

    `git diff --raw -z` rather than a textual diff, deliberately. A patch cannot
    represent a binary file and drops mode transitions, so a run that made a script
    executable or added a PNG would publish something different from what it built
    and tested. The raw format gives the destination mode and status directly.

    Staged and unstaged changes plus untracked files are all included, because the
    agent's work is "what is in the tree", not "what it remembered to stage".
    """
    _git(repo, "add", "--all")
    raw = _git(repo, "diff", "--raw", "-z", "--no-renames", "--cached", base)
    changes: list[LocalChange] = []
    fields = [field for field in raw.split("\0") if field != ""]
    index = 0
    while index < len(fields):
        meta = fields[index]
        if not meta.startswith(":"):
            index += 1
            continue
        parts = meta[1:].split()
        if len(parts) < 5 or index + 1 >= len(fields):
            raise MediatedError("git reported an unreadable change record")
        destination_mode, status = parts[1], parts[4]
        path = fields[index + 1]
        index += 2

        if status.startswith("D") or destination_mode == "000000":
            changes.append(LocalChange(path=path, deleted=True))
            continue
        if destination_mode == _GITLINK_MODE:
            # Refused here rather than sent to be refused: the message is better,
            # and it costs nothing to say so before uploading anything.
            raise MediatedError(f"submodule changes cannot be published through mediation: {path}")
        if destination_mode not in _SUPPORTED_MODES:
            raise MediatedError(f"unsupported file mode {destination_mode} for {path}")
        content = _git(repo, "show", f":0:{path}", binary=True)
        if len(content) > MAX_BLOB_BYTES:
            raise MediatedError(f"{path} exceeds the mediated blob limit")
        changes.append(LocalChange(path=path, content=content, mode=destination_mode))

    if len(changes) > MAX_FILES_PER_COMMIT:
        raise MediatedError(f"{len(changes)} changed files exceeds the mediated per-commit limit")
    total_content = sum(len(change.content) for change in changes if change.content is not None)
    if total_content > MAX_COMMIT_CONTENT_BYTES:
        raise MediatedError(f"the proposed commit's {total_content} bytes of content exceeds the mediated per-commit total limit")
    return changes


def current_head(repo: str) -> str | None:
    """The commit the work tree is based on, for the expected-old-head check.

    Returned so the gateway can refuse a non-fast-forward instead of overwriting
    whatever arrived on the branch since this run cloned it. `None` when the branch
    does not exist remotely yet, which is a creation rather than an update.
    """
    try:
        head = _git(repo, "rev-parse", "HEAD").strip()
    except MediatedError:
        return None
    return head if len(head) == 40 else None


def _publication_state_path(repo: str) -> Path:
    """Where the publication receipt lives, under the git directory.

    Inside `.git` deliberately: it must survive between separate helper processes
    (the agent may publish several times) while never appearing as a work-tree file
    that `collect_changes` would pick up and publish.
    """
    path = Path(_git(repo, "rev-parse", "--git-path", "adp-mediated-publication.json").strip())
    return path if path.is_absolute() else Path(repo) / path


def _publication_base(repo: str) -> tuple[str | None, str, Path]:
    """Recover the confirmed remote head and local tree used for publication.

    Local commits and gateway commits have different IDs. After a publish, its
    receipt supplies the remote head while the index supplies the exact tree we
    sent. Keeping both under the git directory lets the next process publish only
    subsequent changes, even when the agent made local checkpoint commits.
    Before the first publish, bootstrap's upstream ref is the confirmed base.
    """
    path = _publication_state_path(repo)
    if path.exists():
        try:
            state = json.loads(path.read_text())
            head, tree = state["head"], state["tree"]
            if state.get("version") != 1 or not all(
                isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value)
                for value in (head, tree)
            ):
                raise ValueError("invalid publication state")
            return head, tree, path
        except (OSError, ValueError, KeyError, TypeError):
            raise MediatedError("cannot read the last confirmed publication base") from None
    try:
        base = _git(repo, "rev-parse", "--verify", "@{upstream}").strip()
    except MediatedError:
        base = current_head(repo)
    if base is None:
        raise MediatedError("cannot establish the work tree's publication base")
    return base, base, path


def _record_publication(path: Path, *, head: str, tree: str) -> None:
    """Record a receipt atomically; a failed provider call never advances it."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as target:
            temporary = target.name
            json.dump({"version": 1, "head": head, "tree": tree}, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    except OSError:
        raise MediatedError(
            f"commit {head} was published but its local receipt could not be saved"
        ) from None
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def publish_commit(
    *,
    repo: str,
    message: str,
    repository: str | None = None,
    branch: str | None = None,
    expected_head: str | None = None,
    changes: list[LocalChange] | None = None,
) -> PublishedCommit:
    """Publish the work tree's changes to the run's assigned branch.

    `repository` and `branch` are assertions, not instructions: the gateway derives
    both from protected records and refuses on mismatch. Passing them turns "this
    run's view of its assignment has drifted" into a refusal rather than a write to
    somewhere unintended.

    Raises:
        MediatedConflict: The branch moved. Reconcile and retry.
        MediatedUnavailable: Transient. The gateway has already checked whether the
            commit landed before reporting this, so a retry is safe.
        MediatedRefused: Authorization refused, or the content requires separately
            accepted authority (a workflow definition, for instance). Retrying
            unchanged will not help.
    """
    publication_path = None
    published_tree = None
    if changes is None:
        confirmed_head, base, publication_path = _publication_base(repo)
        collected = collect_changes(repo, base=base)
        # Capture the tree before the network call: edits made during that call
        # belong to the next checkpoint, not to this receipt.
        published_tree = _git(repo, "write-tree").strip()
    else:
        collected = changes
        confirmed_head = current_head(repo)
    if not collected:
        raise MediatedError("there are no local changes to publish")
    if not message.strip():
        raise MediatedError("a commit needs a message")

    payload = {
        "operation": "publish_commit",
        "message": message,
        "expected_head": expected_head if expected_head is not None else confirmed_head,
        "changes": [change.payload() for change in collected],
    }
    if repository:
        payload["repository"] = repository
    if branch:
        payload["branch"] = branch

    logger.info("Publishing %d changed file(s) through mediated GitHub operations", len(collected))
    result = _request(payload)
    sha = result.get("commit_sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise MediatedError("mediated commit returned no commit")
    if publication_path is not None and published_tree is not None:
        _record_publication(publication_path, head=sha, tree=published_tree)
    return PublishedCommit(
        sha=sha, branch=result.get("branch", ""), parent_sha=result.get("parent_sha", "")
    )


def upsert_pull_request(*, title: str, body: str, repository: str | None = None, branch: str | None = None) -> dict:
    """Open or update the pull request for the run's assigned branch.

    Head and base are not parameters. The gateway always uses the assigned branch
    and the repository's default branch, so this cannot open a PR between refs the
    run was not assigned.
    """
    payload = {"operation": "upsert_pull_request", "title": title, "body": body}
    if repository:
        payload["repository"] = repository
    if branch:
        payload["branch"] = branch
    return _request(payload).get("pull_request", {})


def publish_review(*, pull_number: int, body: str, event: str = "COMMENT") -> dict:
    """Publish the assigned review or comment on a pull request.

    `APPROVE` is accepted by the signature but refused by the gateway when the PR
    is on this run's own assigned branch — a PR's author approving it would forge
    the review a merge gate depends on. Reviewer runs approving someone else's work
    are the legitimate case.
    """
    return _request({"operation": "publish_review", "pull_number": int(pull_number), "body": body, "review_event": event}).get("review", {})


def read_repository(*, repository: str | None = None) -> dict:
    """Read the assigned repository through mediation.

    Present so a read does not need a token either. Reads must respect the same
    short authorization as writes; reaching for a long-lived token to clone with
    would reintroduce the credential this path removes.
    """
    payload = {"operation": "read_repository"}
    if repository:
        payload["repository"] = repository
    return _request(payload)


def _fetch_archive(*, ref: str | None, repository: str | None) -> tuple[bytes, dict]:
    """Reassemble the repository archive from mediated slices.

    The gateway cannot return the archive in one response: the deployed edge is a
    REST API Gateway with a hard 10 MB response limit, and base64 costs 4/3, so a
    real repository's tarball does not fit. Each response therefore carries one
    window plus the whole archive's `archive_total_bytes` and `archive_digest`.

    Every slice request re-fetches the archive gateway-side, so a push landing
    between slices would otherwise splice two different snapshots into one work
    tree. Two things prevent that: the digest must be identical on every slice, and
    the SHA-256 of the reassembled bytes must equal it. `commit_sha` is deliberately
    NOT used for this — `git archive` output is not byte-identical across fetches of
    the same commit, so equal SHAs would not prove equal bytes. A mismatch raises
    `MediatedConflict`, which callers already treat as "reconcile and retry".

    Returns:
        `(archive_bytes, last_result)` — the verified archive, and the final slice's
        envelope with the archive payload fields removed.
    """
    collected = bytearray()
    expected_total: int | None = None
    expected_digest: str | None = None
    result: dict = {}

    # Bounded rather than `while True`: with a minimum useful slice size, a
    # well-behaved gateway needs at most this many responses for the largest
    # archive it will send. Exceeding it means the gateway is not advancing, and
    # looping forever on a stuck offset is worse than failing.
    for _ in range(_MAX_ARCHIVE_SLICES):
        payload: dict = {"operation": "fetch_repository_archive", "archive_offset": len(collected)}
        if ref:
            payload["ref"] = ref
        if repository:
            payload["repository"] = repository

        result = _request(payload, timeout=_ARCHIVE_TIMEOUT, max_response_bytes=_MAX_ARCHIVE_RESPONSE_BYTES)

        remote_head = result.get("commit_sha")
        encoded = result.get("archive_base64")
        total = result.get("archive_total_bytes")
        digest = result.get("archive_digest")
        if (
            not isinstance(encoded, str)
            or not isinstance(remote_head, str)
            or not re.fullmatch(r"[0-9a-f]{40}", remote_head)
            or not isinstance(total, int)
            or isinstance(total, bool)
            or total < 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or result.get("archive_digest_algorithm") not in (None, "sha256")
        ):
            raise MediatedError("the mediated archive response is unusable")

        if expected_digest is None:
            expected_total, expected_digest = total, digest
        elif digest != expected_digest or total != expected_total:
            # The repository moved between slices. Restarting is the caller's
            # decision, not a silent retry that could loop against a busy branch.
            raise MediatedConflict("the repository archive changed while it was being transferred")

        try:
            chunk = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise MediatedError("the mediated archive is not decodable") from None

        if len(collected) + len(chunk) > _MAX_ARCHIVE_BYTES:
            raise MediatedError("the mediated archive exceeds the accepted size")
        collected += chunk

        if len(collected) >= expected_total:
            break
        if not chunk:
            # No progress and not finished: stop rather than spin on a stuck offset.
            raise MediatedError("the mediated archive transfer did not progress")
    else:
        raise MediatedError("the mediated archive required too many transfers")

    if len(collected) != expected_total:
        raise MediatedError("the mediated archive is incomplete")
    if hashlib.sha256(collected).hexdigest() != expected_digest:
        # Verified before a single byte is extracted: a spliced or corrupted archive
        # must not become a work tree the agent then builds and publishes from.
        raise MediatedError("the mediated archive failed its integrity check")

    envelope = {key: value for key, value in result.items() if key not in _ARCHIVE_PAYLOAD_FIELDS}
    return bytes(collected), envelope


def materialize_repository(
    destination: str,
    *,
    ref: str | None = None,
    repository: str | None = None,
    identity: tuple[str, str] | None = None,
) -> dict:
    """Create a work tree at `destination` from the assigned repository (#5223).

    The mediated replacement for `git clone`. Startup used to clone over HTTPS with
    the run's installation token, which is the broad, hour-long, merge-capable
    credential this whole path exists to withhold — so a run whose policy keeps
    merge human-only could not start. This asks the gateway for a bounded archive of
    one commit instead, and builds a git repository around it locally.

    What the result is, precisely: a real git repository with one commit containing
    the archive's tree, checked out, with `origin` configured and a local ref
    recording the true remote commit. It is deliberately NOT a clone — there is no
    history, and the local commit's SHA is not the provider's SHA, because a commit
    object built here cannot reproduce the original's committer metadata.

    That difference is why the confirmed remote head is written to the publication
    receipt: `publish_commit` needs the *provider's* head to send as
    `expected_head`, and the local SHA would be rejected as a stale expectation.
    Recording it here is what lets the first publication from a materialized tree
    fast-forward correctly instead of looking like a conflict.

    Args:
        destination: Directory to create. Must not already exist as a git tree.
        ref: Assigned working branch or default branch; the gateway refuses others.
        repository: Asserted `owner/name`, refused on mismatch.
        identity: `(name, email)` for the local commit, so the tree has an author.

    Returns:
        The gateway's archive result, plus `local_head` and `remote_head`.

    Raises:
        MediatedRefused: Authorization refused, or the ref is not covered.
        MediatedUnavailable: Transient; retrying is reasonable.
        MediatedError: The archive could not be turned into a work tree.
    """
    import tarfile

    logger.info("Materializing the work tree through mediated GitHub operations")
    archive, result = _fetch_archive(ref=ref, repository=repository)
    remote_head = result["commit_sha"]

    target = Path(destination)
    if (target / ".git").exists():
        raise MediatedError(f"{destination} already contains a git repository")
    target.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="adp-archive-") as staging:
        staging_path = Path(staging)
        tarball = staging_path / "repository.tar.gz"
        tarball.write_bytes(archive)
        extracted = staging_path / "extracted"
        extracted.mkdir()
        try:
            with tarfile.open(tarball, mode="r:gz") as bundle:
                # GitHub tarballs wrap everything in one `<owner>-<repo>-<sha>/`
                # directory. Members are validated rather than trusted: a tar entry
                # can name `../` or an absolute path, and extracting one would write
                # outside the work tree. `filter="data"` also drops device nodes and
                # refuses links that escape, which is why it is used instead of a
                # hand-rolled path check.
                bundle.extractall(extracted, filter="data")
        except (tarfile.TarError, OSError, ValueError) as exc:
            raise MediatedError(f"the mediated archive could not be extracted: {exc}") from None

        roots = [entry for entry in extracted.iterdir() if entry.is_dir()]
        source = roots[0] if len(roots) == 1 else extracted
        for entry in source.iterdir():
            shutil.move(str(entry), str(target / entry.name))

    branch = result.get("branch") or "main"
    name, email = identity or ("adp-agent[bot]", "adp-agent[bot]@users.noreply.github.com")
    repo = str(target)
    _git(repo, "init", "--initial-branch", branch)
    _git(repo, "config", "user.name", name)
    _git(repo, "config", "user.email", email)
    # `commit.gpgsign=false`: a signing config inherited from the image would make
    # this commit fail in a pod with no key, for a commit that is never published.
    _git(repo, "config", "commit.gpgsign", "false")
    if result.get("repository"):
        _git(repo, "remote", "add", "origin", f"https://github.com/{result['repository']}")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--allow-empty", "-m", f"Materialized {remote_head} through mediated operations")
    local_head = _git(repo, "rev-parse", "HEAD").strip()

    # The confirmed remote head, recorded so the first publication sends the
    # provider's head as `expected_head` rather than this local commit's SHA. The
    # tree is the local one, because that is what a subsequent `collect_changes`
    # diffs against to find the agent's actual work.
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    _record_publication(_publication_state_path(repo), head=remote_head, tree=tree)

    logger.info("Work tree materialized at %s from %s", destination, remote_head[:7])
    # `result` is already the transfer-stripped envelope from `_fetch_archive`.
    return {**result, "local_head": local_head, "remote_head": remote_head}


def enabled() -> bool:
    """Whether the mediated-operations FEATURE is switched on.

    Dormant unless explicitly switched on, so the required gateway cohort can be
    deployed before any worker depends on the endpoint existing. A run with the
    flag off keeps its existing behaviour untouched.

    This reports the deployment-wide flag, NOT whether the current run is mediated.
    The per-run decision also excludes PAT runs and runs without protected-worker
    authority, and it is made once in `entrypoint.main`. Anything deciding how a run
    behaves must use that decision; this predicate cannot see it.

    Accepts the same spellings as `entrypoint._mediated_github_enabled` ("1"/"true"/
    "yes"). It previously accepted only "true", so the two disagreed for a flag set
    to "1" — the deployment would be mediated while this helper reported it off.
    Two parsers for one variable is the same defect class as two decisions for one
    run, and a divergence that only appears for one spelling of a value is the kind
    that reaches production.
    """
    return os.environ.get("ADP_MEDIATED_GITHUB_ENABLED", "").lower() in ("1", "true", "yes")


__all__ = [
    "MAX_BLOB_BYTES",
    "MAX_COMMIT_CONTENT_BYTES",
    "MAX_FILES_PER_COMMIT",
    "MEDIATED_OPERATION_PATH",
    "LocalChange",
    "MediatedConflict",
    "MediatedError",
    "MediatedRefused",
    "MediatedUnavailable",
    "PublishedCommit",
    "collect_changes",
    "current_head",
    "enabled",
    "materialize_repository",
    "publish_commit",
    "publish_review",
    "read_repository",
    "upsert_pull_request",
]
