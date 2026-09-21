"""Regression cases found while reviewing the complete CLI intake boundary."""

from types import SimpleNamespace

import pytest

from src.orchestration import intake_routes
from tests.orchestration.test_intake_routes import ORG, OTHER_ORG, SESSION, SESSIONS, a_row, client, token_context


@pytest.mark.asyncio
async def test_repository_resolution_stays_within_the_active_tenant(monkeypatch):
    from src.admin.connections import service

    calls = []

    async def connections(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            connections=[
                SimpleNamespace(installation_id=1, repositories=["acme/web"], repositories_live=True, tenant_id=ORG),
                SimpleNamespace(installation_id=2, repositories=["other/private"], repositories_live=True, tenant_id=OTHER_ORG),
            ]
        )

    monkeypatch.setattr(service, "list_connections", connections)
    result = await intake_routes._available_repositories(token_context(), None)
    assert calls[0]["member_tenant_ids"] == [ORG]
    assert result == [(1, ["acme/web"], True)]


def test_opening_retries_recover_one_identity_without_crossing_tenants():
    http, ingest = client()
    payload = {"message": "Improve checkout", "retry_token": "same-request"}
    first = http.post(SESSIONS, json=payload).json()
    second = http.post(SESSIONS, json=payload).json()
    assert first["session_id"] == second["session_id"]
    assert ingest.payloads[0]["message_id"] == ingest.payloads[1]["message_id"] == "same-request"
    other, _ = client(caller_org=OTHER_ORG)
    assert other.post(SESSIONS, json=payload).json()["session_id"] != first["session_id"]


@pytest.mark.parametrize("token", ["", " ", "\t\n"])
def test_empty_retry_tokens_are_refused_before_dispatch(token):
    http, ingest = client(rows=[a_row()])
    for path in (SESSIONS, f"{SESSIONS}/{SESSION}/turns"):
        assert http.post(path, json={"message": "Answer", "retry_token": token}).status_code == 422
    assert ingest.calls == []


def test_issue_context_is_read_under_the_resolved_installation(monkeypatch):
    from src.orchestration.tracker_provider import GitHubTrackerProvider

    async def repositories(*_):
        return [(11, ["acme/web"], True)]

    calls = []

    async def issue_body(self, **kwargs):
        calls.append(kwargs)
        return "Current requirements"

    monkeypatch.setattr(intake_routes, "_available_repositories", repositories)
    monkeypatch.setattr(GitHubTrackerProvider, "read_issue_body", issue_body)
    http, ingest = client()
    response = http.post(SESSIONS, json={"message": "Improve checkout", "repository": "acme/web", "issue": "42"})
    assert response.status_code == 202
    assert calls == [{"org_id": ORG, "installation_id": 11, "repo": "acme/web", "issue_number": 42}]
    assert "Current requirements" in ingest.payloads[0]["message"]
    assert ingest.payloads[0]["repository"] == "acme/web"
    assert ingest.payloads[0]["issue"] == "42"


def test_resumed_planning_rejects_a_different_repository(monkeypatch):
    async def repositories(*_):
        return [(11, ["acme/web", "acme/other"], True)]

    monkeypatch.setattr(intake_routes, "_available_repositories", repositories)
    row = {**a_row(), "intake_repository": "acme/web", "intake_issue": "42"}
    http, _ = client(rows=[row], drafts={f"session#{SESSION}": {"intent": "Checkout", "outcomes": ["Fast"]}})
    response = http.post(f"{SESSIONS}/{SESSION}/plan", json={"repository": "acme/other"})
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "repository_changed"


def test_unanswered_questions_cannot_be_bypassed_by_direct_plan_request():
    http, _ = client(
        rows=[a_row()], drafts={f"session#{SESSION}": {"intent": "Checkout", "outcomes": ["Fast"], "openQuestions": ["Which customers?"]}}
    )
    response = http.post(f"{SESSIONS}/{SESSION}/plan", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "draft_not_ready"
