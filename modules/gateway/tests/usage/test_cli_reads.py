"""Real ORM, serializer and permission predicates for CLI usage reads."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.usage import UsageLog
from src.usage import cli_reads as reads

START = datetime(2026, 9, 25, tzinfo=UTC)
END = START + timedelta(days=1)


def record(identifier, *, org="org-001", user="user-001", timestamp=START, confidence="verified", amount="0.123456", department="dept-001", run=None):
    return UsageLog(
        id=identifier,
        timestamp=timestamp,
        org_id=org,
        department_id=department,
        team_id="team-001",
        user_id=user,
        account_type="human",
        model="model-1",
        input_tokens=10,
        output_tokens=5,
        cost_usd=Decimal(amount),
        latency_ms=1,
        status_code=200,
        request_id="request-1",
        pricing_confidence=confidence,
        agent_run_id=run,
        pricing_decision={"secret": "must-not-escape"},
        model_decision={"prompt": "must-not-escape"},
        chat_log_s3_key="private-bucket-key",
    )


@pytest.fixture(autouse=True)
def identity(monkeypatch):
    monkeypatch.setattr(reads, "workspace_user", AsyncMock(return_value=SimpleNamespace(id="user-001")))


async def read(db, user, **kwargs):
    return await reads.read_records(
        db,
        user,
        org_id=kwargs.pop("org_id", None),
        start=START,
        end=END,
        request_id=None,
        cursor=kwargs.pop("cursor", None),
        limit=kwargs.pop("limit", 1),
        **kwargs,
    )


async def test_keyset_same_timestamp_and_tenant_owner_constraints(db_session, org_user_context):
    db_session.add_all([record("a"), record("b"), record("c", org="foreign"), record("d", user="foreign"), record("e", timestamp=END)])
    await db_session.commit()
    first = await read(db_session, org_user_context)
    second = await read(db_session, org_user_context, cursor=first.next_cursor)
    assert [r.id for r in first.items + second.items] == ["b", "a"]
    assert first.complete is False and second.complete is True
    payload = first.model_dump_json()
    assert "0.123456" in payload and "must-not-escape" not in payload and "private-bucket" not in payload
    assert first.items[0].cost.settlement == "unknown"
    assert first.items[0].root_human_id is None


async def test_legacy_zero_is_unknown(db_session, org_user_context):
    db_session.add(record("zero", confidence=None, amount="0"))
    await db_session.commit()
    result = await read(db_session, org_user_context)
    assert result.items[0].cost.amount is None
    assert result.items[0].cost.status == "unknown"
    assert reads.aggregate(result, "summary")["items"][0]["cost"]["amount"] is None


async def test_decimal_aggregate_and_unknown_subtotal(db_session, org_user_context):
    db_session.add_all([record("a", amount="0.100001"), record("b", amount="0.200002"), record("c", amount="0", confidence=None)])
    await db_session.commit()
    result = await read(db_session, org_user_context, limit=100)
    cost = reads.aggregate(result, "summary")["items"][0]["cost"]
    assert cost["amount"] == "0.300003"
    assert cost["status"] == "lower_bound"
    assert cost["unknown_records"] == 1


async def test_cursor_cannot_switch_scope_or_range(db_session, org_user_context):
    db_session.add_all([record("a"), record("b")])
    await db_session.commit()
    result = await read(db_session, org_user_context)
    with pytest.raises(HTTPException) as exc:
        await reads.read_records(
            db_session, org_user_context, org_id=None, start=START, end=END + timedelta(days=1), request_id=None, cursor=result.next_cursor, limit=1
        )
    assert exc.value.status_code == 422


@pytest.mark.parametrize(
    "role,org,department,allowed",
    [
        (AdminRole.MEMBER, "org-001", None, False),
        (AdminRole.ORG_ADMIN, "foreign", None, False),
        (AdminRole.ORG_ADMIN, "org-001", None, True),
        (AdminRole.DEPT_ADMIN, "org-001", "dept-001", True),
        (AdminRole.PLATFORM_ADMIN, "foreign", None, True),
    ],
)
async def test_actual_permission_predicate_managed_scope(db_session, org_user_context, monkeypatch, role, org, department, allowed):
    # Only the authoritative role resolver is replaced; check_permission and
    # its real permission/scope predicate execute against every role case.
    monkeypatch.setattr(AccessControl, "get_user_role", AsyncMock(return_value=(role, "org-001", department)))
    db_session.add_all([record("a", org=org), record("b", org=org, department="other-department")])
    await db_session.commit()
    if not allowed:
        with pytest.raises(HTTPException) as exc:
            await read(db_session, org_user_context, org_id=org, limit=100)
        assert exc.value.status_code == 403
    else:
        result = await read(db_session, org_user_context, org_id=org, limit=100)
        assert len(result.items) == (1 if role == AdminRole.DEPT_ADMIN else 2)
        assert all(row.org_id == org for row in result.items)


async def test_run_lookup_authorizes_activity_before_service_principal_usage(db_session, org_user_context, monkeypatch):
    from src.activity.service import ActivityService

    lookup = Mock(return_value=SimpleNamespace(root_human_id="user-001"))
    monkeypatch.setattr(ActivityService, "__init__", lambda self: None)
    monkeypatch.setattr(ActivityService, "get_invocation", lookup)
    db_session.add_all([record("a", user="worker-service", run="owned-run"), record("b", user="worker-service", run="foreign-run")])
    await db_session.commit()
    result = await read(db_session, org_user_context, run_id="owned-run", limit=100)
    lookup.assert_called_once_with("owned-run", user_id="user-001", tenant_id="org-001")
    assert [item.id for item in result.items] == ["a"]
    assert result.items[0].root_human_id == "user-001"
    lookup.return_value = None
    with pytest.raises(HTTPException) as exc:
        await read(db_session, org_user_context, run_id="foreign-run")
    assert exc.value.status_code == 404


@pytest.mark.parametrize("extra", ["org_id=foreign", "user_id=foreign", "include_content=true"])
async def test_http_rejects_own_scope_override(db_session, org_user_context, extra):
    app = FastAPI()
    app.include_router(reads.router, prefix="/usage")
    app.dependency_overrides[get_current_user] = lambda: org_user_context
    app.dependency_overrides[get_db] = lambda: db_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/usage/me/requests?start=2026-09-25T00:00:00Z&end=2026-09-26T00:00:00Z&{extra}")
    assert response.status_code == 422


@pytest.mark.parametrize("start,end", [(START.replace(tzinfo=None), END), (END, START), (START, START + timedelta(days=91))])
def test_range_validation(start, end):
    with pytest.raises(HTTPException) as exc:
        reads.utc_range(start, end)
    assert exc.value.status_code == 422


async def test_real_http_serialization_preserves_decimal_and_redacts(db_session, org_user_context):
    from src.usage.routes import router as production_router

    app = FastAPI()
    app.include_router(production_router)
    assert "/usage/me/{view}" in {route.path for route in app.routes}
    assert "/usage/managed/{org_id}/{view}" in {route.path for route in app.routes}
    app.dependency_overrides[get_current_user] = lambda: org_user_context
    app.dependency_overrides[get_db] = lambda: db_session
    db_session.add(record("precise", amount="1.000001"))
    await db_session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/usage/me/requests?start=2026-09-25T00:00:00Z&end=2026-09-26T00:00:00Z")
    assert response.status_code == 200
    assert response.json()["items"][0]["cost"]["amount"] == "1.000001"
    assert response.json()["items"][0]["timestamp"].endswith("Z")
    assert "must-not-escape" not in response.text


async def test_aggregate_bound_refuses_instead_of_silent_truncation(db_session, org_user_context, monkeypatch):
    monkeypatch.setattr(reads, "MAX_AGGREGATE_ROWS", 1)
    db_session.add_all([record("a"), record("b")])
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await reads.dispatch_read(db_session, org_user_context, "summary", None, START, END, None, None, 50)
    assert exc.value.status_code == 422


@pytest.mark.parametrize("days_ago,expected", [(1, "within_retention"), (90, "overlaps_retention"), (100, "before_retention")])
async def test_retention_window_metadata_distinguishes_empty_history_without_claiming_purge(
    db_session, org_user_context, monkeypatch, days_ago, expected
):
    observed = datetime(2026, 9, 25, 12, tzinfo=UTC)
    monkeypatch.setattr(reads, "observation_time", lambda: observed)
    monkeypatch.setattr(reads, "get_usage_config", lambda: SimpleNamespace(raw_log_retention_days=90))
    start = observed.replace(hour=0) - timedelta(days=days_ago)
    end = start + timedelta(days=1)
    result = await reads.read_records(db_session, org_user_context, org_id=None, start=start, end=end, request_id=None, cursor=None, limit=50)
    summary = reads.aggregate(result, "summary")
    assert result.items == [] and summary["items"] == []
    assert result.window_coverage == expected == summary["window_coverage"]
    assert result.observed_at == summary["observed_at"] == observed
    assert result.raw_retention_start == summary["raw_retention_start"] == observed - timedelta(days=90)
    assert summary["cost_status"] == "unknown"
    assert "expired" not in result.model_dump_json()


@pytest.mark.parametrize(
    "fault,expected",
    [
        (None, 200),
        ("foreign_owner", 404),
        ("foreign_tenant", 404),
        ("caller_mismatch", 404),
        ("revoked", 403),
        ("storage", 503),
        ("stale_generation", 404),
    ],
)
async def test_task_run_usage_uses_real_activity_adapter_and_policy(db_session, org_user_context, monkeypatch, fault, expected):
    from dataclasses import replace

    from src.activity import task_readthrough
    from src.activity.service import ActivityService
    from src.tasks import authz, errors
    from src.tasks.read_store import InMemoryTaskStore, TaskRecord

    invocation = "57e3ed64-0794-4df0-828f-565479f9ddac"
    task_id = "tsk_6011c464-98d4-4cb3-95f7-6cf7937c4819"
    task = TaskRecord(
        task_id,
        invocation,
        "org-001",
        "human:user-001",
        "agent-task-codex-developer",
        "completed",
        4,
        "2026-09-25T00:00:00Z",
        "2026-09-25T00:01:00Z",
        "2026-09-25T01:00:00Z",
    )
    if fault == "foreign_owner":
        task = replace(task, owner_principal_id="human:foreign")
    if fault == "foreign_tenant":
        task = replace(task, tenant_id="foreign")
    store = InMemoryTaskStore()
    store.tasks[task_id] = task
    if fault == "storage":
        store.fail = True
    if fault == "revoked":
        monkeypatch.setattr(store, "require_policy", Mock(side_effect=errors.disallowed_scope("revoked")))
    if fault == "stale_generation":
        monkeypatch.setattr(store, "resolve_invocation", lambda **kw: (task_id, 2))
    monkeypatch.setenv("ADP_TASK_API_READ_ENABLED", "true")
    monkeypatch.setattr(task_readthrough, "get_store", lambda: store)
    monkeypatch.setattr(authz, "authenticate", Mock(return_value=(org_user_context, frozenset({authz.SCOPE_READ}))))
    caller = AsyncMock(
        return_value=authz.Caller("human:other" if fault == "caller_mismatch" else "human:user-001", "org-001", frozenset({authz.SCOPE_READ}))
    )
    monkeypatch.setattr(authz, "resolve_caller", caller)
    monkeypatch.setattr(ActivityService, "__init__", lambda self: None)
    monkeypatch.setattr(ActivityService, "get_invocation", Mock(return_value=None))
    db_session.add_all(
        [
            record("owned-task", user="worker-service", run=invocation),
            record("other-run", user="worker-service", run="other"),
            record("other-tenant", org="foreign", user="worker-service", run=invocation),
        ]
    )
    await db_session.commit()
    app = FastAPI()
    app.include_router(reads.router, prefix="/usage")
    app.dependency_overrides[get_current_user] = lambda: org_user_context
    app.dependency_overrides[get_db] = lambda: db_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/usage/me/requests?start=2026-09-25T00:00:00Z&end=2026-09-26T00:00:00Z&run_id={invocation}")
    assert response.status_code == expected, response.text
    if expected == 200:
        body = response.json()
        assert [item["id"] for item in body["items"]] == ["owned-task"]
        assert body["items"][0]["root_human_id"] == "user-001"
        assert body["scope"]["coverage"] == "selected_run"
        caller.assert_awaited_once()
