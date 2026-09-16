"""Write-ahead fixture inventory for the qualification harness (#5156).

Every fixture this harness creates is recorded *before* the provider call that
creates it. That ordering is the whole point: if the process dies between the
record and the create, the intent is still on disk, so `--resume` can ask the
provider what actually happened instead of guessing or leaking a resource.

Each entry carries the qualification id, the intended resource identity, a
provider idempotency token where the provider supports one, the ownership tags
stamped on the resource, and — once the create returns — the observed resource
id. Cleanup deletes only what it can positively tie back to these records.

Writes are atomic (temp file in the same directory, fsync, ``os.replace``) so a
crash mid-write leaves the previous inventory intact rather than a truncated
file. Standard library only; no AWS call lives in this module.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

INVENTORY_VERSION = 1
INVENTORY_FILENAME = "inventory.json"
MANAGED_BY = "tests.e2e.orchestration"

# A qualification id names a directory, so it may not contain a separator, a
# '..' segment, an absolute-path prefix or anything else that could escape the
# artifact root. Checked rather than sanitized: silently rewriting an operator's
# id would make the resulting inventory hard to find.
_QUALIFICATION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")

# Lifecycle. PLANNED is written before the provider call; CREATED after the
# observed id is known; DELETED after cleanup verified the resource is gone.
# RECONCILE_FAILED marks an entry resume could not resolve — it is never
# silently dropped, because it may be a leak needing a human.
PLANNED = "planned"
CREATED = "created"
DELETED = "deleted"
RECONCILE_FAILED = "reconcile_failed"
_STATES = frozenset({PLANNED, CREATED, DELETED, RECONCILE_FAILED})

# Entry fields that may never hold a credential. The inventory is an artifact
# that gets uploaded, so a secret reaching it would outlive the run.
_SECRET_LIKE = re.compile(
    r"(password|passwd|secret|token|credential|private_key|access_key|api_key)",
    re.IGNORECASE,
)
# `idempotency_token` is a provider dedupe key, not a credential, so it is
# exempt from the name check above.
_TOKEN_ALLOWED = frozenset({"idempotency_token"})


class InventoryError(RuntimeError):
    """The inventory on disk cannot be trusted for the requested operation."""


class ForeignInventoryError(InventoryError):
    """The inventory belongs to another qualification, tool or environment.

    Raised instead of adopting it: acting on records this run did not write is
    how one run deletes another run's resources.
    """


@dataclass(frozen=True)
class FixtureRecord:
    """One fixture: what we intended, and what the provider actually made."""

    fixture_id: str
    kind: str
    intended_identity: str
    state: str
    ownership_tags: dict[str, str]
    idempotency_token: str | None = None
    observed_resource_id: str | None = None
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "fixture_id": self.fixture_id,
            "kind": self.kind,
            "intended_identity": self.intended_identity,
            "state": self.state,
            "ownership_tags": dict(self.ownership_tags),
            "idempotency_token": self.idempotency_token,
            "observed_resource_id": self.observed_resource_id,
            "detail": self.detail,
        }

    @classmethod
    def from_json(cls, body: Any) -> FixtureRecord:
        if not isinstance(body, dict):
            raise InventoryError("inventory entry is not an object")
        missing = [k for k in ("fixture_id", "kind", "intended_identity", "state") if k not in body]
        if missing:
            raise InventoryError(f"inventory entry is missing required field(s): {', '.join(missing)}")
        state = body["state"]
        if state not in _STATES:
            raise InventoryError(f"inventory entry has unknown state {state!r}")
        tags = body.get("ownership_tags") or {}
        if not isinstance(tags, dict):
            raise InventoryError("inventory entry ownership_tags must be an object")
        return cls(
            fixture_id=str(body["fixture_id"]),
            kind=str(body["kind"]),
            intended_identity=str(body["intended_identity"]),
            state=str(state),
            ownership_tags={str(k): str(v) for k, v in tags.items()},
            idempotency_token=_optional_str(body.get("idempotency_token")),
            observed_resource_id=_optional_str(body.get("observed_resource_id")),
            detail=_optional_str(body.get("detail")),
        )


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def new_qualification_id() -> str:
    """Mint a qualification id. Random, so two runs never share an inventory."""
    return f"q-{uuid.uuid4().hex[:16]}"


def inventory_path(artifact_directory: str | Path, qualification_id: str) -> Path:
    """Resolve the inventory path for a qualification, refusing escapes.

    The qualification id is validated *and* the resolved path is confirmed to
    sit under the artifact root, so a crafted id cannot write outside it even
    if the pattern above is later loosened.
    """
    if not isinstance(qualification_id, str) or not _QUALIFICATION_ID.match(qualification_id):
        raise InventoryError(
            f"invalid qualification id {qualification_id!r}: expected 8-64 characters "
            f"matching {_QUALIFICATION_ID.pattern}"
        )
    root = Path(artifact_directory).expanduser().resolve()
    candidate = (root / qualification_id / INVENTORY_FILENAME).resolve()
    if root not in candidate.parents:
        raise InventoryError(f"qualification id {qualification_id!r} resolves outside the artifact directory {root}")
    return candidate


@dataclass
class Inventory:
    """The mutable in-memory inventory, persisted on every change."""

    qualification_id: str
    environment: str
    path: Path
    fixtures: list[FixtureRecord]

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def create(cls, artifact_directory: str | Path, qualification_id: str, environment: str) -> Inventory:
        """Start a new inventory and persist it before any fixture exists."""
        path = inventory_path(artifact_directory, qualification_id)
        if path.exists():
            raise InventoryError(
                f"inventory already exists for qualification {qualification_id!r} at {path}; "
                f"use resume or cleanup rather than starting over"
            )
        inventory = cls(
            qualification_id=qualification_id,
            environment=environment,
            path=path,
            fixtures=[],
        )
        inventory.flush()
        return inventory

    @classmethod
    def load(
        cls,
        artifact_directory: str | Path,
        qualification_id: str,
        environment: str | None = None,
    ) -> Inventory:
        """Load an existing inventory, refusing anything foreign.

        `environment` is checked when supplied: an inventory recorded against a
        different environment must not be resumed or cleaned up here, because
        its resource ids refer to another account's resources.
        """
        path = inventory_path(artifact_directory, qualification_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise InventoryError(f"no inventory for qualification {qualification_id!r} at {path}") from None
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InventoryError(f"inventory at {path} is not valid JSON: {exc}") from None
        if not isinstance(document, dict):
            raise InventoryError(f"inventory at {path} must be a JSON object")

        version = document.get("inventory_version")
        if version != INVENTORY_VERSION:
            raise InventoryError(
                f"inventory at {path} has version {version!r}; this harness writes and reads "
                f"version {INVENTORY_VERSION} and will not guess at another layout"
            )
        if document.get("managed_by") != MANAGED_BY:
            raise ForeignInventoryError(
                f"inventory at {path} is managed by {document.get('managed_by')!r}, not {MANAGED_BY!r}"
            )
        recorded_id = document.get("qualification_id")
        if recorded_id != qualification_id:
            raise ForeignInventoryError(
                f"inventory at {path} records qualification {recorded_id!r}, not the requested "
                f"{qualification_id!r}"
            )
        recorded_env = document.get("environment")
        if environment is not None and recorded_env != environment:
            raise ForeignInventoryError(
                f"inventory {qualification_id!r} was recorded against environment {recorded_env!r} "
                f"but this run targets {environment!r}; refusing to act on another environment's fixtures"
            )

        entries = document.get("fixtures")
        if not isinstance(entries, list):
            raise InventoryError(f"inventory at {path} must carry a 'fixtures' list")
        return cls(
            qualification_id=qualification_id,
            environment=str(recorded_env),
            path=path,
            fixtures=[FixtureRecord.from_json(entry) for entry in entries],
        )

    # -- mutation ----------------------------------------------------------

    def record_planned(
        self,
        *,
        fixture_id: str,
        kind: str,
        intended_identity: str,
        ownership_tags: dict[str, str],
        idempotency_token: str | None = None,
    ) -> FixtureRecord:
        """Record an intent and persist it BEFORE the provider is called."""
        if any(f.fixture_id == fixture_id for f in self.fixtures):
            raise InventoryError(f"fixture {fixture_id!r} is already recorded in this inventory")
        record = FixtureRecord(
            fixture_id=fixture_id,
            kind=kind,
            intended_identity=intended_identity,
            state=PLANNED,
            ownership_tags=dict(ownership_tags),
            idempotency_token=idempotency_token,
        )
        _reject_secretish(record)
        self.fixtures.append(record)
        self.flush()
        return record

    def mark_created(self, fixture_id: str, observed_resource_id: str) -> FixtureRecord:
        """Attach the provider's real resource id to a planned fixture."""
        if not observed_resource_id:
            raise InventoryError(f"fixture {fixture_id!r} cannot be marked created without a resource id")
        return self._update(fixture_id, state=CREATED, observed_resource_id=str(observed_resource_id))

    def mark_deleted(self, fixture_id: str, detail: str | None = None) -> FixtureRecord:
        """Record that cleanup verified this fixture is gone."""
        return self._update(fixture_id, state=DELETED, detail=detail)

    def mark_reconcile_failed(self, fixture_id: str, detail: str) -> FixtureRecord:
        """Record that resume could not determine this fixture's real state."""
        return self._update(fixture_id, state=RECONCILE_FAILED, detail=detail)

    def _update(self, fixture_id: str, **changes: Any) -> FixtureRecord:
        for index, record in enumerate(self.fixtures):
            if record.fixture_id == fixture_id:
                updated = replace(record, **changes)
                _reject_secretish(updated)
                self.fixtures[index] = updated
                self.flush()
                return updated
        raise InventoryError(f"fixture {fixture_id!r} is not in inventory {self.qualification_id!r}")

    # -- queries -----------------------------------------------------------

    def get(self, fixture_id: str) -> FixtureRecord:
        for record in self.fixtures:
            if record.fixture_id == fixture_id:
                return record
        raise InventoryError(f"fixture {fixture_id!r} is not in inventory {self.qualification_id!r}")

    def in_state(self, *states: str) -> list[FixtureRecord]:
        wanted = set(states)
        return [record for record in self.fixtures if record.state in wanted]

    @property
    def unresolved(self) -> list[FixtureRecord]:
        """Fixtures that may exist in the provider: resume's work list.

        PLANNED means we may have crashed before or after the create, so the
        provider must be asked. CREATED means it exists and needs cleanup.
        """
        return self.in_state(PLANNED, CREATED)

    def live_resource_count(self) -> int:
        return len(self.in_state(CREATED))

    # -- persistence -------------------------------------------------------

    def flush(self) -> Path:
        """Persist atomically: temp file in the same dir, fsync, then replace.

        Same-directory temp keeps ``os.replace`` on one filesystem, where it is
        atomic. A crash therefore leaves either the old inventory or the new
        one, never a half-written file that would strand real resources.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "inventory_version": INVENTORY_VERSION,
            "managed_by": MANAGED_BY,
            "qualification_id": self.qualification_id,
            "environment": self.environment,
            "fixtures": [record.to_json() for record in self.fixtures],
        }
        payload = json.dumps(document, indent=2, sort_keys=True) + "\n"

        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(self.path.parent),
            prefix=f".{INVENTORY_FILENAME}.",
            suffix=".tmp",
            delete=False,
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        # The inventory names real resources; keep it owner-readable only.
        os.chmod(self.path, 0o600)
        return self.path


def _reject_secretish(record: FixtureRecord) -> None:
    """Keep credentials out of an artifact that outlives the run."""
    for key in record.ownership_tags:
        if key not in _TOKEN_ALLOWED and _SECRET_LIKE.search(key):
            raise InventoryError(f"refusing to record secret-like ownership tag {key!r} in the inventory")


def verify_ownership(
    record: FixtureRecord,
    observed_tags: dict[str, str] | None,
    expected_tags: dict[str, str],
) -> tuple[bool, str]:
    """Decide whether a fixture is positively ours to delete.

    Returns ``(owned, reason)``. The default is *not* owned: an absent tag
    read, a resource with no tags, or any mismatch all mean "leave it alone and
    tell a human". Deleting on a failed read is how a cleanup run destroys
    somebody else's resource.
    """
    if record.observed_resource_id is None:
        return False, "no observed resource id was recorded, so nothing can be verified"
    if observed_tags is None:
        return False, "ownership tags could not be read from the provider"
    if not observed_tags:
        return False, "the resource carries no tags, so ownership cannot be established"
    mismatched = [
        f"{key}={observed_tags.get(key)!r} (expected {value!r})"
        for key, value in expected_tags.items()
        if observed_tags.get(key) != value
    ]
    if mismatched:
        return False, "ownership tag mismatch: " + "; ".join(sorted(mismatched))
    return True, "ownership tags match this qualification"


def restore_inventory(
    artifact_directory: str | Path,
    qualification_id: str,
    restore_root: str | Path,
    environment: str | None = None,
) -> Path:
    """Bring a previous run's inventory into this run's artifact directory.

    A ``--resume`` or ``--cleanup`` dispatch is a *separate* workflow run with an
    empty workspace: the inventory the original run wrote is not there, so its
    fixtures would be unreachable and uncleanable. The workflow downloads the
    original run's artifact and this copies the verified inventory into place.

    ``restore_root`` is the downloaded artifact tree. The file is verified with
    :meth:`Inventory.load` — version, ``managed_by``, qualification id and
    environment — *before* it is installed, so a foreign or mismatched archive is
    refused rather than adopted. Returns the path written.
    """
    source_root = Path(restore_root).expanduser().resolve()
    if not source_root.is_dir():
        raise InventoryError(f"restore root does not exist or is not a directory: {source_root}")

    # Locate the inventory inside the downloaded tree. An artifact may unpack
    # either as `<root>/<qual-id>/inventory.json` or with the artifact directory
    # nested one or more levels down, so search rather than assume one layout.
    candidates = sorted(
        path
        for path in source_root.rglob(INVENTORY_FILENAME)
        if path.is_file() and path.parent.name == qualification_id
    )
    if not candidates:
        raise InventoryError(
            f"no {INVENTORY_FILENAME} for qualification {qualification_id!r} was found under "
            f"{source_root}; the originating run's artifact must be restored before resume or cleanup"
        )
    if len(candidates) > 1:
        raise InventoryError(
            f"found {len(candidates)} inventories for qualification {qualification_id!r} under "
            f"{source_root}; refusing to guess which one is authoritative: "
            f"{', '.join(str(c) for c in candidates)}"
        )
    source = candidates[0]

    # Validate BEFORE installing. `load` performs the foreign/version/environment
    # checks, so an archive from another tool or environment never lands on disk
    # where a later load would trust it.
    Inventory.load(source.parent.parent, qualification_id, environment)

    destination = inventory_path(artifact_directory, qualification_id)
    if destination.exists():
        # The current run already has one. Adopting a downloaded copy over it
        # could silently roll back deletions this run already recorded.
        existing = Inventory.load(artifact_directory, qualification_id, environment)
        raise InventoryError(
            f"an inventory for qualification {qualification_id!r} already exists at {destination} "
            f"with {len(existing.fixtures)} fixture(s); refusing to overwrite it with a restored copy"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    # Re-serialize through the validated object rather than copying bytes, so
    # nothing unvalidated in the archive survives into this run's artifact.
    restored = Inventory.load(source.parent.parent, qualification_id, environment)
    restored.path = destination
    return restored.flush()


def sanitize_evidence(entries: Iterable[FixtureRecord]) -> list[dict[str, Any]]:
    """Reduce records to evidence safe to keep after cleanup.

    Identities and resource ids are retained because they are what makes a leak
    investigable; the provider idempotency token is dropped since it has no
    forensic value once the resource is gone.
    """
    evidence = []
    for record in entries:
        body = record.to_json()
        body.pop("idempotency_token", None)
        evidence.append(body)
    return evidence
