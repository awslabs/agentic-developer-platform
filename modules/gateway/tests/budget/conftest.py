"""Existing cap-unit harnesses run with an explicitly enforcing control posture.

These harnesses stub budget SQL/results rather than a database. Live controls,
identity binding, off-mode accounting and transitions use real SQL/Redis in
 test_enforcement_controls and tests/agentauth/test_shared_model_identity.py.
"""

from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def enforcing_posture_for_legacy_cap_harnesses(request, monkeypatch):
    if request.module.__name__.rsplit(".", 1)[-1] in {
        "test_budget_overshoot",
        "test_check_unavailable_response",
        "test_fail_closed_enforcement",
        "test_org_settled_cap",
        "test_root_human_envelope",
        "test_run_spend_cap",
        "test_shadow_mode_attribution",
    }:
        monkeypatch.setattr("src.budget.enforcement_service.BudgetEnforcementService.prepare_enforcement_context", AsyncMock(return_value=None))
