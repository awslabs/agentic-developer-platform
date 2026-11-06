"""Read-only installed worker database readiness; never admits or repairs work."""

import asyncio
import os
import re

PROTOCOL = "l5-worker-database-privileges/v1"
REQUEST_KEY = "l5_database_privilege_probe"
REQUEST = {REQUEST_KEY: 1}
SERVICE_IDS = frozenset({"security", "devops", "transform", "well_architected", "finops"})
CRUD = ("SELECT", "INSERT", "UPDATE", "DELETE")
MAX_TABLES = 2048
MAX_MISSING = 20
CODES = frozenset({
    "worker_database_ready", "worker_database_grants_missing",
    "worker_database_role_invalid", "worker_database_protected_access",
    "worker_database_schema_missing", "worker_database_schema_access_missing", "worker_database_probe_unavailable",
    "worker_database_probe_invalid",
})


def response(service_id, code, *, tables_checked=0, missing=()):
    return {"protocol": PROTOCOL, "service_id": service_id,
            "passed": code == "worker_database_ready", "code": code,
            "tables_checked": tables_checked, "missing_grants_total": len(missing),
            "missing_grants": list(missing[:MAX_MISSING])}


def validate_response(value, service_id):
    """Reject older/malformed output before it reaches a lifecycle receipt."""
    keys = {"protocol", "service_id", "passed", "code", "tables_checked",
            "missing_grants_total", "missing_grants"}
    if (not isinstance(value, dict) or set(value) != keys
            or value["protocol"] != PROTOCOL or value["service_id"] != service_id
            or type(value["passed"]) is not bool or not isinstance(value["code"], str) or value["code"] not in CODES
            or value["passed"] != (value["code"] == "worker_database_ready")
            or any(type(value[key]) is not int or not 0 <= value[key] <= MAX_TABLES
                   for key in ("tables_checked", "missing_grants_total"))
            or not isinstance(value["missing_grants"], list)
            or len(value["missing_grants"]) != min(value["missing_grants_total"], MAX_MISSING)
            or value["missing_grants_total"] > value["tables_checked"]):
        raise ValueError("Unsupported or malformed database readiness response")
    names = []
    for item in value["missing_grants"]:
        if (not isinstance(item, dict) or set(item) != {"table", "privileges"}
                or not isinstance(item["table"], str)
                or not re.fullmatch(r"l5_[a-z0-9_]{1,60}", item["table"])
                or not isinstance(item["privileges"], list) or not item["privileges"]
                or item["privileges"] != [right for right in CRUD if right in item["privileges"]]):
            raise ValueError("Malformed database grant identifiers")
        names.append(item["table"])
    if (names != sorted(set(names))
            or (value["code"] == "worker_database_grants_missing") != (value["missing_grants_total"] > 0)
            or (value["passed"] and value["tables_checked"] == 0)):
        raise ValueError("Inconsistent database readiness response")
    return value


async def inspect_database(sessions, service_id, configured_role):
    """Inspect catalogs as the actual worker principal, within a bounded read transaction."""
    from sqlalchemy import text

    async with sessions() as session:
        try:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            await session.execute(text("SET LOCAL statement_timeout='3s'"))
            role = (await session.execute(text("""
                SELECT current_user = session_user AND current_user = :role AS identity_matches,
                       rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls,
                       rolinherit, rolcanlogin
                FROM pg_roles WHERE rolname = current_user
            """), {"role": configured_role})).mappings().one()
            memberships = (await session.execute(text("""
                SELECT parent.rolname, membership.admin_option
                FROM pg_auth_members membership
                JOIN pg_roles parent ON parent.oid = membership.roleid
                JOIN pg_roles member ON member.oid = membership.member
                WHERE member.rolname = current_user ORDER BY parent.rolname
            """))).mappings().all()
            if (role["identity_matches"] is not True or role["rolcanlogin"] is not True
                    or any(role[key] is not False for key in (
                        "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls", "rolinherit"))
                    or [dict(row) for row in memberships] != [{"rolname": "rds_iam", "admin_option": False}]):
                return response(service_id, "worker_database_role_invalid")
            scope = (await session.execute(text("""
                SELECT has_database_privilege(current_user, current_database(), 'CREATE') AS database_create,
                       has_schema_privilege(current_user, 'public', 'USAGE') AS public_usage,
                       EXISTS (SELECT 1 FROM pg_namespace
                               WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'
                               AND has_schema_privilege(current_user, oid, 'CREATE')) AS schema_create
            """))).mappings().one()
            if scope["database_create"] is not False or scope["schema_create"] is not False:
                return response(service_id, "worker_database_protected_access")
            if scope["public_usage"] is not True:
                return response(service_id, "worker_database_schema_access_missing")
            tables = (await session.execute(text("""
                SELECT c.relname, starts_with(c.relname, 'l5_') AS is_l5,
                       has_table_privilege(current_user, c.oid, 'SELECT') AS can_select,
                       has_table_privilege(current_user, c.oid, 'INSERT') AS can_insert,
                       has_table_privilege(current_user, c.oid, 'UPDATE') AS can_update,
                       has_table_privilege(current_user, c.oid, 'DELETE') AS can_delete,
                       has_table_privilege(current_user, c.oid, 'TRUNCATE') AS can_truncate,
                       has_table_privilege(current_user, c.oid, 'REFERENCES') AS can_references,
                       has_table_privilege(current_user, c.oid, 'TRIGGER') AS can_trigger,
                       NOT starts_with(c.relname, 'l5_') AND (
                           has_any_column_privilege(current_user, c.oid, 'SELECT')
                           OR has_any_column_privilege(current_user, c.oid, 'INSERT')
                           OR has_any_column_privilege(current_user, c.oid, 'UPDATE')
                           OR has_any_column_privilege(current_user, c.oid, 'REFERENCES')) AS protected_column_access
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND (c.relkind IN ('r', 'p')
                    OR (NOT starts_with(c.relname, 'l5_') AND c.relkind IN ('v', 'm', 'f')))
                ORDER BY c.relname LIMIT 2049
            """))).mappings().all()
            if len(tables) > MAX_TABLES:
                return response(service_id, "worker_database_probe_unavailable")
            l5_tables = [row for row in tables if row["is_l5"]]
            for row in tables:
                if not row["is_l5"] and (row["protected_column_access"] is not False
                        or any(row["can_" + right.lower()] is not False
                               for right in (*CRUD, "TRUNCATE", "REFERENCES", "TRIGGER"))):
                    return response(service_id, "worker_database_protected_access", tables_checked=len(l5_tables))
            if not l5_tables:
                return response(service_id, "worker_database_schema_missing")
            missing = [{"table": row["relname"],
                        "privileges": [right for right in CRUD if row["can_" + right.lower()] is not True]}
                       for row in l5_tables if any(row["can_" + right.lower()] is not True for right in CRUD)]
            result = response(service_id, "worker_database_grants_missing" if missing else "worker_database_ready",
                              tables_checked=len(l5_tables), missing=missing)
            return validate_response(result, service_id)
        finally:
            await session.rollback()


async def run_probe(event, *, sessions=None):
    """No factory load, broker/provider call, work claim, role change or grant."""
    service_id = os.environ.get("L5_SERVICE_ID")
    if service_id not in SERVICE_IDS:
        return response("unknown", "worker_database_role_invalid")
    if (not isinstance(event, dict) or set(event) != {REQUEST_KEY}
            or type(event[REQUEST_KEY]) is not int or event[REQUEST_KEY] != 1):
        return response(service_id, "worker_database_probe_invalid")
    configured_role = os.environ.get("L5_RDS_USER")
    if configured_role != "adp_l5_worker":
        return response(service_id, "worker_database_role_invalid")
    try:
        async with asyncio.timeout(10):
            if sessions is None:
                from l5_services.runtime_support import sessions as installed_sessions
                sessions = installed_sessions()
            return await inspect_database(sessions, service_id, configured_role)
    except Exception:
        # Database exceptions can contain SQL, credentials or parameters. No raw error logging.
        return response(service_id, "worker_database_probe_unavailable")
