"""Keep the public Slack illustration compatible with private fixture admission."""

import json
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from src.internal.slack_poster import approved_post_fixture, configured_post_fixture, fixture_text

DOC = Path(__file__).resolve().parents[4] / "docs/tools/slack-qualification.md"


@pytest.fixture
def illustration():
    text = DOC.read_text()
    examples = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert len(examples) == 1
    return json.loads(examples[0])


def test_documented_fixture_requires_separate_enablement_and_matching_owner(illustration, monkeypatch):
    assert illustration == {
        "owner_id": "owner-example",
        "team_id": "T00000001",
        "channel_id": "C00000001",
        "message": "ADP isolated fixture message.",
        "run_marker": "7d787d28-5c0e-4c86-b56f-fc13bb332b00",
        "approved": True,
        "isolated": True,
        "cleanup_method": "chat.delete",
    }
    monkeypatch.setenv("ADP_TOOLS_SLACK_POST_FIXTURE", json.dumps(illustration))
    monkeypatch.delenv("ADP_TOOLS_SLACK_POST_ENABLED", raising=False)
    with pytest.raises(HTTPException) as refused:
        configured_post_fixture()
    assert refused.value.status_code == 403
    monkeypatch.setenv("ADP_TOOLS_SLACK_POST_ENABLED", "true")
    fixture = approved_post_fixture(owner_id="owner-example", team_id="T00000001")
    assert fixture_text(fixture, illustration["run_marker"]) == ("ADP isolated fixture message. [run:7d787d28-5c0e-4c86-b56f-fc13bb332b00]")
    with pytest.raises(HTTPException) as wrong_owner:
        approved_post_fixture(owner_id="another-owner", team_id="T00000001")
    assert wrong_owner.value.status_code == 403


@pytest.mark.parametrize(
    "change",
    [
        {"approved": False},
        {"isolated": False},
        {"cleanup_method": "unapproved"},
        {"channel_id": "D00000001"},
        {"run_marker": "not-a-uuid"},
    ],
)
def test_documented_approval_rejects_unapproved_fixture_changes(illustration, monkeypatch, change):
    monkeypatch.setenv("ADP_TOOLS_SLACK_POST_ENABLED", "true")
    monkeypatch.setenv("ADP_TOOLS_SLACK_POST_FIXTURE", json.dumps({**illustration, **change}))
    with pytest.raises(HTTPException) as refused:
        approved_post_fixture(owner_id="owner-example", team_id="T00000001")
    assert refused.value.status_code == 403
