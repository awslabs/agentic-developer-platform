"""Bounded GitHub provider observations for admitted legacy tool calls."""

import asyncio
import json
import re
from dataclasses import dataclass

import httpx
from fastapi import HTTPException

from tool_adapters.legacy_transport import provider_destination

GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_INSTALLATIONS_URL = "https://api.github.com/user/installations"
MAX_RESPONSE = 65536
MAX_INSTALLATIONS_RESPONSE = 1048576


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


async def observe_github_app_user(token: str, *, app_id: str) -> GithubObservation:
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 16384
        or any(char.isspace() for char in token)
        or not isinstance(app_id, str)
        or not re.fullmatch(r"[1-9][0-9]{0,18}", app_id)
    ):
        raise HTTPException(403, detail={"error": "github_reconnect_required"})

    async def read(client, url, *, max_bytes=MAX_RESPONSE):
        destination = await asyncio.to_thread(provider_destination, url, "api.github.com")
        async with client.stream(
            "GET",
            destination,
            headers={
                "Authorization": "Bearer " + token,
                "Host": "api.github.com",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            extensions={"sni_hostname": "api.github.com"},
        ) as response:
            if response.status_code != 200:
                raise ValueError("GitHub App evidence unavailable")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > max_bytes:
                    raise ValueError("GitHub App response too large")
        if token.encode() in raw:
            raise ValueError("GitHub echoed the access token")
        return json.loads(raw)

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10, connect=3),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            user = await read(client, GITHUB_USER_URL)
            if (
                not isinstance(user, dict)
                or type(user.get("id")) is not int
                or not 0 < user["id"] < 2**63
                or user.get("type") != "User"
                or not isinstance(user.get("login"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", user["login"])
            ):
                raise ValueError("GitHub App account mismatch")
            installation_ids = []
            for page in range(1, 102):
                result = await read(
                    client,
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
                        or type(installation.get("app_id")) is not int
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
                {
                    "id": user["id"],
                    "login": user["login"],
                    "html_url": "https://github.com/" + user["login"],
                },
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
    except (
        httpx.HTTPError,
        HTTPException,
        ValueError,
        KeyError,
        TypeError,
        UnicodeError,
    ):
        raise HTTPException(403, detail={"error": "github_reconnect_required"}) from None


async def observe_github_user(token: str) -> GithubObservation:
    if not isinstance(token, str) or not token or len(token) > 16384 or any(char.isspace() for char in token):
        raise HTTPException(502, "GitHub identity unavailable")
    try:
        destination = await asyncio.to_thread(provider_destination, GITHUB_USER_URL, "api.github.com")
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10, connect=3),
            trust_env=False,
            follow_redirects=False,
        ) as client:
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
        profile = {
            "id": body["id"],
            "login": body["login"],
            "html_url": "https://github.com/" + body["login"],
        }
        return GithubObservation(str(body["id"]), tuple(scopes), profile)
    except (
        httpx.HTTPError,
        HTTPException,
        ValueError,
        KeyError,
        TypeError,
        UnicodeError,
    ):
        raise HTTPException(502, "GitHub identity unavailable") from None
