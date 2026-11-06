"""Install fenced native knowledge history through the production Gateway chain."""

revision = "179_l5_knowledge_effects"
down_revision = "178_security_manual_evidence"
branch_labels = None
depends_on = None


def upgrade():
    from l5_services.migrations.versions import l5_knowledge_effects

    l5_knowledge_effects.upgrade()


def downgrade():
    raise RuntimeError("The L5 release retains owner authorization and qualification history and cannot safely be reversed")
