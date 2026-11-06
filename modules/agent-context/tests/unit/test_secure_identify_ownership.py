"""Identify and plan must authorize repository names before catalogue reads."""

import sqlite3
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from door.acl import CallerPrincipal
from door.secure_backend import handle_identify, handle_plan


class CataloguePool:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.queries = []
        self.db.executescript("""
            CREATE TABLE repositories (id TEXT, repo_name TEXT UNIQUE);
            INSERT INTO repositories VALUES ('a', 'OrgA/Service');
            INSERT INTO repositories VALUES ('b', 'OrgB/Service');
            INSERT INTO repositories VALUES ('c', 'OrgA/Empty');
            CREATE TABLE vulnerabilities (
                cve_id TEXT, package TEXT, affected_versions TEXT,
                safe_version TEXT, severity TEXT, details TEXT
            );
            INSERT INTO vulnerabilities VALUES (
                'CVE-2026-1234', 'pkg:pypi/example', '<2.0.0', '2.0.0', 'HIGH', NULL
            );
            CREATE TABLE dependencies (repo_id TEXT, version TEXT, package_coordinate TEXT);
            INSERT INTO dependencies VALUES ('a', '1.0.0', 'pkg:pypi/example@1.0.0');
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

    def fetchall(self):
        return self.current.fetchall()


@pytest.fixture
def catalogue():
    pool = CataloguePool()
    yield pool
    pool.db.close()


@pytest.fixture
def authority():
    return CallerPrincipal(github_login="alice", tenant_id="tenant-a"), Mock(
        get_allowed_repos=Mock(return_value={"OrgA/Service", "OrgA/Empty"})
    )


@pytest.mark.parametrize("action", ["identify", "plan"])
async def test_foreign_unknown_and_ambiguous_names_never_query_catalogue(
    action, catalogue, authority
):
    caller, acl_store = authority
    handler = handle_plan if action == "plan" else handle_identify
    results = []
    for name in ("OrgB/Service", "Unknown/Service", "Service", "github.com/OrgB/Service"):
        result = await handler(
            cve="CVE-2026-1234",
            repo=name,
            db_pool=catalogue,
            caller=caller,
            acl_store=acl_store,
        )
        results.append(result)
        assert catalogue.queries == []
        assert result["findings_count"] == 0
        assert result["metadata"]["repo_indexed"] is False
    assert results[0] == {
        **results[1],
        "summary": results[1]["summary"].replace("Unknown/Service", "OrgB/Service"),
        "query": {**results[1]["query"], "repo": "OrgB/Service"},
    }
    assert acl_store.get_allowed_repos.call_count == 4


@pytest.mark.parametrize("action", ["identify", "plan"])
async def test_allowed_alias_and_empty_repository_retain_catalogue_diagnostics(
    action, catalogue, authority
):
    caller, acl_store = authority
    handler = handle_plan if action == "plan" else handle_identify
    result = await handler(
        cve="CVE-2026-1234",
        repo="github.com/orga/service",
        db_pool=catalogue,
        caller=caller,
        acl_store=acl_store,
    )
    assert result["findings_count"] == 1
    assert result["findings"][0]["repos_affected"] == ["OrgA/Service"]
    assert result["metadata"] == {"cve_known": True, "repo_indexed": True}
    assert all("OrgB/Service" not in str(params) for _, params in catalogue.queries)

    catalogue.queries.clear()
    empty = await handler(
        cve="CVE-2026-1234",
        repo="OrgA/Empty",
        db_pool=catalogue,
        caller=caller,
        acl_store=acl_store,
    )
    assert empty["findings_count"] == 0
    assert empty["metadata"]["repo_indexed"] is True
    assert catalogue.queries


@pytest.mark.parametrize(
    "filter_name, filter_value", [("cve", "CVE-2026-1234"), ("package", "example")]
)
async def test_unfiltered_dependency_lookup_receives_only_allowed_catalogue_names(
    filter_name, filter_value, catalogue, authority
):
    caller, acl_store = authority
    result = await handle_identify(
        **{filter_name: filter_value},
        db_pool=catalogue,
        caller=caller,
        acl_store=acl_store,
    )
    assert result["findings_count"] == 1
    assert result["findings"][0]["repos_affected"] == ["OrgA/Service"]
    dependency_queries = [
        (sql, params) for sql, params in catalogue.queries if "JOIN repositories" in sql
    ]
    assert dependency_queries
    assert all("r.repo_name = ANY(%s)" in sql for sql, _ in dependency_queries)
    assert all(params[-1] == ["OrgA/Empty", "OrgA/Service"] for _, params in dependency_queries)


async def test_empty_allowed_set_never_reads_catalogue(catalogue, authority):
    caller, acl_store = authority
    acl_store.get_allowed_repos.return_value = set()
    result = await handle_identify(
        cve="CVE-2026-1234",
        db_pool=catalogue,
        caller=caller,
        acl_store=acl_store,
    )
    assert result["findings_count"] == 0
    assert catalogue.queries == []


@pytest.mark.parametrize("action", ["identify", "plan"])
@pytest.mark.parametrize("error_code", ["NoSuchKey", "AccessDenied"])
async def test_unknown_sbom_freshness_preserves_catalogue_membership(
    action, error_code, catalogue, authority
):
    caller, acl_store = authority
    handler = handle_plan if action == "plan" else handle_identify
    s3_client = Mock()
    s3_client.head_object.side_effect = ClientError(
        {"Error": {"Code": error_code}}, "HeadObject"
    )

    result = await handler(
        cve="CVE-2026-1234",
        repo="OrgA/Service",
        db_pool=catalogue,
        caller=caller,
        acl_store=acl_store,
        s3_client=s3_client,
        s3_bucket="example-content",
    )

    assert result["findings_count"] == 1
    assert result["findings"][0]["repos_affected"] == ["OrgA/Service"]
    assert result["metadata"] == {
        "cve_known": True,
        "repo_indexed": True,
        "sbom_age_hours": None,
    }
    s3_client.head_object.assert_called_once()
