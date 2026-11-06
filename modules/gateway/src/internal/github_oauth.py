"""Owner-bound GitHub account observation; provider tokens stay inside the broker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from src.shared.config import get_settings
from src.shared.identity.oauth_providers import GITHUB_REPOSITORY_SCOPES
from src.shared.models.base import new_uuid
from src.shared.models.vault import OAuthConnectionBinding, UserCredential

GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_INSTALLATIONS_URL = "https://api.github.com/user/installations"
MAX_RESPONSE = 65536
MAX_INSTALLATIONS_RESPONSE = 1048576
# The two consent snapshots an owned GitHub connection may carry: profile only (the
# original tools connection) or profile plus repository access (#8555). Anything else
# is an unreviewed grant and disqualifies the connection.
GITHUB_SCOPE_SNAPSHOTS = ({"oauth_scopes": ["read:user"]}, {"oauth_scopes": list(GITHUB_REPOSITORY_SCOPES)})


def grants_repository_access(credential, binding) -> bool:
    """True when the owner asked for repository access AND GitHub granted it (#8555).

    Both sides are required: the consent snapshot says what the owner approved; the
    observed scopes say what the provider actually issued. A snapshot without the
    grant (the owner unticked `repo` on GitHub's screen) must not read as access.
    """
    if is_github_app_grant(credential.scopes):
        return credential.scopes["github_app_grant"]["repository_access"] is True and valid_app_resources(binding)
    requested = credential.scopes.get("oauth_scopes") if isinstance(credential.scopes, dict) else None
    observed = binding.observed_scopes if isinstance(binding.observed_scopes, list) else []
    return requested == list(GITHUB_REPOSITORY_SCOPES) and {"repo", "workflow"} <= set(observed)


def is_github_app_grant(scopes):
    return isinstance(scopes, dict) and scopes in (
        {"github_app_grant": {"schema_version": 1, "repository_access": False}},
        {"github_app_grant": {"schema_version": 1, "repository_access": True}},
    )


def valid_app_resources(binding):
    resources = binding.observed_resources
    if not isinstance(resources, list) or len(resources) != 1 or not isinstance(resources[0], dict):
        return False
    resource = resources[0]
    installations = resource.get("installation_ids")
    app_id = get_settings().github_app_id
    return (
        set(resource) == {"kind", "schema_version", "app_id", "installation_ids", "account_id"}
        and resource["kind"] == "github_app_user"
        and type(resource["schema_version"]) is int
        and resource["schema_version"] == 1
        and isinstance(app_id, str)
        and re.fullmatch(r"[1-9][0-9]{0,18}", app_id) is not None
        and type(resource["app_id"]) is int
        and str(resource["app_id"]) == app_id
        and resource["account_id"] == binding.provider_account_id
        and isinstance(installations, list)
        and 0 < len(installations) <= 10000
        and all(type(value) is int and 0 < value < 2**63 for value in installations)
        and len(set(installations)) == len(installations)
        and binding.observed_scopes == []
    )


async def github_app_client():
    settings = get_settings()
    app_id = getattr(settings, "github_app_id", "")
    if not isinstance(app_id, str) or re.fullmatch(r"[1-9][0-9]{0,18}", app_id) is None:
        return None
    from src.shared.identity.oauth_setup import get_oauth_setup

    def fetch():
        setup = get_oauth_setup()
        secret = setup.secret_store.get_secret_value(SecretId=setup.secret_name("github"))
        return json.loads(secret["SecretString"])

    try:
        data = await asyncio.to_thread(fetch)
    except Exception:
        return None
    if (
        not isinstance(data, dict)
        or data.get("app_id") != app_id
        or not isinstance(data.get("client_id"), str)
        or re.fullmatch(r"[A-Za-z0-9_]{1,128}", data["client_id"]) is None
        or not isinstance(data.get("client_secret"), str)
        or not data["client_secret"]
    ):
        return None
    return {"client_id": data["client_id"], "client_secret": data["client_secret"], "app_id": app_id}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def credential_digest(credential):
    authority = {
        field: getattr(credential, field)
        for field in (
            "id",
            "org_id",
            "user_id",
            "team_id",
            "domain_app_id",
            "service",
            "provider",
            "credential_type",
            "secret_arn",
            "scopes",
            "agent_consent",
        )
    }
    expiry = credential.expires_at
    authority["expires_at"] = expiry.replace(tzinfo=UTC).isoformat() if expiry else None
    return hashlib.sha256(canonical(authority)).hexdigest()


def observed_scopes(header):
    if not isinstance(header, str) or not 0 < len(header) <= 4096:
        raise ValueError("GitHub scope evidence unavailable")
    scopes = [scope.strip() for scope in header.split(",")]
    if (
        len(scopes) > 64
        or len(set(scopes)) != len(scopes)
        or any(not re.fullmatch(r"[a-z][a-z0-9:_-]{0,127}", scope) for scope in scopes)
        or not {"read:user", "user"}.intersection(scopes)
    ):
        raise ValueError("GitHub profile scope unavailable")
    return sorted(scopes)


@dataclass(frozen=True)
class GithubObservation:
    account_id: str
    scopes: tuple[str, ...]
    profile: dict
    resources: tuple[dict, ...] = ()


async def observe_github_app_user(token: str, *, client_id: str, client_secret: str, app_id: str) -> GithubObservation:
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 16384
        or any(char.isspace() for char in token)
        or not isinstance(client_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_]{1,128}", client_id)
        or not isinstance(client_secret, str)
        or not client_secret
        or not isinstance(app_id, str)
        or not re.fullmatch(r"[1-9][0-9]{0,18}", app_id)
    ):
        raise HTTPException(403, detail={"error": "github_reconnect_required"})

    async def read(client, method, url, *, auth=None, body=None, max_bytes=MAX_RESPONSE):
        from src.internal.credential_routes import _validate_proxy_url

        destination = await asyncio.to_thread(_validate_proxy_url, url, get_settings())
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "Host": "api.github.com"}
        if auth is None:
            headers["Authorization"] = "Bearer " + token
        async with client.stream(
            method, destination, auth=auth, json=body, headers=headers, extensions={"sni_hostname": "api.github.com"}
        ) as response:
            if response.status_code != 200:
                raise ValueError("GitHub App evidence unavailable")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > max_bytes:
                    raise ValueError("GitHub App response too large")
        return json.loads(raw)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3), trust_env=False, follow_redirects=False) as client:
            user = await read(client, "GET", GITHUB_USER_URL)
            checked = await read(
                client,
                "POST",
                f"https://api.github.com/applications/{quote(client_id, safe='')}/token",
                auth=httpx.BasicAuth(client_id, client_secret),
                body={"access_token": token},
            )
            if (
                not isinstance(user, dict)
                or type(user.get("id")) is not int
                or not 0 < user["id"] < 2**63
                or user.get("type") != "User"
                or not isinstance(user.get("login"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", user["login"])
                or not isinstance(checked, dict)
                or not isinstance(checked.get("app"), dict)
                or checked["app"].get("client_id") != client_id
                or not isinstance(checked.get("user"), dict)
                or checked["user"].get("id") != user["id"]
                or type(checked["user"].get("id")) is not int
            ):
                raise ValueError("GitHub App account mismatch")
            installation_ids = []
            for page in range(1, 102):
                result = await read(
                    client,
                    "GET",
                    f"{GITHUB_INSTALLATIONS_URL}?per_page=100&page={page}",
                    max_bytes=MAX_INSTALLATIONS_RESPONSE,
                )
                if (
                    not isinstance(result, dict)
                    or type(result.get("total_count")) is not int
                    or not 0 <= result["total_count"] <= 10000
                    or not isinstance(result.get("installations"), list)
                    or len(result["installations"]) > 100
                ):
                    raise ValueError("GitHub installation evidence unavailable")
                for installation in result["installations"]:
                    if (
                        not isinstance(installation, dict)
                        or type(installation.get("id")) is not int
                        or not 0 < installation["id"] < 2**63
                        or not isinstance(installation.get("app_id"), int)
                    ):
                        raise ValueError("GitHub installation evidence unavailable")
                    if str(installation["app_id"]) == app_id:
                        installation_ids.append(installation["id"])
                if page * 100 >= result["total_count"]:
                    if (page - 1) * 100 + len(result["installations"]) != result["total_count"]:
                        raise ValueError("Incomplete GitHub installation evidence")
                    break
                if len(result["installations"]) != 100:
                    raise ValueError("Incomplete GitHub installation evidence")
            else:
                raise ValueError("Too many GitHub installations")
            if not installation_ids or len(set(installation_ids)) != len(installation_ids):
                raise ValueError("GitHub App installation unavailable")
            installation_ids.sort()
            account_id = str(user["id"])
            return GithubObservation(
                account_id,
                (),
                {"id": user["id"], "login": user["login"], "html_url": "https://github.com/" + user["login"]},
                (
                    {
                        "kind": "github_app_user",
                        "schema_version": 1,
                        "app_id": int(app_id),
                        "installation_ids": installation_ids,
                        "account_id": account_id,
                    },
                ),
            )
    except (httpx.HTTPError, HTTPException, ValueError, KeyError, TypeError, UnicodeError):
        raise HTTPException(403, detail={"error": "github_reconnect_required"}) from None


async def observe_github_user(token: str) -> GithubObservation:
    if not isinstance(token, str) or not token or len(token) > 16384 or any(char.isspace() for char in token):
        raise HTTPException(502, "GitHub identity unavailable")
    try:
        from src.internal.credential_routes import _validate_proxy_url

        destination = await asyncio.to_thread(_validate_proxy_url, GITHUB_USER_URL, get_settings())
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3), trust_env=False, follow_redirects=False) as client:
            async with client.stream(
                "GET",
                destination,
                extensions={"sni_hostname": "api.github.com"},
                headers={
                    "Authorization": "Bearer " + token,
                    "Host": "api.github.com",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            ) as response:
                if response.status_code != 200 or len(response.headers.get_list("x-oauth-scopes")) != 1:
                    raise ValueError("GitHub scope evidence unavailable")
                scopes = observed_scopes(response.headers["x-oauth-scopes"])
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise ValueError("GitHub response too large")
        if token.encode() in raw:
            raise ValueError("GitHub echoed the access token")
        body = json.loads(raw)
        if (
            not isinstance(body, dict)
            or type(body.get("id")) is not int
            or not 0 < body["id"] < 2**63
            or body.get("type") != "User"
            or not isinstance(body.get("login"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", body["login"])
        ):
            raise ValueError("GitHub account evidence unavailable")
        profile = {"id": body["id"], "login": body["login"], "html_url": "https://github.com/" + body["login"]}
        return GithubObservation(str(body["id"]), tuple(scopes), profile)
    except (httpx.HTTPError, HTTPException, ValueError, KeyError, TypeError, UnicodeError):
        raise HTTPException(502, "GitHub identity unavailable") from None


def new_github_binding(credential, observation):
    return OAuthConnectionBinding(
        credential_id=credential.id,
        org_id=credential.org_id,
        user_id=credential.user_id,
        provider="github",
        revision=new_uuid(),
        credential_digest=credential_digest(credential),
        provider_account_id=observation.account_id,
        observed_scopes=list(observation.scopes),
        observed_resources=list(observation.resources) if observation.resources else None,
        verified_at=datetime.now(UTC),
    )


def binding_claim(binding):
    claim = {
        "connection_id": binding.credential_id,
        "revision": binding.revision,
        "account_id": binding.provider_account_id,
        "scopes_sha256": hashlib.sha256(canonical(binding.observed_scopes)).hexdigest(),
        "credential_sha256": binding.credential_digest,
    }
    if binding.observed_resources is not None:
        claim["resources_sha256"] = hashlib.sha256(canonical(binding.observed_resources)).hexdigest()
    return claim


async def selected_github_connection(db, *, user_id, tenant_id, expected=None):
    rows = (
        await db.execute(
            select(UserCredential, OAuthConnectionBinding)
            .join(OAuthConnectionBinding, OAuthConnectionBinding.credential_id == UserCredential.id)
            .where(
                UserCredential.user_id == user_id,
                UserCredential.org_id == tenant_id,
                UserCredential.service == "github",
                UserCredential.provider == "agentcore-identity",
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    ).all()
    if len(rows) != 1:
        raise HTTPException(403, "GitHub connection unqualified")
    credential, binding = rows[0]
    workload = get_settings().oauth_workload_name
    app_grant = is_github_app_grant(credential.scopes)
    try:
        scopes = [] if app_grant and binding.observed_scopes == [] else observed_scopes(",".join(binding.observed_scopes))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(403, "GitHub connection unqualified") from None
    if (
        credential.team_id is not None
        or credential.domain_app_id is not None
        or credential.credential_type != "oauth_token"
        or credential.agent_consent is not True
        or not app_grant
        and credential.scopes not in GITHUB_SCOPE_SNAPSHOTS
        or app_grant
        and not valid_app_resources(binding)
        or not app_grant
        and binding.observed_resources is not None
        or not workload.endswith("-oauth-user")
        or credential.secret_arn != f"agentcore-identity:{workload}:github"
        or credential.expires_at is not None
        and credential.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)
        or binding.user_id != user_id
        or binding.org_id != tenant_id
        or binding.provider != "github"
        or not re.fullmatch(r"[1-9][0-9]{0,18}", binding.provider_account_id)
        or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", binding.revision)
        or not isinstance(binding.observed_scopes, list)
        or binding.observed_scopes != scopes
        or binding.verified_at.replace(tzinfo=UTC) > datetime.now(UTC)
        or binding.credential_digest != credential_digest(credential)
    ):
        raise HTTPException(403, "GitHub connection unqualified")
    claim = binding_claim(binding)
    if expected is not None and claim != expected:
        raise HTTPException(403, "GitHub connection changed")
    return credential, binding, claim
