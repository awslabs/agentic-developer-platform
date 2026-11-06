-- Run as the database owner after applying L5 migrations. This grants no
-- access to Product, identity, credential, or orchestration tables.
BEGIN;
DO $bootstrap$
DECLARE
    relation record;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'adp_l5_worker') THEN
        CREATE ROLE adp_l5_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'adp_l5_worker'
        AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Existing adp_l5_worker has unexpected privileges';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_auth_members membership
        JOIN pg_roles member_role ON member_role.oid = membership.member
        JOIN pg_roles granted_role ON granted_role.oid = membership.roleid
        WHERE member_role.rolname = 'adp_l5_worker'
        AND granted_role.rolname <> 'rds_iam'
    ) THEN
        RAISE EXCEPTION 'Existing adp_l5_worker has unexpected role memberships';
    END IF;
    GRANT rds_iam TO adp_l5_worker;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO adp_l5_worker', current_database());
    GRANT USAGE ON SCHEMA public TO adp_l5_worker;
    FOR relation IN
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public' AND starts_with(tablename, 'l5_')
    LOOP
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO adp_l5_worker', relation.tablename);
    END LOOP;
END
$bootstrap$;
COMMIT;
