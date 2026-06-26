"""Unit tests for the bulk-upload file parser.

Issue #2045: Relocated from agent-context into gateway tests.
Original: Issue #1792 (Story C of E10 #1736).

Tests cover:
- File parsing: comments, empty lines, extended format, type inference
- MAX_FILE_SIZE_BYTES and MAX_LINES constants are enforced
- parse_bulk_file returns correct valid/rejected/counts
"""

from __future__ import annotations

from src.knowledge.bulk_parser import (
    MAX_FILE_SIZE_BYTES,
    MAX_LINES,
    infer_asset_type,
    parse_bulk_file,
)


class TestInferAssetType:
    """Tests for infer_asset_type()."""

    def test_github_https(self):
        assert infer_asset_type("https://github.com/acme/repo") == "repo"

    def test_github_ssh(self):
        assert infer_asset_type("git@github.com:acme/repo.git") == "repo"

    def test_s3_path(self):
        assert infer_asset_type("s3://my-bucket/docs/file.pdf") == "doc"

    def test_http_url(self):
        assert infer_asset_type("https://docs.aws.amazon.com/bedrock/") == "url"

    def test_unsupported_protocol(self):
        assert infer_asset_type("ftp://old-server/file") is None

    def test_bare_string(self):
        assert infer_asset_type("not-a-url") is None


class TestParseBulkFile:
    """Tests for parse_bulk_file()."""

    def test_simple_file(self):
        content = "https://github.com/acme/repo1\nhttps://github.com/acme/repo2\n"
        valid, rejected, total, skipped = parse_bulk_file(content)
        assert len(valid) == 2
        assert len(rejected) == 0
        assert total == 2
        assert skipped == 0
        assert valid[0].source_ref == "https://github.com/acme/repo1"
        assert valid[0].asset_type == "repo"
        assert valid[0].line == 1

    def test_comments_and_empty_lines(self):
        content = "# Comment\n\nhttps://github.com/acme/repo\n# Another comment\n"
        valid, rejected, total, skipped = parse_bulk_file(content)
        assert len(valid) == 1
        assert total == 4
        assert skipped == 3  # 2 comments + 1 empty

    def test_extended_format_with_display_name_and_tags(self):
        content = "https://github.com/acme/repo | My Repo | team:platform, priority:high\n"
        valid, rejected, total, skipped = parse_bulk_file(content)
        assert len(valid) == 1
        assert valid[0].display_name == "My Repo"
        assert valid[0].tags == {"team": "platform", "priority": "high"}

    def test_extended_format_display_name_only(self):
        content = "https://docs.aws.amazon.com/bedrock/ | Bedrock Docs\n"
        valid, rejected, total, skipped = parse_bulk_file(content)
        assert len(valid) == 1
        assert valid[0].display_name == "Bedrock Docs"
        assert valid[0].tags == {}
        assert valid[0].asset_type == "url"

    def test_rejected_unsupported_protocol(self):
        content = "ftp://old-server/file\n"
        valid, rejected, total, skipped = parse_bulk_file(content)
        assert len(valid) == 0
        assert len(rejected) == 1
        assert rejected[0].reason == "Cannot infer asset_type from source_ref"

    def test_mixed_file(self):
        content = (
            "# Assets\n"
            "https://github.com/acme/repo1\n"
            "s3://bucket/doc.pdf | Architecture Doc | category:design\n"
            "not-a-url\n"
            "https://docs.example.com/ | Docs\n"
        )
        valid, rejected, total, skipped = parse_bulk_file(content)
        assert len(valid) == 3
        assert len(rejected) == 1
        assert total == 5
        assert skipped == 1
        assert valid[0].asset_type == "repo"
        assert valid[1].asset_type == "doc"
        assert valid[2].asset_type == "url"


class TestConstants:
    """Verify bulk parser constants are intact after relocation."""

    def test_max_file_size(self):
        assert MAX_FILE_SIZE_BYTES == 1 * 1024 * 1024  # 1 MB

    def test_max_lines(self):
        assert MAX_LINES == 500
