"""Agreement with #5043's published lease contract.

Issue #5527 (w6-04), EPIC #4910, Wave 6.

`harness_jobs.leases` duplicates the lease ceiling, the default duration and the fencing
rule that `superplane_contracts.leases` publishes. Duplicated rather than imported for
the reason `test_contract_agreement.py` gives for the permission string: this package is
installed independently and must not require the contracts package on `sys.path`.

Duplication without a test is drift waiting to happen, and here drift is not cosmetic.
If the contract tightened `MAX_LEASE_DURATION` and this package kept the old value, this
package would grant leases the platform considers unbounded. If the two disagreed about
which direction `is_fenced_out` compares, the executor would fence out the *current*
holder and admit every stale one -- a fence installed backwards, which is worse than no
fence because it looks present.

Skips when the contracts package is not importable, which is honest: a skip says "not
checked in this run", whereas a silent pass would say "checked and agreed".
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harness_jobs import leases as ours

_AWARE = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

_REPO = Path(__file__).resolve().parents[4]
_CONTRACTS = _REPO / "modules" / "domain-apps" / "superplane" / "contracts"

if _CONTRACTS.is_dir() and str(_CONTRACTS) not in sys.path:
    sys.path.insert(0, str(_CONTRACTS))

theirs = pytest.importorskip(
    "superplane_contracts.leases",
    reason=(
        "superplane-contracts is not importable here; the agreement between the two "
        "spellings of the lease contract is therefore not checked in this run rather "
        "than assumed"
    ),
)
ContractViolation = pytest.importorskip(
    "superplane_contracts.health", reason="see above"
).ContractViolation


def test_the_lease_ceiling_is_the_same_duration():
    """The ceiling is what makes expiry a bound rather than a suggestion.

    A longer local ceiling means this package can grant a lease the platform would have
    refused, and the operation it covers is unrecoverable for that whole window.
    """
    assert ours.MAX_LEASE_DURATION == theirs.MAX_LEASE_DURATION


def test_the_default_duration_is_the_same():
    assert ours.DEFAULT_LEASE_DURATION == theirs.DEFAULT_LEASE_DURATION


def test_the_fencing_rule_agrees_across_the_whole_comparison():
    """Every ordering, not a sampled one, because the failure is direction-shaped.

    A reversed comparison agrees with the contract on the equal case and disagrees on
    both unequal ones, so checking only `is_fenced_out(2, 2)` would pass against an
    implementation that admits every stale worker.
    """
    for observed in range(4):
        for highest in range(4):
            assert ours.is_fenced_out(observed, highest) == theirs.is_fenced_out(
                observed, highest
            ), f"disagreement at observed={observed}, highest={highest}"


def test_the_contract_still_requires_a_positive_token():
    """Our `ExecutionLease` enforces `fence_token >= 1`; it must not be alone in that.

    Zero is the stored "never granted" sentinel. If the contract came to allow a lease
    carrying zero, our refusal would start rejecting valid leases -- so this fails on
    the contract changing, which is the point of pinning it.
    """
    with pytest.raises(ContractViolation):
        theirs.Lease(scope="op-1", holder="worker-1", fence_token=0, expires_at=_AWARE)


def test_the_contract_still_requires_an_aware_expiry():
    """Ours refuses naive datetimes too. Both refuse for the same reason: a naive
    expiry cannot be compared against a clock without guessing a zone, and a wrong guess
    is a lease that expires hours early or late -- either an operation unrecoverable for
    hours, or a fence that opens immediately.
    """
    from datetime import datetime

    with pytest.raises(ContractViolation):
        theirs.Lease(
            scope="op-1",
            holder="worker-1",
            fence_token=1,
            expires_at=datetime(2026, 9, 20, 12, 0),
        )


def test_our_release_is_at_least_as_strict_as_the_contracts():
    """Ownership on release: every case the contract refuses, ours must refuse.

    The contract authorizes release on holder identity alone. Our `release` matches on
    holder *and* fence token, so it is strictly stronger -- which is the permitted
    direction. This test pins that direction: it fails if the contract ever became the
    stricter of the two, which would mean our SQL predicate had a hole.

    Asserted as a table rather than by calling the function from SQL, which is not
    possible; `test_leases_postgres.py` asserts the SQL side of the same three cases
    (`test_a_stale_holder_cannot_release_the_successors_lease` and
    `test_a_holder_with_the_right_token_but_wrong_name_is_refused`).
    """
    lease = theirs.Lease(
        scope="op-1", holder="worker-1", fence_token=3, expires_at=_AWARE
    )
    assert theirs.authorize_release(lease, "worker-1").granted is True
    assert theirs.authorize_release(lease, "worker-2").granted is False
