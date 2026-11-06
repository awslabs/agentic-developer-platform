"""Offline recovery selection, durable ownership and retained attempt controls."""

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from tests.e2e.new_ui.product_fixture_provider import BrowserFixtureProvider
from tests.e2e.new_ui.product_fixture_state import BrowserRunState
from tests.e2e.product_onboarding.product_catalog_fixture import (
    begin_membership_fixture, pinned_membership_scenario, record_membership_cleanup, record_membership_receipts,
)
from tests.e2e.product_onboarding.product_catalog_recovery_scenario import (
    MAX_ATTEMPTS, RECOVERY_SELECTORS, begin_recovery_fixture, pinned_recovery_scenario, record_recovery_cleanup,
)
from tests.e2e.product_onboarding.product_catalog_release import pin_catalog_release
from tests.e2e.product_onboarding.test_product_catalog_fixture import LocalState
from tests.e2e.product_onboarding.test_product_catalog_release import catalog_release_fixture

ROOT = Path(__file__).resolve().parents[3]


def owned_state():
    binding = {"run_id": "example-recovery", "source_revision": "a" * 40,
               "tenant_id": "example-tenant", "actor_id": "example-editor"}
    state = LocalState(binding)
    plan = pinned_membership_scenario(binding["source_revision"], "b" * 40,
                                      binding["run_id"], "2026-10-01T00:00:00Z")
    begin_membership_fixture(state, plan, {"product_id": "owned-product", "project_id": "owned-project",
                                           "product_name": "Owned Product"})
    selection, store, observe, _, _, _ = catalog_release_fixture(state)
    release = pin_catalog_release(state, selection, store, observe)
    return state, release


def test_recovery_plan_pins_release_owner_clock_and_collectable_local_selectors():
    state, release = owned_state()
    plan = pinned_recovery_scenario(state)
    assert plan["scenario_id"] == "SCN-03" and plan["status"] == "not_run"
    assert plan["max_attempts"] == MAX_ATTEMPTS == 3
    assert plan["selectors"] == RECOVERY_SELECTORS
    assert plan["release"] == {key: value for key, value in release.items() if key != "observation_ref"}
    assert plan["owner"] == {"tenant_id": state.binding["tenant_id"], "actor_id": state.binding["actor_id"],
                             "product_id": "owned-product", "project_id": "owned-project",
                             "portfolios": ["Commerce", "Shared services"]}
    assert plan["clock"] == state.document["catalog"]["plan"]["clock"]
    for selector in RECOVERY_SELECTORS.values():
        result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", selector],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.splitlines().count(selector) == 1
    with pytest.raises(ValueError, match="reviewed API and cleanup"):
        pinned_recovery_scenario(state, {**RECOVERY_SELECTORS, "api": "unreviewed"})
    other = LocalState({**state.binding, "actor_id": "foreign-editor"})
    other.document = deepcopy(state.document)
    assert pinned_recovery_scenario(other)["owner"] != plan["owner"]
    with pytest.raises(ValueError, match="protected ownership"):
        begin_recovery_fixture(other, plan)
    changed = deepcopy(state.document)
    changed["catalog"]["plan"]["requirements_revision"] = "not-a-revision"
    state.document = changed
    with pytest.raises(ValueError, match="pinned source"):
        pinned_recovery_scenario(state)


def test_recovery_intent_blocks_foreign_restart_and_releases_only_after_recorded_cleanup():
    state, release = owned_state()
    plan = pinned_recovery_scenario(state)
    assert begin_recovery_fixture(state, plan)["cleanup_pending"] is True
    saved = state.saved
    assert begin_recovery_fixture(state, plan)["plan"] == plan and state.saved == saved
    resumed = LocalState(state.binding)
    resumed.document = deepcopy(state.document)
    assert begin_recovery_fixture(resumed, plan)["plan"] == plan and resumed.saved == 0
    stale_release = LocalState(state.binding)
    stale_release.document = deepcopy(state.document)
    stale_release.document["catalog_release"]["schema_head"] = "other_head"
    with pytest.raises(ValueError, match="protected ownership"):
        begin_recovery_fixture(stale_release, plan)
    assert "access_token" not in json.dumps(state.document)
    with pytest.raises(ValueError, match="protected ownership"):
        begin_recovery_fixture(state, {**plan, "max_attempts": 4})
    with pytest.raises(ValueError, match="independent owned cleanup"):
        record_recovery_cleanup(state)
    browser = object.__new__(BrowserRunState)
    browser.document = state.document
    with pytest.raises(ValueError, match="catalog fixture cleanup"):
        browser.release()
    provider = object.__new__(BrowserFixtureProvider)
    provider.state = state
    with pytest.raises(ValueError, match="catalog links must close"):
        provider.cleanup("owned-product", None)

    for index, entry in enumerate(state.document["catalog"]["portfolios"]):
        record_membership_receipts(state, entry["name"],
            {"id": f"portfolio-{index}", "revision": "1", "change_id": f"portfolio-change-{index}"},
            {"id": f"link-{index}", "revision": "1", "change_id": f"link-change-{index}"})
    first = state.document["catalog"]["portfolios"][0]
    first["recovery"] = {"release": plan["release"], "attempts": [
        {"state": "response_unknown", "observation_ref": release["observation_ref"]}]}
    state.save(deepcopy(state.document))
    observations = {entry["name"]: {"closed_revision": "2", "retired_revision": "2",
                                    "current_memberships": [], "portfolio_status": "retired"}
                    for entry in state.document["catalog"]["portfolios"]}
    record_membership_cleanup(state, observations)
    with pytest.raises(ValueError, match="unfinished"):
        record_recovery_cleanup(state)
    assert state.document["catalog_recovery"]["cleanup_pending"] is True
    first = state.document["catalog"]["portfolios"][0]
    first["recovery"]["attempts"].append({"state": "verified", "receipt": first["receipts"]["relationship"],
                                            "observation_ref": release["observation_ref"]})
    state.save(deepcopy(state.document))
    archived = record_recovery_cleanup(state)
    assert archived["cleanup_pending"] is False and archived["cleanup_observations"] == observations
    assert [attempt["state"] for attempt in archived["attempts"]] == ["response_unknown", "verified"]
    assert state.document["catalog_release"] == release
    assert record_recovery_cleanup(state) == archived
    foreign = LocalState({**state.binding, "actor_id": "foreign-editor"})
    foreign.document = deepcopy(state.document)
    with pytest.raises(ValueError, match="original selected release or ownership"):
        record_recovery_cleanup(foreign)
