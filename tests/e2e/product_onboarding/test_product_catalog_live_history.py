"""Offline actor and dated-read contracts for the protected live history selector."""

import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.e2e.product_onboarding.product_catalog_history import (
    begin_history_fixture, record_history_expectations,
)
from tests.e2e.product_onboarding.product_catalog_live_history import verify_live_history
from tests.e2e.product_onboarding.test_product_catalog_history import LocalState, scenario as scenario_fixture

ROOT = Path(__file__).resolve().parents[3]
SELECTOR = "tests/e2e/product_onboarding/test_v12.py::live_catalog_history_access"


def fixture_run(scenario):
    binding, plan = scenario
    state = LocalState(binding)
    begin_history_fixture(state, plan)
    first, second = plan["products"]
    expected = {"initiative_id": "shared-initiative",
                "membership": {"portfolio_id": "first-portfolio", "relationship_id": "dated-link",
                               "revision": "1", "valid_from": plan["timeline"]["valid_from"]},
                "contributions": {
                    first["project_id"]: {"id": "first-contribution", "product_id": first["product_id"],
                                          "before": {"revision": "1", "objective": "Original", "work_ids": [first["work_id"]]},
                                          "after": {"revision": "2", "objective": "Corrected", "work_ids": [first["work_id"]]}},
                    second["project_id"]: {"id": "second-contribution", "product_id": second["product_id"],
                                           "before": {"revision": "1", "objective": "Independent", "work_ids": [second["work_id"]]},
                                           "after": {"revision": "1", "objective": "Independent", "work_ids": [second["work_id"]]}},
                }}
    record_history_expectations(state, expected)
    sessions = {role: {"access_token": f"synthetic-{role}"} for role in plan["actors"]}
    calls = []
    faults = {}

    def get(url, *, headers, timeout):
        parsed = urlsplit(url)
        path, params = parsed.path, parse_qs(parsed.query)
        actor = headers["Authorization"].removeprefix("Bearer synthetic-")
        calls.append((path, actor, params))
        status = 200
        response_headers = {"X-Catalog-View": "historical"}
        if path == "/api/auth/me":
            body = {"account_type": "human", "is_admin": False,
                    "org_id": plan["actors"][actor]["tenant_id"],
                    "user_id": plan["actors"][actor]["subject"]}
            if faults.get("actor") == actor:
                body["user_id"] = "foreign-subject"
        elif path.startswith("/api/v1/catalog/initiatives/"):
            body = {"id": expected["initiative_id"]}
        elif path.endswith("/memberships"):
            rows = [] if params["effective_at"] == [plan["timeline"]["switch"]] else [{
                "relationship_id": "dated-link", "relationship_revision": "1",
                "valid_from": plan["timeline"]["valid_from"],
                "product_ref": {"record_id": first["product_id"], "project_id": first["project_id"],
                                "tenant_id": binding["tenant_id"]}}]
            body = {"items": rows}
        elif path.startswith("/api/v1/products/"):
            product = next(item for item in plan["products"] if item["project_id"] == path.split("/")[-1])
            body = {"product_id": product["product_id"], "project_id": product["project_id"]}
        elif path.endswith("/contributions"):
            project_id = path.split("/")[-2]
            if actor == "revoked" or (actor == "authorized" and project_id in (second["project_id"], "unknown")):
                status = 403 if actor == "revoked" else 404
                body = {"detail": ("Private outcome" if faults.get("leaked_detail") and project_id != "unknown"
                                   else "Unavailable"), **({"total": 1} if faults.get("leaked_count") else {})}
            else:
                receipt = expected["contributions"][project_id]
                time = "before" if params.get("decision_at") == [plan["timeline"]["before_correction"]] else "after"
                values = receipt[time]
                rows = [{"id": receipt["id"], "revision": values["revision"], "data": {
                    "objective": values["objective"], "product_ref": {"record_id": receipt["product_id"]},
                    "initiative_ref": {"record_id": expected["initiative_id"]},
                    "work_links": [{"work_ref": {"record_id": work_id, "project_id": project_id,
                                                 "tenant_id": binding["tenant_id"]}}
                                   for work_id in values["work_ids"]]}}]
                if faults.get("duplicate") and project_id == first["project_id"] and actor == "editor":
                    rows.append(deepcopy(rows[0]))
                body = {"items": rows}
        else:
            raise AssertionError("Unexpected Product/catalog route")
        return SimpleNamespace(status=status, headers=response_headers, json=lambda: deepcopy(body))

    provider = SimpleNamespace(state=state, binding=binding, run_id=binding["run_id"],
                               base_url="https://catalog.example.invalid", frontend={"verified": True},
                               request=SimpleNamespace(get=get))
    return provider, sessions, calls, faults


def test_history_live_selector_collects_and_requires_explicit_selected_actors(monkeypatch):
    monkeypatch.delenv("PRODUCT_CATALOG_LIVE_ENABLED", raising=False)
    selected = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", SELECTOR],
                              cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert selected.returncode == 0, selected.stdout + selected.stderr
    assert selected.stdout.splitlines().count(SELECTOR) == 1
    environment = dict(os.environ, PRODUCT_CATALOG_LIVE_ENABLED="1")
    environment.pop("PRODUCT_EXPECTED_REVISION", None)
    gated = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short", SELECTOR],
                           cwd=ROOT, capture_output=True, text=True, timeout=30, env=environment)
    assert gated.returncode != 0 and "requires PRODUCT_EXPECTED_REVISION" in gated.stdout


def test_history_live_readback_checks_three_actors_and_independent_as_of_results(scenario, monkeypatch):
    provider, sessions, calls, _ = fixture_run(scenario)
    monkeypatch.setattr("tests.e2e.product_onboarding.product_catalog_live_history.check_served_assets",
                        lambda *args: None)
    native_calls = []

    def native_reader(product, editor):
        native_calls.append((product["project_id"], editor["access_token"]))
        return [{"id": product["work_id"], "project_id": product["project_id"]}]

    result = verify_live_history(provider, sessions, provider.binding["source_revision"], native_reader)
    assert result["denials_verified"] is result["native_work_verified"] is True
    assert native_calls == [("project-1", "synthetic-editor"), ("project-2", "synthetic-editor")]
    assert result["contributions"] == {"project-1": "first-contribution", "project-2": "second-contribution"}
    assert [role for path, role, _ in calls if path == "/api/auth/me"] == ["editor", "authorized", "revoked"]
    assert [role for path, role, _ in calls if path.endswith("/contributions")].count("editor") == 4
    revoked = [(path, params) for path, role, params in calls if path.endswith("/contributions") and role == "revoked"]
    assert revoked == [("/api/v1/catalog/projects/project-1/contributions", {
        "effective_at": [scenario[1]["timeline"]["clock"]],
        "decision_at": [scenario[1]["timeline"]["clock"]]})]


@pytest.mark.parametrize("fault", ["wrong_actor", "leaked_count", "leaked_detail", "duplicate", "stale_expectations",
                                  "reused_session", "foreign_work_scope", "wrong_initiative", "missing_native",
                                  "duplicated_native"])
def test_history_live_readback_fails_closed_on_scope_or_revision_errors(scenario, monkeypatch, fault):
    provider, sessions, _, faults = fixture_run(scenario)
    monkeypatch.setattr("tests.e2e.product_onboarding.product_catalog_live_history.check_served_assets",
                        lambda *args: None)
    if fault == "wrong_actor":
        faults["actor"] = "authorized"
    elif fault == "leaked_count":
        faults["leaked_count"] = True
    elif fault == "leaked_detail":
        faults["leaked_detail"] = True
    elif fault == "duplicate":
        faults["duplicate"] = True
    elif fault == "stale_expectations":
        provider.state.document["catalog_history"]["expected"]["contributions"]["project-1"]["before"]["revision"] = "99"
    elif fault == "foreign_work_scope":
        provider.state.document["catalog_history"]["expected"]["contributions"]["project-1"]["before"]["work_ids"] = ["foreign"]
    elif fault == "wrong_initiative":
        provider.state.document["catalog_history"]["expected"]["initiative_id"] = "wrong-initiative"
    else:
        sessions["revoked"] = sessions["authorized"]
    def native_reader(product, editor):
        records = [{"id": product["work_id"], "project_id": product["project_id"]}]
        return records * (2 if fault == "duplicated_native" else 1)

    with pytest.raises((AssertionError, ValueError)):
        verify_live_history(provider, sessions, provider.binding["source_revision"],
                            None if fault == "missing_native" else native_reader)


def test_history_expectations_are_owned_and_cannot_change_after_recorded_attempts(scenario):
    provider, _, _, _ = fixture_run(scenario)
    state = provider.state
    expected = deepcopy(state.document["catalog_history"]["expected"])
    assert record_history_expectations(state, expected) == expected
    with pytest.raises(ValueError, match="cannot change"):
        record_history_expectations(state, {**expected, "initiative_id": "other"})
    del state.document["catalog_history"]["expected"]
    state.document["catalog_history"]["attempts"]["existing"] = {"result": "interrupted"}
    with pytest.raises(ValueError, match="precede verification"):
        record_history_expectations(state, expected)


@pytest.fixture
def scenario():
    return scenario_fixture.__wrapped__()
