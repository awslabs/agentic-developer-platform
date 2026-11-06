"""Readiness proves DB isolation without loading providers or changing durable work."""

import importlib
import json
from unittest.mock import AsyncMock, Mock

import pytest

from l5_services.workers import database_probe as probe


class Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def one(self):
        assert len(self.rows) == 1
        return self.rows[0]

    def all(self):
        return self.rows


def table(name, allowed):
    return {"relname": name, "is_l5": name.startswith("l5_"), "protected_column_access": False,
            **{"can_" + right.lower(): right in allowed
               for right in (*probe.CRUD, "TRUNCATE", "REFERENCES", "TRIGGER")}}


class CatalogSession:
    def __init__(self):
        self.role = {"identity_matches": True, "rolcanlogin": True,
                     **dict.fromkeys(("rolsuper", "rolcreatedb", "rolcreaterole",
                                      "rolreplication", "rolbypassrls", "rolinherit"), False)}
        self.memberships = [{"rolname": "rds_iam", "admin_option": False}]
        self.scope = {"database_create": False, "schema_create": False, "public_usage": True}
        self.tables = [table("identity_users", ()), table("l5_service_setup_authority_continuations", probe.CRUD)]
        self.statements = []
        self.rollbacks = 0
        self.fail = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, statement, params=None):
        sql = str(statement).strip()
        self.statements.append(sql)
        if sql.startswith("SET "):
            return Result([])
        assert sql.startswith("SELECT "), "Probe attempted a database write"
        if self.fail:
            raise RuntimeError("password=private-marker SQL=private-sql parameters=private-tenant")
        if "FROM pg_roles WHERE" in sql:
            assert params == {"role": "adp_l5_worker"}
            return Result([self.role])
        if "FROM pg_auth_members" in sql:
            return Result(self.memberships)
        if "has_database_privilege" in sql:
            return Result([self.scope])
        assert "FROM pg_class" in sql
        return Result(self.tables)

    async def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setenv("L5_SERVICE_ID", "devops")
    monkeypatch.setenv("L5_RDS_USER", "adp_l5_worker")
    return CatalogSession()


async def inspect(catalog, event=None):
    return await probe.run_probe(probe.REQUEST if event is None else event, sessions=lambda: catalog)


async def test_readiness_reads_only_catalogs_and_rolls_back(catalog):
    result = await inspect(catalog)
    assert result == probe.response("devops", "worker_database_ready", tables_checked=1)
    assert catalog.statements[:2] == ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
                                      "SET LOCAL statement_timeout='3s'"]
    assert len(catalog.statements) == 6 and catalog.rollbacks == 1
    assert all("SET ROLE" not in statement for statement in catalog.statements)


async def test_new_continuation_table_without_grants_has_actionable_bounded_result(catalog):
    catalog.tables[-1] = table("l5_service_setup_authority_continuations", ())
    result = await inspect(catalog)
    assert result["code"] == "worker_database_grants_missing"
    assert result["missing_grants"] == [{"table": "l5_service_setup_authority_continuations",
                                          "privileges": list(probe.CRUD)}]
    assert not result["passed"] and catalog.rollbacks == 1


@pytest.mark.parametrize("right", (*probe.CRUD, "TRUNCATE", "REFERENCES", "TRIGGER"))
async def test_protected_table_access_blocks_without_disclosing_table_name(catalog, right):
    catalog.tables[0] = table("private_identity_marker", [right])
    result = await inspect(catalog)
    assert result["code"] == "worker_database_protected_access"
    assert "private_identity_marker" not in json.dumps(result)


@pytest.mark.parametrize("field", ["identity_matches", "rolcanlogin", "rolsuper", "rolcreatedb",
                                    "rolcreaterole", "rolreplication", "rolbypassrls", "rolinherit"])
async def test_role_must_be_exact_non_elevated_worker(catalog, field):
    catalog.role[field] = not catalog.role[field]
    assert (await inspect(catalog))["code"] == "worker_database_role_invalid"
    assert catalog.rollbacks == 1


@pytest.mark.parametrize("memberships", [[], [{"rolname": "rds_iam", "admin_option": True}],
    [{"rolname": "rds_iam", "admin_option": False}, {"rolname": "private_role", "admin_option": False}]])
async def test_role_memberships_cannot_expand_authority(catalog, memberships):
    catalog.memberships = memberships
    result = await inspect(catalog)
    assert result["code"] == "worker_database_role_invalid"
    assert "private_role" not in json.dumps(result)


@pytest.mark.parametrize("field", ["database_create", "schema_create"])
async def test_worker_cannot_create_additional_objects(catalog, field):
    catalog.scope[field] = True
    assert (await inspect(catalog))["code"] == "worker_database_protected_access"


async def test_probe_rejects_nonworker_configuration_before_opening_database(catalog, monkeypatch):
    monkeypatch.setenv("L5_RDS_USER", "database-owner-private-marker")
    assert (await inspect(catalog))["code"] == "worker_database_role_invalid"
    assert catalog.statements == []


async def test_probe_exception_has_no_raw_sql_credential_or_identity(catalog, caplog):
    catalog.fail = True
    result = await inspect(catalog)
    assert result["code"] == "worker_database_probe_unavailable"
    assert catalog.rollbacks == 1
    assert "private-" not in json.dumps(result) + caplog.text


async def test_probe_connection_timeout_is_bounded(catalog, monkeypatch):
    class Expired:
        async def __aenter__(self):
            raise TimeoutError("private timeout context")
        async def __aexit__(self, *args):
            pass
    timeout = Mock(return_value=Expired())
    monkeypatch.setattr(probe.asyncio, "timeout", timeout)
    assert (await inspect(catalog))["code"] == "worker_database_probe_unavailable"
    timeout.assert_called_once_with(10)
    assert catalog.statements == []


async def test_missing_grants_list_is_capped_but_total_is_retained(catalog):
    catalog.tables = [table(f"l5_table_{index:02}", ()) for index in range(25)]
    result = await inspect(catalog)
    assert result["missing_grants_total"] == 25 and len(result["missing_grants"]) == 20
    assert len(json.dumps(result)) < 4096
    assert probe.validate_response(result, "devops") == result


async def test_missing_schema_and_excessive_inventory_fail_closed(catalog):
    catalog.tables = [catalog.tables[0]]
    assert (await inspect(catalog))["code"] == "worker_database_schema_missing"
    catalog.tables = [table("l5_table", probe.CRUD)] * (probe.MAX_TABLES + 1)
    assert (await inspect(catalog))["code"] == "worker_database_probe_unavailable"


@pytest.mark.parametrize("event", [{probe.REQUEST_KEY: True}, {probe.REQUEST_KEY: "1"},
    {probe.REQUEST_KEY: 2}, {probe.REQUEST_KEY: 1, "Records": []}])
async def test_malformed_probe_cannot_fall_through_into_work(catalog, event):
    assert (await inspect(catalog, event))["code"] == "worker_database_probe_invalid"
    assert catalog.statements == []


@pytest.mark.parametrize("name", ["execution", "recovery", "devops_events", "devops_readback"])
@pytest.mark.parametrize("event", [probe.REQUEST, {probe.REQUEST_KEY: True}, {probe.REQUEST_KEY: 1, "Records": []}])
def test_all_handlers_route_probe_before_factory_or_work(name, event, monkeypatch):
    worker = importlib.import_module("l5_services.workers." + name)
    loader = Mock(side_effect=AssertionError("probe must not load provider factory"))
    monkeypatch.setattr(worker, "load_service", loader)
    run = AsyncMock(return_value=probe.response("devops", "worker_database_ready", tables_checked=1))
    monkeypatch.setattr(probe, "run_probe", run)
    assert worker.lambda_handler(event, None) == run.return_value
    run.assert_awaited_once_with(event)
    loader.assert_not_called()


@pytest.mark.parametrize("mutation", [
    lambda result: result.update(code=[]), lambda result: result.update(tables_checked=True),
    lambda result: result.update(protocol="old"), lambda result: result.update(service_id="security"),
    lambda result: result.update(passed="true"), lambda result: result.update(extra="private"),
    lambda result: result.update(missing_grants_total=1), lambda result: result.update(tables_checked=0),
])
def test_malformed_probe_response_never_proves_readiness(mutation):
    result = probe.response("devops", "worker_database_ready", tables_checked=1)
    mutation(result)
    with pytest.raises(ValueError):
        probe.validate_response(result, "devops")


async def test_untrusted_catalog_identifier_cannot_enter_diagnosis(catalog, caplog):
    catalog.tables[-1] = table("l5_secret\nprivate-marker", ())
    result = await inspect(catalog)
    assert result["code"] == "worker_database_probe_unavailable"
    assert "private-marker" not in json.dumps(result) + caplog.text


async def test_schema_usage_is_required_even_when_tables_have_crud(catalog):
    catalog.scope["public_usage"] = False
    assert (await inspect(catalog))["code"] == "worker_database_schema_access_missing"


async def test_column_only_protected_access_is_also_rejected(catalog):
    catalog.tables[0]["protected_column_access"] = True
    assert (await inspect(catalog))["code"] == "worker_database_protected_access"
