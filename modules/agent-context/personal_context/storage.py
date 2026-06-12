"""Owner-scoped storage namespaces for personal context.

Provides namespace path construction, entry CRUD, and owner/visibility
read-filter logic. All entries are stored as JSON documents in an
AGFS-compatible backend (S3 or legacy OpenViking) under owner-scoped paths:

- Private: ``/personal/<cognito_sub>/{learnings,syntheses,patterns}/<ulid>.json``
- Shared:  ``/shared/<tenant_id>/{learnings,syntheses,patterns}/<ulid>.json``

The read filter enforces:
- Owner sees their own private entries.
- Owner sees shared entries within their tenant.
- A missing/empty filter returns ZERO results (never all).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .identity import CallerIdentity, is_valid_uuid
from .models import EntryType, PersonalContextEntry, Visibility

# Type-to-directory mapping (plural form for AGFS paths)
_TYPE_DIRS: dict[EntryType, str] = {
    EntryType.learning: "learnings",
    EntryType.synthesis: "syntheses",
    EntryType.pattern: "patterns",
}


def _build_private_path(owner_sub: str, entry_type: EntryType, entry_id: str) -> str:
    """Build the AGFS path for a private entry."""
    type_dir = _TYPE_DIRS[entry_type]
    return f"/personal/{owner_sub}/{type_dir}/{entry_id}.json"


def _build_shared_path(tenant_id: str, entry_type: EntryType, entry_id: str) -> str:
    """Build the AGFS path for a shared entry."""
    type_dir = _TYPE_DIRS[entry_type]
    return f"/shared/{tenant_id}/{type_dir}/{entry_id}.json"


def build_entry_path(entry: PersonalContextEntry) -> str:
    """Determine the AGFS storage path for an entry based on its visibility."""
    if entry.visibility == Visibility.shared:
        return _build_shared_path(entry.tenant_id, entry.type, entry.id)
    return _build_private_path(entry.owner_sub, entry.type, entry.id)


def build_read_paths(identity: CallerIdentity, entry_type: EntryType | None = None) -> list[str]:
    """Build the list of AGFS prefix paths a caller is allowed to read.

    Returns prefixes for:
    - All private paths owned by ``identity.owner_sub``
    - All shared paths within ``identity.tenant_id``

    If ``entry_type`` is specified, narrows to that type's directory only.
    """
    types = [entry_type] if entry_type else list(EntryType)
    paths: list[str] = []

    for t in types:
        type_dir = _TYPE_DIRS[t]
        paths.append(f"/personal/{identity.owner_sub}/{type_dir}/")
        paths.append(f"/shared/{identity.tenant_id}/{type_dir}/")

    return paths


class PersonalContextStore:
    """Owner-scoped CRUD layer for personal-context entries.

    This class manages entries in an AGFS-compatible storage backend.
    The ``backend`` parameter accepts any object implementing the
    :class:`AGFSBackend` protocol (get/put/delete/list by path prefix).

    All write operations force-stamp ``owner_sub`` and ``tenant_id`` from
    the caller identity, ignoring any client-supplied values (anti-spoof).
    All read operations apply owner/visibility filters — a missing identity
    always returns empty results.
    """

    def __init__(self, backend: Any):
        """Initialize with an AGFS-compatible backend.

        Parameters
        ----------
        backend:
            Object with ``put(path, data)``, ``get(path)``, ``delete(path)``,
            and ``list_prefix(prefix)`` methods.
        """
        self.backend = backend

    def write_entry(
        self,
        identity: CallerIdentity,
        entry_data: dict[str, Any],
    ) -> PersonalContextEntry:
        """Write a personal-context entry with owner-stamping.

        Force-stamps ``owner_sub`` and ``tenant_id`` from the identity,
        ignoring any client-supplied values in ``entry_data`` (anti-spoof).

        Parameters
        ----------
        identity:
            Validated caller identity (from headers).
        entry_data:
            Entry fields from the client. ``owner_sub`` and ``tenant_id``
            will be overwritten.

        Returns
        -------
        The stored entry.

        Raises
        ------
        ValueError
            If ``identity.owner_sub`` is not a valid UUID.
        """
        # Anti-spoof: force-stamp identity fields
        entry_data["owner_sub"] = identity.owner_sub
        entry_data["tenant_id"] = identity.tenant_id

        # Validate and construct the entry
        entry = PersonalContextEntry(**entry_data)

        # Build storage path
        path = build_entry_path(entry)

        # Store as JSON
        self.backend.put(path, entry.model_dump())

        return entry

    def read_entry(
        self,
        identity: CallerIdentity,
        path: str,
    ) -> PersonalContextEntry | None:
        """Read a single entry, enforcing ownership/visibility filter.

        Returns ``None`` if the entry doesn't exist or the caller lacks access.
        """
        data = self.backend.get(path)
        if data is None:
            return None

        entry = PersonalContextEntry(**data)

        # Enforce read filter
        if not self._caller_can_read(identity, entry):
            return None

        # Update last_accessed_at
        entry.last_accessed_at = datetime.now(timezone.utc).isoformat()
        self.backend.put(path, entry.model_dump())

        return entry

    def list_entries(
        self,
        identity: CallerIdentity | None,
        entry_type: EntryType | None = None,
    ) -> list[PersonalContextEntry]:
        """List entries visible to the caller.

        If ``identity`` is None (fail-closed), returns an empty list — never all.
        """
        # Fail-closed: no identity → zero results
        if identity is None:
            return []

        prefixes = build_read_paths(identity, entry_type)
        entries: list[PersonalContextEntry] = []

        for prefix in prefixes:
            items = self.backend.list_prefix(prefix)
            for data in items:
                try:
                    entry = PersonalContextEntry(**data)
                except Exception:
                    continue

                if self._caller_can_read(identity, entry):
                    entries.append(entry)

        return entries

    def delete_entry(
        self,
        identity: CallerIdentity,
        path: str,
    ) -> bool:
        """Delete an entry if the caller owns it.

        Returns True if deleted, False if not found or not owned.
        """
        data = self.backend.get(path)
        if data is None:
            return False

        entry = PersonalContextEntry(**data)

        # Only the owner can delete
        if entry.owner_sub != identity.owner_sub:
            return False

        self.backend.delete(path)
        return True

    @staticmethod
    def _caller_can_read(identity: CallerIdentity, entry: PersonalContextEntry) -> bool:
        """Check if a caller can read a given entry.

        Rules:
        - Owner can always read their own entries (any visibility).
        - Non-owner can read shared entries within the same tenant.
        - Everything else is denied.
        """
        # Owner always sees their own
        if entry.owner_sub == identity.owner_sub:
            return True

        # Shared entries visible within same tenant
        if entry.visibility == Visibility.shared and entry.tenant_id == identity.tenant_id:
            return True

        return False


def validate_namespace_sub(cognito_sub: str) -> str:
    """Validate and normalize a cognito_sub for use in AGFS paths.

    Returns the lowercased UUID string or raises ValueError.
    This MUST be called before constructing any filesystem path
    to prevent path-traversal attacks.
    """
    if not is_valid_uuid(cognito_sub):
        raise ValueError(
            f"cognito_sub must be a valid UUID for namespace path, got: {cognito_sub!r}"
        )
    return cognito_sub.lower().strip()
