"""Live-only Product API scenarios; fixture tests are in test_product_live_wiring.py."""

import os

import pytest

from tests.e2e.product_onboarding import live


@pytest.mark.live_only
def api_product_lifecycle():
    live.main([
        "--cases", "api_product_lifecycle",
        "--expected-revision", os.environ.get("PRODUCT_EXPECTED_REVISION", ""),
    ])


@pytest.mark.live_only
def api_denial_oracle():
    live.main([
        "--cases", "api_denial_oracle",
        "--expected-revision", os.environ.get("PRODUCT_EXPECTED_REVISION", ""),
    ])


@pytest.mark.live_only
def api_recovery_cleanup():
    live.main([
        "--cases", "api_recovery_cleanup",
        "--expected-revision", os.environ.get("PRODUCT_EXPECTED_REVISION", ""),
    ])
