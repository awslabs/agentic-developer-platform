import re

import migration_receipt


def checked_roles(config, manifest, manifest_digest):
    database = config["database_name"]
    cell = config["cell_id"]
    resource_id = config["existing_db_resource_id"]
    migration = config["migration_username"]
    writer = config["writer_db_username"]
    reader = config["reader_db_username"]
    if (not isinstance(database, str) or not migration_receipt.IDENTIFIER.fullmatch(database) or
            not isinstance(cell, str) or not migration_receipt.IDENTIFIER.fullmatch(cell) or
            not isinstance(resource_id, str) or not migration_receipt.RESOURCE_ID.fullmatch(resource_id) or
            not isinstance(manifest_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_digest) or
            not all(isinstance(role, str) and migration_receipt.ROLE.fullmatch(role)
                    for role in (migration, writer, reader)) or
            len({migration, writer, reader}) != 3):
        raise ValueError("registered cell database and three distinct roles are required")

    tables = manifest["raw"]["tables"] + manifest["tool"] + manifest["domain"]
    quoted = ", ".join(f"'{table}'" for table in tables)
    objects = ", ".join(f'public."{table}"' for table in tables)
    guard_checks = "\n".join(
        "    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger "
        f"WHERE tgrelid = pg_catalog.to_regclass('public.{table}') "
        "AND tgfoid = 'devlake_meta.require_current_generation()'::regprocedure "
        "AND tgenabled = 'O' AND tgtype & 31 = 31) THEN "
        "RAISE EXCEPTION 'migration_required: collection write guard is absent'; END IF;"
        for table in tables
    )
    return f"""
DO $cell_roles$
BEGIN
    IF current_database() <> '{database}' OR current_user <> '{migration}' OR
       NOT EXISTS (SELECT 1 FROM pg_catalog.pg_database
                   WHERE datname = '{database}' AND datdba = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = '{migration}')) THEN
        RAISE EXCEPTION 'registered cell DB must be owned by the migration role';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname IN ('{migration}', '{writer}', '{reader}')
               AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls)) OR
       (SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname IN ('{migration}', '{writer}', '{reader}')) <> 3 OR
       EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname IN ('{writer}', '{reader}')
               AND (rolinherit OR NOT rolcanlogin)) OR
       EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members AS membership
               JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member
               JOIN pg_catalog.pg_roles AS granted ON granted.oid = membership.roleid
               WHERE member.rolname IN ('{migration}', '{writer}', '{reader}') AND granted.rolname <> 'rds_iam') OR
       (SELECT count(*) FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member
        JOIN pg_catalog.pg_roles AS granted ON granted.oid = membership.roleid
        WHERE member.rolname IN ('{migration}', '{writer}', '{reader}') AND granted.rolname = 'rds_iam') <> 3 THEN
        RAISE EXCEPTION 'registered cell roles must be nonprivileged IAM-only identities';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_class AS table_object
               JOIN pg_catalog.pg_namespace AS schema_object ON schema_object.oid = table_object.relnamespace
               WHERE schema_object.nspname = 'public' AND table_object.relkind IN ('r', 'p')
                 AND (table_object.relname NOT IN ({quoted}) OR
                      table_object.relowner <> (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = '{migration}'))) THEN
        RAISE EXCEPTION 'unregistered or foreign-owned table exists in selected cell database';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_class AS table_object
               JOIN pg_catalog.pg_namespace AS schema_object ON schema_object.oid = table_object.relnamespace
               CROSS JOIN LATERAL pg_catalog.aclexplode(
                   coalesce(table_object.relacl, pg_catalog.acldefault('r', table_object.relowner))) AS privilege
               WHERE schema_object.nspname IN ('public', 'devlake_meta') AND table_object.relkind IN ('r', 'p')
                 AND privilege.grantee <> 0 AND privilege.grantee NOT IN
                     (SELECT oid FROM pg_catalog.pg_roles WHERE rolname IN ('{migration}', '{writer}', '{reader}'))) THEN
        RAISE EXCEPTION 'foreign identity already holds a cell table grant';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM devlake_meta.schema_receipts
                   WHERE cell_id = '{cell}' AND db_resource_id = '{resource_id}'
                     AND manifest_sha256 = '{manifest_digest}' AND schema_version = 2 AND status = 'applied') THEN
        RAISE EXCEPTION 'migration_required: matching cell schema receipt is absent';
    END IF;
    IF EXISTS (SELECT 1 FROM devlake_meta.schema_receipts
               WHERE cell_id <> '{cell}' OR db_resource_id <> '{resource_id}') THEN
        RAISE EXCEPTION 'foreign cell receipt exists in selected database';
    END IF;
{guard_checks}
END;
$cell_roles$;
REVOKE CONNECT, CREATE, TEMPORARY ON DATABASE "{database}" FROM PUBLIC, "{writer}", "{reader}";
REVOKE ALL ON SCHEMA public FROM PUBLIC, "{writer}", "{reader}";
REVOKE ALL ON SCHEMA devlake_meta FROM PUBLIC, "{writer}", "{reader}";
REVOKE ALL ON {objects} FROM PUBLIC, "{writer}", "{reader}";
GRANT CONNECT ON DATABASE "{database}" TO "{migration}", "{writer}", "{reader}";
GRANT USAGE ON SCHEMA public TO "{writer}", "{reader}";
GRANT SELECT, INSERT, UPDATE, DELETE ON {objects} TO "{writer}";
GRANT SELECT ON {objects} TO "{reader}";
DO $cell_sequences$
DECLARE sequence_name regclass;
BEGIN
    FOR sequence_name IN
        SELECT sequence_object.oid::regclass
        FROM pg_catalog.pg_class AS sequence_object
        JOIN pg_catalog.pg_depend AS dependency ON dependency.objid = sequence_object.oid
        JOIN pg_catalog.pg_class AS table_object ON table_object.oid = dependency.refobjid
        JOIN pg_catalog.pg_namespace AS schema_object ON schema_object.oid = table_object.relnamespace
        WHERE sequence_object.relkind = 'S' AND dependency.deptype IN ('a', 'i')
          AND schema_object.nspname = 'public' AND table_object.relname IN ({quoted})
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SEQUENCE %s FROM PUBLIC, %I, %I', sequence_name, '{writer}', '{reader}');
        EXECUTE pg_catalog.format('GRANT USAGE, SELECT ON SEQUENCE %s TO %I', sequence_name, '{writer}');
    END LOOP;
END;
$cell_sequences$;
GRANT USAGE ON SCHEMA devlake_meta TO "{reader}";
REVOKE ALL ON ALL TABLES IN SCHEMA devlake_meta FROM PUBLIC, "{writer}", "{reader}";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA devlake_meta FROM PUBLIC, "{writer}", "{reader}";
GRANT SELECT ON devlake_meta.collection_batch_manifests, devlake_meta.collection_batch_items TO "{reader}";
GRANT USAGE ON SCHEMA devlake_meta TO "{writer}";
GRANT SELECT ON devlake_meta.schema_receipts, devlake_meta.collection_generations,
    devlake_meta.collection_checkpoints TO "{writer}";
GRANT EXECUTE ON FUNCTION
    devlake_meta.begin_collection(text, bigint, text, text, text, text),
    devlake_meta.lock_generation(text, bigint),
    devlake_meta.cancel_collection(text, bigint),
    devlake_meta.native_connection(text, text, text, text, text, bigint),
    devlake_meta.seal_batch(text, bigint, uuid, bytea[], bytea, jsonb)
TO "{writer}";
ALTER DEFAULT PRIVILEGES FOR ROLE "{migration}" IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE "{migration}" IN SCHEMA devlake_meta REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE "{migration}" REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
"""
