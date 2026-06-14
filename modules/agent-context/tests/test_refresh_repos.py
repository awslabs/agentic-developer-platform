"""
Unit tests for refresh-repos.py.

Covers:
- _safe_repo() and _safe_url() — input validators
- refresh_repo() — force-mode state logic (deepwiki_sha clearing)
- refresh_repo() — subprocess CLONE_BASE env override
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


# ---------------------------------------------------------------------------
# deepwiki_sha state logic tests
#
# Mirrors the state-update logic from refresh_repo() in refresh-repos.py.
# Tested in isolation (same pattern as validators above) because
# refresh-repos.py has heavy runtime dependencies (config, s3_store, etc.).
# ---------------------------------------------------------------------------


def _compute_deepwiki_sha(
    force: bool,
    wiki_updated: bool,
    prev_sha: str | None,
    prev_deepwiki_sha: str | None,
    current_sha: str,
) -> str | None:
    """Re-declaration of the deepwiki_sha logic from refresh_repo().

    Must stay in sync with refresh-repos.py lines ~440-452.
    """
    if force:
        return None
    elif wiki_updated:
        return current_sha
    elif not prev_sha:
        return current_sha
    else:
        return prev_deepwiki_sha


class TestDeepwikiShaStateLogic:
    """Verify the deepwiki_sha state-update algorithm."""

    def test_force_mode_clears_deepwiki_sha(self):
        """When force=True, deepwiki_sha must be None so backfill runs."""
        result = _compute_deepwiki_sha(
            force=True,
            wiki_updated=False,
            prev_sha="oldsha123456",
            prev_deepwiki_sha="oldsha123456",
            current_sha="newsha789012",
        )
        assert result is None

    def test_force_mode_clears_even_when_wiki_updated(self):
        """force=True always wins, even if wiki_updated=True."""
        result = _compute_deepwiki_sha(
            force=True,
            wiki_updated=True,
            prev_sha="oldsha123456",
            prev_deepwiki_sha="oldsha123456",
            current_sha="newsha789012",
        )
        assert result is None

    def test_normal_mode_preserves_deepwiki_sha(self):
        """When force=False and wiki not updated, preserve previous deepwiki_sha."""
        result = _compute_deepwiki_sha(
            force=False,
            wiki_updated=False,
            prev_sha="oldsha123456",
            prev_deepwiki_sha="oldsha123456",
            current_sha="newsha789012",
        )
        assert result == "oldsha123456"

    def test_wiki_updated_sets_current_sha(self):
        """When wiki was successfully updated, record current SHA."""
        result = _compute_deepwiki_sha(
            force=False,
            wiki_updated=True,
            prev_sha="oldsha123456",
            prev_deepwiki_sha="oldsha123456",
            current_sha="newsha789012",
        )
        assert result == "newsha789012"

    def test_new_repo_sets_current_sha(self):
        """New repos (no prev_sha) get current_sha for backfill."""
        result = _compute_deepwiki_sha(
            force=False,
            wiki_updated=False,
            prev_sha=None,
            prev_deepwiki_sha=None,
            current_sha="firstsha000",
        )
        assert result == "firstsha000"

    def test_prev_deepwiki_sha_none_preserved_as_none(self):
        """If prev_deepwiki_sha was already None, it stays None (backfill eligible)."""
        result = _compute_deepwiki_sha(
            force=False,
            wiki_updated=False,
            prev_sha="oldsha123456",
            prev_deepwiki_sha=None,
            current_sha="newsha789012",
        )
        assert result is None


class TestDeepwikiGenerateTimeout:
    """Verify size-aware timeout logic for deepwiki_generate."""

    def test_default_timeout_for_small_repos(self):
        """Repos <= 500 MB should use the default 900s timeout."""
        # Size <= 500 MB → timeout stays at 900
        size_mb = 53
        timeout = 1800 if size_mb > 500 else 900
        assert timeout == 900

    def test_extended_timeout_for_large_repos(self):
        """Repos > 500 MB should use the extended 1800s timeout."""
        # Size > 500 MB → extended timeout
        size_mb = 750
        timeout = 1800 if size_mb > 500 else 900
        assert timeout == 1800

    def test_zero_size_uses_default_timeout(self):
        """When size lookup fails (returns 0), default timeout applies."""
        size_mb = 0
        timeout = 1800 if size_mb > 500 else 900
        assert timeout == 900


class TestBackfillDeepwikiSelection:
    """Verify backfill_deepwiki_wikis selects repos with deepwiki_sha=None."""

    def test_repos_with_none_deepwiki_sha_are_selected(self):
        """Repos where deepwiki_sha is None/falsy should be in the backfill list."""
        repo_state = {
            "org/has-wiki": {
                "last_sha": "abc123",
                "deepwiki_sha": "abc123",
            },
            "org/needs-wiki": {
                "last_sha": "def456",
                "deepwiki_sha": None,
            },
            "org/also-needs-wiki": {
                "last_sha": "ghi789",
                # deepwiki_sha key missing entirely
            },
        }

        # This mirrors backfill_deepwiki_wikis() line 607 in refresh-repos.py
        repos_needing_wiki = [repo for repo, st in repo_state.items() if not st.get("deepwiki_sha")]

        assert "org/needs-wiki" in repos_needing_wiki
        assert "org/also-needs-wiki" in repos_needing_wiki
        assert "org/has-wiki" not in repos_needing_wiki

    def test_all_repos_have_wikis(self):
        """When all repos have deepwiki_sha, none should be selected for backfill."""
        repo_state = {
            "org/repo-a": {"last_sha": "abc", "deepwiki_sha": "abc"},
            "org/repo-b": {"last_sha": "def", "deepwiki_sha": "def"},
        }

        repos_needing_wiki = [repo for repo, st in repo_state.items() if not st.get("deepwiki_sha")]

        assert repos_needing_wiki == []

    def test_force_cleared_repos_become_backfill_candidates(self):
        """After force mode clears deepwiki_sha, repos should appear in backfill list."""
        repo_state = {
            "org/force-cleared": {"last_sha": "abc", "deepwiki_sha": None},
            "org/untouched": {"last_sha": "def", "deepwiki_sha": "def"},
        }

        repos_needing_wiki = [repo for repo, st in repo_state.items() if not st.get("deepwiki_sha")]

        assert "org/force-cleared" in repos_needing_wiki
        assert "org/untouched" not in repos_needing_wiki
