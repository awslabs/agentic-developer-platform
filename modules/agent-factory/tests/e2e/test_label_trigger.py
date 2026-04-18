"""
GitHub Actions ingest path test (live-only, workflow marker).

Test 25 from the issue:
  Create a throwaway issue, apply `agent-reviewer` label, confirm ARC runner
  picks it up and the workflow completes. Clean up afterward.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest


pytestmark = [pytest.mark.workflow, pytest.mark.live_only]


class TestLabelTrigger:
    """Test 25: Label trigger dispatches a GitHub Actions workflow."""

    def test_agent_label_triggers_workflow(self, test_env):
        org = test_env.live.github_org
        repo = test_env.live.github_repo

        if not org or not repo:
            pytest.skip("GITHUB_ORG / GITHUB_REPO not set")

        full_repo = f"{org}/{repo}"
        issue_title = f"[E2E Test] Label trigger test {int(time.time())}"
        issue_body = (
            "Automated E2E test for agent-gateway label trigger path.\n"
            "This issue will be closed automatically after the test.\n"
            "DO NOT assign or work on this issue."
        )

        issue_number = None
        try:
            # Create issue
            result = subprocess.run(
                ["gh", "issue", "create", "--repo", full_repo,
                 "--title", issue_title, "--body", issue_body],
                capture_output=True, text=True, timeout=30,
            )
            assert result.returncode == 0, f"Failed to create issue: {result.stderr}"
            issue_url = result.stdout.strip()
            issue_number = int(issue_url.rstrip("/").split("/")[-1])

            # Apply label
            subprocess.run(
                ["gh", "issue", "edit", str(issue_number), "--repo", full_repo,
                 "--add-label", "agent-reviewer"],
                capture_output=True, text=True, timeout=15,
                check=True,
            )

            # Wait for workflow to be triggered (up to 60s)
            workflow_found = False
            for _ in range(12):
                time.sleep(5)
                runs = subprocess.run(
                    ["gh", "run", "list", "--repo", full_repo, "--limit", "5", "--json", "name,status,headBranch,createdAt"],
                    capture_output=True, text=True, timeout=15,
                )
                if runs.returncode == 0:
                    run_list = json.loads(runs.stdout)
                    for run in run_list:
                        if "agent" in run.get("name", "").lower():
                            workflow_found = True
                            break
                if workflow_found:
                    break

            assert workflow_found, (
                "No agent workflow triggered within 60 seconds after labeling. "
                f"Check: gh run list --repo {full_repo}"
            )

        finally:
            # Clean up: close and delete the test issue
            if issue_number:
                subprocess.run(
                    ["gh", "issue", "close", str(issue_number), "--repo", full_repo,
                     "--comment", "E2E test complete — closing."],
                    capture_output=True, text=True, timeout=15,
                )
