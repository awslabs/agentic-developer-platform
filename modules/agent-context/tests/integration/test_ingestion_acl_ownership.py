"""Exercise actual PostgreSQL ownership and ACL updates on an ephemeral local DB."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "images" / "ingestion"))
import db


@pytest.fixture(scope="module")
def local_acl_database():
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if not initdb or not pg_ctl:
        pytest.skip("local PostgreSQL server tools are required for SQL integration coverage")
    with tempfile.TemporaryDirectory(prefix="adp-acl-pg-", dir="/tmp") as root:
        data = str(Path(root) / "data")
        subprocess.run(
            [initdb, "-D", data, "-A", "trust", "-U", "acl_test", "--no-locale"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                pg_ctl,
                "-D",
                data,
                "-l",
                str(Path(root) / "server.log"),
                "-o",
                f"-F -c listen_addresses='' -k {root}",
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
        )
        try:
            kwargs = dict(host=root, dbname="postgres", user="acl_test")
            with psycopg2.connect(**kwargs) as conn:
                with conn.cursor() as cur:
                    cur.execute("""CREATE TABLE repositories (
                        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), repo_name text UNIQUE NOT NULL,
                        git_url text, owner text, allowed_principals jsonb,
                        tenant_id text, owner_sub text)""")
            yield kwargs
        finally:
            subprocess.run(
                [pg_ctl, "-D", data, "-m", "fast", "-w", "stop"], check=True, capture_output=True
            )


@pytest.fixture
def conn(local_acl_database):
    conn = psycopg2.connect(**local_acl_database)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE repositories")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def register(conn, *, tenant="team-a", owner=None, principals=None):
    return db.ensure_repo_exists(
        conn,
        "org/service",
        "https://github.com/org/service",
        tenant_id=tenant,
        owner_sub=owner,
        allowed_principals=principals,
    )


def stored(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT allowed_principals, tenant_id, owner_sub FROM repositories WHERE repo_name='org/service'"
        )
        return cur.fetchone()


def test_new_unverified_row_is_denied(conn):
    register(conn)
    assert stored(conn) == ([], "team-a", None)


def test_explicit_rederivation_revokes_legacy_public_and_removed_principals(conn):
    repo_id = register(conn, principals=["*"])
    assert register(conn, principals=["alice"]) == repo_id
    assert stored(conn) == (["alice"], "team-a", None)
    register(conn, principals=[])
    assert stored(conn) == ([], "team-a", None)


def test_sbom_lookup_preserves_the_verified_acl(conn):
    register(conn, principals=["alice"])
    register(conn)
    assert stored(conn) == (["alice"], "team-a", None)


@pytest.mark.parametrize(
    ("tenant", "owner"), [("team-b", None), (None, None), ("team-a", "mallory")]
)
def test_conflicting_queue_owner_cannot_relabel_or_replace_acl(conn, tenant, owner):
    register(conn, principals=["alice"])
    with pytest.raises(RuntimeError, match="ownership conflicts"):
        register(conn, tenant=tenant, owner=owner, principals=["*"])
    assert stored(conn) == (["alice"], "team-a", None)


def test_concurrent_first_registration_has_only_one_owner(conn, local_acl_database):
    def attempt(tenant):
        with psycopg2.connect(**local_acl_database) as other:
            try:
                register(other, tenant=tenant, principals=[tenant])
                return tenant
            except RuntimeError:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ["team-a", "team-b"]))
    winners = [value for value in outcomes if value]
    assert len(winners) == 1
    assert stored(conn) == ([winners[0]], winners[0], None)


@pytest.fixture
def backfill():
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "backfill_repo_acls.py"
    spec = importlib.util.spec_from_file_location("acl_backfill_postgres_review", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backfill_does_not_replace_a_newer_acl(conn, backfill):
    repo_id = register(conn, principals=["*"])
    plan = [
        {
            "id": repo_id,
            "repo_name": "org/service",
            "allowed_principals": ["*"],
            "outcome": backfill.OUTCOME_TIGHTEN,
            "new_acl": ["alice"],
        }
    ]
    register(conn, principals=["bob"])
    assert backfill.apply_changes(conn, plan, deny_unknown=False) == []
    assert stored(conn) == (["bob"], "team-a", None)


def test_failed_journal_prevents_database_commit(conn, backfill, monkeypatch, tmp_path):
    repo_id = register(conn, principals=["*"])
    plan = [
        {
            "id": repo_id,
            "repo_name": "org/service",
            "allowed_principals": ["*"],
            "outcome": backfill.OUTCOME_TIGHTEN,
            "new_acl": ["alice"],
        }
    ]

    def fail(_fd):
        raise OSError("disk unavailable")

    monkeypatch.setattr(backfill.os, "fsync", fail)
    with pytest.raises(OSError):
        backfill.apply_changes(
            conn, plan, deny_unknown=False, journal_path=str(tmp_path / "journal.json")
        )
    assert stored(conn) == (["*"], "team-a", None)


def test_rollback_does_not_undo_a_subsequent_acl_change(conn, backfill, tmp_path):
    import json

    repo_id = register(conn, principals=["bob"])
    journal = tmp_path / "journal.json"
    journal.write_text(
        json.dumps(
            [
                {
                    "id": repo_id,
                    "repo_name": "org/service",
                    "previous_acl": ["*"],
                    "new_acl": ["alice"],
                }
            ]
        )
    )
    backfill.rollback(conn, str(journal), apply=True)
    assert stored(conn) == (["bob"], "team-a", None)


@pytest.mark.parametrize("run_bound", [False, True])
def test_real_door_query_denies_unowned_private_and_another_persons_rows(conn, run_bound):
    from door.acl import CallerPrincipal, PostgresACLStore

    rows = [
        ("org/public", '["*"]', None, None),
        ("org/unowned-private", '["alice"]', None, None),
        ("org/team-a", '["alice"]', "team-a", None),
        ("org/team-b", '["alice"]', "team-b", None),
        ("org/alice-personal", "[]", "team-a", "alice-id"),
        ("org/alice-other-tenant", "[]", "team-b", "alice-id"),
        ("org/bob-personal", '["alice"]', "team-a", "bob-id"),
    ]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO repositories(repo_name, allowed_principals, tenant_id, owner_sub) VALUES (%s,%s,%s,%s)",
            rows,
        )
    conn.commit()

    class Pool:
        def getconn(self):
            return conn

        def putconn(self, _conn):
            pass

    store = PostgresACLStore(Pool(), tenant_scope_enabled=True)
    caller = CallerPrincipal(
        github_login="alice", tenant_id="team-a", owner_sub="alice-id", run_bound=run_bound
    )
    expected = {"org/public", "org/team-a", "org/alice-personal"}
    if not run_bound:
        expected.add("org/alice-other-tenant")
    assert store.get_allowed_repos(caller) == expected
