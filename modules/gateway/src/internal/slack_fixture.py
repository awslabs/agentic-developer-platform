"""Validate the existing isolated Slack write fixture without provider transport."""

import json
import os
import re
from dataclasses import dataclass
from uuid import UUID

from src.internal.slack_reader import TEAM_ID, _error, _invalid_constant, _json_object

CHANNEL_ID = re.compile(r"[CG][A-Z0-9]{8,63}\Z")
MARKER = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


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
        or set(fixture) != {"owner_id", "team_id", "channel_id", "message", "run_marker", "approved", "isolated", "cleanup_method"}
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
    return ApprovedPostFixture(fixture["owner_id"], fixture["team_id"], fixture["channel_id"], fixture["message"], fixture["run_marker"])


def approved_post_fixture(*, owner_id: str, team_id: str) -> ApprovedPostFixture:
    fixture = configured_post_fixture()
    if fixture.owner_id != owner_id or fixture.team_id != team_id:
        raise _error("slack_post_fixture_unavailable", 403)
    return fixture


def fixture_text(fixture: ApprovedPostFixture, run_marker: str) -> str:
    if not isinstance(run_marker, str) or not MARKER.fullmatch(run_marker) or UUID(run_marker).version != 4:
        raise _error("slack_post_marker_invalid", 400)
    return f"{fixture.message} [run:{run_marker}]"
