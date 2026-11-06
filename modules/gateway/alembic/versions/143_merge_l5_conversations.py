"""Join L5 and conversation history and install additive binding/setup tables."""

revision = "143_merge_l5_conversations"
down_revision = ("142_l5_security_release", "142_conversation_identity")
branch_labels = None
depends_on = None


def upgrade() -> None:
    from l5_services.migrations.versions import l5_bindings_and_setup

    l5_bindings_and_setup.upgrade()


def downgrade() -> None:
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed")
