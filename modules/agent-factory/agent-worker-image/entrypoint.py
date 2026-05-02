#!/usr/bin/env python3
"""Agent pod entrypoint: SQS envelope -> vault -> token mint -> clone -> agent exec -> PR.

Bridges the KEDA-injected SQS message to the existing agent-worker.js runtime.
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


def main() -> int:
    raw_message = os.environ.get("SQS_MESSAGE_BODY")
    if not raw_message:
        logger.error("SQS_MESSAGE_BODY not set")
        return 1

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
        "ANTHROPIC_MODEL": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
    }
    os.environ.update(env_vars)

    # Step 5: Clone customer repo
    clone_url = f"https://x-access-token:{token}@github.com/{repo}"
    WORK_DIR.parent.mkdir(parents=True, exist_ok=True)
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    run_cmd(["git", "clone", "--depth=20", clone_url, str(WORK_DIR)])
    logger.info("Cloned %s to %s", repo, WORK_DIR)

    # Step 6: Configure git identity
    bot_email = f"{app_id}+adp-agent[bot]@users.noreply.github.com"
    run_cmd(["git", "config", "user.email", bot_email], cwd=WORK_DIR)
    run_cmd(["git", "config", "user.name", "adp-agent[bot]"], cwd=WORK_DIR)

    # Step 7: If persona needs AWS, assume customer role
    if persona in PERSONAS_NEEDING_AWS:
        try:
            aws_creds = vault.get_secret(f"tenants/{tenant_id}/aws-access")
            creds = assume_customer_role(
                role_arn=aws_creds["role_arn"],
                external_id=aws_creds["external_id"],
                tenant_id=tenant_id,
                actor_login=actor.get("github_login", "unknown"),
                actor_id=str(actor.get("github_id", "0")),
                run_id=message_id,
                repo=repo,
                issue=issue,
                persona=persona,
                duration_seconds=aws_creds.get("session_duration_seconds", 3600),
            )
            os.environ.update(creds)
            logger.info("Assumed customer AWS role")
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
    started_body = (
        f"{started_marker}\n"
        f"🤖 **Agent `{persona}` started** working on this issue.\n\n"
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

    # Step 10: Exec the agent
    logger.info("Execing agent-worker.js with persona=%s", persona)
    branch_name = f"agent/issue-{issue}"
    result = subprocess.run(
        ["node", AGENT_BINARY],
        cwd=WORK_DIR,
        env={**os.environ},
    )

    # Step 11/12: Post-agent actions
    if result.returncode == 0:
        return _handle_success(repo, issue, branch_name, persona, message_id)
    else:
        return _handle_failure(repo, issue, persona, message_id, result.returncode)


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


def _handle_success(repo: str, issue: int, branch: str, persona: str, message_id: str) -> int:
    """Step 11: Commit, push branch, create PR, post completion comment."""
    try:
        # Check if there are changes to commit
        diff = run_cmd(["git", "diff", "--stat"], cwd=WORK_DIR)
        if not diff.stdout.strip():
            logger.info("No changes to commit")
            _post_comment(
                repo,
                issue,
                message_id,
                "completed",
                f"Agent `{persona}` finished — no changes needed.",
            )
            return 0

        run_cmd(["git", "checkout", "-b", branch], cwd=WORK_DIR)
        run_cmd(["git", "add", "-A"], cwd=WORK_DIR)
        run_cmd(
            ["git", "commit", "-m", f"feat: agent/{persona} work for #{issue}"],
            cwd=WORK_DIR,
        )
        run_cmd(["git", "push", "-u", "origin", branch], cwd=WORK_DIR)

        # Create PR
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
        )
    except subprocess.CalledProcessError as exc:
        logger.error("Post-agent git/PR step failed: %s", exc.stderr or exc)
        return 1
    return 0


def _handle_failure(repo: str, issue: int, persona: str, message_id: str, exit_code: int) -> int:
    """Step 12: Post failure comment, exit nonzero."""
    summary = f"Agent `{persona}` failed with exit code {exit_code}."
    _post_comment(repo, issue, message_id, "failed", summary)
    return exit_code


def _post_comment(repo: str, issue: int, message_id: str, status: str, body: str) -> None:
    """Post an idempotent comment (checks for existing marker)."""
    marker = f"<!-- adp-{status}:{message_id} -->"
    full_body = f"{marker}\n{body}"
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
