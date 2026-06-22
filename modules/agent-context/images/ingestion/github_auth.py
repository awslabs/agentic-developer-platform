"""Shared GitHub App token minting for the ingestion pipeline.

Both sqs-worker.py (KEDA ScaledJob path) and refresh-repos.py (CronJob/Job path)
need authenticated git access for private repos. This module provides a single
implementation so the two paths can't drift.

Usage:
    from github_auth import mint_github_token
    success = mint_github_token()
    # If True, GIT_ASKPASS is now set in os.environ and /tmp/github-token exists.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

from config import settings

log = logging.getLogger(__name__)


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

        result = subprocess.run(
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
