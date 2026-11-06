"""Fixture-backed selector guards; no deployed V10 evidence is produced here."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.e2e.cli_uplift.product_guidance import REQUIRED_CHECKS
from tests.e2e.product_onboarding import test_v10

SOURCE = "a" * 40
REQUIREMENTS = "b" * 40


def selected():
    cleanups = []
    journeys = {}
    hashes = {"adp": "c" * 64, "adp-onboard.py": "d" * 64}
    for starting_point in REQUIRED_CHECKS:
        run_id = "owned-" + starting_point
        fixture = {"owned_fixture": True, "source_revision": SOURCE,
                   "run_id": run_id, "frontend_release": {"verified": True}}
        journeys[starting_point] = {
            "fixture": fixture, "deployment_source_revision": SOURCE,
            "requirements_revision": REQUIREMENTS,
            "checksums": hashes,
            "provider": SimpleNamespace(request=object()), "base_url": "https://example.invalid",
            "binary": "adp", "record": object(), "create_ui_session": lambda: None,
            "observe_provider": lambda: {},
            "cleanup": lambda run_id=run_id: cleanups.append(run_id) or {
                "run_id": run_id, "source_revision": SOURCE, "status": "verified",
                "owned_absent": True, "shared_unchanged": True, "receipt_ref": "private:cleanup",
            },
        }
    return {"journeys": journeys, "expected_cli_hashes": hashes}, cleanups


def test_full_selector_checks_every_starting_point_and_cleanup(monkeypatch):
    run, cleanups = selected()
    checked = []
    calls = []
    monkeypatch.setattr(test_v10, "check_served_assets", lambda *args: checked.append(args[-1]))
    monkeypatch.setattr(test_v10, "run_full_journey", lambda _journey, point, source, requirements:
                        calls.append((point, source, requirements)) or {"starting_point": point})
    results = test_v10.run_cli_full_journeys(run, SOURCE, REQUIREMENTS)
    assert set(results) == set(REQUIRED_CHECKS)
    assert all(result["cleanup"]["status"] == "verified" for result in results.values())
    assert [point for point, _source, _requirements in calls] == list(REQUIRED_CHECKS)
    assert all(source == SOURCE and requirements == REQUIREMENTS
               for _point, source, requirements in calls)
    assert checked == [SOURCE] * len(REQUIRED_CHECKS)
    assert len(cleanups) == len(set(cleanups)) == len(REQUIRED_CHECKS)


@pytest.mark.parametrize("fault", ("missing_journey", "duplicate_fixture", "wrong_source",
                                    "missing_hash", "wrong_hash_pin", "no_cleanup", "unowned"))
def test_full_selector_rejects_missing_or_unpinned_fixtures(monkeypatch, fault):
    run, cleanups = selected()
    journeys = run["journeys"]
    first, second = list(journeys)[:2]
    if fault == "missing_journey":
        journeys.pop(first)
    elif fault == "duplicate_fixture":
        journeys[second]["fixture"]["run_id"] = journeys[first]["fixture"]["run_id"]
    elif fault == "wrong_source":
        journeys[first]["deployment_source_revision"] = "f" * 40
    elif fault == "missing_hash":
        journeys[first]["checksums"].pop("adp-onboard.py")
    elif fault == "wrong_hash_pin":
        run["expected_cli_hashes"] = {"adp": "e" * 64, "adp-onboard.py": "d" * 64}
    elif fault == "no_cleanup":
        journeys[first]["cleanup"] = None
    else:
        journeys[first]["fixture"]["owned_fixture"] = False
    monkeypatch.setattr(test_v10, "run_full_journey", lambda *_args: pytest.fail("Ran unpinned fixture"))
    with pytest.raises(ValueError, match="V10"):
        test_v10.run_cli_full_journeys(run, SOURCE, REQUIREMENTS)
    assert cleanups == []


@pytest.mark.parametrize("fault", ("journey", "cleanup", "cleanup_exception"))
def test_full_selector_checks_cleanup_even_after_failed_journey(monkeypatch, fault):
    run, cleanups = selected()
    first = next(iter(REQUIRED_CHECKS))
    monkeypatch.setattr(test_v10, "check_served_assets", lambda *_args: None)
    if fault == "journey":
        monkeypatch.setattr(test_v10, "run_full_journey", lambda *_args: pytest.fail("Independent readback failed"))
        with pytest.raises(pytest.fail.Exception, match="readback failed"):
            test_v10.run_cli_full_journeys(run, SOURCE, REQUIREMENTS)
    else:
        if fault == "cleanup_exception":
            def failed_cleanup():
                cleanups.append("failed")
                raise RuntimeError("Private cleanup unavailable")

            run["journeys"][first]["cleanup"] = failed_cleanup
        else:
            run["journeys"][first]["cleanup"] = lambda: cleanups.append("failed") or {"status": "failed"}
        monkeypatch.setattr(test_v10, "run_full_journey", lambda *_args: {"revision": 2})
        with pytest.raises(AssertionError, match="cleanup"):
            test_v10.run_cli_full_journeys(run, SOURCE, REQUIREMENTS)
    assert len(cleanups) == len(REQUIRED_CHECKS)


def test_live_selector_is_collectable_and_requires_authorized_fixture(monkeypatch):
    selector = "tests/e2e/product_onboarding/test_v10.py::live_cli_full_journeys"
    result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", selector],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines().count(selector) == 1
    monkeypatch.setenv("PRODUCT_V10_LIVE_ENABLED", "1")
    monkeypatch.setenv("PRODUCT_V10_SOURCE_REVISION", SOURCE)
    monkeypatch.setenv("PRODUCT_V10_REQUIREMENTS_REVISION", REQUIREMENTS)
    request = SimpleNamespace(getfixturevalue=lambda _: pytest.fail("No authorized V10 fixture"))
    with pytest.raises(pytest.fail.Exception, match="No authorized V10 fixture"):
        test_v10.live_cli_full_journeys(request)
