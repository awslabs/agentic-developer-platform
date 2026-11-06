"""Private, unregistered transport for one owner-approved Slack test-channel post."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from uuid import UUID

import httpx
from fastapi import HTTPException

from tool_adapters.legacy_slack import (
    API_ERRORS,
    HOST,
    MAX_RESPONSE,
    TEAM_ID,
    SlackIdentityObservation,
    _check_json,
    _error,
    _invalid_constant,
    _json_object,
    _retry_after,
    _validate_identity,
    observe_slack_identity,
)
from tool_adapters.legacy_transport import provider_destination

CHANNEL_ID = re.compile(r"[CG][A-Z0-9]{8,63}\Z")
TIMESTAMP = re.compile(r"[0-9]{10,16}\.[0-9]{1,6}\Z")
MARKER = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
POST_URL = "https://slack.com/api/chat.postMessage"


@dataclass(frozen=True)
class ApprovedPostFixture:
    owner_id: str
    team_id: str
    channel_id: str
    message: str
    run_marker: str


def configured_post_fixture() -> ApprovedPostFixture:
    raw = os.environ.get("ADP_TOOLS_SLACK_POST_FIXTURE", "")
    if os.environ.get("ADP_TOOLS_SLACK_POST_ENABLED") != "true":
        raise _error("slack_post_fixture_unavailable", 403)
    if not 1 <= len(raw) <= 2048:
        raise _error("slack_post_fixture_unavailable", 403)
    try:
        fixture = json.loads(raw, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise _error("slack_post_fixture_unavailable", 403) from None
    if (
        not isinstance(fixture, dict)
        or set(fixture)
        != {
            "owner_id",
            "team_id",
            "channel_id",
            "message",
            "run_marker",
            "approved",
            "isolated",
            "cleanup_method",
        }
        or not isinstance(fixture["owner_id"], str)
        or not 0 < len(fixture["owner_id"]) <= 255
        or not isinstance(fixture["team_id"], str)
        or not TEAM_ID.fullmatch(fixture["team_id"])
        or not isinstance(fixture["channel_id"], str)
        or not CHANNEL_ID.fullmatch(fixture["channel_id"])
        or fixture["approved"] is not True
        or fixture["isolated"] is not True
        or fixture["cleanup_method"] != "chat.delete"
        or not isinstance(fixture["message"], str)
        or not 1 <= len(fixture["message"]) <= 180
        or any(char in fixture["message"] for char in "@<>\r\n\x00")
        or not isinstance(fixture["run_marker"], str)
        or not MARKER.fullmatch(fixture["run_marker"])
        or UUID(fixture["run_marker"]).version != 4
    ):
        raise _error("slack_post_fixture_unavailable", 403)
    return ApprovedPostFixture(
        fixture["owner_id"],
        fixture["team_id"],
        fixture["channel_id"],
        fixture["message"],
        fixture["run_marker"],
    )


def approved_post_fixture(*, owner_id: str, team_id: str) -> ApprovedPostFixture:
    fixture = configured_post_fixture()
    if fixture.owner_id != owner_id or fixture.team_id != team_id:
        raise _error("slack_post_fixture_unavailable", 403)
    return fixture


def fixture_text(fixture: ApprovedPostFixture, run_marker: str) -> str:
    if not isinstance(run_marker, str) or not MARKER.fullmatch(run_marker) or UUID(run_marker).version != 4:
        raise _error("slack_post_marker_invalid", 400)
    return f"{fixture.message} [run:{run_marker}]"


async def post_slack_fixture(
    token: str,
    *,
    protected_identity: SlackIdentityObservation,
    fixture: ApprovedPostFixture,
    owner_id: str,
    channel_id: str,
    run_marker: str,
    text: str,
) -> dict:
    _validate_identity(protected_identity)
    approved = approved_post_fixture(owner_id=owner_id, team_id=protected_identity.team_id)
    if (
        fixture != approved
        or channel_id != approved.channel_id
        or run_marker != approved.run_marker
        or text != fixture_text(approved, run_marker)
    ):
        raise _error("slack_post_fixture_denied", 403)
    if "chat:write" not in protected_identity.scopes:
        raise _error("slack_write_scope_missing", 403)
    observed = await observe_slack_identity(token)
    if observed.user_id != protected_identity.user_id or observed.team_id != protected_identity.team_id:
        raise _error("slack_identity_changed", 403)
    if "chat:write" not in observed.scopes:
        raise _error("slack_write_scope_missing", 403)
    if observed != protected_identity:
        raise _error("slack_identity_changed", 403)
    try:
        async with asyncio.timeout(15):
            try:
                destination = await asyncio.to_thread(provider_destination, POST_URL, HOST)
            except HTTPException:
                raise _error("slack_egress_unavailable", 503) from None
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10, connect=3),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "POST",
                    destination,
                    extensions={"sni_hostname": HOST},
                    headers={
                        "Authorization": "Bearer " + token,
                        "Host": HOST,
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "Content-Type": "application/json",
                    },
                    json={
                        "channel": approved.channel_id,
                        "text": text,
                        "parse": "none",
                        "link_names": False,
                        "unfurl_links": False,
                        "unfurl_media": False,
                    },
                ) as response:
                    status = response.status_code
                    if status != 200:
                        if status < 400 or status >= 500:
                            raise _error("slack_post_uncertain", 503, provider_status=status)
                        raise _error(
                            "slack_http_error",
                            status if 400 <= status <= 599 else 502,
                            provider_status=status,
                            retry_after=_retry_after(response) if status == 429 else None,
                        )
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise _error("slack_post_uncertain", 503)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > MAX_RESPONSE:
                            raise _error("slack_post_uncertain", 503)
                        raw.extend(chunk)
                try:
                    body = json.loads(
                        raw.decode("utf-8"),
                        object_pairs_hook=_json_object,
                        parse_constant=_invalid_constant,
                    )
                    _check_json(body, token)
                except (ValueError, UnicodeError, RecursionError, HTTPException):
                    raise _error("slack_post_uncertain", 503) from None
                if not isinstance(body, dict) or type(body.get("ok")) is not bool:
                    raise _error("slack_post_uncertain", 503)
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
                message = body.get("message")
                if (
                    body.get("error")
                    or body.get("channel") != approved.channel_id
                    or not isinstance(message, dict)
                    or message.get("user") != protected_identity.user_id
                    or message.get("text") != text
                    or not isinstance(body.get("ts"), str)
                    or not TIMESTAMP.fullmatch(body["ts"])
                    or message.get("ts", body["ts"]) != body["ts"]
                ):
                    raise _error("slack_post_uncertain", 503)
                return {
                    "channel_id": approved.channel_id,
                    "ts": body["ts"],
                    "user_id": protected_identity.user_id,
                    "text": text,
                    "run_marker": run_marker,
                }
    except (httpx.HTTPError, TimeoutError):
        raise _error("slack_post_uncertain", 503) from None
