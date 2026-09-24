"""Schema checks for replay-safe workspace and deployment creation."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_create_operation_migration_is_the_single_head():
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))

    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["029_add_event_principal_outcome"]
    assert scripts.get_revision("029_add_event_principal_outcome").down_revision == "028_deployment_namespace_quota"
    assert scripts.get_revision("028_deployment_namespace_quota").down_revision == "027_cli_bootstrap_foundation"
    assert set(scripts.get_revision("027_cli_bootstrap_foundation").down_revision) == {
        "021_deployment_identity",
        "018_bootstrap_read_tokens",
    }
    assert (
        scripts.get_revision("021_deployment_identity").down_revision
        == "020_merge_workspace_cli"
    )
    head = scripts.get_revision("020_merge_workspace_cli")
    assert set(head.down_revision) == {
        "019_workspace_operation_state",
        "017_add_workspace_bootstrap_reservations",
    }
    revision = scripts.get_revision("019_workspace_operation_state")
    assert revision.down_revision == "018_create_operation_idempotency"
