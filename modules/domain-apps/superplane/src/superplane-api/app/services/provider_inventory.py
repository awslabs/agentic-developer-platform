"""Trusted allocation membership, independent of operation/outcome reports.

B must supply a fresh, complete inventory under its allocation recovery fence,
including compute, storage and network resources. No reader is installed until
that integration exists. Observations and operation names never create inventory.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from superplane_contracts import Submitter


@dataclass(frozen=True)
class AllocationResourceIdentity:
    resource_id: str
    provider: str
    provider_reference: str
    kind: str
    operation_keys: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class VerifiedAllocationInventory:
    workspace: str
    org_id: str
    allocation_id: str
    revision: str
    resources: tuple[AllocationResourceIdentity, ...]
    complete: bool
    expires_at: datetime
    executor_id: str
    active: bool
    attested_report_digest: str


class AllocationInventoryReader(Protocol):
    async def read(
        self,
        *,
        submitter: Submitter,
        workspace: str,
        allocation_id: str,
        operation_authority: str,
        report_digest: str,
    ) -> VerifiedAllocationInventory | None:
        """Return authoritative membership under a current B recovery fence.

        Verify active allocation-scoped B cleanup authority for this executor and
        an attestation of the exact report digest, independently through B. Never
        merely echo the digest or treat a workspace credential as B authority.
        The attestation must originate from B's authenticated provider-report path.
        Complete means all independently billable resources are enumerated and
        creation is fenced. Do not synthesize this from the submitted observations
        or from A's operation records. None means unavailable/unverified.
        """
        ...


_reader: AllocationInventoryReader | None = None


def set_allocation_inventory_reader(reader: AllocationInventoryReader | None) -> None:
    global _reader
    _reader = reader


def get_allocation_inventory_reader() -> AllocationInventoryReader | None:
    return _reader


def uninstall_allocation_inventory_reader(reader: AllocationInventoryReader) -> bool:
    """Remove `reader` if it is the installed one. Returns whether it was.

    Identity-scoped, as with the other three ports: a composition releases what it
    installed and leaves anything else alone. See
    `credential_evidence.uninstall_credential_evidence_reader`.
    """
    global _reader
    if _reader is not reader:
        return False
    _reader = None
    return True
