"""Thin client over the versioned Superplane domain contract.

**This is a recorded mock.** R9 permits it explicitly: "The tools here are thin
adapters over versioned domain APIs, so they consume A's own contracts; where a
shared contract is unavailable, mock it and record the mock."

What is unavailable, and why this is a mock rather than a stub of something that
exists:

* The Superplane domain API lives upstream (`src/superplane-api/`), and the
  versioned OpenAPI contract this client is meant to consume is owned by **U8**
  (`contracts/`), which has not landed. There is nothing to generate a client from.
* `modules/harness/mcp-hub/` has "no running service yet" and its
  `contracts/tool.schema.json` is unwritten. R9 states plainly that this is a
  fact about today's ADP and **not** a prerequisite this unit takes on.
* The upstream source itself was not reachable from the runtime that wrote this
  file, so even the request/response shapes could not be copied from it. They are
  derived from the design note's capacity model.

So the boundary is real and the transport is not. Everything above this module —
the single authorization decision, the read/spend tool split, the redaction
boundary — is the actual deliverable and is fully exercised by the tests. When
U8 lands the versioned contract, `CapacityContract` is the one seam to replace:
swap the in-memory data for HTTP calls against the generated client and the tool
surface above it does not change.

The mock is deliberately **not** a silent one. `IS_MOCK` is True, and
`superplane_discover_capacity` reports `"source": "mock"` in its result, so a
model or operator reading a result can tell it did not come from a provider.
Returning plausible numbers with no such marker is how a mock gets mistaken for
a live reading.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# Read by the tool surface and asserted by the tests. Flipping this to False
# without replacing the transport below would make results claim to be live.
IS_MOCK = True

# The contract version these shapes correspond to. Pinned so a future real
# client can assert it is talking to a compatible server rather than guessing.
CONTRACT_VERSION = "v1"


class ContractError(RuntimeError):
    """Raised when the domain contract rejects an operation.

    Carries no provider payload: the message is composed here from the
    operation and a stable reason, so an upstream error body cannot smuggle a
    credential into a model-visible tool result via an exception string.
    """


@dataclass(frozen=True)
class CapacityOffer:
    """A capacity option a caller could allocate.

    Note there is no credential field anywhere in this shape. Discovery answers
    "what could I run, and what would it cost"; it never needs a provider secret,
    which is why the read-only tool can be safely model-visible.
    """

    offer_id: str
    workspace: str
    instance_type: str
    accelerator: str
    available: int
    price_per_hour_usd: float
    region: str


@dataclass(frozen=True)
class Allocation:
    """A capacity allocation that exists and is costing money."""

    allocation_id: str
    workspace: str
    offer_id: str
    state: str


# In-memory fixture data standing in for the domain API. Keyed by workspace so
# the tenant-isolation tests have something to isolate.
_OFFERS: dict[str, tuple[CapacityOffer, ...]] = {
    "ws-alpha": (
        CapacityOffer(
            offer_id="offer-a1",
            workspace="ws-alpha",
            instance_type="g5.xlarge",
            accelerator="nvidia-a10g",
            available=4,
            price_per_hour_usd=1.006,
            region="us-east-1",
        ),
        CapacityOffer(
            offer_id="offer-a2",
            workspace="ws-alpha",
            instance_type="p4d.24xlarge",
            accelerator="nvidia-a100",
            available=1,
            price_per_hour_usd=32.7726,
            region="us-east-1",
        ),
    ),
    "ws-beta": (
        CapacityOffer(
            offer_id="offer-b1",
            workspace="ws-beta",
            instance_type="g5.2xlarge",
            accelerator="nvidia-a10g",
            available=2,
            price_per_hour_usd=1.212,
            region="us-west-2",
        ),
    ),
}


class CapacityContract:
    """Thin adapter over the versioned capacity contract.

    Split into read and write methods so the tool layer above can bind a
    read-only tool to `list_capacity()` alone. The split is enforced by the tool
    handler table and asserted by `tests/test_tool_separation.py`.
    """

    def __init__(self) -> None:
        # Per-instance so tests do not leak allocations into one another.
        self._allocations: dict[str, Allocation] = {}
        self._counter = 0

    # -- read-only ---------------------------------------------------------

    def list_capacity(
        self, workspace: str, accelerator: str | None = None
    ) -> list[dict]:
        """Return capacity offers for a workspace. Spends nothing, mutates nothing."""
        offers = _OFFERS.get(workspace, ())
        if accelerator:
            offers = tuple(o for o in offers if o.accelerator == accelerator)
        return [asdict(o) for o in offers]

    # -- spending / mutating ----------------------------------------------

    def allocate(self, workspace: str, offer_id: str) -> dict:
        """Allocate capacity. This spends money."""
        available = {o.offer_id for o in _OFFERS.get(workspace, ())}
        if offer_id not in available:
            # Same message whether the offer belongs to another workspace or does
            # not exist, so this cannot enumerate another tenant's offers.
            raise ContractError(f"offer not available in workspace: {offer_id}")
        self._counter += 1
        allocation = Allocation(
            allocation_id=f"alloc-{self._counter}",
            workspace=workspace,
            offer_id=offer_id,
            state="allocated",
        )
        self._allocations[allocation.allocation_id] = allocation
        return asdict(allocation)

    def release(self, workspace: str, allocation_id: str) -> dict:
        """Release an allocation. This mutates provider state."""
        existing = self._allocations.get(allocation_id)
        if existing is None or existing.workspace != workspace:
            raise ContractError(f"allocation not found: {allocation_id}")
        released = Allocation(
            allocation_id=existing.allocation_id,
            workspace=existing.workspace,
            offer_id=existing.offer_id,
            state="released",
        )
        self._allocations[allocation_id] = released
        return asdict(released)
