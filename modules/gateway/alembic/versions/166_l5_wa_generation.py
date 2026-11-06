"""Install durable native generation requests and collection on the gateway lineage."""

revision = "166_l5_wa_generation"
down_revision = "165_l5_source_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from l5_services.migrations.versions import l5_wa_collection_details, l5_wa_generation_collection, l5_wa_generation_request

    l5_wa_generation_request.upgrade()
    l5_wa_generation_collection.upgrade()
    l5_wa_collection_details.upgrade()


def downgrade() -> None:
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed")
