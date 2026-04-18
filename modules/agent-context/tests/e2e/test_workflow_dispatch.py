"""
E2E test for GitHub Actions ingestion trigger.

Test 17 from issue #21:
17. agent-context-ingest.yml fires on workflow_dispatch
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest


@pytest.mark.workflow
@pytest.mark.live_only
class TestWorkflowDispatch:
    """Trigger the agent-context-ingest workflow and verify it completes."""

    def test_workflow_dispatch_succeeds(self, test_env):
        """Fire workflow_dispatch and poll for completion."""
        repo = f"{test_env.live.github_org}/{test_env.live.github_repo}"

        # Trigger the workflow
        result = subprocess.run(
            [
                "gh", "workflow", "run",
                "agent-context-ingest.yml",
                "--repo", repo,
                "--ref", "main",
                "-f", "full_reindex=false",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Failed to trigger workflow: {result.stderr}"
        )

        # Wait a moment for the run to register
        time.sleep(5)

        # Poll for completion (max 10 minutes)
        deadline = time.time() + 600
        run_id = None

        while time.time() < deadline:
            list_result = subprocess.run(
                [
                    "gh", "run", "list",
                    "--repo", repo,
                    "--workflow", "agent-context-ingest.yml",
                    "--limit", "1",
                    "--json", "databaseId,status,conclusion",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if list_result.returncode != 0:
                time.sleep(15)
                continue

            runs = json.loads(list_result.stdout)
            if not runs:
                time.sleep(15)
                continue

            run = runs[0]
            run_id = run.get("databaseId")
            status = run.get("status", "")
            conclusion = run.get("conclusion", "")

            if status == "completed":
                assert conclusion == "success", (
                    f"Workflow run {run_id} completed with conclusion: {conclusion}"
                )
                return

            time.sleep(15)

        pytest.fail(
            f"Workflow run {run_id} did not complete within 10 minutes"
        )
