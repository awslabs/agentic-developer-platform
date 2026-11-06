"""Read an immutable GitHub document through its selected Product connection."""

import asyncio
import base64
import hashlib
import re
from urllib.parse import quote

from fastapi import HTTPException

from src.admin.connections.github_client import GitHubAppClient, github_account_id

MAX_DOCUMENT_BYTES = 65536


def document_location(binding):
    """Use a repository ID and relative path, never a caller-controlled URL."""
    repository, separator, path = binding.native_id.partition(":")
    if (
        not separator
        or not github_account_id(repository)
        or not path
        or len(path) > 400
        or any(part in ("", ".", "..") for part in path.split("/"))
        or any(ord(character) < 32 for character in path)
        or "\\" in path
        or (binding.desired or {}).get("repository_id", repository) != repository
    ):
        raise ValueError("A document selection needs repository-id:relative/path")
    return repository, path


async def read_document_bytes(binding, connection, *, client=None):
    """Return bounded bytes only after proving connection, repository and blob identity."""
    if binding.kind != "document" or binding.provider != "github" or not binding.installation_id:
        raise HTTPException(503, detail={"code": "dependency_unavailable", "dependency": "document_provider"})
    if (
        connection is None
        or connection.id != binding.installation_id
        or connection.org_id != binding.tenant_id
        or connection.provider != "github"
        or connection.ownership_disputed
    ):
        raise HTTPException(404, detail={"code": "source_unavailable"})
    installation = github_account_id(connection.installation_id)
    repository, path = document_location(binding)
    if not installation or not github_account_id(binding.namespace):
        raise HTTPException(404, detail={"code": "source_unavailable"})
    close = client is None
    if client is None:
        from src.admin.connections.service import _get_github_app_credentials

        app_id, private_key = _get_github_app_credentials()
        if not app_id or not private_key:
            raise HTTPException(503, detail={"code": "dependency_unavailable", "dependency": "github_app"})
        client = GitHubAppClient(app_id, private_key)
    try:
        async with asyncio.timeout(30):
            installed = await client.get_installation(int(installation))
            account = installed.get("account") or {}
            if (
                github_account_id(installed.get("id")) != installation
                or github_account_id(account.get("id")) != binding.namespace
                or connection.provider_scope_id not in (binding.namespace, account.get("login"))
            ):
                raise HTTPException(404, detail={"code": "source_unavailable"})
            repo = await client.get_installation_repository(int(installation), int(repository))
            owner, name = repo.get("owner") or {}, repo.get("name")
            login = owner.get("login")
            if (
                github_account_id(repo.get("id")) != repository
                or github_account_id(owner.get("id")) != binding.namespace
                or not isinstance(login, str)
                or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login)
                or not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name)
                or name in (".", "..")
            ):
                raise HTTPException(404, detail={"code": "source_unavailable"})
            file = await client.installation_request(
                int(installation),
                "GET",
                f"/repos/{quote(login, safe='')}/{quote(name, safe='')}/contents/{quote(path, safe='/')}",
            )
            if (
                not isinstance(file, dict)
                or file.get("type") != "file"
                or file.get("path") != path
                or file.get("encoding") != "base64"
                or file.get("submodule_git_url")
                or type(file.get("size")) is not int
                or not 0 < file["size"] <= MAX_DOCUMENT_BYTES
                or not isinstance(file.get("content"), str)
                or len(file["content"]) > MAX_DOCUMENT_BYTES * 2
                or not isinstance(file.get("sha"), str)
                or not re.fullmatch(r"[a-f0-9]{40}", file["sha"])
            ):
                raise ValueError("The selected document is not a bounded file")
            content = base64.b64decode(file["content"].replace("\n", ""), validate=True)
            # Match Git's object ID; retained document identities use SHA-256.
            digest = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content, usedforsecurity=False).hexdigest()
            if len(content) != file["size"] or digest != file["sha"]:
                raise ValueError("The document content differs from its source revision")
            return content, digest
    finally:
        if close:
            await client.aclose()
