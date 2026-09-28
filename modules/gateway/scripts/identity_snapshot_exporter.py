#!/usr/bin/env python3
"""Bounded read-only PostgreSQL snapshot exporter for S11 identity provenance.

Connects to a dedicated read-only database role via projection views in the
``s11_inventory`` schema, exports per-tenant snapshot files compatible with
``identity_provenance_inventory.py``, and runs offline provenance classification
per tenant. No DDL, no writes, no base-table access, no secret storage.

Issue #6071 (S11 follow-up). The dedicated ``s11_inventory`` role, projection
views, and their grants are owned by root DBA setup, not this script.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1.0.0"
SCHEMA_NAME = "s11_inventory"
EXPECTED_ROLE = "s11_inventory"

# Bounds — compatible with identity_provenance_inventory.py
MAX_RUN_BYTES = 128 * 1024 * 1024
MAX_ROWS_PER_TABLE = 100_000
MAX_BYTES_PER_TENANT = 32 * 1024 * 1024  # 32 MiB

# Timeouts (seconds)
WALL_TIMEOUT = 300
STATEMENT_TIMEOUT_MS = 15_000
LOCK_TIMEOUT_MS = 2_000
IDLE_IN_TRANSACTION_TIMEOUT_MS = 60_000

# Views the DBA must have created in the s11_inventory schema
EXPECTED_VIEWS = {
    "organizations": ("id",),
    "identities": (
        "id",
        "org_id",
        "user_id",
        "provider",
        "provider_user_id",
        "verification_method",
        "created_at",
        "verified_at",
        "updated_at",
        "team_id",
        "is_primary",
    ),
    "users": (
        "id",
        "org_id",
        "is_shadow",
        "user_kind",
        "bot_kind",
        "created_at",
        "updated_at",
    ),
    "audit": (
        "id",
        "org_id",
        "event_type",
        "actor_id",
        "created_at",
        "details",
    ),
}

AUDIT_EVENT_ALLOWLIST = (
    "shadow_user_created",
    "identity_linked",
    "identity_unlinked",
    "magic_link_consumed",
    "magic_link_issued",
)

AUDIT_DETAIL_KEYS = (
    "provider",
    "provider_user_id",
    "shadow_user_id",
    "verification_method",
    "delivery_method",
    "ownership_proven",
)


class ExportError(Exception):
    """Fixed error codes; never include untrusted values in messages."""


# ---------------------------------------------------------------------------
# Inventory bridge — import the sibling offline classifier at runtime
# ---------------------------------------------------------------------------

_INVENTORY_PATH = Path(__file__).resolve().parent / "identity_provenance_inventory.py"


def _load_inventory():
    """Load the offline inventory module from its file path."""
    spec = importlib.util.spec_from_file_location("identity_provenance_inventory", _INVENTORY_PATH)
    if spec is None or spec.loader is None:
        raise ExportError("inventory_module_not_found")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# IAM token generation (production path; tests bypass via DATABASE_URL)
# ---------------------------------------------------------------------------


def generate_iam_token(host: str, port: int, user: str, region: str) -> str:
    """Generate an ephemeral RDS IAM auth token. Never stored or printed."""
    import boto3  # deferred so tests never import boto3

    client = boto3.client("rds", region_name=region)
    return client.generate_db_auth_token(DBHostname=host, Port=port, DBUsername=user, Region=region)


# ---------------------------------------------------------------------------
# Connection and safety verification
# ---------------------------------------------------------------------------


def _connect(dsn: str):
    """Open a psycopg2 connection with timeouts and verify-full intent."""
    import psycopg2  # deferred; tests provide their own connection

    conn = psycopg2.connect(dsn, connect_timeout=10)
    return conn


def verify_connection(cursor) -> None:
    """Verify principal, isolation, read-only status, timezone, and views."""
    # Principal
    cursor.execute("SELECT current_user")
    role = cursor.fetchone()[0]
    if role != EXPECTED_ROLE:
        raise ExportError("unexpected_principal")

    # Transaction isolation
    cursor.execute("SHOW transaction_isolation")
    isolation = cursor.fetchone()[0]
    if isolation != "repeatable read":
        raise ExportError("unexpected_isolation_level")

    # Read-only
    cursor.execute("SHOW transaction_read_only")
    read_only = cursor.fetchone()[0]
    if read_only != "on":
        raise ExportError("transaction_not_read_only")

    # Timezone
    cursor.execute("SHOW timezone")
    tz = cursor.fetchone()[0]
    if tz != "UTC":
        raise ExportError("unexpected_timezone")

    # View existence, columns, ownership
    for view_name, expected_cols in EXPECTED_VIEWS.items():
        # Check view exists and is a view (not a table)
        cursor.execute(
            "SELECT table_type FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
            (SCHEMA_NAME, view_name),
        )
        row = cursor.fetchone()
        if row is None:
            raise ExportError("missing_view")
        if row[0] != "VIEW":
            raise ExportError("expected_view_not_table")

        # Check columns
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (SCHEMA_NAME, view_name),
        )
        actual_cols = tuple(r[0] for r in cursor.fetchall())
        if actual_cols != expected_cols:
            raise ExportError("view_column_mismatch")

    verify_effective_privileges(cursor)


def verify_effective_privileges(cursor):
    """Include PUBLIC and every reachable role, even NOINHERIT/SET ROLE paths."""
    roles = """WITH RECURSIVE reachable(oid) AS (
        SELECT oid FROM pg_roles WHERE rolname=current_user
        UNION SELECT m.roleid FROM pg_auth_members m JOIN reachable r ON m.member=r.oid
    ) """
    cursor.execute(
        roles
        + """SELECT EXISTS (
        SELECT FROM reachable r JOIN pg_roles p ON p.oid=r.oid
        WHERE p.rolsuper OR p.rolcreatedb OR p.rolcreaterole OR p.rolreplication OR p.rolbypassrls
    ) OR EXISTS (
        SELECT FROM reachable r, pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog','information_schema','s11_inventory')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND c.relkind IN ('r','p','v','m','f')
          AND (has_any_column_privilege(r.oid,c.oid,'SELECT,INSERT,UPDATE,REFERENCES')
            OR has_table_privilege(r.oid,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'))
    ) OR EXISTS (
        SELECT FROM reachable r, pg_namespace n
        WHERE n.nspname NOT LIKE 'pg_temp_%' AND has_schema_privilege(r.oid,n.oid,'CREATE')
    ) OR EXISTS (
        SELECT FROM reachable r WHERE has_database_privilege(r.oid,current_database(),'CREATE')
    )"""
    )
    if cursor.fetchone()[0]:
        raise ExportError("unexpected_effective_privileges")
    cursor.execute(
        roles
        + """SELECT EXISTS (
        SELECT FROM reachable r, pg_proc p
        WHERE p.prosecdef AND has_function_privilege(r.oid,p.oid,'EXECUTE')
    )"""
    )
    if cursor.fetchone()[0]:
        raise ExportError("security_definer_routine_accessible")
    cursor.execute("""SELECT count(*)=4 AND bool_and(
        c.relkind='v' AND c.relowner<>(SELECT oid FROM pg_roles WHERE rolname=current_user)
        AND c.reloptions @> ARRAY['security_barrier=true']::text[]
        AND has_table_privilege(current_user,c.oid,'SELECT')
        AND NOT has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
    ) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='s11_inventory'""")
    if cursor.fetchone()[0] is not True:
        raise ExportError("unexpected_projection_authority")


# ---------------------------------------------------------------------------
# Tenant discovery
# ---------------------------------------------------------------------------


def _table(name: str) -> str:
    """Return a schema-qualified table reference. Empty SCHEMA_NAME omits the dot."""
    return f"{SCHEMA_NAME}.{name}" if SCHEMA_NAME else name


class SnapshotCursor:
    """Stream each query from a fresh server cursor in the same transaction."""

    def __init__(self, connection, deadline):
        self.connection = connection
        self.deadline = deadline
        self.cursor = None
        self.sequence = 0

    def check_deadline(self):
        if time.monotonic() >= self.deadline:
            raise ExportError("wall_timeout")

    def execute(self, query, params=None):
        self.check_deadline()
        if self.cursor is not None:
            self.cursor.close()
        remaining_ms = max(1, min(STATEMENT_TIMEOUT_MS, int((self.deadline - time.monotonic()) * 1000)))
        with self.connection.cursor() as control:
            control.execute("SELECT set_config('statement_timeout', %s, true)", (str(remaining_ms),))
        self.sequence += 1
        self.cursor = self.connection.cursor(name=f"s11_snapshot_{self.sequence}")
        self.cursor.execute(query, params)

    def fetchone(self):
        self.check_deadline()
        row = self.cursor.fetchone()
        self.check_deadline()
        return row


def _bounded_rows(cursor, *, row_limit=None, byte_limit=None):
    row_limit = MAX_ROWS_PER_TABLE if row_limit is None else row_limit
    byte_limit = MAX_BYTES_PER_TENANT if byte_limit is None else byte_limit
    rows = []
    size = 0
    while True:
        row = cursor.fetchone()
        if row is None:
            return rows
        if len(rows) >= row_limit:
            raise ExportError("row_limit_exceeded")
        size += len(_canonical_bytes(row))
        if size > byte_limit:
            raise ExportError("tenant_byte_limit_exceeded")
        rows.append(row)


def discover_tenants(cursor) -> list[str]:
    """Canonical tenant set, bounded including orphan tenant IDs."""
    cursor.execute(
        f"SELECT id FROM {_table('organizations')} "
        f"UNION SELECT DISTINCT org_id FROM {_table('identities')} "
        f"UNION SELECT DISTINCT org_id FROM {_table('users')} "
        f"UNION SELECT DISTINCT org_id FROM {_table('audit')}"
    )
    tenants = [r[0] for r in _bounded_rows(cursor, row_limit=10_000, byte_limit=1024 * 1024)]
    if any(not isinstance(t, str) or not t for t in tenants):
        raise ExportError("invalid_tenant_scope")
    return sorted(tenants)


# ---------------------------------------------------------------------------
# Per-tenant data fetch
# ---------------------------------------------------------------------------


def _json_serial(obj: Any) -> str:
    """JSON serializer for datetime objects."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


def _filter_audit_details(details: dict | None) -> dict | None:
    """Allowlist audit detail keys; preserve ownership_proven type."""
    if details is None:
        return None
    filtered = {}
    for key in AUDIT_DETAIL_KEYS:
        if key in details:
            filtered[key] = details[key]
    return filtered if filtered else None


def fetch_tenant_identities(cursor, tenant_id: str) -> list[dict]:
    """Fetch identities for a tenant from the projection view."""
    cursor.execute(
        f"SELECT id, org_id, user_id, provider, provider_user_id, "
        f"verification_method, created_at, verified_at, updated_at, "
        f"team_id, is_primary "
        f"FROM {_table('identities')} WHERE org_id = %s ORDER BY id",
        (tenant_id,),
    )
    rows = _bounded_rows(cursor)
    result = []
    for row in rows:
        result.append(
            {
                "id": row[0],
                "org_id": row[1],
                "user_id": row[2],
                "provider": row[3],
                "provider_user_id": row[4],
                "verification_method": row[5],
                "created_at": row[6].isoformat() if isinstance(row[6], datetime) else row[6],
                "verified_at": row[7].isoformat() if isinstance(row[7], datetime) else row[7],
                "updated_at": row[8].isoformat() if isinstance(row[8], datetime) else row[8],
                "team_id": row[9],
                "is_primary": bool(row[10]) if row[10] is not None else None,
            }
        )
    return result


def fetch_tenant_users(cursor, tenant_id: str) -> list[dict]:
    """Fetch users for a tenant, including cross-tenant referenced users."""
    cursor.execute(
        f"SELECT u.id, u.org_id, u.is_shadow, u.user_kind, u.bot_kind, "
        f"u.created_at, u.updated_at "
        f"FROM {_table('users')} u "
        f"WHERE u.org_id = %s "
        f"OR EXISTS (SELECT 1 FROM {_table('identities')} i "
        f"           WHERE i.org_id = %s AND i.user_id = u.id) "
        f"ORDER BY u.id",
        (tenant_id, tenant_id),
    )
    rows = _bounded_rows(cursor)
    result = []
    for row in rows:
        result.append(
            {
                "id": row[0],
                "org_id": row[1],
                "is_shadow": bool(row[2]) if row[2] is not None else None,
                "user_kind": row[3],
                "bot_kind": row[4],
                "created_at": row[5].isoformat() if isinstance(row[5], datetime) else row[5],
                "updated_at": row[6].isoformat() if isinstance(row[6], datetime) else row[6],
            }
        )
    return result


def fetch_tenant_audit(cursor, tenant_id: str) -> list[dict]:
    """Fetch allowlisted audit events with filtered details."""
    placeholders = ",".join(["%s"] * len(AUDIT_EVENT_ALLOWLIST))
    cursor.execute(
        f"SELECT id, org_id, event_type, actor_id, details, created_at "
        f"FROM {_table('audit')} "
        f"WHERE org_id = %s AND event_type IN ({placeholders}) "
        f"ORDER BY created_at, id",
        (tenant_id, *AUDIT_EVENT_ALLOWLIST),
    )
    rows = _bounded_rows(cursor)
    result = []
    for row in rows:
        raw_details = row[4]
        if isinstance(raw_details, str):
            try:
                raw_details = json.loads(raw_details)
            except (json.JSONDecodeError, TypeError):
                raw_details = None
        result.append(
            {
                "id": row[0],
                "org_id": row[1],
                "event_type": row[2],
                "actor_id": row[3],
                "details": _filter_audit_details(raw_details),
                "created_at": row[5].isoformat() if isinstance(row[5], datetime) else row[5],
            }
        )
    return result


# ---------------------------------------------------------------------------
# Snapshot assembly and output
# ---------------------------------------------------------------------------


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=_json_serial, allow_nan=False).encode()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_snapshot(snapshot: dict, path: str) -> int:
    """Write snapshot JSON to an exclusive 0600 file. Returns bytes written."""
    content = _canonical_bytes(snapshot) + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        # fd is already closed by fdopen context manager on success;
        # on exception during write, fdopen still owns it.
        raise
    return len(content)


def create_run_directory(base_dir: str) -> str:
    """Create a unique 0700 run directory under base_dir."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = os.path.join(base_dir, f"run-{timestamp}-{os.getpid()}")
    os.makedirs(run_dir, mode=0o700, exist_ok=False)
    return run_dir


# ---------------------------------------------------------------------------
# Main export orchestration
# ---------------------------------------------------------------------------


def export_snapshot(
    cursor,
    output_dir: str,
    source_sha: str,
    *,
    inventory_module=None,
    wall_start: float | None = None,
) -> dict:
    """Run the full export. Receipt contains tenant IDs and stays private."""
    if wall_start is None:
        wall_start = time.monotonic()

    if inventory_module is None:
        inventory_module = _load_inventory()

    tenants = discover_tenants(cursor)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "exporter": "identity_snapshot_exporter",
        "started_at": datetime.now(UTC).isoformat(),
        "source_sha": source_sha,
        "tenant_count": len(tenants),
        "tenants": {},
        "status": "INCOMPLETE",
    }

    if not tenants:
        receipt["status"] = "EMPTY"
        receipt["completed_at"] = datetime.now(UTC).isoformat()
        return receipt

    all_complete = True
    output_bytes = 0

    for tenant_index, tenant_id in enumerate(tenants):
        # Wall-clock check
        elapsed = time.monotonic() - wall_start
        if elapsed > WALL_TIMEOUT:
            receipt["tenants"][tenant_id] = {"status": "SKIPPED", "reason": "wall_timeout"}
            all_complete = False
            continue

        tenant_receipt: dict[str, Any] = {"status": "INCOMPLETE"}
        try:
            # Fetch data
            identities = fetch_tenant_identities(cursor, tenant_id)
            users = fetch_tenant_users(cursor, tenant_id)
            audit_logs = fetch_tenant_audit(cursor, tenant_id)

            snapshot = {
                "user_identities": identities,
                "users": users,
                "security_audit_logs": audit_logs,
            }

            # Check byte limit
            snapshot_bytes = _canonical_bytes(snapshot)
            if len(snapshot_bytes) > MAX_BYTES_PER_TENANT:
                raise ExportError("tenant_byte_limit_exceeded")

            if time.monotonic() - wall_start >= WALL_TIMEOUT:
                raise ExportError("wall_timeout")

            # Write snapshot file (no raw IDs in filename)
            snapshot_hash = _sha256(snapshot_bytes)
            snapshot_filename = f"snapshot-{tenant_index:06d}-{snapshot_hash[:16]}.json"
            snapshot_path = os.path.join(output_dir, snapshot_filename)
            if output_bytes + len(snapshot_bytes) + 1 > MAX_RUN_BYTES:
                raise ExportError("run_byte_limit_exceeded")
            bytes_written = write_snapshot(snapshot, snapshot_path)
            output_bytes += bytes_written

            tenant_receipt["snapshot_file"] = snapshot_filename
            tenant_receipt["snapshot_hash"] = snapshot_hash
            tenant_receipt["snapshot_file_sha256"] = _sha256(snapshot_bytes + b"\n")
            tenant_receipt["bytes_written"] = bytes_written
            tenant_receipt["row_counts"] = {
                "user_identities": len(identities),
                "users": len(users),
                "security_audit_logs": len(audit_logs),
            }

            # Run offline inventory classification per tenant
            # Close/ROLLBACK is not needed here; the cursor stays in its
            # REPEATABLE READ snapshot, but we do not read after this point.
            try:
                manifest = inventory_module.build_manifest(snapshot, tenant_id, source_sha)
                manifest_filename = f"manifest-{tenant_index:06d}-{snapshot_hash[:16]}.json"
                manifest_path = os.path.join(output_dir, manifest_filename)
                if output_bytes + len(_canonical_bytes(manifest)) + 1 > MAX_RUN_BYTES:
                    raise ExportError("run_byte_limit_exceeded")
                output_bytes += write_snapshot(manifest, manifest_path)
                tenant_receipt["manifest_file"] = manifest_filename
                tenant_receipt["manifest_hash"] = manifest["manifest_hash"]
                tenant_receipt["manifest_file_sha256"] = _sha256(_canonical_bytes(manifest) + b"\n")
                tenant_receipt["has_unresolved"] = manifest["has_unresolved"]
                tenant_receipt["classification_summary"] = manifest["classification_summary"]
                # Export complete; provenance outcome is separate
                if time.monotonic() - wall_start >= WALL_TIMEOUT:
                    raise ExportError("wall_timeout")
                tenant_receipt["status"] = "EXPORTED"
            except Exception:
                # Classification failure does not invalidate the snapshot export
                tenant_receipt["status"] = "EXPORTED_CLASSIFICATION_FAILED"
                all_complete = False

        except ExportError as e:
            tenant_receipt["status"] = "FAILED"
            tenant_receipt["error_code"] = str(e)
            all_complete = False
        except Exception:
            tenant_receipt["status"] = "FAILED"
            tenant_receipt["error_code"] = "unexpected_error"
            all_complete = False

        receipt["tenants"][tenant_id] = tenant_receipt

    receipt["completed_at"] = datetime.now(UTC).isoformat()
    if all_complete:
        receipt["status"] = "COMPLETE"

    return receipt


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_dsn(host: str, port: int, dbname: str, user: str, *, password: str | None = None, sslmode: str = "verify-full") -> str:
    """Build a PostgreSQL DSN. Password is an ephemeral IAM token, never stored."""
    from psycopg2.extensions import make_dsn

    return make_dsn(host=host, port=port, dbname=dbname, user=user, sslmode=sslmode, password=password)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded read-only snapshot exporter for S11 identity provenance")
    parser.add_argument("--host", required=True, help="Database hostname")
    parser.add_argument("--sslrootcert", required=True, help="Reviewed RDS CA bundle path")
    parser.add_argument("--port", type=int, default=5432, help="Database port")
    parser.add_argument("--dbname", required=True, help="Database name")
    parser.add_argument("--user", default=EXPECTED_ROLE, help="Database role")
    parser.add_argument("--output-dir", required=True, help="Base output directory")
    parser.add_argument("--source-sha", required=True, help="Source commit SHA for provenance")
    parser.add_argument("--region", default="us-east-1", help="AWS region for IAM token")
    args = parser.parse_args(argv)

    import re

    if not re.fullmatch(r"[0-9a-f]{40}", args.source_sha):
        print("ERROR: invalid source SHA", file=sys.stderr)
        return 2

    try:
        import psycopg2

        # Generate ephemeral IAM token
        token = generate_iam_token(args.host, args.port, args.user, args.region)
        dsn = build_dsn(args.host, args.port, args.dbname, args.user, password=token)

        run_dir = create_run_directory(args.output_dir)

        conn = psycopg2.connect(dsn, connect_timeout=10, sslrootcert=args.sslrootcert)
        try:
            conn.autocommit = False
            cursor = conn.cursor()

            # Set timeouts
            cursor.execute("SET statement_timeout = %s", (STATEMENT_TIMEOUT_MS,))
            cursor.execute("SET lock_timeout = %s", (LOCK_TIMEOUT_MS,))
            cursor.execute("SET idle_in_transaction_session_timeout = %s", (IDLE_IN_TRANSACTION_TIMEOUT_MS,))

            # Begin REPEATABLE READ READ ONLY
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute("SET LOCAL timezone = 'UTC'")

            # Verify connection safety
            verify_connection(cursor)

            wall_start = time.monotonic()
            receipt = export_snapshot(SnapshotCursor(conn, wall_start + WALL_TIMEOUT), run_dir, args.source_sha, wall_start=wall_start)

            # Write private receipt (tenant identifiers; never publish raw receipt)
            receipt_path = os.path.join(run_dir, "receipt.json")
            write_snapshot(receipt, receipt_path)

        finally:
            # Always ROLLBACK; we never write
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()

    except ExportError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except Exception:
        print("ERROR: export failed; no identity data written to stdout", file=sys.stderr)
        return 2

    # Redacted summary on stdout
    status = receipt.get("status", "UNKNOWN")
    tenant_count = receipt.get("tenant_count", 0)
    exported = sum(1 for t in receipt.get("tenants", {}).values() if t.get("status", "").startswith("EXPORTED"))
    print(f"Snapshot export {status}: {exported}/{tenant_count} tenants exported")
    if receipt.get("status") == "COMPLETE":
        print(f"Run directory: {run_dir}")

    return 0 if status == "COMPLETE" else 1


if __name__ == "__main__":
    sys.exit(main())
