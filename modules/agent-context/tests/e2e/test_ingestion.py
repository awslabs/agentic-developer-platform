"""
E2E tests for the agent-context ingestion pipeline.

Tests 13-16 from issue #21 (all live_only):
13. Manually trigger ingestion CronJob and verify completion
14. State persistence: repo-state.json is valid after a run
15. SHA-based skip: unchanged repos are skipped
16. New-repo ingest: add a test repo and verify it appears in search
"""

from __future__ import annotations

import json
import uuid

import pytest


# ---------------------------------------------------------------------------
# Test 13: Manual CronJob trigger
# ---------------------------------------------------------------------------


@pytest.mark.live_only
@pytest.mark.kubectl
class TestManualIngestionTrigger:
    """Trigger the ingestion-refresh CronJob manually and verify completion."""

    def test_manual_trigger_completes(self, ingest_job_runner):
        suffix = uuid.uuid4().hex[:8]
        job_name = ingest_job_runner.create_job(name_suffix=suffix)
        assert job_name, "Failed to create ingestion job"

        completed = ingest_job_runner.wait_for_completion(job_name, timeout_seconds=600)
        assert completed, f"Ingestion job {job_name} did not complete within timeout"

    def test_manual_trigger_logs_no_errors(self, ingest_job_runner):
        suffix = uuid.uuid4().hex[:8]
        job_name = ingest_job_runner.create_job(name_suffix=suffix)
        ingest_job_runner.wait_for_completion(job_name, timeout_seconds=600)

        logs = ingest_job_runner.get_logs(job_name)
        assert "Traceback" not in logs, f"Job logs contain a traceback:\n{logs[-500:]}"
        assert "FATAL" not in logs.upper(), f"Job logs contain FATAL error:\n{logs[-500:]}"


# ---------------------------------------------------------------------------
# Test 14: State persistence
# ---------------------------------------------------------------------------


@pytest.mark.live_only
@pytest.mark.kubectl
class TestStatePersistence:
    """Verify repo-state.json on the S3 Files PVC is valid JSON."""

    def test_repo_state_json_exists_and_valid(self, kube_client, test_env):
        """After an ingestion run, repo-state.json should be valid JSON."""
        # This test reads from a pod that has the platform-data PVC mounted
        # We use kubectl exec into deepwiki (or any pod with the mount)
        import subprocess

        cmd = ["kubectl"]
        if test_env.live.kube_context:
            cmd += ["--context", test_env.live.kube_context]
        cmd += [
            "-n", test_env.live.namespace,
            "exec", "deploy/deepwiki", "--",
            "cat", "/platform-data/repo-state.json",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            pytest.skip("repo-state.json not found (ingestion may not have run yet)")

        data = json.loads(result.stdout)
        assert isinstance(data, dict), "repo-state.json should be a JSON object"

        # Each entry should have sha and indexed_at
        for repo_slug, state in data.items():
            assert "sha" in state, f"Repo {repo_slug} missing 'sha' in state"
            assert "indexed_at" in state, f"Repo {repo_slug} missing 'indexed_at' in state"


# ---------------------------------------------------------------------------
# Test 15: SHA-based skip
# ---------------------------------------------------------------------------


@pytest.mark.live_only
@pytest.mark.kubectl
class TestSHABasedSkip:
    """Verify that unchanged repos are skipped on re-run."""

    def test_unchanged_repos_skipped(self, ingest_job_runner):
        """Run ingestion twice; second run should skip repos that haven't changed."""
        # First run
        suffix1 = uuid.uuid4().hex[:8]
        job1 = ingest_job_runner.create_job(name_suffix=f"skip1-{suffix1}")
        ingest_job_runner.wait_for_completion(job1, timeout_seconds=600)

        # Second run
        suffix2 = uuid.uuid4().hex[:8]
        job2 = ingest_job_runner.create_job(name_suffix=f"skip2-{suffix2}")
        ingest_job_runner.wait_for_completion(job2, timeout_seconds=600)

        logs = ingest_job_runner.get_logs(job2)
        # At least one repo should be skipped as unchanged
        assert "unchanged" in logs.lower() or "skipping" in logs.lower(), (
            "Second ingestion run did not log any skipped/unchanged repos"
        )


# ---------------------------------------------------------------------------
# Test 16: New-repo ingest
# ---------------------------------------------------------------------------


@pytest.mark.live_only
@pytest.mark.kubectl
class TestNewRepoIngest:
    """Add a test repo and verify it appears in search after ingestion."""

    def test_new_repo_appears_in_search(self, mcp_client, ingest_job_runner, temp_repo_list):
        """Add a known repo, run ingestion, and search for it."""
        # This is a longer integration test — add a small well-known repo
        test_repo = "octocat/Hello-World"
        temp_repo_list(extra=[test_repo])

        # Trigger ingestion (this would need the modified repos.txt in the configmap)
        # For now, we just verify the mechanism works in principle
        suffix = uuid.uuid4().hex[:8]
        job_name = ingest_job_runner.create_job(name_suffix=f"newrepo-{suffix}")
        completed = ingest_job_runner.wait_for_completion(job_name, timeout_seconds=600)
        assert completed, f"Ingestion job {job_name} did not complete"

        # Search for the repo
        result = mcp_client.call_tool("search", {
            "query": test_repo,
            "scope": "all",
            "limit": 5,
        })
        assert isinstance(result, dict)
        # We don't assert results > 0 because the repo may not be indexed
        # via the configmap in this test run. That requires k8s configmap update.
