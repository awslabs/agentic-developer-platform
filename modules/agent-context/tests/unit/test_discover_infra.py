"""Unit tests for discover-infra.py IaC wiring (issue #1699).

Tests that main() correctly invokes parse_and_load_iac for repos with .tf files,
clones repos ephemerally, and skips repos without Terraform.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Path to the module under test
DISCOVER_INFRA_PATH = str(
    Path(__file__).resolve().parents[2] / "images" / "ingestion" / "discover-infra.py"
)

# Add the ingestion image directory to path for imports (needed for config, etc.)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "images" / "ingestion"))


def _load_discover_infra():
    """Import discover-infra.py (hyphenated name requires importlib)."""
    spec = importlib.util.spec_from_file_location("discover_infra", DISCOVER_INFRA_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Cache so patch() can find it by dotted name
    sys.modules["discover_infra"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_tf_tree(base_dir: str, repo: str) -> str:
    """Create a fake cloned repo with .tf files."""
    repo_dir = os.path.join(base_dir, repo)
    infra_dir = os.path.join(repo_dir, "infra")
    os.makedirs(infra_dir, exist_ok=True)
    with open(os.path.join(infra_dir, "main.tf"), "w") as f:
        f.write('resource "aws_s3_bucket" "test" { bucket = "test" }\n')
    os.makedirs(os.path.join(repo_dir, ".git"), exist_ok=True)
    return repo_dir


def _create_non_tf_tree(base_dir: str, repo: str) -> str:
    """Create a fake cloned repo without .tf files."""
    repo_dir = os.path.join(base_dir, repo)
    os.makedirs(repo_dir, exist_ok=True)
    with open(os.path.join(repo_dir, "README.md"), "w") as f:
        f.write("# No terraform here\n")
    os.makedirs(os.path.join(repo_dir, ".git"), exist_ok=True)
    return repo_dir


# ---------------------------------------------------------------------------
# Tests for _parse_repos_file
# ---------------------------------------------------------------------------


class TestParseReposFile:
    """Tests for the repos.txt parser."""

    def test_parses_repos(self, tmp_path: Path):
        """Reads repo lines, skipping comments and blanks."""
        repos_file = tmp_path / "repos.txt"
        repos_file.write_text("# Comment\naws-e/adp\n\norg/other-repo\n")

        mod = _load_discover_infra()
        result = mod._parse_repos_file(str(repos_file))
        assert result == ["aws-e/adp", "org/other-repo"]

    def test_missing_file_returns_empty(self, tmp_path: Path):
        """Returns empty list for non-existent file."""
        mod = _load_discover_infra()
        result = mod._parse_repos_file(str(tmp_path / "nonexistent.txt"))
        assert result == []


# ---------------------------------------------------------------------------
# Tests for _clone_repo
# ---------------------------------------------------------------------------


class TestCloneRepo:
    """Tests for the shallow-clone helper."""

    @patch("subprocess.run")
    def test_successful_clone(self, mock_run, tmp_path: Path):
        """Returns True on successful git clone."""
        mock_run.return_value = MagicMock(returncode=0)
        mod = _load_discover_infra()
        result = mod._clone_repo("aws-e/adp", str(tmp_path / "aws-e" / "adp"))
        assert result is True
        # Verify git clone was called with correct args
        call_args = mock_run.call_args
        assert "git" in call_args[0][0]
        assert "--depth=1" in call_args[0][0]
        assert "https://github.com/aws-e/adp" in call_args[0][0]

    @patch("subprocess.run")
    def test_failed_clone(self, mock_run, tmp_path: Path):
        """Returns False on git clone failure."""
        import subprocess as sp

        mock_run.side_effect = sp.CalledProcessError(128, "git clone", stderr=b"auth failed")
        mod = _load_discover_infra()
        result = mod._clone_repo("org/private", str(tmp_path / "org" / "private"))
        assert result is False

    @patch("subprocess.run")
    def test_timeout_returns_false(self, mock_run, tmp_path: Path):
        """Returns False on timeout."""
        import subprocess as sp

        mock_run.side_effect = sp.TimeoutExpired("git clone", 300)
        mod = _load_discover_infra()
        result = mod._clone_repo("org/slow-repo", str(tmp_path / "org" / "slow-repo"))
        assert result is False


# ---------------------------------------------------------------------------
# Source-level wiring assertions (lightweight, no heavy deps needed)
# ---------------------------------------------------------------------------


class TestWiringAssertions:
    """Source-level checks that the IaC pipeline is properly wired."""

    def test_main_calls_parse_and_load_iac(self):
        """main() must invoke parse_and_load_iac (not just define it)."""
        with open(DISCOVER_INFRA_PATH) as f:
            source = f.read()

        # Find main() body
        main_pos = source.find("def main():")
        assert main_pos != -1, "def main(): not found"
        main_body = source[main_pos:]

        # parse_and_load_iac must be called in main
        assert "parse_and_load_iac(" in main_body, (
            "parse_and_load_iac() is not called in main() — "
            "IaC parsing is defined but never invoked (issue #1699)"
        )

    def test_main_reads_repos_file(self):
        """main() reads repos from repos.txt (not from persistent clone_base)."""
        with open(DISCOVER_INFRA_PATH) as f:
            source = f.read()

        main_pos = source.find("def main():")
        main_body = source[main_pos:]

        # Should read from repos_file, not iterate clone_base dirs
        assert "_parse_repos_file(" in main_body or "repos_file" in main_body, (
            "main() does not read repos from repos.txt — "
            "it needs an explicit repo list since persistent clones don't exist"
        )

    def test_imports_github_auth(self):
        """discover-infra.py imports mint_github_token for private repo access."""
        with open(DISCOVER_INFRA_PATH) as f:
            source = f.read()

        assert "from github_auth import mint_github_token" in source

    def test_mint_called_before_clone(self):
        """mint_github_token() must be called before _clone_repo in main()."""
        with open(DISCOVER_INFRA_PATH) as f:
            source = f.read()

        main_pos = source.find("def main():")
        main_body = source[main_pos:]

        mint_pos = main_body.find("mint_github_token()")
        clone_pos = main_body.find("_clone_repo(")

        assert mint_pos != -1, "mint_github_token() not called in main()"
        assert clone_pos != -1, "_clone_repo() not called in main()"
        assert mint_pos < clone_pos, (
            "mint_github_token() must be called before _clone_repo() — "
            "otherwise private repos will fail to clone"
        )

    def test_ephemeral_clone_with_cleanup(self):
        """main() uses tempfile + shutil.rmtree for ephemeral clones."""
        with open(DISCOVER_INFRA_PATH) as f:
            source = f.read()

        main_pos = source.find("def main():")
        main_body = source[main_pos:]

        assert "tempfile.mkdtemp(" in main_body, "Step 4 must use ephemeral temp dirs for cloning"
        assert "shutil.rmtree(" in main_body, "Step 4 must clean up temp clone dirs"

    def test_no_reliance_on_persistent_clone_base(self):
        """main() Step 4 must NOT iterate clone_base (it's empty at runtime)."""
        with open(DISCOVER_INFRA_PATH) as f:
            source = f.read()

        main_pos = source.find("def main():")
        main_body = source[main_pos:]

        # The old broken pattern was: for org_name in os.listdir(clone_base)
        assert "os.listdir(clone_base)" not in main_body, (
            "main() still iterates clone_base — this is the broken pattern. "
            "Persistent clones don't exist because refresh-repos.py uses ephemeral /tmp dirs."
        )
