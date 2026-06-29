"""Shared GitHub App token minting for the ingestion pipeline.

Both sqs-worker.py (KEDA ScaledJob path) and refresh-repos.py (CronJob/Job path)
need authenticated git access for private repos. This module provides a single
implementation so the two paths can't drift.

Usage:
    from github_auth import mint_github_token
    success = mint_github_token()
    # If True, GIT_ASKPASS is now set in os.environ and /tmp/github-token exists.

Per-installation auth (issue #2088):
    from github_auth import mint_installation_token, InstallationRevokedError
    try:
        mint_installation_token(installation_id=12345)
    except InstallationRevokedError:
        # Mark asset failed: access_revoked
        ...
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

from config import settings

log = logging.getLogger(__name__)

# Exit code from github-app-token.py indicating the installation was not found (404/410)
_EXIT_INSTALLATION_REVOKED = 2


class InstallationRevokedError(Exception):
    """Raised when the GitHub App installation has been revoked or removed.

    The token mint returned 404/410 — the installation no longer exists.
    Callers must treat this as a terminal failure (fail-closed).
    """

    def __init__(self, installation_id: int, detail: str = ""):
        self.installation_id = installation_id
        self.detail = detail
        super().__init__(f"Installation {installation_id} revoked or not found. {detail}".strip())


def mint_github_token() -> bool:
    """Mint a GitHub App installation token and configure git credential helper.

    On success:
      - Writes token to /tmp/github-token
      - Sets os.environ["GIT_ASKPASS"] = "/app/git-credential-helper.sh"
      - Sets os.environ["GIT_TERMINAL_PROMPT"] = "0"

    Returns True if token was successfully obtained, False otherwise.
    Failure is non-fatal for public repos — caller should log and continue.

    Note: GitHub App installation tokens expire after 1 hour (GitHub-enforced).
    Since each KEDA ScaledJob pod processes one message then exits, and refresh
    CronJob runs complete in <1 hour, mint-per-pod/job is sufficient.
    """
    app_id_secret = settings.github_app_id_secret
    app_key_secret = settings.github_app_key_secret

    if not app_id_secret or not app_key_secret:
        log.info(
            "No GitHub App secrets configured — using anonymous clones (private repos will fail)"
        )
        return False

    owner = settings.github_app_owner

    try:
        cmd = [
            sys.executable,
            "/app/github-app-token.py",
            "--app-id-secret",
            app_id_secret,
            "--app-key-secret",
            app_key_secret,
            "--region",
            settings.aws_region,
            "--output-file",
            "/tmp/github-token",
        ]
        if owner:
            cmd.extend(["--owner", owner])

        result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
            cmd,
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            log.info("GitHub App token minted successfully")
            os.environ["GIT_ASKPASS"] = "/app/git-credential-helper.sh"
            os.environ["GIT_TERMINAL_PROMPT"] = "0"
            return True
        else:
            stderr = result.stderr.decode()[:200]
            log.warning("GitHub App token mint failed: %s", stderr)
            return False
    except subprocess.TimeoutExpired:
        log.warning("GitHub App token mint timed out")
        return False
    except Exception as e:
        log.warning("GitHub App token mint error: %s", e)
        return False


def mint_installation_token(installation_id: int) -> bool:
    """Mint a GitHub App token for a specific installation ID.

    Used for per-pod installation auth (issue #2088): each private-repo
    ingestion pod authenticates with the installation_id stored on the asset
    at register time. No discovery, no fallback to another installation.

    On success:
      - Writes token to /tmp/github-token
      - Sets os.environ["GIT_ASKPASS"] = "/app/git-credential-helper.sh"
      - Sets os.environ["GIT_TERMINAL_PROMPT"] = "0"

    Returns True on success.

    Raises:
        InstallationRevokedError: if the installation is not found (404/410).
            Caller MUST treat this as terminal — fail-closed, no fallback.
        RuntimeError: on transient/unexpected errors (caller may retry or fail).
    """
    app_id_secret = settings.github_app_id_secret
    app_key_secret = settings.github_app_key_secret

    if not app_id_secret or not app_key_secret:
        raise RuntimeError(
            "GitHub App secrets not configured — cannot mint per-installation token. "
            "Set GITHUB_APP_ID_SECRET and GITHUB_APP_KEY_SECRET."
        )

    try:
        cmd = [
            sys.executable,
            "/app/github-app-token.py",
            "--app-id-secret",
            app_id_secret,
            "--app-key-secret",
            app_key_secret,
            "--region",
            settings.aws_region,
            "--installation-id",
            str(installation_id),
            "--output-file",
            "/tmp/github-token",
        ]

        result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
            cmd,
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            log.info("GitHub App token minted for installation %d", installation_id)
            os.environ["GIT_ASKPASS"] = "/app/git-credential-helper.sh"
            os.environ["GIT_TERMINAL_PROMPT"] = "0"
            return True
        elif result.returncode == _EXIT_INSTALLATION_REVOKED:
            stderr = result.stderr.decode()[:300]
            log.error(
                "Installation %d revoked (exit code %d): %s",
                installation_id,
                _EXIT_INSTALLATION_REVOKED,
                stderr,
            )
            raise InstallationRevokedError(installation_id=installation_id, detail=stderr)
        else:
            stderr = result.stderr.decode()[:200]
            raise RuntimeError(
                f"Token mint failed for installation {installation_id} "
                f"(exit code {result.returncode}): {stderr}"
            )
    except InstallationRevokedError:
        raise
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Token mint timed out for installation {installation_id}")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Token mint error for installation {installation_id}: {e}") from e
