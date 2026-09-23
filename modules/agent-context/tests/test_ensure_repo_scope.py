"""Repository ownership tests execute the production helper's SQL.

SQLite translates only parameter/cast/locking syntax here. PostgreSQL integration
coverage separately exercises transactions and concurrent first registration.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "images" / "ingestion"))
import db


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def execute(self, sql, params):
        self.cursor.execute(
            sql.replace("%s", "?").replace("::jsonb", "").replace(" FOR UPDATE", ""), params
        )

    def fetchone(self):
        return self.cursor.fetchone()

    def close(self):
        self.cursor.close()


class Connection:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("""CREATE TABLE repositories (
            id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))), repo_name TEXT UNIQUE NOT NULL,
            git_url TEXT, owner TEXT, allowed_principals TEXT, tenant_id TEXT, owner_sub TEXT)""")

    def cursor(self):
        return Cursor(self.connection.cursor())

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()


@pytest.fixture
def conn():
    connection = Connection()
    yield connection
    connection.connection.close()


def register(conn, tenant=None, owner=None):
    return db.ensure_repo_exists(
        conn, "org/repo", "https://github.com/org/repo", tenant_id=tenant, owner_sub=owner
    )


@pytest.mark.parametrize(("tenant", "owner"), [("team-a", "alice"), ("team-a", None), (None, None)])
def test_first_registration_preserves_exact_producer_scope(conn, tenant, owner):
    repo_id = register(conn, tenant, owner)
    assert conn.connection.execute(
        "SELECT id, tenant_id, owner_sub FROM repositories"
    ).fetchone() == (repo_id, tenant, owner)
    assert register(conn, tenant, owner) == repo_id


@pytest.mark.parametrize(
    ("tenant", "owner"), [(None, None), ("team-b", "alice"), ("team-a", "mallory")]
)
def test_later_message_cannot_overwrite_verified_owner(conn, tenant, owner):
    register(conn, "team-a", "alice")
    with pytest.raises(RuntimeError, match="ownership conflicts"):
        register(conn, tenant, owner)
    assert conn.connection.execute("SELECT tenant_id, owner_sub FROM repositories").fetchone() == (
        "team-a",
        "alice",
    )


def test_unowned_legacy_row_needs_explicit_migration(conn):
    register(conn)
    with pytest.raises(RuntimeError, match="ownership conflicts"):
        register(conn, "team-a", "alice")
    assert conn.connection.execute("SELECT tenant_id, owner_sub FROM repositories").fetchone() == (
        None,
        None,
    )
