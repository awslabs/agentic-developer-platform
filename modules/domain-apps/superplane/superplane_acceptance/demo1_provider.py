"""Read-only observer contract; diagnostic inventory is never a live receipt."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol

from .demo1_evidence import DemoInput, EvidenceError, identifier, instant, text


@dataclass(frozen=True)
class InventoryQuery:
    connection_id: str
    role: str
    account: str
    region: str
    workspace_id: str
    expected_owned: tuple[str, ...]
    expected_survivors: tuple[str, ...]


class ProviderReader(Protocol):
    """External read-only lookup, not an operation executor or admission route."""

    def read_inventory(self, query: InventoryQuery) -> object:
        """Return resource observations and target identity from the selected provider."""


@dataclass(frozen=True)
class InventoryAssessment:
    cleanup: str
    survivors: str
    cost: str
    reason: str
    observed_at: str | None
    origin: str


def _resource_set(value: object, label: str) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise EvidenceError(f"{label}: invalid resource listing")
    items = [text(item, label) for item in value]
    if len(items) != len(set(items)):
        raise EvidenceError(f"{label}: duplicate resource listing")
    return set(items)


def observe_provider(
    selected: DemoInput,
    workspace_id: str,
    expected_owned: tuple[str, ...],
    reader: ProviderReader,
    *,
    origin: str = "fixture",
) -> InventoryAssessment:
    """Compare read-only observations to an independently supplied creation inventory.

    The caller must obtain ``expected_owned`` from verified creation provenance;
    an empty list cannot establish deletion. A provider adapter must separately
    authenticate its read and register with the live acceptance path before any
    diagnostic can be promoted to an authoritative result.
    """
    if origin not in ("fixture", "provider-unverified"):
        raise EvidenceError("inventory: unregistered observer origin")
    if not expected_owned or len(set(expected_owned)) != len(expected_owned):
        raise EvidenceError("inventory: independent owned-resource baseline required")
    owned_expected = tuple(text(item, "owned resource") for item in expected_owned)
    query = InventoryQuery(
        selected.connection_id,
        selected.role,
        selected.account,
        selected.region,
        identifier(workspace_id, "workspace_id"),
        owned_expected,
        selected.survivors,
    )
    missing = object()
    observed = missing
    with suppress(Exception):
        observed = reader.read_inventory(query)
    if observed is missing:
        return InventoryAssessment(
            "BLOCKED", "BLOCKED", "BLOCKED", "provider lookup unavailable", None, origin
        )
    if not isinstance(observed, dict) or set(observed) != {
        "connection_id",
        "role",
        "account",
        "region",
        "workspace_id",
        "status",
        "owned_present",
        "survivors_present",
        "cost_usd",
        "observed_at",
    }:
        raise EvidenceError("inventory: missing or unexpected fields")
    if any(
        observed[field] != getattr(query, field)
        for field in (
            "connection_id",
            "role",
            "account",
            "region",
            "workspace_id",
        )
    ):
        raise EvidenceError("inventory: provider target or credential mismatch")
    stamp = instant(observed["observed_at"], "inventory time")
    if not selected.authorized_at <= stamp <= selected.deadline:
        raise EvidenceError("inventory: stale or out-of-window observation")
    if observed["status"] not in ("complete", "incomplete", "denied"):
        raise EvidenceError("inventory: invalid read status")
    if observed["status"] != "complete":
        return InventoryAssessment(
            "BLOCKED",
            "BLOCKED",
            "BLOCKED",
            "provider inventory denied or incomplete",
            stamp.isoformat(),
            origin,
        )
    owned_present = _resource_set(observed["owned_present"], "owned_present")
    survivors_present = _resource_set(
        observed["survivors_present"], "survivors_present"
    )
    if not owned_present <= set(owned_expected):
        raise EvidenceError("inventory: resource not in original ownership baseline")
    if not survivors_present <= set(selected.survivors):
        raise EvidenceError("inventory: resource not in survivor baseline")
    cleanup = "FAIL" if owned_present else "BLOCKED"
    survivors = "FAIL" if survivors_present != set(selected.survivors) else "BLOCKED"
    raw_cost = observed["cost_usd"]
    if raw_cost is None:
        cost = "BLOCKED"
    else:
        try:
            amount = Decimal(text(raw_cost, "cost_usd"))
        except InvalidOperation as error:
            raise EvidenceError("inventory: invalid cost") from error
        if not amount.is_finite() or amount < 0:
            raise EvidenceError("inventory: invalid cost")
        cost = "FAIL" if amount > selected.budget_usd else "BLOCKED"
    reason = "diagnostic only; authoritative provider attestation not registered"
    if owned_present or survivors_present != set(selected.survivors):
        reason = "owned resource remains or survivor is missing"
    elif cost == "FAIL":
        reason = "observed cost exceeds selected budget"
    elif raw_cost is None:
        reason = "cost unknown; diagnostic inventory cannot establish zero spend"
    return InventoryAssessment(
        cleanup, survivors, cost, reason, stamp.isoformat(), origin
    )
