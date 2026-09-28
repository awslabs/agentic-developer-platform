"""Mounted provenance boundary using the actual canonical run-identity resolver.

Authentication/flow validation is a fixed synthetic runtime fixture. Its exact
DynamoDB GetItem reads use moto; request-body correlation and invocation values
never choose authority. Downstream membership is real SQLite for positive cases.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.agentauth.bootstrap import BootstrapRefusedError
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.provenance_routes import router
from src.shared.config import Settings
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.provenance import ActionProvenance

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_TABLE = "fixture-provenance-events"
_REGION = "us-east-1"
_CORR = "owned-chain"
_EVENT = "authenticated-run"
_ARRIVED = "2026-09-01T10:00:00Z"


def _body(**overrides):
    value = dict(
        actor_user_id="user-bot",
        triggered_by=None,
        root_human_id="user-alice",
        is_human_rooted=True,
        action_kind="webhook_trigger",
        source_event={"source": "fixture"},
        correlation_id=_CORR,
        org_id="org-test",
        parent_invocation_id=None,
    )
    value.update(overrides)
    return value


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def db() -> AsyncSession:
    engine = create_async_engine(
        TEST_DB_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        import src.shared.models.audit  # noqa: F401
        import src.shared.models.provenance  # noqa: F401
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        rows: list = []
        # Two orgs, both on the DEFAULT trigger policy (settings={}). That default
        # is what makes this test meaningful: the pre-existing membership gate
        # authorizes any known actor for either org, so a refusal below can only
        # come from the chain-attribution check.
        for org_id in ("org-test", "org-other", "org-acme"):
            rows.append(
                Organization(
                    id=org_id,
                    name=org_id,
                    aws_accounts=[],
                    role_mappings={},
                    settings={},
                    github_installation_ids=[],
                    cognito_client_ids=[],
                )
            )
            rows.append(Department(id=f"dept-{org_id}", org_id=org_id, name="Eng"))
            rows.append(Team(id=f"team-{org_id}", org_id=org_id, department_id=f"dept-{org_id}", name="Eng"))
        for user_id, org_id in (
            ("user-alice", "org-test"),
            ("user-bot", "org-test"),
            ("user-mallory", "org-other"),
            ("user-bot-7f3a", "org-acme"),
            ("user-human-9c21", "org-acme"),
        ):
            rows.append(
                User(
                    id=user_id,
                    org_id=org_id,
                    team_id=f"team-{org_id}",
                    email=f"{user_id}@test.com",
                )
            )
        session.add_all(rows)
        await session.commit()
        yield session

    await engine.dispose()


@pytest.fixture
def boundary(db, monkeypatch):
    """Real helper and DynamoDB reads; fixed authentication and execution key."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=_REGION)
        table = resource.create_table(
            TableName=_TABLE,
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}, {"AttributeName": "arrived_at", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # Deliberately no correlation-index: the new authority contract never queries it.
        origin = dict(
            event_id=_EVENT,
            arrived_at=_ARRIVED,
            tenant_id="org-test",
            actor_user_id="user-bot",
            correlation_id=_CORR,
            root_human_id="user-alice",
            is_human_rooted=True,
        )
        table.put_item(Item=origin)
        ddb = boto3.client("dynamodb", region_name=_REGION)
        canonical_client = MagicMock()
        canonical_client.get_item.side_effect = ddb.get_item
        caller = SimpleNamespace(invocation_id=_EVENT, tenant_id="org-test")
        grant = SimpleNamespace(tenant_id="org-test", authority=SimpleNamespace(org_id="org-test", human_id="user-alice"))
        runtime = SimpleNamespace(
            authenticate=MagicMock(return_value=(None, caller, None, grant)),
            validate_flow=AsyncMock(),
            store=SimpleNamespace(
                _read=MagicMock(return_value={"arrived_at": {"S": _ARRIVED}}),
                client=canonical_client,
            ),
        )
        monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)
        monkeypatch.setattr("src.shared.config.get_settings", lambda: Settings(webhook_events_table=_TABLE, aws_region=_REGION))
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[verify_internal_or_irsa] = lambda: None

        async def session():
            yield db

        app.dependency_overrides[get_db] = session
        client = TestClient(app, raise_server_exceptions=False)
        endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/internal/v1/provenance")
        yield SimpleNamespace(client=client, runtime=runtime, origin=origin, table=table, endpoint=endpoint, monkeypatch=monkeypatch)


def _post(boundary, body=None):
    return boundary.client.post(
        "/internal/v1/provenance", json=body or _body(), headers={"X-ADP-Run-Credential": "fixture", "X-ADP-Workload-Proof": "fixture"}
    )


def _forbid_downstream(boundary):
    guard = AsyncMock(side_effect=AssertionError("identity denial must precede policy/database write"))
    boundary.monkeypatch.setitem(boundary.endpoint.__globals__, "_actor_is_member_of", guard)
    return guard


@pytest.mark.asyncio
async def test_truthful_identity_is_persisted_with_exact_origin_key(boundary, db):
    response = _post(boundary)
    assert response.status_code == 201, response.text
    row = (await db.execute(select(ActionProvenance))).scalar_one()
    assert (row.org_id, row.actor_user_id, row.correlation_id) == ("org-test", "user-bot", _CORR)
    call = boundary.runtime.store.client.get_item.call_args.kwargs
    assert call["Key"] == {"event_id": {"S": _EVENT}, "arrived_at": {"S": _ARRIVED}}
    assert call["ConsistentRead"] is True
    boundary.runtime.store._read.assert_called_once_with("TENANT#org-test", "EXEC#" + _EVENT)
    boundary.runtime.validate_flow.assert_awaited_once()


@pytest.mark.parametrize(
    "field,value",
    [
        ("org_id", "org-other"),
        ("actor_user_id", "user-mallory"),
        ("correlation_id", "foreign-chain"),
        ("correlation_id", ""),
        ("root_human_id", "user-mallory"),
        ("is_human_rooted", False),
        ("parent_invocation_id", "foreign-parent"),
        ("triggered_by", "user-mallory"),
    ],
)
def test_every_body_identity_mismatch_is_denied_before_policy(boundary, field, value):
    guard = _forbid_downstream(boundary)
    response = _post(boundary, _body(**{field: value}))
    assert response.status_code == 403
    assert response.json() == {"detail": "provenance does not match authenticated run"}
    guard.assert_not_awaited()


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", None),
        ("tenant_id", "org-other"),
        ("actor_user_id", None),
        ("correlation_id", None),
        ("is_human_rooted", None),
        ("is_human_rooted", "true"),
        ("root_human_id", None),
        ("root_human_id", "user-mallory"),
    ],
)
def test_incomplete_or_contradictory_origin_is_not_replaced_by_body(boundary, field, value):
    guard = _forbid_downstream(boundary)
    row = boundary.origin.copy()
    if value is None:
        row.pop(field)
    else:
        row[field] = value
    boundary.table.put_item(Item=row)
    assert _post(boundary).status_code == 403
    guard.assert_not_awaited()


@pytest.mark.parametrize("fault", ["origin", "execution", "authentication", "flow"])
def test_missing_canonical_authority_denies_even_same_org(boundary, fault):
    guard = _forbid_downstream(boundary)
    if fault == "origin":
        boundary.table.delete_item(Key={"event_id": _EVENT, "arrived_at": _ARRIVED})
    elif fault == "execution":
        boundary.runtime.store._read.return_value = {}
    elif fault == "authentication":
        boundary.runtime.authenticate.side_effect = BootstrapRefusedError("fixture refusal")
    else:
        boundary.runtime.validate_flow.side_effect = BootstrapRefusedError("fixture refusal")
    assert _post(boundary).status_code == 403
    guard.assert_not_awaited()


def test_origin_store_failure_is_unavailable_not_authorized(boundary):
    guard = _forbid_downstream(boundary)
    boundary.runtime.store.client.get_item.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "fixture"}}, "GetItem"
    )
    assert _post(boundary).status_code == 503
    guard.assert_not_awaited()


@pytest.mark.parametrize("arrived", ["2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z"])
def test_other_rows_in_same_chain_cannot_change_authenticated_origin(boundary, arrived):
    other = dict(boundary.origin, event_id="other-run", arrived_at=arrived, tenant_id="org-other", root_human_id="user-mallory")
    boundary.table.put_item(Item=other)
    assert _post(boundary).status_code == 201
    assert _post(boundary, _body(org_id="org-other", root_human_id="user-mallory")).status_code == 403
    assert all(call.kwargs["Key"]["event_id"] == {"S": _EVENT} for call in boundary.runtime.store.client.get_item.call_args_list)


def test_parent_and_trigger_are_taken_from_server_origin(boundary):
    row = dict(boundary.origin, parent_invocation_id="owned-parent", triggered_by="user-alice")
    boundary.table.put_item(Item=row)
    assert _post(boundary, _body(parent_invocation_id="owned-parent", triggered_by="user-alice")).status_code == 201
    assert _post(boundary).status_code == 403


def test_service_origin_cannot_be_upgraded_to_human_authority(boundary):
    boundary.table.put_item(Item=dict(boundary.origin, root_human_id="user-bot", is_human_rooted=False))
    assert _post(boundary, _body(root_human_id="user-bot", is_human_rooted=False)).status_code == 201
    assert _post(boundary).status_code == 403


def test_valid_shared_secret_still_cannot_enter_provenance(boundary):
    boundary.client.app.dependency_overrides.pop(verify_internal_or_irsa)
    boundary.monkeypatch.setattr("src.internal.auth_deps.get_settings", lambda: Settings(internal_api_key="known-test-key"))
    response = boundary.client.post("/internal/v1/provenance", json=_body(), headers={"X-Internal-Api-Key": "known-test-key"})
    assert response.status_code == 403
    boundary.runtime.authenticate.assert_not_called()


def test_no_runtime_flag_can_disable_binding():
    assert "enforce_provenance_chain_attribution" not in Settings.model_fields
