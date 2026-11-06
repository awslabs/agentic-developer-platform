"""Unregistered Slack user-token observations and bounded profile reads only."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException

from src.shared.config import get_settings

HOST = "slack.com"
MAX_RESPONSE = 65536
USER_ID = re.compile(r"[UW][A-Z0-9]{8,63}\Z")
TEAM_ID = re.compile(r"T[A-Z0-9]{8,63}\Z")
SCOPE = re.compile(r"[a-z][a-z0-9:._-]{0,127}\Z")
READ_SCOPE = "users:read"
API_ERRORS = {
    "not_authed": 401,
    "invalid_auth": 401,
    "token_revoked": 401,
    "token_expired": 401,
    "account_inactive": 401,
    "missing_scope": 403,
    "no_permission": 403,
    "team_access_not_granted": 403,
    "not_allowed_token_type": 403,
    "org_login_required": 403,
    "user_not_found": 404,
    "users_not_found": 404,
    "ratelimited": 429,
    "request_timeout": 503,
    "internal_error": 502,
    "fatal_error": 502,
}


def _error(code, status=502, *, provider_status=None, provider_error=None, retry_after=None):
    detail = {"error": code}
    if provider_status is not None:
        detail["provider_status"] = provider_status
    if provider_error is not None:
        detail["provider_error"] = provider_error
    return HTTPException(status, detail, headers={"Retry-After": retry_after} if retry_after is not None else None)


def _scopes(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        raise _error("slack_scope_evidence_unavailable")
    scopes = [scope.strip() for scope in value.split(",")]
    if len(scopes) > 64 or len(set(scopes)) != len(scopes) or any(not SCOPE.fullmatch(scope) for scope in scopes):
        raise _error("slack_scope_evidence_unavailable")
    if READ_SCOPE not in scopes:
        raise _error("slack_read_scope_missing", 403)
    return tuple(sorted(scopes))


@dataclass(frozen=True)
class SlackIdentityObservation:
    """Provider observation, not an ADP owner or signed connection binding."""

    user_id: str
    team_id: str
    scopes: tuple[str, ...]


def _validate_identity(identity):
    if (
        not isinstance(identity, SlackIdentityObservation)
        or not isinstance(identity.user_id, str)
        or not USER_ID.fullmatch(identity.user_id)
        or not isinstance(identity.team_id, str)
        or not TEAM_ID.fullmatch(identity.team_id)
        or not isinstance(identity.scopes, tuple)
        or len(identity.scopes) > 64
        or any(not isinstance(scope, str) for scope in identity.scopes)
    ):
        raise _error("slack_identity_evidence_unavailable", 403)
    try:
        if identity.scopes != _scopes(",".join(identity.scopes)):
            raise ValueError("noncanonical scopes")
    except (HTTPException, ValueError):
        raise _error("slack_identity_evidence_unavailable", 403) from None


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-JSON constant")


def _check_json(body, token):
    pending = [(body, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 32:
            raise ValueError("JSON nesting limit")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.keys())
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
        elif isinstance(value, str):
            value.encode("utf-8")
            if token in value:
                raise _error("slack_response_refused")


def _retry_after(response):
    if len(response.headers.get_list("retry-after")) == 1:
        value = response.headers["retry-after"]
        if re.fullmatch(r"[0-9]{1,5}", value) and int(value) <= 86400:
            return str(int(value))
    return None


async def _request(token, *, user_id=None):
    """Only two fixed endpoints; callers cannot supply a URL, method or headers."""
    if not isinstance(token, str) or not 1 <= len(token) <= 16384 or any(not 33 <= ord(char) <= 126 for char in token):
        raise _error("slack_credential_unavailable", 403)
    if user_id is not None and (not isinstance(user_id, str) or not USER_ID.fullmatch(user_id)):
        raise _error("slack_invalid_user_id", 400)
    method = "POST" if user_id is None else "GET"
    url = "https://slack.com/api/auth.test" if user_id is None else "https://slack.com/api/users.info?" + urlencode({"user": user_id})
    from src.internal.credential_routes import _validate_proxy_url

    try:
        async with asyncio.timeout(15):
            try:
                destination = await asyncio.to_thread(_validate_proxy_url, url, get_settings())
            except HTTPException:
                raise _error("slack_egress_unavailable", 503) from None
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3), trust_env=False, follow_redirects=False) as client:
                async with client.stream(
                    method,
                    destination,
                    extensions={"sni_hostname": HOST},
                    headers={
                        "Authorization": "Bearer " + token,
                        "Host": HOST,
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    if response.status_code != 200:
                        status = response.status_code
                        code = {401: "slack_unauthorized", 403: "slack_forbidden", 404: "slack_not_found", 429: "slack_throttled"}.get(
                            status, "slack_http_error"
                        )
                        raise _error(
                            code,
                            status if 400 <= status <= 599 else 502,
                            provider_status=status,
                            retry_after=_retry_after(response) if status == 429 else None,
                        )
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise _error("slack_encoded_response_refused")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > MAX_RESPONSE:
                            raise _error("slack_response_too_large")
                        raw.extend(chunk)
                body = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_object, parse_constant=_invalid_constant)
                _check_json(body, token)
                if not isinstance(body, dict) or type(body.get("ok")) is not bool:
                    raise _error("slack_invalid_response")
                if body["ok"] is False:
                    vendor = body.get("error")
                    known = isinstance(vendor, str) and vendor in API_ERRORS
                    raise _error(
                        "slack_api_error",
                        API_ERRORS[vendor] if known else 502,
                        provider_status=200,
                        provider_error=vendor if known else "unrecognized_error",
                        retry_after=_retry_after(response) if vendor == "ratelimited" else None,
                    )
                if body.get("error"):
                    raise _error("slack_invalid_response")
                # Accepted scopes describe the API, not this token's actual grants.
                if len(response.headers.get_list("x-oauth-scopes")) != 1:
                    raise _error("slack_scope_evidence_unavailable")
                scope_header = response.headers["x-oauth-scopes"]
                scopes = _scopes(scope_header)
                if token in scope_header:
                    raise _error("slack_response_refused")
                return body, scopes
    except (httpx.HTTPError, TimeoutError):
        raise _error("slack_transport_unavailable", 503) from None
    except (ValueError, UnicodeError, RecursionError):
        raise _error("slack_invalid_response") from None


async def observe_slack_identity(token: str) -> SlackIdentityObservation:
    """Observe the actual user/workspace/scopes of the broker-supplied token."""
    body, scopes = await _request(token)
    if "bot_id" in body or ("is_bot" in body and body["is_bot"] is not False):
        raise _error("slack_user_token_required", 403)
    identity = SlackIdentityObservation(body.get("user_id"), body.get("team_id"), scopes)
    _validate_identity(identity)
    return identity


async def read_slack_user(token: str, *, protected_identity: SlackIdentityObservation, user_id: str) -> dict:
    """Re-observe exact protected identity, then read one requested visible user.

    The original mediator must supply current ADP authority and a signed owned
    connection revision. This private data object cannot grant either authority.
    """
    _validate_identity(protected_identity)
    if not isinstance(user_id, str) or not USER_ID.fullmatch(user_id):
        raise _error("slack_invalid_user_id", 400)
    try:
        async with asyncio.timeout(25):
            observed = await observe_slack_identity(token)
            if observed != protected_identity:
                raise _error("slack_identity_changed", 403)
            body, scopes = await _request(token, user_id=user_id)
            if scopes != observed.scopes:
                raise _error("slack_scope_changed", 403)
            user = body.get("user")
            if (
                not isinstance(user, dict)
                or user.get("id") != user_id
                or not isinstance(user.get("name"), str)
                or not 1 <= len(user["name"]) <= 80
                or any(ord(char) < 32 or ord(char) == 127 for char in user["name"])
            ):
                raise _error("slack_invalid_user_response")
            return {"id": user_id, "name": user["name"]}
    except TimeoutError:
        raise _error("slack_transport_unavailable", 503) from None
