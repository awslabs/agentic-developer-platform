"""Reject a nonarchived Product without a current Project at commit.

Revision ID: 113_product_scope_guard
Revises: 112_product_profile_foundation
"""

from alembic import op

revision = "113_product_scope_guard"
down_revision = "112_product_profile_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE FUNCTION product_scope_current_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            scope_tenant_id varchar(255);
            scope_product_id varchar(36);
            current_archived_at timestamptz;
        BEGIN
            IF TG_TABLE_NAME = 'products' THEN
                scope_tenant_id := NEW.tenant_id;
                scope_product_id := NEW.id;
            ELSE
                scope_tenant_id := OLD.tenant_id;
                scope_product_id := OLD.product_id;
            END IF;

            SELECT archived_at INTO current_archived_at
            FROM products
            WHERE tenant_id = scope_tenant_id AND id = scope_product_id
            FOR UPDATE;

            IF FOUND AND current_archived_at IS NULL AND NOT EXISTS (
                SELECT 1 FROM product_project_bindings
                WHERE tenant_id = scope_tenant_id
                  AND product_id = scope_product_id
                  AND valid_to IS NULL
            ) THEN
                RAISE EXCEPTION 'nonarchived Product must retain a current Project'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_product_current_project';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER product_scope_product_guard
        AFTER INSERT OR UPDATE ON products
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION product_scope_current_guard()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER product_scope_binding_guard
        AFTER UPDATE OR DELETE ON product_project_bindings
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION product_scope_current_guard()
        """
    )


def downgrade():
    op.execute("DROP TRIGGER product_scope_binding_guard ON product_project_bindings")
    op.execute("DROP TRIGGER product_scope_product_guard ON products")
    op.execute("DROP FUNCTION product_scope_current_guard()")
