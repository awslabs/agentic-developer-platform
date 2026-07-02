"""Runtime GitHub App credential provider with Secrets Manager caching.

Issue #2594: Replaces static env-var reads (BG_GITHUB_APP_*) with a cached
Secrets Manager reader so that credential changes (register, rotate, disconnect)
take effect without a pod restart.

Resolution order:
  1. Secrets Manager at adp/<env>/github-app/adp-agent-platform-{id,key,meta}
  2. BG_ environment variables (backward-compatible fallback)

Callers (register-callback, rotate-key, disconnect) call invalidate() after
writing the secret so the next read picks up the new value immediately.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_TTL_SECONDS = 300  # 5 minutes

# Terraform seeds secrets with this literal placeholder at deploy time
# (modules/agent-factory/webhook-ingress/infra/secrets.tf:39,54).
# It must never be treated as a real App credential.
_PLACEHOLDER_SENTINEL = "PLACEHOLDER_SET_BY_REGISTER_SCRIPT"


@dataclass
class _CachedCreds:
    """Cached GitHub App credentials from Secrets Manager."""

    app_id: str = ""
    private_key: str = ""
    slug: str = ""
    fetched_at: float = 0.0


class GitHubAppCredsProvider:
    """In-process cached reader for platform GitHub App credentials.

    Thread-safe. Reads from Secrets Manager on first access, then serves from
    cache until TTL expires or invalidate() is called.

    Falls back to BG_ env vars when the secret is absent or unreadable.
    """

    def __init__(self, *, ttl_seconds: float = _DEFAULT_CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._cache: _CachedCreds | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_credentials(self) -> tuple[str, str]:
        """Return (app_id, private_key_pem).

        Resolves from Secrets Manager (cached), falling back to BG_ env vars.
        Returns empty strings if nothing is configured (caller handles the
        unconfigured case).
        """
        creds = self._resolve()
        return creds.app_id, creds.private_key

    def get_slug(self) -> str:
        """Return the GitHub App slug.

        Resolution: SM meta secret → BG_GITHUB_APP_SLUG env var → empty.
        """
        creds = self._resolve()
        return creds.slug

    def invalidate(self) -> None:
        """Clear the cache so the next read goes to Secrets Manager.

        Call after writing/rotating/deleting the secret.
        """
        with self._lock:
            self._cache = None
        logger.debug("GitHubAppCredsProvider: cache invalidated")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve(self) -> _CachedCreds:
        """Return cached credentials or fetch fresh ones."""
        with self._lock:
            if self._cache is not None:
                age = time.monotonic() - self._cache.fetched_at
                if age < self._ttl:
                    return self._cache

        # Outside the lock — SM read is I/O; we don't want to block other
        # threads. A brief race where two threads both fetch is acceptable
        # (idempotent read, last-write-wins into cache).
        creds = self._fetch_from_sm()
        if creds is not None:
            with self._lock:
                self._cache = creds
            logger.debug(
                "GitHubAppCredsProvider: resolved from Secrets Manager (app_id=%s, slug=%s)",
                creds.app_id[:8] + "..." if len(creds.app_id) > 8 else creds.app_id,
                creds.slug,
            )
            return creds

        # Fallback: BG_ env vars
        fallback = self._fallback_from_env()
        with self._lock:
            self._cache = fallback
        logger.debug("GitHubAppCredsProvider: resolved from env-var fallback")
        return fallback

    def _fetch_from_sm(self) -> _CachedCreds | None:
        """Attempt to read credentials from Secrets Manager.

        Returns None if the secrets don't exist or are unreadable (so the
        caller can fall back to env vars).
        """
        env = os.environ.get("ENVIRONMENT", "dev")
        region = os.environ.get("AWS_REGION", "us-east-1")

        id_path = f"adp/{env}/github-app/adp-agent-platform-id"
        key_path = f"adp/{env}/github-app/adp-agent-platform-key"
        meta_path = f"adp/{env}/github-app/adp-agent-platform-meta"

        try:
            sm = boto3.client("secretsmanager", region_name=region)
        except Exception as exc:
            logger.warning("GitHubAppCredsProvider: cannot create SM client: %s", exc)
            return None

        app_id = self._read_secret(sm, id_path)
        if not app_id or app_id.strip() == _PLACEHOLDER_SENTINEL:
            return None

        private_key = self._read_secret(sm, key_path)
        if not private_key or private_key.strip() == _PLACEHOLDER_SENTINEL:
            return None

        # Slug: try meta secret first, fall back to env var
        slug = ""
        meta_raw = self._read_secret(sm, meta_path)
        if meta_raw:
            try:
                meta = json.loads(meta_raw)
                slug = meta.get("app_slug", "")
            except (json.JSONDecodeError, TypeError):
                pass

        # If slug not in meta, fall back to env var
        if not slug:
            from src.shared.config import get_settings

            settings = get_settings()
            slug = getattr(settings, "github_app_slug", "") or ""

        return _CachedCreds(
            app_id=app_id,
            private_key=private_key,
            slug=slug,
            fetched_at=time.monotonic(),
        )

    @staticmethod
    def _read_secret(sm_client: Any, secret_id: str) -> str:
        """Read a single secret string. Returns empty string on failure."""
        try:
            resp = sm_client.get_secret_value(SecretId=secret_id)
            return resp.get("SecretString", "")
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("ResourceNotFoundException", "InvalidRequestException"):
                return ""
            logger.warning(
                "GitHubAppCredsProvider: error reading secret %s: %s",
                secret_id,
                exc,
            )
            return ""
        except Exception as exc:
            logger.warning(
                "GitHubAppCredsProvider: unexpected error reading %s: %s",
                secret_id,
                exc,
            )
            return ""

    @staticmethod
    def _fallback_from_env() -> _CachedCreds:
        """Build credentials from BG_ environment variables."""
        from src.shared.config import get_settings

        settings = get_settings()
        return _CachedCreds(
            app_id=getattr(settings, "github_app_id", "") or "",
            private_key=getattr(settings, "github_app_private_key", "") or "",
            slug=getattr(settings, "github_app_slug", "") or "",
            fetched_at=time.monotonic(),
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_provider: GitHubAppCredsProvider | None = None
_provider_lock = threading.Lock()


def get_github_app_provider() -> GitHubAppCredsProvider:
    """Return the module-level singleton provider instance."""
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                _provider = GitHubAppCredsProvider()
    return _provider


def _reset_provider_for_testing(provider: GitHubAppCredsProvider | None = None) -> None:
    """Replace the singleton — TEST USE ONLY."""
    global _provider
    with _provider_lock:
        _provider = provider
