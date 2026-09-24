"""The production `allocation_inventory` adapter over `harness_jobs`.

Issue #5535 (Superplane W6), EPIC #4910.

Implements the `AllocationInventoryReader` Protocol declared at
`app/services/provider_inventory.py:38`, over
`harness_jobs.inventory.InventoryAuthority.read_inventory`.

## Why this is a field copy and not a translation

`harness_jobs.inventory.VerifiedInventory` says so itself: "The field names are the
consumer's, not this package's: they match
`provider_inventory.VerifiedAllocationInventory` one for one so that #5535's adapter
is a field copy rather than a translation. A translation is where a `complete` flag
ends up mapped from the wrong source."

The copy is still explicit — field by field, by name — rather than
`VerifiedAllocationInventory(**asdict(verified))`. Spreading would make the two
types' agreement a runtime coincidence: a field renamed on either side would raise
`TypeError` inside a path whose contract is "return `None` when unverified", and
the `None` would report a *packaging* mismatch as an unverified allocation. Named
assignment fails at the name, where the mismatch is.

The one thing that must not be copied is `attested_report_digest`. It is read from
the harness's verified answer, never from this adapter's `report_digest` argument,
because the port's whole requirement is an attestation that originated
independently — "Never merely echo the digest or treat a workspace credential as B
authority." Echoing the argument would satisfy the consumer's equality check while
establishing nothing, which is the defect in its purest form.

## Executor identity

The port passes a `Submitter`; the harness wants an `executor_id` and compares it
against `lease.holder`, refusing when they differ. So `submitter.submitter_id` is
passed through as the executor and the harness does the comparison against
server-held state. This adapter deliberately does not pre-check it: one comparison,
made where the lease row is locked, rather than a second one out here that could
drift from it.

## `None` everywhere

Both sides declare `NONE_MEANS_UNVERIFIED`, and `read_inventory` already answers
`None` for every failure including an unreachable database — with the reasoning
that the consumer's only two behaviours are "use this inventory" and "retain
exposure", so anything short of a verified answer must arrive as the second. This
adapter adds nothing to that but a guard against its own copy raising.

Retained exposure is not a silent failure: it means the domain holds budget rather
than releasing it, which is the conservative direction and the one
`operation_budget_ledger.retain` exists to record.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from superplane_contracts import Submitter

from app.services.provider_inventory import (
    AllocationResourceIdentity,
    VerifiedAllocationInventory,
)

logger = logging.getLogger(__name__)


class HarnessAllocationInventory:
    """Adapts `harness_jobs.InventoryAuthority` to the domain's inventory port."""

    def __init__(self, authority: Any) -> None:
        self._authority = authority

    async def read(
        self,
        *,
        submitter: Submitter,
        workspace: str,
        allocation_id: str,
        operation_authority: str,
        report_digest: str,
    ) -> VerifiedAllocationInventory | None:
        """Authoritative membership under a current fence, or `None`."""
        try:
            if not isinstance(submitter, Submitter):
                return None
            verified = await self._authority.read_inventory(
                executor_id=submitter.submitter_id,
                workspace_id=workspace,
                allocation_id=allocation_id,
                operation_authority=operation_authority,
                report_digest=report_digest,
            )
            if verified is None:
                return None
            return _inventory(verified)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Unverified. Logged without the report digest or the authority value.
            logger.info("an allocation inventory was not verified", exc_info=False)
            return None


def _inventory(verified: Any) -> VerifiedAllocationInventory:
    """Copy the harness's verified inventory into the consumer's own type.

    Every field named. `attested_report_digest` comes from `verified`, never from
    the caller's argument — see the module docstring.
    """
    return VerifiedAllocationInventory(
        workspace=verified.workspace,
        org_id=verified.org_id,
        allocation_id=verified.allocation_id,
        revision=verified.revision,
        resources=tuple(_resource(item) for item in verified.resources),
        complete=verified.complete,
        expires_at=verified.expires_at,
        executor_id=verified.executor_id,
        active=verified.active,
        attested_report_digest=verified.attested_report_digest,
    )


def _resource(resource: Any) -> AllocationResourceIdentity:
    """One resource, copied field by field.

    `operation_keys` is rebuilt as a `frozenset` rather than passed through: the
    consumer's type declares one, and an adapter that happened to receive a
    mutable set would hand the consumer an aliased collection it believes is
    immutable.
    """
    return AllocationResourceIdentity(
        resource_id=resource.resource_id,
        provider=resource.provider,
        provider_reference=resource.provider_reference,
        kind=resource.kind,
        operation_keys=frozenset(resource.operation_keys or ()),
    )
