"""Forge app identity and bounded Jira reads behind ADP's existing MCP adapter."""

import re
import time
from functools import lru_cache
from urllib.parse import urlsplit

import httpx
import jwt

from tool_adapters.atlassian_observe import ProviderResponseError

JWKS_URL = "https://forge.cdn.prod.atlassian-dev.net/.well-known/jwks.json"
SITE = r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"
SCHEMAS = {
    f"jira:{name}": {"type": "object", "version": "forge-rest-1"}
    for name in (
        "projects",
        "boards",
        "issues",
        "backlog",
        "board",
        "sprints",
    )
}


@lru_cache(maxsize=1)
def signing_keys():
    return jwt.PyJWKClient(JWKS_URL, cache_jwk_set=True, lifespan=3600, timeout=3)


def verified_delivery(body, *, key_resolver=None):
    """The expected app/environment come from signed ADP configuration, never FIT."""
    if not body.forge_app_id or not body.forge_environment_id or body.forge_action not in {"pair", "refresh"}:
        raise ValueError("forge_not_configured")
    resolver = key_resolver or signing_keys().get_signing_key_from_jwt
    key = resolver(body.forge_invocation)
    claims = jwt.decode(
        body.forge_invocation,
        getattr(key, "key", key),
        algorithms=["RS256"],
        audience=body.forge_app_id,
        issuer="forge/invocation-token",
        options={"require": ["exp", "iat", "iss", "aud"]},
    )
    app = claims["app"]
    expected_modules = (
        {"adp-jira-pair", "adp-jira-setup"}
        if body.forge_action == "pair"
        else {"adp-jira-refresh", "adp-jira-refresh-hourly"}
    )
    # Forge documents both environment ARIs and the app/environment ARI form.
    # Configuration uses the stable environment UUID returned by the CLI.
    environment = body.forge_environment_id
    expected_environments = {
        environment,
        "ari:cloud:ecosystem::environment/" + environment,
        "ari:cloud:ecosystem::environment/" + body.forge_app_id.rsplit("/", 1)[1] + "/" + environment,
    }
    if (
        app["id"] != body.forge_app_id
        or app["environment"]["id"] not in expected_environments
        or app["module"]["key"] not in expected_modules
        or claims["iat"] < time.time() - 300
    ):
        raise ValueError("forge_invocation_mismatch")
    if (
        body.forge_action == "pair"
        and claims.get("context", {}).get("extension", {}).get("type", "jira:adminPage") != "jira:adminPage"
    ):
        raise ValueError("forge_admin_page_required")
    installation = app["installationId"]
    if not isinstance(installation, str) or not re.fullmatch(
        r"ari:cloud:ecosystem::installation/" + SITE, installation
    ):
        raise ValueError("forge_installation_invalid")
    if app.get("installation", {}).get("id", installation) != installation:
        raise ValueError("forge_installation_mismatch")
    if body.forge_installation_id and installation != body.forge_installation_id:
        raise ValueError("forge_installation_mismatch")
    base = app["apiBaseUrl"]
    if not isinstance(base, str) or not re.fullmatch(r"https://api\.atlassian\.com/ex/jira/" + SITE, base):
        raise ValueError("forge_site_invalid")
    site = base.rsplit("/", 1)[1]
    if claims.get("context", {}).get("cloudId", site) != site or body.site_id != site:
        raise ValueError("forge_site_mismatch")
    # Atlassian documents reading exp for system-token freshness. Authentication
    # is established by the signed FIT plus actual calls as an app to this site.
    token = body.forge_token
    if not isinstance(token, str) or any(c.isspace() for c in token):
        raise ValueError("forge_token_invalid")
    expiry = jwt.decode(token, options={"verify_signature": False})["exp"]
    if type(expiry) is not int or not time.time() + 60 < expiry <= time.time() + 4 * 3600 + 60:
        raise ValueError("forge_token_expired")
    return {"installation_id": installation, "site_id": site, "expires_at": expiry}


class JiraREST:
    def __init__(self, site_id, token, deadline, *, transport=None):
        if not re.fullmatch(SITE, site_id):
            raise ValueError("forge_site_invalid")
        self.base = "https://api.atlassian.com/ex/jira/" + site_id
        self.token, self.deadline, self.transport = token, deadline, transport

    def request(self, path, *, params=None, body=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        with httpx.Client(
            timeout=min(remaining, 10), follow_redirects=False, trust_env=False, transport=self.transport
        ) as client:
            with client.stream(
                "POST" if body is not None else "GET",
                self.base + path,
                params=params,
                json=body,
                headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"},
            ) as response:
                if response.status_code != 200:
                    code = {
                        400: "unsupported_view",
                        401: "provider_unauthorized",
                        403: "provider_forbidden",
                        404: "not_found",
                        429: "rate_limited",
                    }.get(response.status_code, "provider_unavailable")
                    raise ProviderResponseError(code)
                content = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() >= self.deadline or len(content) + len(chunk) > 4 * 1024 * 1024:
                        raise ValueError("provider_response_limit")
                    content.extend(chunk)
                import json

                value = json.loads(content)
                if not isinstance(value, dict):
                    raise ValueError("provider_response_invalid")
                return value


def page_args(args):
    limit = args.get("maxResults", 20)
    start = args.get("startAt", 0)
    if type(limit) is not int or not 1 <= limit <= 100 or type(start) is not int or not 0 <= start <= 100000:
        raise ValueError("invalid_page")
    return {"maxResults": limit, "startAt": start}


def read(body, token, *, client_factory=JiraREST):
    client = client_factory(body.site_id, token, time.monotonic() + (body.deadline_at_ms / 1000 - time.time()))
    if body.operation == "verify":
        identity = client.request("/rest/api/3/myself")
        if not identity.get("accountId") or identity.get("accountType") != "app":
            raise ValueError("forge_app_identity_required")
        projects = client.request("/rest/api/3/project/search", params={"maxResults": 1})
        issues = client.request("/rest/api/3/search/jql", body={"jql": "project IS NOT EMPTY", "maxResults": 1})
        if not isinstance(projects.get("values"), list) or not isinstance(issues.get("issues"), list):
            raise ValueError("provider_verification_incomplete")
        return {"account_id": identity["accountId"], "schemas": SCHEMAS}
    if body.schemas != SCHEMAS:
        raise ValueError("provider_schema_changed")
    args = body.arguments
    operation = body.operation
    if operation == "issues" and "boardId" not in args:
        query = {key: args[key] for key in ("jql", "maxResults", "fields", "nextPageToken") if key in args}
        value = client.request("/rest/api/3/search/jql", body=query)
    elif operation in {"projects", "boards"}:
        params = page_args(args)
        params.update({key: args[key] for key in ("query", "projectKeyOrId", "id") if key in args})
        value = client.request(
            "/rest/api/3/project/search" if operation == "projects" else "/rest/agile/1.0/board", params=params
        )
    else:
        board = args.get("boardId")
        if type(board) is not int or board <= 0:
            raise ValueError("board_required")
        suffix = {"issues": "issue", "backlog": "backlog", "board": "configuration", "sprints": "sprint"}[operation]
        if operation in {"issues", "backlog"}:
            # Enhanced Software endpoints use the provider's continuation token
            # and retain board/backlog semantics. No local approximation of rank
            # or sprint membership, and no dependency on deprecated offset reads.
            params = {
                key: ",".join(args[key]) if key == "fields" else args[key]
                for key in ("jql", "fields", "maxResults", "nextPageToken")
                if key in args
            }
            value = client.request(f"/rest/software/1.0/board/{board}/{suffix}", params=params)
        else:
            params = {} if operation == "board" else page_args(args)
            value = client.request(f"/rest/agile/1.0/board/{board}/{suffix}", params=params)
            if operation == "board":
                # Configuration and board details have different location
                # shapes. Take current type and scope from the board resource.
                detail = client.request(f"/rest/agile/1.0/board/{board}")
                if detail.get("id") != board or not isinstance(detail.get("type"), str):
                    raise ValueError("provider_board_invalid")
                value["type"] = detail["type"]
                value["location"] = detail.get("location")
    if operation in {"projects", "boards", "sprints"} and not isinstance(value.get("values"), list):
        raise ValueError("provider_page_invalid")
    if operation in {"issues", "backlog"}:
        if not isinstance(value.get("issues"), list) or type(value.get("isLast")) is not bool:
            raise ValueError("provider_page_invalid")
        for issue in value["issues"]:
            if not isinstance(issue, dict) or not isinstance(issue.get("key"), str):
                raise ValueError("provider_issue_invalid")
            location = urlsplit(issue.get("self", ""))
            if (
                location.scheme == "https"
                and location.hostname
                and location.hostname.endswith(".atlassian.net")
                and not location.username
                and not location.password
                and location.port in (None, 443)
                and re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", issue["key"])
            ):
                issue["webUrl"] = f"https://{location.hostname}/browse/{issue['key']}"
    return {"data": value, "upstream_tool": "jira:" + operation, "retrieved_at_ms": int(time.time() * 1000)}
