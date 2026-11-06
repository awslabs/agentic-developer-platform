"""Protected real-client Security scenarios; missing live authority is not acceptance."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "live"))
from fixtures import load_live_fixture
from scenarios import clients_and_setup


def test_clients_and_setup():
    path = os.environ.get("ADP_L5_SECURITY_LIVE_CONFIG")
    if not path:
        pytest.skip("deployed-target: protected owner-authorized configuration is required")
    if os.environ.get("ADP_L5_SECURITY_LIVE_ENABLED") != "I_ACCEPT_PAID_TESTS":
        pytest.fail("explicit protected paid-test enablement is required before live setup")
    fixture = load_live_fixture(path)
    from security_manual_setup import ManualSetupDriver, preflight

    drivers = [ManualSetupDriver(case, fixture, path) for case in fixture["cases"]]
    try:
        preflight(drivers)
        for driver in drivers:
            driver.run_setup(fixture["limits"])
    finally:
        for driver in drivers:
            driver.close()


def test_real_security_assessments():
    path = os.environ.get("ADP_L5_SECURITY_LIVE_CONFIG")
    if not path:
        pytest.skip("deployed-target: protected provider and source fixture required")
    if os.environ.get("ADP_L5_SECURITY_LIVE_ENABLED") != "I_ACCEPT_PAID_TESTS":
        pytest.fail("explicit protected paid-test enablement required before assessment")
    from assessment_live import LiveAssessmentDriver
    from assessment_scenarios import real_security_assessments

    fixture = load_live_fixture(path)
    for selected in fixture["cases"]:
        case = {**selected, "deadline_seconds": fixture["limits"]["deadline_seconds"],
                "max_attempts": fixture["limits"]["max_attempts"]}
        driver = LiveAssessmentDriver(case)
        try:
            real_security_assessments(case, driver)
        finally:
            driver.close()


def test_isolation_recovery_cleanup():
    path = os.environ.get("ADP_L5_SECURITY_LIVE_CONFIG")
    if not path:
        pytest.skip("deployed-target: protected fault and independent readback fixtures required")
    if os.environ.get("ADP_L5_SECURITY_LIVE_ENABLED") != "I_ACCEPT_PAID_TESTS":
        pytest.fail("explicit protected paid-test enablement required before recovery faults")
    from recovery_live import LiveRecoveryDriver
    from recovery_scenarios import isolation_recovery_cleanup

    fixture = load_live_fixture(path)
    selected, foreign = fixture["cases"]
    case = {**selected, "deadline_seconds": fixture["limits"]["deadline_seconds"],
            "max_attempts": fixture["limits"]["max_attempts"]}
    driver = LiveRecoveryDriver(case, foreign)
    try:
        isolation_recovery_cleanup(case, foreign, driver)
    finally:
        driver.close()


def test_context_reuse():
    path = os.environ.get("ADP_L5_SECURITY_LIVE_CONFIG")
    if not path:
        pytest.skip("deployed-target: protected Context and later agent fixtures required")
    if os.environ.get("ADP_L5_SECURITY_LIVE_ENABLED") != "I_ACCEPT_PAID_TESTS":
        pytest.fail("explicit protected paid-test enablement required before Context replay")
    from context_live import LiveContextDriver
    from context_scenarios import context_reuse

    fixture = load_live_fixture(path)
    selected, foreign = fixture["cases"]
    case = {**selected, "deadline_seconds": fixture["limits"]["deadline_seconds"],
            "max_attempts": fixture["limits"]["max_attempts"]}
    driver = LiveContextDriver(case, foreign)
    try:
        context_reuse(case, foreign, driver)
    finally:
        driver.close()
