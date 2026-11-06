"""Serialize identity and membership changes with durable person admissions."""

from alembic import op

revision = "119_metering_binding_fence"
down_revision = "118_merge_metering_onboarding"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_metering_alias_identity_revision", "metering_scope_aliases", ["identity_revision"])
    op.execute(
        """
        CREATE FUNCTION metering_binding_fence() RETURNS trigger AS $$
        DECLARE
            keys text[] := ARRAY[]::text[];
            key text;
        BEGIN
            PERFORM set_config('lock_timeout', '500ms', true);
            IF TG_OP <> 'INSERT' THEN
                keys := array_append(keys, 'user:' || OLD.user_id);
            END IF;
            IF TG_OP <> 'DELETE' THEN
                keys := array_append(keys, 'user:' || NEW.user_id);
            END IF;
            IF TG_TABLE_NAME = 'user_identities' THEN
                IF TG_OP <> 'INSERT' THEN
                    keys := array_append(keys, 'anchor:' || OLD.provider || ':' || OLD.provider_user_id);
                END IF;
                IF TG_OP <> 'DELETE' THEN
                    keys := array_append(keys, 'anchor:' || NEW.provider || ':' || NEW.provider_user_id);
                END IF;
            END IF;
            FOR key IN SELECT DISTINCT unnest(keys) ORDER BY 1 LOOP
                PERFORM pg_advisory_xact_lock(hashtextextended('metering:binding:' || key, 0));
            END LOOP;
            IF TG_TABLE_NAME = 'user_identities' AND TG_OP <> 'DELETE' THEN
                IF NEW.verification_method IN ('oauth', 'org_placement', 'admin_attested', 'magic_link_confirmed') THEN
                INSERT INTO metering_scope_aliases (alias_kind, stable_alias, scope_key, identity_revision)
                SELECT DISTINCT 'person_user', recipients.user_id, aliases.scope_key, aliases.identity_revision
                FROM user_identities donors
                JOIN user_identities recipients
                    ON recipients.provider = donors.provider
                    AND recipients.provider_user_id = donors.provider_user_id
                    AND recipients.verification_method IN ('oauth', 'org_placement', 'admin_attested', 'magic_link_confirmed')
                JOIN metering_scope_aliases aliases
                    ON aliases.alias_kind = 'person_user'
                    AND (aliases.stable_alias = donors.user_id
                        OR aliases.identity_revision = NEW.provider || ':' || NEW.provider_user_id)
                WHERE donors.provider = NEW.provider AND donors.provider_user_id = NEW.provider_user_id
                    AND donors.verification_method IN ('oauth', 'org_placement', 'admin_attested', 'magic_link_confirmed')
                ON CONFLICT DO NOTHING;
                END IF;
            END IF;
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in ("user_identities", "tenant_memberships", "team_memberships"):
        op.execute(
            f"CREATE TRIGGER trg_metering_binding_fence AFTER INSERT OR UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION metering_binding_fence()"
        )
    op.execute(
        """
        CREATE FUNCTION metering_user_binding_fence() RETURNS trigger AS $$
        DECLARE
            key text;
        BEGIN
            PERFORM set_config('lock_timeout', '500ms', true);
            FOR key IN
                SELECT DISTINCT binding FROM (
                    SELECT 'anchor:' || provider || ':' || provider_user_id AS binding
                    FROM user_identities WHERE user_id = NEW.id
                    UNION ALL SELECT 'anchor:cognito:' || OLD.cognito_sub WHERE OLD.cognito_sub IS NOT NULL
                    UNION ALL SELECT 'anchor:cognito:' || NEW.cognito_sub WHERE NEW.cognito_sub IS NOT NULL
                    UNION ALL SELECT 'user:' || NEW.id
                ) AS keys ORDER BY 1
            LOOP
                PERFORM pg_advisory_xact_lock(hashtextextended('metering:binding:' || key, 0));
            END LOOP;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_metering_user_binding_fence AFTER UPDATE OF cognito_sub, org_id, team_id ON users "
        "FOR EACH ROW EXECUTE FUNCTION metering_user_binding_fence()"
    )
    op.execute(
        """
        CREATE FUNCTION metering_user_deletion_guard() RETURNS trigger AS $$
        DECLARE
            key text;
        BEGIN
            PERFORM set_config('lock_timeout', '500ms', true);
            FOR key IN
                SELECT DISTINCT binding FROM (
                    SELECT 'anchor:' || provider || ':' || provider_user_id AS binding
                    FROM user_identities WHERE user_id = OLD.id
                    UNION ALL SELECT 'anchor:cognito:' || OLD.cognito_sub WHERE OLD.cognito_sub IS NOT NULL
                    UNION ALL SELECT 'user:' || OLD.id
                ) AS keys ORDER BY 1
            LOOP
                PERFORM pg_advisory_xact_lock(hashtextextended('metering:binding:' || key, 0));
            END LOOP;
            IF EXISTS (
                SELECT 1 FROM metering_scope_aliases alias
                JOIN metering_attempt_scopes scope ON scope.scope_key = alias.scope_key
                JOIN metering_attempts attempt
                    ON attempt.tenant_id = scope.tenant_id AND attempt.request_id = scope.request_id
                WHERE alias.alias_kind = 'person_user' AND alias.stable_alias = OLD.id
                    AND attempt.state IN ('prepared', 'dispatch_armed', 'unknown', 'quarantined')
            ) THEN
                RAISE EXCEPTION 'unsettled metering person binding cannot be deleted';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("CREATE TRIGGER trg_metering_user_deletion_guard BEFORE DELETE ON users FOR EACH ROW EXECUTE FUNCTION metering_user_deletion_guard()")


def downgrade():
    raise RuntimeError("Metering identity and membership fences must survive rollback")
