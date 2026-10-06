"""EXT01-t3: delegated work keeps ADP runs and provider history distinguishable."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.activity import chat_work
from src.activity.external_events import ProviderEvent, classify_events
from src.activity.external_scope import ExternalRead
from src.activity.routes import get_activity_service
from src.activity.schemas import InvocationItem
from src.activity.service import WorkActivityPage
from src.agentauth import chat_data_routes
from tests.agentauth.chat_activity_fixtures import HEADERS, workload_runtime

URL = "/v1/chat/data/activity/work"
WINDOW = {"from": "2026-10-01T00:00:00Z", "to": "2026-10-04T00:00:00Z", "timezone": "UTC", "run_id": "run-a"}


def event(kind, source_id, when, *, actor="17", actor_kind="human", on_behalf_of=None):
    return ProviderEvent(
        provider="github",
        kind=kind,
        event_id=source_id,
        source_url="https://github.com/org/repo/pull/1" if kind == "pull_request" else f"https://github.com/org/repo/issues/1#{source_id}",
        repository="org/repo",
        actor_id=actor,
        actor_kind=actor_kind,
        occurred_at=when,
        on_behalf_of=on_behalf_of,
    )


def app_with_work(service):
    capabilities = MagicMock()
    capabilities.verify_run.return_value = SimpleNamespace(user_id="owner", tenant_id="tenant-a")
    services = workload_runtime(capabilities)
    app = FastAPI()
    app.include_router(chat_data_routes.router)
    app.dependency_overrides[chat_data_routes.runtime] = lambda: services
    app.dependency_overrides[get_activity_service] = lambda: service
    return app


def test_delegated_summary_merges_distinct_provider_history_and_preserves_run_provenance(monkeypatch):
    monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", "true")
    monkeypatch.delenv("ADP_TASK_API_READ_ENABLED", raising=False)
    invocation = InvocationItem(
        invocation_id="adp-run",
        invoked_at="2026-10-02T09:00:00Z",
        status="complete",
        source_url="https://github.com/org/repo/pull/1",
        repo="org/repo",
        issue_number=1,
    )
    service = MagicMock()
    service.query_work_by_user.return_value = WorkActivityPage(items=[invocation], direct_cursor="next", descendant_cursor=None)
    calls = []

    async def authorized(request, launch, start, end):
        calls.append((request.url.path, launch.user_id, launch.tenant_id, start.isoformat(), end.isoformat()))
        records = [
            event("pull_request", "pr-1", "2026-10-02T10:00:00Z"),
            event("pull_request", "pr-1", "2026-10-02T10:00:00Z"),
            event("review", "review-2", "2026-10-02T11:00:00Z", actor="bot", actor_kind="bot", on_behalf_of="17"),
            event("comment", "outside", "2026-10-05T11:00:00Z"),
        ]
        return ExternalRead(
            events=classify_events(records, {"github": {"17"}}),
            coverage=[
                {"source": "github", "status": "available", "reason": "queried"},
            ],
        )

    monkeypatch.setattr(chat_work, "read_external_work", authorized)
    with TestClient(app_with_work(service)) as client:
        response = client.post(URL, headers=HEADERS, json=WINDOW)
        assert response.status_code == 200, response.text
        result = response.json()
        assert response.headers["cache-control"] == "no-store"
        assert result["runs"][0]["invocation_id"] == "adp-run"
        assert result["issues"][0]["invocation_ids"] == ["adp-run"]
        assert [entry["event_kind"] for entry in result["external_events"]] == ["pull_request", "review"]
        assert [entry["human_work"] for entry in result["external_events"]] == [True, False]
        assert result["external_events"][0]["source_url"] == result["runs"][0]["evidence"]["source"]
        assert [entry["source_type"] for entry in result["timeline"]] == ["adp", "github", "github"]
        assert result["timeline"][1]["source_id"] == result["external_events"][0]["source_id"]
        assert result["status"] == "partial" and result["last_key"]
        continuation = client.post(URL, headers=HEADERS, json={**WINDOW, "last_key": result["last_key"]})
        assert continuation.status_code == 200
        assert continuation.json()["external_events"] == []
    assert calls == [(URL, "owner", "tenant-a", "2026-10-01T00:00:00+00:00", "2026-10-04T00:00:00+00:00")]


def test_default_external_reader_fails_closed_with_explicit_coverage(monkeypatch):
    monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", "true")
    monkeypatch.delenv("ADP_TASK_API_READ_ENABLED", raising=False)
    service = MagicMock()
    service.query_work_by_user.return_value = WorkActivityPage(items=[], direct_cursor=None, descendant_cursor=None)
    with TestClient(app_with_work(service)) as client:
        response = client.post(URL, headers=HEADERS, json=WINDOW)
    assert response.status_code == 200
    assert response.json()["external_events"] == []
    assert {(entry["source"], entry["reason"]) for entry in response.json()["coverage"]} >= {
        ("github", "authorization_not_configured"),
        ("gitlab", "authorization_not_configured"),
    }
    assert response.json()["status"] == "unavailable"
