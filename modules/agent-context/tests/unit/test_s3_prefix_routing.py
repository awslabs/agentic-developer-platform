"""Unit tests for S3 prefix routing based on ingestion scope.

Tests the scope module's compute_s3_prefix() function and the
parse_scope_from_env() helper that reads scope from environment variables.

Issue #1773 (Child 4 of #1721 — Tenant Isolation).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add ingestion source to path for import
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "images" / "ingestion"))

from scope import (  # noqa: E402
    DEFAULT_SCOPE,
    IngestionScope,
    compute_s3_prefix,
    parse_scope,
    parse_scope_from_env,
)


# ---------------------------------------------------------------------------
# compute_s3_prefix tests
# ---------------------------------------------------------------------------


class TestComputeS3Prefix:
    """Tests for compute_s3_prefix() — resolving S3 key prefixes."""

    # --- Shared scope (no change) ---

    def test_shared_returns_base_prefix_unchanged(self):
        """Shared scope returns the base prefix unchanged."""
        scope = IngestionScope()
        assert compute_s3_prefix(scope, "content/wikis") == "content/wikis"

    def test_shared_code_index_prefix(self):
        """Shared scope returns base code-index prefix unchanged."""
        scope = IngestionScope()
        assert compute_s3_prefix(scope, "content/code-indexes") == "content/code-indexes"

    def test_shared_sbom_prefix(self):
        """Shared scope returns base SBOM prefix unchanged."""
        scope = IngestionScope()
        assert compute_s3_prefix(scope, "sbom") == "sbom"

    def test_shared_content_prefix(self):
        """Shared scope returns content prefix unchanged."""
        scope = IngestionScope()
        assert compute_s3_prefix(scope, "content") == "content"

    # --- Tenant scope ---

    def test_tenant_wiki_prefix(self):
        """Tenant scope routes wikis under tenants/{id}/wikis."""
        scope = IngestionScope(tenant_id="acme-corp", visibility="tenant")
        assert compute_s3_prefix(scope, "content/wikis") == "tenants/acme-corp/wikis"

    def test_tenant_code_index_prefix(self):
        """Tenant scope routes code-indexes under tenants/{id}/code-indexes."""
        scope = IngestionScope(tenant_id="acme-corp", visibility="tenant")
        assert compute_s3_prefix(scope, "content/code-indexes") == "tenants/acme-corp/code-indexes"

    def test_tenant_sbom_prefix(self):
        """Tenant scope routes SBOM under tenants/{id}/sbom."""
        scope = IngestionScope(tenant_id="acme-corp", visibility="tenant")
        assert compute_s3_prefix(scope, "sbom") == "tenants/acme-corp/sbom"

    def test_tenant_content_prefix(self):
        """Tenant scope with bare 'content' prefix."""
        scope = IngestionScope(tenant_id="acme-corp", visibility="tenant")
        result = compute_s3_prefix(scope, "content")
        assert result == "tenants/acme-corp/content"

    # --- Personal scope ---

    def test_personal_wiki_prefix(self):
        """Personal scope routes wikis under users/{sub}/wikis."""
        scope = IngestionScope(owner_sub="user-xyz-789", visibility="personal")
        assert compute_s3_prefix(scope, "content/wikis") == "users/user-xyz-789/wikis"

    def test_personal_code_index_prefix(self):
        """Personal scope routes code-indexes under users/{sub}/code-indexes."""
        scope = IngestionScope(owner_sub="user-xyz-789", visibility="personal")
        assert compute_s3_prefix(scope, "content/code-indexes") == "users/user-xyz-789/code-indexes"

    def test_personal_sbom_prefix(self):
        """Personal scope routes SBOM under users/{sub}/sbom."""
        scope = IngestionScope(owner_sub="user-xyz-789", visibility="personal")
        assert compute_s3_prefix(scope, "sbom") == "users/user-xyz-789/sbom"

    # --- Edge cases ---

    def test_trailing_slash_stripped(self):
        """Trailing slash in base_prefix is stripped."""
        scope = IngestionScope()
        assert compute_s3_prefix(scope, "content/wikis/") == "content/wikis"

    def test_tenant_with_trailing_slash(self):
        """Trailing slash handled correctly in tenant scope."""
        scope = IngestionScope(tenant_id="acme", visibility="tenant")
        assert compute_s3_prefix(scope, "content/wikis/") == "tenants/acme/wikis"

    def test_tenant_id_with_special_chars(self):
        """Tenant IDs with hyphens and underscores are preserved."""
        scope = IngestionScope(tenant_id="my-org_123", visibility="tenant")
        assert compute_s3_prefix(scope, "content/wikis") == "tenants/my-org_123/wikis"

    def test_owner_sub_uuid_format(self):
        """UUID-style owner_sub is preserved correctly."""
        scope = IngestionScope(
            owner_sub="550e8400-e29b-41d4-a716-446655440000", visibility="personal"
        )
        assert (
            compute_s3_prefix(scope, "content/wikis")
            == "users/550e8400-e29b-41d4-a716-446655440000/wikis"
        )


# ---------------------------------------------------------------------------
# parse_scope_from_env tests
# ---------------------------------------------------------------------------


class TestParseScopeFromEnv:
    """Tests for parse_scope_from_env() — reading scope from environment."""

    def test_no_env_vars_returns_shared(self):
        """Missing env vars default to shared scope."""
        env = {}
        with patch.dict(os.environ, env, clear=True):
            scope = parse_scope_from_env()
        assert scope.is_shared
        assert scope.tenant_id is None
        assert scope.owner_sub is None

    def test_shared_visibility_explicit(self):
        """Explicit shared visibility from env."""
        env = {"INGESTION_SCOPE_VISIBILITY": "shared"}
        with patch.dict(os.environ, env, clear=True):
            scope = parse_scope_from_env()
        assert scope.is_shared

    def test_tenant_scope_from_env(self):
        """Tenant scope read correctly from env vars."""
        env = {
            "INGESTION_SCOPE_VISIBILITY": "tenant",
            "INGESTION_SCOPE_TENANT_ID": "acme-corp",
            "INGESTION_SCOPE_OWNER_SUB": "",
            "INGESTION_SCOPE_PROJECT_ID": "proj-123",
        }
        with patch.dict(os.environ, env, clear=True):
            scope = parse_scope_from_env()
        assert scope.is_tenant
        assert scope.tenant_id == "acme-corp"
        assert scope.owner_sub is None  # Empty string normalizes to None
        assert scope.project_id == "proj-123"

    def test_personal_scope_from_env(self):
        """Personal scope read correctly from env vars."""
        env = {
            "INGESTION_SCOPE_VISIBILITY": "personal",
            "INGESTION_SCOPE_TENANT_ID": "acme-corp",
            "INGESTION_SCOPE_OWNER_SUB": "user-abc-123",
        }
        with patch.dict(os.environ, env, clear=True):
            scope = parse_scope_from_env()
        assert scope.is_personal
        assert scope.owner_sub == "user-abc-123"

    def test_tenant_without_id_falls_back_shared(self):
        """Tenant visibility without tenant_id falls back to shared."""
        env = {
            "INGESTION_SCOPE_VISIBILITY": "tenant",
            "INGESTION_SCOPE_TENANT_ID": "",
        }
        with patch.dict(os.environ, env, clear=True):
            scope = parse_scope_from_env()
        assert scope.is_shared

    def test_personal_without_owner_sub_falls_back_shared(self):
        """Personal visibility without owner_sub falls back to shared."""
        env = {
            "INGESTION_SCOPE_VISIBILITY": "personal",
            "INGESTION_SCOPE_OWNER_SUB": "",
        }
        with patch.dict(os.environ, env, clear=True):
            scope = parse_scope_from_env()
        assert scope.is_shared

    def test_invalid_visibility_falls_back_shared(self):
        """Unknown visibility value defaults to shared."""
        env = {"INGESTION_SCOPE_VISIBILITY": "unknown_value"}
        with patch.dict(os.environ, env, clear=True):
            scope = parse_scope_from_env()
        assert scope.is_shared


# ---------------------------------------------------------------------------
# IngestionScope dataclass tests
# ---------------------------------------------------------------------------


class TestIngestionScopeProperties:
    """Tests for IngestionScope boolean properties and to_env()."""

    def test_default_scope_is_shared(self):
        """Default IngestionScope() is shared."""
        scope = IngestionScope()
        assert scope.is_shared is True
        assert scope.is_tenant is False
        assert scope.is_personal is False

    def test_tenant_scope_properties(self):
        """Tenant scope has correct boolean properties."""
        scope = IngestionScope(tenant_id="t1", visibility="tenant")
        assert scope.is_shared is False
        assert scope.is_tenant is True
        assert scope.is_personal is False

    def test_personal_scope_properties(self):
        """Personal scope has correct boolean properties."""
        scope = IngestionScope(owner_sub="u1", visibility="personal")
        assert scope.is_shared is False
        assert scope.is_tenant is False
        assert scope.is_personal is True

    def test_scope_is_frozen(self):
        """IngestionScope is immutable (frozen dataclass)."""
        scope = IngestionScope()
        with pytest.raises(Exception):  # FrozenInstanceError
            scope.visibility = "tenant"  # type: ignore[misc]

    def test_to_env_shared(self):
        """to_env() for shared scope."""
        scope = IngestionScope()
        env = scope.to_env()
        assert env["INGESTION_SCOPE_VISIBILITY"] == "shared"
        assert env["INGESTION_SCOPE_TENANT_ID"] == ""
        assert env["INGESTION_SCOPE_OWNER_SUB"] == ""
        assert env["INGESTION_SCOPE_PROJECT_ID"] == ""

    def test_to_env_tenant(self):
        """to_env() for tenant scope preserves values."""
        scope = IngestionScope(
            tenant_id="acme", owner_sub="user-1", project_id="proj-x", visibility="tenant"
        )
        env = scope.to_env()
        assert env["INGESTION_SCOPE_VISIBILITY"] == "tenant"
        assert env["INGESTION_SCOPE_TENANT_ID"] == "acme"
        assert env["INGESTION_SCOPE_OWNER_SUB"] == "user-1"
        assert env["INGESTION_SCOPE_PROJECT_ID"] == "proj-x"

    def test_to_env_roundtrip(self):
        """to_env() -> parse_scope_from_env() round-trips correctly."""
        original = IngestionScope(tenant_id="acme", visibility="tenant", project_id="proj-1")
        env = original.to_env()
        with patch.dict(os.environ, env, clear=True):
            restored = parse_scope_from_env()
        assert restored.tenant_id == original.tenant_id
        assert restored.visibility == original.visibility
        assert restored.project_id == original.project_id


# ---------------------------------------------------------------------------
# parse_scope validation tests (extended for #1773)
# ---------------------------------------------------------------------------


class TestParseScopeValidation:
    """Tests for parse_scope() validation — ensures invalid input defaults safely."""

    def test_tenant_missing_tenant_id_returns_shared(self):
        """Tenant visibility without tenant_id falls back to shared."""
        raw = {"visibility": "tenant", "tenant_id": None}
        scope = parse_scope(raw)
        assert scope.is_shared

    def test_tenant_empty_tenant_id_returns_shared(self):
        """Tenant visibility with empty tenant_id falls back to shared."""
        raw = {"visibility": "tenant", "tenant_id": ""}
        scope = parse_scope(raw)
        assert scope.is_shared

    def test_personal_missing_owner_sub_returns_shared(self):
        """Personal visibility without owner_sub falls back to shared."""
        raw = {"visibility": "personal", "owner_sub": None}
        scope = parse_scope(raw)
        assert scope.is_shared

    def test_non_dict_scope_returns_default(self):
        """Non-dict raw value returns default shared scope."""
        scope = parse_scope(None)
        assert scope is DEFAULT_SCOPE

    def test_empty_dict_returns_default(self):
        """Empty dict returns default (treated as falsy by parse_scope)."""
        scope = parse_scope({})
        assert scope.is_shared
