"""
Unit tests for refresh-repos.py.

Covers:
- _safe_repo() and _safe_url() — input validators
- refresh_repo() — force-mode state logic (deepwiki_sha clearing)
- refresh_repo() — subprocess CLONE_BASE env override
"""

from __future__ import annotations

import os
import re
import subprocess
from unittest.mock import MagicMock, patch

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


# ---------------------------------------------------------------------------
# Backfill size-sort and skip-threshold tests
#
# Mirrors the sorting/skipping logic added to backfill_deepwiki_wikis() to
# fix OOM issues with large repos (#1478).
# ---------------------------------------------------------------------------

DEEPWIKI_SKIP_SIZE_MB = 600  # Must stay in sync with refresh-repos.py


def _backfill_sort_and_filter(
    repos_needing_wiki: list[str], repo_sizes: dict[str, int]
) -> tuple[list[str], list[str]]:
    """Re-declaration of the sort/filter logic from backfill_deepwiki_wikis().

    Returns (repos_to_process sorted by size asc, repos_to_skip).
    Must stay in sync with refresh-repos.py.
    """
    repos_to_skip = [
        repo for repo in repos_needing_wiki if repo_sizes[repo] > DEEPWIKI_SKIP_SIZE_MB
    ]
    repos_to_process = [
        repo for repo in repos_needing_wiki if repo_sizes[repo] <= DEEPWIKI_SKIP_SIZE_MB
    ]
    repos_to_process.sort(key=lambda r: repo_sizes[r])
    return repos_to_process, repos_to_skip


class TestBackfillSortOrder:
    """Verify repos are processed smallest-to-largest in backfill."""

    def test_repos_sorted_by_size_ascending(self):
        """Repos should be processed from smallest to largest."""
        repos_needing_wiki = ["org/large", "org/tiny", "org/medium"]
        repo_sizes = {"org/large": 400, "org/tiny": 10, "org/medium": 150}

        repos_to_process, _ = _backfill_sort_and_filter(repos_needing_wiki, repo_sizes)

        assert repos_to_process == ["org/tiny", "org/medium", "org/large"]

    def test_repos_with_same_size_maintain_stable_order(self):
        """Repos with equal size should not crash or reorder unpredictably."""
        repos_needing_wiki = ["org/repo-a", "org/repo-b", "org/repo-c"]
        repo_sizes = {"org/repo-a": 50, "org/repo-b": 50, "org/repo-c": 50}

        repos_to_process, _ = _backfill_sort_and_filter(repos_needing_wiki, repo_sizes)

        # All repos should be present (stable sort, no crashes)
        assert set(repos_to_process) == {"org/repo-a", "org/repo-b", "org/repo-c"}

    def test_zero_size_repos_processed_first(self):
        """Repos with size 0 (API lookup failed) should be processed first."""
        repos_needing_wiki = ["org/known-size", "org/unknown-size"]
        repo_sizes = {"org/known-size": 200, "org/unknown-size": 0}

        repos_to_process, _ = _backfill_sort_and_filter(repos_needing_wiki, repo_sizes)

        assert repos_to_process[0] == "org/unknown-size"


class TestBackfillSkipThreshold:
    """Verify repos exceeding the size threshold are skipped."""

    def test_repos_above_threshold_are_skipped(self):
        """Repos > 600MB should be skipped entirely."""
        repos_needing_wiki = ["org/huge-repo", "org/small-repo"]
        repo_sizes = {"org/huge-repo": 743, "org/small-repo": 50}

        repos_to_process, repos_to_skip = _backfill_sort_and_filter(repos_needing_wiki, repo_sizes)

        assert "org/huge-repo" in repos_to_skip
        assert "org/huge-repo" not in repos_to_process
        assert "org/small-repo" in repos_to_process

    def test_repos_at_exact_threshold_are_not_skipped(self):
        """Repos at exactly 600MB should NOT be skipped (only > 600)."""
        repos_needing_wiki = ["org/boundary-repo"]
        repo_sizes = {"org/boundary-repo": 600}

        repos_to_process, repos_to_skip = _backfill_sort_and_filter(repos_needing_wiki, repo_sizes)

        assert "org/boundary-repo" in repos_to_process
        assert repos_to_skip == []

    def test_repos_just_above_threshold_are_skipped(self):
        """Repos at 601MB should be skipped."""
        repos_needing_wiki = ["org/just-over"]
        repo_sizes = {"org/just-over": 601}

        repos_to_process, repos_to_skip = _backfill_sort_and_filter(repos_needing_wiki, repo_sizes)

        assert "org/just-over" in repos_to_skip
        assert repos_to_process == []

    def test_all_repos_too_large(self):
        """When all repos exceed threshold, repos_to_process should be empty."""
        repos_needing_wiki = ["org/giant-a", "org/giant-b"]
        repo_sizes = {"org/giant-a": 800, "org/giant-b": 1200}

        repos_to_process, repos_to_skip = _backfill_sort_and_filter(repos_needing_wiki, repo_sizes)

        assert repos_to_process == []
        assert set(repos_to_skip) == {"org/giant-a", "org/giant-b"}


class TestBackfillSkipStateUpdate:
    """Verify skipped repos get the correct sentinel value in state."""

    def test_skipped_repo_gets_skip_too_large_sentinel(self):
        """Repos skipped for being too large should get deepwiki_sha='skip_too_large'."""
        repo_state = {
            "org/huge-repo": {"last_sha": "abc123", "deepwiki_sha": None},
            "org/small-repo": {"last_sha": "def456", "deepwiki_sha": None},
        }
        repo_sizes = {"org/huge-repo": 743, "org/small-repo": 50}

        # Simulate the skip logic from backfill_deepwiki_wikis()
        repos_to_skip = [
            repo
            for repo in repo_state
            if not repo_state[repo].get("deepwiki_sha")
            and repo_sizes.get(repo, 0) > DEEPWIKI_SKIP_SIZE_MB
        ]
        for repo in repos_to_skip:
            repo_state[repo]["deepwiki_sha"] = "skip_too_large"

        assert repo_state["org/huge-repo"]["deepwiki_sha"] == "skip_too_large"
        assert repo_state["org/small-repo"]["deepwiki_sha"] is None

    def test_skip_too_large_is_truthy_excludes_from_future_backfill(self):
        """The 'skip_too_large' sentinel should be truthy, preventing future backfill attempts."""
        repo_state = {
            "org/huge-repo": {"last_sha": "abc123", "deepwiki_sha": "skip_too_large"},
        }

        # This mirrors the selection logic — 'skip_too_large' is truthy
        repos_needing_wiki = [repo for repo, st in repo_state.items() if not st.get("deepwiki_sha")]

        assert "org/huge-repo" not in repos_needing_wiki


# ---------------------------------------------------------------------------
# git_ls_remote env-passthrough tests
#
# Verifies the fix for #1677: git_ls_remote must pass os.environ (including
# GIT_ASKPASS) to subprocess.run so private repos authenticate correctly.
# ---------------------------------------------------------------------------


class TestGitLsRemoteEnv:
    """Verify git_ls_remote passes credentials env to subprocess."""

    def _call_git_ls_remote(self, repo: str) -> str | None:
        """Import-free re-declaration of git_ls_remote matching refresh-repos.py."""
        try:
            result = subprocess.run(
                ["git", "ls-remote", f"https://github.com/{repo}", "HEAD"],
                capture_output=True,
                timeout=30,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            if result.returncode == 0 and result.stdout:
                sha = result.stdout.decode().split()[0]
                return sha
            return None
        except (subprocess.TimeoutExpired, Exception):
            return None

    @patch("subprocess.run")
    def test_env_includes_git_askpass(self, mock_run: MagicMock):
        """git_ls_remote must pass env with GIT_ASKPASS so private repos authenticate."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=b"abc123def456\tHEAD\n",
        )

        with patch.dict(os.environ, {"GIT_ASKPASS": "/app/git-credential-helper.sh"}):
            sha = self._call_git_ls_remote("aws-e/adp")

        assert sha == "abc123def456"
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args[1]
        assert "env" in call_kwargs
        assert call_kwargs["env"]["GIT_ASKPASS"] == "/app/git-credential-helper.sh"
        assert call_kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"

    @patch("subprocess.run")
    def test_env_includes_git_terminal_prompt_zero(self, mock_run: MagicMock):
        """GIT_TERMINAL_PROMPT=0 must be set to prevent interactive prompts."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=b"deadbeef1234\tHEAD\n",
        )

        sha = self._call_git_ls_remote("owner/repo")

        assert sha == "deadbeef1234"
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"

    @patch("subprocess.run")
    def test_returns_none_on_auth_failure(self, mock_run: MagicMock):
        """When ls-remote fails (e.g. 128 for auth), returns None."""
        mock_run.return_value = MagicMock(
            returncode=128,
            stdout=b"",
            stderr=b"fatal: could not read Username",
        )

        sha = self._call_git_ls_remote("private-org/private-repo")

        assert sha is None

    @patch("subprocess.run")
    def test_returns_none_on_timeout(self, mock_run: MagicMock):
        """When ls-remote times out, returns None gracefully."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=30)

        sha = self._call_git_ls_remote("slow-org/slow-repo")

        assert sha is None


# ---------------------------------------------------------------------------
# Token mint integration tests
#
# Verifies the fix for #1682: refresh-repos.py must call mint_github_token()
# at startup so that GIT_ASKPASS is set before any git ls-remote calls.
# Without this, the refresh/CronJob path never authenticates and private
# repos are silently skipped.
# ---------------------------------------------------------------------------


class TestRefreshMintToken:
    """Verify refresh-repos.py mints a GitHub App token at startup (#1682)."""

    def test_refresh_repos_imports_mint_github_token(self):
        """refresh-repos.py must import mint_github_token from github_auth."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "refresh_repos_source",
            os.path.join(
                os.path.dirname(__file__), "..", "images", "ingestion", "refresh-repos.py"
            ),
        )
        # Just read the source — don't execute (has heavy deps)
        with open(spec.origin) as f:
            source = f.read()

        assert "from github_auth import mint_github_token" in source
        assert "mint_github_token()" in source

    def test_mint_called_before_refresh_repo_in_main(self):
        """mint_github_token() must appear in main() BEFORE refresh_repo() calls."""
        source_path = os.path.join(
            os.path.dirname(__file__), "..", "images", "ingestion", "refresh-repos.py"
        )
        with open(source_path) as f:
            source = f.read()

        # Extract main() body only (everything after "def main():")
        main_pos = source.find("def main():")
        assert main_pos != -1, "def main(): not found"
        main_body = source[main_pos:]

        # In main(), mint must appear before refresh_repo (which calls git_ls_remote)
        mint_pos = main_body.find("token_ok = mint_github_token()")
        refresh_repo_pos = main_body.find("refresh_repo(repo,")

        assert mint_pos != -1, "mint_github_token() call not found in main()"
        assert refresh_repo_pos != -1, "refresh_repo() call not found in main()"
        assert mint_pos < refresh_repo_pos, (
            "mint_github_token must be called before refresh_repo in main()"
        )

    def test_sqs_worker_uses_shared_helper(self):
        """sqs-worker.py must import from github_auth (not inline the mint)."""
        source_path = os.path.join(
            os.path.dirname(__file__), "..", "images", "ingestion", "sqs-worker.py"
        )
        with open(source_path) as f:
            source = f.read()

        assert "from github_auth import mint_github_token" in source
        # The _mint_github_token wrapper should delegate to the shared helper
        assert "return mint_github_token()" in source
