"""Offline cleanup selector guards; provider calls are owned fixture doubles."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.e2e.product_onboarding import test_v08
from tests.e2e.product_onboarding.test_product_handoffs import adoption_case
from tests.e2e.product_onboarding.test_v08_handoff_selector import REQUIREMENTS, SOURCE


def cleanup_run(monkeypatch):
    fixture, request, provider, snapshots, history, observe, delete_owned = adoption_case()
    fixture.update(owned_fixture=True, source_revision=SOURCE, frontend_release={},
                   evidence_context={"deployment": {"observed": {"product_onboarding_enabled": True}}})
    recovered = []
    page = SimpleNamespace(get_by_role=lambda role, name: SimpleNamespace(
        wait_for=lambda *, timeout: recovered.append((role, name, timeout))))

    def durable_recovery():
        recovered.append("cleanup")
        if provider["owned"] is not None:
            provider["owned"] = None
            history.append({"receipt_ref": "private:recovery", "native_id": "91"})
        return {"run_id": fixture["run_id"], "source_revision": SOURCE, "owned_absent": True,
                "existing_unchanged": provider["existing"]["protected"] is True,
                "history_retained": len(history) >= 2, "receipt_ref": "private:owned-recovery"}

    run = {"fixture": fixture, "page": page, "provider": SimpleNamespace(request=request),
           "base_url": "https://example.invalid", "requirements_revision": REQUIREMENTS,
           "deployment_source_revision": SOURCE, "cli_sha256": "c" * 64,
           "navigate": lambda: recovered.append("navigate"), "existing_product_name": "Example Product",
           "cli_read_product": lambda: {"tenant_id": fixture["tenant_id"], "product_id": fixture["product_id"]},
           "existing_observer": lambda: observe("existing"), "owned_observer": lambda: observe("owned"),
           "delete_owned": delete_owned, "history_reader": lambda: list(history),
           "recover_cleanup": durable_recovery}
    monkeypatch.setattr(test_v08, "check_served_assets", lambda *_args: None)
    return run, provider, recovered, snapshots, history


def test_cleanup_selector_checks_live_shaped_cli_ui_and_receipts(monkeypatch):
    run, provider, recovered, snapshots, history = cleanup_run(monkeypatch)
    result = test_v08.run_provider_cleanup(run, SOURCE, REQUIREMENTS)
    assert result["result"]["owned_after"]["repository"] is None
    assert result["result"]["existing_before"]["repository"] == result["result"]["existing_after"]["repository"]
    assert result["cli_before"] == result["cli_after"]
    assert recovered == ["navigate", ("heading", "Example Product", 30_000), "cleanup"]
    assert provider["owned"] is None and len(history) == 2 and snapshots


@pytest.mark.parametrize("fault", ["source", "cli", "manifest", "history", "cleanup"])
def test_cleanup_selector_refuses_unverified_pin_readback_or_recovery(monkeypatch, fault):
    run, provider, recovered, _snapshots, history = cleanup_run(monkeypatch)
    if fault == "source":
        run["deployment_source_revision"] = "d" * 40
    elif fault == "cli":
        run["cli_read_product"] = lambda: {"product_id": "other", "tenant_id": run["fixture"]["tenant_id"]}
    elif fault == "manifest":
        run["fixture"]["ownership_manifest"]["run_id"] = "other-run"
    elif fault == "history":
        run["history_reader"] = list
    else:
        run["recover_cleanup"] = lambda: {"run_id": run["fixture"]["run_id"], "owned_absent": False}
    with pytest.raises((AssertionError, ValueError), match="pinned deployed|Installed CLI|manifest|history|cleanup"):
        test_v08.run_provider_cleanup(run, SOURCE, REQUIREMENTS)
    assert ("cleanup" in recovered) is (fault not in ("source", "cleanup"))
    if fault == "cleanup":
        assert provider["owned"] is None and len(history) == 2


def test_cleanup_selector_is_collectable_and_requires_authorized_fixture(monkeypatch):
    selector = "tests/e2e/product_onboarding/test_v08.py::live_provider_cleanup"
    collected = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", selector],
                               capture_output=True, text=True, timeout=30, check=False)
    assert collected.returncode == 0, collected.stdout + collected.stderr
    assert collected.stdout.splitlines().count(selector) == 1
    monkeypatch.setenv("PRODUCT_V08_LIVE_ENABLED", "1")
    monkeypatch.setenv("PRODUCT_V08_SOURCE_REVISION", SOURCE)
    monkeypatch.setenv("PRODUCT_V08_REQUIREMENTS_REVISION", REQUIREMENTS)
    with pytest.raises(pytest.fail.Exception, match="No authorized cleanup fixture"):
        test_v08.live_provider_cleanup(SimpleNamespace(
            getfixturevalue=lambda _: pytest.fail("No authorized cleanup fixture")))
