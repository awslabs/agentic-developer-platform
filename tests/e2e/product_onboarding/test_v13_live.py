"""Deployed V13 CLI checkpoint; local fixtures never count as live evidence."""

import os
import shutil

import pytest

from tests.e2e.product_onboarding.product_insights_cli_live import protected_input, verify_cli_checkpoint


@pytest.mark.live_only
def live_insights_cli_checkpoint():
    if os.environ.get("PRODUCT_V13_LIVE_ENABLED") != "1":
        pytest.skip("V13 requires a selected serving release and protected owned fixtures")
    required = ("PRODUCT_V13_FIXTURE_PATH", "PRODUCT_V13_RELEASE_MANIFEST_PATH",
                "PRODUCT_EXPECTED_REVISION", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")
    if any(not os.environ.get(name) for name in required):
        pytest.fail("V13 needs pinned run, release manifest and protected fixture paths")
    release = protected_input(os.environ["PRODUCT_V13_RELEASE_MANIFEST_PATH"])
    fixture = protected_input(os.environ["PRODUCT_V13_FIXTURE_PATH"])
    expected_run = f"product-{int(os.environ['GITHUB_RUN_ID'])}-{int(os.environ['GITHUB_RUN_ATTEMPT'])}"
    if (release.get("source_revision") != os.environ["PRODUCT_EXPECTED_REVISION"]
            or fixture.get("run_id") != expected_run):
        pytest.fail("V13 fixture or installed CLI differs from the selected run and release")
    binary = shutil.which("adp")
    if not binary:
        pytest.fail("V13 needs the installed adp CLI")
    assert verify_cli_checkpoint(fixture, release, binary)
