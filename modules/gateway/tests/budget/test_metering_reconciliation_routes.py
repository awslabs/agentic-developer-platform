"""Administrator reconciliation requires a live human, protected grant and atomic evidence."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import psycopg2
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.admin.exceptions import AccessDeniedError
from src.auth.cognito_jwt import CognitoTokenClaims
from src.budget import metering_reconciliation_routes as routes
from src.shared.database import get_db
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.usage.metering_journal import ImmutableMeteringJournal
from tests.budget.test_metering_reconciliation_transactions import event_rows
from tests.budget.test_metering_scope_admission import admission_for, context
from tests.budget.test_metering_scope_admission import journal as base_journal
from tests.budget.test_metering_scope_admission import metering_db as base_metering_db
from tests.budget.test_metering_settlement_receipts import complete, ledger
from tests.migrations.conftest_postgres import to_async_url

pytest_plugins = ("tests.migrations.conftest_postgres",)


@pytest.fixture
def metering_db(pg_head_url):
    return base_metering_db.__wrapped__(pg_head_url)


@pytest.fixture
def journal():
    yield from base_journal.__wrapped__()


async def admin_app(pg_url, journal, monkeypatch):
    engine = create_async_engine(to_async_url(pg_url), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Organization(id="tenant-1", name="Metering fixture workspace"))
        await session.flush()
        session.add(User(id="human-operator", cognito_sub="operator-login", org_id="tenant-1", team_id="", email="operator@example.test"))
        await session.flush()
        session.add(TenantMembership(user_id="human-operator", tenant_id="tenant-1", role="org_admin", is_active=True))
        await session.commit()

    class ControlledValidator:
        def validate_token(self, token):
            if token == "expired-fixture":
                raise jwt.ExpiredSignatureError
            if token not in {"admin-fixture", "org-admin-fixture", "service-fixture", "session-expired-fixture"}:
                raise jwt.InvalidTokenError
            current = int(datetime.now(UTC).timestamp())
            return CognitoTokenClaims(
                sub="operator-login" if token != "service-fixture" else "service-subject",
                iss="https://fixture.invalid",
                client_id="fixture-web" if token != "service-fixture" else "service-client",
                token_use="access",
                exp=current - 5 if token == "session-expired-fixture" else current + 3600,
                iat=current - 60,
                username="operator-login",
                org_id="tenant-1",
                role="org_admin" if token == "org-admin-fixture" else "platform_admin",
                account_type="service" if token == "service-fixture" else "human",
            )

    app = FastAPI()

    @app.exception_handler(AccessDeniedError)
    async def access_denied(request, error):
        return JSONResponse(status_code=403, content={"detail": "Administrator authority required"})

    app.include_router(routes.router)
    monkeypatch.setattr("src.auth.dependencies._get_cognito_validator", lambda: ControlledValidator())
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: factory)
    monkeypatch.setattr("src.admin.platform_authority.current_cognito_admin", lambda subject, username="": subject == "operator-login")

    async def db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = db
    monkeypatch.setattr(routes, "get_accounting_session_factory", lambda: factory)
    monkeypatch.setenv("BG_METERING_JOURNAL_BUCKET", journal._bucket)
    monkeypatch.setattr(ImmutableMeteringJournal, "_s3", lambda self: journal._client)
    return app, engine


def body_for(receipt, *, revision=3, action="apply_trusted_receipt", key="admin-key-1"):
    body = {
        "tenant_id": receipt.tenant_id,
        "expected_revision": revision,
        "idempotency_key": key,
        "action": action,
        "reason": "verified provider evidence",
        "evidence_ref": receipt.key,
        "evidence_sha256": receipt.sha256,
    }
    if action == "apply_trusted_receipt":
        body.update(receipt_key=receipt.key, receipt_version=receipt.version_id, receipt_hash=receipt.sha256)
    return body


def grant(monkeypatch, receipt, body, *, actor="human-operator", expires=None):
    monkeypatch.setenv(
        "BG_METERING_INCIDENT_GRANTS",
        json.dumps(
            [
                {
                    "incident_id": "incident-fixture",
                    "actor_id": actor,
                    "tenant_id": receipt.tenant_id,
                    "request_id": receipt.request_id,
                    "action": body["action"],
                    "expected_revision": body["expected_revision"],
                    "evidence_ref": body["evidence_ref"],
                    "evidence_sha256": body["evidence_sha256"],
                    "evidence_version": receipt.version_id,
                    "expires_at": (expires or datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                }
            ]
        ),
    )


def access_rows(pg_url):
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT details->>'outcome', details->>'evidence_ref_sha256' FROM security_audit_logs "
            "WHERE event_type='metering_reconciliation_access' ORDER BY created_at, id"
        )
        return cursor.fetchall()


async def test_authorized_late_receipt_replays_without_reopening_journal(metering_db, journal, monkeypatch):
    admission, engine = admission_for(metering_db, journal)
    app_engine = None
    try:
        attempt, receipt, decision = await complete(admission, context(), "admin-route-late")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE metering_attempts SET state='unknown', final_version=NULL, final_sha256=NULL WHERE request_id='admin-route-late'")
        body = body_for(receipt)
        grant(monkeypatch, receipt, body)
        app, app_engine = await admin_app(metering_db, journal, monkeypatch)
        path = "/admin/metering/attempts/admin-route-late/reconcile"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer admin-fixture"}
        ) as client:
            first = await client.post(path, json=body)
            assert first.status_code == 200, first.text
            assert first.json() == {"disposition": "settled", "revision": 4, "spend_blocked": False, "exact_receipt": True}
            with monkeypatch.context() as failure:

                async def unavailable(*args, **kwargs):
                    raise TimeoutError("journal unavailable")

                failure.setattr(ImmutableMeteringJournal, "read_final", unavailable)
                replay = await client.post(path, json=body)
            assert replay.status_code == 200 and replay.json() == first.json()
            conflict = await client.post(path, json=body | {"reason": "different reason"})
            assert conflict.status_code == 409
            stale = await client.post(path, json=body | {"idempotency_key": "admin-key-2"})
            assert stale.status_code == 409
        assert ledger(metering_db, "admin-route-late")[1] == [(decision.ledger_cost, 3)]
        assert len(ledger(metering_db, "admin-route-late")[2:][0]) == 9
        assert len(event_rows(metering_db)) == 1
        assert len(access_rows(metering_db)) == 6
    finally:
        if app_engine is not None:
            await app_engine.dispose()
        await engine.dispose()


@pytest.mark.parametrize("change", ["not_admin", "service", "expired", "wrong_grant", "no_grant", "exception_action"])
async def test_forbidden_reconciliation_preserves_unknown_hold(metering_db, journal, monkeypatch, change):
    admission, engine = admission_for(metering_db, journal)
    app_engine = None
    try:
        attempt, receipt, _ = await complete(admission, context(), "admin-route-denied")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE metering_attempts SET state='unknown' WHERE request_id='admin-route-denied'")
        body = body_for(receipt)
        grant(monkeypatch, receipt, body, actor="different-operator" if change == "wrong_grant" else "human-operator")
        if change == "no_grant":
            monkeypatch.delenv("BG_METERING_INCIDENT_GRANTS")
        if change == "exception_action":
            body["action"] = "close_unknown_by_exception"
        app, app_engine = await admin_app(metering_db, journal, monkeypatch)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer admin-fixture"}
        ) as client:
            bearer = {"not_admin": "org-admin-fixture", "service": "service-fixture", "expired": "session-expired-fixture"}.get(change)
            response = await client.post(
                "/admin/metering/attempts/admin-route-denied/reconcile",
                json=body,
                headers={"Authorization": f"Bearer {bearer}"} if bearer else None,
            )
        assert response.status_code in (403, 422), response.text
        assert ledger(metering_db, "admin-route-denied")[0][0] == "unknown"
        assert ledger(metering_db, "admin-route-denied")[1:][0] == []
        assert event_rows(metering_db) == []
        if change != "exception_action":
            assert [row[0] for row in access_rows(metering_db)] == ["requested", "denied:403"]
    finally:
        if app_engine is not None:
            await app_engine.dispose()
        await engine.dispose()


async def test_incident_revoked_while_evidence_is_read_cannot_change_finances(metering_db, journal, monkeypatch):
    admission, engine = admission_for(metering_db, journal)
    app_engine = None
    try:
        attempt, receipt, _ = await complete(admission, context(), "admin-route-revoked")
        body = body_for(receipt)
        grant(monkeypatch, receipt, body)
        app, app_engine = await admin_app(metering_db, journal, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        original = ImmutableMeteringJournal.read_final

        async def delayed(self, *args, **kwargs):
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            return await original(self, *args, **kwargs)

        with monkeypatch.context() as timing:
            timing.setattr(ImmutableMeteringJournal, "read_final", delayed)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer admin-fixture"}
            ) as client:
                operation = asyncio.create_task(client.post("/admin/metering/attempts/admin-route-revoked/reconcile", json=body))
                await asyncio.wait_for(entered.wait(), 5)
                monkeypatch.delenv("BG_METERING_INCIDENT_GRANTS")
                release.set()
                response = await operation
        assert response.status_code == 403
        assert ledger(metering_db, "admin-route-revoked")[0][0] == "dispatch_armed"
        assert ledger(metering_db, "admin-route-revoked")[1] == []
        assert event_rows(metering_db) == []
        assert [row[0] for row in access_rows(metering_db)] == ["requested", "denied:403"]
    finally:
        if app_engine is not None:
            await app_engine.dispose()
        await engine.dispose()


async def test_prepared_cancellation_replays_but_armed_attempt_cannot_release(metering_db, journal, monkeypatch):
    from tests.budget.test_metering_scope_admission import prepare

    admission, engine = admission_for(metering_db, journal)
    app_engine = None
    try:
        prepared = await prepare(admission, context(), "admin-route-prepared")
        body = body_for(prepared.receipt, revision=2, action="confirm_not_dispatched")
        grant(monkeypatch, prepared.receipt, body)
        app, app_engine = await admin_app(metering_db, journal, monkeypatch)
        path = "/admin/metering/attempts/admin-route-prepared/reconcile"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer admin-fixture"}
        ) as client:
            accepted = await client.post(path, json=body)
            assert accepted.status_code == 200, accepted.text
            assert accepted.json() == {"disposition": "cancelled_unsubmitted", "revision": 3, "spend_blocked": False, "exact_receipt": False}
            replay = await client.post(path, json=body)
            assert replay.status_code == 200 and replay.json() == accepted.json()
        armed, _, _ = await complete(admission, context(), "admin-route-armed")
        armed_body = body_for(armed.receipt, revision=3, action="confirm_not_dispatched")
        grant(monkeypatch, armed.receipt, armed_body)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer admin-fixture"}
        ) as client:
            refused = await client.post("/admin/metering/attempts/admin-route-armed/reconcile", json=armed_body)
            assert refused.status_code == 422
        assert ledger(metering_db, "admin-route-prepared")[0][0] == "cancelled_unsubmitted"
        assert ledger(metering_db, "admin-route-armed")[0][0] == "dispatch_armed"
        assert [event[0] for event in event_rows(metering_db)] == ["confirm_not_dispatched"]
    finally:
        if app_engine is not None:
            await app_engine.dispose()
        await engine.dispose()


async def test_missing_journal_evidence_refuses_without_charge(metering_db, journal, monkeypatch):
    admission, engine = admission_for(metering_db, journal)
    app_engine = None
    try:
        _, receipt, _ = await complete(admission, context(), "admin-route-evidence-unavailable")
        body = body_for(receipt)
        grant(monkeypatch, receipt, body)
        app, app_engine = await admin_app(metering_db, journal, monkeypatch)
        with monkeypatch.context() as unavailable:

            async def fail_read(*args, **kwargs):
                raise TimeoutError("journal unavailable")

            unavailable.setattr(ImmutableMeteringJournal, "read_final", fail_read)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer admin-fixture"}
            ) as client:
                refused = await client.post("/admin/metering/attempts/admin-route-evidence-unavailable/reconcile", json=body)
        assert refused.status_code == 503
        assert ledger(metering_db, "admin-route-evidence-unavailable")[0][0] == "dispatch_armed"
        assert ledger(metering_db, "admin-route-evidence-unavailable")[1] == []
        assert event_rows(metering_db) == []
        assert [row[0] for row in access_rows(metering_db)] == ["requested", "denied:503"]
    finally:
        if app_engine is not None:
            await app_engine.dispose()
        await engine.dispose()


@pytest.mark.parametrize("authorization", [None, "Token admin-fixture", "Bearer unknown-fixture", "Bearer expired-fixture"])
async def test_unverified_bearer_never_enters_reconciliation(metering_db, journal, monkeypatch, authorization):
    admission, engine = admission_for(metering_db, journal)
    app_engine = None
    try:
        _, receipt, _ = await complete(admission, context(), "unverified-bearer")
        body = body_for(receipt)
        grant(monkeypatch, receipt, body)
        app, app_engine = await admin_app(metering_db, journal, monkeypatch)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/admin/metering/attempts/unverified-bearer/reconcile",
                json=body,
                headers={"Authorization": authorization} if authorization else {},
            )
        assert response.status_code == 401
        assert ledger(metering_db, "unverified-bearer")[0][0] == "dispatch_armed"
        assert ledger(metering_db, "unverified-bearer")[1] == []
        assert access_rows(metering_db) == []
    finally:
        if app_engine is not None:
            await app_engine.dispose()
        await engine.dispose()


@pytest.mark.parametrize("revocation", ["membership", "platform"])
async def test_revoked_authority_cannot_reconcile_with_platform_claim(metering_db, journal, monkeypatch, revocation):
    admission, engine = admission_for(metering_db, journal)
    app_engine = None
    try:
        _, receipt, _ = await complete(admission, context(), "revoked-member")
        body = body_for(receipt)
        grant(monkeypatch, receipt, body)
        app, app_engine = await admin_app(metering_db, journal, monkeypatch)
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            if revocation == "membership":
                cursor.execute("UPDATE tenant_memberships SET revoked_at=NOW() WHERE user_id='human-operator'")
            else:
                cursor.execute("INSERT INTO platform_admin_grants (cognito_sub, enabled, updated_at) VALUES ('operator-login', false, NOW())")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer admin-fixture"}
        ) as client:
            response = await client.post("/admin/metering/attempts/revoked-member/reconcile", json=body)
        assert response.status_code == 403
        assert ledger(metering_db, "revoked-member")[0][0] == "dispatch_armed"
        assert ledger(metering_db, "revoked-member")[1] == []
        assert event_rows(metering_db) == []
        assert [row[0] for row in access_rows(metering_db)] == ["requested", "denied:403"]
    finally:
        if app_engine is not None:
            await app_engine.dispose()
        await engine.dispose()
