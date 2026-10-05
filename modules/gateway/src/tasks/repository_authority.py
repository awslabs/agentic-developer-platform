"""Verify coding snapshots against enrolled repository scope and immutable Git blobs."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from urllib.parse import quote

import httpx
from starlette.concurrency import run_in_threadpool

from src.knowledge import github_app_service as github
from src.tasks import errors

CODING_PERSONA = "agent-task-claude-developer"
CODING_PERSONAS = frozenset({CODING_PERSONA, "agent-task-codex-developer"})
SHA = re.compile(r"^[a-f0-9]{40}$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def safe_path(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 512
        and not value.startswith("/")
        and all(part not in {"", ".", "..", ".git"} for part in value.split("/"))
        and "\\" not in value
        and re.fullmatch(r"[A-Za-z0-9_.\-/]+", value) is not None
    )


def validate_snapshot(value, scopes):
    if not isinstance(value, dict) or set(value) != {"schema_version", "repository_id", "repository", "commit_sha", "issue", "files"}:
        raise errors.invalid_request("Coding snapshot does not match the repository contract.")
    if (
        value["schema_version"] != "1.0"
        or type(value["repository_id"]) is not int
        or value["repository_id"] <= 0
        or type(value["issue"]) is not int
        or value["issue"] <= 0
    ):
        raise errors.invalid_request("Coding snapshot identity is invalid.")
    if (
        not isinstance(value["repository"], str)
        or not REPO.fullmatch(value["repository"])
        or not isinstance(value["commit_sha"], str)
        or not SHA.fullmatch(value["commit_sha"])
    ):
        raise errors.invalid_request("Coding snapshot repository or commit is invalid.")
    matches = [
        scope
        for scope in (scopes if isinstance(scopes, list) else [])
        if isinstance(scope, dict)
        and isinstance(scope.get("repository"), str)
        and scope.get("repository_id") == value["repository_id"]
        and scope.get("repository", "").lower() == value["repository"].lower()
    ]
    if len(matches) != 1:
        raise errors.disallowed_scope("Repository is not enrolled for this human Task owner.")
    prefixes = matches[0].get("path_prefixes", [])
    if not isinstance(prefixes, list) or not prefixes or not all(safe_path(p.rstrip("/")) if isinstance(p, str) else False for p in prefixes):
        raise errors.disallowed_scope("Repository path enrollment is invalid.")
    files = value["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= 32:
        raise errors.invalid_request("Coding snapshot requires 1..32 bounded files.")
    seen = set()
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "blob_sha", "content"}
            or not safe_path(item["path"])
            or item["path"] in seen
            or not isinstance(item["content"], str)
        ):
            raise errors.invalid_request("Coding snapshot file is invalid.")
        if not any(item["path"] == prefix or item["path"].startswith(prefix.rstrip("/") + "/") for prefix in prefixes):
            raise errors.disallowed_scope("Coding snapshot file is outside the enrolled paths.")
        raw = item["content"].encode("utf-8")
        # Git object IDs are a compatibility check; verify_snapshot compares
        # the actual bytes fetched from the authenticated repository API.
        digest = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False).hexdigest()
        if item["blob_sha"] != digest:
            raise errors.invalid_request("Coding snapshot file does not match its immutable blob.")
        seen.add(item["path"])
    return value


async def verify_snapshot(value, *, caller, db, client=None):
    owner, name = value["repository"].split("/")
    installation = await github.resolve_installation_for_repo(owner, name)
    if not installation or not await github.verify_installation_ownership(caller.tenant_id, installation, db=db):
        raise errors.not_found()
    app, key = github._get_global_app_credentials()
    token, _ = await github.mint_installation_token_with_expiry(
        app, key, installation, repositories=[name], permissions={"contents": "read", "issues": "read", "metadata": "read"}
    )
    owned = client is None
    client = client or httpx.AsyncClient(base_url="https://api.github.com", timeout=10, follow_redirects=False)

    async def read(path):
        async with client.stream("GET", path, headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}) as response:
            if response.status_code == 404:
                raise errors.not_found()
            if response.status_code != 200:
                raise errors.prerequisite_unavailable("Repository snapshot verification is unavailable.")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 1048576:
                    raise errors.invalid_request("Repository metadata exceeds its bound.")
            result = json.loads(content)
            if not isinstance(result, dict):
                raise errors.invalid_request("Repository metadata is malformed.")
            return result

    try:
        base = "/repos/" + value["repository"]
        repo = await read(base)
        issue = await read(base + "/issues/" + str(value["issue"]))
        commit = await read(base + "/git/commits/" + value["commit_sha"])
        if (
            repo.get("id") != value["repository_id"]
            or str(repo.get("full_name", "")).lower() != value["repository"].lower()
            or issue.get("number") != value["issue"]
            or "pull_request" in issue
            or commit.get("sha") != value["commit_sha"]
        ):
            raise errors.not_found()
        for item in value["files"]:
            metadata = await read(base + "/contents/" + quote(item["path"], safe="/") + "?ref=" + value["commit_sha"])
            if metadata.get("type") != "file" or metadata.get("sha") != item["blob_sha"] or metadata.get("path") != item["path"]:
                raise errors.invalid_request("Repository file changed or is not the requested immutable blob.")
            # SHA-1 alone is not a collision-resistant content binding. Compare
            # the authenticated API payload, even when the Git object ID matches.
            encoded = metadata.get("content")
            if metadata.get("encoding") != "base64" or not isinstance(encoded, str):
                raise errors.prerequisite_unavailable("Repository file content is unavailable.")
            try:
                actual = base64.b64decode("".join(encoded.split()), validate=True)
            except (ValueError, binascii.Error):
                raise errors.invalid_request("Repository file content is malformed.") from None
            if actual != item["content"].encode("utf-8"):
                raise errors.invalid_request("Repository file content does not match the submitted snapshot.")
    finally:
        if owned:
            await client.aclose()


async def require_coding_snapshot(*, caller, submit, policy, db, store=None):
    if not caller.principal_id.startswith("human:"):
        raise errors.disallowed_scope("Coding Tasks require an enrolled human owner.")
    inputs = submit.get("inputs", {})
    if set(inputs) != {"repository_snapshot_artifact"}:
        raise errors.invalid_request("Coding Tasks require one repository_snapshot_artifact input.")
    artifact_id = inputs["repository_snapshot_artifact"]
    if artifact_id not in submit.get("artifact_ids", []):
        raise errors.invalid_request("Coding snapshot must be an attached Task artifact.")
    if store is None:
        from src.tasks.routes import get_store

        store = get_store()
    record = await run_in_threadpool(store.load_artifact, artifact_id=artifact_id)
    if (
        record is None
        or record.tenant_id != caller.tenant_id
        or record.owner_principal_id != caller.principal_id
        or record.content_type != "application/json"
    ):
        raise errors.not_found()
    content = await run_in_threadpool(store.read_artifact, record=record)
    try:
        value = validate_snapshot(json.loads(content), policy.get("repository_scopes", []))
        await verify_snapshot(value, caller=caller, db=db)
    except (ValueError, TypeError, httpx.HTTPError):
        raise errors.prerequisite_unavailable("Repository snapshot could not be independently verified.") from None
