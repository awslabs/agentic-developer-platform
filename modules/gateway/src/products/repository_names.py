"""Bounded display-name lookups for already-authorized GitHub source bindings."""

import asyncio
import re
import time
from collections import OrderedDict

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from jwt import PyJWTError

from src.admin.connections.github_client import GitHubAppClient, github_account_id

LOOKUP_TIMEOUT = 4
_names = OrderedDict()


def qualified_name(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}", value) is not None
        and value.split("/")[1] not in (".", "..")
    )


def numeric_label(value, namespace, repository_id):
    owner, repository = github_account_id(namespace), github_account_id(repository_id)
    return bool(owner and repository and isinstance(value, str) and tuple(map(github_account_id, value.split("/"))) == (owner, repository))


async def repository_names(pairs):
    """Call only after current connection/grant visibility checks, including cache hits.

    Names are display metadata. Native IDs, binding versions, grants and source
    verification are never changed by a lookup or a provider outage.
    """
    from src.admin.connections.service import _get_github_app_credentials

    resolved, pending = {}, {}
    for binding, connection in pairs:
        if (
            connection is None
            or connection.id != binding.installation_id
            or connection.org_id != binding.tenant_id
            or connection.provider != "github"
            or connection.ownership_disputed
        ):
            continue
        installation = github_account_id(connection.installation_id)
        owner = github_account_id(binding.namespace)
        repository = github_account_id(binding.native_id)
        if not all((installation, owner, repository)):
            continue
        key = (binding.tenant_id, connection.id, installation, owner, repository, connection.provider_scope_id)
        cached = _names.get(key)
        if cached and cached[0] > time.monotonic():
            resolved[binding.id] = cached[1]
        else:
            pending.setdefault(key, []).append(binding.id)
    if not pending:
        return resolved

    semaphore = asyncio.Semaphore(4)
    client = None

    async def read(key, binding_ids):
        _, _, installation, owner_id, repository_id, scope = key
        name = None
        try:
            async with semaphore:
                document = await client.get_installation_repository(int(installation), int(repository_id))
            owner = document.get("owner") or {}
            candidate = document.get("full_name")
            if (
                github_account_id(document.get("id")) == repository_id
                and github_account_id(owner.get("id")) == owner_id
                and (github_account_id(scope) == owner_id or scope == owner.get("login"))
                and qualified_name(candidate)
                and candidate == f"{owner.get('login')}/{document.get('name')}"
            ):
                name = candidate
        except (httpx.HTTPError, PyJWTError, ValueError, TypeError, AttributeError):
            pass
        _names[key] = (time.monotonic() + (300 if name else 20), name)
        _names.move_to_end(key)
        while len(_names) > 1024:
            _names.popitem(last=False)
        resolved.update(dict.fromkeys(binding_ids, name))

    try:
        async with asyncio.timeout(LOOKUP_TIMEOUT):
            app_id, private_key = await asyncio.to_thread(_get_github_app_credentials)
            if app_id and private_key:
                client = GitHubAppClient(app_id, private_key)
                await asyncio.gather(*(read(key, ids) for key, ids in pending.items()))
    except (TimeoutError, BotoCoreError, ClientError, PyJWTError, ValueError):
        pass
    finally:
        if client is not None:
            await client.aclose()
    return resolved
