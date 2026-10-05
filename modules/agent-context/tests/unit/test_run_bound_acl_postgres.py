"""A run stays in its delegated tenant even if the legacy Door flag is off."""

import tempfile

import pytest

from door.acl import CallerPrincipal, PostgresACLStore, extract_caller_principal


@pytest.fixture(scope="module")
def pool():
    pgserver = pytest.importorskip(
        "pgserver", reason="real PostgreSQL needs pgserver (Python <=3.12)"
    )
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2.pool import SimpleConnectionPool

    server = pgserver.get_server(tempfile.mkdtemp(prefix="run-door-acl-"), cleanup_mode="delete")
    connections = SimpleConnectionPool(1, 2, server.get_uri())
    try:
        with psycopg2.connect(server.get_uri()) as conn, conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE repositories (repo_name text, allowed_principals jsonb, tenant_id text, owner_sub text, acl_public_verified boolean NOT NULL DEFAULT false)"
            )
            cur.executemany(
                "INSERT INTO repositories(repo_name, allowed_principals, tenant_id, owner_sub) VALUES (%s,%s,%s,%s)",
                [
                    ("shared/public", '["*"]', None, None),
                    ("one/source", '["alice"]', "one", None),
                    ("two/source", '["alice"]', "two", None),
                    ("one/personal", "[]", "one", "owner"),
                    ("two/personal", "[]", "two", "owner"),
                    ("one/other-person", "[]", "one", "other-owner"),
                ],
            )
            cur.execute(
                "UPDATE repositories SET acl_public_verified=true WHERE repo_name='shared/public'"
            )
        yield connections
    finally:
        connections.closeall()
        server.cleanup()


@pytest.mark.parametrize("legacy_flag", [False, True])
def test_run_bound_queries_exclude_other_tenant_and_other_owner(pool, legacy_flag):
    principal = extract_caller_principal(
        {
            "x-github-login": "alice",
            "x-owner-sub": "owner",
            "x-tenant-id": "one",
            "x-adp-run-service": "true",
        }
    )
    assert principal is not None and principal.run_bound
    store = PostgresACLStore(pool, tenant_scope_enabled=legacy_flag)
    assert store.get_allowed_repos(principal) == {"shared/public", "one/source", "one/personal"}


def test_missing_tenant_does_not_fall_back_to_legacy_query(pool):
    store = PostgresACLStore(pool, tenant_scope_enabled=False)
    assert store.get_allowed_repos(CallerPrincipal(github_login="alice", run_bound=True)) == set()


def test_legacy_callers_keep_the_existing_flag_behavior(pool):
    store = PostgresACLStore(pool, tenant_scope_enabled=False)
    assert store.get_allowed_repos(CallerPrincipal(github_login="alice", tenant_id="one")) == {
        "shared/public",
        "one/source",
        "two/source",
    }
