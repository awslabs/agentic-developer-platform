#!/usr/bin/env python3
"""Agent pod entrypoint: SQS envelope -> vault -> token mint -> clone -> agent exec -> PR.

KEDA's ScaledJob spawns this pod when queue depth >= 1 but does NOT inject
the message body as an env var — the pod receives its own message via the
SQS SDK. On success we DeleteMessage so KEDA sees queue drain. On failure
we leave the message invisible (visibility timeout returns it for retry;
DLQ kicks in after maxReceiveCount).

Performs a 12-step sequence to set up the environment and exec the agent.

Idempotency: uses envelope message_id to prevent duplicate comments/branches.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import boto3

from lib.bootstrap_logger import BootstrapLogger
from lib.check_run import create_check_run, update_check_run
from lib.correlation_marker import prepend_correlation_marker
from lib.correlation_store import channel_key, write_pointer
from lib.engine_registration import draft_registration_note
from lib.invocation_status import (
    clear_control_endpoint,
    register_control_endpoint,
)
from lib.invocation_status import update_status as update_invocation_status
from lib.gateway_credential_client import GatewayCredentialClient, GatewayCredentialError
from lib.github_token import mint_installation_token
from lib.provenance_client import post_provenance
from lib.vault_client import VaultClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

WORK_DIR = Path("/work/repo")
PERSONAS_DIR = Path("/app/personas")
SKILLS_DIR = Path("/app/skills")
AGENT_BINARY = "/app/dist/agent-worker.js"
PERSONAS_NEEDING_AWS = frozenset({"operations", "agent-operations"})

# Retired ADP_BEDROCK_VIA values, mapped to the error shown when one is set.
#
# Issue #4747 (ruling 3 of #4692): `user` routed Bedrock through the customer's
# own assumed credentials, bypassing the gateway — so those calls were billed to
# the customer but written to no `usage_logs` row at all. Per-principal routing
# (#4742-#4746) replaces it with a mapping that reaches the same account *with*
# metering, so the mode is retired rather than migrated.
#
# This is a rejection guard, NOT a routing branch: no path honors `user` as a
# mode. It fails loudly on purpose. Falling through to the trailing `else` would
# silently run the agent on pod IRSA — i.e. platform-billed Bedrock for someone
# who explicitly asked to be billed on their own account. A silent billing
# switch is exactly what the ruling forbids, so an unroutable pod must die
# before it spends anything rather than spend it against the wrong account.
RETIRED_BEDROCK_VIA = {
    "user": (
        "ADP_BEDROCK_VIA=user is retired (issue #4747, ruling 3 of #4692). It billed "
        "Bedrock to the customer's account while writing no usage row, so platform "
        "metering could not see the spend. To route a principal's Bedrock calls to "
        "their own AWS account with metering intact, create a per-principal Bedrock "
        "account mapping (Settings -> Credentials, or the admin Bedrock routing "
        "surface) and leave ADP_BEDROCK_VIA=gateway. Use ADP_BEDROCK_VIA=direct only "
        "as the documented kill switch for platform-billed direct Bedrock."
    ),
}

# Exit code by which the Node worker asks for the SQS message to be RETRIED rather
# than acked. Step 13 below deletes the message on every other terminal exit, so a
# plain non-zero exit would destroy the task instead of retrying it.
#
# Issue #4369: the worker's auth watchdog uses this when GitHub 401s survive a
# forced token refresh — the run cannot make progress, but the task is untouched
# and a fresh pod (with a fresh installation token) will succeed. Leaving the
# message alone lets its visibility timeout lapse so SQS redelivers, bounded by the
# queue's maxReceiveCount before it lands in the DLQ. Keep in sync with
# EXIT_RETRYABLE in agent/src/agent-worker.ts.
AGENT_EXIT_RETRYABLE = 75

# Personas whose branch-bootstrap logic should NEVER delete an existing remote
# branch. AIDLC runs multiple sequential stages on the same issue/branch, each
# committing artifacts (problem-frame.md, requirements, design, stories, delivery
# plan) without opening a PR until the final stage. The stale-branch reset
# (case (a) in Step 6b) would destroy prior stage commits. These personas always
# fetch + extend instead. Issue #3430.
PERSONAS_EXTENDING_BRANCH = frozenset({"aidlc"})

# Personas whose finish path registers an authored loop proposal with the
# orchestration engine (issue #4528). Only the authoring persona composes a
# proposal, so only it has one to register; every other persona's finish path is
# byte-identical to before. A frozenset rather than an `== "aidlc"` check for the
# same reason as the set above — the gate is a list of personas, and the next one
# added should not require finding this branch.
PERSONAS_REGISTERING_DRAFTS = frozenset({"aidlc"})

# STS session tag values must match [\p{L}\p{Z}\p{N}_.:/=+\-@]*. The natural
# task ID shape `<owner>/<repo>#<issue>` contains '#' which fails validation.
# Replace any character outside the allowed set with '_'.
_STS_TAG_FORBIDDEN = re.compile(r"[^A-Za-z0-9_.:/=+\-@]")


def _sanitize_for_sts_tag(value: str) -> str:
    return _STS_TAG_FORBIDDEN.sub("_", value)


# --- Issue #4272: GitHub-token gatekeeper kill-switch ---------------------------
# Mirrors the ADP_PAT_EXECUTION_ENABLED precedent below: default off = today's
# behavior byte-for-byte. When on, the platform GitHub App private key is never
# read in this pod at all — the gateway mints on our behalf, both for the
# bootstrap token and for every in-run refresh — and GH_APP_PRIVATE_KEY is not
# exported to the agent subprocess.
ADP_GH_TOKEN_BROKER_ENV = "ADP_GH_TOKEN_BROKER_ENABLED"


def _gh_token_broker_enabled(environ: dict | None = None) -> bool:
    """Return True when the GitHub-token gatekeeper is enabled (issue #4272)."""
    env = environ if environ is not None else os.environ
    return env.get(ADP_GH_TOKEN_BROKER_ENV, "").lower() in ("1", "true", "yes")


def _broker_installation_token(
    *,
    installation_id: int,
    repo_owner: str,
    repo_name: str,
    cred_client: GatewayCredentialClient | None = None,
) -> tuple[str, str]:
    """Mint this run's GitHub token through the gateway gatekeeper.

    Issue #4272. Replaces the in-pod ``mint_installation_token`` (and the vault
    read that fed it) so the platform App private key never enters this process.

    Deliberately has NO local-mint fallback: falling back would keep the key in
    pod memory and quietly undo the whole change. A gatekeeper outage is a loud
    bootstrap failure, which the caller surfaces via _fail_bootstrap_status.

    Returns:
        ``(token, app_id)``. The App ID is public (not a credential) and comes
        back from the gateway because the caller still needs it for the bot commit
        identity and for the GH_APP_ID the JS TokenManager gates on — both of
        which used to be read from the vault alongside the private key.

    Raises:
        RuntimeError: if the gateway is not configured for this pod.
        GatewayCredentialError: if the gatekeeper call fails.
    """
    client = cred_client or GatewayCredentialClient()
    if not client.is_configured:
        raise RuntimeError(
            f"{ADP_GH_TOKEN_BROKER_ENV} is on but the gateway is not reachable from this pod "
            "(neither ADP_GATEWAY_ENDPOINT nor VAULT_GATEWAY_URL+VAULT_INTERNAL_API_KEY is set). "
            "Refusing to fall back to an in-pod mint."
        )

    result = client.github_installation_token(
        installation_id=int(installation_id),
        repo_owner=repo_owner,
        repo_name=repo_name,
        purpose="bootstrap GitHub token for agent run",
    )
    return result["token"], str(result.get("app_id") or "")


class PatResolutionResult:
    """Result of _resolve_execution_token() — PAT mode or App fallback."""

    __slots__ = ("token_mode", "token", "github_login", "warning")

    def __init__(
        self,
        token_mode: str = "app",
        token: str | None = None,
        github_login: str = "",
        warning: str | None = None,
    ):
        self.token_mode = token_mode
        self.token = token
        self.github_login = github_login
        self.warning = warning


def _resolve_execution_token(
    *,
    envelope: dict,
    environ: dict,
    cred_client: GatewayCredentialClient | None = None,
    bootstrap_log: "BootstrapLogger | None" = None,
) -> PatResolutionResult:
    """Decide PAT vs App token mode and resolve PAT if needed.

    Extracted from the inline entrypoint logic for testability (issue #3385).

    Args:
        envelope: Parsed SQS envelope dict.
        environ: Environment dict (usually os.environ).
        cred_client: Optional GatewayCredentialClient instance (created if None).
        bootstrap_log: Optional bootstrap logger (steps logged if provided).

    Returns:
        PatResolutionResult with token_mode, resolved token (if PAT), and login.

    Raises:
        RuntimeError: If PAT resolution or validation fails (no App fallback).
    """
    pat_execution_enabled = environ.get(
        "ADP_PAT_EXECUTION_ENABLED", ""
    ).lower() in ("1", "true", "yes")
    token_source = envelope.get("token_source")

    # Not enabled or not PAT → App path
    if not (pat_execution_enabled and token_source == "pat"):
        warning = None
        if token_source == "pat" and not pat_execution_enabled:
            warning = (
                "token_source=pat requested but ADP_PAT_EXECUTION_ENABLED "
                "not set — falling back to App token path"
            )
            logger.warning("%s (message_id=%s)", warning, envelope.get("message_id"))
        return PatResolutionResult(token_mode="app", warning=warning)

    # PAT resolution (C1)
    if bootstrap_log:
        bootstrap_log.step_start(2, "pat_resolve", mode="explicit")

    if cred_client is None:
        cred_client = GatewayCredentialClient()

    actor = envelope.get("actor", {})
    actor_user_id = envelope.get("user_id") or actor.get("user_id", "")
    persona = envelope.get("persona", "")
    message_id = envelope.get("message_id", "")
    source_ref = envelope.get("source_ref", {})
    repo = source_ref.get("repo", "")
    issue = source_ref.get("issue", "")

    try:
        result = cred_client.raw_read(
            user_id=actor_user_id,
            agent_id=persona,
            task_id=message_id or f"{repo}/{issue}",
            service="github",
            label="github-pat",
            purpose="entrypoint: PAT resolution for execution token",
        )
        pat_token = result["value"]
    except GatewayCredentialError as exc:
        if bootstrap_log:
            bootstrap_log.step_error(2, "pat_resolve", exc)
            bootstrap_log.close()
        raise RuntimeError(
            "PAT mode requested (token_source=pat) but credential "
            f"resolution failed: {exc}"
        ) from exc

    if bootstrap_log:
        bootstrap_log.step_success(2, "pat_resolve")

    # PAT zero-token guard (C4) — validate before clone
    if bootstrap_log:
        bootstrap_log.step_start(3, "pat_validate")

    try:
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {pat_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            user_data = json.loads(resp.read().decode("utf-8"))
            github_login = user_data.get("login", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            err = RuntimeError(
                "PAT validation failed: token is expired or revoked. "
                "Please register a new PAT in Settings > Credentials."
            )
        elif exc.code == 403:
            err = RuntimeError(
                "PAT validation failed: token lacks required permissions. "
                "Ensure the PAT has Contents, Issues, and Pull Requests "
                "scopes."
            )
        else:
            err = RuntimeError(f"PAT validation failed: HTTP {exc.code}")
        if bootstrap_log:
            bootstrap_log.step_error(3, "pat_validate", err)
            bootstrap_log.close()
        raise err from exc
    except Exception as exc:
        err = RuntimeError(f"PAT validation failed: {exc}")
        if bootstrap_log:
            bootstrap_log.step_error(3, "pat_validate", err)
            bootstrap_log.close()
        raise err from exc

    if bootstrap_log:
        bootstrap_log.step_success(
            3, "pat_validate", github_login=github_login
        )

    return PatResolutionResult(
        token_mode="pat", token=pat_token, github_login=github_login
    )


def parse_envelope(raw: str) -> dict:
    """Step 1: Parse SQS envelope and extract required fields."""
    env = json.loads(raw)
    required = ("tenant_id", "persona", "source_ref")
    for key in required:
        if key not in env:
            raise ValueError(f"Envelope missing required field: {key}")
    src = env["source_ref"]
    for key in ("installation_id", "repo", "issue"):
        if key not in src:
            raise ValueError(f"source_ref missing required field: {key}")
    return env


def run_cmd(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a shell command, raising on failure."""
    return subprocess.run(
        args, check=True, capture_output=True, text=True, **kwargs
    )  # nosemgrep: dangerous-subprocess-use-audit


def _receive_one_message(queue_url: str, region: str):
    """Block for up to 20s waiting for one SQS message.

    Returns (body, receipt_handle) or (None, None) if the queue is empty
    after the long-poll window. FIFO queues require MessageGroupId-aware
    receive semantics; for single-message-at-a-time processing the defaults
    are fine.
    """
    sqs = boto3.client("sqs", region_name=region)
    resp = sqs.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=20,
        AttributeNames=["All"],
        MessageAttributeNames=["All"],
    )
    messages = resp.get("Messages", [])
    if not messages:
        return None, None
    msg = messages[0]
    return msg["Body"], msg["ReceiptHandle"]


def _delete_message(queue_url: str, region: str, receipt_handle: str) -> None:
    """Ack-by-delete so the message doesn't come back after visibility timeout."""
    boto3.client("sqs", region_name=region).delete_message(
        QueueUrl=queue_url,
        ReceiptHandle=receipt_handle,
    )


# ---------------------------------------------------------------------------
# SQS visibility heartbeat — keeps long-running agent messages in-flight
# without requiring the base visibility_timeout to match max run time.
# ---------------------------------------------------------------------------

# Defaults: extend visibility by 300s every 120s. Missing ~2 consecutive
# heartbeats frees the message (safety margin = 300 - 120 = 180s).
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "120"))
HEARTBEAT_EXTEND = int(os.environ.get("HEARTBEAT_EXTEND", "300"))


class VisibilityHeartbeat:
    """Daemon thread that periodically extends SQS message visibility.

    Ensures a healthy, long-running worker keeps its message in-flight
    indefinitely while a dead worker's message frees in ~5 minutes (the base
    visibility timeout) because the heartbeat stops.

    Usage:
        hb = VisibilityHeartbeat(queue_url, region, receipt_handle)
        hb.start()
        # ... run agent ...
        hb.stop()  # blocks until thread exits
    """

    def __init__(self, queue_url: str, region: str, receipt_handle: str) -> None:
        self._queue_url = queue_url
        self._region = region
        self._receipt_handle = receipt_handle
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._extensions = 0
        self._consecutive_failures = 0

    def start(self) -> None:
        """Start the heartbeat daemon thread."""
        self._thread = threading.Thread(
            target=self._run, name="sqs-visibility-heartbeat", daemon=True
        )
        self._thread.start()
        logger.info(
            "Heartbeat started (interval=%ds, extend=%ds)",
            HEARTBEAT_INTERVAL,
            HEARTBEAT_EXTEND,
        )

    def stop(self) -> None:
        """Signal the heartbeat to stop and wait for it to exit.

        Must be called BEFORE _delete_message to avoid racing the receipt
        handle invalidation.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=HEARTBEAT_INTERVAL + 5)
        logger.info("Heartbeat stopped (total extensions=%d)", self._extensions)

    def _run(self) -> None:
        """Heartbeat loop: sleep for interval, then extend visibility."""
        # Create a per-thread SQS client (boto3 clients are not thread-safe).
        try:
            sqs = boto3.client("sqs", region_name=self._region)
        except Exception as exc:
            logger.warning("Heartbeat: failed to create SQS client: %s", exc)
            return
        while not self._stop_event.wait(timeout=HEARTBEAT_INTERVAL):
            try:
                sqs.change_message_visibility(
                    QueueUrl=self._queue_url,
                    ReceiptHandle=self._receipt_handle,
                    VisibilityTimeout=HEARTBEAT_EXTEND,
                )
                self._extensions += 1
                self._consecutive_failures = 0
                logger.debug("Heartbeat extended visibility (extensions=%d)", self._extensions)
            except Exception as exc:
                self._consecutive_failures += 1
                if self._consecutive_failures >= 3:
                    logger.warning(
                        "Heartbeat: %d consecutive failures (latest: %s). "
                        "Message may become visible for redelivery.",
                        self._consecutive_failures,
                        exc,
                    )
                else:
                    logger.debug("Heartbeat extension failed (will retry): %s", exc)


def _is_already_completed(repo: str, issue: int, token: str) -> bool:
    """Check if the agent branch for this issue already has a merged PR.

    Returns True if the issue has a merged PR from the agent branch
    (agent/issue-NNN), indicating a prior run already completed successfully.
    This is the idempotency guard for SQS redelivery (issue #1864).

    Fail-open: returns False on any error (so the run proceeds normally).
    """
    branch_name = f"agent/issue-{issue}"
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--head",
                branch_name,
                "--state",
                "merged",
                "--json",
                "number",
                "--jq",
                ".[0].number // empty",
            ],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "GH_TOKEN": token},
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.info(
                "Idempotency check: found merged PR #%s on branch %s",
                result.stdout.strip(),
                branch_name,
            )
            return True
    except Exception as exc:
        logger.warning("Idempotency check failed (proceeding with run): %s", exc)
    return False


def _read_run_reports(directory: str = "/tmp") -> tuple[str, str]:
    """Read GitHub's bounded display and the independent explanation archive.

    Older workers only wrote the GitHub display. Preserve it as a clearly
    labeled fallback; never describe that potentially clipped record as full.
    Each read is best-effort so one missing artifact cannot hide the other.
    """
    def read(name: str) -> str:
        try:
            with open(os.path.join(directory, name), "r", encoding="utf-8") as fh:
                return fh.read()
        except FileNotFoundError:
            return ""
        except Exception as exc:
            logger.warning("Could not read report %s (non-fatal): %s", name, exc)
            return ""

    github_text = read("adp-check-run-final.md")
    transcript_text = read("adp-run-transcript.md")
    if not transcript_text.strip():
        transcript_text = (
            "_Archive source: GitHub display fallback. The independent explanation "
            "transcript was unavailable; this record may be truncated or incomplete._\n\n"
            + github_text
        ) if github_text else ""
    return github_text, transcript_text


def _upload_transcript_to_s3(
    final_text: str, repo: str, issue: int, message_id: str, arrived_at: str, persona: str
) -> str | None:
    """Upload the captured explanation transcript or labeled fallback (best-effort).

    Object key: {persona}/{org}/{repo_name}/issue-{issue}/{timestamp}-{run_id}.md

    Returns the S3 object key on success, None on skip/failure.

    Skips silently if AGENT_RUN_LOGS_BUCKET is unset (backward compat for
    un-applied accounts) or if final_text is empty. Failures are logged but
    NEVER affect pod exit code — same contract as check-run finalize.
    """
    bucket = os.environ.get("AGENT_RUN_LOGS_BUCKET", "")
    if not bucket or not final_text:
        return None

    try:
        # Build the S3 object key: {org}/{repo}/issue-{N}/{timestamp}-{run_id}.md
        # arrived_at is ISO format (e.g. "2026-07-06T15:35:57Z"); convert to
        # compact UTC form for key prefix. Fall back to "unknown" on error.
        timestamp = arrived_at.replace("-", "").replace(":", "").replace(".", "")
        # Truncate to YYYYMMDDTHHMMSSz form (strip sub-seconds if present)
        if "T" in timestamp:
            timestamp = timestamp.split("Z")[0] + "Z"
        else:
            timestamp = "unknown"

        # run_id: use first 8 chars of message_id for disambiguation
        run_id = (message_id or "norunid")[:8]

        key = f"{persona}/{repo}/issue-{issue}/{timestamp}-{run_id}.md"

        s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=final_text.encode("utf-8"),
            ContentType="text/markdown",
        )
        logger.info("Transcript uploaded to s3://%s/%s (%d bytes)", bucket, key, len(final_text.encode("utf-8")))
        return key
    except Exception as exc:
        logger.warning("Failed to upload transcript to S3 (non-fatal): %s", exc)
        return None


# ---------------------------------------------------------------------------
# GitLab Tier-A acknowledge path (Issue #3436)
# ---------------------------------------------------------------------------
# Minimal handler for GitLab-originated messages: posts an ack comment on the
# source issue, creates a branch, and deletes the SQS message. Full agent
# execution on GitLab repos (clone, code, MR) is deferred to Phase 1 (#3329).
# ---------------------------------------------------------------------------


def _handle_gitlab_mention(
    envelope: dict,
    queue_url: str,
    region: str,
    receipt_handle: str,
) -> int:
    """Handle a GitLab-originated mention: ack comment + branch create + delete msg.

    Returns 0 on success, 1 on failure. Failures still delete the SQS message
    to prevent FIFO head-of-line blocking (same contract as the poison guard).
    """
    payload = envelope.get("payload", {})
    source = payload.get("source", {})
    project_id = source.get("project_id")
    issue_iid = source.get("issue_iid")
    # URL precedence: envelope field is primary (self-describing message);
    # GITLAB_URL env var is an optional break-glass override only.
    gitlab_url = source.get("gitlab_url", "") or os.environ.get("GITLAB_URL", "")
    persona = envelope.get("persona", "developer")
    correlation = envelope.get("correlation", {})
    correlation_id = correlation.get("correlation_id", "")

    if not gitlab_url or not project_id or not issue_iid:
        logger.error(
            "GitLab path: missing required fields (gitlab_url=%s, project_id=%s, issue_iid=%s)",
            gitlab_url,
            project_id,
            issue_iid,
        )
        _delete_message(queue_url, region, receipt_handle)
        return 1

    # Resolve GitLab API token from Secrets Manager.
    # Single-tenant spike: token secret is adp/<env>/gitlab-api-token.
    # Phase 1 (#3329) will resolve per-tenant tokens.
    env_name = os.environ.get("ENVIRONMENT", os.environ.get("ENV", "dev"))
    secret_name = f"adp/{env_name}/gitlab-api-token"
    try:
        sm = boto3.client("secretsmanager", region_name=region)
        resp = sm.get_secret_value(SecretId=secret_name)
        api_token = resp["SecretString"]
    except Exception as exc:
        logger.error("GitLab path: failed to read API token from %s: %s", secret_name, exc)
        _delete_message(queue_url, region, receipt_handle)
        return 1

    # Strip trailing slash from URL for clean concatenation
    base_url = gitlab_url.rstrip("/")
    headers = {"PRIVATE-TOKEN": api_token, "Content-Type": "application/json"}

    # 1. Post acknowledge comment on the source issue
    ack_body = (
        f"🤖 **Agent `{persona}` acknowledged** this mention.\n\n"
        f"Correlation: `{correlation_id}`\n\n"
        f"_Processing — Tier A acknowledge only (Phase 0 spike)._"
    )
    notes_url = f"{base_url}/api/v4/projects/{project_id}/issues/{issue_iid}/notes"
    note_payload = json.dumps({"body": ack_body}).encode("utf-8")

    ack_failed = False
    try:
        req = urllib.request.Request(notes_url, data=note_payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            logger.info(
                "GitLab ack comment posted: project=%s issue=%s status=%s",
                project_id,
                issue_iid,
                resp.status,
            )
    except Exception as exc:
        logger.error("GitLab path: failed to post ack comment: %s", exc)
        ack_failed = True

    # 2. Resolve default branch from project metadata
    default_branch = "main"  # fallback
    project_url = f"{base_url}/api/v4/projects/{project_id}"
    try:
        req = urllib.request.Request(project_url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            project_data = json.loads(resp.read().decode("utf-8"))
            default_branch = project_data.get("default_branch", "main") or "main"
            logger.info(
                "GitLab default branch resolved: project=%s branch=%s",
                project_id,
                default_branch,
            )
    except Exception as exc:
        logger.warning(
            "GitLab path: failed to resolve default branch, falling back to 'main': %s", exc
        )

    # 3. Create branch agent/issue-<iid> from default branch (idempotent)
    branch_name = f"agent/issue-{issue_iid}"
    branches_url = f"{base_url}/api/v4/projects/{project_id}/repository/branches"
    branch_payload = json.dumps({"branch": branch_name, "ref": default_branch}).encode("utf-8")

    try:
        req = urllib.request.Request(
            branches_url, data=branch_payload, headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            logger.info("GitLab branch created: %s (status=%s)", branch_name, resp.status)
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            # Disambiguate: "already exists" is tolerable; other 400s are real errors
            error_body = ""
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            if "already exists" in error_body.lower():
                logger.info("GitLab branch already exists: %s (400 tolerated)", branch_name)
            else:
                logger.error(
                    "GitLab path: branch create 400 — not 'already exists': %s body=%s",
                    branch_name,
                    error_body,
                )
                ack_failed = True
        else:
            logger.error("GitLab path: failed to create branch: %s (status=%s)", exc, exc.code)
    except Exception as exc:
        logger.error("GitLab path: failed to create branch: %s", exc)

    # 4. Delete the SQS message — always, to prevent FIFO jam
    try:
        _delete_message(queue_url, region, receipt_handle)
        logger.info("GitLab message deleted successfully")
    except Exception as exc:
        logger.error("GitLab path: failed to delete SQS message: %s", exc)
        return 1

    # Return 1 if the ack comment failed (the primary deliverable of Tier A);
    # the message is still deleted above to prevent FIFO jam.
    return 1 if ack_failed else 0


def _fail_bootstrap_status(message_id: str, arrived_at: str, error_message: str) -> None:
    """Mark the webhook-events row failed with a concrete reason. Issue #4030.

    Bootstrap failures used to write no status at all — the first status write
    was the ``in_progress`` transition at the END of bootstrap. So a pod that
    died fetching credentials or cloning left its row at ``webhook_received``,
    which Agent Activity excludes from its default view. The run was not shown
    as failed; it was not shown at all. Operators saw their `@agent-...` comment
    vanish into silence and had to trace Lambda → SQS → KEDA → pod logs by hand.

    Fail-soft by construction: ``update_status`` never raises, and we swallow
    anything it somehow lets through. Reporting a failure must never mask the
    original one, whose traceback is the thing worth propagating.
    """
    if not message_id or not arrived_at:
        # Pre-parse could not recover the row key (PK/SK) — nothing to update.
        logger.warning(
            "Cannot record bootstrap failure status (message_id=%r arrived_at=%r): %s",
            message_id,
            arrived_at,
            error_message,
        )
        return
    try:
        update_invocation_status(
            message_id,
            arrived_at,
            "failed",
            summary=error_message,
            error_message=error_message,
        )
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - defensive
        # Intentionally blind: the caller is mid-failure and about to re-raise.
        # Any exception escaping here would replace a real, diagnosable bootstrap
        # traceback with a bookkeeping error.
        logger.warning("Failed to record bootstrap failure status (non-fatal): %s", exc)


def _load_door_api_key(region: str) -> None:
    """Resolve the Door shared secret into DOOR_API_KEY. Issue #4073, finding #8.

    The Door (context-mcp) authenticates every caller with this key. The Node
    runtime reads it via ``lib/doorAuth.ts``, which looks at ``DOOR_API_KEY``.

    Resolution order:
      1. ``DOOR_API_KEY`` already in the environment (local dev / explicit
         override) — used as-is, no AWS call.
      2. Secrets Manager, at the name in ``ADP_DOOR_API_KEY_SECRET``.

    Degrades gracefully rather than failing the run, mirroring
    ``lib/marker_signing.py``: the Knowledge Layer verbs are an enhancement
    (``KNOWLEDGE_LAYER_ENABLED`` defaults off) and an agent summoned to fix an
    issue must not die because a context-retrieval credential is unavailable. The
    Door is the side that fails closed — it serves nothing without a key. The
    cost of this choice is that a misconfiguration shows up as "the agent had no
    context" rather than a hard error, so both failure paths log at WARNING.
    """
    if os.environ.get("DOOR_API_KEY"):
        logger.debug("DOOR_API_KEY already set in environment; not reading Secrets Manager")
        return

    secret_id = os.environ.get("ADP_DOOR_API_KEY_SECRET")
    if not secret_id:
        logger.warning(
            "ADP_DOOR_API_KEY_SECRET is not set; Knowledge Layer calls to the Door "
            "will be rejected with 401 (issue #4073). Set it on the ScaledJob."
        )
        return

    try:
        sm = boto3.client("secretsmanager", region_name=region)
        os.environ["DOOR_API_KEY"] = sm.get_secret_value(SecretId=secret_id)["SecretString"]
        logger.info("Door API key loaded from %s", secret_id)
    except Exception as exc:  # noqa: BLE001
        # Blind by design: any failure here must degrade to "no Door access",
        # never abort the agent run. Never log the exception's response body —
        # only the secret name and the error text.
        logger.warning(
            "Failed to load Door API key from %s: %s. Knowledge Layer verbs will "
            "return 401 (issue #4073).",
            secret_id,
            exc,
        )


def _describe_vault_fetch_failure(exc: Exception, secret_path: str) -> str:
    """Turn a vault_fetch exception into an operator-actionable reason. #4030.

    Discriminates on the botocore error *code*, not the exception class:
    ``VaultClient.get_secret`` does not wrap anything, so every failure arrives
    as a generic ``ClientError``. Catching them all as "secret missing" would
    send an operator to create a secret that already exists when the real
    problem is an IAM denial.
    """
    code = ""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code", "")

    if code == "ResourceNotFoundException":
        return (
            f"tenant secret missing: {secret_path} — the tenant's GitHub App "
            f"credentials were never provisioned. Repair: aws secretsmanager "
            f"create-secret --name {secret_path} --secret-string "
            f'\'{{"app_id":"<id>","private_key":"<pem>"}}\''
        )
    if code in ("AccessDeniedException", "AccessDenied"):
        return (
            f"access denied reading tenant secret {secret_path} — the secret may "
            f"exist but the worker role cannot read it (check the ScaledJob role's "
            f"secretsmanager:GetSecretValue grant and the secret's KMS key policy)"
        )
    if code == "DecryptionFailure":
        return (
            f"cannot decrypt tenant secret {secret_path} — the worker role lacks "
            f"kms:Decrypt on the secret's KMS key"
        )
    detail = f"{code}: {exc}" if code else str(exc)
    return f"failed to read tenant secret {secret_path} — {detail}"


def _checkout_existing_work_branch(branch: str) -> None:
    """Extend a remote branch even when clone --depth only mapped main.

    A bare `fetch origin branch` otherwise updates FETCH_HEAD alone: checkout
    fails, and finalization later cannot push the missing local branch. Register
    the mapping before fetching so checkout also establishes a usable upstream.
    Never reset an existing local branch or discard its changes.
    """
    run_cmd(["git", "remote", "set-branches", "--add", "origin", branch], cwd=WORK_DIR)
    run_cmd(["git", "fetch", "origin", branch], cwd=WORK_DIR)
    run_cmd(["git", "checkout", branch], cwd=WORK_DIR)


def main() -> int:
    queue_url = os.environ.get("QUEUE_URL")
    if not queue_url:
        logger.error("QUEUE_URL env var is not set")
        return 1
    region = os.environ.get("AWS_REGION", "us-east-1")

    raw_message, receipt_handle = _receive_one_message(queue_url, region)
    if raw_message is None:
        # KEDA spawned us speculatively but the queue drained before we could
        # receive. Exit clean (not an error — KEDA will handle scaling).
        logger.info("No message available after long-poll; exiting cleanly")
        return 0

    # --- Bootstrap Logger: initialized after first parse to get correlation_id ---
    # We do a lightweight pre-parse to extract correlation_id before the full
    # parse_envelope call, so the logger can key the stream by correlation_id.
    _pre = {}
    try:
        _pre = json.loads(raw_message) if isinstance(raw_message, str) else {}
    except (json.JSONDecodeError, TypeError):
        pass
    _corr_pre = (_pre.get("correlation") or {}).get("correlation_id", "")
    _msg_id_pre = _pre.get("message_id", "")
    # Issue #4030: the webhook-events row key (PK=message_id, SK=arrived_at) has
    # to come from the pre-parse too — a parse_envelope failure needs to mark the
    # row failed, and by definition cannot read the parsed envelope to do it.
    _arrived_at_pre = _pre.get("arrived_at", "")
    _env_name = os.environ.get("ENVIRONMENT", os.environ.get("ENV", "dev"))

    bootstrap_log = BootstrapLogger(
        environment=_env_name,
        correlation_id=_corr_pre,
        region=region,
        message_id=_msg_id_pre,
    )

    # Step 1: Parse envelope
    bootstrap_log.step_start(1, "parse_envelope", message_id=_msg_id_pre)
    try:
        envelope = parse_envelope(raw_message)
    except Exception as exc:
        bootstrap_log.step_error(1, "parse_envelope", exc)
        _fail_bootstrap_status(_msg_id_pre, _arrived_at_pre, f"malformed SQS envelope: {exc}")
        bootstrap_log.close()
        raise
    tenant_id = envelope["tenant_id"]
    persona = envelope["persona"]
    source = envelope["source_ref"]
    installation_id = source["installation_id"]
    repo = source["repo"]
    issue = source["issue"]
    message_id = envelope.get("message_id", "")
    arrived_at = envelope.get("arrived_at", "")
    actor = envelope.get("actor", {})

    # Issue #3436: Provider detection — route GitLab messages to the lightweight
    # acknowledge path before the poison guard fires. GitLab envelopes always
    # have installation_id=0 (no GitHub App) so the guard would delete them.
    provider = (envelope.get("payload") or {}).get("provider", "")
    if provider == "gitlab":
        logger.info(
            "GitLab provider detected (message_id=%s, repo=%s, issue=%s). "
            "Routing to GitLab acknowledge path.",
            message_id,
            repo,
            issue,
        )
        bootstrap_log.step_success(
            1, "parse_envelope", tenant_id=tenant_id, persona=persona, repo=repo, issue=issue
        )
        bootstrap_log.close()
        return _handle_gitlab_mention(envelope, queue_url, region, receipt_handle)

    # Issue #2336: Defense-in-depth — if installation_id is 0/None/"0", the
    # token-mint will 404 deterministically. Delete the poison message to
    # prevent FIFO head-of-line blocking and exit cleanly. Only applies to
    # GitHub-path messages (GitLab is routed above).
    if installation_id in (0, None, "0"):
        logger.error(
            "FATAL: installation_id=%r is invalid (message_id=%s, repo=%s, issue=%s). "
            "Deleting poison message to prevent FIFO jam.",
            installation_id,
            message_id,
            repo,
            issue,
        )
        bootstrap_log.step_error(
            1, "parse_envelope", RuntimeError(f"invalid installation_id={installation_id}")
        )
        _fail_bootstrap_status(
            message_id,
            arrived_at,
            f"invalid installation_id={installation_id!r} in envelope — the GitHub App "
            "installation could not be resolved when the webhook was dispatched, so no "
            "token can be minted for this run",
        )
        bootstrap_log.close()
        try:
            _delete_message(queue_url, region, receipt_handle)
            logger.info("Poison message deleted (installation_id=0 guard)")
        except Exception as exc:
            logger.error("Failed to delete poison message: %s", exc)
        return 1

    # Read correlation context from SQS envelope.
    # ENVELOPE CONTRACT: handler.py publishes correlation fields NESTED under
    # envelope["correlation"] (see handler.py:711-718). Do NOT read them top-level.
    corr_ctx = envelope.get("correlation", {}) or {}
    correlation_id = corr_ctx.get("correlation_id", "")
    root_human_id = corr_ctx.get("root_human_id", "")
    is_human_rooted = corr_ctx.get("is_human_rooted", False)

    # Expose correlation context as env vars for the Node agent runtime
    if correlation_id:
        os.environ["ADP_CORRELATION_ID"] = correlation_id
    if root_human_id:
        os.environ["ADP_ROOT_HUMAN_ID"] = root_human_id
    os.environ["ADP_IS_HUMAN_ROOTED"] = "true" if is_human_rooted else "false"

    # Issue #1460: Export the run's own message_id so outbound correlation writes
    # can record which run produced the action (parent edge for lineage).
    if message_id:
        os.environ["ADP_MESSAGE_ID"] = message_id

    # Issue #1696: Export chain depth so outbound markers carry it for cross-agent
    # lineage inheritance. Missing → treat as 0 (chain root / unknown depth).
    chain_depth = corr_ctx.get("chain_depth")
    if chain_depth is not None:
        os.environ["ADP_CHAIN_DEPTH"] = str(chain_depth)
    else:
        os.environ["ADP_CHAIN_DEPTH"] = "0"

    # Issue #1289: Expose personal-context identity for the Node agent runtime.
    # These env vars are read by the worker harness to set X-Owner-Sub and
    # X-Tenant-Id on Context MCP requests. Set from trusted dispatch metadata
    # only — never from agent/LLM input.
    cognito_sub = envelope.get("cognito_sub", "")
    if cognito_sub:
        os.environ["ADP_OWNER_SUB"] = cognito_sub
    # tenant_id is already extracted above; expose it under the personal-context
    # name so the harness doesn't need to know about TENANT_ID vs ADP_TENANT_ID.
    os.environ["ADP_TENANT_ID"] = tenant_id

    # Issue #1591: Expose GitHub login for knowledge-layer code-verb ACL.
    # Code verbs (search/understand/impact/browse) filter by X-GitHub-Login;
    # the Door's allowed_principals stores GitHub logins + team slugs.
    github_login = actor.get("github_login", "")
    if github_login:
        os.environ["ADP_GITHUB_LOGIN"] = github_login

    # Issue #4073 (finding #8): load the Door's shared secret so the Node runtime
    # can authenticate to context-mcp.
    #
    # The identity headers exported just above are exactly what an attacker would
    # forge, so the Door no longer trusts them on their own — every path except
    # /health requires this key. Without it the knowledge-layer MCP tools,
    # experience-save and recall-at-task-start all get 401.
    _load_door_api_key(region)

    repo_owner, repo_name = repo.split("/", 1)
    bootstrap_log.step_success(
        1,
        "parse_envelope",
        tenant_id=tenant_id,
        persona=persona,
        repo=repo,
        issue=issue,
    )
    logger.info(
        "Processing: tenant=%s persona=%s repo=%s issue=#%s correlation=%s",
        tenant_id,
        persona,
        repo,
        issue,
        correlation_id or "(none)",
    )

    # --- Issue #3385: PAT execution path (C1+C4) behind kill-switch ---
    # Gated on ADP_PAT_EXECUTION_ENABLED env var (default absent = dead code).
    # When enabled AND envelope token_source == "pat", resolve PAT from vault
    # and skip App token mint. Otherwise: existing App path, byte-identical.
    _pat_result = _resolve_execution_token(
        envelope=envelope,
        environ=dict(os.environ),
        bootstrap_log=bootstrap_log,
    )
    _token_mode = _pat_result.token_mode
    _pat_token = _pat_result.token
    _pat_github_login = _pat_result.github_login

    # Step 2: Fetch GitHub App credentials from vault (App path — skipped in PAT mode)
    if _token_mode == "pat":
        # PAT resolved above; no vault fetch or token mint needed.
        # Set token variable for downstream use (clone, check-run, etc.)
        token = _pat_token
        app_id = ""
        private_key = ""
    elif _gh_token_broker_enabled():
        # Issue #4272: broker mode. Neither the vault read nor the mint happens
        # in this pod — the gateway holds the App private key and mints a token
        # scoped to this run's own org and repo. private_key stays empty so
        # nothing downstream can export or re-use it; the JS runtime re-mints
        # through the same gatekeeper (see token-refresh.ts broker mode).
        #
        # app_id comes back from the gatekeeper. It is a public identifier, not a
        # credential, and leaving it empty would break two things quietly: the bot
        # commit identity (step 6 builds `<app_id>+adp-agent[bot]@…`), and the
        # GH_APP_ID the JS TokenManager gates on — i.e. no refresh, 1-hour death.
        private_key = ""
        bootstrap_log.step_start(
            2,
            "broker_mint_token",
            installation_id=installation_id,
            repo=f"{repo_owner}/{repo_name}",
        )
        try:
            token, app_id = _broker_installation_token(
                installation_id=installation_id,
                repo_owner=repo_owner,
                repo_name=repo_name,
            )
        except Exception as exc:
            # Loud failure, never silent drift onto a dying/absent token.
            bootstrap_log.step_error(2, "broker_mint_token", exc)
            _fail_bootstrap_status(
                message_id,
                arrived_at,
                f"the GitHub-token gatekeeper could not mint a token for "
                f"installation_id={installation_id} repo={repo_owner}/{repo_name} — the gateway "
                f"may be unreachable, or this run may not be bound to that installation. "
                f"No in-pod fallback exists by design ({ADP_GH_TOKEN_BROKER_ENV} is on): {exc}",
            )
            bootstrap_log.close()
            raise
        bootstrap_log.step_success(2, "broker_mint_token")
    else:
        _secret_rel_path = f"tenants/{tenant_id}/github-app"
        bootstrap_log.step_start(2, "vault_fetch", secret=_secret_rel_path)
        try:
            # Issue #4030: pass the pod's ENVIRONMENT through. VaultClient
            # defaults its prefix from ADP_ENV, which is set nowhere in the
            # ScaledJob pod spec — so it silently resolved adp/dev/... in every
            # environment. Benign in dev, wrong everywhere else, and it would
            # have made the repair hint below name a secret we never tried.
            vault = VaultClient(region=os.environ.get("AWS_REGION", "us-east-1"), env=_env_name)
            app_creds = vault.get_secret(_secret_rel_path)
            app_id = app_creds["app_id"]
            private_key = app_creds["private_key"]
        except Exception as exc:
            bootstrap_log.step_error(2, "vault_fetch", exc)
            _fail_bootstrap_status(
                message_id,
                arrived_at,
                _describe_vault_fetch_failure(exc, f"adp/{_env_name}/{_secret_rel_path}"),
            )
            bootstrap_log.close()
            raise
        bootstrap_log.step_success(2, "vault_fetch", app_id=app_id)

        # Step 3: Mint installation token
        bootstrap_log.step_start(3, "mint_token", app_id=app_id, installation_id=installation_id)
        try:
            token = mint_installation_token(str(app_id), private_key, installation_id)
        except Exception as exc:
            bootstrap_log.step_error(3, "mint_token", exc)
            _fail_bootstrap_status(
                message_id,
                arrived_at,
                f"could not mint a GitHub installation token for app_id={app_id} "
                f"installation_id={installation_id} — the App may be uninstalled, or its "
                f"stored credentials may not match the installation: {exc}",
            )
            bootstrap_log.close()
            raise
        bootstrap_log.step_success(3, "mint_token")

    # Step 3b: Idempotency guard — skip redelivered messages for completed work.
    # If the issue's agent branch already has a MERGED PR, a prior run completed
    # successfully and this message is a stale SQS redelivery (visibility timeout
    # expired before delete). Delete the message and exit cleanly.
    # This is the primary defense against issue #1864 (6h redelivery spawns
    # redundant runs on already-merged stories).
    if _is_already_completed(repo, issue, token):
        logger.info(
            "Idempotency guard: issue #%s already has merged PR on agent branch — "
            "skipping redelivered message (message_id=%s)",
            issue,
            message_id,
        )
        bootstrap_log.step_success(4, "idempotency_guard_skip", issue=issue)
        # Issue #4020: transition the row off webhook_received. This exit used to
        # write no status at all, so the run sat at webhook_received forever — a
        # status Agent Activity filters out of its default view, making a
        # correctly-deduplicated redelivery indistinguishable from a lost one.
        # "skipped" (not "failed"): nothing went wrong, the work already landed.
        update_invocation_status(
            message_id,
            arrived_at,
            "skipped",
            skip_reason="idempotency_merged_pr",
            summary=(
                "Skipped: a merged PR already exists on this issue's agent branch, "
                "so this was a duplicate SQS delivery of completed work."
            ),
        )
        bootstrap_log.close()
        try:
            _delete_message(queue_url, region, receipt_handle)
            logger.info("SQS message deleted (idempotency skip)")
        except Exception as exc:
            logger.error("Failed to delete SQS message during idempotency skip: %s", exc)
        return 0

    # Step 4: Set environment variables
    bootstrap_log.step_start(4, "set_env")

    # Issue #2279: If the envelope carries a validated model_resolved, use it
    # instead of the pod's default ANTHROPIC_MODEL. This implements the
    # /model directive: explicit /model > pod ANTHROPIC_MODEL default.
    model_resolved = envelope.get("model_resolved")
    effective_model = model_resolved or os.environ.get(
        "ANTHROPIC_MODEL", "global.anthropic.claude-opus-5"
    )

    env_vars = {
        "GITHUB_TOKEN": token,
        "GH_TOKEN": token,
        "GIT_ASKPASS": "/usr/local/bin/git-askpass-helper",
        "GIT_TERMINAL_PROMPT": "0",
        "AGENT_TYPE": persona,
        "ISSUE_NUMBER": str(issue),
        "REPO_OWNER": repo_owner,
        "REPO_NAME": repo_name,
        "TARGET_REPO": repo,
        "WORK_DIR": str(WORK_DIR),
        "TENANT_ID": tenant_id,
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "ANTHROPIC_MODEL": effective_model,
    }

    # Issue #3385 (A4): In PAT mode, do NOT export GH_APP_* vars — TokenManager
    # must not run its refresh loop (which would overwrite the PAT with a bot
    # installation token mid-run). Instead export ADP_TOKEN_MODE=pat so the TS
    # side adopts the env GITHUB_TOKEN as-is.
    if _token_mode == "pat":
        env_vars["ADP_TOKEN_MODE"] = "pat"
        # Write PAT to the askpass token file so git-askpass-helper reads it.
        # TokenManager won't overwrite since it has no app credentials.
        # Use 0o600 + atomic rename to prevent world-readable window.
        _token_tmp = "/tmp/.adp-gh-token.tmp"
        _token_path = "/tmp/.adp-gh-token"
        fd = os.open(_token_tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, token.encode())
        finally:
            os.close(fd)
        os.replace(_token_tmp, _token_path)
    else:
        # Credentials the agent-worker.ts TokenManager needs to re-mint an
        # installation token before the 1-hour expiry (#1502). Without a working
        # refresh path, long-running agents die with 401.
        #
        # Issue #4272: in broker mode the private key is NOT exported — the JS
        # side re-mints through the gateway gatekeeper instead. GH_APP_ID and
        # GH_APP_INSTALLATION_ID still go out: the former is harmless (it is a
        # public identifier, not a credential) and the latter is what pins the
        # re-mint to THIS run's org.
        #
        # ADP_GH_TOKEN_BROKER_ENABLED must be exported too. Both initTokenManager
        # call sites (agent-worker.ts, agent-pm.ts) historically gated on the key
        # being present; with the key gone and no flag to key off, they would go
        # false, the token manager would never initialise, no refresh would ever
        # be scheduled, and the run would die silently at the 1-hour mark.
        env_vars["GH_APP_ID"] = str(app_id)
        if _gh_token_broker_enabled():
            env_vars[ADP_GH_TOKEN_BROKER_ENV] = "1"
            # Not setting it is NOT sufficient. The agent subprocess env is
            # os.environ.copy() (see the agent_env assembly below), so any
            # GH_APP_PRIVATE_KEY the pod inherited from somewhere else — a
            # leftover from an earlier code path, a Secret projected into the pod
            # spec, an operator debugging by hand — would still reach the agent
            # and the flag would be silently ineffective. Remove it explicitly so
            # the invariant holds regardless of how the pod env was populated.
            os.environ.pop("GH_APP_PRIVATE_KEY", None)
        else:
            env_vars["GH_APP_PRIVATE_KEY"] = private_key
        # Authoritative installation id for THIS run's target org. The JS worker
        # must re-mint against this installation — NOT installations[0], which is
        # an arbitrary (newest-first) install and resolves to the wrong org once
        # more than one tenant is onboarded, causing 404s on comment/check-run
        # PATCH calls (cross-installation resource access).
        env_vars["GH_APP_INSTALLATION_ID"] = str(installation_id)

    # Issue #2279: Expose model_requested so the worker can post a warning
    # if the requested model was rejected (lenient path).
    model_requested = envelope.get("model_requested")
    if model_requested:
        env_vars["ADP_MODEL_REQUESTED"] = model_requested
    if model_resolved:
        env_vars["ADP_MODEL_RESOLVED"] = model_resolved

    # Issue #3574: Expose /aws-label directive for agent visibility.
    # The label targets a specific linked AWS account within the user's vault.
    aws_label = envelope.get("aws_label")
    if aws_label:
        env_vars["ADP_AWS_LABEL"] = aws_label

    # Vault credential context for adp-cred CLI (#137).
    # task_id flows into STS session tags via the gateway's assume-role
    # endpoint; STS rejects values outside [\p{L}\p{Z}\p{N}_.:/=+\-@]*. The
    # natural shape "<repo>#<issue>" contains '#', which fails STS validation.
    # Sanitize once here so every downstream consumer sees the same safe value.
    task_id = _sanitize_for_sts_tag(message_id or f"{repo}#{issue}")
    user_id = envelope.get("user_id") or actor.get("user_id", "")
    if user_id:
        env_vars["ADP_USER_ID"] = user_id
        env_vars["ADP_AGENT_ID"] = persona
        env_vars["ADP_TASK_ID"] = task_id

    os.environ.update(env_vars)

    bootstrap_log.step_success(4, "set_env")

    # Step 4b: Compose OTEL_RESOURCE_ATTRIBUTES with per-run dimensions (#1630).
    # The ScaledJob template sets static attributes (service.namespace,
    # deployment.environment) and ENABLE_AGENT_OTEL=1 when the flag is on.
    # Here we append the per-message dimensions (tenant, user, persona) that
    # are only known at runtime from the SQS envelope.
    if os.environ.get("ENABLE_AGENT_OTEL") == "1":
        base_attrs = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
        runtime_attrs = [
            f"tenant.id={tenant_id}",
            f"agent.persona={persona}",
        ]
        if user_id:
            runtime_attrs.append(f"enduser.id={user_id}")
        if correlation_id:
            runtime_attrs.append(f"session.id={correlation_id}")
        # Issue #1695: Append GitHub login for human-readable identity on the
        # dashboard. Guarded: only when non-empty (bot/cron paths may lack it).
        # GitHub logins are [A-Za-z0-9-] so no encoding needed for the
        # OTEL_RESOURCE_ATTRIBUTES comma-separated format. Bot suffixes like
        # "[bot]" contain brackets which are safe (not reserved in OTEL attrs).
        if github_login:
            runtime_attrs.append(f"github.login={github_login}")
        # Merge: base (from ScaledJob env) + runtime dimensions
        merged = ",".join(filter(None, [base_attrs] + runtime_attrs))
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = merged

    # Step 5: Clone customer repo
    bootstrap_log.step_start(5, "clone", repo=repo)
    # Username-only URL — GIT_ASKPASS provides the password from $GITHUB_TOKEN
    clone_url = f"https://x-access-token@github.com/{repo}"
    WORK_DIR.parent.mkdir(parents=True, exist_ok=True)
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    try:
        run_cmd(["git", "clone", "--depth=20", clone_url, str(WORK_DIR)])
    except Exception as exc:
        bootstrap_log.step_error(5, "clone", exc)
        _fail_bootstrap_status(
            message_id,
            arrived_at,
            f"could not clone {repo} — check that the GitHub App installation grants "
            f"Contents access to this repository: {exc}",
        )
        bootstrap_log.close()
        raise
    bootstrap_log.step_success(5, "clone", target=str(WORK_DIR))
    logger.info("Cloned %s to %s", repo, WORK_DIR)

    # Step 6: Configure git identity (must come BEFORE WIP branch creation)
    bootstrap_log.step_start(6, "git_config")
    if _token_mode == "pat" and _pat_github_login:
        # Issue #3385: PAT runs act AS the human — use their GitHub identity.
        pat_email = f"{_pat_github_login}@users.noreply.github.com"
        run_cmd(["git", "config", "user.email", pat_email], cwd=WORK_DIR)
        run_cmd(["git", "config", "user.name", _pat_github_login], cwd=WORK_DIR)
    else:
        bot_email = f"{app_id}+adp-agent[bot]@users.noreply.github.com"
        run_cmd(["git", "config", "user.email", bot_email], cwd=WORK_DIR)
        run_cmd(["git", "config", "user.name", "adp-agent[bot]"], cwd=WORK_DIR)
    bootstrap_log.step_success(6, "git_config")

    # Step 6b: Create or reset the agent branch + WIP commit BEFORE exec
    bootstrap_log.step_start(7, "wip_branch", branch=f"agent/issue-{issue}")
    # Create or reset the agent branch + WIP commit BEFORE exec so that:
    #   1. The Check Run attaches to the branch SHA (not default-branch HEAD).
    #   2. Users see a "WIP" commit immediately on the branch.
    #   3. Real agent commits stack cleanly on top.
    #
    # Branch convention `agent/issue-NNN` is fixed (A4 auto-merge, reviewer
    # workflows, operators all rely on it). When this issue has been worked
    # before — typically architect-then-developer in sequence — the remote
    # branch already exists. Two cases:
    #
    #   (a) Stale branch, no open PR:  prior architect/developer run created
    #       a WIP commit but no PR shipped. Force-reset to current main so
    #       this run starts clean. Otherwise the agent's `git fetch`+`merge`
    #       pulls in everything that landed on main since the prior run,
    #       inflating the eventual PR diff with already-merged work.
    #
    #   (b) Branch with an open PR:  operator may be iterating, or an
    #       earlier architect run shipped a PR (rare). Don't force-reset —
    #       extend the existing branch so the PR's review state is preserved.
    #
    # SQS FIFO MessageGroupId=tenant#repo#issue serializes runs on the same
    # issue, so concurrent-run race conditions don't apply here.
    branch_name = f"agent/issue-{issue}"
    wip_sha: str = ""
    work_branch_ready = False
    try:
        # Detect whether the remote branch exists. Use subprocess.run directly
        # because run_cmd hardcodes check=True; we want to inspect returncode.
        remote_check = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--heads", "origin", branch_name],
            cwd=WORK_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        remote_branch_exists = remote_check.returncode == 0

        if remote_branch_exists:
            # Check whether an open PR exists for this branch
            open_pr_check = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    repo,
                    "--head",
                    branch_name,
                    "--state",
                    "open",
                    "--json",
                    "number",
                    "--jq",
                    ".[0].number // empty",
                ],
                cwd=WORK_DIR,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ},
            )
            has_open_pr = bool(open_pr_check.stdout.strip())

            if has_open_pr:
                # (b) Extend the existing branch — preserve the PR's review state.
                logger.info(
                    "Branch %s exists with open PR; extending instead of resetting",
                    branch_name,
                )
                _checkout_existing_work_branch(branch_name)
            elif persona in PERSONAS_EXTENDING_BRANCH:
                # (a-aidlc) AIDLC stages commit artifacts sequentially on one
                # branch without opening a PR until the end. Never delete the
                # remote branch — fetch + extend so prior stage commits survive.
                # Issue #3430.
                logger.info(
                    "Branch %s exists with no open PR; persona=%s is in "
                    "PERSONAS_EXTENDING_BRANCH — extending instead of resetting",
                    branch_name,
                    persona,
                )
                _checkout_existing_work_branch(branch_name)
            else:
                # (a) Stale branch, no PR — delete it and start fresh from main.
                logger.info(
                    "Branch %s exists with no open PR; resetting from main",
                    branch_name,
                )
                subprocess.run(
                    ["git", "push", "--delete", "origin", branch_name],
                    cwd=WORK_DIR,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                run_cmd(["git", "checkout", "-b", branch_name], cwd=WORK_DIR)
        else:
            # First run on this issue — clean creation
            run_cmd(["git", "checkout", "-b", branch_name], cwd=WORK_DIR)

        work_branch_ready = True
        run_cmd(
            ["git", "commit", "--allow-empty", "-m", f"WIP: agent/{persona} starting #{issue}"],
            cwd=WORK_DIR,
        )
        run_cmd(["git", "push", "-u", "origin", branch_name], cwd=WORK_DIR)
        sha_result = run_cmd(["git", "rev-parse", "HEAD"], cwd=WORK_DIR)
        wip_sha = sha_result.stdout.strip()
        bootstrap_log.step_success(7, "wip_branch", sha=wip_sha[:7])
        logger.info("WIP branch %s created; sha=%s", branch_name, wip_sha[:7])
    except Exception as exc:
        bootstrap_log.step_error(7, "wip_branch", exc)
        # Never launch the model on main after a failed branch checkout. A WIP
        # commit/push failure remains nonfatal once the work branch is ready.
        if not work_branch_ready:
            _fail_bootstrap_status(
                message_id, arrived_at, f"could not prepare work branch {branch_name}"
            )
            bootstrap_log.close()
            raise
        logger.warning("WIP commit/push failed (non-fatal): %s", exc)
        # Fall back to the current work-branch HEAD sha for the Check Run
        try:
            sha_result = run_cmd(["git", "rev-parse", "HEAD"], cwd=WORK_DIR)
            wip_sha = sha_result.stdout.strip()
        except Exception:
            pass

    # Create GitHub Check Run (best-effort — failure must NOT fail the pod)
    # Use the WIP commit sha so the check attaches to the agent branch.
    check_run_id: int | None = None
    check_run_url: str = ""
    if wip_sha:
        try:
            cr = create_check_run(
                repo=repo,
                head_sha=wip_sha,
                persona=persona,
                issue=issue,
                token=token,
            )
            check_run_id = cr["id"]
            check_run_url = cr["html_url"]
            # Expose to the node process so CheckRunStreamer can PATCH live updates
            os.environ["CHECK_RUN_ID"] = str(check_run_id)
            logger.info("Check run created: id=%s", check_run_id)
        except Exception as exc:
            logger.warning("Failed to create check run (non-fatal): %s", exc)

    # Step 7: If persona needs AWS, assume customer role via the gateway's
    # assume-role endpoint. Gateway does the STS AssumeRole server-side with
    # session tagging and returns short-lived credentials. Preferred over the
    # raw-read path because credential-assume-role isn't gated by the
    # `vault_raw_read_enabled` feature flag.
    #
    # Issue #3574: When aws_label is non-None, the assume-role call targets a
    # SPECIFIC linked account. Failure is FATAL — we must NOT fall back to
    # label=None (which would silently pick a different account via the ranked
    # picker, reproducing the exact bug this fixes). When aws_label is None,
    # preserve today's non-fatal warning behavior for backward compat.
    if persona in PERSONAS_NEEDING_AWS:
        try:
            sts_creds = _fetch_assumed_aws_credentials(
                user_id=user_id,
                agent_id=persona,
                task_id=task_id,
                label=aws_label,
            )
            os.environ.update(
                {
                    "AWS_ACCESS_KEY_ID": sts_creds["access_key_id"],
                    "AWS_SECRET_ACCESS_KEY": sts_creds["secret_access_key"],
                    "AWS_SESSION_TOKEN": sts_creds["session_token"],
                }
            )
            logger.info(
                "Assumed customer AWS role (user-scoped) via gateway "
                "label=%r provenance_id=%s expires=%s",
                aws_label,
                sts_creds.get("provenance_id"),
                sts_creds.get("expiration"),
            )
        except Exception as exc:
            if aws_label:
                # Issue #3574 invariant 2: explicit label + failure = FATAL.
                # Do NOT retry with label=None — that would silently land in the
                # wrong account (the exact bug this directive fixes).
                logger.error(
                    "FATAL: AWS role assumption failed with explicit label=%r "
                    "(refusing to fallback to ranked picker): %s",
                    aws_label,
                    exc,
                )
                # Issue #4020 (routed from the #4053 review): this exit fires
                # BEFORE the in_progress write below, so #4053's five bootstrap
                # status writes did not cover it — the run vanished from Activity
                # exactly like the failures that issue fixed. The label is
                # operator-supplied and already charset-validated in
                # intent_parser (#3574), so echoing it is safe and is the single
                # most useful detail for diagnosing the failure.
                _fail_bootstrap_status(
                    message_id,
                    arrived_at,
                    f"could not assume the AWS role for the /aws-label {aws_label!r} "
                    "requested in the triggering comment — check that this label is "
                    "linked in your vault and that its role trusts the platform: "
                    f"{exc}",
                )
                bootstrap_log.close()
                raise
            logger.warning("AWS role assumption failed (non-fatal): %s", exc)

    # Step 8: Remove trigger label
    try:
        run_cmd(
            ["gh", "issue", "edit", str(issue), "--remove-label", persona, "-R", repo],
            env={**os.environ},
        )
    except subprocess.CalledProcessError:
        logger.warning("Failed to remove label (non-fatal)")

    # Step 9: Post "started" comment (idempotent via message_id)
    started_marker = f"<!-- adp-run:{message_id} -->"
    _live_link = f"\n\n**Live progress:** [View run ↗]({check_run_url})" if check_run_url else ""
    started_body = (
        f"{started_marker}\n"
        f"🤖 **Agent `{persona}` started** working on this issue."
        f"{_live_link}\n\n"
        f"_Run ID: `{message_id}`_"
    )
    # Prepend correlation marker (Phase 2-d)
    started_body = prepend_correlation_marker(started_body)
    try:
        # Check for existing comment with this marker (idempotency)
        existing = run_cmd(
            [
                "gh",
                "issue",
                "view",
                str(issue),
                "-R",
                repo,
                "--json",
                "comments",
                "--jq",
                f'.comments[].body | select(contains("{started_marker}"))',
            ],
            env={**os.environ},
        )
        if not existing.stdout.strip():
            run_cmd(
                ["gh", "issue", "comment", str(issue), "--body", started_body, "-R", repo],
                env={**os.environ},
            )
            # On success: write pointer + provenance (fail-soft)
            _write_outbound_correlation(repo, f"issue:{issue}", "comment_post")
    except subprocess.CalledProcessError:
        logger.warning("Failed to post started comment (non-fatal)")

    # Stage personas and skills into workspace
    _stage_personas_and_skills()

    # Step 10: Build scoped agent env and exec the agent.
    # ADP_BEDROCK_VIA controls the Bedrock routing path:
    #   - "gateway" (default): route through platform gateway via sigv4-proxy sidecar
    #   - "direct": use pod IRSA to call Bedrock directly (fallback/rollback)
    #   - "platform": alias for "direct" (legacy compat)
    #
    # "user" is RETIRED (#4747) — see RETIRED_BEDROCK_VIA above. Setting it is a
    # startup error, not a silent fallback.
    #
    # When ADP_BEDROCK_VIA=gateway AND the persona has assumed a customer role,
    # the two compose: Bedrock routes through the platform gateway (platform IRSA,
    # platform billing), while the agent's shell `aws ...` commands use the
    # customer's STS creds for deployment / inspection work in the customer
    # account. The sigv4-proxy is started with platform IRSA (customer creds
    # stripped) so it can authenticate to API Gateway's execute-api SigV4.
    #
    # CRITICAL: We build a SEPARATE env dict for the child process. We do NOT
    # mutate os.environ — the entrypoint's post-agent SQS delete needs
    # os.environ to retain IRSA for platform-account access.
    agent_env = os.environ.copy()
    bedrock_via_raw = os.environ.get("ADP_BEDROCK_VIA")
    bedrock_via = (bedrock_via_raw or "gateway").strip().lower()

    # Reject retired routing modes before starting the proxy or spending a token.
    if bedrock_via in RETIRED_BEDROCK_VIA:
        raise RuntimeError(RETIRED_BEDROCK_VIA[bedrock_via])

    # Start sigv4-proxy subprocess for gateway mode.
    # The proxy must sign with platform IRSA (which has execute-api:Invoke on
    # the gateway), not the customer's STS creds. Build a scoped env that
    # strips any customer creds inherited from os.environ.
    proxy_process: subprocess.Popen | None = None

    if bedrock_via == "gateway":
        proxy_env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
        }
        proxy_process = _start_sigv4_proxy(proxy_env, tenant_id)
        if proxy_process is None:
            logger.warning("sigv4-proxy failed to start; falling back to ADP_BEDROCK_VIA=direct")
            bedrock_via = "direct"
        else:
            # Gateway mode: SDK talks to local proxy, proxy re-signs for API GW
            agent_env["CLAUDE_CODE_USE_BEDROCK"] = "1"
            agent_env["ANTHROPIC_BEDROCK_BASE_URL"] = "http://127.0.0.1:9090"
            # Do NOT set ANTHROPIC_BASE_URL — that routes to the broken translator
            agent_env.pop("ANTHROPIC_BASE_URL", None)
            # claude-agent-sdk >= ~0.3.2xx rejects streaming responses whose
            # content-type isn't Bedrock's binary eventstream. Our gateway
            # (API GW → gateway pod) legitimately re-emits Anthropic-style
            # text/event-stream, so the guard is a false positive on this path.
            agent_env["CLAUDE_CODE_DISABLE_BEDROCK_CONTENT_TYPE_GUARD"] = "1"
            logger.info("ADP_BEDROCK_VIA=gateway — routing through sigv4-proxy → API GW")

    if bedrock_via == "direct" or bedrock_via == "platform":
        # Direct Bedrock via pod IRSA (fallback/rollback path)
        agent_env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        agent_env.pop("ANTHROPIC_BEDROCK_BASE_URL", None)
        agent_env.pop("ANTHROPIC_BASE_URL", None)
        logger.info(
            "ADP_BEDROCK_VIA=%r (normalized: %s) — direct Bedrock via pod IRSA",
            bedrock_via_raw,
            bedrock_via,
        )
    elif bedrock_via == "gateway":
        # Gateway-mode Bedrock already wired above. If a customer role was
        # assumed (line 341-345 above), agent_env retains those AWS_* env vars
        # AND retains pod IRSA env vars — the SDK's credential chain prefers the
        # explicit env keys, so shell `aws ...` commands run as the customer.
        # Strip pod IRSA env vars so they don't shadow customer creds for shell AWS.
        if "AWS_ACCESS_KEY_ID" in agent_env:
            for var in ("AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_PROFILE"):
                agent_env.pop(var, None)
            logger.info(
                "ADP_BEDROCK_VIA=gateway with customer role assumed — Bedrock via "
                "platform gateway, customer AWS creds for shell commands"
            )
    else:
        logger.info(
            "ADP_BEDROCK_VIA=%r (normalized: %s) — agent env retains pod IRSA",
            bedrock_via_raw,
            bedrock_via,
        )

    # Update invocation status to in_progress (best-effort)
    # Issue #3385 (C5): include token_mode provenance on the DDB row.
    _keda_job_name = os.environ.get("JOB_NAME", os.environ.get("HOSTNAME", ""))
    update_invocation_status(
        message_id, arrived_at, "in_progress",
        run_id=_keda_job_name,
        token_mode=_token_mode,
    )

    # Issue #3960: mint the control token and register this pod's control endpoint.
    # Ordered deliberately AFTER the in_progress write and BEFORE the agent exec:
    # registration targets a row that exists, and the child env carries the token
    # before the process that starts the listener is created. No-op when the flag
    # is off; on failure control is unavailable and the run proceeds unchanged.
    control_registered = _setup_agent_control(agent_env, message_id, arrived_at)

    # Flush bootstrap logs to CloudWatch before entering the agent phase.
    # From here on, the Node agent SDK / OTEL handles observability.
    bootstrap_log.step_success(8, "bootstrap_complete")
    bootstrap_log.close()

    # Start SQS visibility heartbeat — keeps the message in-flight for the
    # duration of the agent run without requiring a 6h base visibility timeout.
    # A dead worker's heartbeat stops → message frees in ~5min for retry.
    heartbeat = VisibilityHeartbeat(queue_url, region, receipt_handle)
    heartbeat.start()

    logger.info("Execing agent-worker.js with persona=%s branch=%s", persona, branch_name)
    result = subprocess.run(
        ["node", AGENT_BINARY],
        cwd=WORK_DIR,
        env=agent_env,
    )

    # Stop heartbeat BEFORE any message deletion to avoid racing the receipt
    # handle invalidation. Must join to ensure no in-flight API call.
    heartbeat.stop()

    # Terminate sigv4-proxy if it was started
    if proxy_process is not None:
        _stop_sigv4_proxy(proxy_process)

    # Issue #3960: revoke the control credential as soon as the agent process is
    # gone. Before the terminal handlers, not after: those make GitHub API calls
    # that can take seconds or fail, and the window where a token remains valid
    # for a pod whose agent has already exited should be as short as possible.
    _teardown_agent_control(message_id, arrived_at, control_registered)

    # Issue #4186 (Phase 1): persist the SDK session id the Node worker
    # captured, so the identifier outlives the process that created it.
    # Deliberately before the terminal handlers, which overwrite the status but
    # not this field. Observability only — nothing resumes from it yet.
    _record_session_id(message_id, arrived_at)

    # Step 11/12: Post-agent actions
    if result.returncode == 0:
        exit_code = _handle_success(
            repo, issue, branch_name, persona, message_id, arrived_at, check_run_url
        )
    else:
        exit_code = _handle_failure(
            repo, issue, persona, message_id, arrived_at, result.returncode, check_run_url
        )

    # GitHub's clipped display is separate from the readable explanation archive.
    # Read outside the check-run block so archival remains independent of finalize.
    final_text, transcript_text = _read_run_reports()

    # Finalize the Check Run (best-effort — must NOT affect pod exit code)
    if check_run_id is not None:
        try:
            if exit_code == 0:
                cr_conclusion = "success"
                cr_title = f"Agent {persona} completed successfully"
                cr_summary = f"Agent `{persona}` finished processing issue #{issue}."
            else:
                cr_conclusion = "failure"
                cr_title = f"Agent {persona} failed (exit {result.returncode})"
                cr_summary = (
                    f"Agent `{persona}` exited with code {result.returncode} on issue #{issue}."
                )

            # Resolve PR URL (if agent created one on the branch) and include it
            # as details_url so the Check Run links directly to the PR.
            pr_url: str | None = None
            try:
                pr_result = run_cmd(
                    ["gh", "pr", "view", branch_name, "-R", repo, "--json", "url", "--jq", ".url"],
                    env={**os.environ},
                )
                pr_url = pr_result.stdout.strip() or None
            except Exception:
                pass  # PR may not exist yet; non-fatal

            cr_output: dict = {"title": cr_title, "summary": cr_summary}
            if final_text:
                # GitHub hard limit for output.text is 65,535 chars
                cr_output["text"] = final_text[:65535]

            update_kwargs: dict = dict(
                repo=repo,
                check_run_id=check_run_id,
                token=token,
                status="completed",
                conclusion=cr_conclusion,
                output=cr_output,
            )
            if pr_url:
                update_kwargs["details_url"] = pr_url
                logger.info("Attaching PR URL to check run: %s", pr_url)

            update_check_run(**update_kwargs)
        except Exception as exc:
            logger.warning("Failed to finalize check run (non-fatal): %s", exc)

    # Persist the independent transcript, preserving explanations beyond GitHub's
    # display limit. This includes captured explanations and selected previews,
    # not raw tool results or a complete terminal log. Upload remains best-effort.
    transcript_key = _upload_transcript_to_s3(
        transcript_text, repo, issue, message_id, arrived_at, persona
    )

    # Issue #4187: a run the gateway stopped on a spend cap is neither a success
    # nor a crash, so it gets its own terminal status and a reason. Resolved
    # BEFORE the write below because that write is unconditional: it would
    # otherwise overwrite `budget_stopped` with a plain `failed` (the exit code is
    # non-zero either way) and the distinction would be lost again one line after
    # being made.
    stop_reason = _budget_stop_reason(_read_result_metadata())

    # Issue #3069: Write-back the S3 key to the DDB invocation row so the
    # gateway can serve the transcript from the Agent Activity UI.
    # Fail-soft: reuses the same update_invocation_status contract (logs, never raises).
    if transcript_key or stop_reason:
        if stop_reason:
            terminal_status = "budget_stopped"
        else:
            terminal_status = "complete" if exit_code == 0 else "failed"
        update_invocation_status(
            message_id,
            arrived_at,
            terminal_status,
            transcript_key=transcript_key,
            stop_reason=stop_reason,
        )

    # Step 13: Delete the SQS message on ANY terminal exit — success or failure.
    #
    # Rationale: once the pod has reached _handle_success or _handle_failure,
    # it has already posted a comment to GitHub reporting the outcome. The
    # run is terminal. Leaving the message invisible for retry causes two
    # real problems:
    #   1. Head-of-line blocking — the FIFO group (tenant#repo#issue) is
    #      locked for the visibility timeout, blocking subsequent triggers
    #      on the same issue.
    #   2. Pointless retries — the retry runs identically to the first
    #      attempt and posts the same failure comment, spamming the issue.
    #
    # Retries belong at a higher level (human re-labeling or manually calling
    # the webhook) where the operator has had a chance to fix the cause.
    # DLQ now captures the cases where the pod dies WITHOUT reaching this
    # code path (OOM, node eviction, unhandled exception before this line).
    #
    # Issue #4369: with ONE exception. The reasoning above assumes a retry would
    # run identically to the first attempt, which is true for a bad prompt or a
    # code bug — but not for an expired GitHub installation token. There the retry
    # differs in exactly the way that matters (a fresh pod mints a fresh token),
    # and the first attempt produced nothing at all: no commits, no PR, no useful
    # failure comment. Acking that is losing the task. AGENT_EXIT_RETRYABLE is the
    # worker's way of saying so, so we leave the message for redelivery.
    if not _should_ack_message(result.returncode):
        logger.warning(
            "Worker requested retry (exit_code=%d) — leaving SQS message for "
            "redelivery after the visibility timeout",
            result.returncode,
        )
        return exit_code

    try:
        _delete_message(queue_url, region, receipt_handle)
        logger.info("SQS message acked and deleted (exit_code=%d)", exit_code)
    except Exception as exc:
        logger.error("Failed to delete SQS message: %s", exc)
        # Don't fail the pod — agent work already committed to GitHub

    return exit_code


SIGV4_PROXY_SCRIPT = "/app/dist/sigv4-proxy.js"
SIGV4_PROXY_HEALTH_TIMEOUT = 10  # seconds to wait for proxy health


def _start_sigv4_proxy(env: dict, tenant_id: str) -> subprocess.Popen | None:
    """Start the sigv4-proxy subprocess and wait for it to become healthy.

    Returns the Popen object on success, None on failure.
    The proxy listens on 127.0.0.1:SIGV4_PROXY_PORT and re-signs requests
    for the gateway API Gateway using execute-api SigV4.
    """
    import time
    import urllib.request
    import urllib.error

    proxy_target = env.get("SIGV4_PROXY_TARGET", "")
    proxy_port = env.get("SIGV4_PROXY_PORT", "9090")

    if not proxy_target:
        logger.error("SIGV4_PROXY_TARGET not set; cannot start sigv4-proxy")
        return None

    if not Path(SIGV4_PROXY_SCRIPT).exists():
        logger.error("sigv4-proxy script not found at %s", SIGV4_PROXY_SCRIPT)
        return None

    proxy_env = env.copy()
    proxy_env["SIGV4_PROXY_TARGET"] = proxy_target
    proxy_env["SIGV4_PROXY_PORT"] = proxy_port
    proxy_env["TENANT_ID"] = tenant_id
    # Issue #1616: Pass run identity to proxy for per-run cost traceability
    proxy_env["ADP_MESSAGE_ID"] = os.environ.get("ADP_MESSAGE_ID", "")
    proxy_env["ADP_CORRELATION_ID"] = os.environ.get("ADP_CORRELATION_ID", "")

    try:
        proc = subprocess.Popen(
            ["node", SIGV4_PROXY_SCRIPT],
            env=proxy_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except Exception as exc:
        logger.error("Failed to spawn sigv4-proxy: %s", exc)
        return None

    # Wait for health check
    health_url = f"http://127.0.0.1:{proxy_port}/__health"
    deadline = time.monotonic() + SIGV4_PROXY_HEALTH_TIMEOUT
    while time.monotonic() < deadline:
        # Check the process hasn't crashed
        if proc.poll() is not None:
            logger.error("sigv4-proxy exited prematurely (code=%d)", proc.returncode)
            return None
        try:
            resp = urllib.request.urlopen(health_url, timeout=1)
            if resp.status == 200:
                logger.info(
                    "[sigv4-proxy] healthy on port %s, target=%s",
                    proxy_port,
                    proxy_target,
                )
                return proc
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.3)  # nosemgrep: arbitrary-sleep

    # Timeout — kill and return None
    logger.error("sigv4-proxy health check timed out after %ds", SIGV4_PROXY_HEALTH_TIMEOUT)
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    return None


def _stop_sigv4_proxy(proc: subprocess.Popen) -> None:
    """Gracefully stop the sigv4-proxy subprocess."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    logger.info("sigv4-proxy stopped (exit=%s)", proc.returncode)


def _fetch_assumed_aws_credentials(
    *, user_id: str, agent_id: str, task_id: str, label: str | None = None
) -> dict:
    """Get short-lived AWS credentials via the gateway's assume-role endpoint.

    Calls POST /internal/v1/credential-assume-role with service="aws". The
    gateway resolves the user's aws_role credential, performs STS AssumeRole
    server-side with session tagging, and returns ready-to-use temp creds.

    Preferred over the raw-read path because credential-assume-role isn't
    gated by the `vault_raw_read_enabled` feature flag (default off).

    Args:
        user_id: Platform user_id of the acting user.
        agent_id: Agent persona identifier.
        task_id: Unique task/run identifier.
        label: Optional credential label targeting a specific linked account
            (issue #3574). When non-None, the gateway's CredentialResolver
            adds a WHERE label=:label filter WITHIN the authorized user's vault.

    Returns:
        Dict with {profile_name, access_key_id, secret_access_key,
        session_token, expiration, region, provenance_id}.

    Raises:
        GatewayCredentialError: If the gateway call fails.
        ValueError: If the user_id is empty (no acting user in envelope).
    """
    if not user_id:
        raise ValueError(
            "Cannot fetch user-scoped AWS credentials: no user_id in envelope. "
            "Ensure the envelope includes actor.user_id."
        )

    gw_client = GatewayCredentialClient()
    if not gw_client.is_configured:
        raise GatewayCredentialError(
            "Gateway credential client not configured. "
            "Set ADP_GATEWAY_ENDPOINT (preferred, uses IRSA/SigV4) "
            "or VAULT_GATEWAY_URL + VAULT_INTERNAL_API_KEY (legacy)."
        )

    return gw_client.assume_role(
        user_id=user_id,
        agent_id=agent_id,
        task_id=task_id,
        service="aws",
        label=label,
        purpose="entrypoint: assume customer AWS role",
    )


def _stage_personas_and_skills() -> None:
    """Copy personas and skills from image into the workspace."""
    if PERSONAS_DIR.exists():
        target = WORK_DIR / ".adp-rules" / "personas"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(PERSONAS_DIR, target, dirs_exist_ok=True)
    if SKILLS_DIR.exists():
        target = WORK_DIR / ".claude" / "skills"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(SKILLS_DIR, target, dirs_exist_ok=True)

    # Image-baked runtime artifacts — never commit them. `.git/info/exclude`
    # is a per-clone gitignore that isn't tracked, so `git add -A` (and the
    # agent's own git-add calls) skip these paths. Tracked files in the
    # customer repo at the same paths keep working — exclude only affects
    # untracked files.
    exclude_file = WORK_DIR / ".git" / "info" / "exclude"
    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    with exclude_file.open("a") as f:
        f.write("\n.adp-rules/\n.claude/skills/\n")


# Path where the Node agent-worker persists SDK result metadata (cost/turns).
# Mirrors CheckRunStreamer's /tmp/adp-check-run-final.md bridge.
RESULT_METADATA_PATH = "/tmp/adp-result-metadata.json"


def _read_result_metadata() -> dict | None:
    """Read the SDK result metadata the Node worker wrote, or None if absent.

    The file contains {subtype, total_cost_usd, num_turns}. Fail-soft: any
    read/parse error returns None (treated as "no signal available").
    """
    try:
        with open(RESULT_METADATA_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# Absolute ceiling on a control token's lifetime (Issue #3960). Applied on top of
# the pod deadline, so raising `agent_pod_deadline_seconds` cannot quietly extend
# how long a leaked credential stays valid.
MAX_CONTROL_TOKEN_TTL_SECONDS = 6 * 60 * 60


def _is_agent_control_enabled() -> bool:
    """Strict, fail-closed read of the worker's own control flag (Issue #3960).

    Only the exact string ``"true"`` enables. Read here rather than inherited from
    any gateway-side decision: the gateway runs an independent reader, and a
    gateway flag that could start a listener in a pod would let one config change
    open a port the ingress NetworkPolicy may not yet cover (revival-design §3).
    """
    return os.environ.get("FEATURE_AGENT_CONTROL_ENABLED", "").strip() == "true"


def _control_port() -> int:
    """The pinned control port. Pinned because the ingress policy names one port.

    Reads ``ADP_CONTROL_PORT`` — the name the ScaledJob template renders from
    ``var.agent_control_port`` (scaledjob.tf) and the same name the Node listener
    reads (agent-worker.ts). One name across all three sides is deliberate: an
    earlier revision read ``AGENT_CONTROL_PORT`` here while Terraform injected
    ``ADP_CONTROL_PORT``, so a configured non-default port was silently ignored
    and the pod bound 8770 while the policy allowed the configured port. Nothing
    errors in that state; the listener is simply unreachable.

    Invalid or absent values resolve to the default rather than to an arbitrary
    port: a pod listening on a port the policy does not cover is unreachable, and
    that failure surfaces as a mysterious timeout rather than a config error.
    """
    raw = os.environ.get("ADP_CONTROL_PORT", "").strip()
    if raw.isdigit() and 0 < int(raw) < 65536:
        return int(raw)
    return 8770


def _setup_agent_control(
    agent_env: dict,
    message_id: str,
    arrived_at: str,
) -> bool:
    """Mint a per-run control token and register this pod's control endpoint.

    Issue #3960. Returns True when the run's control channel is registered and the
    child env carries what the listener needs to start.

    **The token is minted here, in the pod, and never travels inbound.** It is
    generated with ``secrets.token_urlsafe`` (a CSPRNG — never ``random``), handed
    to the Node worker through its env, and written to the invocation row so the
    gateway can present it. No component outside this pod chooses it, so a
    compromised gateway cannot pick a token for a pod, and a token cannot be
    reused across runs.

    **Registration precedes the listener, and both are gated on the flag.** When
    the flag is off, nothing is minted, nothing is written and no port is
    advertised — the row is byte-identical to a run without this feature (FR-1.1).

    **The token's lifetime is bounded by the pod's, not by a fixed window.** The
    expiry is derived from ``ADP_POD_DEADLINE_SECONDS`` — the same
    ``activeDeadlineSeconds`` Kubernetes enforces on this pod — so a token cannot
    outlive the process it authenticates; a leaked token from a finished run is
    already expired even if terminal cleanup never ran.

    **The generation is assigned by the invocation row, not read from config.**
    ``register_control_endpoint`` returns it from an atomic increment, so a retry
    pod for the same message gets a strictly higher number than the attempt it
    replaces and the listener's generation check has something real to compare.

    Fail-soft but *loud*: any failure returns False, is logged, and leaves control
    unavailable. Control is an observability/intervention add-on; it must never
    abort the run it is attached to. What it must not do is fail silently, since
    the UI would then offer a channel that does not exist (FR-1.12, NFR-10).
    """
    if not _is_agent_control_enabled():
        logger.info("Agent control disabled (FEATURE_AGENT_CONTROL_ENABLED not 'true')")
        return False

    # The pod IP arrives via the downwardAPI. Absent means the deployment did not
    # project it — treated as a hard stop, never as a licence to bind every
    # interface, which is the whole point of the explicit-bind requirement.
    pod_ip = os.environ.get("POD_IP", "").strip()
    if not pod_ip:
        logger.error(
            "Agent control enabled but POD_IP is not set — control unavailable. "
            "The scaledjob must project status.podIP via the downwardAPI."
        )
        return False

    try:
        # 32 bytes of CSPRNG entropy. `secrets`, not `random`: `random` is
        # deterministic from its seed and is not a credential source.
        token = secrets.token_urlsafe(32)
        port = _control_port()

        # Bound by the pod deadline so the credential cannot outlive the listener
        # that honours it.
        expires_at = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() + _control_token_ttl_seconds()),
        )

        # The generation comes back from the write. It is not computed here: see
        # register_control_endpoint for why the row is the only source that
        # actually differs between attempts.
        generation = register_control_endpoint(
            message_id,
            arrived_at,
            address=pod_ip,
            port=port,
            token=token,
            token_expires_at=expires_at,
        )
        if generation is None:
            # Deliberately does NOT start the listener. An unregistered listener is
            # an open port nothing can reach through the policy and nothing knows
            # the token for: pure attack surface with no capability.
            logger.error(
                "Control endpoint registration failed — not starting listener "
                "(control_registration_failed)"
            )
            return False

        # Child env only. os.environ is untouched so the token does not leak into
        # any other subprocess this entrypoint spawns (gh, git, the sigv4 proxy).
        agent_env["ADP_CONTROL_TOKEN"] = token
        agent_env["ADP_CONTROL_PORT"] = str(port)
        agent_env["ADP_CONTROL_BIND_ADDRESS"] = pod_ip
        agent_env["ADP_CONTROL_GENERATION"] = str(generation)

        # Armed only after the write succeeded, so there is no path where a
        # teardown is scheduled for a registration that never happened.
        _install_control_teardown_guard(message_id, arrived_at)

        logger.info(
            "Agent control registered: port=%d generation=%d expires_at=%s",
            port,
            generation,
            expires_at,
        )
        return True
    except Exception as exc:
        logger.error("Agent control setup failed (control unavailable): %s", exc)
        return False


def _control_token_ttl_seconds() -> int:
    """Token TTL, bounded by the deadline Kubernetes actually enforces.

    Reads ``ADP_POD_DEADLINE_SECONDS``, which the ScaledJob renders from the same
    ``var.agent_pod_deadline_seconds`` it passes to ``activeDeadlineSeconds``. The
    two therefore cannot drift: whatever wall-clock limit the pod is killed at is
    the limit the credential expires at.

    Falls back to ``MAX_CONTROL_TOKEN_TTL_SECONDS`` when unset or unparseable, and
    never exceeds it. The cap is not redundant with the deadline: an operator can
    raise ``agent_pod_deadline_seconds``, and an unbounded TTL would silently turn
    a leaked token into a near-permanent one. A too-short TTL only costs the
    ability to control a long run's tail; a too-long one is a live credential for
    a pod that no longer exists.
    """
    raw = os.environ.get("ADP_POD_DEADLINE_SECONDS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return min(int(raw), MAX_CONTROL_TOKEN_TTL_SECONDS)
    return MAX_CONTROL_TOKEN_TTL_SECONDS


# Registered control channel awaiting teardown, or None. Module-level because the
# backstops that consume it — an atexit hook and a SIGTERM handler — cannot be
# passed arguments (Issue #3960).
_pending_control_teardown: tuple[str, str] | None = None


def _install_control_teardown_guard(message_id: str, arrived_at: str) -> None:
    """Arrange for the control credential to be revoked however this pod ends.

    The normal path calls :func:`_teardown_agent_control` right after the agent
    process exits, which is where teardown *should* happen — as early as possible.
    This guard exists for the paths that never reach that line:

    * an exception anywhere in the post-agent handling (PR creation, check-run
      finalisation, S3 upload — all of which make network calls that can raise),
    * ``activeDeadlineSeconds`` expiring, which is a SIGTERM from Kubernetes,
    * a node drain or eviction, likewise SIGTERM.

    Without it, those endings leave a live token and a pod IP on the row. That is
    the dangerous residue: pod IPs get reused, so a stale address eventually names
    somebody else's pod, and the token stays valid until its expiry. The gateway
    defends independently (it refuses terminal runs and checks expiry), but a
    credential should not depend on a second component declining to use it.

    SIGTERM is handled rather than left to the default so the revocation happens
    inside the grace period; the handler then re-raises the signal with the default
    disposition so the exit status and observable behaviour are unchanged.
    """
    global _pending_control_teardown
    _pending_control_teardown = (message_id, arrived_at)

    atexit.register(_revoke_pending_control)
    try:
        signal.signal(signal.SIGTERM, _control_sigterm_handler)
    except (ValueError, OSError) as exc:
        # Only possible off the main thread. Not fatal: atexit still covers the
        # exception paths, and control is an add-on that must never break a run.
        logger.warning("Could not install control teardown signal handler: %s", exc)


def _control_sigterm_handler(signum, _frame) -> None:
    """Revoke the control credential, then die exactly as SIGTERM would have."""
    _revoke_pending_control()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def _revoke_pending_control() -> None:
    """Idempotent backstop: clear the control record if it has not been cleared.

    Idempotent by clearing the pending key first, so the normal-path call, the
    atexit hook and a SIGTERM arriving mid-teardown cannot produce a second write.
    """
    global _pending_control_teardown
    pending, _pending_control_teardown = _pending_control_teardown, None
    if pending is None:
        return
    try:
        clear_control_endpoint(*pending)
    except Exception as exc:
        logger.warning("Control endpoint teardown failed (non-fatal): %s", exc)


def _teardown_agent_control(message_id: str, arrived_at: str, was_registered: bool) -> None:
    """Remove the control token and address at terminal teardown.

    Skipped entirely when registration never happened, so a flag-off run performs
    no control writes at all — including no deletes, which would otherwise be an
    observable difference from a run predating the feature.
    """
    if not was_registered:
        return
    _revoke_pending_control()


def _record_session_id(message_id: str, arrived_at: str) -> str | None:
    """Record the SDK session id on the invocation row (issue #4186, Phase 1).

    The Node worker captures the session id mid-stream and writes it to
    RESULT_METADATA_PATH; this reads it back and persists it to DynamoDB. The
    handover goes through the file rather than an env var because the DDB row
    key is (event_id=message_id, arrived_at) and ``arrived_at`` never reaches
    the Node process — so Python has to own the write, and keeping it here also
    keeps a single DDB writer.

    Observability only. Nothing reads this field to resume a run: the resume
    branch is Phase 3 and is not implemented. Writing it changes no run
    outcome.

    Status is re-asserted as ``in_progress`` because that is the row's current
    value at this point (set before the agent exec) — this call must add a
    field, never move the status. The terminal handlers that run after this set
    the real terminal status.

    Returns the session id written, or None if there was nothing to write.

    Fail-soft twice over: update_invocation_status already logs rather than
    raising, and this wraps it anyway. This runs after a completed agent exec
    but before the terminal handlers post their outcome, so an exception here
    would turn a successful run into a failed pod — over a field that is purely
    observational.
    """
    try:
        meta = _read_result_metadata()
        session_id = (meta or {}).get("session_id")
        if not isinstance(session_id, str) or not session_id:
            logger.debug("No SDK session id in result metadata; skipping session_id write")
            return None

        update_invocation_status(
            message_id,
            arrived_at,
            "in_progress",
            session_id=session_id,
        )
        logger.info("Recorded SDK session id on invocation row: %s", session_id)
        return session_id
    except Exception as exc:
        logger.warning("Failed to record SDK session id (non-fatal): %s", exc)
        return None


def _should_ack_message(worker_exit_code: int) -> bool:
    """Should the SQS message be deleted for a worker that exited with this code?

    True for every terminal outcome (issue #2117's deliberate ack-on-failure: the
    pod already reported the outcome to GitHub, and leaving the message invisible
    causes head-of-line blocking on the FIFO group plus duplicate failure comments).

    False only for AGENT_EXIT_RETRYABLE (issue #4369). That reasoning assumes a
    retry would behave identically to the first attempt — true for a bad prompt or
    a code bug, false for an expired GitHub installation token, where a fresh pod
    mints a fresh token and the first attempt produced nothing at all (no commits,
    no PR). Acking that loses the task outright, which is strictly worse than the
    bug being fixed, so the message is left to redeliver when its visibility
    timeout lapses (bounded by the queue's maxReceiveCount before the DLQ).
    """
    return worker_exit_code != AGENT_EXIT_RETRYABLE


def _budget_stop_reason(meta: dict | None) -> str | None:
    """Return the spend-cap stop reason from SDK metadata, or None (issue #4187).

    The Node worker records ``budget_stopped`` when the gateway refuses a model
    call with a 402 naming an exhausted cap. Reading it here is what turns that
    into an operator-visible outcome: without it the run lands as a generic
    ``failed`` with an HTTP error in the transcript, which reads as a platform
    bug rather than the cap doing its job.

    Fail-soft, like every other reader of this file: anything unexpected returns
    None and the existing success/failure classification stands.
    """
    if not meta or not meta.get("budget_stopped"):
        return None
    reason = meta.get("stop_reason")
    # A static enum, rendered as prose by the UI — same contract as skip_reason.
    return str(reason) if reason else "budget_cap_exceeded"


def _is_zero_token_failure(meta: dict | None) -> bool:
    """True when the SDK result signature indicates the model call never ran.

    A genuine "no changes needed" verdict costs >0 tokens (the model must read
    the issue to decide), so a run that burned $0.0000 across a single turn is a
    reliable discriminator for an infrastructure failure — Bedrock model access
    / agent registry / sigv4 chain — that the SDK swallowed and returned
    gracefully (issue #2883). Requires BOTH cost==0 AND turns<=1; if either
    field is missing we cannot conclude failure and return False (fail open to
    the existing success path — no regression on partial failures).
    """
    if not meta:
        return False
    cost = meta.get("total_cost_usd")
    turns = meta.get("num_turns")
    if cost is None or turns is None:
        return False
    try:
        return float(cost) == 0.0 and int(turns) <= 1
    except (TypeError, ValueError):
        return False


def _register_authored_draft(persona: str, issue: int) -> str:
    """Register the run's loop proposal with the engine; return a comment section.

    Issue #4528. Runs only for the authoring personas, and only after Step 11 has
    pushed the branch — the committed markdown is the source of truth, and the
    engine draft is a view of it, so the artifacts must be safe on the remote
    before anything tries to turn them into engine state.

    Fail-soft with no error handling here: `draft_registration_note` never raises
    and returns "" when registration does not apply. A failed registration becomes
    a warning section in the closing comment and the run still succeeds — the
    issue's third bug class ("compile failure kills the AIDLC run").
    """
    if persona not in PERSONAS_REGISTERING_DRAFTS:
        return ""
    return draft_registration_note(work_dir=WORK_DIR, issue=issue)


def _outcome_report_link(meta: dict | None, repo: str, issue: int) -> str:
    """Reference the worker's single outcome report without trusting arbitrary URLs."""
    url = (meta or {}).get("outcome_comment_url")
    prefix = f"https://github.com/{repo}/issues/{issue}#issuecomment-"
    if isinstance(url, str) and url.startswith(prefix) and url[len(prefix):].isdigit():
        return f"\n\n[Outcome, remaining work and next action]({url})."
    return ""


def _handle_success(
    repo: str,
    issue: int,
    branch: str,
    persona: str,
    message_id: str,
    arrived_at: str,
    check_run_url: str = "",
) -> int:
    """Step 11: Commit remaining changes, push branch, create PR if needed."""
    try:
        # Commit any uncommitted changes the agent left behind.
        # (Agents normally commit their own work; this is a safety net.)
        diff = run_cmd(["git", "diff", "--stat"], cwd=WORK_DIR)
        status_out = run_cmd(["git", "status", "--porcelain"], cwd=WORK_DIR)
        has_uncommitted = bool(diff.stdout.strip() or status_out.stdout.strip())

        if has_uncommitted:
            run_cmd(["git", "add", "-A"], cwd=WORK_DIR)
            run_cmd(
                ["git", "commit", "-m", f"feat: agent/{persona} work for #{issue}"],
                cwd=WORK_DIR,
            )

        # Push any commits that haven't been pushed yet (WIP + agent commits).
        # The branch tracking was set up during WIP commit creation, so a plain
        # "git push origin branch" is sufficient.
        try:
            unpushed = run_cmd(
                ["git", "log", f"origin/{branch}..HEAD", "--oneline"],
                cwd=WORK_DIR,
            )
            has_unpushed = bool(unpushed.stdout.strip())
        except subprocess.CalledProcessError:
            has_unpushed = has_uncommitted  # fallback: push if we just committed

        if not has_uncommitted and not has_unpushed:
            # Only the empty WIP commit is on the branch from the entrypoint's
            # view — but the AGENT may have self-pushed and self-opened a PR
            # during its exec (the common case: agent-worker.js runs `git push`
            # + `gh pr create` itself). In that case there are no changes left
            # for the entrypoint to push, yet a PR exists whose body needs the
            # correlation marker for the webhook reviewer trigger (#1696/#1721).
            # Backfill it before returning — this is the path #1723 missed
            # (the backfill was only wired into the entrypoint-creates-PR block,
            # which this early return never reaches).
            logger.info("No local changes or unpushed commits remain")

            # Distinguish a genuine "no changes needed" verdict from an
            # infrastructure failure the SDK swallowed (issue #2883). A run that
            # burned $0.0000 across a single turn never actually reached the
            # model (Bedrock AccessDenied, sigv4 403, throttling); reporting it
            # as success masks the real error in pod logs only. Fail the check
            # run with a diagnostic instead.
            meta = _read_result_metadata()
            if _is_zero_token_failure(meta):
                logger.error(
                    "Zero-token/single-turn result signature detected "
                    "(cost=%s turns=%s subtype=%s) — treating as infrastructure "
                    "failure, not 'no changes needed'",
                    meta.get("total_cost_usd"),
                    meta.get("num_turns"),
                    meta.get("subtype"),
                )
                diagnostic = (
                    f"Agent `{persona}` failed: the model call never succeeded "
                    "(0 tokens burned). Likely causes: Bedrock model access / "
                    "agent registry / sigv4 chain. See pod logs for the "
                    "underlying error."
                )
                _post_comment(repo, issue, message_id, "failed", diagnostic, check_run_url)
                update_invocation_status(
                    message_id,
                    arrived_at,
                    "failed",
                    summary=f"{persona} — model call never succeeded (0 tokens)",
                )
                return 1

            self_pr = _find_open_pr(repo, branch)
            if self_pr:
                _ensure_pr_body_marker(repo, self_pr, branch)
            # The authoring persona reaches this branch on the common path: it
            # commits and pushes its own artifacts during the run, so the
            # entrypoint finds nothing left to push. Registration therefore has to
            # be wired here too, not only on the PR-creating path below.
            draft_note = _register_authored_draft(persona, issue)
            if self_pr:
                git_outcome = f"PR #{self_pr} is open: https://github.com/{repo}/pull/{self_pr}."
            else:
                git_outcome = "No local changes remain to push; task completion is not verified by this check."
            summary = f"Agent `{persona}` run ended. {git_outcome}" + _outcome_report_link(meta, repo, issue)
            _post_comment(
                repo,
                issue,
                message_id,
                "completed",
                f"{summary}\n\n{draft_note}" if draft_note else summary,
                check_run_url,
            )
            update_invocation_status(
                message_id,
                arrived_at,
                "complete",
                summary=f"{persona} — run ended; " + (f"PR #{self_pr} open" if self_pr else "no local changes to push"),
            )
            return 0

        if has_unpushed or has_uncommitted:
            run_cmd(["git", "push", "origin", branch], cwd=WORK_DIR)

        # Create PR if one doesn't already exist on this branch
        pr_already_exists = False
        existing_pr_number = ""
        transcript_only = False
        try:
            existing_pr = run_cmd(
                [
                    "gh",
                    "pr",
                    "list",
                    "--head",
                    branch,
                    "-R",
                    repo,
                    "--json",
                    "number",
                    "--jq",
                    ".[0].number",
                ],
                env={**os.environ},
            )
            existing_pr_number = existing_pr.stdout.strip()
            pr_already_exists = bool(existing_pr_number)
        except subprocess.CalledProcessError:
            pass

        # Reviewer runs deliver their output as PR comments; the transcript
        # under data/code-review/ is an archival by-product. Opening a PR whose
        # ONLY content is transcripts created ~20 junk PRs/day (567 open at the
        # 2026-07-29 cleanup — the "reviewer PR queue noise" pattern). If every
        # changed file on the branch is a transcript, push it (archival) but do
        # NOT open a PR. Any non-transcript file keeps the normal PR flow, so a
        # reviewer that fixes code during review still gets its PR.
        if not pr_already_exists and _branch_changes_are_transcript_only(branch):
            logger.info(
                "Branch %s contains only data/code-review/ transcripts — "
                "skipping PR creation (review was delivered as PR comments)",
                branch,
            )
            transcript_only = True
            pr_already_exists = True  # skip the create block below

        if not pr_already_exists:
            pr_body = f"Automated work by agent `{persona}` for #{issue}.\n\nRun ID: `{message_id}`"
            pr_body = prepend_correlation_marker(pr_body)
            run_cmd(
                [
                    "gh",
                    "pr",
                    "create",
                    "--title",
                    f"[{persona}] Agent work for #{issue}",
                    "--body",
                    pr_body,
                    "--head",
                    branch,
                    "-R",
                    repo,
                ],
                env={**os.environ},
            )
            # On success: write pointer + provenance for the PR (fail-soft)
            _write_outbound_correlation(repo, f"pr:{branch}", "pr_create")
        elif existing_pr_number:
            # The agent opened its OWN PR (via the SDK's `gh pr create`), so the
            # entrypoint's marker-prepend above was skipped. Agent-authored PR
            # bodies therefore carry NO adp-* correlation marker — which means
            # the webhook's marker-gated reviewer trigger (issue #1696) blocks
            # the PR and cross-agent lineage is lost (issue #1721). Backfill it:
            # edit the PR body to prepend the marker if it isn't already there.
            _ensure_pr_body_marker(repo, existing_pr_number, branch)
        draft_note = _register_authored_draft(persona, issue)
        if transcript_only:
            git_outcome = f"Review transcripts were pushed to `{branch}`; no PR was created for them."
        elif existing_pr_number:
            git_outcome = f"PR #{existing_pr_number} is open: https://github.com/{repo}/pull/{existing_pr_number}."
        else:
            git_outcome = f"PR opened on branch `{branch}`; merge and deployment are not verified by this check."
        summary = f"Agent `{persona}` run ended. {git_outcome}" + _outcome_report_link(_read_result_metadata(), repo, issue)
        _post_comment(
            repo,
            issue,
            message_id,
            "completed",
            f"{summary}\n\n{draft_note}" if draft_note else summary,
            check_run_url,
        )
        update_invocation_status(
            message_id,
            arrived_at,
            "complete",
            summary=f"{persona} — run ended; " + ("review transcripts pushed" if transcript_only else f"PR on {branch}"),
        )
    except subprocess.CalledProcessError as exc:
        logger.error("Post-agent git/PR step failed: %s", exc.stderr or exc)
        update_invocation_status(
            message_id,
            arrived_at,
            "failed",
            summary=f"{persona} — post-agent step failed",
        )
        return 1
    return 0


def _branch_changes_are_transcript_only(branch: str) -> bool:
    """True if every file the branch changes vs origin/main is a review transcript.

    Used by Step 11 to suppress PR creation for reviewer runs whose only
    output is data/code-review/*.md (the review itself was delivered as PR
    comments). Fail-soft: any error returns False so the normal PR flow runs —
    a spurious PR is recoverable; a silently missing PR for real work is not.
    An empty diff also returns False (that case is handled earlier in Step 11).
    """
    try:
        run_cmd(["git", "fetch", "origin", "main", "--depth=1"], cwd=WORK_DIR)
        changed = run_cmd(
            ["git", "diff", "--name-only", "origin/main...HEAD"],
            cwd=WORK_DIR,
        )
        files = [f for f in changed.stdout.strip().splitlines() if f.strip()]
        if not files:
            return False
        return all(f.startswith("data/code-review/") for f in files)
    except (subprocess.CalledProcessError, OSError):
        return False


def _find_open_pr(repo: str, branch: str) -> str:
    """Return the PR number open on `branch`, or "" if none. Fail-soft."""
    try:
        res = run_cmd(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "-R",
                repo,
                "--json",
                "number",
                "--jq",
                ".[0].number",
            ],
            env={**os.environ},
        )
        return res.stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def _ensure_pr_body_marker(repo: str, pr_number: str, branch: str) -> None:
    """Backfill the correlation marker onto an agent-authored PR body (issue #1721).

    When the agent opens its own PR via the SDK, the entrypoint's marker-prepend
    is skipped, so the PR body has no adp-* marker and the webhook's marker-gated
    reviewer trigger (#1696) blocks it. This reads the current PR body, prepends
    the marker if absent (prepend_correlation_marker is idempotent + no-ops when
    correlation env vars are missing), edits the PR, and writes the outbound
    correlation pointer so lineage round-trips. Fail-soft: never raises.
    """
    if not pr_number:
        return
    try:
        view = run_cmd(
            ["gh", "pr", "view", pr_number, "-R", repo, "--json", "body", "--jq", ".body"],
            env={**os.environ},
        )
        current_body = view.stdout.rstrip("\n")
    except subprocess.CalledProcessError as exc:
        logger.warning("Could not read PR #%s body for marker backfill: %s", pr_number, exc)
        return

    # Idempotent: prepend_correlation_marker is a no-op if the marker is already
    # present (first 500 bytes) or if correlation env vars are unset.
    new_body = prepend_correlation_marker(current_body)
    if new_body == current_body:
        logger.info("PR #%s body already marked (or no correlation context) — skip", pr_number)
        return

    try:
        run_cmd(
            ["gh", "pr", "edit", pr_number, "-R", repo, "--body", new_body],
            env={**os.environ},
        )
        logger.info("Backfilled correlation marker onto agent-authored PR #%s", pr_number)
        _write_outbound_correlation(repo, f"pr:{branch}", "pr_create")
    except subprocess.CalledProcessError as exc:
        logger.warning("Failed to backfill marker on PR #%s (non-fatal): %s", pr_number, exc)


def _handle_failure(
    repo: str,
    issue: int,
    persona: str,
    message_id: str,
    arrived_at: str,
    exit_code: int,
    check_run_url: str = "",
) -> int:
    """Step 12: Post failure comment, exit nonzero."""
    summary = f"Agent `{persona}` failed with exit code {exit_code}."
    _post_comment(repo, issue, message_id, "failed", summary, check_run_url)
    update_invocation_status(
        message_id,
        arrived_at,
        "failed",
        summary=summary,
    )
    return exit_code


def _write_outbound_correlation(repo: str, channel_suffix: str, action_kind: str) -> None:
    """Write DDB pointer + provenance after a successful outbound GitHub action.

    Fail-soft: logs warnings but never raises. Called only after the GitHub API
    call succeeded (Phase 2-d order of operations).

    Issue #1460: Records the producing run's message_id as triggering_invocation_id
    on the DDB pointer so the next inbound webhook can set parent_invocation_id.

    Issue #1661: Uses canonical channel_key() format matching the webhook-ingress
    Lambda so the pointer round-trips correctly.
    """
    corr = os.environ.get("ADP_CORRELATION_ID", "")
    # root / rooted are still needed for the provenance POST below (the gateway
    # attributes the record). Issue #4129: they are NO LONGER passed to
    # write_pointer — the webhook resolves chain provenance from its own
    # webhook-events rows, so the pod cannot name a root human on the pointer.
    root = os.environ.get("ADP_ROOT_HUMAN_ID", "")
    rooted = os.environ.get("ADP_IS_HUMAN_ROOTED", "false") == "true"
    own_message_id = os.environ.get("ADP_MESSAGE_ID", "")

    if not corr or not root:
        return  # No correlation context — skip silently

    # Build canonical channel key matching webhook-ingress format (#1661).
    # channel_suffix is "issue:{N}" or "pr:{branch}" — parse to extract kind/number.
    if channel_suffix.startswith("issue:"):
        issue_number = int(channel_suffix.split(":", 1)[1])
        key = channel_key("github", repo, "issue", issue_number)
    else:
        # PR path: keep legacy format for now (out of scope per #1661 approved design).
        key = f"github:{repo}:{channel_suffix}"

    # DDB pointer write (fail-soft) — chain id + parent edge only (#4129)
    try:
        write_pointer(
            channel_key=key,
            correlation_id=corr,
            triggering_invocation_id=own_message_id or None,
        )
    except Exception as exc:
        logger.warning("Outbound correlation pointer write failed (non-fatal): %s", exc)

    # Provenance POST (fail-soft)
    #
    # Issue #4029: this call 422'd on every invocation. source_event must be a dict
    # (the column is JSONB) and org_id must be a non-null tenant (the column is NOT
    # NULL) — the previous call passed a bare string and omitted org_id entirely.
    try:
        user_id = os.environ.get("ADP_USER_ID", "")
        # Tenant comes from the run's server-resolved envelope (exported as
        # ADP_TENANT_ID during bootstrap), never from anything the agent can influence.
        tenant = os.environ.get("ADP_TENANT_ID", "")
        if not tenant:
            # Better to skip than to post a null org_id the gateway must reject.
            logger.warning("No ADP_TENANT_ID in env — skipping provenance post")
        else:
            # Key vocabulary mirrors the webhook-ingress producer
            # (spawn_persona.py) so JSONB consumers need no per-producer branches.
            source_event = {
                "source": "worker:entrypoint",
                "event_type": action_kind,
                "repo": repo,
            }
            if channel_suffix.startswith("issue:"):
                source_event["issue"] = int(channel_suffix.split(":", 1)[1])
            elif channel_suffix.startswith("pr:"):
                source_event["branch"] = channel_suffix.split(":", 1)[1]

            post_provenance(
                actor_user_id=user_id,
                triggered_by=None,
                root_human_id=root,
                is_human_rooted=rooted,
                action_kind=action_kind,
                source_event=source_event,
                correlation_id=corr,
                org_id=tenant,
            )
    except Exception as exc:
        logger.warning("Outbound provenance post failed (non-fatal): %s", exc)


def _post_comment(
    repo: str, issue: int, message_id: str, status: str, body: str, check_run_url: str = ""
) -> None:
    """Post an idempotent comment (checks for existing marker).

    Order of operations (Phase 2-d):
      1. Prepend correlation marker (no I/O)
      2. Post to GitHub via gh CLI
      3. On success only: write DDB pointer + post provenance (fail-soft)
    """
    marker = f"<!-- adp-{status}:{message_id} -->"
    run_details = f"\n\n**Run details:** [View run ↗]({check_run_url})" if check_run_url else ""
    full_body = f"{marker}\n{body}{run_details}"
    # Step 1: Prepend correlation marker
    full_body = prepend_correlation_marker(full_body)
    try:
        existing = run_cmd(
            [
                "gh",
                "issue",
                "view",
                str(issue),
                "-R",
                repo,
                "--json",
                "comments",
                "--jq",
                f'.comments[].body | select(contains("{marker}"))',
            ],
            env={**os.environ},
        )
        if not existing.stdout.strip():
            # Step 2: GitHub API call
            run_cmd(
                ["gh", "issue", "comment", str(issue), "--body", full_body, "-R", repo],
                env={**os.environ},
            )
            # Step 3: On success — write pointer + provenance (fail-soft)
            _write_outbound_correlation(repo, f"issue:{issue}", "comment_post")
    except subprocess.CalledProcessError:
        logger.warning("Failed to post %s comment", status)


if __name__ == "__main__":
    sys.exit(main())
