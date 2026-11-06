"""Selector guards run offline; real deployment and admin provider remain required."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.e2e.cli_uplift.test_product_handoffs import FakeRequest, receipt
from tests.e2e.new_ui.test_product_handoffs import browser_admin_resume
from tests.e2e.product_onboarding.test_product_handoffs import Browser, owned_fixture
from tests.e2e.product_onboarding import test_v08

SOURCE = "a" * 40
REQUIREMENTS = "b" * 40


def make_run():
    fixture = owned_fixture()
    fixture.update(owned_fixture=True, source_revision=SOURCE, frontend_release={},
                   evidence_context={"deployment": {"observed": {"product_onboarding_enabled": True}}})
    request = FakeRequest(fixture)
    original_get = request.get

    def observe(url, *, headers, timeout):
        if headers.get("Authorization") == "Bearer synthetic-observer-token":
            return SimpleNamespace(status=200, json=lambda: deepcopy(request.session))
        return original_get(url, headers=headers, timeout=timeout)

    request.get = observe
    cleanups = []
    run = {"fixture": fixture, "requirements_revision": REQUIREMENTS, "deployment_source_revision": SOURCE,
           "cli_sha256": "c" * 64, "page": Browser(request), "provider": SimpleNamespace(request=request),
           "base_url": "https://example.invalid", "session_id": "session-owned", "key": "owned-handoff-key-123",
           "navigate": lambda: None, "complete_admin": lambda _: setattr(request, "finished", True),
           "receipt_observer": lambda _: receipt(fixture), "resume_cli": lambda _: deepcopy(request.session),
           "cleanup": lambda: cleanups.append(1) or {"run_id": fixture["run_id"], "source_revision": SOURCE,
                                                  "status": "verified", "owned_absent": True,
                                                  "shared_unchanged": True, "receipt_ref": "private:owned-cleanup"}}
    return run, cleanups


def test_live_selector_exercises_browser_driver_and_requires_cleanup(monkeypatch):
    run, cleanups = make_run()
    monkeypatch.setattr(test_v08, "check_served_assets", lambda *_args: None)
    assert test_v08.run_admin_handoff(run, SOURCE, REQUIREMENTS)["result"]["callback_receipt"]["sequence"] == 1
    assert cleanups == [1]


@pytest.mark.parametrize("change", [
    {"deployment_source_revision": "d" * 40}, {"cli_sha256": "missing"},
    {"requirements_revision": "d" * 40}, {"complete_admin": None},
])
def test_live_selector_rejects_unpinned_and_missing_authority(change):
    run, cleanups = make_run()
    run.update(change)
    with pytest.raises(ValueError, match="pinned deployed"):
        test_v08.run_admin_handoff(run, SOURCE, REQUIREMENTS)
    assert cleanups == []


def test_live_selector_rejects_failed_cleanup(monkeypatch):
    run, cleanups = make_run()
    monkeypatch.setattr(test_v08, "check_served_assets", lambda *_args: None)
    run["cleanup"] = lambda: {"run_id": run["fixture"]["run_id"], "status": "failed"}
    with pytest.raises(AssertionError, match="cleanup"):
        test_v08.run_admin_handoff(run, SOURCE, REQUIREMENTS)


def test_live_selector_requires_actual_fixture_when_enabled(monkeypatch):
    monkeypatch.setenv("PRODUCT_V08_LIVE_ENABLED", "1")
    monkeypatch.setenv("PRODUCT_V08_SOURCE_REVISION", SOURCE)
    monkeypatch.setenv("PRODUCT_V08_REQUIREMENTS_REVISION", REQUIREMENTS)
    request = SimpleNamespace(getfixturevalue=lambda _: pytest.fail("No authorized deployed fixture"))
    with pytest.raises(pytest.fail.Exception, match="No authorized deployed fixture"):
        test_v08.live_admin_handoff(request)


def test_live_selector_is_collectable():
    import subprocess
    import sys

    selector = "tests/e2e/product_onboarding/test_v08.py::live_admin_handoff"
    result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", selector],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines().count(selector) == 1
