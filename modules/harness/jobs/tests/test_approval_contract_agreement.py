"""Agreement between this package's approval vocabulary and its upstream sources.

Issue #5526 (w6-03), EPIC #4910, Wave 6.

## Why the vocabulary is duplicated at all

`harness_jobs.approval` re-spells two things that already exist in this repository:

* the four HITL results, from `contracts/hitl-ticket/v1/models.py` (#4178);
* `APPROVAL_PERMISSION`, from `superplane_auth.policy.Permission.ADMINISTER`.

Neither is imported, for the reason `pyproject.toml` states: this package declares
`dependencies = []` and is installed beside the API server without a dependency
conflict. The HITL contract is a pydantic module, so importing it would put pydantic in
this package's runtime closure; `superplane_auth` is the domain app's, and importing it
would make the harness depend on the consumer it is shared *by*.

Duplication without a test is drift waiting to happen -- `test_contract_agreement.py`
makes the same argument for #5525's constants. These tests are what notices. Every
assertion is driven from the upstream module's *own* members rather than a hand-written
list, so a value added, renamed or removed upstream fails here instead of leaving this
package quietly holding a stale vocabulary.

They skip when the upstream is not importable. A skip is honest -- it says "not checked
in this run" -- whereas a hand-written list would say "checked and agreed" while
checking only that this file agrees with itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from harness_jobs.approval import (
    APPROVAL_PERMISSION,
    NON_PERMISSIVE_RESULTS,
    ApprovalResult,
)

_REPO = Path(__file__).resolve().parents[4]
_HITL = _REPO / "contracts" / "hitl-ticket" / "v1"
_AUTH = _REPO / "modules" / "domain-apps" / "superplane" / "auth"

for candidate in (_HITL, _AUTH):
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


# ---------------------------------------------------------------------------
# The HITL answer vocabulary (contracts/hitl-ticket/v1)
# ---------------------------------------------------------------------------


def _hitl():
    """The contract module, or a skip naming what is therefore unchecked.

    Imported inside each test rather than at module scope so that the permission
    agreement below still runs when pydantic is absent. A module-level
    `importorskip` would take the whole file down with it, and the two upstreams
    fail independently.
    """
    return pytest.importorskip(
        "models",
        reason=(
            "contracts/hitl-ticket/v1/models.py is not importable here (it requires "
            "pydantic); the agreement between the four result values is therefore not "
            "checked in this run rather than assumed"
        ),
    )


def test_the_four_result_values_agree_exactly():
    """Set equality in both directions, from upstream's own members.

    Not a subset check. A value upstream that this package lacks is a result a consumer
    can send and this gate cannot interpret; a value here that upstream lacks is one
    this gate would honour and no contract defines. `ApprovalResult` is deliberately a
    different type -- what has to match is the wire string.
    """
    upstream = {member.value for member in _hitl().HitlResult}
    ours = {member.value for member in ApprovalResult}
    assert ours == upstream, (
        "the approval vocabulary has drifted from contracts/hitl-ticket/v1. Added "
        f"here: {sorted(ours - upstream)}; missing here: {sorted(upstream - ours)}"
    )


def test_only_allowed_once_permits_on_both_sides():
    """The permissive value is the same single value in both vocabularies.

    Derived by subtracting each side's non-permissive set from its full set, rather than
    naming `allowed-once` twice. A change upstream that made a second result permissive
    -- the `allowed-always` this contract's docstring explicitly refuses -- fails here,
    which is the whole point: a fail-closed gate whose permissive set silently widened
    is no longer fail-closed.
    """
    hitl = _hitl()
    upstream_permissive = {
        member.value
        for member in hitl.HitlResult
        if member not in hitl.NON_PERMISSIVE_RESULTS
    }
    ours_permissive = {
        member.value
        for member in ApprovalResult
        if member not in NON_PERMISSIVE_RESULTS
    }
    assert ours_permissive == upstream_permissive == {"allowed-once"}


def test_unavailable_and_rejected_are_both_present_and_distinct():
    """The distinction #4178 exists to preserve, asserted on this package's copy.

    Stated separately from set equality because it is the one collapse that would still
    pass a looser check: a package that mapped both onto a single `DENIED` member would
    satisfy "every value is accounted for" under a generous reading, and would lose the
    difference between a transport failure and a human saying no.
    """
    values = {member.value for member in ApprovalResult}
    assert {"unavailable", "rejected"} <= values
    assert ApprovalResult.UNAVAILABLE is not ApprovalResult.REJECTED
    assert len(NON_PERMISSIVE_RESULTS) == len(ApprovalResult) - 1


# ---------------------------------------------------------------------------
# The permission (superplane_auth.policy)
# ---------------------------------------------------------------------------


def test_the_approval_permission_is_the_administer_string():
    """`workspace:administer`, as the domain's policy spells it.

    A drifted string here is worse than a mismatch: `evaluate_approval` requires this
    permission on every selected approver, so a spelling no role grants would refuse
    every approval (a visible outage), and a spelling that happened to match a *weaker*
    permission would accept approvers the policy never authorized.
    """
    policy = pytest.importorskip(
        "superplane_auth.policy",
        reason=(
            "superplane_auth is not importable here; the approval permission string is "
            "therefore not checked against the domain policy in this run"
        ),
    )
    assert APPROVAL_PERMISSION == policy.Permission.ADMINISTER.value


def test_approval_authority_is_not_the_permission_that_requests_an_operation():
    """ADMINISTER and PROVISION must stay different strings.

    The blast-radius separation `policy.py:174-186` describes. If approval authority
    were PROVISION, every principal able to *request* an operation could approve one,
    and the distinct-approver rule would be all that remained -- satisfiable with two
    colluding or two compromised requester accounts.

    Asserted against the domain's own two members, so a policy change that merged them
    fails here rather than silently collapsing the separation this gate relies on.
    """
    policy = pytest.importorskip(
        "superplane_auth.policy",
        reason="superplane_auth is not importable here; the separation is unchecked",
    )
    assert policy.Permission.ADMINISTER.value != policy.Permission.PROVISION.value
    assert APPROVAL_PERMISSION != policy.Permission.PROVISION.value

    # And ADMINISTER must still imply PROVISION, because an administrator approving an
    # operation they could not themselves request would be an authority gap rather than
    # a separation. Asserted through the public `expand_permissions` rather than the
    # private `_IMPLIED` table it closes over: the implication is the policy's published
    # behaviour, and a test reaching into a private name would break on a refactor that
    # changed nothing observable.
    expanded = policy.expand_permissions([policy.Permission.ADMINISTER])
    assert policy.Permission.PROVISION in expanded
