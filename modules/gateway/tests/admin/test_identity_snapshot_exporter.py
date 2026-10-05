"""Tests for the S11 identity snapshot exporter.

Uses disposable local database (SQLite for query logic, pgserver for Postgres
isolation and privilege tests where available). Never connects to a live DB.
Dummy AWS credentials and IMDS are disabled for all tests.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import stat
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/identity_snapshot_exporter.py"
INVENTORY_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/identity_provenance_inventory.py"

TENANT_A = "org-tenant-alpha"
TENANT_B = "org-tenant-beta"
ORPHAN_TENANT = "org-orphan-only"
SOURCE_SHA = "a" * 40


# ---------------------------------------------------------------------------
# Module loading (no network, no AWS)
# ---------------------------------------------------------------------------


@pytest.fixture
def exporter(monkeypatch):
    """Load the exporter module with network and AWS disabled."""

    def no_network(*args, **kwargs):
        raise AssertionError("network forbidden in tests")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    # Disable IMDS
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    spec = importlib.util.spec_from_file_location("identity_snapshot_exporter", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def inventory(monkeypatch):
    """Load the offline inventory module."""

    def no_network(*args, **kwargs):
        raise AssertionError("network forbidden in tests")

    monkeypatch.setattr(socket, "socket", no_network)
    spec = importlib.util.spec_from_file_location("identity_provenance_inventory", INVENTORY_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# SQLite-based cursor mock — matches the psycopg2 cursor interface for
# column-positional fetchall() results
# ---------------------------------------------------------------------------


class SqliteCursorAdapter:
    """Wraps a sqlite3 cursor to behave like psycopg2 for the exporter's queries.

    The exporter uses %s placeholders (psycopg2 style). SQLite uses ? style.
    This adapter translates on the fly.
    """

    def __init__(self, conn):
        self._conn = conn
        self._cursor = conn.cursor()
        self._last_result = None

    def execute(self, query, params=None):
        # Translate %s to ? for SQLite
        translated = query.replace("%s", "?")
        if params is None:
            self._cursor.execute(translated)
        elif isinstance(params, dict):
            self._cursor.execute(translated, params)
        else:
            self._cursor.execute(translated, params)
        self._last_result = self._cursor

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


# ---------------------------------------------------------------------------
# Test database setup
# ---------------------------------------------------------------------------


def _create_test_db(exporter):
    """Create an in-memory SQLite database with the s11_inventory schema views.

    Since SQLite doesn't have schemas, we create tables directly and
    patch the exporter's schema reference. The verify_connection checks
    are tested separately with mocks.
    """
    import sqlite3

    db = sqlite3.connect(":memory:")

    # Register jsonb_build_object for audit detail filtering
    db.create_function("jsonb_build_object", -1, lambda *args: json.dumps(dict(zip(args[::2], args[1::2]))))

    # Create tables matching the projection views
    db.execute("CREATE TABLE organizations(id TEXT PRIMARY KEY)")
    db.execute(
        "CREATE TABLE identities("
        "id TEXT, org_id TEXT, user_id TEXT, provider TEXT, provider_user_id TEXT, "
        "verification_method TEXT, created_at TEXT, verified_at TEXT, updated_at TEXT, "
        "team_id TEXT, is_primary INTEGER)"
    )
    db.execute("CREATE TABLE users(id TEXT, org_id TEXT, is_shadow INTEGER, user_kind TEXT, bot_kind TEXT, created_at TEXT, updated_at TEXT)")
    db.execute("CREATE TABLE audit(id TEXT, org_id TEXT, event_type TEXT, actor_id TEXT, details TEXT, created_at TEXT)")
    return db


def _seed_canonical_data(db):
    """Seed a canonical test dataset with multiple tenants and cross-references."""
    # Organizations
    db.execute("INSERT INTO organizations VALUES (?)", (TENANT_A,))
    db.execute("INSERT INTO organizations VALUES (?)", (TENANT_B,))
    # ORPHAN_TENANT has no org row — it appears only in identities

    # Identities
    db.executemany(
        "INSERT INTO identities VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "id-a1",
                TENANT_A,
                "user-a1",
                "github",
                "gh-100",
                "oauth",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                None,
                1,
            ),
            ("id-a2", TENANT_A, "user-a2", "github", "gh-200", "admin_manual", "2026-02-01T00:00:00+00:00", None, None, "team-1", 0),
            ("id-b1", TENANT_B, "user-b1", "slack", "sl-100", "magic_link", "2026-03-01T00:00:00+00:00", None, None, None, 0),
            # Cross-tenant: identity in ORPHAN_TENANT references user-a1 from TENANT_A
            ("id-o1", ORPHAN_TENANT, "user-a1", "github", "gh-300", "self_asserted", "2026-04-01T00:00:00+00:00", None, None, None, 0),
        ],
    )

    # Users
    db.executemany(
        "INSERT INTO users VALUES (?,?,?,?,?,?,?)",
        [
            ("user-a1", TENANT_A, 0, "human", None, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            ("user-a2", TENANT_A, 1, "human", None, "2026-02-01T00:00:00+00:00", None),
            ("user-b1", TENANT_B, 0, "human", None, "2026-03-01T00:00:00+00:00", None),
        ],
    )

    # Audit events
    db.executemany(
        "INSERT INTO audit VALUES (?,?,?,?,?,?)",
        [
            (
                "evt-1",
                TENANT_A,
                "shadow_user_created",
                None,
                json.dumps({"provider": "github", "provider_user_id": "gh-200", "shadow_user_id": "user-a2", "extra_secret": "should_be_filtered"}),
                "2026-02-01T00:00:01+00:00",
            ),
            (
                "evt-2",
                TENANT_A,
                "identity_linked",
                "user-a1",
                json.dumps({"provider": "github", "provider_user_id": "gh-100", "ownership_proven": True}),
                "2026-01-01T00:00:01+00:00",
            ),
            # Event type NOT in allowlist — should be excluded
            (
                "evt-3",
                TENANT_A,
                "login_attempt",
                "user-a1",
                json.dumps({"provider": "github"}),
                "2026-01-01T00:00:02+00:00",
            ),
        ],
    )
    db.commit()


@pytest.fixture
def test_db(exporter):
    db = _create_test_db(exporter)
    _seed_canonical_data(db)
    yield db
    db.close()


@pytest.fixture
def cursor(exporter, test_db, monkeypatch):
    """Provide a SQLite cursor adapter, patching schema references for SQLite."""
    # Patch SCHEMA_NAME to empty string for SQLite (no schema support)
    monkeypatch.setattr(exporter, "SCHEMA_NAME", "")
    adapter = SqliteCursorAdapter(test_db)
    return adapter


def _patch_schema_refs(exporter, monkeypatch):
    """Remove schema prefix from queries for SQLite compatibility."""
    monkeypatch.setattr(exporter, "SCHEMA_NAME", "")


# ---------------------------------------------------------------------------
# Core export tests
# ---------------------------------------------------------------------------


class TestTenantDiscovery:
    def test_discovers_all_tenants_including_orphans(self, exporter, cursor, monkeypatch):
        _patch_schema_refs(exporter, monkeypatch)
        tenants = exporter.discover_tenants(cursor)
        assert TENANT_A in tenants
        assert TENANT_B in tenants
        assert ORPHAN_TENANT in tenants

    def test_sorted_deterministic_order(self, exporter, cursor, monkeypatch):
        _patch_schema_refs(exporter, monkeypatch)
        tenants = exporter.discover_tenants(cursor)
        assert tenants == sorted(tenants)

    def test_empty_database_returns_empty(self, exporter, monkeypatch):
        import sqlite3

        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE organizations(id TEXT)")
        db.execute("CREATE TABLE identities(id TEXT, org_id TEXT)")
        db.execute("CREATE TABLE users(id TEXT, org_id TEXT)")
        db.execute("CREATE TABLE audit(id TEXT, org_id TEXT)")
        monkeypatch.setattr(exporter, "SCHEMA_NAME", "")
        adapter = SqliteCursorAdapter(db)
        assert exporter.discover_tenants(adapter) == []
        db.close()


class TestIdentityFetch:
    def test_fetches_correct_tenant_identities(self, exporter, cursor, monkeypatch):
        _patch_schema_refs(exporter, monkeypatch)
        identities = exporter.fetch_tenant_identities(cursor, TENANT_A)
        assert len(identities) == 2
        ids = {i["id"] for i in identities}
        assert ids == {"id-a1", "id-a2"}

    def test_preserves_all_fields(self, exporter, cursor, monkeypatch):
        _patch_schema_refs(exporter, monkeypatch)
        identities = exporter.fetch_tenant_identities(cursor, TENANT_A)
        first = next(i for i in identities if i["id"] == "id-a1")
        assert first["org_id"] == TENANT_A
        assert first["user_id"] == "user-a1"
        assert first["provider"] == "github"
        assert first["provider_user_id"] == "gh-100"
        assert first["verification_method"] == "oauth"
        assert first["is_primary"] is True

    def test_null_verification_method_is_preserved(self, exporter, test_db, monkeypatch):
        monkeypatch.setattr(exporter, "SCHEMA_NAME", "")
        test_db.execute(
            "INSERT INTO identities VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("id-null", TENANT_A, "user-a1", "github", "gh-null", None, "2026-01-01T00:00:00+00:00", None, None, None, 0),
        )
        test_db.commit()
        adapter = SqliteCursorAdapter(test_db)
        identities = exporter.fetch_tenant_identities(adapter, TENANT_A)
        null_id = next(i for i in identities if i["id"] == "id-null")
        assert null_id["verification_method"] is None


class TestUserFetch:
    def test_includes_cross_tenant_referenced_users(self, exporter, cursor, monkeypatch):
        _patch_schema_refs(exporter, monkeypatch)
        # ORPHAN_TENANT has identity id-o1 referencing user-a1 from TENANT_A
        users = exporter.fetch_tenant_users(cursor, ORPHAN_TENANT)
        user_ids = {u["id"] for u in users}
        # user-a1 should be included because the ORPHAN_TENANT identity references it
        assert "user-a1" in user_ids

    def test_own_tenant_users(self, exporter, cursor, monkeypatch):
        _patch_schema_refs(exporter, monkeypatch)
        users = exporter.fetch_tenant_users(cursor, TENANT_A)
        user_ids = {u["id"] for u in users}
        assert "user-a1" in user_ids
        assert "user-a2" in user_ids

    def test_boolean_types_preserved(self, exporter, cursor, monkeypatch):
        _patch_schema_refs(exporter, monkeypatch)
        users = exporter.fetch_tenant_users(cursor, TENANT_A)
        shadow_user = next(u for u in users if u["id"] == "user-a2")
        assert shadow_user["is_shadow"] is True
        normal_user = next(u for u in users if u["id"] == "user-a1")
        assert normal_user["is_shadow"] is False


class TestAuditFetch:
    def test_allowlist_filters_event_types(self, exporter, cursor, monkeypatch):
        _patch_schema_refs(exporter, monkeypatch)
        audit = exporter.fetch_tenant_audit(cursor, TENANT_A)
        event_types = {e["event_type"] for e in audit}
        # login_attempt should be excluded
        assert "login_attempt" not in event_types
        assert "shadow_user_created" in event_types
        assert "identity_linked" in event_types

    def test_detail_keys_filtered(self, exporter, cursor, monkeypatch):
        _patch_schema_refs(exporter, monkeypatch)
        audit = exporter.fetch_tenant_audit(cursor, TENANT_A)
        shadow_event = next(e for e in audit if e["event_type"] == "shadow_user_created")
        details = shadow_event["details"]
        assert "provider" in details
        assert "shadow_user_id" in details
        # extra_secret should be filtered out
        assert "extra_secret" not in details

    def test_ownership_proven_preserved(self, exporter, cursor, monkeypatch):
        _patch_schema_refs(exporter, monkeypatch)
        audit = exporter.fetch_tenant_audit(cursor, TENANT_A)
        link_event = next(e for e in audit if e["event_type"] == "identity_linked")
        assert link_event["details"]["ownership_proven"] is True

    def test_null_details_handled(self, exporter, test_db, monkeypatch):
        monkeypatch.setattr(exporter, "SCHEMA_NAME", "")
        test_db.execute(
            "INSERT INTO audit VALUES (?,?,?,?,?,?)",
            ("evt-null", TENANT_A, "magic_link_issued", "user-a1", None, "2026-05-01T00:00:00+00:00"),
        )
        test_db.commit()
        adapter = SqliteCursorAdapter(test_db)
        audit = exporter.fetch_tenant_audit(adapter, TENANT_A)
        null_event = next(e for e in audit if e["id"] == "evt-null")
        assert null_event["details"] is None


# ---------------------------------------------------------------------------
# Full export pipeline tests
# ---------------------------------------------------------------------------


class TestExportSnapshot:
    def test_canonical_export_produces_valid_snapshots(self, exporter, cursor, inventory, tmp_path, monkeypatch):
        _patch_schema_refs(exporter, monkeypatch)
        run_dir = str(tmp_path / "run")
        os.makedirs(run_dir, mode=0o700)

        receipt = exporter.export_snapshot(cursor, run_dir, SOURCE_SHA, inventory_module=inventory)
        assert receipt["tenant_count"] == 3
        # All tenants should be exported
        for tenant_id, tenant_data in receipt["tenants"].items():
            assert tenant_data["status"].startswith("EXPORTED"), f"{tenant_id}: {tenant_data}"

    def test_export_status_complete_when_all_classify(self, exporter, inventory, tmp_path, monkeypatch):
        """COMPLETE requires all tenants to both export and classify."""
        import sqlite3

        # Use a simple dataset with no cross-tenant references
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE organizations(id TEXT)")
        db.execute(
            "CREATE TABLE identities("
            "id TEXT, org_id TEXT, user_id TEXT, provider TEXT, "
            "provider_user_id TEXT, verification_method TEXT, "
            "created_at TEXT, verified_at TEXT, updated_at TEXT, "
            "team_id TEXT, is_primary INTEGER)"
        )
        db.execute("CREATE TABLE users(id TEXT, org_id TEXT, is_shadow INTEGER, user_kind TEXT, bot_kind TEXT, created_at TEXT, updated_at TEXT)")
        db.execute("CREATE TABLE audit(id TEXT, org_id TEXT, event_type TEXT, actor_id TEXT, details TEXT, created_at TEXT)")
        db.execute("INSERT INTO organizations VALUES (?)", ("org-simple",))
        db.execute(
            "INSERT INTO identities VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("id-1", "org-simple", "u-1", "github", "gh-1", "oauth", "2026-01-01T00:00:00+00:00", None, None, None, 0),
        )
        db.execute(
            "INSERT INTO users VALUES (?,?,?,?,?,?,?)",
            ("u-1", "org-simple", 0, "human", None, "2026-01-01T00:00:00+00:00", None),
        )
        db.commit()
        monkeypatch.setattr(exporter, "SCHEMA_NAME", "")
        adapter = SqliteCursorAdapter(db)

        run_dir = str(tmp_path / "run")
        os.makedirs(run_dir, mode=0o700)
        receipt = exporter.export_snapshot(adapter, run_dir, SOURCE_SHA, inventory_module=inventory)
        assert receipt["status"] == "COMPLETE"
        db.close()

    def test_cross_tenant_user_causes_classification_failure(self, exporter, cursor, inventory, tmp_path, monkeypatch):
        """Cross-tenant referenced users trigger inventory validation failure, marked separately."""
        _patch_schema_refs(exporter, monkeypatch)
        run_dir = str(tmp_path / "run")
        os.makedirs(run_dir, mode=0o700)

        receipt = exporter.export_snapshot(cursor, run_dir, SOURCE_SHA, inventory_module=inventory)
        # ORPHAN_TENANT has identity referencing user from TENANT_A — classification fails
        orphan = receipt["tenants"][ORPHAN_TENANT]
        assert orphan["status"] == "EXPORTED_CLASSIFICATION_FAILED"
        # The snapshot itself was written successfully
        assert "snapshot_file" in orphan
        # Overall status is INCOMPLETE because classification failed
        assert receipt["status"] != "COMPLETE"

    def test_snapshot_files_are_inventory_compatible(self, exporter, cursor, inventory, tmp_path, monkeypatch):
        """Verify that snapshot files can be loaded by the offline inventory."""
        _patch_schema_refs(exporter, monkeypatch)
        run_dir = str(tmp_path / "run")
        os.makedirs(run_dir, mode=0o700)

        receipt = exporter.export_snapshot(cursor, run_dir, SOURCE_SHA, inventory_module=inventory)

        for tenant_id, tenant_data in receipt["tenants"].items():
            if not tenant_data.get("snapshot_file"):
                continue
            snapshot_path = os.path.join(run_dir, tenant_data["snapshot_file"])
            with open(snapshot_path) as f:
                snapshot = json.load(f)
            # Should have the three expected tables
            assert set(snapshot.keys()) == {"user_identities", "users", "security_audit_logs"}
            # Each table should be a list
            for table in snapshot.values():
                assert isinstance(table, list)

    def test_orphan_tenant_exported(self, exporter, cursor, inventory, tmp_path, monkeypatch):
        """Orphan tenant (no org row, only identities) is still exported."""
        _patch_schema_refs(exporter, monkeypatch)
        run_dir = str(tmp_path / "run")
        os.makedirs(run_dir, mode=0o700)

        receipt = exporter.export_snapshot(cursor, run_dir, SOURCE_SHA, inventory_module=inventory)
        assert ORPHAN_TENANT in receipt["tenants"]
        assert receipt["tenants"][ORPHAN_TENANT]["status"].startswith("EXPORTED")

    def test_classification_remains_unresolved(self, exporter, cursor, inventory, tmp_path, monkeypatch):
        """Existing inventory behavior: rows with insufficient proof stay unresolved."""
        _patch_schema_refs(exporter, monkeypatch)
        run_dir = str(tmp_path / "run")
        os.makedirs(run_dir, mode=0o700)

        receipt = exporter.export_snapshot(cursor, run_dir, SOURCE_SHA, inventory_module=inventory)
        tenant_a = receipt["tenants"][TENANT_A]
        # The inventory never resolves anything as proven
        assert tenant_a["has_unresolved"] is True

    def test_export_completion_separate_from_provenance(self, exporter, cursor, inventory, tmp_path, monkeypatch):
        """Export status EXPORTED does not imply provenance is resolved."""
        _patch_schema_refs(exporter, monkeypatch)
        run_dir = str(tmp_path / "run")
        os.makedirs(run_dir, mode=0o700)

        receipt = exporter.export_snapshot(cursor, run_dir, SOURCE_SHA, inventory_module=inventory)
        for tenant_data in receipt["tenants"].values():
            # Status is EXPORTED (data acquisition), not "resolved" or "proven"
            assert "EXPORTED" in tenant_data["status"]
            # Even complete exports can have unresolved provenance
            if "has_unresolved" in tenant_data:
                # This is the inventory's determination, separate from export status
                assert isinstance(tenant_data["has_unresolved"], bool)


# ---------------------------------------------------------------------------
# Bounds and timeout enforcement
# ---------------------------------------------------------------------------


class TestBoundsEnforcement:
    def test_row_limit_marks_incomplete(self, exporter, cursor, inventory, tmp_path, monkeypatch):
        _patch_schema_refs(exporter, monkeypatch)
        monkeypatch.setattr(exporter, "MAX_ROWS_PER_TABLE", 1)
        run_dir = str(tmp_path / "run")
        os.makedirs(run_dir, mode=0o700)

        receipt = exporter.export_snapshot(cursor, run_dir, SOURCE_SHA, inventory_module=inventory)
        # TENANT_A has 2 identities, which exceeds limit of 1
        assert receipt["tenants"][TENANT_A]["status"] == "FAILED"
        assert receipt["status"] != "COMPLETE"

    def test_byte_limit_marks_incomplete(self, exporter, cursor, inventory, tmp_path, monkeypatch):
        _patch_schema_refs(exporter, monkeypatch)
        monkeypatch.setattr(exporter, "MAX_BYTES_PER_TENANT", 10)
        run_dir = str(tmp_path / "run")
        os.makedirs(run_dir, mode=0o700)

        receipt = exporter.export_snapshot(cursor, run_dir, SOURCE_SHA, inventory_module=inventory)
        for tenant_data in receipt["tenants"].values():
            assert tenant_data["status"] == "FAILED"
        assert receipt["status"] != "COMPLETE"

    def test_wall_timeout_skips_remaining_tenants(self, exporter, cursor, inventory, tmp_path, monkeypatch):
        _patch_schema_refs(exporter, monkeypatch)
        run_dir = str(tmp_path / "run")
        os.makedirs(run_dir, mode=0o700)

        # Start with wall_start far in the past to simulate timeout
        receipt = exporter.export_snapshot(
            cursor,
            run_dir,
            SOURCE_SHA,
            inventory_module=inventory,
            wall_start=time.monotonic() - 400,
        )
        # All tenants should be SKIPPED
        for tenant_data in receipt["tenants"].values():
            assert tenant_data["status"] == "SKIPPED"
        assert receipt["status"] != "COMPLETE"

    def test_partial_failure_never_complete(self, exporter, test_db, inventory, tmp_path, monkeypatch):
        """If any tenant fails, the entire run is INCOMPLETE."""
        monkeypatch.setattr(exporter, "SCHEMA_NAME", "")
        # Add many rows to TENANT_B to exceed a low limit
        for i in range(5):
            test_db.execute(
                "INSERT INTO identities VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"id-extra-{i}", TENANT_B, f"user-extra-{i}", "github", f"gh-extra-{i}", "oauth", "2026-01-01T00:00:00+00:00", None, None, None, 0),
            )
        test_db.commit()
        monkeypatch.setattr(exporter, "MAX_ROWS_PER_TABLE", 3)

        run_dir = str(tmp_path / "run")
        os.makedirs(run_dir, mode=0o700)
        adapter = SqliteCursorAdapter(test_db)
        receipt = exporter.export_snapshot(adapter, run_dir, SOURCE_SHA, inventory_module=inventory)
        assert receipt["status"] != "COMPLETE"


# ---------------------------------------------------------------------------
# Output security
# ---------------------------------------------------------------------------


class TestOutputSecurity:
    def test_exclusive_file_creation(self, exporter, tmp_path):
        snapshot = {"user_identities": [], "users": [], "security_audit_logs": []}
        path = str(tmp_path / "test.json")
        exporter.write_snapshot(snapshot, path)
        assert os.path.exists(path)
        # Writing again should fail (O_EXCL)
        with pytest.raises(OSError):
            exporter.write_snapshot(snapshot, path)

    def test_file_permissions_0600(self, exporter, tmp_path):
        snapshot = {"user_identities": [], "users": [], "security_audit_logs": []}
        path = str(tmp_path / "private.json")
        exporter.write_snapshot(snapshot, path)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_run_directory_permissions_0700(self, exporter, tmp_path):
        run_dir = exporter.create_run_directory(str(tmp_path))
        mode = stat.S_IMODE(os.stat(run_dir).st_mode)
        assert mode == 0o700

    def test_no_symlink_following(self, exporter, tmp_path):
        victim = tmp_path / "victim"
        victim.write_text("sensitive data")
        link = tmp_path / "link.json"
        link.symlink_to(victim)
        snapshot = {"user_identities": [], "users": [], "security_audit_logs": []}
        with pytest.raises(OSError):
            exporter.write_snapshot(snapshot, str(link))
        # Victim must be untouched
        assert victim.read_text() == "sensitive data"

    def test_no_raw_ids_in_receipt(self, exporter, cursor, inventory, tmp_path, monkeypatch):
        _patch_schema_refs(exporter, monkeypatch)
        run_dir = str(tmp_path / "run")
        os.makedirs(run_dir, mode=0o700)

        receipt = exporter.export_snapshot(cursor, run_dir, SOURCE_SHA, inventory_module=inventory)
        receipt_json = json.dumps(receipt)
        # No raw user/identity IDs in the receipt's serializable representation
        # The receipt keys are tenant IDs (org_id), which is the necessary minimum
        # for identifying which tenant was processed. But raw user/provider IDs
        # should not appear.
        assert "gh-100" not in receipt_json
        assert "gh-200" not in receipt_json
        assert "user-a1" not in receipt_json
        assert "user-a2" not in receipt_json

    def test_no_raw_ids_in_filenames(self, exporter, cursor, inventory, tmp_path, monkeypatch):
        _patch_schema_refs(exporter, monkeypatch)
        run_dir = str(tmp_path / "run")
        os.makedirs(run_dir, mode=0o700)

        exporter.export_snapshot(cursor, run_dir, SOURCE_SHA, inventory_module=inventory)
        filenames = os.listdir(run_dir)
        for filename in filenames:
            assert "gh-100" not in filename
            assert "user-a1" not in filename
            assert TENANT_A not in filename


# ---------------------------------------------------------------------------
# Connection verification
# ---------------------------------------------------------------------------


class TestConnectionVerification:
    def _mock_cursor(self, overrides=None):
        """Create a mock cursor that returns expected verification values."""
        defaults = {
            "current_user": ("s11_inventory",),
            "transaction_isolation": ("repeatable read",),
            "transaction_read_only": ("on",),
            "timezone": ("UTC",),
        }
        if overrides:
            defaults.update(overrides)

        results = []

        def mock_execute(query, params=None):
            nonlocal results
            q = query.strip().upper()
            if q == "SELECT CURRENT_USER":
                results.append(defaults["current_user"])
            elif "TRANSACTION_ISOLATION" in q:
                results.append(defaults["transaction_isolation"])
            elif "TRANSACTION_READ_ONLY" in q:
                results.append(defaults["transaction_read_only"])
            elif "TIMEZONE" in q:
                results.append(defaults["timezone"])
            elif "INFORMATION_SCHEMA.TABLES" in q:
                results.append(("VIEW",))
            elif "INFORMATION_SCHEMA.COLUMNS" in q:
                # Return expected columns for the queried view
                view_name = params[1] if params else None
                from scripts.identity_snapshot_exporter import EXPECTED_VIEWS

                if view_name and view_name in EXPECTED_VIEWS:
                    results.append(EXPECTED_VIEWS[view_name])
                else:
                    results.append(())
            elif "TABLE_PRIVILEGES" in q:
                results.append((0,))
            elif "ROUTINE_PRIVILEGES" in q:
                results.append((0,))
            else:
                results.append(None)

        def mock_fetchone():
            if results:
                return results.pop(0)
            return None

        def mock_fetchall():
            if results:
                r = results.pop(0)
                if isinstance(r, tuple):
                    return [(c,) for c in r]
                return r if isinstance(r, list) else []
            return []

        cursor = MagicMock()
        cursor.execute = mock_execute
        cursor.fetchone = mock_fetchone
        cursor.fetchall = mock_fetchall
        return cursor

    def test_wrong_principal_rejected(self, exporter):
        cursor = self._mock_cursor({"current_user": ("postgres",)})
        with pytest.raises(exporter.ExportError, match="unexpected_principal"):
            exporter.verify_connection(cursor)

    def test_wrong_isolation_rejected(self, exporter):
        cursor = self._mock_cursor({"transaction_isolation": ("read committed",)})
        with pytest.raises(exporter.ExportError, match="unexpected_isolation_level"):
            exporter.verify_connection(cursor)

    def test_not_read_only_rejected(self, exporter):
        cursor = self._mock_cursor({"transaction_read_only": ("off",)})
        with pytest.raises(exporter.ExportError, match="transaction_not_read_only"):
            exporter.verify_connection(cursor)

    def test_wrong_timezone_rejected(self, exporter):
        cursor = self._mock_cursor({"timezone": ("America/New_York",)})
        with pytest.raises(exporter.ExportError, match="unexpected_timezone"):
            exporter.verify_connection(cursor)


# ---------------------------------------------------------------------------
# Schema/privilege rejection
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_missing_view_detected(self, exporter):
        """If a required view is missing, export refuses."""
        results = []

        def mock_execute(query, params=None):
            q = query.strip().upper()
            if q == "SELECT CURRENT_USER":
                results.append(("s11_inventory",))
            elif "TRANSACTION_ISOLATION" in q:
                results.append(("repeatable read",))
            elif "TRANSACTION_READ_ONLY" in q:
                results.append(("on",))
            elif "TIMEZONE" in q:
                results.append(("UTC",))
            elif "INFORMATION_SCHEMA.TABLES" in q:
                results.append(None)  # View not found
            else:
                results.append(None)

        cursor = MagicMock()
        cursor.execute = mock_execute
        cursor.fetchone = lambda: results.pop(0) if results else None
        cursor.fetchall = lambda: []

        with pytest.raises(exporter.ExportError, match="missing_view"):
            exporter.verify_connection(cursor)


# ---------------------------------------------------------------------------
# Audit detail filtering
# ---------------------------------------------------------------------------


class TestAuditDetailFiltering:
    def test_allowlisted_keys_preserved(self, exporter):
        details = {
            "provider": "github",
            "provider_user_id": "12345",
            "shadow_user_id": "user-1",
            "verification_method": "oauth",
            "delivery_method": "provider_dm",
            "ownership_proven": True,
            "jti": "secret-nonce",
            "token": "Bearer secret",
            "channel_context": "private",
        }
        filtered = exporter._filter_audit_details(details)
        assert "provider" in filtered
        assert "provider_user_id" in filtered
        assert "shadow_user_id" in filtered
        assert "verification_method" in filtered
        assert "delivery_method" in filtered
        assert "ownership_proven" in filtered
        assert "jti" not in filtered
        assert "token" not in filtered
        assert "channel_context" not in filtered

    def test_null_details_returns_none(self, exporter):
        assert exporter._filter_audit_details(None) is None

    def test_empty_details_returns_none(self, exporter):
        assert exporter._filter_audit_details({}) is None

    def test_details_with_no_matching_keys_returns_none(self, exporter):
        assert exporter._filter_audit_details({"jti": "nonce", "token": "secret"}) is None


# ---------------------------------------------------------------------------
# Empty database / edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @staticmethod
    def _empty_db():
        import sqlite3

        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE organizations(id TEXT)")
        db.execute(
            "CREATE TABLE identities("
            "id TEXT, org_id TEXT, user_id TEXT, provider TEXT, "
            "provider_user_id TEXT, verification_method TEXT, "
            "created_at TEXT, verified_at TEXT, updated_at TEXT, "
            "team_id TEXT, is_primary INTEGER)"
        )
        db.execute("CREATE TABLE users(id TEXT, org_id TEXT, is_shadow INTEGER, user_kind TEXT, bot_kind TEXT, created_at TEXT, updated_at TEXT)")
        db.execute("CREATE TABLE audit(id TEXT, org_id TEXT, event_type TEXT, actor_id TEXT, details TEXT, created_at TEXT)")
        return db

    def test_empty_export(self, exporter, inventory, tmp_path, monkeypatch):
        db = self._empty_db()
        monkeypatch.setattr(exporter, "SCHEMA_NAME", "")
        adapter = SqliteCursorAdapter(db)

        run_dir = str(tmp_path / "run")
        os.makedirs(run_dir, mode=0o700)
        receipt = exporter.export_snapshot(adapter, run_dir, SOURCE_SHA, inventory_module=inventory)
        assert receipt["status"] == "EMPTY"
        assert receipt["tenant_count"] == 0
        db.close()

    def test_tenant_with_only_org_row_no_data(self, exporter, inventory, tmp_path, monkeypatch):
        """An org that exists but has no identities/users/audit still exports."""
        db = self._empty_db()
        db.execute("INSERT INTO organizations VALUES (?)", ("empty-org",))
        db.commit()
        monkeypatch.setattr(exporter, "SCHEMA_NAME", "")
        adapter = SqliteCursorAdapter(db)

        run_dir = str(tmp_path / "run")
        os.makedirs(run_dir, mode=0o700)
        receipt = exporter.export_snapshot(adapter, run_dir, SOURCE_SHA, inventory_module=inventory)
        assert receipt["status"] == "COMPLETE"
        assert "empty-org" in receipt["tenants"]
        db.close()

    def test_snapshot_deterministic_hash(self, exporter, cursor, inventory, tmp_path, monkeypatch):
        """Same data produces the same snapshot hash across runs."""
        _patch_schema_refs(exporter, monkeypatch)
        run1 = str(tmp_path / "run1")
        run2 = str(tmp_path / "run2")
        os.makedirs(run1, mode=0o700)
        os.makedirs(run2, mode=0o700)

        receipt1 = exporter.export_snapshot(cursor, run1, SOURCE_SHA, inventory_module=inventory)
        # Recreate cursor since SQLite cursor state may differ
        import sqlite3

        db = sqlite3.connect(":memory:")
        _create_test_db_ref = _create_test_db
        db.close()

        # Use the same cursor (SQLite is deterministic for same data)
        db2 = _create_test_db(exporter)
        _seed_canonical_data(db2)
        adapter2 = SqliteCursorAdapter(db2)
        receipt2 = exporter.export_snapshot(adapter2, run2, SOURCE_SHA, inventory_module=inventory)

        for tenant_id in receipt1["tenants"]:
            if receipt1["tenants"][tenant_id].get("snapshot_hash"):
                assert receipt1["tenants"][tenant_id]["snapshot_hash"] == receipt2["tenants"][tenant_id]["snapshot_hash"]
        db2.close()


# ---------------------------------------------------------------------------
# DSN construction
# ---------------------------------------------------------------------------


class TestDsnConstruction:
    def test_builds_valid_dsn(self, exporter):
        dsn = exporter.build_dsn("db.example.com", 5432, "mydb", "myuser", password="token123")
        assert "host=db.example.com" in dsn
        assert "port=5432" in dsn
        assert "dbname=mydb" in dsn
        assert "user=myuser" in dsn
        assert "password=token123" in dsn
        assert "sslmode=verify-full" in dsn

    def test_dsn_without_password(self, exporter):
        dsn = exporter.build_dsn("db.example.com", 5432, "mydb", "myuser")
        assert "password" not in dsn


# ---------------------------------------------------------------------------
# CLI argument validation
# ---------------------------------------------------------------------------


class TestCli:
    def test_invalid_source_sha_rejected(self, exporter, capsys):
        result = exporter.main(["--sslrootcert", "/test/ca.pem", "--host", "h", "--dbname", "d", "--output-dir", "/tmp", "--source-sha", "not-a-sha"])
        assert result == 2

    def test_short_source_sha_rejected(self, exporter, capsys):
        result = exporter.main(["--sslrootcert", "/test/ca.pem", "--host", "h", "--dbname", "d", "--output-dir", "/tmp", "--source-sha", "abc123"])
        assert result == 2


# ---------------------------------------------------------------------------
# PostgreSQL isolation test (requires pgserver or BG_TEST_POSTGRES_URI)
# ---------------------------------------------------------------------------


def _pg_available():
    try:
        # Also need pgserver or an external URI
        import os

        import psycopg2  # noqa: F401

        if os.environ.get("BG_TEST_POSTGRES_URI"):
            return True
        try:
            import pgserver  # noqa: F401

            return True
        except ImportError:
            return False
    except ImportError:
        return False


@pytest.mark.skipif(not _pg_available(), reason="requires pgserver or BG_TEST_POSTGRES_URI")
class TestPostgresIsolation:
    """REPEATABLE READ isolation: concurrent inserts during export are excluded.

    These tests require a real PostgreSQL server (via pgserver or an external
    URI). They are skipped when neither is available.
    """

    @pytest.fixture
    def pg_db(self):
        """Create a temporary PostgreSQL database with the s11_inventory schema."""
        import os
        import uuid

        import psycopg2

        uri = os.environ.get("BG_TEST_POSTGRES_URI")
        if not uri:
            import pgserver

            data_dir = f"/tmp/pgtest-{uuid.uuid4().hex[:8]}"
            os.makedirs(data_dir, exist_ok=True)
            server = pgserver.get_server(data_dir)
            uri = server.get_uri()
        else:
            server = None

        db_name = f"t{uuid.uuid4().hex[:16]}"
        conn = psycopg2.connect(uri)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{db_name}"')
        conn.close()

        # Get the database-specific URI
        if server:
            db_uri = server.get_uri(database=db_name)
        else:
            base, _, query = uri.partition("?")
            db_uri = base.rsplit("/", 1)[0] + "/" + db_name
            if query:
                db_uri += "?" + query

        # Create schema and tables
        conn = psycopg2.connect(db_uri)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA s11_inventory")
            cur.execute("CREATE TABLE s11_inventory.organizations(id TEXT PRIMARY KEY)")
            cur.execute(
                "CREATE TABLE s11_inventory.identities("
                "id TEXT, org_id TEXT, user_id TEXT, provider TEXT, "
                "provider_user_id TEXT, verification_method TEXT, "
                "created_at TIMESTAMPTZ, verified_at TIMESTAMPTZ, "
                "updated_at TIMESTAMPTZ, team_id TEXT, is_primary BOOLEAN)"
            )
            cur.execute(
                "CREATE TABLE s11_inventory.users("
                "id TEXT, org_id TEXT, is_shadow BOOLEAN, user_kind TEXT, "
                "bot_kind TEXT, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ)"
            )
            cur.execute(
                "CREATE TABLE s11_inventory.audit(id TEXT, org_id TEXT, event_type TEXT, actor_id TEXT, details JSONB, created_at TIMESTAMPTZ)"
            )

            # Seed data
            cur.execute(
                "INSERT INTO s11_inventory.organizations VALUES (%s)",
                ("org-pg-test",),
            )
            cur.execute(
                "INSERT INTO s11_inventory.identities VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    "id-pg1",
                    "org-pg-test",
                    "user-pg1",
                    "github",
                    "gh-pg1",
                    "oauth",
                    "2026-01-01T00:00:00+00:00",
                    None,
                    None,
                    None,
                    False,
                ),
            )
            cur.execute(
                "INSERT INTO s11_inventory.users VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    "user-pg1",
                    "org-pg-test",
                    False,
                    "human",
                    None,
                    "2026-01-01T00:00:00+00:00",
                    None,
                ),
            )
        conn.close()

        yield db_uri, db_name

        # Cleanup
        conn = psycopg2.connect(uri)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        conn.close()
        if server:
            server.cleanup()

    @pytest.fixture
    def exp_module(self):
        """Load the exporter module without network blocking (pgserver needs sockets)."""
        spec = importlib.util.spec_from_file_location("identity_snapshot_exporter_pg", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_concurrent_insert_excluded_by_repeatable_read(self, exp_module, pg_db):
        """An audit event inserted after our snapshot transaction starts is invisible."""
        import json

        import psycopg2

        db_uri, _ = pg_db

        # Open snapshot transaction
        snap_conn = psycopg2.connect(db_uri)
        snap_conn.autocommit = False
        snap_cursor = snap_conn.cursor()
        snap_cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")

        # Force the transaction to take a snapshot by reading
        snap_cursor.execute("SELECT id FROM s11_inventory.organizations")
        snap_cursor.fetchall()

        # Now insert an audit event in a SEPARATE connection (concurrent write)
        write_conn = psycopg2.connect(db_uri)
        write_conn.autocommit = True
        with write_conn.cursor() as wcur:
            wcur.execute(
                "INSERT INTO s11_inventory.audit VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    "evt-concurrent",
                    "org-pg-test",
                    "identity_linked",
                    "user-pg1",
                    json.dumps({"provider": "github", "provider_user_id": "gh-pg1"}),
                    "2026-09-01T00:00:00+00:00",
                ),
            )
        write_conn.close()

        # The snapshot cursor should NOT see the concurrent insert
        audit = exp_module.fetch_tenant_audit(snap_cursor, "org-pg-test")
        audit_ids = {e["id"] for e in audit}
        assert "evt-concurrent" not in audit_ids

        snap_conn.rollback()
        snap_conn.close()

    def test_read_only_transaction_refuses_writes(self, pg_db):
        """A READ ONLY transaction cannot INSERT/UPDATE/DELETE."""
        import psycopg2

        db_uri, _ = pg_db

        conn = psycopg2.connect(db_uri)
        conn.autocommit = False
        cursor = conn.cursor()
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")

        with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
            cursor.execute(
                "INSERT INTO s11_inventory.organizations VALUES (%s)",
                ("attacker-org",),
            )

        conn.rollback()
        conn.close()


def test_two_empty_tenants_have_distinct_files(exporter, inventory, tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, "discover_tenants", lambda _: ["empty-one", "empty-two"])
    for name in ("fetch_tenant_identities", "fetch_tenant_users", "fetch_tenant_audit"):
        monkeypatch.setattr(exporter, name, lambda *_: [])
    receipt = exporter.export_snapshot(None, str(tmp_path), SOURCE_SHA, inventory_module=inventory)
    assert receipt["status"] == "COMPLETE"
    assert len(list(tmp_path.glob("snapshot-*.json"))) == 2
    assert len(list(tmp_path.glob("manifest-*.json"))) == 2
    import hashlib

    for tenant in receipt["tenants"].values():
        for kind in ("snapshot", "manifest"):
            assert tenant[f"{kind}_file_sha256"] == hashlib.sha256((tmp_path / tenant[f"{kind}_file"]).read_bytes()).hexdigest()


def test_dsn_values_cannot_inject_options(exporter):
    from psycopg2.extensions import parse_dsn

    dsn = exporter.build_dsn("db.example", 5432, "name sslmode=disable", "s11_inventory", password="a' b")
    parsed = parse_dsn(dsn)
    assert parsed["dbname"] == "name sslmode=disable"
    assert parsed["sslmode"] == "verify-full"
    assert parsed["password"] == "a' b"


def test_streaming_stops_at_bound_without_fetchall(exporter, monkeypatch):
    monkeypatch.setattr(exporter, "MAX_ROWS_PER_TABLE", 2)

    class EndlessCursor:
        calls = 0

        def fetchone(self):
            self.calls += 1
            return ("data",)

    cursor = EndlessCursor()
    with pytest.raises(exporter.ExportError, match="row_limit_exceeded"):
        exporter._bounded_rows(cursor)
    assert cursor.calls == 3


@pytest.mark.skipif(not _pg_available(), reason="requires disposable PostgreSQL")
class TestPostgresExporterSafety:
    pg_db = TestPostgresIsolation.pg_db
    exp_module = TestPostgresIsolation.exp_module

    @pytest.mark.parametrize("hazard", ["none", "public_definer", "public_raw", "inherited_raw"])
    def test_effective_privileges_and_streamed_export(self, exp_module, pg_db, tmp_path, hazard):
        import time

        import psycopg2

        uri, _ = pg_db
        admin = psycopg2.connect(uri)
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("CREATE ROLE s11_inventory NOLOGIN NOINHERIT")
            cur.execute("CREATE ROLE s11_extra NOLOGIN")
            for name, columns in exp_module.EXPECTED_VIEWS.items():
                cur.execute(f"ALTER TABLE s11_inventory.{name} SET SCHEMA public")
                cur.execute(f"CREATE VIEW s11_inventory.{name} WITH (security_barrier=true) AS SELECT {','.join(columns)} FROM public.{name}")
            cur.execute("GRANT USAGE ON SCHEMA s11_inventory TO s11_inventory")
            cur.execute("GRANT SELECT ON ALL TABLES IN SCHEMA s11_inventory TO s11_inventory")
            if hazard == "public_definer":
                cur.execute("CREATE FUNCTION public.s11_probe() RETURNS integer LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'")
            elif hazard == "public_raw":
                cur.execute("GRANT SELECT ON public.identities TO PUBLIC")
            elif hazard == "inherited_raw":
                cur.execute("GRANT SELECT ON public.identities TO s11_extra")
                cur.execute("GRANT s11_extra TO s11_inventory")
        conn = psycopg2.connect(uri)
        try:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                cur.execute("SET LOCAL ROLE s11_inventory")
                cur.execute("SET LOCAL timezone='UTC'")
                if hazard != "none":
                    with pytest.raises(exp_module.ExportError):
                        exp_module.verify_connection(cur)
                    return
                exp_module.verify_connection(cur)
            reader = exp_module.SnapshotCursor(conn, time.monotonic() + 30)
            receipt = exp_module.export_snapshot(reader, str(tmp_path), SOURCE_SHA)
            assert receipt["status"] == "COMPLETE"
            assert receipt["tenant_count"] == 1
            assert reader.sequence == 4
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM s11_inventory.identities")
                assert cur.fetchone()[0] == 1
        finally:
            conn.rollback()
            conn.close()
            with admin.cursor() as cur:
                cur.execute("DROP OWNED BY s11_inventory, s11_extra")
                cur.execute("DROP ROLE s11_inventory, s11_extra")
            admin.close()
