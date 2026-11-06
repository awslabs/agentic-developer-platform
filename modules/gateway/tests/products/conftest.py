"""Collect the stable Product scenario selectors alongside ordinary pytest tests."""

import pytest

from src.auth.dependencies import get_current_user
from src.shared.models.organization import Organization, User
from tests.products.membership import add_team_member


@pytest.fixture(autouse=True)
def product_profile_enabled(monkeypatch):
    monkeypatch.setenv("BG_PRODUCT_PROFILE_ENABLED", "true")


def pytest_configure(config):
    for name in (
        "minimal_create_replay",
        "ui_minimal_save",
        "ui_shared_edit",
        "ui_input_recovery",
        "invalid_create_atomic",
        "read_scope_denial",
        "edit_race",
        "archive_and_history",
        "lost_response_replay",
        "cli_install_and_create",
        "cli_errors",
        "cli_retry_compatibility",
        "bind_close_history",
        "binding_constraints",
        "shared_team_permissions",
        "changefeed_immutable_refs",
        "changefeed_replay_retention",
        "changefeed_current_access",
        "scope_switch",
        "scope_drafts_and_links",
        "scope_admin_states",
        "scope_denials",
        "session_minimal_resume",
        "progress_applicability",
        "session_state_freshness",
        "binding_verified_identity",
        "binding_freshness_redaction",
        "binding_retry_unlink",
        "financial_roundtrip",
        "financial_validation",
        "financial_scope_provenance",
        "handoff_authority",
        "callback_admission",
        "handoff_recovery",
        "source_reuse",
        "source_bounds_coverage",
        "source_untrusted_metadata",
        "environment_connection_identity",
        "source_picker_coverage",
        "source_picker_scope",
        "repository_explicit_create",
        "repository_exactly_once",
        "running_product_adoption",
        "deterministic_guidance",
        "bounded_model_proposals",
        "guidance_recovery",
        "selective_confirmation",
        "proposal_conflict_expiry",
        "proposal_replay_override",
    ):
        config.addinivalue_line("python_functions", name)


@pytest.fixture
async def source_member(db_session, app, valid_token_context):
    db_session.add(Organization(id=valid_token_context.org_id, name="Test workspace"))
    db_session.add(
        User(
            id=valid_token_context.user_id,
            org_id=valid_token_context.org_id,
            team_id=valid_token_context.team_id,
            email="member@example.test",
            user_kind="human",
        )
    )
    await add_team_member(db_session, valid_token_context)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: valid_token_context
    return valid_token_context
