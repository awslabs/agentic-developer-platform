"""Unit tests for the asset type registry.

Issue #2045: Relocated from agent-context into gateway tests.
Original: Issue #1797 (Story H of E10 #1736).

Tests cover:
- Registry contains expected types with correct steps
- is_valid_asset_type for known and unknown types
- validate_source_ref pattern matching per type
- get_steps returns correct steps and raises on unknown type
- list_asset_types returns all registered types
"""

from __future__ import annotations

import pytest

from src.knowledge.type_registry import (
    ASSET_TYPE_REGISTRY,
    get_steps,
    get_type_config,
    is_valid_asset_type,
    list_asset_types,
    validate_source_ref,
)


class TestAssetTypeRegistry:
    """Tests for the ASSET_TYPE_REGISTRY dict structure."""

    def test_registry_contains_repo(self):
        """Repo type is registered with expected steps."""
        assert "repo" in ASSET_TYPE_REGISTRY
        config = ASSET_TYPE_REGISTRY["repo"]
        assert config["steps"] == ["s3_upload", "cgc", "deepwiki", "graphrag"]
        assert config["timeout"] == 900
        assert config["requires_github_app"] is True

    def test_registry_contains_url(self):
        """URL type is registered with expected steps."""
        assert "url" in ASSET_TYPE_REGISTRY
        config = ASSET_TYPE_REGISTRY["url"]
        assert config["steps"] == ["s3_upload", "graphrag"]
        assert config["timeout"] == 600
        assert config["requires_github_app"] is False

    def test_registry_contains_doc(self):
        """Doc type is registered with expected steps."""
        assert "doc" in ASSET_TYPE_REGISTRY
        config = ASSET_TYPE_REGISTRY["doc"]
        assert config["steps"] == ["s3_upload", "graphrag"]
        assert config["timeout"] == 300
        assert config["requires_github_app"] is False

    def test_all_types_have_required_keys(self):
        """Every registered type has steps, timeout, source_ref_pattern, requires_github_app."""
        required_keys = {"steps", "timeout", "source_ref_pattern", "requires_github_app"}
        for asset_type, config in ASSET_TYPE_REGISTRY.items():
            missing = required_keys - set(config.keys())
            assert not missing, f"Type '{asset_type}' missing keys: {missing}"


class TestIsValidAssetType:
    """Tests for is_valid_asset_type()."""

    def test_known_types_are_valid(self):
        assert is_valid_asset_type("repo") is True
        assert is_valid_asset_type("url") is True
        assert is_valid_asset_type("doc") is True

    def test_unknown_type_is_invalid(self):
        assert is_valid_asset_type("confluence") is False
        assert is_valid_asset_type("") is False
        assert is_valid_asset_type("REPO") is False  # Case-sensitive


class TestValidateSourceRef:
    """Tests for validate_source_ref() pattern matching."""

    def test_repo_github_https(self):
        assert validate_source_ref("repo", "https://github.com/acme/my-service") is True
        assert validate_source_ref("repo", "https://github.com/org/repo.git") is True

    def test_repo_github_ssh(self):
        assert validate_source_ref("repo", "git@github.com:acme/my-service") is True
        assert validate_source_ref("repo", "git@github.com:org/repo.git") is True

    def test_repo_rejects_non_github(self):
        assert validate_source_ref("repo", "https://gitlab.com/acme/repo") is False
        assert validate_source_ref("repo", "http://github.com/acme/repo") is False
        assert validate_source_ref("repo", "s3://bucket/path") is False

    def test_url_accepts_http_https(self):
        assert validate_source_ref("url", "https://docs.example.com/page") is True
        assert validate_source_ref("url", "http://internal.wiki.dev/page") is True

    def test_url_rejects_non_http(self):
        assert validate_source_ref("url", "s3://bucket/key") is False
        assert validate_source_ref("url", "ftp://server/file") is False

    def test_doc_accepts_s3(self):
        assert validate_source_ref("doc", "s3://my-bucket/docs/file.pdf") is True
        assert validate_source_ref("doc", "s3://bucket/path") is True

    def test_doc_rejects_non_s3(self):
        assert validate_source_ref("doc", "https://example.com/file.pdf") is False
        assert validate_source_ref("doc", "/local/path/file.txt") is False

    def test_unknown_type_returns_false(self):
        assert validate_source_ref("unknown", "https://anything.com") is False


class TestGetSteps:
    """Tests for get_steps()."""

    def test_returns_steps_for_known_types(self):
        assert get_steps("repo") == ["s3_upload", "cgc", "deepwiki", "graphrag"]
        assert get_steps("url") == ["s3_upload", "graphrag"]
        assert get_steps("doc") == ["s3_upload", "graphrag"]

    def test_raises_for_unknown_type(self):
        with pytest.raises(KeyError):
            get_steps("unknown")


class TestGetTypeConfig:
    """Tests for get_type_config()."""

    def test_returns_config_for_known(self):
        config = get_type_config("repo")
        assert config is not None
        assert "steps" in config

    def test_returns_none_for_unknown(self):
        assert get_type_config("unknown") is None


class TestListAssetTypes:
    """Tests for list_asset_types()."""

    def test_returns_all_types(self):
        types = list_asset_types()
        assert set(types) == {"repo", "url", "doc"}
