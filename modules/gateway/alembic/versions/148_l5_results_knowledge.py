"""Install additive L5 result, triage and knowledge publication tables."""

revision = "148_l5_results_knowledge"
down_revision = "147_merge_l5_list_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from l5_services.migrations.versions import l5_devops_knowledge_publication, l5_results_and_triage

    l5_results_and_triage.upgrade()
    l5_devops_knowledge_publication.upgrade()


def downgrade() -> None:
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed")
