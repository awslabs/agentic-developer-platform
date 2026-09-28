"""Populated-data checks for revision 017's uniqueness preflight."""

from __future__ import annotations

import functools
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

API_ROOT = Path(__file__).resolve().parent.parent


@functools.lru_cache(maxsize=1)
def revision():
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return (
        ScriptDirectory.from_config(config)
        .get_revision("017_unique_credential_reference")
        .module
    )


@pytest.fixture
def connection():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE credential_registry ("
            "id TEXT PRIMARY KEY, org_id TEXT NOT NULL, adp_credential_id TEXT NOT NULL, "
            "provider TEXT NOT NULL, friendly_name TEXT NOT NULL, credential_type TEXT NOT NULL, "
            "status TEXT NOT NULL)"
        )
        yield connection
    engine.dispose()


def apply_upgrade(connection):
    context = MigrationContext.configure(connection=connection)
    with Operations.context(context):
        revision().upgrade()


def insert(connection, record_id, *, name="prod", provider="nebius"):
    connection.exec_driver_sql(
        "INSERT INTO credential_registry "
        "(id, org_id, adp_credential_id, provider, friendly_name, credential_type, status) "
        "VALUES (?, 'org-1', 'vault-1', ?, ?, 'api_key', 'Active')",
        (record_id, provider, name),
    )


def test_identical_duplicates_refuse_before_ddl_and_name_recovery_steps(connection):
    insert(connection, "record-1")
    insert(connection, "record-2")

    with pytest.raises(revision().DuplicateCredentialReferencesError) as raised:
        apply_upgrade(connection)

    message = str(raised.value)
    assert "identical metadata" in message
    assert "record-1, record-2" in message
    assert "cluster_vault_assignments" in message
    assert "credential_audit_log" in message
    indexes = connection.exec_driver_sql(
        "PRAGMA index_list('credential_registry')"
    ).all()
    assert all(
        "uq_credential_registry_org_adp_credential" not in row for row in indexes
    )
    assert (
        connection.exec_driver_sql(
            "SELECT COUNT(*) FROM credential_registry"
        ).scalar_one()
        == 2
    )


def test_conflicting_duplicates_are_not_automatically_consolidated(connection):
    insert(connection, "record-1", name="prod")
    insert(connection, "record-2", name="staging", provider="lambda")

    with pytest.raises(revision().DuplicateCredentialReferencesError) as raised:
        apply_upgrade(connection)

    assert "conflicting metadata" in str(raised.value)
    assert connection.exec_driver_sql(
        "SELECT friendly_name FROM credential_registry ORDER BY id"
    ).scalars().all() == [
        "prod",
        "staging",
    ]
