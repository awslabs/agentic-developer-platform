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
from pathlib import Path

import boto3

from lib.check_run import create_check_run, update_check_run
from lib.correlation_marker import prepend_correlation_marker
from lib.correlation_store import write_pointer
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
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)


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

    # Step 1: Parse envelope
    envelope = parse_envelope(raw_message)
    tenant_id = envelope["tenant_id"]
    persona = envelope["persona"]
    source = envelope["source_ref"]
    installation_id = source["installation_id"]
    repo = source["repo"]
    issue = source["issue"]
    message_id = envelope.get("message_id", "")
    actor = envelope.get("actor", {})

    # Read correlation context from SQS envelope (Phase 2-c adds these fields)
    correlation_id = envelope.get("correlation_id", "")
    root_human_id = envelope.get("root_human_id", "")
    is_human_rooted = envelope.get("is_human_rooted", False)

    # Expose correlation context as env vars for the Node agent runtime
    if correlation_id:
        os.environ["ADP_CORRELATION_ID"] = correlation_id
    if root_human_id:
        os.environ["ADP_ROOT_HUMAN_ID"] = root_human_id
    os.environ["ADP_IS_HUMAN_ROOTED"] = "true" if is_human_rooted else "false"

    repo_owner, repo_name = repo.split("/", 1)
    logger.info(
        "Processing: tenant=%s persona=%s repo=%s issue=#%s correlation=%s",
        tenant_id,
        persona,
        repo,
        issue,
        correlation_id or "(none)",
    )

    # Step 2: Fetch GitHub App credentials from vault
    vault = VaultClient(region=os.environ.get("AWS_REGION", "us-east-1"))
    app_creds = vault.get_secret(f"tenants/{tenant_id}/github-app")
    app_id = app_creds["app_id"]
    private_key = app_creds["private_key"]

    # Step 3: Mint installation token
    token = mint_installation_token(str(app_id), private_key, installation_id)

    # Step 4: Set environment variables
    env_vars = {
        "GITHUB_TOKEN": token,
        "GH_TOKEN": token,
        "AGENT_TYPE": persona,
        "ISSUE_NUMBER": str(issue),
        "REPO_OWNER": repo_owner,
        "REPO_NAME": repo_name,
        "TARGET_REPO": repo,
        "WORK_DIR": str(WORK_DIR),
        "TENANT_ID": tenant_id,
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "ANTHROPIC_MODEL": os.environ.get("ANTHROPIC_MODEL", "global.anthropic.claude-opus-4-6-v1"),
    }

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

    # Step 5: Clone customer repo
    clone_url = f"https://x-access-token:{token}@github.com/{repo}"
    WORK_DIR.parent.mkdir(parents=True, exist_ok=True)
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    run_cmd(["git", "clone", "--depth=20", clone_url, str(WORK_DIR)])
    logger.info("Cloned %s to %s", repo, WORK_DIR)

    # Step 6: Configure git identity (must come BEFORE WIP branch creation)
    bot_email = f"{app_id}+adp-agent[bot]@users.noreply.github.com"
    run_cmd(["git", "config", "user.email", bot_email], cwd=WORK_DIR)
    run_cmd(["git", "config", "user.name", "adp-agent[bot]"], cwd=WORK_DIR)

    # Step 6b: Create or reset the agent branch + WIP commit BEFORE exec so that:
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
        logger.info("WIP branch %s created; sha=%s", branch_name, wip_sha[:7])
    except Exception as exc:
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
    #   - "user": use customer's assumed credentials (operations persona only)
    #   - "platform": alias for "direct" (legacy compat)
    #
    # CRITICAL: We build a SEPARATE env dict for the child process. We do NOT
    # mutate os.environ — the entrypoint's post-agent SQS delete (line ~386)
    # needs os.environ to retain IRSA for platform-account access.
    agent_env = os.environ.copy()
    bedrock_via_raw = os.environ.get("ADP_BEDROCK_VIA")
    bedrock_via = (bedrock_via_raw or "gateway").strip().lower()

    # Start sigv4-proxy subprocess for gateway mode
    proxy_process: subprocess.Popen | None = None

    if bedrock_via == "gateway":
        proxy_process = _start_sigv4_proxy(agent_env, tenant_id)
        if proxy_process is None:
            # Proxy failed to start — fall back to direct Bedrock
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
        pass  # Already handled above
    else:
        logger.info(
            "ADP_BEDROCK_VIA=%r (normalized: %s) — agent env retains pod IRSA",
            bedrock_via_raw,
            bedrock_via,
        )

    logger.info("Execing agent-worker.js with persona=%s branch=%s", persona, branch_name)
    result = subprocess.run(
        ["node", AGENT_BINARY],
        cwd=WORK_DIR,
        env=agent_env,
    )

    # Terminate sigv4-proxy if it was started
    if proxy_process is not None:
        _stop_sigv4_proxy(proxy_process)

    # Step 11/12: Post-agent actions
    if result.returncode == 0:
        exit_code = _handle_success(repo, issue, branch_name, persona, message_id, check_run_url)
    else:
        exit_code = _handle_failure(
            repo, issue, persona, message_id, result.returncode, check_run_url
        )

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

            # Read the final rendered Markdown written by CheckRunStreamer (if any).
            # This preserves the full per-turn transcript across the process boundary.
            final_text: str = ""
            cr_final_path = "/tmp/adp-check-run-final.md"
            try:
                if os.path.exists(cr_final_path):
                    with open(cr_final_path, "r", encoding="utf-8") as fh:
                        final_text = fh.read()
            except Exception:
                pass

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
        time.sleep(0.3)

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
            "Set VAULT_GATEWAY_URL and VAULT_INTERNAL_API_KEY."
        )

    return gw_client.assume_role(
        user_id=user_id,
        agent_id=agent_id,
        task_id=task_id,
        service="aws",
        label="default",
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


def _handle_success(
    repo: str, issue: int, branch: str, persona: str, message_id: str, check_run_url: str = ""
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
            # Only the empty WIP commit is on the branch; agent made no real changes.
            logger.info("No agent changes beyond WIP commit")
            _post_comment(
                repo,
                issue,
                message_id,
                "completed",
                f"Agent `{persona}` finished — no changes needed.",
                check_run_url,
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
            pr_already_exists = bool(existing_pr.stdout.strip())
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
        _post_comment(
            repo,
            issue,
            message_id,
            "completed",
            f"Agent `{persona}` completed. PR opened on branch `{branch}`.",
            check_run_url,
        )
    except subprocess.CalledProcessError as exc:
        logger.error("Post-agent git/PR step failed: %s", exc.stderr or exc)
        return 1
    return 0


def _handle_failure(
    repo: str, issue: int, persona: str, message_id: str, exit_code: int, check_run_url: str = ""
) -> int:
    """Step 12: Post failure comment, exit nonzero."""
    summary = f"Agent `{persona}` failed with exit code {exit_code}."
    _post_comment(repo, issue, message_id, "failed", summary, check_run_url)
    return exit_code


def _write_outbound_correlation(repo: str, channel_suffix: str, action_kind: str) -> None:
    """Write DDB pointer + provenance after a successful outbound GitHub action.

    Fail-soft: logs warnings but never raises. Called only after the GitHub API
    call succeeded (Phase 2-d order of operations).
    """
    corr = os.environ.get("ADP_CORRELATION_ID", "")
    root = os.environ.get("ADP_ROOT_HUMAN_ID", "")
    rooted = os.environ.get("ADP_IS_HUMAN_ROOTED", "false") == "true"

    if not corr or not root:
        return  # No correlation context — skip silently

    channel_key = f"github:{repo}:{channel_suffix}"

    # DDB pointer write (fail-soft)
    try:
        write_pointer(
            channel_key=channel_key,
            correlation_id=corr,
            root_human_id=root,
            is_human_rooted=rooted,
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
