"""Agreement with the published inventory, reconciliation and accounting contracts.

Issue #5529 (w6-06), EPIC #4910, Wave 6.

`harness_jobs.inventory` duplicates three things the domain publishes: the presence and
release vocabularies (`superplane_contracts.reconciliation`,
`superplane_contracts.accounting`), the field shape of the verified inventory
(`app.services.provider_inventory.VerifiedAllocationInventory`) and -- most importantly
-- the **canonical digest form** the consumer computes before it asks.

Duplicated rather than imported for the reason `test_lease_contract_agreement.py` gives:
this package is installed standalone and must not require the contracts package on
`sys.path`.

Here drift is not cosmetic, and the digest is the sharpest case. The consumer arrives
holding a digest *it* computed and looks the attestation up by that value. If the two
canonical forms disagree by a single byte -- a space after a separator, insertion order
instead of sorted keys, a field spelled differently -- then no attestation is found.
That does not surface as a mismatch anybody diagnoses: `read` returns `None`, the domain
retains exposure, and the symptom is that budget is silently never released. A green
suite on both sides would show nothing at all.

The field-name agreement matters for the adjacent reason. #5535 composes this
implementation into the port by copying fields across, and the consumer rejects an
inventory whose `complete`, `active` or `executor_id` it cannot read
(`provider_handles.py:800-870`). A renamed field there fails closed too, and just as
quietly.

Skips when the upstream is not importable, which is honest: a skip says "not checked in
this run", whereas a silent pass would say "checked and agreed".
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict, fields
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harness_jobs import inventory as ours

_REPO = Path(__file__).resolve().parents[4]
_SUPERPLANE = _REPO / "modules" / "domain-apps" / "superplane"
_CONTRACTS = _SUPERPLANE / "contracts"
_API = _SUPERPLANE / "src" / "superplane-api"

for _path in (_CONTRACTS, _API):
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

reconciliation = pytest.importorskip(
    "superplane_contracts.reconciliation",
    reason=(
        "superplane-contracts is not importable here; the agreement between the two "
        "spellings of the provider-presence vocabulary is therefore not checked in "
        "this run rather than assumed"
    ),
)
accounting = pytest.importorskip("superplane_contracts.accounting", reason="see above")


def test_the_presence_vocabulary_agrees_value_for_value():
    """A missing value is a provider answer this package cannot represent.

    Checked as whole sets rather than member by member, because the failure shape is an
    upstream *addition*: a fourth presence this package silently cannot receive would
    otherwise pass a per-member check.
    """
    assert {item.value for item in ours.ResourcePresence} == {
        item.value for item in reconciliation.ProviderPresence
    }


def test_unknown_is_spelled_the_same_on_both_sides():
    """The one value whose meaning the whole module turns on.

    Called out separately from the set comparison because if these two ever disagreed
    about this spelling, an UNKNOWN crossing the boundary would fail to match and be
    handled as something else -- and every other branch here leads to releasing budget.
    """
    assert ours.ResourcePresence.UNKNOWN.value == (
        reconciliation.ProviderPresence.UNKNOWN.value
    )


def test_the_release_and_exposure_vocabularies_agree():
    assert {item.value for item in ours.ReleaseState} == {
        item.value for item in accounting.ReleaseState
    }
    assert {item.value for item in ours.CostExposure} == {
        item.value for item in accounting.CostExposure
    }


def test_the_canonical_digest_is_byte_identical_to_the_consumers():
    """**The load-bearing agreement in this file.**

    Computes the digest both ways over the same observations: once through this package,
    once through the exact expression `assess_allocation_release` uses
    (`services/provider_handles.py:772`). They must produce the same string, or the
    attestation this package commits can never be looked up by the consumer that needs
    it.

    The observations deliberately include a `None` `provider_state`, a non-empty
    `detail`, and keys in non-sorted order, because those are the three places the two
    encodings could differ without the simple case noticing.
    """
    ours_observations = {
        "zeta": ours.ResourceObservation(
            presence=ours.ResourcePresence.PRESENT,
            queried_by="zeta",
            provider_state="RUNNING",
            detail="still up",
        ),
        "alpha": ours.ResourceObservation(
            presence=ours.ResourcePresence.ABSENT, queried_by="alpha"
        ),
        "mid": ours.ResourceObservation(
            presence=ours.ResourcePresence.UNKNOWN,
            queried_by="mid",
            detail="the provider API timed out",
        ),
    }
    theirs_observations = {
        "zeta": reconciliation.ProviderObservation(
            presence=reconciliation.ProviderPresence.PRESENT,
            queried_by="zeta",
            provider_state="RUNNING",
            detail="still up",
        ),
        "alpha": reconciliation.ProviderObservation(
            presence=reconciliation.ProviderPresence.ABSENT, queried_by="alpha"
        ),
        "mid": reconciliation.ProviderObservation(
            presence=reconciliation.ProviderPresence.UNKNOWN,
            queried_by="mid",
            detail="the provider API timed out",
        ),
    }
    consumer_digest = hashlib.sha256(
        json.dumps(
            {key: asdict(value) for key, value in theirs_observations.items()},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert ours.report_digest(ours_observations) == consumer_digest


def test_the_observation_field_names_agree():
    """The digest is taken over these names, so a rename changes every digest."""
    assert [field.name for field in fields(ours.ResourceObservation)] == [
        field.name for field in fields(reconciliation.ProviderObservation)
    ]


def test_both_sides_require_the_query_to_have_used_the_providers_handle():
    """**F3.** The agreement that matters is the MEANING of `queried_by`, not its name.

    Field names and digest bytes agreeing is what this file checked, and it is not
    enough: both sides can spell `queried_by` identically, digest it identically, and
    still disagree about what makes an observation authoritative. That is what happened.
    The consumer required the query to have used the member's stored
    `provider_reference` before it would trust an answer
    (`provider_handles.py:993`); this package compared `queried_by` against the mapping
    key instead. So a release the consumer would have refused was authorized by the
    authority that is supposed to be the stricter of the two -- and no test here could
    see it, because every field name and every digest byte still matched.

    Asserted as behaviour on both sides over the same two observations, rather than by
    inspecting either implementation: the claim is that the two reach the same verdict,
    and only running both can establish that.
    """
    member = ours.AllocationResource(
        resource_id="cluster-1",
        provider="aws",
        provider_reference="i-123",
        kind="compute",
    )
    allocation = accounting.AllocationResources(
        allocation_id="alloc-1", resource_ids=frozenset({member.resource_id})
    )

    def consumer_verdict(queried_by):
        """The consumer's own two steps: normalize by identity, then assess."""
        observed = reconciliation.ProviderObservation(
            presence=reconciliation.ProviderPresence.ABSENT, queried_by=queried_by
        )
        # `provider_handles.assess_allocation_release` replaces a matching observation's
        # `queried_by` with the resource id and substitutes UNKNOWN otherwise, before
        # handing the result to `assess_release`. Reproduced rather than imported: the
        # API tree may not be importable, and this is the rule being compared.
        if queried_by == member.provider_reference:
            evidence = {
                member.resource_id: reconciliation.ProviderObservation(
                    presence=observed.presence, queried_by=member.resource_id
                )
            }
        else:
            evidence = {
                member.resource_id: reconciliation.ProviderObservation(
                    presence=reconciliation.ProviderPresence.UNKNOWN,
                    queried_by=member.resource_id,
                    detail="provider evidence does not match authoritative membership",
                )
            }
        return accounting.assess_release(evidence, allocation=allocation).state.value

    def our_verdict(queried_by):
        inventory = ours.VerifiedInventory(
            workspace="ws-1",
            org_id="org-1",
            allocation_id=allocation.allocation_id,
            revision="inventory-x",
            resources=(member,),
            complete=True,
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
            executor_id="worker-1",
            active=True,
            attested_report_digest="d" * 64,
        )
        observations = {
            member.resource_id: ours.ResourceObservation(
                presence=ours.ResourcePresence.ABSENT, queried_by=queried_by
            )
        }
        return ours._reconcile(inventory, observations).state.value

    # The provider's own handle: authoritative on both sides, so the budget is released.
    assert our_verdict("i-123") == consumer_verdict("i-123") == "released"
    # The LOCAL id: not a handle the provider can answer about, so neither side treats
    # the answer as evidence, and neither releases.
    assert our_verdict("cluster-1") == consumer_verdict("cluster-1") == "unresolved"


def test_an_observation_this_package_accepts_the_contract_also_accepts():
    """Validation parity, so a published attestation is always readable downstream.

    An observation this package stored but the contract refuses to construct would be an
    attestation of something no consumer can deserialize -- verified, and unusable.
    """
    for presence, state, detail in (
        (ours.ResourcePresence.PRESENT, "RUNNING", ""),
        (ours.ResourcePresence.ABSENT, None, ""),
        (ours.ResourcePresence.UNKNOWN, None, "the provider API timed out"),
    ):
        ours.ResourceObservation(
            presence=presence,
            queried_by="r-1",
            provider_state=state,
            detail=detail,
        )
        reconciliation.ProviderObservation(
            presence=reconciliation.ProviderPresence(presence.value),
            queried_by="r-1",
            provider_state=state,
            detail=detail,
        )


# ---------------------------------------------------------------------------
# The port's own dataclasses, which live in the API tree rather than the contracts
# package. Imported separately so a missing API tree skips only these.
# ---------------------------------------------------------------------------

provider_inventory = pytest.importorskip(
    "app.services.provider_inventory",
    reason=(
        "the superplane-api tree is not importable here; the field agreement between "
        "this implementation and the port's dataclasses is therefore not checked in "
        "this run rather than assumed"
    ),
)


def test_the_verified_inventory_fields_agree_name_for_name():
    """#5535 copies these across; a rename fails the consumer's check, silently."""
    assert [field.name for field in fields(ours.VerifiedInventory)] == [
        field.name for field in fields(provider_inventory.VerifiedAllocationInventory)
    ]


def test_the_resource_identity_fields_agree_name_for_name():
    assert [field.name for field in fields(ours.AllocationResource)] == [
        field.name for field in fields(provider_inventory.AllocationResourceIdentity)
    ]


def test_a_resource_this_package_records_satisfies_the_consumers_limits():
    """The field widths the consumer independently enforces (`provider_handles.py:815`).

    Checked at the boundary values, because a limit that is one character apart on the
    two sides is exactly the drift that passes every ordinary test: this package stores
    the row, the consumer refuses the inventory containing it, and the release is
    unresolved for a reason no log explains.
    """
    widest = ours.AllocationResource(
        resource_id="r" * 255,
        provider="p" * 64,
        provider_reference="h" * 255,
        kind="k" * 64,
        operation_keys=frozenset({"o" * 255}),
    )
    identity = provider_inventory.AllocationResourceIdentity(
        resource_id=widest.resource_id,
        provider=widest.provider,
        provider_reference=widest.provider_reference,
        kind=widest.kind,
        operation_keys=widest.operation_keys,
    )
    assert identity.resource_id == widest.resource_id
    for oversized in (
        {"resource_id": "r" * 256},
        {"provider": "p" * 65},
        {"provider_reference": "h" * 256},
        {"kind": "k" * 65},
    ):
        values = {
            "resource_id": "r-1",
            "provider": "aws",
            "provider_reference": "h-1",
            "kind": "compute",
        }
        values.update(oversized)
        with pytest.raises(ours.ContractViolation):
            ours.AllocationResource(**values)
