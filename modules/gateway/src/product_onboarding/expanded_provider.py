"""Current-credential, bounded Profile reads for optional Go discovery."""

import asyncio
import hashlib
import json
import re
from datetime import timedelta
from urllib.parse import quote, unquote, urlsplit

import httpx
from fastapi import HTTPException

from src.product_onboarding.source_discovery import connector_credential
from src.shared.models.base import utcnow

PROVIDERS = ("sonarqube", "argocd", "gitlab", "pagerduty", "linear", "slack")
MAX_BYTES = 1 << 20


def refuse():
    raise HTTPException(404, detail={"code": "profile_discovery_work_unavailable"})


def endpoint(credential, secret):
    provider = credential.provider or credential.service
    raw = secret.get("url")
    token = secret.get("token")
    if provider == "pagerduty":
        raw = raw or "https://api.pagerduty.com"
    elif provider == "linear":
        raw = raw or "https://api.linear.app/graphql"
    elif provider == "slack":
        raw = raw or "https://slack.com"
    if not isinstance(raw, str) or not isinstance(token, str) or not token or len(token) > 65536:
        refuse()
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(part in (".", "..") for part in parsed.path.split("/"))
        or any(character in raw for character in "\\\r\n")
    ):
        refuse()
    if provider == "linear" and parsed.path in ("", "/"):
        raw = raw.rstrip("/") + "/graphql"
    suffix = {"sonarqube": "api", "argocd": "api/v1", "gitlab": "api/v4", "pagerduty": "", "linear": "", "slack": "api"}[provider]
    return raw.rstrip("/") + ("/" + suffix if suffix else ""), token


async def material(credential, secrets, *, expected_version=None):
    if (
        credential is None
        or (credential.provider or credential.service) not in PROVIDERS
        or not connector_credential(credential.provider or credential.service, credential)
    ):
        refuse()
    try:
        version = await asyncio.to_thread(secrets.current_version_id, credential.secret_arn)
        if not version or expected_version is not None and version != expected_version:
            refuse()
        raw, served = await asyncio.to_thread(secrets.get_secret_at_version, credential.secret_arn, version)
        if served != version:
            refuse()
        base, token = endpoint(credential, json.loads(raw))
        return base, token, version
    except (ValueError, KeyError, TypeError, OSError):
        refuse()


def expected(provider, namespace, cursor, limit):
    page, native = 1, ""
    if cursor:
        match = re.fullmatch(r"v1:" + provider + r":100:([2-5]):([^\s]{0,448})", cursor)
        if not match:
            refuse()
        page, native = int(match.group(1)), unquote(match.group(2))
    if provider == "sonarqube":
        return "GET", "components/search_projects", {"p": [str(page)], "ps": [str(limit)]}, None
    if provider == "argocd":
        if page != 1:
            refuse()
        return "GET", "applications", {}, None
    if provider == "gitlab":
        scope = "" if namespace == "/" else namespace
        stage, number = native.split(":", 1) if native else ("projects" if scope.startswith("user:") else "groups", "1")
        if (
            stage not in ("groups", "projects")
            or not number.isascii()
            or not number.isdecimal()
            or not 1 <= int(number) <= page
            or (not scope and stage != "groups")
        ):
            refuse()
        if scope and not re.fullmatch(r"(?:user:)?[1-9][0-9]*", scope):
            refuse()
        query = {"page": [number], "per_page": [str(limit)]}
        if stage == "groups":
            if not scope:
                query["top_level_only"] = ["true"]
                path = "groups"
            else:
                path = "groups/" + scope + "/subgroups"
        else:
            query.update(archived=["false"], min_access_level=["20"])
            if scope.startswith("user:"):
                path = "users/" + scope[5:] + "/projects"
            else:
                query.update(with_shared=["false"], include_subgroups=["false"])
                path = "groups/" + scope + "/projects"
        return "GET", path, query, None
    if provider == "pagerduty":
        return "GET", "services", {"offset": [str((page - 1) * limit)], "limit": [str(limit)], "query": [""]}, None
    if provider == "slack":
        query = {"limit": [str(limit)], "types": ["public_channel,private_channel"]}
        if page > 1:
            query["cursor"] = [native]
        return "GET", "conversations.list", query, None
    if provider == "linear":
        after = ", after: " + json.dumps(native) if page > 1 else ""
        return (
            "POST",
            "",
            {},
            {"query": f"query {{ teams(first: {limit}{after}) {{ nodes {{ id name key description }} pageInfo {{ hasNextPage endCursor }} }} }}"},
        )
    refuse()


async def read(credential, secrets, method, path, query, body=None, *, expected_version=None):
    provider = credential.provider or credential.service
    base, token, version = await material(credential, secrets, expected_version=expected_version)
    if (
        not isinstance(query, dict)
        or len(query) > 10
        or not all(isinstance(key, str) and isinstance(value, list) and len(value) == 1 and isinstance(value[0], str) for key, value in query.items())
    ):
        refuse()
    if (
        not isinstance(path, str)
        or not isinstance(method, str)
        or path.startswith("//")
        or "/" in path
        and any(part in (".", "..") for part in path.split("/"))
    ):
        refuse()
    headers = {"Authorization": "Bearer " + token}
    if provider == "sonarqube":
        headers = {"Authorization": "Bearer " + token}
    elif provider == "gitlab":
        headers = {"PRIVATE-TOKEN": token}
    elif provider == "pagerduty":
        headers = {"Authorization": "Token token=" + token, "Accept": "application/vnd.pagerduty+json;version=2"}
    elif provider == "linear" and credential.credential_type == "api_key":
        headers = {"Authorization": token}
    target = base + ("/" + path.lstrip("/") if path else "")
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            async with client.stream(
                method, target, params={key: values[0] for key, values in query.items()}, json=body if method == "POST" else None, headers=headers
            ) as response:
                if response.status_code != 200:
                    refuse()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > MAX_BYTES:
                        refuse()
                    data.extend(chunk)
                paging = (
                    {key: [response.headers[key]] for key in ("X-Next-Page", "X-Page", "X-Per-Page", "X-Total-Pages") if key in response.headers}
                    if provider == "gitlab"
                    else {}
                )
        if await asyncio.to_thread(secrets.current_version_id, credential.secret_arn) != version:
            refuse()
        return json.loads(data), paging, version
    except (httpx.HTTPError, ValueError, TypeError):
        refuse()


async def observe(binding, credential, secrets, *, now=None):
    if credential is None or credential.id != binding.credential_id or credential.org_id != binding.tenant_id:
        return {"coverage": "denied", "status": "revoked", "reason": "source_access_denied"}
    provider, native_id = binding.provider, binding.native_id
    if provider not in PROVIDERS:
        return {"coverage": "unavailable", "status": "unverified", "reason": "unsupported_adapter"}
    quoted = quote(native_id, safe="")
    project, _, namespace = binding.namespace.partition("/")
    requests = {
        "sonarqube": ("GET", "components/show", {"component": [native_id]}, None),
        "argocd": ("GET", "applications/" + quoted, {"project": [project], "appNamespace": [namespace]}, None),
        "gitlab": ("GET", "projects/" + quoted, {}, None),
        "pagerduty": ("GET", "services/" + quoted, {}, None),
        "linear": ("POST", "", {}, {"query": "query { team(id: " + json.dumps(native_id) + ") { id name } }"}),
        "slack": ("GET", "conversations.info", {"channel": [native_id]}, None),
    }
    try:
        data, _, version = await read(credential, secrets, *requests[provider])
        selected = {
            "sonarqube": lambda: data["component"]["key"] == native_id,
            "argocd": lambda: data["metadata"]["name"] == native_id
            and data["metadata"]["namespace"] == namespace
            and data["spec"]["project"] == project,
            "gitlab": lambda: str(data["id"]) == native_id
            and (
                str(data["namespace"]["id"]) == binding.namespace
                if not binding.namespace.startswith("user:")
                else data["namespace"]["kind"] == "user" and str(data["owner"]["id"]) == binding.namespace[5:]
            ),
            "pagerduty": lambda: data["service"]["id"] == native_id,
            "linear": lambda: data["data"]["team"]["id"] == native_id,
            "slack": lambda: data["ok"] is True
            and data["channel"]["id"] == native_id
            and (data["channel"].get("is_channel") or data["channel"].get("is_group"))
            and not data["channel"].get("is_im")
            and not data["channel"].get("is_mpim")
            and (data["channel"].get("context_team_id") or "/") == binding.namespace,
        }[provider]()
        if not selected:
            refuse()
        observed_at = now or utcnow()
        revision = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {
            "coverage": "complete",
            "status": "healthy",
            "observed": {
                "observed_at": observed_at.isoformat(),
                "stale_after": (observed_at + timedelta(minutes=15)).isoformat(),
                "source_revision": "observation-sha256:" + revision,
                "native_id": native_id,
                "namespace": binding.namespace,
                "credential_version": version,
            },
        }
    except (HTTPException, KeyError, TypeError, ValueError):
        return {"coverage": "unavailable", "status": "unverified", "reason": "provider_read_unavailable"}
