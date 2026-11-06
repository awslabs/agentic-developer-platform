"""Admin actions share the trusted receipt transaction and ordered scope locks."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import psycopg2
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.budget.metering_reconciliation import (
    MeteringReconciliationCommand,
    MeteringReconciliationConflictError,
    cancel_prepared_attempt,
)
from src.budget.metering_settlement import settle_metering_receipt
from src.orchestration.provider_quotes import TrustedUsage
from tests.budget.test_metering_scope_admission import context, prepare
from tests.budget.test_metering_scope_admission import journal as base_journal
from tests.budget.test_metering_scope_admission import metering_db as base_metering_db
from tests.budget.test_metering_settlement_receipts import complete, ledger
from tests.proxy.test_mantle_metering_admission import USAGE

pytest_plugins = ("tests.migrations.conftest_postgres",)


@pytest.fixture
def metering_db(pg_head_url):
    return base_metering_db.__wrapped__(pg_head_url)


@pytest.fixture
def journal():
    yield from base_journal.__wrapped__()


def command(monkeypatch, attempt, *, action, key="admin-key-1", revision=3, evidence=None):
    evidence = evidence or attempt.receipt
    monkeypatch.setenv(
        "BG_METERING_INCIDENT_GRANTS",
        json.dumps(
            [
                {
                    "incident_id": "incident-fixture",
                    "actor_id": "human-operator",
                    "tenant_id": evidence.tenant_id,
                    "request_id": evidence.request_id,
                    "action": action,
                    "expected_revision": revision,
                    "evidence_ref": evidence.key,
                    "evidence_sha256": evidence.sha256,
                    "evidence_version": evidence.version_id,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                }
            ]
        ),
    )
    return MeteringReconciliationCommand(
        actor_id="human-operator",
        incident_id="incident-fixture",
        expected_revision=revision,
        idempotency_key=key,
        payload_sha256="c" * 64,
        action=action,
        reason="verified incident",
        evidence_ref=evidence.key,
        evidence_sha256=evidence.sha256,
        evidence_version=evidence.version_id,
    )


def event_rows(pg_url):
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT action, prior_revision, new_revision, disposition, receipt_sha256 FROM metering_reconciliation_events ORDER BY created_at, id"
        )
        return cursor.fetchall()


async def test_admin_late_receipt_settles_once_with_atomic_event_and_replay(metering_db, journal, monkeypatch):
    from tests.budget.test_metering_scope_admission import admission_for

    admission, engine = admission_for(metering_db, journal)
    try:
        attempt, receipt, decision = await complete(admission, context(), "admin-late")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE metering_attempts SET state='unknown', final_version=NULL, final_sha256=NULL WHERE request_id='admin-late'")
        admin = command(monkeypatch, attempt, action="apply_trusted_receipt", evidence=receipt)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        results = await asyncio.gather(
            settle_metering_receipt(
                factory, journal, receipt=receipt, admission=attempt.receipt, scope_keys=tuple(sorted(attempt.scope_revisions)), admin_command=admin
            ),
            settle_metering_receipt(
                factory, journal, receipt=receipt, admission=attempt.receipt, scope_keys=tuple(sorted(attempt.scope_revisions)), admin_command=admin
            ),
        )
        assert results == [{"disposition": "settled", "revision": 4, "spend_blocked": False, "exact_receipt": True}] * 2
        assert ledger(metering_db, "admin-late")[0:2] == (("settled", receipt.version_id), [(decision.ledger_cost, 3)])
        assert len(ledger(metering_db, "admin-late")[2]) == 9
        assert len(ledger(metering_db, "admin-late")[3]) == 1
        assert event_rows(metering_db) == [("apply_trusted_receipt", 3, 4, "settled", receipt.sha256)]
        with pytest.raises(MeteringReconciliationConflictError):
            await settle_metering_receipt(
                factory,
                journal,
                receipt=receipt,
                admission=attempt.receipt,
                scope_keys=tuple(sorted(attempt.scope_revisions)),
                admin_command=command(monkeypatch, attempt, action="apply_trusted_receipt", key="different-key", evidence=receipt),
            )
        assert len(event_rows(metering_db)) == 1
    finally:
        await engine.dispose()


async def test_only_prepared_admission_can_cancel_and_late_valid_receipt_quarantines(metering_db, journal, monkeypatch):
    from pricing_policy import RoutingEvidence
    from tests.budget.test_metering_scope_admission import admission_for
    from tests.proxy.test_mantle_metering_admission import MODEL

    admission, engine = admission_for(metering_db, journal)
    try:
        attempt = await prepare(admission, context(), "never-armed")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        cancelled = await cancel_prepared_attempt(
            factory,
            journal,
            admission=attempt.receipt,
            scope_keys=tuple(sorted(attempt.scope_revisions)),
            command=command(monkeypatch, attempt, action="confirm_not_dispatched", revision=2),
        )
        assert cancelled == {"disposition": "cancelled_unsubmitted", "revision": 3, "spend_blocked": False, "exact_receipt": False}
        assert len(event_rows(metering_db)) == 1
        routing = RoutingEvidence(
            original_model_id=MODEL,
            billing_model_id=MODEL,
            forwarded_model_id=MODEL,
            endpoint_host="bedrock-mantle.us-east-1.api.aws",
            endpoint_region="us-east-1",
            geography="in_region",
            served_service_tier_raw="standard",
        )
        decision = admission.price_final(
            pricing_basis=attempt.pricing_basis,
            usage=USAGE,
            routing=routing,
            request_id="never-armed",
            tenant_id="tenant-1",
            bound=attempt.quote.total_usd,
        )
        receipt = await journal.write_final(
            admission=attempt.receipt,
            usage=USAGE,
            trusted=TrustedUsage(USAGE["input_tokens"], USAGE["output_tokens"], True),
            provider_request_id="contradictory-provider",
            deadline=asyncio.get_running_loop().time() + 5,
            pricing_decision=decision.to_dict(),
            completed_at=datetime.now(UTC).isoformat(),
            latency_ms=12,
        )
        result = await settle_metering_receipt(
            factory,
            journal,
            receipt=receipt,
            admission=attempt.receipt,
            scope_keys=tuple(sorted(attempt.scope_revisions)),
            admin_command=command(monkeypatch, attempt, action="apply_trusted_receipt", key="admin-key-2", evidence=receipt),
        )
        assert result == {"disposition": "quarantined", "revision": 4, "spend_blocked": True, "exact_receipt": True}
        assert ledger(metering_db, "never-armed") == (("quarantined", receipt.version_id), [], [], [])
        assert [row[0] for row in event_rows(metering_db)] == ["confirm_not_dispatched", "apply_trusted_receipt"]
    finally:
        await engine.dispose()


async def test_settling_one_attempt_does_not_claim_another_shared_unknown_is_unblocked(metering_db, journal, monkeypatch):
    from tests.budget.test_metering_scope_admission import admission_for

    admission, engine = admission_for(metering_db, journal)
    try:
        first, first_receipt, _ = await complete(admission, context(), "first-overlap")
        second, second_receipt, _ = await complete(admission, context(), "second-overlap")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE metering_attempts SET state='unknown' WHERE request_id IN ('first-overlap', 'second-overlap')")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        admin = command(monkeypatch, first, action="apply_trusted_receipt", evidence=first_receipt)
        result = await settle_metering_receipt(
            factory,
            journal,
            receipt=first_receipt,
            admission=first.receipt,
            scope_keys=tuple(sorted(first.scope_revisions)),
            admin_command=admin,
        )
        assert result == {"disposition": "settled", "revision": 4, "spend_blocked": True, "exact_receipt": True}
        assert ledger(metering_db, "first-overlap")[0][0] == "settled"
        assert ledger(metering_db, "second-overlap")[0][0] == "unknown"
        assert await admission.settle_receipt(second, second_receipt) is True
        replay = await settle_metering_receipt(
            factory,
            journal,
            receipt=first_receipt,
            admission=first.receipt,
            scope_keys=tuple(sorted(first.scope_revisions)),
            admin_command=admin,
        )
        assert replay == result
        assert len(event_rows(metering_db)) == 1
    finally:
        await engine.dispose()
