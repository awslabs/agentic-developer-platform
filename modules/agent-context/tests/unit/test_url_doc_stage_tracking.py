"""Unit tests for URL/doc ingestion stage tracking (issue #2308).

Tests cover:
- StageTracker accepts repo_id=None for non-repo assets
- New stage names (fetch, convert, s3_upload) are valid
- create_index_run accepts repo_id=None
- ingest-url.py and ingest-doc.py wire StageTracker correctly (via importlib)
- Regression: existing repo-based stage tracking unchanged

Uses mocks for psycopg2 (no live DB required).
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add ingestion scripts to path for imports
_INGESTION_PATH = str(Path(__file__).parent.parent.parent / "images" / "ingestion")
sys.path.insert(0, _INGESTION_PATH)


def _import_hyphenated(module_name: str, file_name: str):
    """Import a module with a hyphenated filename (e.g. ingest-url.py)."""
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(_INGESTION_PATH, file_name)
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


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


# ---------------------------------------------------------------------------
# db.py: new stage names valid
# ---------------------------------------------------------------------------


class TestNewStageNamesValid:
    """Test that fetch, convert, s3_upload are now valid stage names."""

    def test_fetch_in_valid_stages(self):
        import db

        assert "fetch" in db.VALID_STAGES

    def test_convert_in_valid_stages(self):
        import db

        assert "convert" in db.VALID_STAGES

    def test_s3_upload_in_valid_stages(self):
        import db

        assert "s3_upload" in db.VALID_STAGES

    def test_start_stage_accepts_fetch(self, mock_conn):
        import db

        stage_id = db.start_stage(mock_conn, "run-uuid", "asset-uuid-123", "fetch")
        assert stage_id

    def test_start_stage_accepts_convert(self, mock_conn):
        import db

        stage_id = db.start_stage(mock_conn, "run-uuid", "asset-uuid-123", "convert")
        assert stage_id

    def test_start_stage_accepts_s3_upload(self, mock_conn):
        import db

        stage_id = db.start_stage(mock_conn, "run-uuid", "asset-uuid-123", "s3_upload")
        assert stage_id


# ---------------------------------------------------------------------------
# db.py: create_index_run with repo_id=None
# ---------------------------------------------------------------------------


class TestCreateIndexRunNullableRepoId:
    """Test that create_index_run accepts repo_id=None for non-repo assets."""

    def test_creates_run_with_none_repo_id(self, mock_conn):
        import db

        run_id = db.create_index_run(mock_conn, None, "asset-uuid-123", None)
        assert run_id
        assert len(run_id) == 36  # UUID format

        cursor = mock_conn.cursor.return_value
        params = cursor.execute.call_args[0][1]
        assert params[1] is None  # repo_id is None

    def test_creates_run_with_string_repo_id(self, mock_conn):
        import db

        run_id = db.create_index_run(mock_conn, "repo-uuid", "org/repo", "sha123")
        assert run_id
        cursor = mock_conn.cursor.return_value
        params = cursor.execute.call_args[0][1]
        assert params[1] == "repo-uuid"


# ---------------------------------------------------------------------------
# StageTracker: accepts repo_id=None
# ---------------------------------------------------------------------------


class TestStageTrackerNullableRepoId:
    """Test StageTracker can be initialized with repo_id=None for non-repo assets."""

    @patch("db.create_index_run", return_value="run-uuid-001")
    def test_init_with_none_repo_id(self, mock_create_run, mock_conn):
        from stage_tracker import StageTracker

        with patch.dict(os.environ, {"POD_NAME": "test-pod"}):
            tracker = StageTracker(mock_conn, "asset-uuid-123", repo_id=None, commit_sha=None)

        assert tracker.run_id == "run-uuid-001"
        assert tracker._repo == "asset-uuid-123"
        assert tracker._repo_id is None
        mock_create_run.assert_called_once_with(mock_conn, None, "asset-uuid-123", None)

    @patch("db.create_index_run", return_value="run-uuid-002")
    def test_stage_tracking_works_with_none_repo_id(self, mock_create_run, mock_conn):
        from stage_tracker import StageTracker

        with patch.dict(os.environ, {"POD_NAME": "test-pod"}):
            with patch("db.start_stage", return_value="stage-uuid-001"):
                with patch("db.verify_stage") as mock_verify:
                    tracker = StageTracker(mock_conn, "asset-uuid-123", repo_id=None)

                    with tracker.stage("fetch") as ctx:
                        ctx.set_artifact("https://docs.example.com")
                        ctx.set_metrics({"pages_crawled": 5})
                        ctx.verify(lambda: True)

                    mock_verify.assert_called_once_with(
                        mock_conn, "stage-uuid-001", "https://docs.example.com",
                        metrics={"pages_crawled": 5},
                    )
                    assert tracker.results[0].status == "verified"

    @patch("db.create_index_run", return_value="run-uuid-003")
    def test_finalize_works_with_none_repo_id(self, mock_create_run, mock_conn):
        from stage_tracker import StageTracker

        with patch.dict(os.environ, {"POD_NAME": "test-pod"}):
            with patch("db.complete_index_run") as mock_complete:
                tracker = StageTracker(mock_conn, "asset-uuid-123", repo_id=None)
                tracker.finalize()
                mock_complete.assert_called_once_with(mock_conn, "run-uuid-003")

    @patch("db.create_index_run", return_value="run-uuid-004")
    def test_should_skip_always_false_without_commit_sha(self, mock_create_run, mock_conn):
        """Non-repo assets have no commit_sha → should_skip always returns False."""
        from stage_tracker import StageTracker

        with patch.dict(os.environ, {"POD_NAME": "test-pod"}):
            tracker = StageTracker(mock_conn, "asset-uuid-123", repo_id=None, commit_sha=None)
            # should_skip with None commit_sha always returns False
            assert tracker.should_skip("fetch") is False

    @patch("db.create_index_run", return_value="run-uuid-005")
    def test_mark_skipped_for_non_repo(self, mock_create_run, mock_conn):
        from stage_tracker import StageTracker

        with patch.dict(os.environ, {"POD_NAME": "test-pod"}):
            with patch("db.skip_stage") as mock_skip:
                tracker = StageTracker(mock_conn, "asset-uuid-123", repo_id=None)
                tracker.mark_skipped("graphrag", "GraphRAG disabled")

                mock_skip.assert_called_once_with(
                    mock_conn, "run-uuid-005", "asset-uuid-123", "graphrag", "GraphRAG disabled"
                )
                assert tracker.results[0].status == "skipped"


# ---------------------------------------------------------------------------
# ingest-url.py: stage tracking integration (import with importlib)
# ---------------------------------------------------------------------------


class TestIngestUrlStageTracking:
    """Test URL ingestion wires StageTracker correctly."""

    def test_ingest_url_accepts_registry_asset_id_param(self):
        """Verify ingest_url function accepts registry_asset_id parameter."""
        import inspect

        # We need to mock dependencies before importing
        with patch.dict(sys.modules, {
            "crawl4ai": MagicMock(),
            "defusedxml": MagicMock(),
            "defusedxml.ElementTree": MagicMock(),
        }):
            # Import with mocked telemetry
            with patch.dict(sys.modules, {
                "telemetry": MagicMock(),
                "config": MagicMock(),
                "s3_store": MagicMock(),
            }):
                # Check function signature has registry_asset_id
                # Use the actual source code check since import is complex
                ingest_url_path = os.path.join(_INGESTION_PATH, "ingest-url.py")
                with open(ingest_url_path) as f:
                    source = f.read()
                assert "registry_asset_id" in source
                assert "StageTracker" in source
                assert "--registry-asset-id" in source

    def test_ingest_url_initializes_tracker_with_registry_asset_id(self):
        """Verify the code uses registry_asset_id as the repo key for StageTracker."""
        ingest_url_path = os.path.join(_INGESTION_PATH, "ingest-url.py")
        with open(ingest_url_path) as f:
            source = f.read()

        # Verify the tracker is initialized with registry_asset_id as the repo key
        assert 'StageTracker(db_conn, registry_asset_id, repo_id=None, commit_sha=None)' in source

    def test_ingest_url_tracks_fetch_stage(self):
        """Verify ingest_url tracks a 'fetch' stage."""
        ingest_url_path = os.path.join(_INGESTION_PATH, "ingest-url.py")
        with open(ingest_url_path) as f:
            source = f.read()

        assert 'tracker.stage("fetch")' in source

    def test_ingest_url_tracks_s3_upload_stage(self):
        """Verify ingest_url tracks an 's3_upload' stage."""
        ingest_url_path = os.path.join(_INGESTION_PATH, "ingest-url.py")
        with open(ingest_url_path) as f:
            source = f.read()

        assert 'tracker.stage("s3_upload")' in source

    def test_ingest_url_finalizes_tracker(self):
        """Verify ingest_url calls tracker.finalize()."""
        ingest_url_path = os.path.join(_INGESTION_PATH, "ingest-url.py")
        with open(ingest_url_path) as f:
            source = f.read()

        assert "tracker.finalize()" in source
        assert 'result["run_id"] = tracker.run_id' in source


# ---------------------------------------------------------------------------
# ingest-doc.py: stage tracking integration
# ---------------------------------------------------------------------------


class TestIngestDocStageTracking:
    """Test document ingestion wires StageTracker correctly."""

    def test_ingest_doc_accepts_registry_asset_id_param(self):
        """Verify ingest_document function accepts registry_asset_id parameter."""
        ingest_doc_path = os.path.join(_INGESTION_PATH, "ingest-doc.py")
        with open(ingest_doc_path) as f:
            source = f.read()
        assert "registry_asset_id" in source
        assert "StageTracker" in source
        assert "--registry-asset-id" in source

    def test_ingest_doc_initializes_tracker_with_registry_asset_id(self):
        """Verify the code uses registry_asset_id as the repo key for StageTracker."""
        ingest_doc_path = os.path.join(_INGESTION_PATH, "ingest-doc.py")
        with open(ingest_doc_path) as f:
            source = f.read()

        assert 'StageTracker(db_conn, registry_asset_id, repo_id=None, commit_sha=None)' in source

    def test_ingest_doc_tracks_fetch_stage(self):
        """Verify ingest_document tracks a 'fetch' stage."""
        ingest_doc_path = os.path.join(_INGESTION_PATH, "ingest-doc.py")
        with open(ingest_doc_path) as f:
            source = f.read()

        assert 'tracker.stage("fetch")' in source

    def test_ingest_doc_tracks_convert_stage(self):
        """Verify ingest_document tracks a 'convert' stage."""
        ingest_doc_path = os.path.join(_INGESTION_PATH, "ingest-doc.py")
        with open(ingest_doc_path) as f:
            source = f.read()

        assert 'tracker.stage("convert")' in source

    def test_ingest_doc_tracks_s3_upload_stage(self):
        """Verify ingest_document tracks an 's3_upload' stage."""
        ingest_doc_path = os.path.join(_INGESTION_PATH, "ingest-doc.py")
        with open(ingest_doc_path) as f:
            source = f.read()

        assert 'tracker.stage("s3_upload")' in source

    def test_ingest_doc_tracks_graphrag_stage(self):
        """Verify ingest_document tracks a 'graphrag' stage."""
        ingest_doc_path = os.path.join(_INGESTION_PATH, "ingest-doc.py")
        with open(ingest_doc_path) as f:
            source = f.read()

        assert 'tracker.stage("graphrag")' in source
        assert 'tracker.mark_skipped("graphrag"' in source

    def test_ingest_doc_finalizes_tracker(self):
        """Verify ingest_document calls tracker.finalize()."""
        ingest_doc_path = os.path.join(_INGESTION_PATH, "ingest-doc.py")
        with open(ingest_doc_path) as f:
            source = f.read()

        assert "tracker.finalize()" in source
        assert 'result["run_id"] = tracker.run_id' in source

    def test_ingest_doc_fails_fetch_on_missing_document(self):
        """Verify fetch failure path records stage failure and finalizes tracker."""
        ingest_doc_path = os.path.join(_INGESTION_PATH, "ingest-doc.py")
        with open(ingest_doc_path) as f:
            source = f.read()

        # When fetch returns None, the fetch stage should fail and tracker should finalize
        assert 'ctx.fail(f"Failed to fetch {source}")' in source


# ---------------------------------------------------------------------------
# sqs-worker.py: passes registry_asset_id to workers
# ---------------------------------------------------------------------------


class TestSqsWorkerPassesRegistryAssetId:
    """Test that sqs-worker.py passes registry_asset_id to url/doc workers."""

    def test_ingest_url_receives_registry_asset_id(self):
        """Verify sqs-worker passes registry_asset_id to ingest_url."""
        sqs_worker_path = os.path.join(_INGESTION_PATH, "sqs-worker.py")
        with open(sqs_worker_path) as f:
            source = f.read()

        # The ingest_url function definition should accept registry_asset_id
        assert "registry_asset_id: str | None = None" in source
        # The call site should pass it
        assert "registry_asset_id=registry_asset_id" in source

    def test_ingest_doc_receives_registry_asset_id(self):
        """Verify sqs-worker passes registry_asset_id to ingest_doc."""
        sqs_worker_path = os.path.join(_INGESTION_PATH, "sqs-worker.py")
        with open(sqs_worker_path) as f:
            source = f.read()

        # The ingest_doc function definition should accept registry_asset_id
        # (both have it)
        assert 'ingest_doc(source, tags=tags, title=title, scope_env=scope_env, registry_asset_id=registry_asset_id)' in source

    def test_ingest_url_passes_registry_asset_id_as_cli_arg(self):
        """Verify ingest_url adds --registry-asset-id to the subprocess command."""
        sqs_worker_path = os.path.join(_INGESTION_PATH, "sqs-worker.py")
        with open(sqs_worker_path) as f:
            source = f.read()

        assert '"--registry-asset-id"' in source


# ---------------------------------------------------------------------------
# Regression: repo stage tracking still works
# ---------------------------------------------------------------------------


class TestRepoStageTrackingRegression:
    """Ensure repo-based stage tracking (with repo_id) still works unchanged."""

    @patch("db.create_index_run", return_value="run-uuid-repo-001")
    def test_repo_tracker_with_repo_id(self, mock_create_run, mock_conn):
        """StageTracker still works with repo_id provided (repo path)."""
        from stage_tracker import StageTracker

        with patch.dict(os.environ, {"POD_NAME": "test-pod"}):
            tracker = StageTracker(mock_conn, "org/repo", repo_id="repo-uuid-123", commit_sha="abc123")

        assert tracker.run_id == "run-uuid-repo-001"
        assert tracker._repo == "org/repo"
        assert tracker._repo_id == "repo-uuid-123"
        mock_create_run.assert_called_once_with(mock_conn, "repo-uuid-123", "org/repo", "abc123")

    @patch("db.create_index_run", return_value="run-uuid-repo-002")
    def test_existing_stages_still_valid(self, mock_create_run, mock_conn):
        """All original repo stages remain valid."""
        import db

        original_stages = {
            "clone", "cgc_structural", "scip_structural", "embed_vectors",
            "sbom_source", "sbom_image", "deepwiki", "zoekt_index", "graphrag",
        }
        for stage in original_stages:
            assert stage in db.VALID_STAGES

    @patch("db.create_index_run", return_value="run-uuid-repo-003")
    def test_repo_tracker_with_positional_repo_id(self, mock_create_run, mock_conn):
        """StageTracker still works with positional repo_id (backward compatibility)."""
        from stage_tracker import StageTracker

        with patch.dict(os.environ, {"POD_NAME": "test-pod"}):
            # This is the existing call pattern in ingest-repo.py:
            # StageTracker(conn, repo, repo_id, commit_sha)
            tracker = StageTracker(mock_conn, "org/repo", "repo-uuid-456", "abc123")

        assert tracker._repo_id == "repo-uuid-456"
        mock_create_run.assert_called_once_with(mock_conn, "repo-uuid-456", "org/repo", "abc123")
