"""Offline startup and import validation for the agent-mail container image.

These tests verify that the mcp-agent-mail serve-http entrypoint can load
without errors after the Dockerfile's dependency changes (removal of ruff
and diskcache, upgraded constraints). They run against a local virtualenv
install — no Docker daemon or network access required.

Usage:
    python -m pytest modules/harness/mcp-hub/docker/agent-mail/test_startup.py -v

Prerequisites:
    A virtualenv with mcp-agent-mail installed from the pinned upstream
    commit plus the constraints.txt overrides applied, and ruff/diskcache
    uninstalled. See the Dockerfile build steps for exact commands.
"""

import importlib
import subprocess
import sys

import pytest


def _can_import(module_name: str) -> bool:
    """Return True if the module is importable."""
    try:
        importlib.import_module(module_name)
        return True
    except ImportError:
        return False


# ── Core module imports ──────────────────────────────────────────────


class TestCoreImports:
    """Verify the serve-http import chain loads without errors."""

    def test_cli_module_imports(self):
        from mcp_agent_mail import cli  # noqa: F401

    def test_storage_module_imports(self):
        from mcp_agent_mail import storage  # noqa: F401

    def test_http_module_imports(self):
        from mcp_agent_mail import http  # noqa: F401

    def test_app_module_imports(self):
        from mcp_agent_mail import app  # noqa: F401


# ── Removed packages stay removed ───────────────────────────────────


class TestRemovedPackages:
    """Packages uninstalled in the Dockerfile must not be importable."""

    def test_ruff_not_importable(self):
        assert not _can_import("ruff"), "ruff should be uninstalled (unused linter)"

    def test_diskcache_not_importable(self):
        assert not _can_import("diskcache"), (
            "diskcache should be uninstalled (orphan dep with pickle CVE)"
        )


# ── Required packages present at correct versions ───────────────────


class TestConstrainedVersions:
    """Packages in constraints.txt must be installed at or above the floor."""

    def test_authlib_version(self):
        import authlib

        major, minor = (int(x) for x in authlib.__version__.split(".")[:2])
        assert (major, minor) >= (1, 6), (
            f"authlib {authlib.__version__} < 1.6.0 (GHSA-9ggr/GHSA-pq5p)"
        )

    def test_fastmcp_version(self):
        import fastmcp

        parts = fastmcp.__version__.split(".")
        major, minor = int(parts[0]), int(parts[1])
        assert (major, minor) >= (2, 14) or major >= 3, (
            f"fastmcp {fastmcp.__version__} < 2.14.0 (GHSA-rcfx)"
        )

    def test_cryptography_present(self):
        import cryptography  # noqa: F401

    def test_certifi_present(self):
        import certifi  # noqa: F401

    def test_pillow_present(self):
        from PIL import Image  # noqa: F401


# ── GitPython requires git binary ───────────────────────────────────


class TestGitDependency:
    """GitPython needs the git binary; verify it's importable."""

    def test_gitpython_imports(self):
        import git  # noqa: F401

    def test_git_binary_available(self):
        result = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=5
        )
        assert result.returncode == 0, "git binary not found"


# ── Serve-http entrypoint smoke test ────────────────────────────────


class TestServeHttpEntrypoint:
    """Verify the CMD entrypoint expression loads without crashing."""

    def test_entrypoint_loads(self):
        """Simulate the Dockerfile CMD without actually starting the server."""
        saved_argv = sys.argv[:]
        try:
            sys.argv = ["mcp-agent-mail", "serve-http", "--help"]
            from mcp_agent_mail.cli import app  # noqa: F811

            # If we get here, the import chain succeeded.
            # Don't call app() — that would start the server or print help and
            # raise SystemExit. The import alone proves the entrypoint resolves.
        finally:
            sys.argv = saved_argv
