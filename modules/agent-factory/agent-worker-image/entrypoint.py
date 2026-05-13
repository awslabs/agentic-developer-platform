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
import shutil
import subprocess
import sys
from pathlib import Path

import boto3

from lib.check_run import create_check_run, update_check_run
from lib.gateway_credential_client import GatewayCredentialClient, GatewayCredentialError
from lib.github_token import mint_installation_token
from lib.sts_assume import assume_customer_role
from lib.vault_client import VaultClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

WORK_DIR = Path("/work/repo")
PERSONAS_DIR = Path("/app/personas")
SKILLS_DIR = Path("/app/skills")
AGENT_BINARY = "/app/dist/agent-worker.js"
PERSONAS_NEEDING_AWS = frozenset({"operations", "agent-operations"})


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

    repo_owner, repo_name = repo.split("/", 1)
    logger.info(
        "Processing: tenant=%s persona=%s repo=%s issue=#%s",
        tenant_id,
        persona,
        repo,
        issue,
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

    # Vault credential context for adp-cred CLI (#137)
    user_id = envelope.get("user_id") or actor.get("user_id", "")
    if user_id:
        env_vars["ADP_USER_ID"] = user_id
        env_vars["ADP_AGENT_ID"] = persona
        env_vars["ADP_TASK_ID"] = message_id or f"{repo}#{issue}"

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

    # Step 6b: Create the agent branch + WIP commit BEFORE exec so that:
    #   1. The Check Run attaches to the branch SHA (not default-branch HEAD).
    #   2. Users see a "WIP" commit immediately on the branch.
    #   3. Real agent commits stack cleanly on top.
    branch_name = f"agent/issue-{issue}"
    wip_sha: str = ""
    try:
        run_cmd(["git", "checkout", "-b", branch_name], cwd=WORK_DIR)
        run_cmd(
            ["git", "commit", "--allow-empty",
             "-m", f"WIP: agent/{persona} starting #{issue}"],
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

    # Step 7: If persona needs AWS, assume customer role via user-scoped creds
    if persona in PERSONAS_NEEDING_AWS:
        try:
            aws_creds = _fetch_aws_credentials(
                user_id=user_id,
                agent_id=persona,
                task_id=message_id or f"{repo}#{issue}",
            )
            creds = assume_customer_role(
                role_arn=aws_creds["role_arn"],
                external_id=aws_creds["external_id"],
                tenant_id=tenant_id,
                actor_login=actor.get("github_login", "unknown"),
                actor_id=str(actor.get("github_id", "0")),
                user_id=user_id,
                run_id=message_id,
                repo=repo,
                issue=issue,
                persona=persona,
                duration_seconds=aws_creds.get("session_duration_seconds", 3600),
            )
            os.environ.update(creds)
            logger.info("Assumed customer AWS role (user-scoped)")
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
    _live_link = (
        f"\n\n**Live progress:** [View run ↗]({check_run_url})"
        if check_run_url
        else ""
    )
    started_body = (
        f"{started_marker}\n"
        f"🤖 **Agent `{persona}` started** working on this issue."
        f"{_live_link}\n\n"
        f"_Run ID: `{message_id}`_"
    )
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
    except subprocess.CalledProcessError:
        logger.warning("Failed to post started comment (non-fatal)")

    # Stage personas and skills into workspace
    _stage_personas_and_skills()

    # Step 10: Build scoped agent env and exec the agent.
    # ADP_BEDROCK_VIA controls whether the agent's AWS calls route through
    # the platform account (pod IRSA) or the user's connected account.
    # CRITICAL: We build a SEPARATE env dict for the child process. We do NOT
    # mutate os.environ — the entrypoint's post-agent SQS delete (line ~386)
    # needs os.environ to retain IRSA for platform-account access.
    agent_env = os.environ.copy()
    bedrock_via_raw = os.environ.get("ADP_BEDROCK_VIA")
    bedrock_via = (bedrock_via_raw or "platform").strip().lower()

    if bedrock_via == "user" and "AWS_ACCESS_KEY_ID" in agent_env:
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
            persona, sorted(PERSONAS_NEEDING_AWS),
        )
    else:
        logger.info(
            "ADP_BEDROCK_VIA=%r (normalized: %s) — agent env retains pod IRSA",
            bedrock_via_raw, bedrock_via,
        )

    logger.info("Execing agent-worker.js with persona=%s branch=%s", persona, branch_name)
    result = subprocess.run(
        ["node", AGENT_BINARY],
        cwd=WORK_DIR,
        env=agent_env,
    )

    # Step 11/12: Post-agent actions
    if result.returncode == 0:
        exit_code = _handle_success(repo, issue, branch_name, persona, message_id, check_run_url)
    else:
        exit_code = _handle_failure(repo, issue, persona, message_id, result.returncode, check_run_url)

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
                    ["gh", "pr", "view", branch_name, "-R", repo,
                     "--json", "url", "--jq", ".url"],
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
        logger.info(
            "SQS message acked and deleted (exit_code=%d)", exit_code
        )
    except Exception as exc:
        logger.error("Failed to delete SQS message: %s", exc)
        # Don't fail the pod — agent work already committed to GitHub

    return exit_code


def _fetch_aws_credentials(*, user_id: str, agent_id: str, task_id: str) -> dict:
    """Fetch AWS credentials via the gateway's user-scoped credential endpoint.

    Calls /internal/v1/credential-raw-read with service="aws_role_assume".
    The gateway resolves the credential through the user -> team -> org scope chain.

    Returns:
        Dict with at least {role_arn, external_id} and optionally
        {session_duration_seconds, default_region}.

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

    resp = gw_client.raw_read(
        user_id=user_id,
        agent_id=agent_id,
        task_id=task_id,
        service="aws_role_assume",
        label="default",
        purpose="entrypoint: assume customer AWS role for operations persona",
    )

    # The raw value is a JSON string; parse it to get the credential fields.
    value = resp.get("value", "")
    try:
        cred_data = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise GatewayCredentialError(
            f"Failed to parse credential value as JSON: {exc}"
        ) from exc

    if "role_arn" not in cred_data:
        raise GatewayCredentialError(
            f"Credential value missing required field 'role_arn'. Got keys: {list(cred_data.keys())}"
        )

    return cred_data


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
                ["gh", "pr", "list", "--head", branch, "-R", repo, "--json", "number", "--jq", ".[0].number"],
                env={**os.environ},
            )
            pr_already_exists = bool(existing_pr.stdout.strip())
        except subprocess.CalledProcessError:
            pass

        if not pr_already_exists:
            run_cmd(
                [
                    "gh",
                    "pr",
                    "create",
                    "--title",
                    f"[{persona}] Agent work for #{issue}",
                    "--body",
                    f"Automated work by agent `{persona}` for #{issue}.\n\nRun ID: `{message_id}`",
                    "--head",
                    branch,
                    "-R",
                    repo,
                ],
                env={**os.environ},
            )
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


def _post_comment(
    repo: str, issue: int, message_id: str, status: str, body: str, check_run_url: str = ""
) -> None:
    """Post an idempotent comment (checks for existing marker)."""
    marker = f"<!-- adp-{status}:{message_id} -->"
    run_details = (
        f"\n\n**Run details:** [View run ↗]({check_run_url})"
        if check_run_url
        else ""
    )
    full_body = f"{marker}\n{body}{run_details}"
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
            run_cmd(
                ["gh", "issue", "comment", str(issue), "--body", full_body, "-R", repo],
                env={**os.environ},
            )
    except subprocess.CalledProcessError:
        logger.warning("Failed to post %s comment", status)


if __name__ == "__main__":
    sys.exit(main())
