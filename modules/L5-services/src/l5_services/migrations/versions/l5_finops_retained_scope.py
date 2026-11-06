"""Retain native FinOps agent scope claims across project and tenant boundaries.

Revision ID: l5_finops_retained_scope
Revises: l5_devops_prevention_obs
"""

from hashlib import sha256
from json import loads

import rfc8785
import sqlalchemy as sa
from alembic import op

revision = "l5_finops_retained_scope"
down_revision = "l5_devops_prevention_obs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "l5_service_retained_agent_scopes",
        sa.Column("service_id", sa.String(32), primary_key=True),
        sa.Column("identity_digest", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(255)),
        sa.Column("product_id", sa.String(255)),
        sa.Column("project_id", sa.String(255)),
        sa.Column("binding_id", sa.String(36)),
        sa.Column("conflicted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    connection = op.get_bind()
    rows = connection.execute(sa.text(
        "SELECT tenant_id, product_id, project_id, binding_id, provider_identity, "
        "native_resource_ref FROM l5_service_bindings WHERE service_id = 'finops' "
        "AND provider_identity IS NOT NULL AND native_resource_ref IS NOT NULL"
    )).mappings()
    claims = {}
    for row in rows:
        identity = row["provider_identity"]
        reference = row["native_resource_ref"]
        try:
            identity = loads(identity) if isinstance(identity, str) else identity
            reference = loads(reference) if isinstance(reference, str) else reference
        except ValueError:
            continue
        if (not isinstance(identity, dict) or not isinstance(reference, dict)
                or reference.get("record_type") != "finops_agent"
                or any(not isinstance(identity.get(field), str) or not identity[field]
                       for field in ("account_id", "region"))
                or not isinstance(reference.get("record_id"), str)
                or not reference["record_id"]):
            continue
        digest = sha256(rfc8785.dumps({"account_id": identity["account_id"],
                                      "region": identity["region"],
                                      "agent_id": reference["record_id"]})).hexdigest()
        claim = claims.setdefault(digest, {
            "service_id": "finops", "identity_digest": digest,
            "tenant_id": row["tenant_id"], "product_id": row["product_id"],
            "project_id": row["project_id"], "binding_id": row["binding_id"],
            "conflicted": False,
        })
        if (claim["tenant_id"], claim["product_id"], claim["project_id"]) != (
            row["tenant_id"], row["product_id"], row["project_id"]
        ):
            claim["conflicted"] = True
    if claims:
        table = sa.table(
            "l5_service_retained_agent_scopes",
            sa.column("service_id", sa.String(32)), sa.column("identity_digest", sa.String(64)),
            sa.column("tenant_id", sa.String(255)), sa.column("product_id", sa.String(255)),
            sa.column("project_id", sa.String(255)), sa.column("binding_id", sa.String(36)),
            sa.column("conflicted", sa.Boolean()),
        )
        op.bulk_insert(table, list(claims.values()))


def downgrade():
    op.drop_table("l5_service_retained_agent_scopes")
