"""Exercise verify's ownership query with real rows and supported repo aliases."""

import sqlite3
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from door.acl import CallerPrincipal
from door.secure_backend import handle_verify


class CataloguePool:
    """Run the handler's SQL on SQLite, translating only PostgreSQL parameters."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.queries = []
        self.db.executescript("""
            CREATE TABLE repositories (id TEXT, repo_name TEXT UNIQUE, indexed_at TEXT);
            INSERT INTO repositories VALUES ('a', 'OrgA/Service', NULL);
            INSERT INTO repositories VALUES ('b', 'OrgB/Service', NULL);
            CREATE TABLE vulnerabilities (
                cve_id TEXT, package TEXT, affected_versions TEXT, safe_version TEXT, severity TEXT
            );
            INSERT INTO vulnerabilities VALUES (
                'CVE-2026-1234', 'pkg:pypi/example', '<2.0.0', '2.0.0', 'HIGH'
            );
            CREATE TABLE dependencies (repo_id TEXT, version TEXT, package_coordinate TEXT);
            INSERT INTO dependencies VALUES ('a', '2.0.0', 'pkg:pypi/example@2.0.0');
            INSERT INTO dependencies VALUES ('b', '1.0.0', 'pkg:pypi/example@1.0.0');
        """)

    def getconn(self):
        return self

    def putconn(self, connection):
        assert connection is self

    @contextmanager
    def cursor(self):
        yield self

    def execute(self, query, params):
        self.queries.append((query, params))
        if "= ANY(%s)" in query:
            values = params[-1]
            query = query.replace("= ANY(%s)", "IN (" + ",".join("?" for _ in values) + ")")
            params = (*params[:-1], *values)
        self.current = self.db.execute(query.replace("%s", "?"), params)

    def fetchone(self):
        return self.current.fetchone()


@pytest.fixture
def catalogue():
    pool = CataloguePool()
    yield pool
    pool.db.close()


def permitted_store():
    return Mock(get_allowed_repos=Mock(return_value={"OrgA/Service"}))


@pytest.mark.parametrize("repo", ["OrgA/Service", "orga/service", "github.com/OrgA/Service"])
async def test_owned_repository_aliases_preserve_verification(repo, catalogue):
    result = await handle_verify(
        "CVE-2026-1234",
        repo,
        db_pool=catalogue,
        caller=CallerPrincipal(github_login="alice", tenant_id="a"),
        acl_store=permitted_store(),
    )
    assert result["status"] == "resolved"
    assert result["details"]["current_version"] == "2.0.0"
    assert result["query"]["repo"] == repo


@pytest.mark.parametrize(
    "repo", ["OrgB/Service", "github.com/OrgB/Service", "Unknown/Service", "Service"]
)
async def test_other_or_unknown_repository_discloses_no_inventory(repo, catalogue):
    result = await handle_verify(
        "CVE-2026-1234",
        repo,
        db_pool=catalogue,
        caller=CallerPrincipal(github_login="alice", tenant_id="a"),
        acl_store=permitted_store(),
    )
    assert result["status"] == "unknown"
    assert result["details"] == {"reason": "repo_not_indexed", "repo_indexed": False}
    assert not catalogue.queries


@pytest.mark.parametrize(
    "store", [None, Mock(get_allowed_repos=Mock(side_effect=RuntimeError("down")))]
)
async def test_missing_or_failed_acl_store_never_reads_catalogue(store, catalogue):
    result = await handle_verify(
        "CVE-2026-1234",
        "OrgA/Service",
        db_pool=catalogue,
        caller=CallerPrincipal(github_login="alice", tenant_id="a"),
        acl_store=store,
    )
    assert result["details"] == {"reason": "repo_not_indexed", "repo_indexed": False}
    assert not catalogue.queries
