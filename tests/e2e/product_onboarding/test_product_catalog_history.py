"""Offline SCN-02 history selection, ownership and actor-session contracts."""

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.new_ui.product_fixture_provider import BrowserFixtureProvider
from tests.e2e.new_ui.product_fixture_state import BrowserRunState
from tests.e2e.product_onboarding.product_catalog_history import (
    HISTORY_SELECTORS,
    begin_history_fixture,
    pinned_history_scenario,
    record_history_attempt,
    verify_history_sessions,
)

ROOT = Path(__file__).resolve().parents[3]


class LocalState:
    def __init__(self, binding):
        self.binding = binding
        self.document = {"cases": {}}
        self.saved = 0

    def save(self, value):
        self.document = deepcopy(value)
        self.saved += 1


@pytest.fixture
def scenario():
    binding = {"run_id": "example-history-run", "source_revision": "a" * 40,
               "tenant_id": "example-tenant", "actor_id": "editor"}
    actors = {role: {"actor_id": role, "subject": f"subject-{role}", "tenant_id": binding["tenant_id"]}
              for role in ("editor", "authorized", "revoked")}
    products = [{"product_id": f"product-{index}", "project_id": f"project-{index}", "work_id": f"story-{index}"}
                for index in (1, 2)]
    timeline = {"valid_from": "2026-10-01T00:00:00Z", "switch": "2026-10-05T00:00:00Z",
                "before_correction": "2026-10-07T00:00:00Z", "after_correction": "2026-10-09T00:00:00Z",
                "clock": "2026-10-10T00:00:00Z"}
    plan = pinned_history_scenario(binding["source_revision"], "b" * 40,
                                   binding["run_id"], actors, products, timeline)
    return binding, plan


def test_history_selectors_pin_exact_local_api_and_access_cases(scenario):
    binding, plan = scenario
    assert plan["scenario_id"] == "SCN-02" and plan["status"] == "not_run"
    assert plan["selectors"] == HISTORY_SELECTORS
    for selector in HISTORY_SELECTORS.values():
        result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", selector],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.splitlines().count(selector) == 1
    with pytest.raises(ValueError, match="reviewed API and access"):
        pinned_history_scenario(plan["source_revision"], plan["requirements_revision"], binding["run_id"],
                                plan["actors"], plan["products"], plan["timeline"], {"history": "other"})
    with pytest.raises(ValueError, match="pinned source"):
        pinned_history_scenario("short", plan["requirements_revision"], binding["run_id"],
                                plan["actors"], plan["products"], plan["timeline"])
    for changed in ({"switch": "2026-10-01T00:00:00Z"}, {"clock": "2026-10-10T00:00:00-04:00"}):
        with pytest.raises(ValueError, match="History dates|UTC instants"):
            pinned_history_scenario(plan["source_revision"], plan["requirements_revision"], binding["run_id"],
                                    plan["actors"], plan["products"], {**plan["timeline"], **changed})
    for roles in ({**plan["actors"], "revoked": plan["actors"]["authorized"]},
                  {**plan["actors"], "revoked": {**plan["actors"]["revoked"], "tenant_id": "other-tenant"}}):
        with pytest.raises(ValueError, match="distinct ordinary actors|selected tenant"):
            pinned_history_scenario(plan["source_revision"], plan["requirements_revision"], binding["run_id"],
                                    roles, plan["products"], plan["timeline"])
    with pytest.raises(ValueError, match="distinct Product"):
        pinned_history_scenario(plan["source_revision"], plan["requirements_revision"], binding["run_id"],
                                plan["actors"], [plan["products"][0]] * 2, plan["timeline"])


def test_history_persists_owned_attempt_provenance_without_sessions(scenario):
    binding, plan = scenario
    state = LocalState(binding)
    started = begin_history_fixture(state, plan)
    assert state.saved == 1 and started["cleanup_pending"] and started["attempts"] == {}
    assert begin_history_fixture(state, plan) == started and state.saved == 1
    with pytest.raises(ValueError, match="original owned intent"):
        begin_history_fixture(state, {**plan, "requirements_revision": "c" * 40})
    different = LocalState({**binding, "actor_id": "different-editor"})
    with pytest.raises(ValueError, match="protected fixture ownership"):
        begin_history_fixture(different, plan)
    entry = record_history_attempt(state, "attempt-1", "authorized", plan["products"][0],
                                   {"result": "interrupted", "evidence_ref": "private-attempt-1"})
    assert entry["source_revision"] == plan["source_revision"]
    assert entry["requirements_revision"] == plan["requirements_revision"]
    assert entry["timeline"] == plan["timeline"] and entry["actor"] == plan["actors"]["authorized"]
    assert record_history_attempt(state, "attempt-1", "authorized", plan["products"][0],
                                  entry["observation"]) == entry and state.saved == 2
    assert "access_token" not in json.dumps(state.document)
    with pytest.raises(ValueError, match="durable record"):
        record_history_attempt(state, "attempt-1", "revoked", plan["products"][0], entry["observation"])
    with pytest.raises(ValueError, match="unowned Product"):
        record_history_attempt(state, "attempt-2", "editor",
                               {**plan["products"][0], "project_id": "foreign-project"}, entry["observation"])
    assert state.saved == 2


def test_history_requires_independent_authenticated_sessions(scenario):
    _, plan = scenario
    sessions = {role: {"access_token": f"private-{role}"} for role in plan["actors"]}
    calls = []

    def read(url, *, headers, timeout):
        assert url == "https://example.invalid/api/auth/me" and timeout == 30_000
        token = headers["Authorization"].removeprefix("Bearer private-")
        calls.append(token)
        return SimpleNamespace(status=200, json=lambda: {
            "account_type": "human", "is_admin": False, "org_id": plan["actors"][token]["tenant_id"],
            "user_id": plan["actors"][token]["subject"],
        })

    request = SimpleNamespace(get=read)
    observed = verify_history_sessions(request, "https://example.invalid", plan, sessions)
    assert calls == ["editor", "authorized", "revoked"]
    assert {role: value["actor_id"] for role, value in observed.items()} == {role: role for role in plan["actors"]}
    with pytest.raises(ValueError, match="separately authenticated"):
        verify_history_sessions(request, "https://example.invalid", plan,
                                {**sessions, "revoked": sessions["authorized"]})
    def wrong_identity(url, *, headers, timeout):
        response = read(url, headers=headers, timeout=timeout)
        value = response.json()
        value["org_id"] = "other-tenant"
        return SimpleNamespace(status=200, json=lambda: value)

    with pytest.raises(ValueError, match="actor differs"):
        verify_history_sessions(SimpleNamespace(get=wrong_identity), "https://example.invalid", plan, sessions)

    duplicated = deepcopy(plan)
    duplicated["actors"]["revoked"]["subject"] = duplicated["actors"]["authorized"]["actor_id"]
    def reused_identity(url, *, headers, timeout):
        response = read(url, headers=headers, timeout=timeout)
        value = response.json()
        if headers["Authorization"].endswith("revoked"):
            value["user_id"] = duplicated["actors"]["authorized"]["actor_id"]
        elif headers["Authorization"].endswith("authorized"):
            value["user_id"] = duplicated["actors"]["authorized"]["actor_id"]
        return SimpleNamespace(status=200, json=lambda: value)

    with pytest.raises(ValueError, match="separately authenticated"):
        verify_history_sessions(SimpleNamespace(get=reused_identity), "https://example.invalid", duplicated, sessions)


def test_history_cleanup_guards_preserve_pending_owned_links(scenario):
    binding, plan = scenario
    state = LocalState(binding)
    begin_history_fixture(state, plan)
    browser = object.__new__(BrowserRunState)
    browser.document = state.document
    with pytest.raises(ValueError, match="catalog fixture cleanup"):
        browser.release()
    provider = object.__new__(BrowserFixtureProvider)
    provider.state = state
    with pytest.raises(ValueError, match="catalog links must close"):
        provider.cleanup("unrelated-product", None)
    assert state.saved == 1 and state.document["catalog_history"]["cleanup_pending"] is True
