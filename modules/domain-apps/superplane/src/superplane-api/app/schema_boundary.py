"""Two independent connection decisions shared by API sessions, the maintained
Alembic chain and the one-shot jobs: which schema to use, and how the transport
is secured.

WHY THESE ARE SEPARATE FUNCTIONS (issue #5676, A22)
---------------------------------------------------
They used to be one. ``connect_args(schema)`` computed the search_path AND
decided encryption, and returned both in one dictionary. Two unrelated
questions answered in one place meant a change to schema handling could move
the encryption posture without anyone reading the diff as a security change.
``schema_connect_args`` and ``transport_connect_args`` are now independent, and
a test asserts that sweeping the schema setting across its whole range leaves
the transport result byte-identical.

WHAT THE DEFECT ACTUALLY WAS
----------------------------
Stated precisely, because the imprecise version is wrong in a way that matters.
The old code attached an SSL context only when ``SUPERPLANE_DATABASE_CA`` was
set, and when it was absent returned connect args with no ssl key at all. It is
tempting to call that "TLS is absent". It is not. asyncpg's default with no ssl
argument is ``sslmode=prefer``, which was verified against asyncpg 0.31.0
rather than read off the parameter name:

    sslmode=prefer -> ctx.verify_mode = CERT_NONE, ctx.check_hostname = False

So the old default did three things, none of them what a reader assumes:

1. It DID attempt an encrypted connection, so "no TLS" is false.
2. It accepted ANY certificate -- unsigned, self-signed, or presented by
   something that is not our database -- because CERT_NONE means the chain is
   never checked and check_hostname=False means the name is never compared.
   Encryption without authentication stops a passive eavesdropper and does
   nothing at all to someone who can answer in the database's place.
3. On failure it SILENTLY RETRIED IN PLAINTEXT. ``prefer`` is the one mode
   asyncpg retries without encryption (see ``_connect_addr``: ``prefer``
   installs ``params_retry`` with ``ssl=None``). So the wire format was
   non-deterministic per connection attempt and nothing logged which one won.

The fix is therefore not "turn TLS on" -- it is make verification mandatory and
make the outcome deterministic.

WHY A CONTEXT OBJECT AND NOT sslmode="verify-full"
--------------------------------------------------
Two dead ends, both checked against asyncpg 0.31.0 rather than assumed, because
the obvious-looking fix does not work:

* ``sslmode`` is NOT a keyword argument of ``asyncpg.connect``. Passing it
  through SQLAlchemy's ``connect_args`` raises
  ``TypeError: connect() got an unexpected keyword argument 'sslmode'`` at the
  first connection -- a startup crash, not a hardening.
* The string form ``ssl="verify-full"`` IS accepted, but with no CA file
  configured asyncpg goes looking for ``~/.postgresql/root.crt`` and raises
  ``ClientConfigurationError`` because that file does not exist in our
  containers. It also cannot read a PEM we hold in memory as a secret value.

So the pinned context is the only form that both carries our CA and is accepted
by the driver. It is also already deterministic, which is the part worth
recording: with ``ssl=<context>``, asyncpg resolves ``sslmode`` to ``disable``
internally, and ``disable`` is not one of the two modes ``_connect_addr``
retries. Since ``params.ssl`` is truthy, the connection still goes through
``_create_ssl_connection`` with ``ssl_is_advisory=False``. Verified TLS,
mandatory, no plaintext fallback:

    ssl=create_default_context(cadata=CA)
      -> verify_mode=CERT_REQUIRED, check_hostname=True, retry=False
"""

import ipaddress
import logging
import os
import re
import ssl

logger = logging.getLogger(__name__)

CA_VARIABLE = "SUPERPLANE_DATABASE_CA"

# The one documented exception, and it is deliberately verbose. Naming it after
# what it gives up rather than after the thing it enables ("SUPERPLANE_DB_TLS",
# "INSECURE_OK") is the point: a reader copying it into a template has to copy
# the words "allow unverified" with it.
#
# It cannot be reached by omitting configuration. An absent CA is a refusal to
# start, NOT a downgrade -- that asymmetry is the whole fix, because the old
# behaviour was exactly "absent config silently means less security".
LOCAL_EXCEPTION_VARIABLE = "SUPERPLANE_DATABASE_ALLOW_UNVERIFIED_LOCAL_TLS"

# This exact service belongs to the disposable, namespace-isolated fixture.
# Its apply wrapper additionally enforces a loopback kind/k3d/minikube cluster;
# a service name alone cannot establish that the cluster is local. Do not
# generalize this to arbitrary Kubernetes service names or DNS suffixes.
LOCAL_FIXTURE_HOST = (
    "superplane-integration-test-postgres.superplane-integration-test.svc.cluster.local"
)


class DatabaseTransportUnverifiable(RuntimeError):
    """Verified transport was required and its trust material was unavailable."""


def schema_name(value: str) -> str | None:
    if not value:
        return None
    if (
        not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value)
        or value in {"public", "information_schema"}
        or value.startswith("pg_")
    ):
        raise ValueError("SUPERPLANE_DB_SCHEMA must name one isolated domain schema")
    return value


def schema_connect_args(value: str) -> dict:
    """Search-path selection only. Decides nothing about transport security."""
    schema = schema_name(value)
    return {"server_settings": {"search_path": schema}} if schema else {}


def _local_exception_enabled() -> bool:
    # Exact match on a single literal. Not a truthiness test: "false", "0" and
    # "no" must not enable an unverified connection, and under a loose check
    # every one of those would.
    return os.environ.get(LOCAL_EXCEPTION_VARIABLE, "") == "true"


def _require_local_database(database_url: str | None) -> None:
    """Only explicitly local effective asyncpg hosts may bypass verification."""
    from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
    from sqlalchemy.engine import make_url

    try:
        url = make_url(database_url or os.environ.get("DATABASE_URL", ""))
        if url.drivername not in {"postgresql", "postgresql+asyncpg"}:
            raise ValueError("unsupported database driver")
        # Use the same URL interpretation as the real SQLAlchemy connection.
        # In particular, query-string hosts override the authority's hostname,
        # and a multi-host URL must not hide a remote fallback behind localhost.
        _, options = PGDialect_asyncpg().create_connect_args(url)
        hosts = options.get("host")
        hosts = hosts if isinstance(hosts, (list, tuple)) else [hosts]
        for host in hosts:
            if not isinstance(host, str) or not host or "\x00" in host:
                raise ValueError("an explicit local host is required")
            if (
                host.startswith("/")
                or host.lower() == "localhost"
                or host == LOCAL_FIXTURE_HOST
            ):
                continue
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError("remote database host")
    except Exception:
        # A DSN can contain a password. Never include it or parser diagnostics.
        raise DatabaseTransportUnverifiable(
            f"{LOCAL_EXCEPTION_VARIABLE} requires an explicit loopback address "
            "or local Unix socket (or the guarded integration fixture) for every "
            "database host; remote and implicit "
            "targets require a trusted database CA."
        ) from None


def transport_connect_args(database_url: str | None = None) -> dict:
    """Return asyncpg transport arguments, verified by default.

    Fails closed: with the exception unset and no CA available, this raises
    rather than returning ``{}``. Returning an empty dict is what handed the
    decision to the driver's ``prefer`` default in the first place.
    """
    ca = os.environ.get(CA_VARIABLE)
    if not ca or not ca.strip():
        if _local_exception_enabled():
            _require_local_database(database_url)
            # Unverified AND explicitly so. sslmode=disable rather than a
            # weakened context: a half-checked connection that reports itself
            # as encrypted is more misleading to an operator than a plainly
            # unencrypted one, and this exception exists for a throwaway local
            # database that has no certificate to check at all.
            logger.warning(
                "%s=true: connecting to the database WITHOUT certificate or "
                "hostname verification. This is supported only for a local "
                "throwaway database. Tenant records, spend rows and audit "
                "history on any shared environment are exposed in transit "
                "under this setting.",
                LOCAL_EXCEPTION_VARIABLE,
            )
            return {"ssl": "disable"}
        raise DatabaseTransportUnverifiable(
            f"{CA_VARIABLE} is not set, so the database certificate cannot be "
            f"verified. Supply the trusted PEM bundle by reference from the "
            f"deployment's managed secret (key 'ca-pem' of the database "
            f"secret). For a local throwaway database only, set "
            f"{LOCAL_EXCEPTION_VARIABLE}=true to connect without verification."
        )

    # An explicit context pins server trust for asyncpg without incompatible
    # libpq URL parameters. create_default_context gives CERT_REQUIRED and
    # check_hostname=True; both are re-asserted below so that the security
    # property is stated in this file rather than inherited from a default
    # that a future Python release could relax.
    context = ssl.create_default_context(cadata=ca)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    return {"ssl": context}


def connect_args(value: str, database_url: str | None = None) -> dict:
    """Combined args for every path that opens a Superplane connection.

    One helper so the service, the Alembic chain and the one-shot jobs cannot
    drift apart on transport. Guarding some entry points and not others leaves
    an equivalent gap open while the finding reads as closed.
    """
    return {**schema_connect_args(value), **transport_connect_args(database_url)}
