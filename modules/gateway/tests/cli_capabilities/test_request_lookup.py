"""Gateway request lookup is tenant-scoped, permissioned and redacted."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.admin.exceptions import AccessDeniedError
from src.admin.models import RequestLog
from src.auth.dependencies import get_current_user
from src.cli_capabilities.routes import get_access_control, router
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext


class Access:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.asked = []

    async def check_permission(self, caller, permission, target_org_id=None):
        self.asked.append((permission.value, target_org_id))
        if not self.allowed:
            raise AccessDeniedError()
        return True


class Scalars:
    def __init__(self, row):
        self.row = row

    def first(self):
        return self.row


class Result:
    def __init__(self, row):
        self.row = row

    def scalars(self):
        return Scalars(self.row)


class Database:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        params = statement.compile().params
        request_id = next(value for key, value in params.items() if "request_id" in key)
        org_id = next(value for key, value in params.items() if "org_id" in key)
        row = next((item for item in self.rows if item.request_id == request_id and item.org_id == org_id), None)
        return Result(row)


def caller(org_id="org-alpha"):
    return TokenContext(
        user_id="admin-alpha",
        org_id=org_id,
        team_id="team",
        department_id="department",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def row(request_id="request-1", org_id="org-alpha"):
    return RequestLog(
        id=f"log-{request_id}",
        request_id=request_id,
        org_id=org_id,
        user_id="private-user-id",
        timestamp=datetime(2026, 9, 22, tzinfo=UTC),
        method="POST",
        path="/v1/messages",
        query_params={"api_key": "must-not-escape"},
        status_code=503,
        response_time_ms=42,
        error_message="provider secret topology",
        client_ip="192.0.2.1",
        user_agent="private-agent",
    )


def client(database, access):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = caller
    app.dependency_overrides[get_access_control] = lambda: access
    app.dependency_overrides[get_db] = lambda: database
    return TestClient(app)


def test_request_lookup_uses_gateway_request_id_and_returns_only_redacted_metadata():
    database, authority = Database([row()]), Access()
    response = client(database, authority).get("/me/cli-requests/request-1")

    assert response.status_code == 200
    assert response.json() == {
        "request_id": "request-1",
        "timestamp": "2026-09-22T00:00:00Z",
        "method": "POST",
        "path": "/v1/messages",
        "status_code": 503,
        "response_time_ms": 42,
    }
    assert authority.asked == [("logs:read", "org-alpha")]
    assert "must-not-escape" not in response.text
    assert "private-user-id" not in response.text


def test_foreign_tenant_request_is_filtered_in_the_database_query():
    database = Database([row("foreign", "org-beta")])
    response = client(database, Access()).get("/me/cli-requests/foreign")

    assert response.status_code == 404
    statement = str(database.statements[0])
    assert "request_logs.request_id" in statement
    assert "request_logs.org_id" in statement


def test_denied_absent_and_foreign_requests_are_byte_identical():
    denied = client(Database([row("foreign", "org-beta")]), Access(False)).get("/me/cli-requests/foreign")
    absent = client(Database(), Access()).get("/me/cli-requests/foreign")
    foreign = client(Database([row("foreign", "org-beta")]), Access()).get("/me/cli-requests/foreign")

    assert denied.status_code == absent.status_code == foreign.status_code == 404
    assert denied.content == absent.content == foreign.content
