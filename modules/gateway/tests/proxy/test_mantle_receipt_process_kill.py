"""Process-kill probes against Responses, migrated PostgreSQL and a shared journal fixture."""

import asyncio
import hashlib
import io
import json
import os
import select
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import psycopg2
import pytest
import rfc8785
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from pricing_policy import load_snapshot
from src.budget.metering_admission import MeteringAccountingUnresolvedError, MeteringAdmission
from src.budget.metering_settlement import settle_metering_receipt
from src.budget.pricing_v2_reader import cached_rate_state
from src.orchestration.provider_quotes import ProviderQuote, TrustedUsage, _pricing_revision, request_digest
from src.proxy.mantle_service import MantlePassthroughService
from src.proxy.model_resolver import ModelResolver
from src.proxy.routes import get_token_context
from src.shared.schemas.auth import TokenContext
from src.usage.metering_journal import ImmutableMeteringJournal, MeteringObjectReceipt
from tests.budget.test_metering_scope_admission import insert_budget
from tests.migrations.conftest_postgres import to_async_url
from tests.proxy.test_mantle_metering_admission import MockMeteredTransport
from tests.proxy.test_mantle_route import StubAuth, build_app

pytest_plugins = ("tests.migrations.conftest_postgres",)
pytestmark = pytest.mark.usefixtures("unmapped_mantle_routing")

MODEL = "openai.gpt-5.5"
USAGE = {"input_tokens": 1, "output_tokens": 2}
BUCKET = "metering-crash-fixture"


def object_path(root: Path, key: str) -> Path:
    return root / hashlib.sha256(key.encode()).hexdigest()


class FileBackedS3:
    def __init__(self, root: Path):
        self.root = root

    def put_object(self, **kwargs):
        assert kwargs["Bucket"] == BUCKET and kwargs["ContentType"] == "application/json" and kwargs["IfNoneMatch"] == "*"
        try:
            descriptor = os.open(object_path(self.root, kwargs["Key"]), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject") from exc
        with os.fdopen(descriptor, "wb") as output:
            output.write(kwargs["Body"])
            output.flush()
            os.fsync(output.fileno())
        return {"VersionId": "fixture-v1"}

    def get_object(self, **kwargs):
        assert kwargs["Bucket"] == BUCKET
        try:
            body = object_path(self.root, kwargs["Key"]).read_bytes()
        except FileNotFoundError as error:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject") from error
        return {"Body": io.BytesIO(body), "VersionId": "fixture-v1"}


@pytest.fixture
def metering_db(pg_head_url):
    with psycopg2.connect(pg_head_url) as connection, connection.cursor() as cursor:
        for kind, identity in (("org", "tenant-1"), ("user", "owner-1"), ("agent", "owner-1")):
            key = f"v1:tenant:{request_digest(rfc8785.dumps(['tenant-1', kind, identity]))}"
            cursor.execute(
                "INSERT INTO metering_scope_gates (scope_key, scope_kind, tenant_id, activation, readiness) "
                "VALUES (%s, %s, 'tenant-1', 'active', 'ready')",
                (key, kind),
            )
    return pg_head_url


def quote(body: bytes) -> ProviderQuote:
    now = datetime.now(UTC)
    return ProviderQuote(
        provider="bedrock",
        endpoint="/openai/v1/responses",
        capability="responses",
        billing_model_id=MODEL,
        request_sha256=request_digest(body),
        pricing_revision=_pricing_revision(
            load_snapshot().snapshot_version,
            cached_rate_state().source,
            cached_rate_state().generation_id,
            cached_rate_state().pointer_revision,
        ),
        currency="USD",
        issued_at=now,
        expires_at=now + timedelta(minutes=1),
        max_input_tokens=20,
        max_output_tokens=20,
        max_tool_tokens=0,
        max_cache_write_tokens=0,
        total_usd=Decimal("0.001"),
        evidence_source="test-snapshot",
    )


def run_worker() -> None:
    stage = os.environ["AC01_CRASH_STAGE"]
    root = Path(os.environ["AC01_CRASH_ROOT"])
    request_id = f"killed-{stage}"
    body = json.dumps({"model": MODEL, "input": "hello", "stream": True, "max_output_tokens": 20}).encode()
    context = TokenContext(
        user_id="owner-1",
        org_id="tenant-1",
        team_id="",
        department_id="",
        attributed_user_id="owner-1",
        account_type="service",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    context._policy_quote = quote(body)

    class PausedJournal(ImmutableMeteringJournal):
        async def write_final(self, **kwargs):
            if stage == "before":
                print("BEFORE_FINAL", flush=True)
                await asyncio.Event().wait()
            receipt = await super().write_final(**kwargs)
            if stage == "after":
                print("AFTER_FINAL", flush=True)
                await asyncio.Event().wait()
            return receipt

    class PausedAdmission(MeteringAdmission):
        async def write_final(self, **kwargs):
            receipt = await super().write_final(**kwargs)
            if stage == "after_pointer":
                print("AFTER_POINTER", flush=True)
                await asyncio.Event().wait()
            return receipt

    journal = PausedJournal(BUCKET, region="us-east-1", client=FileBackedS3(root))
    engine = create_async_engine(to_async_url(os.environ["AC01_CRASH_DB"]), poolclass=NullPool)
    admission = PausedAdmission(async_sessionmaker(engine, expire_on_commit=False), journal)

    def provider(_request):
        print("PROVIDER", flush=True)
        event = {"type": "response.completed", "response": {"usage": USAGE}}
        return httpx.Response(200, content=b"event: response.completed\ndata: " + json.dumps(event).encode() + b"\n\n")

    from src.proxy import mantle_service
    from src.proxy.bedrock_enforcement import RoutingDecision
    from src.proxy.bedrock_routing import BedrockTarget

    mantle_service.resolve_routing_decision = AsyncMock(return_value=RoutingDecision(target=BedrockTarget(account_id=None, rung="platform")))
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    service = MantlePassthroughService(
        StubAuth(), "https://bedrock-mantle.us-east-1.api.aws", http_client=upstream, metering=admission, metered_transport=MockMeteredTransport()
    )
    service._stream_finalization_timeout = 20
    service._trusted_usage = AsyncMock(return_value=TrustedUsage(1, 2, True))
    service._log_usage = AsyncMock(return_value=True)
    app = build_app(service, ModelResolver(allowed_models_config={context.org_id: ["openai.*"]}), context)

    @app.middleware("http")
    async def assign_request_id(request, call_next):
        request.state.request_id = request_id
        return await call_next(request)

    app.dependency_overrides.pop(get_token_context, None)

    async def verify(authorization):
        assert authorization == "Bearer scenario-fixture"
        return context

    with patch("src.proxy.routes.validate_cognito_jwt", side_effect=verify), TestClient(app) as route:
        response = route.post(
            "/openai/v1/responses", content=body, headers={"Content-Type": "application/json", "Authorization": "Bearer scenario-fixture"}
        )
        print("CLIENT:" + response.content.decode(), flush=True)
    asyncio.run(upstream.aclose())
    asyncio.run(engine.dispose())


def read_attempt(pg_url, request_id):
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT state, bound_usd, admission_key, admission_version, admission_sha256, final_key, final_version, final_sha256 "
            "FROM metering_attempts WHERE tenant_id='tenant-1' AND request_id=%s",
            (request_id,),
        )
        attempt = cursor.fetchone()
        cursor.execute("SELECT count(*) FROM metering_attempt_scopes WHERE tenant_id='tenant-1' AND request_id=%s", (request_id,))
        scope_count = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM usage_logs WHERE request_id=%s", (request_id,))
        usage_count = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM budget_settlement_receipts WHERE org_id='tenant-1' AND request_id=%s", (request_id,))
        receipt_count = cursor.fetchone()[0]
    return attempt, scope_count, usage_count, receipt_count


def next_marker(process, timeout=20):
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    assert ready, f"crash worker exited early: {process.poll()}"
    return process.stdout.readline().strip()


def kill_worker(pg_url, root, stage):
    environment = {
        **os.environ,
        "AC01_CRASH_DB": pg_url,
        "AC01_CRASH_ROOT": str(root),
        "AC01_CRASH_STAGE": stage,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    process = subprocess.Popen(
        [sys.executable, "-u", "-m", "tests.proxy.test_mantle_receipt_process_kill", "--worker"],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert next_marker(process) == "PROVIDER"
        marker = {"before": "BEFORE_FINAL", "after": "AFTER_FINAL", "after_pointer": "AFTER_POINTER"}[stage]
        assert next_marker(process) == marker
        assert process.poll() is None
    finally:
        process.kill()
        output, _errors = process.communicate(timeout=5)
    assert process.returncode == -9
    assert "CLIENT:" not in output


@pytest.mark.parametrize("stage", ["before", "after"])
def test_process_kill_preserves_unknown_or_receipted_attempt_without_false_success(metering_db, tmp_path, stage):
    kill_worker(metering_db, tmp_path, stage)
    request_id = f"killed-{stage}"
    attempt, scope_count, usage_count, receipt_count = read_attempt(metering_db, request_id)
    assert attempt[0:2] == ("dispatch_armed", Decimal("0.001000"))
    assert scope_count == 3
    assert usage_count == receipt_count == 0
    admission = object_path(tmp_path, attempt[2]).read_bytes()
    assert attempt[3] == "fixture-v1"
    assert hashlib.sha256(admission).hexdigest() == attempt[4]
    assert json.loads(admission)["request_id"] == request_id
    assert attempt[6] is None
    final_path = object_path(tmp_path, attempt[5])
    assert final_path.exists() is (stage == "after")
    if stage == "after":
        final = json.loads(final_path.read_bytes())
        assert final["usage"] == USAGE
        assert final["admission_sha256"] == attempt[4]
        assert final["request_id"] == request_id
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE metering_attempts SET state='unknown' WHERE tenant_id='tenant-1' AND request_id=%s", (request_id,))
        insert_budget(cursor, "tenant-1", "org", "tenant-1", Decimal("0.00"), enforcement_mode="soft")

    async def denied_spend():
        engine = create_async_engine(to_async_url(metering_db), poolclass=NullPool)
        try:
            caller = TokenContext(
                user_id="owner-1",
                org_id="tenant-1",
                team_id="",
                department_id="",
                attributed_user_id="owner-1",
                account_type="service",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            body = json.dumps({"model": MODEL, "input": "hello", "stream": True, "max_output_tokens": 20}).encode()
            caller._policy_quote = quote(body)
            admission_service = MeteringAdmission(
                async_sessionmaker(engine, expire_on_commit=False),
                ImmutableMeteringJournal(BUCKET, region="us-east-1", client=FileBackedS3(tmp_path)),
            )
            with pytest.raises(MeteringAccountingUnresolvedError):
                await admission_service.prepare(
                    context=caller,
                    request_id=f"denied-after-{stage}",
                    model=MODEL,
                    original=body,
                    submitted=body,
                    deadline=asyncio.get_running_loop().time() + 10,
                )
        finally:
            await engine.dispose()

    asyncio.run(denied_spend())
    assert read_attempt(metering_db, request_id)[0][0] == "unknown"
    assert read_attempt(metering_db, request_id)[2:] == (0, 0)
    assert read_attempt(metering_db, f"denied-after-{stage}")[0] is None


def test_process_kill_after_sql_pointer_recovers_one_charge(metering_db, tmp_path):
    kill_worker(metering_db, tmp_path, "after_pointer")
    request_id = "killed-after_pointer"
    attempt, scope_count, usage_count, receipt_count = read_attempt(metering_db, request_id)
    assert attempt[0] == "dispatch_armed"
    assert attempt[6] == "fixture-v1"
    assert scope_count == 3 and usage_count == receipt_count == 0
    assert hashlib.sha256(object_path(tmp_path, attempt[5]).read_bytes()).hexdigest() == attempt[7]
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT scope_key FROM metering_attempt_scopes WHERE tenant_id='tenant-1' AND request_id=%s ORDER BY scope_key", (request_id,))
        scope_keys = tuple(scope_key for (scope_key,) in cursor.fetchall())
    assert len(scope_keys) == 3
    admission_receipt = MeteringObjectReceipt("tenant-1", request_id, "owner-1", attempt[2], attempt[3], attempt[4])
    final_receipt = MeteringObjectReceipt("tenant-1", request_id, "owner-1", attempt[5], attempt[6], attempt[7])

    async def recover():
        engine = create_async_engine(to_async_url(metering_db), poolclass=NullPool)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        journal = ImmutableMeteringJournal(BUCKET, region="us-east-1", client=FileBackedS3(tmp_path))
        try:

            async def settle():
                return await settle_metering_receipt(
                    session_factory, journal, receipt=final_receipt, admission=admission_receipt, scope_keys=scope_keys
                )

            results = await asyncio.gather(settle(), settle())
            assert sorted(results) == [False, True]
            assert not await settle()
        finally:
            await engine.dispose()

    asyncio.run(recover())
    assert read_attempt(metering_db, request_id)[0][0] == "settled"
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT org_id, user_id, cost_usd, total_tokens FROM budget_settlement_receipts WHERE request_id=%s", (request_id,))
        charged = cursor.fetchall()
        assert len(charged) == 1 and charged[0][:2] == ("tenant-1", "owner-1")
        assert charged[0][2] > 0 and charged[0][3] == 3
        cursor.execute("SELECT org_id, user_id, cost_usd, input_tokens, output_tokens FROM usage_logs WHERE request_id=%s", (request_id,))
        assert cursor.fetchall() == [("tenant-1", "owner-1", charged[0][2], 1, 2)]
        cursor.execute("SELECT total_cost_usd, request_count FROM budget_usage WHERE org_id='tenant-1'")
        debits = cursor.fetchall()
        assert len(debits) == 9
        assert all(cost == charged[0][2] and count == 1 for cost, count in debits)


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    run_worker()
