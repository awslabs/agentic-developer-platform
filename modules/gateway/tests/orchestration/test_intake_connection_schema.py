"""Intake consumes the connection service's real response contract."""

from unittest.mock import AsyncMock

import pytest

from src.admin.connections import service
from src.admin.connections.schemas import ConnectionsListResponse, ConnectionVerification, GitHubConnectionItem
from src.orchestration.intake_routes import _available_repositories

from .test_intake_routes import ORG, OTHER_ORG, SESSIONS, client, token_context


def connection(*, tenant_id=ORG, verification=None):
    return GitHubConnectionItem(
        installation_id=22,
        account_login="acme",
        account_type="Organization",
        repository_selection="selected",
        repositories=["acme/api"],
        configure_url="https://github.com/settings/installations/22",
        tenant_id=tenant_id,
        verification=verification,
    )


@pytest.mark.parametrize("live", [True, False, None, "absent"])
async def test_repository_provenance_comes_from_nested_verification(monkeypatch, live):
    verification = None if live == "absent" else ConnectionVerification(repositories_live=live)
    response = ConnectionsListResponse(
        connections=[
            connection(verification=verification),
            connection(tenant_id=OTHER_ORG, verification=ConnectionVerification(repositories_live=True)),
        ]
    )
    listing = AsyncMock(return_value=response)
    monkeypatch.setattr(service, "list_connections", listing)
    caller = token_context()
    database = object()

    assert await _available_repositories(caller, database) == [(22, ["acme/api"], live is True)]
    listing.assert_awaited_once_with(
        caller_org_id=ORG,
        caller_user_id=caller.user_id,
        db=database,
        member_tenant_ids=[ORG],
        caller_is_admin=caller.is_admin,
    )


def test_start_with_real_connection_response_dispatches_one_planning_turn(monkeypatch):
    monkeypatch.setattr(
        service,
        "list_connections",
        AsyncMock(return_value=ConnectionsListResponse(connections=[connection(verification=ConnectionVerification(repositories_live=True))])),
    )
    http, ingest = client()

    response = http.post(SESSIONS, json={"message": "Add the version endpoint", "repository": "acme/api"})

    assert response.status_code == 202, response.text
    assert len(ingest.payloads) == 1
    assert ingest.payloads[0]["repository"] == "acme/api"


def test_foreign_connection_cannot_start_a_planning_turn(monkeypatch):
    monkeypatch.setattr(
        service,
        "list_connections",
        AsyncMock(return_value=ConnectionsListResponse(connections=[connection(tenant_id=OTHER_ORG)])),
    )
    http, ingest = client()

    response = http.post(SESSIONS, json={"message": "Add the version endpoint", "repository": "acme/api"})

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == "repository_not_connected"
    assert ingest.calls == []
