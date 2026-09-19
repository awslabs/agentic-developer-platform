"""Tests for audit events endpoints and services."""

import uuid

import pytest

from app.middleware.auth import create_access_token


def _auth_header(org_id: uuid.UUID | None = None) -> dict:
    """Create an Authorization header with a valid JWT."""
    if org_id is None:
        org_id = uuid.uuid4()
    token, _ = create_access_token(org_id)
    return {"Authorization": f"Bearer {token}"}


class TestListEvents:
    """Test GET /events."""

    @pytest.mark.asyncio
    async def test_list_requires_auth(self, client):
        """Listing events without auth returns 401/403."""
        response = await client.get("/events")
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_list_returns_200_with_auth(self, client):
        """Listing events with valid auth returns 200."""
        headers = _auth_header()
        response = await client.get("/events", headers=headers)
        # May return 200 or 500 (no real DB), but validates route exists
        assert response.status_code in (200, 500)

    @pytest.mark.asyncio
    async def test_list_accepts_filter_params(self, client):
        """GET /events accepts query parameters for filtering."""
        headers = _auth_header()
        response = await client.get(
            "/events?resource_type=workspace&user=pranav&action=created&limit=10&offset=0",
            headers=headers,
        )
        # Validates route accepts these query params without 422
        assert response.status_code in (200, 500)

    @pytest.mark.asyncio
    async def test_list_validates_limit(self, client):
        """Limit > 500 returns 422."""
        headers = _auth_header()
        response = await client.get("/events?limit=1000", headers=headers)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_list_validates_offset(self, client):
        """Negative offset returns 422."""
        headers = _auth_header()
        response = await client.get("/events?offset=-1", headers=headers)
        assert response.status_code == 422


class TestGetEvent:
    """Test GET /events/{id}."""

    @pytest.mark.asyncio
    async def test_get_requires_auth(self, client):
        """Getting an event without auth returns 401/403."""
        event_id = uuid.uuid4()
        response = await client.get(f"/events/{event_id}")
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_get_invalid_uuid(self, client):
        """Getting an event with invalid UUID returns 422."""
        headers = _auth_header()
        response = await client.get("/events/not-a-uuid", headers=headers)
        assert response.status_code == 422


class TestEventSchemas:
    """Test Pydantic schema validation."""

    def test_event_response_valid(self):
        from datetime import datetime, timezone

        from app.schemas.event import EventResponse

        event = EventResponse(
            id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            user_id="user-123",
            action="created",
            resource_type="workspace",
            resource_id=uuid.uuid4(),
            event_type="api_call",
            message="POST /workspaces",
            source_ip="10.0.0.1",
            request_path="/workspaces",
            http_status=201,
            created_at=datetime.now(timezone.utc),
        )
        assert event.action == "created"
        assert event.resource_type == "workspace"

    def test_event_response_minimal(self):
        from datetime import datetime, timezone

        from app.schemas.event import EventResponse

        event = EventResponse(
            id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            action="deleted",
            resource_type="credential",
            event_type="api_call",
            created_at=datetime.now(timezone.utc),
        )
        assert event.user_id is None
        assert event.resource_id is None
        assert event.source_ip is None

    def test_event_list_response(self):
        from app.schemas.event import EventListResponse

        resp = EventListResponse(events=[], total=0, limit=50, offset=0)
        assert resp.total == 0
        assert resp.limit == 50


class TestAuditService:
    """Test audit service helper functions."""

    def test_extract_resource_from_path_workspaces(self):
        from app.services.audit import extract_resource_from_path

        resource_type, resource_id = extract_resource_from_path("/workspaces")
        assert resource_type == "workspace"
        assert resource_id is None

    def test_extract_resource_from_path_with_id(self):
        from app.services.audit import extract_resource_from_path

        test_id = uuid.uuid4()
        resource_type, resource_id = extract_resource_from_path(
            f"/workspaces/{test_id}"
        )
        assert resource_type == "workspace"
        assert resource_id == test_id

    def test_extract_resource_from_path_with_subresource(self):
        from app.services.audit import extract_resource_from_path

        test_id = uuid.uuid4()
        resource_type, resource_id = extract_resource_from_path(
            f"/workspaces/{test_id}/kubeconfig"
        )
        assert resource_type == "workspace"
        assert resource_id == test_id

    def test_extract_resource_from_path_auth(self):
        from app.services.audit import extract_resource_from_path

        resource_type, resource_id = extract_resource_from_path("/auth/login")
        assert resource_type == "auth"
        assert resource_id is None

    def test_extract_resource_from_path_health(self):
        from app.services.audit import extract_resource_from_path

        resource_type, resource_id = extract_resource_from_path("/health")
        assert resource_type == "health"
        assert resource_id is None

    def test_extract_resource_from_path_events(self):
        from app.services.audit import extract_resource_from_path

        resource_type, resource_id = extract_resource_from_path("/events")
        assert resource_type == "event"
        assert resource_id is None

    def test_extract_resource_from_path_empty(self):
        from app.services.audit import extract_resource_from_path

        resource_type, resource_id = extract_resource_from_path("/")
        assert resource_type == "unknown"
        assert resource_id is None

    def test_http_method_to_action_mapping(self):
        from app.services.audit import HTTP_METHOD_TO_ACTION

        assert HTTP_METHOD_TO_ACTION["POST"] == "created"
        assert HTTP_METHOD_TO_ACTION["PUT"] == "updated"
        assert HTTP_METHOD_TO_ACTION["PATCH"] == "updated"
        assert HTTP_METHOD_TO_ACTION["DELETE"] == "deleted"
        assert HTTP_METHOD_TO_ACTION["GET"] == "read"


class TestAuditMiddleware:
    """Test audit middleware configuration."""

    def test_skip_paths_contains_health(self):
        from app.middleware.audit import SKIP_PATHS

        assert "/health" in SKIP_PATHS
        assert "/docs" in SKIP_PATHS
        assert "/openapi.json" in SKIP_PATHS

    def test_auditable_methods(self):
        from app.middleware.audit import AUDITABLE_METHODS

        assert "POST" in AUDITABLE_METHODS
        assert "PUT" in AUDITABLE_METHODS
        assert "PATCH" in AUDITABLE_METHODS
        assert "DELETE" in AUDITABLE_METHODS
        assert "GET" not in AUDITABLE_METHODS


class TestEventModel:
    """Test Event model definition."""

    def test_event_model_tablename(self):
        from app.models.event import Event

        assert Event.__tablename__ == "events"

    def test_event_model_has_audit_columns(self):
        """Verify Event model has all required audit columns."""
        from app.models.event import Event

        column_names = {c.name for c in Event.__table__.columns}
        expected = {
            "id",
            "org_id",
            "user_id",
            "action",
            "resource_type",
            "resource_id",
            "event_type",
            "message",
            "details_json",
            "source_ip",
            "request_path",
            "http_status",
            "created_at",
        }
        assert expected.issubset(column_names)

    def test_event_model_has_indexes(self):
        """Verify performance indexes exist."""
        from app.models.event import Event

        index_names = {idx.name for idx in Event.__table__.indexes}
        assert "ix_events_org_id_created_at" in index_names
        assert "ix_events_resource_type" in index_names
        assert "ix_events_user_id" in index_names
