"""Shared fixtures for the Superplane observation contract suites.

Issue #5043 (U8), EPIC #4910.

Importing `_contracts_path` first is what makes `superplane_contracts` importable
— see that module's docstring for why the package sits one level inside
`contracts/` and why the path setup is an import rather than inline statements.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import _contracts_path  # noqa: F401  (imported for its sys.path side effect)
import pytest
from superplane_contracts import (
    CheckResult,
    CheckStatus,
    ClusterRef,
    Observation,
    Submitter,
)

# A fixed, timezone-aware instant. Tests assert on ordering and on validation
# branches, never on "now", so a constant keeps them deterministic — and the
# contract requires aware datetimes, so a naive constant would fail construction.
OBSERVED_AT = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)

# Signing key used by the auth tests. Test-only material: it authenticates nothing
# outside this suite, and the contract takes the key as an argument precisely so no
# real key is needed to exercise the rules.
TEST_SIGNING_KEY = b"test-signing-key-not-a-credential"

# The two workspaces every scoping test uses. Named W1/W2 to match the issue's
# wording ("a submitter authenticated for W1 cannot submit about W2's clusters").
W1 = "ws-w1"
W2 = "ws-w2"


@pytest.fixture
def healthy_check() -> CheckResult:
    """A check that genuinely observed a healthy dimension."""
    return CheckResult(
        name="api_server",
        status=CheckStatus.HEALTHY,
        observed_at=OBSERVED_AT,
        detail="ok",
    )


@pytest.fixture
def w1_observation(healthy_check: CheckResult) -> Observation:
    """A well-formed fleet-health observation about a cluster in W1."""
    return Observation(
        kind="fleet_health",
        subject=ClusterRef(cluster_id="cluster-w1-a", workspace=W1),
        reported_at=OBSERVED_AT,
        reporter="platform-monitor",
        checks=(healthy_check,),
    )


@pytest.fixture
def w2_observation(healthy_check: CheckResult) -> Observation:
    """The same shape, but about a cluster in W2 — the cross-workspace subject."""
    return Observation(
        kind="fleet_health",
        subject=ClusterRef(cluster_id="cluster-w2-a", workspace=W2),
        reported_at=OBSERVED_AT,
        reporter="platform-monitor",
        checks=(healthy_check,),
    )


@pytest.fixture
def w1_submitter() -> Submitter:
    """A submitter authenticated for W1 only."""
    return Submitter(submitter_id="monitor-1", workspaces=frozenset({W1}))


def lease_duration(seconds: int) -> timedelta:
    """Small helper so lease tests read in seconds rather than timedelta noise."""
    return timedelta(seconds=seconds)
