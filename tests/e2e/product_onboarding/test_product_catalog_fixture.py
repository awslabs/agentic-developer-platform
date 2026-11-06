"""Offline ownership, selector and recovery checks for SCN-01 catalog fixtures."""

import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from tests.e2e.new_ui.product_fixture_provider import BrowserFixtureProvider
from tests.e2e.new_ui.product_fixture_state import BrowserRunState
from tests.e2e.new_ui.test_product_catalog import membership_rows
from tests.e2e.product_onboarding.product_catalog_fixture import (
    MEMBERSHIP_SELECTORS,
    begin_membership_fixture,
    load_membership_fixture,
    pinned_membership_scenario,
    publish_membership_fixture,
    record_membership_cleanup,
    record_membership_receipts,
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


def test_membership_scenario_pins_only_reviewed_collectable_surfaces():
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    requirements = subprocess.check_output([
        "git", "log", "-1", "--format=%H", "--", "docs/product-onboarding/delivery/verification-contract.md",
    ], cwd=ROOT, text=True).strip()
    plan = pinned_membership_scenario(source, requirements, "example-run", "2026-10-01T00:00:00Z")
    assert plan["status"] == "not_run" and plan["scenario_id"] == "SCN-01"
    assert plan["selectors"] == MEMBERSHIP_SELECTORS
    for selector in MEMBERSHIP_SELECTORS.values():
        result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", selector],
                                cwd=ROOT, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.splitlines().count(selector) == 1
    for wrong in ({"browser": "case:wrong"}, {"api": "case:wrong"}):
        with pytest.raises(ValueError, match="reviewed API and browser"):
            pinned_membership_scenario(source, requirements, "example-run", plan["clock"], {**MEMBERSHIP_SELECTORS, **wrong})
    for invalid in ("", source[:-1], "unknown"):
        with pytest.raises(ValueError, match="pinned source"):
            pinned_membership_scenario(invalid, requirements, "example-run", plan["clock"])
    with pytest.raises(ValueError, match="pinned source"):
        pinned_membership_scenario(source, "unknown", "example-run", plan["clock"])

    with pytest.raises(ValueError, match="UTC clock"):
        pinned_membership_scenario(source, requirements, "example-run", "2026-10-01T00:00:00-04:00")


def test_owned_catalog_manifest_retries_and_cleanup_require_both_receipts(tmp_path):
    binding = {"run_id": "example-run", "source_revision": "a" * 40,
               "tenant_id": "example-tenant", "actor_id": "example-actor"}
    plan = pinned_membership_scenario(binding["source_revision"], "b" * 40,
                                      binding["run_id"], "2026-10-01T00:00:00Z")
    product = {"product_id": "example-product", "project_id": "example-project", "product_name": "Example Product"}
    state = LocalState(binding)
    started = begin_membership_fixture(state, plan, product)
    assert state.saved == 1 and started["cleanup_pending"] is True
    assert all(len(set(item["keys"].values())) == 4 for item in started["portfolios"])
    assert begin_membership_fixture(state, plan, product) == started and state.saved == 1
    with pytest.raises(ValueError, match="original owned intent"):
        begin_membership_fixture(state, plan, {**product, "product_name": "Other Product"})

    with pytest.raises(ValueError, match="both owned records"):
        record_membership_cleanup(state, {})
    assert state.saved == 1
    path = tmp_path / "owned-catalog.json"
    with pytest.raises(ValueError, match="both owned write receipts"):
        publish_membership_fixture(state, path)
    commerce = {"id": "commerce", "revision": "1", "change_id": "portfolio-change"}
    first = {"id": "first", "revision": "1", "change_id": "membership-change"}
    record_membership_receipts(state, "Commerce", commerce, first)
    assert record_membership_receipts(state, "Commerce", commerce, first) == {
        "portfolio": commerce, "relationship": first,
    } and state.saved == 2
    with pytest.raises(ValueError, match="both owned write receipts"):
        publish_membership_fixture(state, path)
    with pytest.raises(ValueError, match="recorded write receipts"):
        record_membership_receipts(state, "Commerce", commerce, {**first, "id": "changed"})
    with pytest.raises(ValueError, match="reused another owned record"):
        record_membership_receipts(state, "Shared services", {**commerce, "change_id": "other"},
                                   {"id": "second", "revision": "1", "change_id": "other-membership"})
    record_membership_receipts(state, "Shared services",
                               {"id": "shared", "revision": "1", "change_id": "second-portfolio"},
                               {"id": "second", "revision": "1", "change_id": "second-membership"})
    assert publish_membership_fixture(state, path) == str(path)
    fixture = load_membership_fixture(path, binding, plan["source_revision"])
    assert {row["relationship_id"] for row in fixture["portfolios"]} == {"first", "second"}
    with pytest.raises(FileExistsError):
        publish_membership_fixture(state, path)
    with pytest.raises(ValueError, match="independent absence"):
        record_membership_cleanup(state, {"Commerce": {"portfolio_status": "retired"}})
    expected = {name: {"closed_revision": "2", "retired_revision": "2", "current_memberships": [],
                       "portfolio_status": "retired"} for name in ("Commerce", "Shared services")}
    record_membership_cleanup(state, expected)
    assert state.document["catalog"]["cleanup_pending"] is False
    assert state.document["catalog"]["cleanup_observations"] == expected
    assert len(state.document["catalog"]["portfolios"]) == 2
    assert record_membership_cleanup(state, expected) == expected
    assert state.saved == 4
    with pytest.raises(ValueError, match="after catalog cleanup"):
        record_membership_receipts(state, "Commerce", commerce, first)
    with pytest.raises(ValueError, match="both owned write receipts"):
        publish_membership_fixture(state, tmp_path / "cleaned-catalog.json")
    with pytest.raises(ValueError, match="recorded cleanup"):
        record_membership_cleanup(state, {"Commerce": expected["Commerce"]})


def test_browser_state_refuses_release_with_unfinished_catalog_cleanup():
    state = object.__new__(BrowserRunState)
    state.document = {"cases": {}, "catalog": {"cleanup_pending": True}}
    with pytest.raises(ValueError, match="catalog fixture cleanup"):
        state.release()


def test_product_retirement_cannot_precede_catalog_cleanup():
    state = LocalState({"run_id": "example-run"})
    state.document["catalog"] = {"cleanup_pending": True}
    provider = object.__new__(BrowserFixtureProvider)
    provider.state = state
    with pytest.raises(ValueError, match="before Product retirement"):
        provider.cleanup("unrelated-product", None)
    assert state.saved == 0


def test_catalog_browser_readback_rejects_unbounded_pagination():
    class Response:
        status = 200

        def json(self):
            return {"items": [], "next_cursor": "next"}

    class Request:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            return Response()

    request = Request()
    with pytest.raises(AssertionError, match="pagination exceeded"):
        membership_rows(request, "https://example.test", {}, "owned-portfolio")
    assert request.calls == 10


def test_catalog_browser_reopens_owned_run_without_retiring_pending_catalog(tmp_path, monkeypatch):
    import inspect

    from tests.e2e.new_ui import conftest as browser_fixtures
    from tests.e2e.new_ui import product_fixture_provider as provider_module
    from tests.e2e.new_ui.test_product_catalog import test_catalog_membership

    assert "product_catalog_provider" in inspect.signature(test_catalog_membership).parameters
    fixture_path = tmp_path / "catalog.json"
    fixture_path.write_text("{}")
    fixture_path.chmod(0o600)
    for name, value in (
        ("PRODUCT_CATALOG_LIVE_ENABLED", "1"),
        ("PRODUCT_RECOVERY_RUN_ID", "product-12-1"),
        ("PRODUCT_CATALOG_FIXTURE_PATH", str(fixture_path)),
        ("PRODUCT_EXPECTED_REVISION", "a" * 40),
    ):
        monkeypatch.setenv(name, value)

    calls = []
    class Context:
        request = object()

        def close(self):
            calls.append("closed")

    class Browser:
        def new_context(self, **options):
            assert options == {"service_workers": "block"}
            calls.append("opened")
            return Context()

    provider = object()
    def build_provider(environment, base_url, request, control, **options):
        assert environment["PRODUCT_RECOVERY_RUN_ID"] == "product-12-1"
        assert (base_url, request, control, options) == (
            "https://example.test", Context.request, "control",
            {"recovery_run_id": "product-12-1"},
        )
        calls.append("recovered")
        return provider

    monkeypatch.setattr(provider_module, "build_provider", build_provider)
    fixture = browser_fixtures.product_catalog_provider.__wrapped__
    opened = fixture(Browser(), "control", "https://example.test")
    assert next(opened) is provider
    with pytest.raises(StopIteration):
        next(opened)
    assert calls == ["opened", "recovered", "closed"]

    monkeypatch.delenv("PRODUCT_RECOVERY_RUN_ID")
    with pytest.raises(pytest.fail.Exception, match="run-owned live fixture"):
        next(fixture(Browser(), "control", "https://example.test"))
    assert calls == ["opened", "recovered", "closed"]

    monkeypatch.setenv("PRODUCT_RECOVERY_RUN_ID", "product-12-1")
    link = tmp_path / "alias.json"
    link.symlink_to(fixture_path)
    monkeypatch.setenv("PRODUCT_CATALOG_FIXTURE_PATH", str(link))
    with pytest.raises(pytest.fail.Exception, match="protected membership fixture"):
        next(fixture(Browser(), "control", "https://example.test"))
    assert calls == ["opened", "recovered", "closed"]

    fixture_path.chmod(0o644)
    monkeypatch.setenv("PRODUCT_CATALOG_FIXTURE_PATH", str(fixture_path))
    with pytest.raises(pytest.fail.Exception, match="protected membership fixture"):
        next(fixture(Browser(), "control", "https://example.test"))
    assert calls == ["opened", "recovered", "closed"]
