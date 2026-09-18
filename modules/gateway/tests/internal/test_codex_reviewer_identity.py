import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException
from starlette.requests import Request

from src.internal import codex_reviewer_identity as identity


def _row(**overrides):
    return {
        "event_id": "delivery-1",
        "arrived_at": "2026-09-18T20:00:00Z",
        "tenant_id": "tenant-1",
        "installation_id": "123",
        "repo": "aws-e/adp",
        "persona": "codex-reviewer",
        "event_type": "pull_request",
        "action": "opened",
        "status": "in_progress",
        **overrides,
    }


def _bind(monkeypatch, row):
    table = Mock()
    table.query.return_value = {"Items": [row] if row is not None else []}
    monkeypatch.setattr(identity, "_table", lambda *_args: table)
    return identity.load_codex_reviewer_binding(
        invocation_id="delivery-1",
        requested_installation_id=123,
        requested_repository="aws-e/adp",
        table_name="events",
        region="us-east-1",
    )


def test_binds_dedicated_reviewer_to_ingress_assignment(monkeypatch):
    binding = _bind(monkeypatch, _row())
    assert binding == identity.CodexReviewerBinding(
        invocation_id="delivery-1",
        arrived_at="2026-09-18T20:00:00Z",
        tenant_id="tenant-1",
        installation_id=123,
        repository="aws-e/adp",
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"repo": "aws-e/other"},
        {"persona": "reviewer"},
        {"event_type": "issue_comment"},
        {"action": "closed"},
        {"status": "complete"},
        {"installation_id": "456"},
    ],
)
def test_refuses_nonmatching_or_inactive_assignment(monkeypatch, changed):
    with pytest.raises(HTTPException) as refused:
        _bind(monkeypatch, _row(**changed))
    assert refused.value.status_code == 403


def test_lookup_failure_is_unavailable_not_an_authorization_fallback(monkeypatch):
    table = Mock()
    table.query.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "nope"}},
        "Query",
    )
    monkeypatch.setattr(identity, "_table", lambda *_args: table)
    with pytest.raises(HTTPException) as unavailable:
        identity.load_codex_reviewer_binding(
            invocation_id="delivery-1",
            requested_installation_id=123,
            requested_repository="aws-e/adp",
            table_name="events",
            region="us-east-1",
        )
    assert unavailable.value.status_code == 503


@pytest.mark.parametrize(
    ("scopes", "contents"),
    [([], "read"), (["codex:branch-write"], "write"), (["codex:merge"], "write")],
)
async def test_broker_permission_is_derived_from_registered_identity(monkeypatch, scopes, contents):
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    request._body = json.dumps(
        {
            "installation_id": 123,
            "repo_owner": "aws-e",
            "repo_name": "adp",
            "invocation_id": "delivery-1",
        }
    ).encode()
    request.state.token_context = SimpleNamespace(
        user_id="agent-codex-reviewer",
        scope="internal",
        credential_scopes=scopes,
    )
    monkeypatch.setattr(
        identity,
        "get_settings",
        lambda: SimpleNamespace(webhook_events_table="events", aws_region="us-east-1"),
    )
    monkeypatch.setattr(
        identity,
        "load_codex_reviewer_binding",
        lambda **_kwargs: identity.CodexReviewerBinding(
            invocation_id="delivery-1",
            arrived_at="2026-09-18T20:00:00Z",
            tenant_id="tenant-1",
            installation_id=123,
            repository="aws-e/adp",
        ),
    )

    await identity.verify_codex_reviewer_broker(request)

    assert request.state.agent_authorized_action.value == "review"
    assert request.state.agent_github_permissions["contents"] == contents
    assert request.state.agent_github_permissions["checks"] == "read"
