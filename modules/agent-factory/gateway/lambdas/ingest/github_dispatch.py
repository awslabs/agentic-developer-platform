"""
GitHub issue creation and agent dispatch.

Instead of workflow_dispatch, creates a GitHub issue with the enriched task
description and labels it with the agent persona. The existing label-triggered
GitHub Actions workflows handle the rest.

Flow:
  1. Create issue with enriched body (or use existing issue_number)
  2. Add agent label (e.g., "agent-developer") to trigger the workflow
  3. Return issue URL for user notification
"""

import json
import logging
import os
import time
import urllib.request
from typing import Any

import boto3

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION_NAME", "us-east-1")
# Secret prefix for the GH App the ingest Lambda uses to create+label issues.
# Expected shape: `adp/<github_org>/gh-app-ops`. Full secret IDs at runtime
# are `${prefix}-id` and `${prefix}-key`. Set by Terraform; no default — we
# prefer a visible cold-start warning over silently reading the wrong path.
GH_APP_SECRET_PREFIX = os.environ.get("GH_APP_SECRET_PREFIX", "")
if not GH_APP_SECRET_PREFIX:
    logger.warning(
        "GH_APP_SECRET_PREFIX env var is unset. github_actions classification "
        "path will fail at runtime because secret lookups will return the wrong "
        "ARN. Fix: set GH_APP_SECRET_PREFIX on the ingest Lambda via Terraform — "
        "see modules/agent-factory/infra/modules/lambda-gateway/main.tf."
    )

_secrets_client = None

# Installation tokens, keyed by normalized org. Issue #4071: this used to be a
# single unkeyed {"token", "expires_at"} pair, and the cache was consulted
# before `org` was even read — so a warm Lambda container handed org A's token
# to a later org-B request for up to 3000s. Keyed per org, and bounded because
# it is a warm-container global that would otherwise grow without limit.
#   org (lowercased) -> (token, expires_at_epoch_seconds)
_token_cache: dict[str, tuple[str, float]] = {}

# Max distinct orgs held at once. The App is installed on few orgs in practice;
# this is a leak guard, not a tuning knob.
_TOKEN_CACHE_MAX_ENTRIES = 32


def _token_cache_get(org: str, now: float) -> str | None:
    """Return a live cached token for `org`, or None. Drops it if expired."""
    entry = _token_cache.get(org)
    if entry is None:
        return None
    token, expires_at = entry
    if now >= expires_at:
        del _token_cache[org]
        return None
    return token


def _token_cache_put(org: str, token: str, expires_at: float, now: float) -> None:
    """Cache `token` for `org`, evicting expired entries and bounding size."""
    for stale_org in [o for o, (_, exp) in _token_cache.items() if now >= exp]:
        del _token_cache[stale_org]
    if org not in _token_cache and len(_token_cache) >= _TOKEN_CACHE_MAX_ENTRIES:
        # Evict the entry closest to expiry so the cap can never be exceeded.
        soonest = min(_token_cache, key=lambda o: _token_cache[o][1])
        del _token_cache[soonest]
    _token_cache[org] = (token, expires_at)


def _get_secrets():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager", region_name=AWS_REGION)
    return _secrets_client


def configured_github_org() -> str:
    """The single GitHub org this Lambda's App serves, lowercased.

    Issue #4233: the ingest App is single-org by construction — Terraform sets
    `GH_APP_SECRET_PREFIX = "adp/<github_org>/gh-app-ops"` — so the configured
    org is derivable from the prefix without a second env var to keep in sync.

    Returns "" when the prefix is unset or malformed; callers treat that as
    "cannot determine the org" and fail closed.
    """
    parts = [p for p in GH_APP_SECRET_PREFIX.split("/") if p]
    return parts[1].strip().lower() if len(parts) >= 3 else ""


def installation_id_for_org(org: str) -> int | None:
    """Resolve the App installation id GitHub reports for `org`, or None.

    Issue #4233: used by the chat-dispatch tenant gate to assert that the
    caller's org OWNS the installation covering the target repo owner, rather
    than trusting that two labels look alike. Deliberately does not mint an
    access token — the gate runs before we are willing to act as the App.
    """
    if not org:
        return None
    if not GH_APP_SECRET_PREFIX:
        logger.error(
            "Cannot resolve installation for org %s: GH_APP_SECRET_PREFIX is unset", org
        )
        return None

    id_secret_id = f"{GH_APP_SECRET_PREFIX}-id"
    key_secret_id = f"{GH_APP_SECRET_PREFIX}-key"
    try:
        secrets = _get_secrets()
        app_id = secrets.get_secret_value(SecretId=id_secret_id)["SecretString"]
        private_key = secrets.get_secret_value(SecretId=key_secret_id)["SecretString"]
        return _get_installation_id(_create_jwt(app_id, private_key), org)
    except Exception as e:
        logger.error(
            "Failed to resolve installation for org %s (secrets: %s, %s): %s",
            org,
            id_secret_id,
            key_secret_id,
            e,
        )
        return None


def create_issue_and_dispatch(
    repo_owner: str,
    repo_name: str,
    title: str,
    body: str,
    persona: str,
    session_id: str = "",
    channel: str = "",
    user_name: str = "",
) -> dict[str, Any]:
    """
    Create a GitHub issue and label it to trigger the agent.

    Returns:
        {"issue_number": 42, "issue_url": "https://...", "dispatched": True}
    """
    token = _get_installation_token(repo_owner)
    if not token:
        return {"dispatched": False, "error": "Could not get GitHub App token"}

    # Add metadata to issue body
    full_body = body
    if session_id or channel or user_name:
        full_body += f"\n\n---\n_Escalated from {channel} by {user_name}_"
        if session_id:
            full_body += f"\n_Session: `{session_id[:12]}...`_"

    # Create the issue
    issue = _create_issue(token, repo_owner, repo_name, title, full_body)
    if not issue:
        return {"dispatched": False, "error": "Failed to create GitHub issue"}

    issue_number = issue["number"]
    issue_url = issue["html_url"]

    # Add the agent label to trigger the workflow
    label = f"agent-{persona}"
    _add_label(token, repo_owner, repo_name, issue_number, label)

    logger.info("Created issue #%d and labeled with %s: %s", issue_number, label, issue_url)

    return {
        "dispatched": True,
        "issue_number": issue_number,
        "issue_url": issue_url,
        "label": label,
    }


def label_existing_issue(
    repo_owner: str,
    repo_name: str,
    issue_number: int,
    persona: str,
    enriched_comment: str = "",
) -> dict[str, Any]:
    """
    Add agent label to an existing issue (and optionally post enriched context as a comment).

    Returns:
        {"issue_number": 42, "issue_url": "https://...", "dispatched": True}
    """
    token = _get_installation_token(repo_owner)
    if not token:
        return {"dispatched": False, "error": "Could not get GitHub App token"}

    # Post enriched context as a comment if provided
    if enriched_comment:
        _post_comment(token, repo_owner, repo_name, issue_number, enriched_comment)

    # Add the agent label
    label = f"agent-{persona}"
    _add_label(token, repo_owner, repo_name, issue_number, label)

    issue_url = f"https://github.com/{repo_owner}/{repo_name}/issues/{issue_number}"
    logger.info("Labeled existing issue #%d with %s", issue_number, label)

    return {
        "dispatched": True,
        "issue_number": issue_number,
        "issue_url": issue_url,
        "label": label,
    }


# --- GitHub API helpers ---

def _create_issue(token: str, owner: str, repo: str, title: str, body: str) -> dict | None:
    url = f"https://api.github.com/repos/{owner}/{repo}/issues"
    payload = json.dumps({"title": title, "body": body}).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers=_gh_headers(token), method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.error("Failed to create issue: %s", e)
        return None


def _add_label(token: str, owner: str, repo: str, issue_number: int, label: str):
    # Ensure label exists
    _ensure_label(token, owner, repo, label)

    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/labels"
    payload = json.dumps({"labels": [label]}).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers=_gh_headers(token), method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
    except Exception as e:
        logger.error("Failed to add label %s to issue #%d: %s", label, issue_number, e)


def _ensure_label(token: str, owner: str, repo: str, label: str):
    """Create the label if it doesn't exist."""
    url = f"https://api.github.com/repos/{owner}/{repo}/labels"
    payload = json.dumps({"name": label, "color": "7057ff", "description": f"Triggers @{label} agent"}).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers=_gh_headers(token), method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 422:  # Already exists
            pass
        else:
            logger.warning("Could not ensure label %s: %s", label, e)


def _post_comment(token: str, owner: str, repo: str, issue_number: int, body: str):
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments"
    payload = json.dumps({"body": body}).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers=_gh_headers(token), method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
    except Exception as e:
        logger.error("Failed to post comment on issue #%d: %s", issue_number, e)


def _gh_headers(token: str) -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }


# --- GitHub App token management ---

def _get_installation_token(org: str) -> str | None:
    now = time.time()
    # Read `org` BEFORE consulting the cache — the old code returned the cached
    # token first, so whichever org warmed the container won for every caller.
    cache_key = (org or "").strip().lower()
    if not cache_key:
        logger.error("Cannot fetch GH App token: no org supplied")
        return None

    cached = _token_cache_get(cache_key, now)
    if cached:
        return cached

    if not GH_APP_SECRET_PREFIX:
        logger.error(
            "Cannot fetch GH App token: GH_APP_SECRET_PREFIX env var is unset. "
            "This is a Terraform misconfiguration — github_actions classifier "
            "path is dead until fixed."
        )
        return None

    id_secret_id = f"{GH_APP_SECRET_PREFIX}-id"
    key_secret_id = f"{GH_APP_SECRET_PREFIX}-key"
    try:
        secrets = _get_secrets()
        app_id = secrets.get_secret_value(SecretId=id_secret_id)["SecretString"]
        private_key = secrets.get_secret_value(SecretId=key_secret_id)["SecretString"]

        jwt_token = _create_jwt(app_id, private_key)
        installation_id = _get_installation_id(jwt_token, org)
        if not installation_id:
            logger.error(
                "Got GH App creds but no installation found for org %s. Check the "
                "app is installed on the org.",
                org,
            )
            return None

        url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
        req = urllib.request.Request(url, data=b"{}", headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {jwt_token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }, method="POST")

        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            token = data["token"]
            _token_cache_put(cache_key, token, now + 3000, now)
            return token

    except Exception as e:
        # Include the exact secret IDs we tried so IAM AccessDenied is
        # debuggable without replaying the call.
        logger.error(
            "Failed to get GitHub installation token (secrets: %s, %s): %s",
            id_secret_id,
            key_secret_id,
            e,
        )
        return None


def _create_jwt(app_id: str, private_key: str) -> str:
    now = int(time.time())
    try:
        import jwt as pyjwt
        return pyjwt.encode({"iat": now - 60, "exp": now + 600, "iss": app_id}, private_key, algorithm="RS256")
    except ImportError:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        import base64

        key = serialization.load_pem_private_key(private_key.encode(), password=None)
        header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b"=")
        payload = base64.urlsafe_b64encode(json.dumps({"iat": now - 60, "exp": now + 600, "iss": app_id}).encode()).rstrip(b"=")
        signing_input = header + b"." + payload
        signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return (signing_input + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


def _get_installation_id(jwt_token: str, org: str) -> int | None:
    url = "https://api.github.com/app/installations"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {jwt_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            for inst in json.loads(resp.read()):
                if inst.get("account", {}).get("login", "").lower() == org.lower():
                    return inst["id"]
            return None
    except Exception as e:
        logger.error("Failed to list installations: %s", e)
        return None
