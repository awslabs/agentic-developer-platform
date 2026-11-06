"""Offline selector guards do not stand in for a deployed provider run."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.e2e.product_onboarding import test_v08
from tests.e2e.product_onboarding.test_v08_handoff_selector import (
    REQUIREMENTS,
    SOURCE,
    make_run,
)


def repository_run(monkeypatch):
    run, cleanups = make_run()
    receipt = {"native_id": "91", "coverage": "complete"}
    run["plan"] = {"name": "run-owned-project"}
    run["keys"] = {"reconcile": "owned-reconcile-key-123"}
    run["provider_observer"] = lambda: {"repositories": [{"native_id": "91"}],
                                        "observation_ref": "private:provider-observer"}
    run["resume_cli"] = lambda _: {"id": run["session_id"], "tenant_id": run["fixture"]["tenant_id"]}
    observed = []
    original_get = run["provider"].request.get

    def read(url, *, headers, timeout):
        if url.endswith("/operations/repo-operation") and headers == {
            "Authorization": "Bearer synthetic-observer-token"} and timeout == 30_000:
            observed.append(url)
            return SimpleNamespace(status=200, json=lambda: {"repository": receipt})
        return original_get(url, headers=headers, timeout=timeout)

    run["provider"].request.get = read
    monkeypatch.setattr(test_v08, "check_served_assets", lambda *_args: None)
    monkeypatch.setattr("tests.e2e.new_ui.test_product_handoffs.browser_repository_recovery",
                        lambda *_args, **_kwargs: {"operation": {"id": "repo-operation", "repository": receipt}})
    return run, cleanups, observed


def test_repository_selector_checks_cli_observer_and_cleanup(monkeypatch):
    run, cleanups, observed = repository_run(monkeypatch)
    result = test_v08.run_repository_recovery(run, SOURCE, REQUIREMENTS)
    assert result["result"]["operation"]["repository"]["native_id"] == "91"
    assert cleanups == [1] and len(observed) == 1


@pytest.mark.parametrize("fault", ["source", "cli_checksum", "no_observer", "no_cleanup", "stale_cli"])
def test_repository_selector_rejects_missing_pins_or_readback(monkeypatch, fault):
    run, cleanups, _observed = repository_run(monkeypatch)
    if fault == "source":
        run["deployment_source_revision"] = "d" * 40
    if fault == "cli_checksum":
        run["cli_sha256"] = "other"
    if fault == "no_observer":
        run["provider"].request.get = lambda *_args, **_kwargs: SimpleNamespace(status=404)
    if fault == "no_cleanup":
        run["cleanup"] = lambda: {"status": "failed"}
    if fault == "stale_cli":
        run["resume_cli"] = lambda _: {"id": "other-session", "tenant_id": run["fixture"]["tenant_id"]}
    with pytest.raises((ValueError, AssertionError), match="pinned deployed|readback|cleanup"):
        test_v08.run_repository_recovery(run, SOURCE, REQUIREMENTS)
    assert cleanups == ([] if fault == "source" or fault == "cli_checksum" or fault == "no_cleanup" else [1])


def test_repository_selector_collects_and_requires_real_fixture(monkeypatch):
    selector = "tests/e2e/product_onboarding/test_v08.py::live_repository_recovery"
    collection = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", selector],
                                capture_output=True, text=True, timeout=30, check=False)
    assert collection.returncode == 0, collection.stdout + collection.stderr
    assert collection.stdout.splitlines().count(selector) == 1
    monkeypatch.setenv("PRODUCT_V08_LIVE_ENABLED", "1")
    monkeypatch.setenv("PRODUCT_V08_SOURCE_REVISION", SOURCE)
    monkeypatch.setenv("PRODUCT_V08_REQUIREMENTS_REVISION", REQUIREMENTS)
    request = SimpleNamespace(getfixturevalue=lambda _: pytest.fail("No owned provider fixture"))
    with pytest.raises(pytest.fail.Exception, match="No owned provider fixture"):
        test_v08.live_repository_recovery(request)
