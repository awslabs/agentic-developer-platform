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

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import boto3

from lib.bootstrap_logger import BootstrapLogger
from lib.check_run import create_check_run, update_check_run
from lib.correlation_marker import prepend_correlation_marker
from lib.correlation_store import channel_key, write_pointer
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

# STS session tag values must match [\p{L}\p{Z}\p{N}_.:/=+\-@]*. The natural
# task ID shape `<owner>/<repo>#<issue>` contains '#' which fails validation.
# Replace any character outside the allowed set with '_'.
_STS_TAG_FORBIDDEN = re.compile(r"[^A-Za-z0-9_.:/=+\-@]")


def _sanitize_for_sts_tag(value: str) -> str:
    return _STS_TAG_FORBIDDEN.sub("_", value)


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


def _upload_transcript_to_s3(
    final_text: str, repo: str, issue: int, message_id: str, arrived_at: str, persona: str
) -> str | None:
    """Upload the full untruncated transcript to S3 (best-effort).

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
        logger.info("Transcript uploaded to s3://%s/%s (%d bytes)", bucket, key, len(final_text))
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

    # 2. Create branch agent/issue-<iid> from default branch (idempotent)
    branch_name = f"agent/issue-{issue_iid}"
    branches_url = f"{base_url}/api/v4/projects/{project_id}/repository/branches"
    branch_payload = json.dumps({"branch": branch_name, "ref": "main"}).encode("utf-8")

    try:
        req = urllib.request.Request(
            branches_url, data=branch_payload, headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            logger.info("GitLab branch created: %s (status=%s)", branch_name, resp.status)
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            # Branch already exists — expected idempotent case
            logger.info("GitLab branch already exists: %s (400 tolerated)", branch_name)
        else:
            logger.error("GitLab path: failed to create branch: %s (status=%s)", exc, exc.code)
    except Exception as exc:
        logger.error("GitLab path: failed to create branch: %s", exc)

    # 3. Delete the SQS message — always, to prevent FIFO jam
    try:
        _delete_message(queue_url, region, receipt_handle)
        logger.info("GitLab message deleted successfully")
    except Exception as exc:
        logger.error("GitLab path: failed to delete SQS message: %s", exc)
        return 1

    # Return 1 if the ack comment failed (the primary deliverable of Tier A);
    # the message is still deleted above to prevent FIFO jam.
    return 1 if ack_failed else 0


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

    # Step 2: Fetch GitHub App credentials from vault
    bootstrap_log.step_start(2, "vault_fetch", secret=f"tenants/{tenant_id}/github-app")
    try:
        vault = VaultClient(region=os.environ.get("AWS_REGION", "us-east-1"))
        app_creds = vault.get_secret(f"tenants/{tenant_id}/github-app")
        app_id = app_creds["app_id"]
        private_key = app_creds["private_key"]
    except Exception as exc:
        bootstrap_log.step_error(2, "vault_fetch", exc)
        bootstrap_log.close()
        raise
    bootstrap_log.step_success(2, "vault_fetch", app_id=app_id)

    # Step 3: Mint installation token
    bootstrap_log.step_start(3, "mint_token", app_id=app_id, installation_id=installation_id)
    try:
        token = mint_installation_token(str(app_id), private_key, installation_id)
    except Exception as exc:
        bootstrap_log.step_error(3, "mint_token", exc)
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
        "ANTHROPIC_MODEL", "us.anthropic.claude-opus-4-6-v1"
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
        # GitHub App credentials for token refresh (#1502). The agent-worker.ts
        # TokenManager requires these to re-mint installation tokens before the
        # 1-hour expiry. Without them, long-running agents die with 401.
        "GH_APP_ID": str(app_id),
        "GH_APP_PRIVATE_KEY": private_key,
        # Authoritative installation id for THIS run's target org. The JS worker
        # must re-mint against this installation — NOT installations[0], which is
        # an arbitrary (newest-first) install and resolves to the wrong org once
        # more than one tenant is onboarded, causing 404s on comment/check-run
        # PATCH calls (cross-installation resource access).
        "GH_APP_INSTALLATION_ID": str(installation_id),
    }

    # Issue #2279: Expose model_requested so the worker can post a warning
    # if the requested model was rejected (lenient path).
    model_requested = envelope.get("model_requested")
    if model_requested:
        env_vars["ADP_MODEL_REQUESTED"] = model_requested
    if model_resolved:
        env_vars["ADP_MODEL_RESOLVED"] = model_resolved

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
        bootstrap_log.close()
        raise
    bootstrap_log.step_success(5, "clone", target=str(WORK_DIR))
    logger.info("Cloned %s to %s", repo, WORK_DIR)

    # Step 6: Configure git identity (must come BEFORE WIP branch creation)
    bootstrap_log.step_start(6, "git_config")
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
                run_cmd(["git", "fetch", "origin", branch_name], cwd=WORK_DIR)
                run_cmd(["git", "checkout", branch_name], cwd=WORK_DIR)
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
        logger.warning("WIP branch creation failed (non-fatal): %s", exc)
        # Fall back to default-branch HEAD sha for the Check Run
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
    if persona in PERSONAS_NEEDING_AWS:
        try:
            sts_creds = _fetch_assumed_aws_credentials(
                user_id=user_id,
                agent_id=persona,
                task_id=task_id,
            )
            os.environ.update(
                {
                    "AWS_ACCESS_KEY_ID": sts_creds["access_key_id"],
                    "AWS_SECRET_ACCESS_KEY": sts_creds["secret_access_key"],
                    "AWS_SESSION_TOKEN": sts_creds["session_token"],
                }
            )
            logger.info(
                "Assumed customer AWS role (user-scoped) via gateway provenance_id=%s expires=%s",
                sts_creds.get("provenance_id"),
                sts_creds.get("expiration"),
            )
        except Exception as exc:
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
    #   - "user": use customer's assumed credentials for both Bedrock + AWS calls
    #     (legacy: operations persona on customer-billed Bedrock)
    #   - "platform": alias for "direct" (legacy compat)
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
    elif bedrock_via == "user" and "AWS_ACCESS_KEY_ID" in agent_env:
        for var in ("AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_PROFILE"):
            agent_env.pop(var, None)
        logger.info(
            "ADP_BEDROCK_VIA=%r (normalized: user) — agent env stripped of IRSA; "
            "user account credentials will be used for all agent AWS calls",
            bedrock_via_raw,
        )
    elif bedrock_via == "user" and persona not in PERSONAS_NEEDING_AWS:
        logger.warning(
            "ADP_BEDROCK_VIA=user set but persona=%r does not assume customer role "
            "(not in PERSONAS_NEEDING_AWS=%s). Agent will use pod IRSA for all AWS "
            "calls including Bedrock. Either add this persona to PERSONAS_NEEDING_AWS "
            "or unset ADP_BEDROCK_VIA on the ScaledJob.",
            persona,
            sorted(PERSONAS_NEEDING_AWS),
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
    _keda_job_name = os.environ.get("JOB_NAME", os.environ.get("HOSTNAME", ""))
    update_invocation_status(message_id, arrived_at, "in_progress", run_id=_keda_job_name)

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

    # Step 11/12: Post-agent actions
    if result.returncode == 0:
        exit_code = _handle_success(
            repo, issue, branch_name, persona, message_id, arrived_at, check_run_url
        )
    else:
        exit_code = _handle_failure(
            repo, issue, persona, message_id, arrived_at, result.returncode, check_run_url
        )

    # Read the final rendered Markdown written by CheckRunStreamer (if any).
    # This preserves the full per-turn transcript across the process boundary.
    # Read outside the check-run block so S3 upload can use it independently.
    final_text: str = ""
    cr_final_path = "/tmp/adp-check-run-final.md"
    try:
        if os.path.exists(cr_final_path):
            with open(cr_final_path, "r", encoding="utf-8") as fh:
                final_text = fh.read()
    except Exception:
        pass

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

    # Persist full untruncated transcript to S3 (best-effort, non-fatal).
    # Issue #3057: transcripts exceed the GitHub Check Run 65,535-char limit;
    # S3 gives us a durable, auditable archive.
    transcript_key = _upload_transcript_to_s3(
        final_text, repo, issue, message_id, arrived_at, persona
    )

    # Issue #3069: Write-back the S3 key to the DDB invocation row so the
    # gateway can serve the transcript from the Agent Activity UI.
    # Fail-soft: reuses the same update_invocation_status contract (logs, never raises).
    if transcript_key:
        update_invocation_status(
            message_id,
            arrived_at,
            "complete" if exit_code == 0 else "failed",
            transcript_key=transcript_key,
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


def _fetch_assumed_aws_credentials(*, user_id: str, agent_id: str, task_id: str) -> dict:
    """Get short-lived AWS credentials via the gateway's assume-role endpoint.

    Calls POST /internal/v1/credential-assume-role with service="aws". The
    gateway resolves the user's aws_role credential, performs STS AssumeRole
    server-side with session tagging, and returns ready-to-use temp creds.

    Preferred over the raw-read path because credential-assume-role isn't
    gated by the `vault_raw_read_enabled` feature flag (default off).

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
        label=None,
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
            logger.info("No agent changes beyond WIP commit")

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
            _post_comment(
                repo,
                issue,
                message_id,
                "completed",
                f"Agent `{persona}` finished — no changes needed.",
                check_run_url,
            )
            update_invocation_status(
                message_id,
                arrived_at,
                "complete",
                summary=f"{persona} — no changes needed",
            )
            return 0

        if has_unpushed or has_uncommitted:
            run_cmd(["git", "push", "origin", branch], cwd=WORK_DIR)

        # Create PR if one doesn't already exist on this branch
        pr_already_exists = False
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
        else:
            # The agent opened its OWN PR (via the SDK's `gh pr create`), so the
            # entrypoint's marker-prepend above was skipped. Agent-authored PR
            # bodies therefore carry NO adp-* correlation marker — which means
            # the webhook's marker-gated reviewer trigger (issue #1696) blocks
            # the PR and cross-agent lineage is lost (issue #1721). Backfill it:
            # edit the PR body to prepend the marker if it isn't already there.
            _ensure_pr_body_marker(repo, existing_pr_number, branch)
        _post_comment(
            repo,
            issue,
            message_id,
            "completed",
            f"Agent `{persona}` completed. PR opened on branch `{branch}`.",
            check_run_url,
        )
        update_invocation_status(
            message_id,
            arrived_at,
            "complete",
            summary=f"{persona} — completed, PR on {branch}",
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
    root = os.environ.get("ADP_ROOT_HUMAN_ID", "")
    rooted = os.environ.get("ADP_IS_HUMAN_ROOTED", "false") == "true"
    own_message_id = os.environ.get("ADP_MESSAGE_ID", "")
    depth_str = os.environ.get("ADP_CHAIN_DEPTH", "0")

    if not corr or not root:
        return  # No correlation context — skip silently

    # Parse chain depth (issue #1696) — defaults to 0 if absent/invalid
    try:
        current_depth = int(depth_str)
    except (ValueError, TypeError):
        current_depth = 0

    # Build canonical channel key matching webhook-ingress format (#1661).
    # channel_suffix is "issue:{N}" or "pr:{branch}" — parse to extract kind/number.
    if channel_suffix.startswith("issue:"):
        issue_number = int(channel_suffix.split(":", 1)[1])
        key = channel_key("github", repo, "issue", issue_number)
    else:
        # PR path: keep legacy format for now (out of scope per #1661 approved design).
        key = f"github:{repo}:{channel_suffix}"

    # DDB pointer write (fail-soft) — includes triggering_invocation_id + chain_depth
    try:
        write_pointer(
            channel_key=key,
            correlation_id=corr,
            root_human_id=root,
            is_human_rooted=rooted,
            triggering_invocation_id=own_message_id or None,
            chain_depth=current_depth,
        )
    except Exception as exc:
        logger.warning("Outbound correlation pointer write failed (non-fatal): %s", exc)

    # Provenance POST (fail-soft)
    try:
        user_id = os.environ.get("ADP_USER_ID", "")
        post_provenance(
            actor_user_id=user_id,
            triggered_by=None,
            root_human_id=root,
            is_human_rooted=rooted,
            action_kind=action_kind,
            source_event="worker:entrypoint",
            correlation_id=corr,
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
