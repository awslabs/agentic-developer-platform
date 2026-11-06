"""Actual PostgreSQL, immutable journal and TLS loopback transport in one bounded request path."""

import asyncio
import ssl
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
import psycopg2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from src.proxy.metered_transport import MeteredHTTPTransport, MeteredProvenUnsentError, MeteredUnsentCancelled
from tests.budget import test_metering_scope_admission as scope_fixtures
from tests.budget.test_metering_scope_admission import BODY, MODEL, admission_for, context, prepare
from tests.proxy.test_mantle_metering_admission import metering_clock as metering_clock_fixture
from tests.proxy.test_metered_transport import loopback

pytest_plugins = ("tests.budget.test_metering_scope_admission",)

metering_clock = metering_clock_fixture


@pytest.fixture
def metering_db(pg_head_url):
    return scope_fixtures.metering_db.__wrapped__(pg_head_url)


@pytest.fixture
def journal():
    yield from scope_fixtures.journal.__wrapped__()


@asynccontextmanager
async def tls_loopback():
    with TemporaryDirectory() as directory:
        private_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
            .not_valid_after(datetime.now(UTC) + timedelta(minutes=5))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(private_key, hashes.SHA256())
        )
        certificate_path = Path(directory) / "cert.pem"
        key_path = Path(directory) / "key.pem"
        certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_tls.load_cert_chain(str(certificate_path), str(key_path))
        async with loopback(server_tls) as (url, received, arrived, release):
            yield url, received, arrived, release, ssl.create_default_context(cafile=str(certificate_path))


@pytest.mark.parametrize("fault", ["cancelled", "expired", "wall_expired", "pricing_changed"])
@pytest.mark.parametrize("second_arm_delay", [0, 1.1], ids=["immediate", "contended"])
async def test_physical_send_and_durable_unsent_cancel_share_real_attempts(
    metering_db, journal, metering_clock, monkeypatch, fault, second_arm_delay
):
    async with tls_loopback() as (url, received, arrived, release, ssl_context):
        release.clear()
        admission, engine = admission_for(metering_db, journal)
        revision = [None]
        monkeypatch.setattr("src.proxy.metered_transport.adapter_for", lambda path: SimpleNamespace(current_pricing_revision=lambda: revision[0]))
        original_arm = admission.arm

        async def arm_with_contention(attempt, **binding):
            if second_arm_delay and attempt.receipt.request_id == f"unsent-{fault}":
                await asyncio.sleep(second_arm_delay)
            return await original_arm(attempt, **binding)

        monkeypatch.setattr(admission, "arm", arm_with_contention)
        try:
            first_attempt = await prepare(admission, context(), f"sent-{fault}")
            second_attempt = await prepare(admission, context(), f"unsent-{fault}")
            revision[0] = first_attempt.quote.pricing_revision
            transport = MeteredHTTPTransport(verify=ssl_context, limits=httpx.Limits(max_connections=1, max_keepalive_connections=0))
            async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
                first_permit = await admission.arm(first_attempt, submitted=BODY, model=MODEL, destination=url)
                first = asyncio.create_task(
                    transport.dispatch_once(client, client.build_request("POST", url, content=BODY), first_permit, model=MODEL, stream=False)
                )
                arrival = asyncio.create_task(arrived.wait())
                tasks = [first, arrival]
                try:
                    completed, _pending = await asyncio.wait(tasks, timeout=2, return_when=asyncio.FIRST_COMPLETED)
                    if first in completed:
                        await first
                    assert arrival in completed, "first metered request did not reach the loopback server"
                    assert not first_permit.proven_unsent()
                    second_permit = await admission.arm(second_attempt, submitted=BODY, model=MODEL, destination=url)
                    second = asyncio.create_task(
                        transport.dispatch_once(client, client.build_request("POST", url, content=BODY), second_permit, model=MODEL, stream=False)
                    )
                    tasks.append(second)
                    await asyncio.sleep(0)
                    if fault == "cancelled":
                        second.cancel()
                        expected = MeteredUnsentCancelled
                    elif fault == "expired":
                        metering_clock["monotonic"] = second_permit.monotonic_deadline
                        expected = MeteredProvenUnsentError
                    elif fault == "wall_expired":
                        metering_clock["wall"] = second_permit.expires_at
                        expected = MeteredProvenUnsentError
                    else:
                        revision[0] = "changed-generation"
                        expected = MeteredProvenUnsentError
                    release.set()
                    assert (await asyncio.wait_for(first, 2)).status_code == 200
                    with pytest.raises(expected) as outcome:
                        await asyncio.wait_for(second, 2)
                    assert outcome.value.permit is second_permit
                    assert second_permit.proven_unsent()
                    assert received == [BODY]
                    await admission.cancel_armed_unsent(second_attempt, outcome.value)
                finally:
                    release.set()
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
            proof = await journal.discover_unsent(second_attempt.receipt, deadline=asyncio.get_running_loop().time() + 2)
            assert proof is not None
            assert (await journal.read_unsent(proof, deadline=asyncio.get_running_loop().time() + 2))[
                "destination_sha256"
            ] == second_permit.destination_sha256
            with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT request_id, state FROM metering_attempts WHERE request_id IN (%s, %s)",
                    (first_attempt.receipt.request_id, second_attempt.receipt.request_id),
                )
                assert set(cursor.fetchall()) == {
                    (first_attempt.receipt.request_id, "dispatch_armed"),
                    (second_attempt.receipt.request_id, "cancelled_unsubmitted"),
                }
        finally:
            release.set()
            await engine.dispose()
