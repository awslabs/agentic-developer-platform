"""Recovery entrypoint against killed Responses children and independent SQL/journal evidence."""

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from multiprocessing import Process

import psycopg2
import pytest
import rfc8785
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.budget.metering_admission import MeteringAccountingUnresolvedError
from src.budget.metering_recovery import scan_metering_attempts
from src.orchestration.provider_quotes import TrustedUsage
from src.proxy.metered_transport import MeteredProvenUnsentError
from src.usage.metering_journal import ImmutableMeteringJournal
from tests.budget.test_metering_scope_admission import BODY, MODEL, admission_for, context, insert_budget, prepare
from tests.migrations.conftest_postgres import to_async_url
from tests.proxy.test_mantle_receipt_process_kill import BUCKET, FileBackedS3, kill_worker, object_path, read_attempt

pytest_plugins = ("tests.proxy.test_mantle_receipt_process_kill",)


async def scan(pg_url, root, *, client=None, now=None):
    engine = create_async_engine(to_async_url(pg_url), poolclass=NullPool)
    try:
        return await scan_metering_attempts(
            async_sessionmaker(engine, expire_on_commit=False),
            ImmutableMeteringJournal(BUCKET, region="us-east-1", client=client or FileBackedS3(root)),
            now=now or datetime.now(UTC) + timedelta(hours=2),
        )
    finally:
        await engine.dispose()


def accounting(pg_url, request_id):
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT org_id, user_id, cost_usd, total_tokens FROM budget_settlement_receipts WHERE request_id=%s", (request_id,))
        receipts = cursor.fetchall()
        cursor.execute("SELECT org_id, user_id, cost_usd, input_tokens, output_tokens FROM usage_logs WHERE request_id=%s", (request_id,))
        usage = cursor.fetchall()
        cursor.execute("SELECT entity_type, total_cost_usd, request_count FROM budget_usage ORDER BY entity_type, period_type")
        debits = cursor.fetchall()
    return receipts, usage, debits


def assert_one_charge(pg_url, request_id):
    receipts, usage, debits = accounting(pg_url, request_id)
    assert len(receipts) == 1 and receipts[0][:2] == ("tenant-1", "owner-1")
    assert receipts[0][2] > 0 and receipts[0][3] == 3
    assert usage == [("tenant-1", "owner-1", receipts[0][2], 1, 2)]
    assert len(debits) == 9
    assert all(cost == receipts[0][2] and count == 1 for _, cost, count in debits)


@pytest.mark.parametrize("stage", ["before", "after", "after_pointer"])
def test_killed_responses_are_classified_or_recovered_without_manual_sql(metering_db, tmp_path, stage):
    kill_worker(metering_db, tmp_path, stage)
    request_id = f"killed-{stage}"
    initial, scope_count, usage_count, receipt_count = read_attempt(metering_db, request_id)
    assert initial[0] == "dispatch_armed" and scope_count == 3
    assert usage_count == receipt_count == 0
    assert object_path(tmp_path, initial[5]).exists() is (stage != "before")
    if stage == "after":
        assert initial[6] is None
    result, next_cursor = asyncio.run(scan(metering_db, tmp_path))
    assert next_cursor is None
    assert result == ["unknown" if stage == "before" else "settled"]
    updated = read_attempt(metering_db, request_id)
    if stage == "before":
        assert updated[0][0] == "unknown" and updated[0][6] is None
        assert accounting(metering_db, request_id) == ([], [], [])
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            insert_budget(cursor, "tenant-1", "org", "tenant-1", 0, enforcement_mode="soft")

        async def reject_affected_spend():
            admission, engine = admission_for(metering_db, ImmutableMeteringJournal(BUCKET, region="us-east-1", client=FileBackedS3(tmp_path)))
            try:
                with pytest.raises(MeteringAccountingUnresolvedError):
                    await prepare(admission, context(), "denied-after-scan")
            finally:
                await engine.dispose()

        asyncio.run(reject_affected_spend())
        assert read_attempt(metering_db, "denied-after-scan")[0] is None
    else:
        assert updated[0][0] == "settled" and updated[0][6] == "fixture-v1"
        assert_one_charge(metering_db, request_id)
        assert asyncio.run(scan(metering_db, tmp_path))[0] == []


def test_competing_scanners_recover_pointerless_receipt_once(metering_db, tmp_path):
    kill_worker(metering_db, tmp_path, "after")

    async def compete():
        return await asyncio.gather(scan(metering_db, tmp_path), scan(metering_db, tmp_path))

    results = asyncio.run(compete())
    assert any("settled" in statuses for statuses, _ in results)
    assert_one_charge(metering_db, "killed-after")


@pytest.mark.parametrize("fence_column", ["revision", "producer_epoch"])
def test_stale_revision_or_epoch_cannot_attach_receipt_after_network_read(metering_db, tmp_path, fence_column):
    kill_worker(metering_db, tmp_path, "after")

    class EpochChangingS3(FileBackedS3):
        changed = False

        def get_object(self, **kwargs):
            if not self.changed:
                self.changed = True
                with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                    statement = {
                        "revision": "UPDATE metering_attempts SET revision=revision+1 WHERE request_id='killed-after'",
                        "producer_epoch": "UPDATE metering_attempts SET producer_epoch=producer_epoch+1 WHERE request_id='killed-after'",
                    }[fence_column]
                    cursor.execute(statement)
            return super().get_object(**kwargs)

    assert asyncio.run(scan(metering_db, tmp_path, client=EpochChangingS3(tmp_path)))[0] == ["stale"]
    assert read_attempt(metering_db, "killed-after")[0][6] is None
    assert accounting(metering_db, "killed-after") == ([], [], [])
    assert asyncio.run(scan(metering_db, tmp_path))[0] == ["settled"]
    assert_one_charge(metering_db, "killed-after")


def test_conflicting_final_evidence_quarantines_without_guessing_usage(metering_db, tmp_path):
    kill_worker(metering_db, tmp_path, "after")
    attempt = read_attempt(metering_db, "killed-after")[0]
    path = object_path(tmp_path, attempt[5])
    original = path.read_bytes()
    payload = json.loads(original)
    payload["admission_sha256"] = "0" * 64
    path.write_bytes(rfc8785.dumps(payload))
    assert asyncio.run(scan(metering_db, tmp_path))[0] == ["quarantined"]
    assert read_attempt(metering_db, "killed-after")[0][0] == "quarantined"
    assert accounting(metering_db, "killed-after") == ([], [], [])
    path.write_bytes(original)
    assert asyncio.run(scan(metering_db, tmp_path))[0] == ["quarantined"]
    assert accounting(metering_db, "killed-after") == ([], [], [])
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT action, disposition FROM metering_reconciliation_events WHERE request_id='killed-after'")
        assert cursor.fetchall() == [("quarantine", "quarantined")]


def test_scan_pages_past_unresolved_exposure_to_receipted_attempt(metering_db, tmp_path):
    kill_worker(metering_db, tmp_path, "before")
    kill_worker(metering_db, tmp_path, "after")

    async def pages():
        engine = create_async_engine(to_async_url(metering_db), poolclass=NullPool)
        journal = ImmutableMeteringJournal(BUCKET, region="us-east-1", client=FileBackedS3(tmp_path))
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            first, cursor = await scan_metering_attempts(factory, journal, now=datetime.now(UTC) + timedelta(hours=2), limit=1)
            assert first == ["unknown"] and cursor is not None
            second, next_cursor = await scan_metering_attempts(factory, journal, now=datetime.now(UTC) + timedelta(hours=2), limit=1, cursor=cursor)
            assert second == ["settled"] and next_cursor is not None
        finally:
            await engine.dispose()

    asyncio.run(pages())
    assert read_attempt(metering_db, "killed-before")[0][0] == "unknown"
    assert_one_charge(metering_db, "killed-after")


def test_delayed_journal_respects_scan_deadline_and_next_page_progresses(metering_db, tmp_path):
    kill_worker(metering_db, tmp_path, "before")
    kill_worker(metering_db, tmp_path, "after")

    class DelayedJournal(ImmutableMeteringJournal):
        async def discover_final(self, admission, *, deadline):
            if admission.request_id == "killed-before":
                await asyncio.sleep(1)
            return await super().discover_final(admission, deadline=deadline)

    async def pages():
        engine = create_async_engine(to_async_url(metering_db), poolclass=NullPool)
        client = FileBackedS3(tmp_path)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            started = time.monotonic()
            held, cursor = await scan_metering_attempts(
                factory,
                DelayedJournal(BUCKET, region="us-east-1", client=client),
                now=datetime.now(UTC) + timedelta(hours=2),
                limit=1,
                budget_seconds=0.2,
            )
            assert time.monotonic() - started < 0.8
            assert held == ["held"] and cursor is not None
            settled, _ = await scan_metering_attempts(
                factory,
                ImmutableMeteringJournal(BUCKET, region="us-east-1", client=client),
                now=datetime.now(UTC) + timedelta(hours=2),
                limit=1,
                cursor=cursor,
            )
            assert settled == ["settled"]
        finally:
            await engine.dispose()

    asyncio.run(pages())
    assert read_attempt(metering_db, "killed-before")[0][0] == "dispatch_armed"
    assert accounting(metering_db, "killed-before")[:2] == ([], [])
    assert_one_charge(metering_db, "killed-after")


def test_held_attempt_row_lock_does_not_block_later_healthy_candidate(metering_db, tmp_path):
    kill_worker(metering_db, tmp_path, "before")
    kill_worker(metering_db, tmp_path, "after")
    with psycopg2.connect(metering_db) as blocker, blocker.cursor() as cursor:
        cursor.execute("SELECT request_id FROM metering_attempts WHERE request_id='killed-before' FOR UPDATE")
        started = time.monotonic()
        results, _ = asyncio.run(scan(metering_db, tmp_path))
        assert time.monotonic() - started < 4
        assert results == ["held", "settled"]
        assert read_attempt(metering_db, "killed-before")[0][0] == "dispatch_armed"
        assert accounting(metering_db, "killed-before")[:2] == ([], [])
    assert_one_charge(metering_db, "killed-after")


def test_expired_prepared_attempt_is_not_released_by_recovery(metering_db, tmp_path):
    async def prepare_expired():
        admission, engine = admission_for(metering_db, ImmutableMeteringJournal(BUCKET, region="us-east-1", client=FileBackedS3(tmp_path)))
        try:
            await prepare(admission, context(), "expired-prepared")
        finally:
            await engine.dispose()

    asyncio.run(prepare_expired())
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE metering_attempts SET prepare_deadline=NOW()-interval '1 hour' WHERE request_id='expired-prepared'")
    assert asyncio.run(scan(metering_db, tmp_path))[0] == []
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT state FROM metering_attempts WHERE request_id='expired-prepared'")
        assert cursor.fetchone() == ("prepared",)
        cursor.execute("SELECT count(*) FROM metering_attempt_scopes WHERE request_id='expired-prepared'")
        assert cursor.fetchone() == (3,)

    async def reject_same_scope():
        admission, engine = admission_for(metering_db, ImmutableMeteringJournal(BUCKET, region="us-east-1", client=FileBackedS3(tmp_path)))
        try:
            with pytest.raises(MeteringAccountingUnresolvedError):
                await prepare(admission, context(), "denied-expired-prepared")
        finally:
            await engine.dispose()

    asyncio.run(reject_same_scope())
    assert read_attempt(metering_db, "denied-expired-prepared")[0] is None


@pytest.mark.asyncio
async def test_recovery_rejects_unbounded_page_and_scan():
    with pytest.raises(ValueError, match="1–100"):
        await scan_metering_attempts(None, None, limit=101)
    with pytest.raises(ValueError, match="at most 30 seconds"):
        await scan_metering_attempts(None, None, budget_seconds=31)


@pytest.mark.asyncio
async def test_recovery_loop_advances_cursor_and_stops_cleanly(monkeypatch):
    from src.budget import metering_recovery

    seen = []
    second_scan = asyncio.Event()
    page_cursor = (datetime.now(UTC), "tenant-1", "attempt-1")

    async def fake_scan(_session_factory, _journal, *, cursor=None):
        seen.append(cursor)
        if len(seen) == 2:
            second_scan.set()
        return [], page_cursor if cursor is None else None

    monkeypatch.setattr(metering_recovery, "scan_metering_attempts", fake_scan)
    task = asyncio.create_task(metering_recovery.maintain_metering_recovery(object(), object(), interval_seconds=0.01))
    try:
        await asyncio.wait_for(second_scan.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert seen[:2] == [None, page_cursor]


@pytest.mark.parametrize("failure", ["sql_timeout", "lost_ack", "crash_after_proof"])
def test_recovery_releases_only_proven_unsent_row_after_failed_ack(metering_db, tmp_path, failure):
    class LostAckS3(FileBackedS3):
        def put_object(self, **kwargs):
            result = super().put_object(**kwargs)
            if kwargs["Key"].endswith("/proven-unsent.json"):
                raise TimeoutError("no-send write acknowledgement lost")
            return result

    async def stage():
        journal = ImmutableMeteringJournal(
            BUCKET, region="us-east-1", client=LostAckS3(tmp_path) if failure == "lost_ack" else FileBackedS3(tmp_path)
        )
        admission, engine = admission_for(metering_db, journal)
        try:
            proved = await prepare(admission, context(), f"unsent-{failure}")
            unproved = await prepare(admission, context(), f"unproved-{failure}")
            destination = "https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses"
            permit = await admission.arm(proved, submitted=BODY, model=MODEL, destination=destination)
            await admission.arm(unproved, submitted=BODY, model=MODEL, destination=destination)
            if failure in {"sql_timeout", "crash_after_proof"}:
                original_factory = admission._session_factory

                class FailingSession:
                    async def __aenter__(self):
                        if failure == "sql_timeout":
                            raise TimeoutError("SQL release failed after proof")
                        raise SystemExit("process exited after proof")

                    async def __aexit__(self, *_):
                        pass

                admission._session_factory = FailingSession
            try:
                with pytest.raises(TimeoutError if failure != "crash_after_proof" else SystemExit):
                    await admission.cancel_armed_unsent(proved, MeteredProvenUnsentError(permit))
            finally:
                if failure in {"sql_timeout", "crash_after_proof"}:
                    admission._session_factory = original_factory
            assert (await journal.discover_unsent(proved.receipt, deadline=asyncio.get_running_loop().time() + 2)) is not None
            assert (await journal.discover_unsent(unproved.receipt, deadline=asyncio.get_running_loop().time() + 2)) is None
            return proved.receipt.request_id, unproved.receipt.request_id
        finally:
            await engine.dispose()

    proved_id, unproved_id = asyncio.run(stage())
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT request_id, state FROM metering_attempts WHERE request_id IN (%s, %s)", (proved_id, unproved_id))
        assert set(cursor.fetchall()) == {(proved_id, "dispatch_armed"), (unproved_id, "dispatch_armed")}
    results, _ = asyncio.run(scan(metering_db, tmp_path, now=datetime.now(UTC) + timedelta(minutes=30)))
    assert set(results) == {"cancelled_unsubmitted", "unknown"}
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT request_id, state FROM metering_attempts WHERE request_id IN (%s, %s)", (proved_id, unproved_id))
        assert set(cursor.fetchall()) == {(proved_id, "cancelled_unsubmitted"), (unproved_id, "unknown")}
        cursor.execute("SELECT action, evidence_ref FROM metering_reconciliation_events WHERE request_id=%s", (proved_id,))
        assert cursor.fetchall() == [("cancel_unsent", ImmutableMeteringJournal.unsent_key("tenant-1", proved_id))]
    asyncio.run(scan(metering_db, tmp_path, now=datetime.now(UTC) + timedelta(minutes=30)))
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM metering_reconciliation_events WHERE request_id=%s", (proved_id,))
        assert cursor.fetchone()[0] == 1
    assert accounting(metering_db, proved_id) == ([], [], [])


@pytest.mark.parametrize("conflict", ["revision", "epoch", "quote", "final"])
def test_recovery_does_not_release_conflicting_no_send_proof(metering_db, tmp_path, conflict):
    async def stage():
        journal = ImmutableMeteringJournal(BUCKET, region="us-east-1", client=FileBackedS3(tmp_path))
        admission, engine = admission_for(metering_db, journal)
        try:
            attempt = await prepare(admission, context(), f"conflict-{conflict}")
            permit = await admission.arm(
                attempt, submitted=BODY, model=MODEL, destination="https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses"
            )
            await journal.write_unsent(
                attempt.receipt,
                revision=permit.revision,
                producer_epoch=permit.producer_epoch,
                request_sha256=attempt.quote.request_sha256,
                submitted_sha256=attempt.submitted_sha256,
                quote_sha256=attempt.quote_sha256,
                destination_sha256=permit.destination_sha256,
                deadline=asyncio.get_running_loop().time() + 2,
            )
            if conflict == "final":
                await journal.write_final(
                    admission=attempt.receipt,
                    usage={"input_tokens": 1, "output_tokens": 1},
                    trusted=TrustedUsage(1, 1, True),
                    provider_request_id=None,
                    deadline=asyncio.get_running_loop().time() + 2,
                )
            return attempt.receipt.request_id
        finally:
            await engine.dispose()

    request_id = asyncio.run(stage())
    if conflict == "quote":
        path = object_path(tmp_path, ImmutableMeteringJournal.unsent_key("tenant-1", request_id))
        payload = json.loads(path.read_bytes())
        payload["quote_sha256"] = "0" * 64
        path.write_bytes(rfc8785.dumps(payload))
    elif conflict in {"revision", "epoch"}:
        field = "revision" if conflict == "revision" else "producer_epoch"
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute(f"UPDATE metering_attempts SET {field}={field}+1 WHERE request_id=%s", (request_id,))
    results, _ = asyncio.run(scan(metering_db, tmp_path, now=datetime.now(UTC) + timedelta(minutes=30)))
    assert results == ["quarantined"]
    assert read_attempt(metering_db, request_id)[0][0] == "quarantined"
    assert accounting(metering_db, request_id) == ([], [], [])


def _crash_after_unsent_journal(pg_url, root, request_id):
    class CrashAfterProof(ImmutableMeteringJournal):
        async def write_unsent(self, *args, **kwargs):
            await super().write_unsent(*args, **kwargs)
            os._exit(23)

    async def run():
        journal = CrashAfterProof(BUCKET, region="us-east-1", client=FileBackedS3(root))
        admission, engine = admission_for(pg_url, journal)
        try:
            attempt = await prepare(admission, context(), request_id)
            permit = await admission.arm(
                attempt, submitted=BODY, model=MODEL, destination="https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses"
            )
            await admission.cancel_armed_unsent(attempt, MeteredProvenUnsentError(permit))
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_killed_after_no_send_proof_recovery_does_not_release_independent_row(metering_db, tmp_path):
    process = Process(target=_crash_after_unsent_journal, args=(metering_db, tmp_path, "killed-unsent"))
    process.start()
    process.join(timeout=15)
    if process.is_alive():
        process.terminate()
        process.join(timeout=2)
    assert process.exitcode == 23

    async def add_independent_row():
        journal = ImmutableMeteringJournal(BUCKET, region="us-east-1", client=FileBackedS3(tmp_path))
        admission, engine = admission_for(metering_db, journal)
        try:
            attempt = await prepare(admission, context(), "killed-unproven")
            await admission.arm(attempt, submitted=BODY, model=MODEL, destination="https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses")
        finally:
            await engine.dispose()

    asyncio.run(add_independent_row())
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT state FROM metering_attempts WHERE request_id='killed-unsent'")
        assert cursor.fetchone()[0] == "dispatch_armed"
    results, _ = asyncio.run(scan(metering_db, tmp_path, now=datetime.now(UTC) + timedelta(minutes=30)))
    assert set(results) == {"cancelled_unsubmitted", "unknown"}
    assert read_attempt(metering_db, "killed-unsent")[0][0] == "cancelled_unsubmitted"
    assert read_attempt(metering_db, "killed-unproven")[0][0] == "unknown"
    assert accounting(metering_db, "killed-unsent") == ([], [], [])
