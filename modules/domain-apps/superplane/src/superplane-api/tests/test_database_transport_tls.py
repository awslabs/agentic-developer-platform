"""Verified database transport (issue #5676, A22).

These tests assert INVARIANTS, not strings, and where the invariant is "what
the driver will actually do on the wire" they assert against asyncpg's own
resolved connection parameters rather than against the dictionary we hand it.
That distinction is the entire finding: the old code produced a connect-args
dict that looked innocuous ("no ssl key") while the driver turned it into
opportunistic, unauthenticated encryption with a silent plaintext retry. A test
that only inspected our dict would have passed against the defect.
"""

import inspect
import ssl
from pathlib import Path

import pytest
from app import schema_boundary
from app.schema_boundary import (
    CA_VARIABLE,
    LOCAL_EXCEPTION_VARIABLE,
    DatabaseTransportUnverifiable,
    connect_args,
    schema_connect_args,
    transport_connect_args,
)

# A real, parseable PEM bundle is required: ssl.create_default_context(cadata=...)
# validates the material, so a placeholder string cannot stand in for it.
SYSTEM_CA_BUNDLES = (
    ssl.get_default_verify_paths().cafile,
    "/etc/ssl/certs/ca-certificates.crt",
)


@pytest.fixture
def ca_pem():
    for candidate in SYSTEM_CA_BUNDLES:
        if candidate and Path(candidate).is_file():
            return Path(candidate).read_text()
    pytest.fail("no system PEM bundle available to test verified database TLS")


@pytest.fixture(autouse=True)
def clean_transport_env(monkeypatch):
    """Neither variable may leak in from the ambient environment.

    conftest.py sets the local exception so the application's import-time engine
    can be constructed offline. If that leaked into these tests, the fail-closed
    assertions below would silently test the exception path instead.
    """
    monkeypatch.delenv(CA_VARIABLE, raising=False)
    monkeypatch.delenv(LOCAL_EXCEPTION_VARIABLE, raising=False)


def resolved_asyncpg_parameters(args: dict):
    """Ask asyncpg what it would really negotiate, given our connect args.

    Uses the same internal resolver ``asyncpg.connect`` uses. Reaching into a
    private helper is deliberate: the public alternative is opening a socket to
    a real PostgreSQL server, and the property under test (what mode is chosen,
    whether a plaintext retry is armed) is decided entirely before the socket.
    """
    asyncpg_connect_utils = pytest.importorskip("asyncpg.connect_utils")
    # asyncpg 0.31 added libpq service-file arguments. The supported 0.30
    # driver in CI has the same TLS resolver without those optional inputs.
    service_args = {
        name: None
        for name in ("service", "servicefile")
        if name
        in inspect.signature(
            asyncpg_connect_utils._parse_connect_dsn_and_args
        ).parameters
    }
    _, params = asyncpg_connect_utils._parse_connect_dsn_and_args(
        dsn="postgresql://user:password@db.internal.example:5432/superplane",
        host=None,
        port=None,
        user=None,
        password=None,
        passfile=None,
        database=None,
        ssl=args.get("ssl"),
        direct_tls=None,
        server_settings=None,
        target_session_attrs=None,
        krbsrvname=None,
        gsslib=None,
        **service_args,
    )
    return asyncpg_connect_utils, params


# --- fail closed --------------------------------------------------------------


def test_missing_trust_material_refuses_before_any_connection():
    """Absent CA must raise, not return {} and let the driver decide."""
    with pytest.raises(DatabaseTransportUnverifiable) as raised:
        transport_connect_args()
    message = str(raised.value)
    # Actionable: names the setting to fix and where the value comes from.
    assert CA_VARIABLE in message
    assert "ca-pem" in message
    assert LOCAL_EXCEPTION_VARIABLE in message


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_blank_trust_material_is_treated_as_missing(monkeypatch, blank):
    """A secret key that exists but is empty must not count as trust material."""
    monkeypatch.setenv(CA_VARIABLE, blank)
    with pytest.raises(DatabaseTransportUnverifiable):
        transport_connect_args()


def test_combined_connect_args_also_fail_closed():
    """The entry points call connect_args(); it must not soften the refusal."""
    with pytest.raises(DatabaseTransportUnverifiable):
        connect_args("superplane")


# --- verified by default ------------------------------------------------------


def test_verified_context_requires_certificate_and_hostname(ca_pem, monkeypatch):
    monkeypatch.setenv(CA_VARIABLE, ca_pem)
    context = transport_connect_args()["ssl"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_driver_negotiates_verified_tls_with_no_plaintext_fallback(ca_pem, monkeypatch):
    """The wire-level invariant, asserted against asyncpg's own resolution.

    ``prefer`` and ``allow`` are the two modes ``_connect_addr`` retries, and
    the ``prefer`` retry is specifically an UNENCRYPTED second attempt. Our
    resolved mode must not be either one.
    """
    monkeypatch.setenv(CA_VARIABLE, ca_pem)
    module, params = resolved_asyncpg_parameters(transport_connect_args())

    assert params.ssl is not None and params.ssl is not False
    assert params.ssl.verify_mode is ssl.CERT_REQUIRED
    assert params.ssl.check_hostname is True
    assert params.sslmode not in (module.SSLMode.prefer, module.SSLMode.allow)


def test_old_default_would_have_been_unverified_with_plaintext_fallback():
    """Regression pin on the defect itself.

    This is what the pre-fix code produced when the CA was absent: an empty
    connect-args dict. It documents, executably, that "no ssl key" did NOT mean
    "no TLS" -- it meant unauthenticated TLS that silently falls back to
    plaintext. If a future asyncpg changes that default, this test fails and
    tells the next reader the threat model moved.
    """
    module, params = resolved_asyncpg_parameters({})
    assert params.sslmode is module.SSLMode.prefer
    assert params.ssl.verify_mode is ssl.CERT_NONE
    assert params.ssl.check_hostname is False


# --- the single documented exception ------------------------------------------


def test_local_exception_is_the_only_route_to_an_unverified_connection(caplog):
    monkeypatch_value = "true"
    import os

    os.environ[LOCAL_EXCEPTION_VARIABLE] = monkeypatch_value
    try:
        with caplog.at_level("WARNING"):
            args = transport_connect_args()
        assert args == {"ssl": "disable"}
        assert LOCAL_EXCEPTION_VARIABLE in caplog.text
        assert "WITHOUT certificate or hostname verification" in caplog.text
    finally:
        del os.environ[LOCAL_EXCEPTION_VARIABLE]


@pytest.mark.parametrize(
    "value", ["false", "0", "no", "off", "TRUE", "True", "yes", "1", " true"]
)
def test_exception_requires_the_exact_literal(monkeypatch, value):
    """A loose truthiness check would make "false" disable verification."""
    monkeypatch.setenv(LOCAL_EXCEPTION_VARIABLE, value)
    with pytest.raises(DatabaseTransportUnverifiable):
        transport_connect_args()


def test_exception_cannot_be_reached_by_omitting_configuration():
    """Absent configuration must fail closed, never downgrade."""
    with pytest.raises(DatabaseTransportUnverifiable):
        transport_connect_args()


def test_trust_material_wins_over_the_exception(ca_pem, monkeypatch):
    """A stale exception left set in an environment that HAS a CA must not
    silently keep that environment unverified."""
    monkeypatch.setenv(CA_VARIABLE, ca_pem)
    monkeypatch.setenv(LOCAL_EXCEPTION_VARIABLE, "true")
    assert isinstance(transport_connect_args()["ssl"], ssl.SSLContext)


# --- the two decisions are independent ----------------------------------------


@pytest.mark.parametrize("schema", ["", "superplane", "domain_a", "s"])
def test_schema_setting_cannot_influence_transport(ca_pem, monkeypatch, schema):
    """Encryption must not be a side effect of schema configuration.

    This is the tangle the old single function created: both answers came out of
    one dictionary, so a schema change could move the security posture.
    """
    monkeypatch.setenv(CA_VARIABLE, ca_pem)
    baseline = transport_connect_args()
    combined = connect_args(schema)
    assert combined["ssl"].verify_mode is baseline["ssl"].verify_mode
    assert combined["ssl"].check_hostname is baseline["ssl"].check_hostname
    assert set(transport_connect_args()) == {"ssl"}


def test_schema_args_carry_no_transport_key():
    assert "ssl" not in schema_connect_args("superplane")
    assert schema_connect_args("superplane") == {
        "server_settings": {"search_path": "superplane"}
    }
    assert schema_connect_args("") == {}


def test_combined_args_preserve_both_decisions(ca_pem, monkeypatch):
    monkeypatch.setenv(CA_VARIABLE, ca_pem)
    args = connect_args("superplane")
    assert args["server_settings"] == {"search_path": "superplane"}
    assert args["ssl"].verify_mode is ssl.CERT_REQUIRED


# --- every entry point routes through the shared decision ---------------------


def test_every_engine_entry_point_uses_the_shared_transport_helper():
    """Guarding the service but not the jobs leaves an equivalent gap open.

    Discovered by reading the sources rather than by listing call sites, so a
    newly added engine cannot quietly skip transport configuration.
    """
    import pathlib
    import re

    api_root = pathlib.Path(schema_boundary.__file__).resolve().parent.parent
    # Shipped code only. The test suite's own SQLite engine is not a Superplane
    # database connection and has no transport to secure; scanning it would make
    # this test assert something it does not mean.
    searched = sorted(
        path
        for directory in ("app", "alembic")
        for path in (api_root / directory).rglob("*.py")
    )
    assert searched, "found no shipped sources to scan; the scan root is wrong"

    offenders = []
    for path in searched:
        text = path.read_text()
        for match in re.finditer(
            r"create_async_engine\(|async_engine_from_config\(", text
        ):
            window = text[match.start() : match.start() + 400]
            if "connect_args" not in window:
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(api_root)}:{line}")
    assert not offenders, (
        "these engines are built without connect_args, so their transport is "
        f"whatever the driver defaults to: {offenders}"
    )


@pytest.mark.asyncio
async def test_server_cannot_downgrade_verified_connection_to_plaintext(
    ca_pem, monkeypatch
):
    """A server rejecting STARTTLS receives no PostgreSQL startup/credential data."""
    import asyncio
    import struct

    import asyncpg

    monkeypatch.setenv(CA_VARIABLE, ca_pem)
    completion = asyncio.get_running_loop().create_future()
    connections = []

    async def refuse_tls(reader, writer):
        try:
            request = await asyncio.wait_for(reader.readexactly(8), timeout=2)
            writer.write(b"N")
            await writer.drain()
            startup = await asyncio.wait_for(reader.read(1024), timeout=2)
            connections.append((request, startup))
        finally:
            writer.close()
            await writer.wait_closed()
            if not completion.done():
                completion.set_result(None)

    server = await asyncio.start_server(refuse_tls, "127.0.0.1", 0)
    try:
        with pytest.raises(ConnectionError, match="rejected SSL upgrade"):
            await asyncpg.connect(
                host="127.0.0.1",
                port=server.sockets[0].getsockname()[1],
                user="offline-test",
                password="offline-test-only",
                database="offline-test",
                timeout=2,
                **transport_connect_args(),
            )
        await asyncio.wait_for(completion, timeout=2)
        assert connections == [(struct.pack("!ll", 8, 80877103), b"")]
    finally:
        server.close()
        await server.wait_closed()
