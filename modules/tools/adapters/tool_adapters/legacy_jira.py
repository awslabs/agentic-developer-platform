"""Private Jira read primitives; site observations are not user identity or authority.

Only the existing broker may supply a token and protected site observation. There
is deliberately no route, connection binding, or public tool registration here.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from tool_adapters.legacy_transport import provider_destination

HOST = "api.atlassian.com"
RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
IDENTITY_URL = "https://api.atlassian.com/me"
IDENTITY_SCOPE = "read:me"
REQUIRED_SCOPE = "read:jira-work"
MAX_RESPONSE = 65536
MAX_SITES = 128
MAX_SCOPES = 64
CLOUD_ID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\Z")
ISSUE_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,127}-[1-9][0-9]{0,19}\Z")
SCOPE = re.compile(r"[a-z][a-z0-9:._-]{0,127}\Z")
ACCOUNT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,127}\Z")


def _error(
    code: str,
    status: int = 502,
    *,
    provider_status: int | None = None,
    retry_after: str | None = None,
) -> HTTPException:
    detail = {"error": code}
    if provider_status is not None:
        detail["provider_status"] = provider_status
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    return HTTPException(status, detail, headers=headers)


def _scopes(value) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > MAX_SCOPES
        or any(not isinstance(scope, str) or not SCOPE.fullmatch(scope) for scope in value)
        or len(set(value)) != len(value)
    ):
        raise _error("jira_invalid_site_evidence")
    return tuple(sorted(value))


@dataclass(frozen=True)
class JiraSiteObservation:
    """Observed resource and grants only; never an account ID or signed binding."""

    cloud_id: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class JiraUserObservation:
    """The account observed at Atlassian's authenticated User Identity API."""

    account_id: str


def select_jira_site(observations: tuple[JiraSiteObservation, ...], cloud_id: str) -> JiraSiteObservation:
    """Select an exact site from broker observations, not caller-provided JSON."""
    if not isinstance(cloud_id, str) or not CLOUD_ID.fullmatch(cloud_id):
        raise _error("jira_invalid_cloud_id", 400)
    if (
        not isinstance(observations, tuple)
        or len(observations) > MAX_SITES
        or any(not isinstance(site, JiraSiteObservation) for site in observations)
    ):
        raise _error("jira_site_evidence_unavailable", 403)
    for site in observations:
        if (
            not isinstance(site.cloud_id, str)
            or not CLOUD_ID.fullmatch(site.cloud_id)
            or not isinstance(site.scopes, tuple)
        ):
            raise _error("jira_site_evidence_unavailable", 403)
        try:
            if _scopes(list(site.scopes)) != site.scopes:
                raise _error("jira_site_evidence_unavailable", 403)
        except HTTPException:
            raise _error("jira_site_evidence_unavailable", 403) from None
    matches = [site for site in observations if site.cloud_id == cloud_id and REQUIRED_SCOPE in site.scopes]
    if len(matches) != 1:
        raise _error("jira_site_denied", 403)
    return matches[0]


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-JSON numeric constant")


def _contains_token(body, token: str) -> bool:
    pending = [(body, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 32:
            raise ValueError("JSON nesting limit exceeded")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.keys())
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
        elif isinstance(value, str):
            value.encode("utf-8")  # Refuse unpaired JSON surrogate escapes.
            if token in value:
                return True
    return False


def _provider_error(response: httpx.Response) -> HTTPException:
    status = response.status_code
    codes = {
        401: "jira_unauthorized",
        403: "jira_forbidden",
        404: "jira_not_found",
        429: "jira_throttled",
    }
    retry_after = None
    if status == 429 and len(response.headers.get_list("retry-after")) == 1:
        value = response.headers["retry-after"]
        if re.fullmatch(r"[0-9]{1,5}", value) and int(value) <= 86400:
            retry_after = str(int(value))
    return _error(
        codes.get(status, "jira_http_error"),
        status if 400 <= status <= 599 else 502,
        provider_status=status,
        retry_after=retry_after,
    )


async def _get_json(token: str, url: str):
    """One GET with a broker token; only fixed internal URL builders call this."""
    if not isinstance(token, str) or not 1 <= len(token) <= 16384 or any(not 33 <= ord(char) <= 126 for char in token):
        raise _error("jira_credential_unavailable", 403)

    try:
        async with asyncio.timeout(15):
            try:
                destination = await asyncio.to_thread(provider_destination, url, HOST)
            except HTTPException:
                raise _error("jira_egress_unavailable", 503) from None
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10, connect=3),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "GET",
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
                        raise _provider_error(response)
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise _error("jira_encoded_response_refused")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > MAX_RESPONSE:
                            raise _error("jira_response_too_large")
                        raw.extend(chunk)
            body = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_json_object,
                parse_constant=_invalid_constant,
            )
            if _contains_token(body, token):
                raise _error("jira_response_refused")
            if isinstance(body, dict) and any(body.get(key) for key in ("error", "errors", "errorMessages")):
                raise _error("jira_api_error", provider_status=200)
            return body
    except (httpx.HTTPError, TimeoutError):
        raise _error("jira_transport_unavailable", 503) from None
    except (ValueError, UnicodeError, RecursionError):
        raise _error("jira_invalid_response") from None


async def observe_jira_sites(token: str) -> tuple[JiraSiteObservation, ...]:
    """Read sites/scopes; accessible-resources does not attest a vendor user."""
    body = await _get_json(token, RESOURCES_URL)
    if not isinstance(body, list) or len(body) > MAX_SITES:
        raise _error("jira_invalid_site_evidence")
    observations = []
    for resource in body:
        if (
            not isinstance(resource, dict)
            or not isinstance(resource.get("id"), str)
            or not CLOUD_ID.fullmatch(resource["id"])
        ):
            raise _error("jira_invalid_site_evidence")
        scopes = _scopes(resource.get("scopes"))
        if REQUIRED_SCOPE in scopes:
            observations.append(JiraSiteObservation(resource["id"], scopes))
    if len({site.cloud_id for site in observations}) != len(observations):
        raise _error("jira_ambiguous_site_evidence")
    return tuple(observations)


async def observe_jira_user(token: str) -> JiraUserObservation:
    """Read the consenting account, retaining only its opaque account ID.

    Atlassian requires the User Identity API and explicit ``read:me`` consent
    for this endpoint. This primitive requests no consent and changes no scope.
    The broker must bind this observation to the same token, owner and current
    connection used for the separate site and issue reads.
    """
    body = await _get_json(token, IDENTITY_URL)
    if (
        not isinstance(body, dict)
        or body.get("account_type") != "atlassian"
        or body.get("account_status") != "active"
        or not isinstance(body.get("account_id"), str)
        or not ACCOUNT_ID.fullmatch(body["account_id"])
    ):
        raise _error("jira_invalid_identity_evidence")
    return JiraUserObservation(body["account_id"])


async def read_jira_issue(
    token: str,
    *,
    protected_site: JiraSiteObservation,
    cloud_id: str,
    issue_id_or_key: str,
) -> dict:
    """One bounded issue read after exact protected site selection.

    Integration must re-observe the site with this user's broker token, verify
    current authority and the protected binding before invoking this primitive.
    This function cannot attribute the result to a vendor account.
    """
    select_jira_site((protected_site,), cloud_id)
    if not isinstance(issue_id_or_key, str) or not (
        ISSUE_ID.fullmatch(issue_id_or_key) or ISSUE_KEY.fullmatch(issue_id_or_key)
    ):
        raise _error("jira_invalid_issue_id", 400)
    url = (
        "https://api.atlassian.com/ex/jira/"
        + quote(cloud_id, safe="")
        + "/rest/api/3/issue/"
        + quote(issue_id_or_key, safe="")
        + "?fields=summary"
    )
    body = await _get_json(token, url)
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("id"), str)
        or not ISSUE_ID.fullmatch(body["id"])
        or not isinstance(body.get("key"), str)
        or not ISSUE_KEY.fullmatch(body["key"])
        or not isinstance(body.get("fields"), dict)
        or not isinstance(body["fields"].get("summary"), str)
    ):
        raise _error("jira_invalid_issue_response")
    summary = body["fields"]["summary"]
    if (
        not 1 <= len(summary) <= 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in summary)
        or (ISSUE_ID.fullmatch(issue_id_or_key) and body["id"] != issue_id_or_key)
        or (ISSUE_KEY.fullmatch(issue_id_or_key) and body["key"] != issue_id_or_key)
    ):
        raise _error("jira_issue_mismatch")
    return {"id": body["id"], "key": body["key"], "fields": {"summary": summary}}
