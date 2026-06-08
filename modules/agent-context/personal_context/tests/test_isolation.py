"""Unit tests for personal-context owner isolation.

Validates:
- Write stamps owner_sub/tenant_id from headers, ignoring body-supplied values.
- Read as owner returns own entries; read as a different sub returns none.
- Read returns shared entries within the same tenant; NOT across tenants.
- Missing X-Owner-Sub on a personal-context op → IdentityError (maps to 403).
- Non-UUID cognito_sub rejected (no path constructed).
- Existing 5 tools behave identically when no personal-context headers are present.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from personal_context.identity import (
    CallerIdentity,
    IdentityError,
    extract_identity,
    is_valid_uuid,
    require_identity,
)
from personal_context.models import (
    EntryType,
    PersonalContextEntry,
    Visibility,
)
from personal_context.storage import (
    PersonalContextStore,
    build_entry_path,
    build_read_paths,
    validate_namespace_sub,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_uuid() -> str:
    return str(uuid.uuid4())


OWNER_A = _make_uuid()
OWNER_B = _make_uuid()
TENANT_1 = "org-acme"
TENANT_2 = "org-globex"


class FakeAGFSBackend:
    """In-memory AGFS backend for testing."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def put(self, path: str, data: dict[str, Any]) -> None:
        self._store[path] = data

    def get(self, path: str) -> dict[str, Any] | None:
        return self._store.get(path)

    def delete(self, path: str) -> None:
        self._store.pop(path, None)

    def list_prefix(self, prefix: str) -> list[dict[str, Any]]:
        return [v for k, v in self._store.items() if k.startswith(prefix)]


@pytest.fixture
def backend() -> FakeAGFSBackend:
    return FakeAGFSBackend()


@pytest.fixture
def store(backend: FakeAGFSBackend) -> PersonalContextStore:
    return PersonalContextStore(backend)


@pytest.fixture
def identity_a() -> CallerIdentity:
    return CallerIdentity(owner_sub=OWNER_A, tenant_id=TENANT_1)


@pytest.fixture
def identity_b() -> CallerIdentity:
    return CallerIdentity(owner_sub=OWNER_B, tenant_id=TENANT_1)


@pytest.fixture
def identity_other_tenant() -> CallerIdentity:
    return CallerIdentity(owner_sub=OWNER_B, tenant_id=TENANT_2)


# ---------------------------------------------------------------------------
# Identity extraction tests
# ---------------------------------------------------------------------------


class TestIdentityExtraction:
    """Tests for header extraction and validation."""

    def test_both_headers_present_valid(self) -> None:
        """Valid UUID owner_sub + tenant_id → CallerIdentity."""
        sub = _make_uuid()
        headers = {"X-Owner-Sub": sub, "X-Tenant-Id": "org-test"}
        identity = extract_identity(headers)
        assert identity is not None
        assert identity.owner_sub == sub.lower()
        assert identity.tenant_id == "org-test"

    def test_no_headers_returns_none(self) -> None:
        """No personal-context headers → None (existing tools unaffected)."""
        identity = extract_identity({})
        assert identity is None

    def test_no_headers_empty_strings_returns_none(self) -> None:
        """Empty string headers → None (not a personal-context request)."""
        headers = {"X-Owner-Sub": "", "X-Tenant-Id": ""}
        identity = extract_identity(headers)
        assert identity is None

    def test_missing_owner_sub_raises(self) -> None:
        """X-Tenant-Id present but X-Owner-Sub missing → IdentityError (403)."""
        headers = {"X-Tenant-Id": "org-test"}
        with pytest.raises(IdentityError, match="Missing X-Owner-Sub"):
            extract_identity(headers)

    def test_missing_tenant_id_raises(self) -> None:
        """X-Owner-Sub present but X-Tenant-Id missing → IdentityError (403)."""
        headers = {"X-Owner-Sub": _make_uuid()}
        with pytest.raises(IdentityError, match="Missing X-Tenant-Id"):
            extract_identity(headers)

    def test_non_uuid_owner_sub_raises(self) -> None:
        """Non-UUID cognito_sub → IdentityError (no path constructed)."""
        headers = {"X-Owner-Sub": "not-a-uuid", "X-Tenant-Id": "org-test"}
        with pytest.raises(IdentityError, match="must be a valid UUID"):
            extract_identity(headers)

    def test_path_traversal_attempt_rejected(self) -> None:
        """Path traversal in owner_sub → IdentityError."""
        headers = {
            "X-Owner-Sub": "../../etc/passwd",
            "X-Tenant-Id": "org-test",
        }
        with pytest.raises(IdentityError, match="must be a valid UUID"):
            extract_identity(headers)

    def test_case_insensitive_header_names(self) -> None:
        """Header names are matched case-insensitively."""
        sub = _make_uuid()
        headers = {"x-owner-sub": sub, "x-tenant-id": "org-test"}
        identity = extract_identity(headers)
        assert identity is not None
        assert identity.owner_sub == sub.lower()

    def test_require_identity_no_headers_raises(self) -> None:
        """require_identity with no headers → IdentityError (fail-closed)."""
        with pytest.raises(IdentityError, match="required"):
            require_identity({})

    def test_uuid_validation(self) -> None:
        """is_valid_uuid accepts valid and rejects invalid formats."""
        assert is_valid_uuid("550e8400-e29b-41d4-a716-446655440000")
        assert is_valid_uuid("550E8400-E29B-41D4-A716-446655440000")
        assert not is_valid_uuid("not-a-uuid")
        assert not is_valid_uuid("")
        assert not is_valid_uuid("550e8400e29b41d4a716446655440000")  # no dashes


# ---------------------------------------------------------------------------
# Write stamp tests (anti-spoof)
# ---------------------------------------------------------------------------


class TestWriteStamping:
    """Write operations force-stamp owner_sub/tenant_id from identity headers."""

    def test_write_stamps_owner_from_headers(
        self, store: PersonalContextStore, identity_a: CallerIdentity
    ) -> None:
        """Body-supplied owner_sub is overwritten by header identity."""
        spoofed_sub = _make_uuid()
        entry_data = {
            "id": "01HXYZ",
            "type": "learning",
            "owner_sub": spoofed_sub,  # Attempt to spoof
            "tenant_id": "spoofed-org",  # Attempt to spoof
            "content": "test content",
        }
        entry = store.write_entry(identity_a, entry_data)

        # Force-stamped from identity, not from body
        assert entry.owner_sub == identity_a.owner_sub
        assert entry.tenant_id == identity_a.tenant_id
        assert entry.owner_sub != spoofed_sub
        assert entry.tenant_id != "spoofed-org"

    def test_write_stores_at_correct_path(
        self,
        store: PersonalContextStore,
        identity_a: CallerIdentity,
        backend: FakeAGFSBackend,
    ) -> None:
        """Written entry is stored at the namespace-scoped AGFS path."""
        entry_data = {
            "id": "01ABC",
            "type": "learning",
            "owner_sub": "",
            "tenant_id": "",
            "content": "my learning",
        }
        store.write_entry(identity_a, entry_data)
        expected_path = f"/personal/{identity_a.owner_sub}/learnings/01ABC.json"
        assert backend.get(expected_path) is not None
        stored = backend.get(expected_path)
        assert stored["owner_sub"] == identity_a.owner_sub

    def test_write_shared_entry_path(
        self,
        store: PersonalContextStore,
        identity_a: CallerIdentity,
        backend: FakeAGFSBackend,
    ) -> None:
        """Shared entries are stored under /shared/<tenant_id>/."""
        entry_data = {
            "id": "01SHARED",
            "type": "pattern",
            "owner_sub": "",
            "tenant_id": "",
            "visibility": "shared",
            "content": "shared pattern",
        }
        store.write_entry(identity_a, entry_data)
        expected_path = f"/shared/{identity_a.tenant_id}/patterns/01SHARED.json"
        assert backend.get(expected_path) is not None


# ---------------------------------------------------------------------------
# Read isolation tests
# ---------------------------------------------------------------------------


class TestReadIsolation:
    """Read operations enforce owner/visibility filtering."""

    def test_owner_reads_own_private_entry(
        self, store: PersonalContextStore, identity_a: CallerIdentity
    ) -> None:
        """Owner can read their own private entries."""
        entry_data = {
            "id": "01OWN",
            "type": "learning",
            "owner_sub": "",
            "tenant_id": "",
            "content": "my private learning",
            "visibility": "private",
        }
        store.write_entry(identity_a, entry_data)

        entries = store.list_entries(identity_a, entry_type=EntryType.learning)
        assert len(entries) == 1
        assert entries[0].id == "01OWN"
        assert entries[0].content == "my private learning"

    def test_different_sub_sees_zero_private_entries(
        self,
        store: PersonalContextStore,
        identity_a: CallerIdentity,
        identity_b: CallerIdentity,
    ) -> None:
        """A different sub cannot read another user's private entries."""
        entry_data = {
            "id": "01PRIVATE",
            "type": "learning",
            "owner_sub": "",
            "tenant_id": "",
            "content": "secret stuff",
            "visibility": "private",
        }
        store.write_entry(identity_a, entry_data)

        # identity_b tries to list — should see zero of identity_a's private entries
        entries = store.list_entries(identity_b, entry_type=EntryType.learning)
        # identity_b's private prefix won't contain identity_a's entries
        a_entries = [e for e in entries if e.owner_sub == identity_a.owner_sub]
        assert len(a_entries) == 0

    def test_shared_visible_within_same_tenant(
        self,
        store: PersonalContextStore,
        identity_a: CallerIdentity,
        identity_b: CallerIdentity,
    ) -> None:
        """Shared entries are visible to other users in the same tenant."""
        entry_data = {
            "id": "01TEAMSHARED",
            "type": "pattern",
            "owner_sub": "",
            "tenant_id": "",
            "visibility": "shared",
            "content": "team pattern",
        }
        store.write_entry(identity_a, entry_data)

        # identity_b (same tenant) can see shared entries
        entries = store.list_entries(identity_b, entry_type=EntryType.pattern)
        shared_entries = [e for e in entries if e.id == "01TEAMSHARED"]
        assert len(shared_entries) == 1
        assert shared_entries[0].content == "team pattern"

    def test_shared_not_visible_across_tenants(
        self,
        store: PersonalContextStore,
        identity_a: CallerIdentity,
        identity_other_tenant: CallerIdentity,
    ) -> None:
        """Shared entries are NOT visible to users in a different tenant."""
        entry_data = {
            "id": "01CROSSORG",
            "type": "synthesis",
            "owner_sub": "",
            "tenant_id": "",
            "visibility": "shared",
            "content": "org-specific synthesis",
        }
        store.write_entry(identity_a, entry_data)

        # identity_other_tenant (different tenant) should NOT see this
        entries = store.list_entries(identity_other_tenant, entry_type=EntryType.synthesis)
        cross_entries = [e for e in entries if e.id == "01CROSSORG"]
        assert len(cross_entries) == 0

    def test_none_identity_returns_empty(
        self, store: PersonalContextStore, identity_a: CallerIdentity
    ) -> None:
        """Null identity (fail-closed) → zero results, never all."""
        entry_data = {
            "id": "01NOLEAK",
            "type": "learning",
            "owner_sub": "",
            "tenant_id": "",
            "content": "should not leak",
        }
        store.write_entry(identity_a, entry_data)

        # None identity must return zero
        entries = store.list_entries(None)
        assert entries == []

    def test_read_entry_by_path_enforces_ownership(
        self,
        store: PersonalContextStore,
        identity_a: CallerIdentity,
        identity_b: CallerIdentity,
    ) -> None:
        """Direct path read enforces ownership check."""
        entry_data = {
            "id": "01DIRECT",
            "type": "learning",
            "owner_sub": "",
            "tenant_id": "",
            "content": "direct read test",
            "visibility": "private",
        }
        entry = store.write_entry(identity_a, entry_data)
        path = build_entry_path(entry)

        # Owner can read
        result = store.read_entry(identity_a, path)
        assert result is not None
        assert result.id == "01DIRECT"

        # Non-owner cannot read
        result = store.read_entry(identity_b, path)
        assert result is None


# ---------------------------------------------------------------------------
# Namespace path validation tests
# ---------------------------------------------------------------------------


class TestNamespaceValidation:
    """Validate cognito_sub format before any path construction."""

    def test_valid_uuid_accepted(self) -> None:
        """Valid UUID passes validation."""
        sub = _make_uuid()
        result = validate_namespace_sub(sub)
        assert result == sub.lower()

    def test_non_uuid_rejected(self) -> None:
        """Non-UUID string raises ValueError (no path constructed)."""
        with pytest.raises(ValueError, match="must be a valid UUID"):
            validate_namespace_sub("not-a-uuid")

    def test_path_traversal_rejected(self) -> None:
        """Path traversal attempt raises ValueError."""
        with pytest.raises(ValueError, match="must be a valid UUID"):
            validate_namespace_sub("../../../etc/passwd")

    def test_empty_string_rejected(self) -> None:
        """Empty string raises ValueError."""
        with pytest.raises(ValueError, match="must be a valid UUID"):
            validate_namespace_sub("")

    def test_uuid_without_dashes_rejected(self) -> None:
        """UUID without dashes is rejected (strict format)."""
        with pytest.raises(ValueError, match="must be a valid UUID"):
            validate_namespace_sub("550e8400e29b41d4a716446655440000")


# ---------------------------------------------------------------------------
# Entry model validation tests
# ---------------------------------------------------------------------------


class TestEntryModel:
    """Tests for PersonalContextEntry validation."""

    def test_valid_entry(self) -> None:
        """A well-formed entry passes validation."""
        entry = PersonalContextEntry(
            id="01TEST",
            type=EntryType.learning,
            owner_sub=_make_uuid(),
            tenant_id="org-test",
            content="valid entry",
        )
        assert entry.visibility == Visibility.private  # default
        assert entry.confidence == 0.7  # default

    def test_invalid_owner_sub_rejected(self) -> None:
        """Entry with non-UUID owner_sub fails validation."""
        with pytest.raises(ValueError, match="must be a valid UUID"):
            PersonalContextEntry(
                id="01BAD",
                type=EntryType.learning,
                owner_sub="bad-sub",
                tenant_id="org-test",
            )


# ---------------------------------------------------------------------------
# Existing tools regression (no headers = pass-through)
# ---------------------------------------------------------------------------


class TestExistingToolsUnaffected:
    """Existing 5 tools behave identically when no personal-context headers present.

    When headers are absent, extract_identity returns None, and the middleware
    should pass through without blocking — no 403, no filtering.
    """

    def test_no_headers_identity_is_none(self) -> None:
        """extract_identity({}) → None: middleware takes no action."""
        assert extract_identity({}) is None

    def test_unrelated_headers_identity_is_none(self) -> None:
        """Unrelated headers do not trigger identity extraction."""
        headers = {
            "Authorization": "Bearer some-token",
            "Content-Type": "application/json",
            "X-Request-Id": "abc123",
        }
        assert extract_identity(headers) is None

    def test_existing_mcp_tools_not_blocked(self) -> None:
        """Simulates existing tool call with no identity headers.

        The identity layer returns None, meaning the middleware should
        NOT block the request — existing tools (search, understand,
        impact, browse, remember) proceed as before.
        """
        # Simulate: request comes in for 'search' tool, no personal-context headers
        request_headers: dict[str, str] = {"Content-Type": "application/json"}
        identity = extract_identity(request_headers)

        # Identity is None → middleware does NOT interfere
        assert identity is None
        # This means the request proceeds to the existing 5 tools normally


# ---------------------------------------------------------------------------
# Path construction tests
# ---------------------------------------------------------------------------


class TestPathConstruction:
    """Tests for AGFS path building."""

    def test_private_learning_path(self) -> None:
        sub = _make_uuid()
        entry = PersonalContextEntry(
            id="01PATH",
            type=EntryType.learning,
            owner_sub=sub,
            tenant_id="org-test",
            visibility=Visibility.private,
        )
        path = build_entry_path(entry)
        assert path == f"/personal/{sub}/learnings/01PATH.json"

    def test_private_synthesis_path(self) -> None:
        sub = _make_uuid()
        entry = PersonalContextEntry(
            id="02PATH",
            type=EntryType.synthesis,
            owner_sub=sub,
            tenant_id="org-test",
            visibility=Visibility.private,
        )
        path = build_entry_path(entry)
        assert path == f"/personal/{sub}/syntheses/02PATH.json"

    def test_shared_pattern_path(self) -> None:
        sub = _make_uuid()
        entry = PersonalContextEntry(
            id="03PATH",
            type=EntryType.pattern,
            owner_sub=sub,
            tenant_id="org-acme",
            visibility=Visibility.shared,
        )
        path = build_entry_path(entry)
        assert path == "/shared/org-acme/patterns/03PATH.json"

    def test_read_paths_include_private_and_shared(self) -> None:
        identity = CallerIdentity(owner_sub=_make_uuid(), tenant_id="org-test")
        paths = build_read_paths(identity, entry_type=EntryType.learning)
        assert len(paths) == 2
        assert any("/personal/" in p and "/learnings/" in p for p in paths)
        assert any("/shared/" in p and "/learnings/" in p for p in paths)

    def test_read_paths_all_types(self) -> None:
        identity = CallerIdentity(owner_sub=_make_uuid(), tenant_id="org-test")
        paths = build_read_paths(identity)
        # 3 types × 2 (private + shared) = 6 paths
        assert len(paths) == 6
