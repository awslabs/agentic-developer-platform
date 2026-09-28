"""U12 R17 baseline and serving acceptance. Run explicitly per README.md.

One capture, two criteria. #5067 tracks the baseline (U12-L1) and serving
(U12-L2) separately because a batch result cannot satisfy serving, so both are
asserted individually and each names its own outstanding checks. They share a
single capture rather than one call each: the evidence file is written exactly
once to a new path, and a second publish of the same run would fail on the
existing file rather than produce a second record of the same observation.

With inputs, target selection or authority absent this fails ``BLOCKED`` before
any observation. It never skips, and no record is published unless both criteria
are satisfied by real observation.
"""

import os

import pytest

from superplane_acceptance.live_baseline import run_live


@pytest.mark.superplane_live
def test_baseline_and_serving_criteria_are_observed():
    """U12-L1: provisioning, EKS node readiness, scheduling, status/logs,
    cancellation, controller lifecycle, cost and provider-verified cleanup.
    U12-L2: serving reachability, authentication and owning-controller
    lifecycle, or observed inventory showing serving absent from the baseline."""
    report = run_live(os.environ)
    baseline, serving = (report["criteria"][name] for name in ("U12-L1", "U12-L2"))
    assert baseline["satisfied"], (
        f"Outstanding baseline checks: {baseline['outstanding_checks']}"
    )
    assert serving["satisfied"], (
        f"Outstanding serving checks: {serving['outstanding_checks']}"
    )
