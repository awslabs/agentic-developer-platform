"""Fixture lifecycle for the qualification harness (#5156).

Provision, read back, resume and clean up the fixtures a bounded qualification
needs, driving the inventory in :mod:`inventory` so every step is recoverable.

The provider is injected as a :class:`FixtureProvider`, not imported. Two
reasons: the offline tests need to simulate a crash at an exact point without
touching AWS, and the real provider for a given resource kind is supplied by
the scenario adapters in #5157 rather than owned here.

The ordering rule this module exists to enforce:

1. record the intent (``planned``) and flush it to disk
2. call the provider
3. record the observed resource id (``created``)

A crash between 1 and 2 leaves a planned entry with no resource. A crash
between 2 and 3 leaves a planned entry whose resource DOES exist. Both look
identical on disk, which is why resume asks the provider instead of assuming —
and why the provider is asked to honour an idempotency token where it can.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tests.e2e.orchestration.config import QualificationConfig
from tests.e2e.orchestration.inventory import (
    CREATED,
    PLANNED,
    FixtureRecord,
    Inventory,
    InventoryError,
    verify_ownership,
)


class FixtureError(RuntimeError):
    """A fixture could not be provisioned, reconciled or removed."""


class BoundExceededError(FixtureError):
    """Provisioning would exceed the config's resource bound.

    Raised before the provider is called, so the bound is a real cap rather
    than an after-the-fact observation.
    """


@runtime_checkable
class FixtureProvider(Protocol):
    """What the harness needs from whatever actually creates a resource.

    Scenario adapters (#5157) supply implementations. Every method is expected
    to be idempotent with respect to ``idempotency_token`` where the underlying
    provider supports one.
    """

    kind: str

    def create(self, *, intended_identity: str, ownership_tags: dict[str, str], idempotency_token: str) -> str:
        """Create the resource and return its provider-assigned id."""

    def find(self, *, intended_identity: str, idempotency_token: str) -> str | None:
        """Return the resource id if it already exists, else ``None``.

        This is what makes resume safe after a crash between create and record.
        Raise to signal "could not determine" — do not return ``None``, which
        means "positively absent".
        """

    def read_tags(self, resource_id: str) -> dict[str, str] | None:
        """Return the resource's tags, or ``None`` if they cannot be read.

        ``None`` blocks deletion: unverifiable ownership is never treated as
        ownership.
        """

    def delete(self, resource_id: str) -> None:
        """Delete the resource. Must tolerate an already-deleted resource."""


@dataclass(frozen=True)
class FixtureRequest:
    """One fixture a scenario wants, named deterministically.

    ``fixture_id`` is the harness-side name (stable across a resume);
    ``intended_identity`` is what the resource will be called in the provider.
    """

    fixture_id: str
    kind: str
    intended_identity: str


@dataclass(frozen=True)
class CleanupOutcome:
    """What cleanup did, and what it deliberately refused to touch."""

    deleted: tuple[str, ...]
    refused: tuple[tuple[str, str], ...]  # (fixture_id, reason)
    failed: tuple[tuple[str, str], ...]  # (fixture_id, error)

    @property
    def clean(self) -> bool:
        """True only when nothing was refused and nothing failed."""
        return not self.refused and not self.failed


def _idempotency_token(qualification_id: str, fixture_id: str) -> str:
    """Derive a stable token from ids already on disk.

    Deriving rather than randomising means a resume after a crash recomputes
    the SAME token, so a provider that honours it returns the original resource
    instead of creating a second one.
    """
    return f"{qualification_id}-{fixture_id}"


def provision(
    inventory: Inventory,
    config: QualificationConfig,
    provider: FixtureProvider,
    request: FixtureRequest,
) -> FixtureRecord:
    """Provision one fixture, recording the intent before creating it."""
    if provider.kind != request.kind:
        raise FixtureError(f"provider handles {provider.kind!r} fixtures, not {request.kind!r}")

    # Bound checked before the provider call. Counting `created` plus anything
    # still `planned` means an interrupted run cannot slip past the cap on
    # resume by leaving unresolved entries uncounted.
    if len(inventory.unresolved) >= config.max_resources:
        raise BoundExceededError(
            f"provisioning {request.fixture_id!r} would exceed bounds.max_resources="
            f"{config.max_resources} (already {len(inventory.unresolved)} unresolved fixtures)"
        )

    token = _idempotency_token(inventory.qualification_id, request.fixture_id)
    tags = config.ownership_tags(inventory.qualification_id)

    # Step 1: intent on disk before the provider is touched.
    inventory.record_planned(
        fixture_id=request.fixture_id,
        kind=request.kind,
        intended_identity=request.intended_identity,
        ownership_tags=tags,
        idempotency_token=token,
    )

    # Step 2: the mutation.
    try:
        resource_id = provider.create(
            intended_identity=request.intended_identity,
            ownership_tags=tags,
            idempotency_token=token,
        )
    except Exception as exc:
        # The planned entry is deliberately LEFT in place: the create may have
        # partially succeeded, and resume must reconcile it rather than trust
        # this exception to mean "nothing was created".
        raise FixtureError(
            f"provider failed creating {request.fixture_id!r}; the planned inventory entry is "
            f"retained for resume to reconcile: {exc}"
        ) from exc

    if not resource_id:
        raise FixtureError(f"provider returned no resource id for {request.fixture_id!r}")

    # Step 3: the observed id.
    return inventory.mark_created(request.fixture_id, resource_id)


def readback(
    inventory: Inventory,
    provider: FixtureProvider,
    fixture_id: str,
    expected_tags: dict[str, str],
) -> FixtureRecord:
    """Confirm a created fixture really exists and is really ours.

    Provisioning trusts the provider's return value; readback verifies it
    against the resource's own tags before a scenario depends on it.
    """
    record = inventory.get(fixture_id)
    if record.state != CREATED:
        raise FixtureError(f"fixture {fixture_id!r} is {record.state!r}, not {CREATED!r}; nothing to read back")
    observed_tags = provider.read_tags(record.observed_resource_id or "")
    owned, reason = verify_ownership(record, observed_tags, expected_tags)
    if not owned:
        raise FixtureError(f"readback of {fixture_id!r} could not confirm ownership: {reason}")
    return record


def resume(inventory: Inventory, providers: dict[str, FixtureProvider]) -> list[FixtureRecord]:
    """Reconcile a partially provisioned qualification before it retries.

    Every ``planned`` entry is ambiguous — the resource may or may not exist.
    Ask the provider, using the same derived idempotency token, and settle it:

    * found            -> ``created`` with the observed id (no duplicate is made)
    * positively absent -> left ``planned`` for the caller to retry normally
    * undeterminable   -> ``reconcile_failed``, never silently dropped

    Returns the records that are still unresolved after reconciliation.
    """
    for record in list(inventory.in_state(PLANNED)):
        provider = providers.get(record.kind)
        if provider is None:
            inventory.mark_reconcile_failed(
                record.fixture_id,
                f"no provider registered for kind {record.kind!r}, so this fixture may exist and leak",
            )
            continue

        token = record.idempotency_token or _idempotency_token(inventory.qualification_id, record.fixture_id)
        # Blind catches here and below are deliberate: a provider is third-party
        # code that may raise anything, and an unhandled exception would abandon
        # the remaining records mid-reconcile — the one outcome guaranteed to
        # leak. Every failure is recorded and reported instead.
        try:
            existing = provider.find(intended_identity=record.intended_identity, idempotency_token=token)
        except Exception as exc:
            inventory.mark_reconcile_failed(
                record.fixture_id,
                f"could not determine whether this fixture exists: {exc}",
            )
            continue

        if existing:
            # Adopt the resource the crashed run created instead of making a
            # second one; this is what prevents duplicate ownership.
            inventory.mark_created(record.fixture_id, existing)

    return inventory.unresolved


def cleanup(
    inventory: Inventory,
    config: QualificationConfig,
    providers: dict[str, FixtureProvider],
) -> CleanupOutcome:
    """Delete only positively verified owned fixtures.

    Anything whose ownership cannot be proved is refused and reported, not
    deleted. Each successful delete is recorded before moving on, so an
    interrupted cleanup resumes without re-deleting or losing track: the
    inventory always reflects what has actually been removed.
    """
    expected_tags = config.ownership_tags(inventory.qualification_id)
    deleted: list[str] = []
    refused: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []

    for record in list(inventory.unresolved):
        if record.state == PLANNED:
            # Never created (or never reconciled): there is no verified resource
            # to delete, and guessing an id could delete something else.
            refused.append(
                (
                    record.fixture_id,
                    (
                        "fixture is still 'planned' with no observed resource id; run --resume "
                        "first so it can be reconciled rather than guessed at"
                    ),
                )
            )
            continue

        provider = providers.get(record.kind)
        if provider is None:
            refused.append((record.fixture_id, f"no provider registered for kind {record.kind!r}"))
            continue

        try:
            observed_tags = provider.read_tags(record.observed_resource_id or "")
        except Exception as exc:
            refused.append((record.fixture_id, f"ownership tags could not be read: {exc}"))
            continue

        owned, reason = verify_ownership(record, observed_tags, expected_tags)
        if not owned:
            refused.append((record.fixture_id, reason))
            continue

        try:
            provider.delete(record.observed_resource_id or "")
        except Exception as exc:
            failed.append((record.fixture_id, str(exc)))
            continue

        # Flushed per fixture, so an interruption here leaves an accurate record.
        try:
            inventory.mark_deleted(record.fixture_id, detail="ownership verified before delete")
        except InventoryError as exc:  # pragma: no cover - defensive
            failed.append((record.fixture_id, f"deleted but could not be recorded: {exc}"))
            continue
        deleted.append(record.fixture_id)

    return CleanupOutcome(
        deleted=tuple(deleted),
        refused=tuple(refused),
        failed=tuple(failed),
    )
