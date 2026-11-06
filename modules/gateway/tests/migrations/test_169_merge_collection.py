"""Collection and service histories remain reachable from either deployed head."""

import pytest
from sqlalchemy import create_engine, inspect, text

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL


def test_collection_and_service_parents_are_preserved():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("169_merge_collection").down_revision == ("167_connector_installation", "168_merge_security_wa")
    assert scripts.get_revision("167_connector_installation").down_revision == "166_l5_wa_generation"
    assert scripts.get_revision("168_merge_security_wa").down_revision == ("164_l5_work_delegation_parent", "167_l5_wa_results")
    for parent in ("167_connector_installation", "168_merge_security_wa"):
        pending = {revision.revision for revision in scripts.iterate_revisions("head", parent, implicit_base=True)}
        assert parent not in pending and "169_merge_collection" in pending


def test_current_head_preserves_existing_downgrade_refusal():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True})
    with Operations.context(context), pytest.raises(RuntimeError, match=f"^{HEAD_DOWNGRADE_REFUSAL}$"):
        for revision in scripts.iterate_revisions("head", "169_atlassian_system"):
            revision.module.downgrade()


@pytest.mark.parametrize("parent", ["167_connector_installation", "168_merge_security_wa"])
def test_upgrade_from_collection_or_service_head_installs_both_contracts(pg_url, parent):
    upgrade(pg_url, parent)
    upgrade(pg_url)
    upgrade(pg_url)
    engine = create_engine(pg_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            inspector = inspect(connection)
            delegation = {column["name"]: column for column in inspector.get_columns("credential_service_delegations")}
            assert delegation["credential_id"]["nullable"]
            assert {"installation_id", "parent_delegation_id"} <= delegation.keys()
            assert "ck_service_delegation_source" in {entry["name"] for entry in inspector.get_check_constraints("credential_service_delegations")}
            assert {"fence", "lease_owner"} <= {column["name"] for column in inspector.get_columns("l5_service_setup_work")}
            assert inspector.has_table("l5_service_wa_usage")
    finally:
        engine.dispose()
