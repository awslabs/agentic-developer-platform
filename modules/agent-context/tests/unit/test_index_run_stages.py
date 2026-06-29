"""Unit tests for per-stage indexing tracking (issue #1423).

Tests cover:
- create_index_run: creates header row, returns UUID
- start_stage: validates stage names, creates row with status=running
- verify_stage: sets verified + verified_at + artifact_ref
- fail_stage: sets failed + error, never sets verified_at
- skip_stage: creates row with status=skipped
- should_skip_stage: returns True only for verified stages at same SHA
- complete_index_run: derives overall status from stage states
- reconcile_stages: flags drift when artifact is missing
- StageTracker: context manager contract (verify-after-write)

Uses mocks for psycopg2 (no live DB required).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add ingestion scripts to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "images" / "ingestion"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_conn():
    """Mock psycopg2 connection with cursor."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    conn.autocommit = False
    return conn


@pytest.fixture
def mock_conn_with_fetchone(mock_conn):
    """Mock connection where cursor.fetchone returns a configurable value."""
    return mock_conn


# ---------------------------------------------------------------------------
# create_index_run tests
# ---------------------------------------------------------------------------


class TestCreateIndexRun:
    """Test index run header creation."""

    def test_creates_run_returns_uuid(self, mock_conn):
        import db

        run_id = db.create_index_run(mock_conn, "repo-uuid-123", "org/repo", "abc123")
        assert run_id  # Non-empty UUID string
        assert len(run_id) == 36  # UUID format: 8-4-4-4-12
        mock_conn.commit.assert_called_once()

    def test_inserts_with_correct_params(self, mock_conn):
        import db

        run_id = db.create_index_run(mock_conn, "repo-uuid-123", "org/repo", "sha456")
        cursor = mock_conn.cursor.return_value
        call_args = cursor.execute.call_args[0]
        sql = call_args[0]
        params = call_args[1]

        assert "INSERT INTO index_runs" in sql
        assert params[0] == run_id  # UUID we generated
        assert params[1] == "repo-uuid-123"  # repo_id
        assert params[2] == "sha456"  # commit_sha

    def test_allows_none_commit_sha(self, mock_conn):
        import db

        run_id = db.create_index_run(mock_conn, "repo-uuid-123", "org/repo", None)
        assert run_id
        cursor = mock_conn.cursor.return_value
        params = cursor.execute.call_args[0][1]
        assert params[2] is None

    def test_rollback_on_error(self, mock_conn):
        import db

        cursor = mock_conn.cursor.return_value
        cursor.execute.side_effect = Exception("DB error")

        with pytest.raises(Exception, match="DB error"):
            db.create_index_run(mock_conn, "repo-uuid", "org/repo", "sha")
        mock_conn.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# start_stage tests
# ---------------------------------------------------------------------------


class TestStartStage:
    """Test stage start (attempt -> running)."""

    def test_creates_stage_row(self, mock_conn):
        import db

        stage_id = db.start_stage(mock_conn, "run-uuid", "org/repo", "clone")
        assert stage_id
        assert len(stage_id) == 36
        mock_conn.commit.assert_called_once()

    def test_rejects_invalid_stage(self, mock_conn):
        import db

        with pytest.raises(ValueError, match="Invalid stage"):
            db.start_stage(mock_conn, "run-uuid", "org/repo", "invalid_stage_name")

    def test_valid_stages_accepted(self, mock_conn):
        import db

        for stage in db.VALID_STAGES:
            mock_conn.reset_mock()
            stage_id = db.start_stage(mock_conn, "run-uuid", "org/repo", stage)
            assert stage_id

    def test_inserts_with_status_running(self, mock_conn):
        import db

        db.start_stage(mock_conn, "run-uuid", "org/repo", "deepwiki")
        cursor = mock_conn.cursor.return_value
        sql = cursor.execute.call_args[0][0]
        assert "'running'" in sql

    def test_rollback_on_error(self, mock_conn):
        import db

        cursor = mock_conn.cursor.return_value
        cursor.execute.side_effect = Exception("constraint violation")

        with pytest.raises(Exception, match="constraint violation"):
            db.start_stage(mock_conn, "run-uuid", "org/repo", "clone")
        mock_conn.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# verify_stage tests
# ---------------------------------------------------------------------------


class TestVerifyStage:
    """Test stage verification (the only path to 'verified' status)."""

    def test_sets_verified_status_and_timestamp(self, mock_conn):
        import db

        db.verify_stage(mock_conn, "stage-uuid", "s3://bucket/key.json")
        cursor = mock_conn.cursor.return_value
        sql = cursor.execute.call_args[0][0]
        params = cursor.execute.call_args[0][1]

        assert "status = 'verified'" in sql
        assert "verified_at" in sql
        assert params[0] == "s3://bucket/key.json"  # artifact_ref
        assert params[3] is None  # metrics (not provided)
        assert params[4] == "stage-uuid"  # WHERE id = %s
        mock_conn.commit.assert_called_once()

    def test_sets_completed_at(self, mock_conn):
        import db

        db.verify_stage(mock_conn, "stage-uuid", "artifact-ref")
        cursor = mock_conn.cursor.return_value
        sql = cursor.execute.call_args[0][0]
        assert "completed_at" in sql


# ---------------------------------------------------------------------------
# fail_stage tests
# ---------------------------------------------------------------------------


class TestFailStage:
    """Test stage failure (never sets verified_at)."""

    def test_sets_failed_status_with_error(self, mock_conn):
        import db

        db.fail_stage(mock_conn, "stage-uuid", "read-back failed: S3 object not found")
        cursor = mock_conn.cursor.return_value
        sql = cursor.execute.call_args[0][0]
        params = cursor.execute.call_args[0][1]

        assert "status = 'failed'" in sql
        assert "verified_at" not in sql  # Never sets verified_at
        assert params[0] == "read-back failed: S3 object not found"
        mock_conn.commit.assert_called_once()

    def test_never_sets_verified_at(self, mock_conn):
        import db

        db.fail_stage(mock_conn, "stage-uuid", "error")
        cursor = mock_conn.cursor.return_value
        sql = cursor.execute.call_args[0][0]
        # verified_at should NOT appear in the UPDATE SET clause
        assert "verified_at" not in sql


# ---------------------------------------------------------------------------
# skip_stage tests
# ---------------------------------------------------------------------------


class TestSkipStage:
    """Test skipping a stage."""

    def test_creates_skipped_row(self, mock_conn):
        import db

        stage_id = db.skip_stage(mock_conn, "run-uuid", "org/repo", "deepwiki", "disabled")
        assert stage_id
        cursor = mock_conn.cursor.return_value
        sql = cursor.execute.call_args[0][0]
        assert "'skipped'" in sql
        mock_conn.commit.assert_called_once()

    def test_rejects_invalid_stage(self, mock_conn):
        import db

        with pytest.raises(ValueError, match="Invalid stage"):
            db.skip_stage(mock_conn, "run-uuid", "org/repo", "bogus", "reason")


# ---------------------------------------------------------------------------
# should_skip_stage tests
# ---------------------------------------------------------------------------


class TestShouldSkipStage:
    """Test skip logic keyed off verified status."""

    def test_returns_true_when_verified_at_same_sha(self, mock_conn):
        import db

        cursor = mock_conn.cursor.return_value
        cursor.fetchone.return_value = (1,)  # row found = verified at this SHA

        assert db.should_skip_stage(mock_conn, "org/repo", "deepwiki", "abc123") is True

    def test_returns_false_when_not_verified(self, mock_conn):
        import db

        cursor = mock_conn.cursor.return_value
        cursor.fetchone.return_value = None  # no verified row

        assert db.should_skip_stage(mock_conn, "org/repo", "deepwiki", "abc123") is False

    def test_returns_false_when_sha_is_none(self, mock_conn):
        """If commit_sha is None, never skip (force re-run)."""
        import db

        result = db.should_skip_stage(mock_conn, "org/repo", "deepwiki", None)
        assert result is False
        # Should not even query the DB
        cursor = mock_conn.cursor.return_value
        cursor.execute.assert_not_called()

    def test_query_joins_on_commit_sha(self, mock_conn):
        import db

        cursor = mock_conn.cursor.return_value
        cursor.fetchone.return_value = None

        db.should_skip_stage(mock_conn, "org/repo", "clone", "sha789")
        sql = cursor.execute.call_args[0][0]
        params = cursor.execute.call_args[0][1]

        assert "ir.commit_sha" in sql
        assert "irs.status = 'verified'" in sql
        assert params == ("org/repo", "clone", "sha789")


# ---------------------------------------------------------------------------
# complete_index_run tests
# ---------------------------------------------------------------------------


class TestCompleteIndexRun:
    """Test index run finalization (derives status from stages)."""

    def test_all_verified_is_complete(self, mock_conn):
        import db

        cursor = mock_conn.cursor.return_value
        # (failed_count, done_count, total)
        cursor.fetchone.return_value = (0, 5, 5)

        db.complete_index_run(mock_conn, "run-uuid")
        # Second execute call is the UPDATE
        update_call = cursor.execute.call_args_list[1]
        assert update_call[0][1][0] == "complete"

    def test_some_failed_is_partial(self, mock_conn):
        import db

        cursor = mock_conn.cursor.return_value
        cursor.fetchone.return_value = (2, 3, 5)

        db.complete_index_run(mock_conn, "run-uuid")
        update_call = cursor.execute.call_args_list[1]
        assert update_call[0][1][0] == "partial"

    def test_none_done_is_incomplete(self, mock_conn):
        import db

        cursor = mock_conn.cursor.return_value
        cursor.fetchone.return_value = (0, 0, 5)

        db.complete_index_run(mock_conn, "run-uuid")
        update_call = cursor.execute.call_args_list[1]
        assert update_call[0][1][0] == "incomplete"


# ---------------------------------------------------------------------------
# reconcile_stages tests
# ---------------------------------------------------------------------------


class TestReconcileStages:
    """Test reconciliation (flags drift when artifact is missing)."""

    def test_no_drift_when_all_artifacts_exist(self, mock_conn):
        import db

        cursor = mock_conn.cursor.return_value
        cursor.fetchall.return_value = [
            ("stage-1", "deepwiki", "s3://bucket/wiki.md"),
            ("stage-2", "cgc_structural", "s3://bucket/code-index.md"),
        ]

        verify_fn = MagicMock(return_value=True)
        drifted = db.reconcile_stages(mock_conn, "org/repo", verify_fn)

        assert drifted == []
        assert verify_fn.call_count == 2

    def test_flags_drift_when_artifact_missing(self, mock_conn):
        import db

        cursor = mock_conn.cursor.return_value
        cursor.fetchall.return_value = [
            ("stage-1", "deepwiki", "s3://bucket/wiki.md"),
            ("stage-2", "cgc_structural", "s3://bucket/code-index.md"),
        ]

        # Second artifact is missing
        verify_fn = MagicMock(side_effect=[True, False])
        drifted = db.reconcile_stages(mock_conn, "org/repo", verify_fn)

        assert len(drifted) == 1
        assert drifted[0]["stage"] == "cgc_structural"
        assert drifted[0]["artifact_ref"] == "s3://bucket/code-index.md"
        mock_conn.commit.assert_called_once()

    def test_drift_updates_status_to_failed(self, mock_conn):
        import db

        cursor = mock_conn.cursor.return_value
        cursor.fetchall.return_value = [
            ("stage-1", "deepwiki", "s3://bucket/wiki.md"),
        ]

        verify_fn = MagicMock(return_value=False)
        db.reconcile_stages(mock_conn, "org/repo", verify_fn)

        # Should UPDATE the row to failed
        update_calls = [
            c for c in cursor.execute.call_args_list if "UPDATE" in str(c)
        ]
        assert len(update_calls) == 1
        sql = update_calls[0][0][0]
        assert "status = 'failed'" in sql
        # The error message is in the SQL as a literal value
        assert "reconciliation: artifact missing" in sql


# ---------------------------------------------------------------------------
# StageTracker (context manager) tests
# ---------------------------------------------------------------------------


class TestStageTracker:
    """Test the StageTracker context manager contract."""

    @patch("db.get_connection")
    @patch("db.create_index_run", return_value="run-uuid-001")
    def test_stage_verified_on_success(self, mock_create_run, mock_get_conn, mock_conn):
        from stage_tracker import StageTracker

        with patch("db.start_stage", return_value="stage-uuid-001"):
            with patch("db.verify_stage") as mock_verify:
                tracker = StageTracker.__new__(StageTracker)
                tracker._conn = mock_conn
                tracker._repo = "org/repo"
                tracker._repo_id = "repo-uuid"
                tracker._commit_sha = "sha123"
                tracker._run_id = "run-uuid-001"
                tracker._results = []
                tracker._worker_pod = "test-pod"

                with tracker.stage("clone") as ctx:
                    ctx.set_artifact("/path/to/clone")
                    ctx.verify(lambda: True)

                mock_verify.assert_called_once_with(
                    mock_conn, "stage-uuid-001", "/path/to/clone",
                    metrics=None,
                )
                assert tracker.results[0].status == "verified"

    @patch("db.get_connection")
    @patch("db.create_index_run", return_value="run-uuid-001")
    def test_stage_failed_when_verify_returns_false(self, mock_create_run, mock_get_conn, mock_conn):
        from stage_tracker import StageTracker

        with patch("db.start_stage", return_value="stage-uuid-001"):
            with patch("db.fail_stage") as mock_fail:
                tracker = StageTracker.__new__(StageTracker)
                tracker._conn = mock_conn
                tracker._repo = "org/repo"
                tracker._repo_id = "repo-uuid"
                tracker._commit_sha = "sha123"
                tracker._run_id = "run-uuid-001"
                tracker._results = []
                tracker._worker_pod = "test-pod"

                with tracker.stage("deepwiki") as ctx:
                    ctx.set_artifact("s3://bucket/wiki.md")
                    ctx.verify(lambda: False)  # Verification fails

                mock_fail.assert_called_once()
                assert tracker.results[0].status == "failed"
                assert "verification failed" in tracker.results[0].error

    @patch("db.get_connection")
    @patch("db.create_index_run", return_value="run-uuid-001")
    def test_stage_failed_on_exception(self, mock_create_run, mock_get_conn, mock_conn):
        from stage_tracker import StageTracker

        with patch("db.start_stage", return_value="stage-uuid-001"):
            with patch("db.fail_stage") as mock_fail:
                tracker = StageTracker.__new__(StageTracker)
                tracker._conn = mock_conn
                tracker._repo = "org/repo"
                tracker._repo_id = "repo-uuid"
                tracker._commit_sha = "sha123"
                tracker._run_id = "run-uuid-001"
                tracker._results = []
                tracker._worker_pod = "test-pod"

                with tracker.stage("sbom_source") as _ctx:
                    raise RuntimeError("Syft crashed")

                mock_fail.assert_called_once()
                assert tracker.results[0].status == "failed"
                assert "Syft crashed" in tracker.results[0].error

    @patch("db.get_connection")
    @patch("db.create_index_run", return_value="run-uuid-001")
    def test_stage_failed_without_verify_call(self, mock_create_run, mock_get_conn, mock_conn):
        """Stage that completes without calling verify() is marked failed."""
        from stage_tracker import StageTracker

        with patch("db.start_stage", return_value="stage-uuid-001"):
            with patch("db.fail_stage") as mock_fail:
                tracker = StageTracker.__new__(StageTracker)
                tracker._conn = mock_conn
                tracker._repo = "org/repo"
                tracker._repo_id = "repo-uuid"
                tracker._commit_sha = "sha123"
                tracker._run_id = "run-uuid-001"
                tracker._results = []
                tracker._worker_pod = "test-pod"

                with tracker.stage("clone") as ctx:
                    ctx.set_artifact("/path/to/clone")
                    # Never calls ctx.verify() — this is the bug we're preventing

                mock_fail.assert_called_once()
                assert tracker.results[0].status == "failed"

    @patch("db.get_connection")
    def test_mark_skipped(self, mock_get_conn, mock_conn):
        from stage_tracker import StageTracker

        with patch("db.create_index_run", return_value="run-uuid-001"):
            with patch("db.skip_stage") as mock_skip:
                tracker = StageTracker.__new__(StageTracker)
                tracker._conn = mock_conn
                tracker._repo = "org/repo"
                tracker._repo_id = "repo-uuid"
                tracker._commit_sha = "sha123"
                tracker._run_id = "run-uuid-001"
                tracker._results = []

                tracker.mark_skipped("deepwiki", "feature disabled")

                mock_skip.assert_called_once_with(
                    mock_conn, "run-uuid-001", "org/repo", "deepwiki", "feature disabled"
                )
                assert tracker.results[0].status == "skipped"


# ---------------------------------------------------------------------------
# Issue #2305: per-stage metrics + worker_pod tests
# ---------------------------------------------------------------------------


class TestStartStageWorkerPod:
    """Test worker_pod is written to stage row on start."""

    def test_worker_pod_passed_to_insert(self, mock_conn):
        import db

        db.start_stage(mock_conn, "run-uuid", "org/repo", "clone", worker_pod="ingestion-abc123")
        cursor = mock_conn.cursor.return_value
        sql = cursor.execute.call_args[0][0]
        params = cursor.execute.call_args[0][1]

        assert "worker_pod" in sql
        assert params[-1] == "ingestion-abc123"

    def test_worker_pod_none_by_default(self, mock_conn):
        import db

        db.start_stage(mock_conn, "run-uuid", "org/repo", "clone")
        cursor = mock_conn.cursor.return_value
        params = cursor.execute.call_args[0][1]

        # Last param is worker_pod, should be None when not specified
        assert params[-1] is None


class TestVerifyStageMetrics:
    """Test metrics JSONB is written on verify."""

    def test_metrics_written_as_json(self, mock_conn):
        import db

        db.verify_stage(
            mock_conn, "stage-uuid", "s3://bucket/key.json",
            metrics={"symbols": 42, "files": 10},
        )
        cursor = mock_conn.cursor.return_value
        sql = cursor.execute.call_args[0][0]
        params = cursor.execute.call_args[0][1]

        assert "metrics" in sql
        # metrics is JSON-serialized; it's the 4th param (after artifact_ref, now, now)
        import json
        assert json.loads(params[3]) == {"symbols": 42, "files": 10}

    def test_metrics_none_by_default(self, mock_conn):
        import db

        db.verify_stage(mock_conn, "stage-uuid", "artifact-ref")
        cursor = mock_conn.cursor.return_value
        params = cursor.execute.call_args[0][1]

        # metrics param should be None when not specified
        assert params[3] is None

    def test_metrics_empty_dict_serialized(self, mock_conn):
        import db

        db.verify_stage(
            mock_conn, "stage-uuid", "artifact-ref",
            metrics={},
        )
        cursor = mock_conn.cursor.return_value
        params = cursor.execute.call_args[0][1]

        import json
        assert json.loads(params[3]) == {}


class TestScipStructuralValidStage:
    """Test that scip_structural is now a valid stage name."""

    def test_scip_structural_in_valid_stages(self):
        import db

        assert "scip_structural" in db.VALID_STAGES

    def test_start_stage_accepts_scip_structural(self, mock_conn):
        import db

        stage_id = db.start_stage(mock_conn, "run-uuid", "org/repo", "scip_structural")
        assert stage_id


class TestStageTrackerWorkerPod:
    """Test StageTracker captures POD_NAME and passes it to start_stage."""

    @patch("db.create_index_run", return_value="run-uuid-001")
    def test_captures_pod_name_from_env(self, mock_create_run, mock_conn):
        from stage_tracker import StageTracker

        with patch.dict(os.environ, {"POD_NAME": "ingestion-worker-xyz789"}):
            tracker = StageTracker(mock_conn, "org/repo", "repo-uuid", "sha123")
            assert tracker._worker_pod == "ingestion-worker-xyz789"

    @patch("db.create_index_run", return_value="run-uuid-001")
    def test_falls_back_to_hostname(self, mock_create_run, mock_conn):
        from stage_tracker import StageTracker

        with patch.dict(os.environ, {}, clear=False):
            # Remove POD_NAME if set
            env_copy = os.environ.copy()
            env_copy.pop("POD_NAME", None)
            with patch.dict(os.environ, env_copy, clear=True):
                with patch("socket.gethostname", return_value="my-hostname"):
                    tracker = StageTracker(mock_conn, "org/repo", "repo-uuid", "sha123")
                    assert tracker._worker_pod == "my-hostname"

    @patch("db.create_index_run", return_value="run-uuid-001")
    def test_worker_pod_passed_to_start_stage(self, mock_create_run, mock_conn):
        from stage_tracker import StageTracker

        with patch.dict(os.environ, {"POD_NAME": "ingestion-worker-abc"}):
            with patch("db.start_stage", return_value="stage-uuid-001") as mock_start:
                with patch("db.verify_stage"):
                    tracker = StageTracker(mock_conn, "org/repo", "repo-uuid", "sha123")

                    with tracker.stage("clone") as ctx:
                        ctx.set_artifact("/path/to/clone")
                        ctx.verify(lambda: True)

                    mock_start.assert_called_once_with(
                        mock_conn, "run-uuid-001", "org/repo", "clone",
                        worker_pod="ingestion-worker-abc",
                    )


class TestStageTrackerSetMetrics:
    """Test StageContext.set_metrics and metrics passing to verify_stage."""

    @patch("db.create_index_run", return_value="run-uuid-001")
    def test_set_metrics_passed_to_verify_stage(self, mock_create_run, mock_conn):
        from stage_tracker import StageTracker

        with patch.dict(os.environ, {"POD_NAME": "test-pod"}):
            with patch("db.start_stage", return_value="stage-uuid-001"):
                with patch("db.verify_stage") as mock_verify:
                    tracker = StageTracker(mock_conn, "org/repo", "repo-uuid", "sha123")

                    with tracker.stage("cgc_structural") as ctx:
                        ctx.set_artifact("s3://bucket/code-index.md")
                        ctx.set_metrics({"symbols": 100, "files": 25})
                        ctx.verify(lambda: True)

                    mock_verify.assert_called_once_with(
                        mock_conn, "stage-uuid-001", "s3://bucket/code-index.md",
                        metrics={"symbols": 100, "files": 25},
                    )

    @patch("db.create_index_run", return_value="run-uuid-001")
    def test_no_metrics_passes_none(self, mock_create_run, mock_conn):
        from stage_tracker import StageTracker

        with patch.dict(os.environ, {"POD_NAME": "test-pod"}):
            with patch("db.start_stage", return_value="stage-uuid-001"):
                with patch("db.verify_stage") as mock_verify:
                    tracker = StageTracker(mock_conn, "org/repo", "repo-uuid", "sha123")

                    with tracker.stage("clone") as ctx:
                        ctx.set_artifact("/path/to/clone")
                        ctx.verify(lambda: True)

                    mock_verify.assert_called_once_with(
                        mock_conn, "stage-uuid-001", "/path/to/clone",
                        metrics=None,
                    )
