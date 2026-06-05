"""
Unit tests for input validators in refresh-repos.py.

Covers _safe_repo() and _safe_url() — ensuring malicious inputs are rejected
while legitimate repo names and URLs pass through.
"""

from __future__ import annotations

import re

import pytest

# ---------------------------------------------------------------------------
# Re-declare validators here to test them in isolation without importing the
# full refresh-repos.py (which pulls requests, etc. and needs /app/).
# These must stay in sync with refresh-repos.py — the canonical definitions.
# ---------------------------------------------------------------------------

_REPO_NAME_RE = re.compile(r"^[a-zA-Z0-9._/-]+$")  # owner/name pattern
_URL_RE = re.compile(r"^https://[a-zA-Z0-9.-]+(/[a-zA-Z0-9._~!$&'()*+,;=:@%/-]*)?$")


def _safe_repo(repo: str) -> str:
    """Validate repo name before passing to subprocess."""
    if repo.startswith("-") or not _REPO_NAME_RE.match(repo):
        raise ValueError(f"refusing to ingest repo with suspicious name: {repo!r}")
    return repo


def _safe_url(url: str) -> str:
    """Validate URL before passing to subprocess."""
    if not _URL_RE.match(url):
        raise ValueError(f"refusing URL: {url!r}")
    return url


# ---------------------------------------------------------------------------
# _safe_repo tests
# ---------------------------------------------------------------------------


class TestSafeRepo:
    """Validate repo name sanitization."""

    @pytest.mark.parametrize(
        "repo",
        [
            "owner/repo",
            "owner/repo.with.dots",
            "owner-org/repo_name",
            "aws-samples/amazon-bedrock-samples",
            "strands-agents/sdk-python",
            "HKUDS/LightRAG",
            "The-Pocket/PocketFlow",
            "e2b-dev/E2B",
            "awslabs/mcp",
        ],
    )
    def test_accepts_valid_repo_names(self, repo: str):
        assert _safe_repo(repo) == repo

    @pytest.mark.parametrize(
        "repo",
        [
            "--evil",
            "-rf",
            "--repo=malicious",
            "-",
        ],
    )
    def test_rejects_leading_dash(self, repo: str):
        with pytest.raises(ValueError, match="refusing to ingest repo"):
            _safe_repo(repo)

    @pytest.mark.parametrize(
        "repo",
        [
            "repo;rm -rf /",
            "repo$(whoami)",
            "repo`id`",
            "repo|cat /etc/passwd",
            "repo & echo pwned",
            "repo\nnewline",
            "repo name with spaces",
            "repo<script>",
            "repo{curly}",
        ],
    )
    def test_rejects_shell_metacharacters(self, repo: str):
        with pytest.raises(ValueError, match="refusing to ingest repo"):
            _safe_repo(repo)

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="refusing to ingest repo"):
            _safe_repo("")


# ---------------------------------------------------------------------------
# _safe_url tests
# ---------------------------------------------------------------------------


class TestSafeUrl:
    """Validate URL sanitization."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/owner/repo",
            "https://docs.aws.amazon.com/bedrock/latest/userguide/",
            "https://strandsagents.com/latest/",
            "https://modelcontextprotocol.io/docs/",
            "https://aws.amazon.com/blogs/aws/launching-s3-files-making-s3-buckets-accessible-as-file-systems/",
        ],
    )
    def test_accepts_valid_https_urls(self, url: str):
        assert _safe_url(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com/insecure",
            "http://github.com/owner/repo",
        ],
    )
    def test_rejects_http_urls(self, url: str):
        with pytest.raises(ValueError, match="refusing URL"):
            _safe_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://evil.com/payload",
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
        ],
    )
    def test_rejects_non_https_schemes(self, url: str):
        with pytest.raises(ValueError, match="refusing URL"):
            _safe_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.com/path`id`",
            "https://evil.com/path|cat",
            "https://evil.com/path\nnewline",
            "https://evil.com/<script>",
            "https://evil.com/{curly}",
            "https://evil.com/path with spaces",
        ],
    )
    def test_rejects_urls_with_invalid_characters(self, url: str):
        with pytest.raises(ValueError, match="refusing URL"):
            _safe_url(url)

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="refusing URL"):
            _safe_url("")

    def test_rejects_leading_dash(self):
        with pytest.raises(ValueError, match="refusing URL"):
            _safe_url("--url=https://evil.com")
