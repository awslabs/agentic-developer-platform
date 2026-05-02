#!/usr/bin/env python3
"""
adp-agent pod entrypoint.

Stages personas and skills from the image into the customer's cloned repo,
configures git credentials, then invokes the agent-worker.js runtime.

Environment variables expected:
  GITHUB_TOKEN      — installation token for repo access
  REPO_FULL_NAME    — org/repo to clone
  AGENT_TYPE        — persona name (developer, pm, operations, reviewer, ...)
  TASK_PAYLOAD      — JSON task payload from SQS
  WORK_DIR          — (optional) override workspace path, default /work/repo
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

WORK_DIR = Path(os.environ.get("WORK_DIR", "/work/repo"))
STAGED_PERSONAS = Path("/app/personas")
STAGED_SKILLS = Path("/app/skills")


def setup_git_credentials() -> None:
    """Configure git to use the GitHub token for HTTPS auth."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("[entrypoint] WARNING: GITHUB_TOKEN not set", file=sys.stderr)
        return

    cred_path = Path.home() / ".git-credentials"
    cred_path.write_text(f"https://x-access-token:{token}@github.com\n")
    cred_path.chmod(0o600)


def clone_repo() -> None:
    """Clone the target repository into WORK_DIR."""
    repo = os.environ.get("REPO_FULL_NAME", "")
    if not repo:
        print("[entrypoint] REPO_FULL_NAME not set, skipping clone", file=sys.stderr)
        return

    if WORK_DIR.exists() and any(WORK_DIR.iterdir()):
        print(f"[entrypoint] {WORK_DIR} already populated, skipping clone")
        return

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("GITHUB_TOKEN", "")
    clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"

    subprocess.run(
        ["git", "clone", "--depth=1", clone_url, str(WORK_DIR)],
        check=True,
    )
    print(f"[entrypoint] Cloned {repo} into {WORK_DIR}")


def stage_personas_and_skills() -> None:
    """Copy personas and skills from image paths into the work directory."""
    personas_dest = WORK_DIR / ".adp-rules" / "personas"
    skills_dest = WORK_DIR / ".claude" / "skills"

    personas_dest.mkdir(parents=True, exist_ok=True)
    skills_dest.mkdir(parents=True, exist_ok=True)

    if STAGED_PERSONAS.exists():
        shutil.copytree(STAGED_PERSONAS, personas_dest, dirs_exist_ok=True)
        count = len(list(personas_dest.glob("*.md")))
        print(f"[entrypoint] Staged {count} personas")

    if STAGED_SKILLS.exists():
        shutil.copytree(STAGED_SKILLS, skills_dest, dirs_exist_ok=True)
        count = len(list(skills_dest.iterdir()))
        print(f"[entrypoint] Staged {count} skills")


def run_agent() -> int:
    """Invoke the agent-worker.js runtime."""
    agent_worker = Path("/app/dist/agent-worker.js")
    if not agent_worker.exists():
        print(f"[entrypoint] ERROR: {agent_worker} not found", file=sys.stderr)
        return 1

    result = subprocess.run(
        ["node", str(agent_worker)],
        cwd=str(WORK_DIR),
        env={**os.environ, "WORK_DIR": str(WORK_DIR)},
    )
    return result.returncode


def main() -> None:
    print("[entrypoint] Starting adp-agent worker")

    setup_git_credentials()
    clone_repo()
    stage_personas_and_skills()

    rc = run_agent()
    sys.exit(rc)


if __name__ == "__main__":
    main()
