"""PostgreSQL enforces tenant/product bindings and keeps recovery IDs on rollback."""

import psycopg2
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.shared.models.organization import Organization, User
from tests.migrations.conftest_postgres import downgrade, upgrade


def test_tenant_binding_uniqueness_and_code_rollback(pg_url):
    upgrade(pg_url, "150_gitlab_product_homes")
    engine = create_engine(pg_url)
    with Session(engine) as db:
        db.add(Organization(id="tenant-a", name="Example"))
        db.flush()
        db.add(User(id="owner", org_id="tenant-a", team_id="team", email="owner@example.test"))
        db.commit()
    engine.dispose()
    with psycopg2.connect(pg_url) as conn, conn.cursor() as cursor:
        cursor.execute("""INSERT INTO products
            (tenant_id,id,name,purpose,intended_users,owner_user_id,starting_point,created_by,goals,provenance,archived_at)
            VALUES ('tenant-a','product-a','Example','Example','[]','owner','idea','owner','[]','[]',now())""")
        cursor.execute("""INSERT INTO product_gitlab_homes (tenant_id,product_id,instance_url,group_id,full_path)
            VALUES ('tenant-a','product-a','https://gitlab.example.test',42,'tenant-a/product-a')""")
    with psycopg2.connect(pg_url) as conn, conn.cursor() as cursor:
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cursor.execute("""INSERT INTO product_gitlab_homes (tenant_id,product_id,instance_url)
                VALUES ('tenant-a','product-a','https://other.example.test')""")
    with psycopg2.connect(pg_url) as conn, conn.cursor() as cursor:
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            cursor.execute("""INSERT INTO product_gitlab_homes (tenant_id,product_id,instance_url)
                VALUES ('tenant-b','product-a','https://gitlab.example.test')""")
    downgrade(pg_url, "149_merge_l5_project_budgets")
    upgrade(pg_url, "150_gitlab_product_homes")
    with psycopg2.connect(pg_url) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT group_id FROM product_gitlab_homes")
        assert cursor.fetchall() == [(42,)]
