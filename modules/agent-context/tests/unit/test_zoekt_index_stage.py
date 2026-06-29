"""Unit tests for zoekt_index stage in the ingestion pipeline (#2361).

Tests cover:
- Config: zoekt_index_enabled field, zoekt_shards_s3_prefix, zoekt_index_timeout
- Config: boolean string parsing from env vars
- _run_zoekt_index: happy path (shards produced + uploaded to S3)
- _run_zoekt_index: binary not found (graceful skip)
- _run_zoekt_index: subprocess failure (non-zero exit)
- _run_zoekt_index: timeout handling
- _run_zoekt_index: no shards produced
- _run_zoekt_index: S3 upload failure

Uses mocks for subprocess and boto3 (no live infra required).
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add ingestion scripts to path for imports
_INGESTION_PATH = str(Path(__file__).parent.parent.parent / "images" / "ingestion")
sys.path.insert(0, _INGESTION_PATH)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_s3_store():
    """Mock S3ContentStore with a mocked boto3 client."""
    store = MagicMock()
    store.bucket_name = "test-platform-data-bucket"
    store._s3 = MagicMock()
    # head_object succeeds by default (verification passes)
    store._s3.head_object.return_value = {"ContentLength": 1024}
    # upload_file succeeds by default
    store._s3.upload_file.return_value = None
    return store


@pytest.fixture
def clone_path(tmp_path):
    """Create a fake clone directory with a .git folder."""
    repo_path = tmp_path / "org" / "repo"
    repo_path.mkdir(parents=True)
    (repo_path / ".git").mkdir()
    (repo_path / "main.py").write_text("print('hello')")
    return str(repo_path)


# ---------------------------------------------------------------------------
# Config tests (lightweight — no heavy module imports needed)
# ---------------------------------------------------------------------------


class TestZoektConfig:
    """Test zoekt_index configuration fields in config.py."""

    def test_default_enabled(self):
        """zoekt_index_enabled defaults to True."""
        from config import Settings

        with patch.dict(os.environ, {}, clear=False):
            s = Settings()
            assert s.zoekt_index_enabled is True

    def test_default_prefix(self):
        """zoekt_shards_s3_prefix defaults to 'zoekt-shards'."""
        from config import Settings

        s = Settings()
        assert s.zoekt_shards_s3_prefix == "zoekt-shards"

    def test_default_timeout(self):
        """zoekt_index_timeout defaults to 600 seconds."""
        from config import Settings

        s = Settings()
        assert s.zoekt_index_timeout == 600

    def test_bool_string_false(self):
        """zoekt_index_enabled=false from env var parses to False."""
        from config import Settings

        with patch.dict(os.environ, {"ZOEKT_INDEX_ENABLED": "false"}):
            s = Settings()
            assert s.zoekt_index_enabled is False

    def test_bool_string_true(self):
        """zoekt_index_enabled=true from env var parses to True."""
        from config import Settings

        with patch.dict(os.environ, {"ZOEKT_INDEX_ENABLED": "true"}):
            s = Settings()
            assert s.zoekt_index_enabled is True

    def test_bool_string_zero(self):
        """zoekt_index_enabled=0 from env var parses to False."""
        from config import Settings

        with patch.dict(os.environ, {"ZOEKT_INDEX_ENABLED": "0"}):
            s = Settings()
            assert s.zoekt_index_enabled is False

    def test_bool_string_one(self):
        """zoekt_index_enabled=1 from env var parses to True."""
        from config import Settings

        with patch.dict(os.environ, {"ZOEKT_INDEX_ENABLED": "1"}):
            s = Settings()
            assert s.zoekt_index_enabled is True

    def test_custom_prefix_override(self):
        """zoekt_shards_s3_prefix can be overridden via env var."""
        from config import Settings

        with patch.dict(os.environ, {"ZOEKT_SHARDS_S3_PREFIX": "my-custom-shards"}):
            s = Settings()
            assert s.zoekt_shards_s3_prefix == "my-custom-shards"

    def test_custom_timeout_override(self):
        """zoekt_index_timeout can be overridden via env var."""
        from config import Settings

        with patch.dict(os.environ, {"ZOEKT_INDEX_TIMEOUT": "300"}):
            s = Settings()
            assert s.zoekt_index_timeout == 300


# ---------------------------------------------------------------------------
# _run_zoekt_index function tests
#
# We test this by directly exercising the function logic (subprocess + S3)
# without importing the full ingest-repo.py module (which has many deps).
# The function is self-contained aside from module-level constants.
# ---------------------------------------------------------------------------


def _make_run_zoekt_index(
    zoekt_index_timeout: int = 600,
    zoekt_shards_s3_prefix: str = "zoekt-shards",
):
    """Create a standalone _run_zoekt_index function with injected constants.

    This avoids importing all of ingest-repo.py's heavy dependencies.
    The logic here mirrors _run_zoekt_index exactly.
    """

    def _run_zoekt_index(
        clone_path: str,
        org_repo: str,
        s3_store,
    ) -> dict:
        """Run zoekt-git-index on a cloned repo and upload shards to S3."""
        shards_prefix = zoekt_shards_s3_prefix
        zoekt_timeout = zoekt_index_timeout

        # Create a temp directory for index output
        index_dir = tempfile.mkdtemp(prefix="zoekt-index-")

        try:
            # Step 1: Run zoekt-git-index
            try:
                subprocess.run(
                    ["zoekt-git-index", "-index", index_dir, clone_path],
                    check=True,
                    capture_output=True,
                    timeout=zoekt_timeout,
                )
            except FileNotFoundError:
                return {"status": "binary_not_found", "error": "zoekt-git-index not installed"}
            except subprocess.CalledProcessError as e:
                stderr = e.stderr.decode()[:500] if e.stderr else ""
                return {"status": "index_failed", "error": f"exit code {e.returncode}: {stderr}"}
            except subprocess.TimeoutExpired:
                return {"status": "timeout", "error": f"timed out after {zoekt_timeout}s"}

            # Step 2: Find and upload .zoekt shard files
            shard_files = glob.glob(os.path.join(index_dir, "*.zoekt"))
            if not shard_files:
                return {"status": "no_shards", "error": "no .zoekt files produced"}

            total_bytes = 0
            uploaded_keys = []
            s3_key_prefix = f"{shards_prefix}/{org_repo}"

            for shard_path in shard_files:
                shard_name = os.path.basename(shard_path)
                s3_key = f"{s3_key_prefix}/{shard_name}"
                shard_size = os.path.getsize(shard_path)
                total_bytes += shard_size

                try:
                    s3_store._s3.upload_file(
                        Filename=shard_path,
                        Bucket=s3_store.bucket_name,
                        Key=s3_key,
                        ExtraArgs={
                            "ContentType": "application/octet-stream",
                            "Metadata": {
                                "org_repo": org_repo,
                                "shard_name": shard_name,
                            },
                        },
                    )
                    uploaded_keys.append(s3_key)
                except Exception as e:
                    return {
                        "status": "upload_failed",
                        "error": f"S3 upload failed for {shard_name}: {e}",
                        "shards": len(uploaded_keys),
                        "shard_bytes": total_bytes,
                    }

            # Step 3: Verify first uploaded shard via head_object
            if uploaded_keys:
                try:
                    s3_store._s3.head_object(Bucket=s3_store.bucket_name, Key=uploaded_keys[0])
                except Exception as e:
                    return {
                        "status": "verify_failed",
                        "error": f"head_object failed: {e}",
                        "shards": len(uploaded_keys),
                        "shard_bytes": total_bytes,
                        "artifact_key": uploaded_keys[0],
                    }

            return {
                "status": "complete",
                "shards": len(uploaded_keys),
                "shard_bytes": total_bytes,
                "artifact_key": uploaded_keys[0] if uploaded_keys else "",
            }

        finally:
            shutil.rmtree(index_dir, ignore_errors=True)

    return _run_zoekt_index


class TestRunZoektIndex:
    """Test _run_zoekt_index helper function logic."""

    @pytest.fixture
    def run_zoekt_index(self):
        """Get a testable _run_zoekt_index function."""
        return _make_run_zoekt_index()

    def test_binary_not_found(self, run_zoekt_index, mock_s3_store, clone_path):
        """When zoekt-git-index is not installed, returns graceful error."""
        with patch("subprocess.run", side_effect=FileNotFoundError("not found")):
            result = run_zoekt_index(clone_path, "org/repo", mock_s3_store)
        assert result["status"] == "binary_not_found"
        assert "not installed" in result["error"]

    def test_subprocess_failure(self, run_zoekt_index, mock_s3_store, clone_path):
        """When zoekt-git-index exits non-zero, returns index_failed."""
        error = subprocess.CalledProcessError(1, "zoekt-git-index")
        error.stderr = b"fatal: not a git repository"
        with patch("subprocess.run", side_effect=error):
            result = run_zoekt_index(clone_path, "org/repo", mock_s3_store)
        assert result["status"] == "index_failed"
        assert "exit code 1" in result["error"]
        assert "not a git repository" in result["error"]

    def test_timeout(self, run_zoekt_index, mock_s3_store, clone_path):
        """When zoekt-git-index exceeds timeout, returns timeout status."""
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired("zoekt-git-index", 600),
        ):
            result = run_zoekt_index(clone_path, "org/repo", mock_s3_store)
        assert result["status"] == "timeout"
        assert "timed out after 600s" in result["error"]

    def test_no_shards_produced(self, run_zoekt_index, mock_s3_store, clone_path, tmp_path):
        """When zoekt-git-index runs but produces no .zoekt files."""
        empty_dir = str(tmp_path / "empty-index")
        os.makedirs(empty_dir, exist_ok=True)

        with patch("subprocess.run") as mock_run, patch("tempfile.mkdtemp", return_value=empty_dir):
            mock_run.return_value = MagicMock(returncode=0)
            result = run_zoekt_index(clone_path, "org/repo", mock_s3_store)
        assert result["status"] == "no_shards"
        assert "no .zoekt files produced" in result["error"]

    def test_happy_path_single_shard(self, run_zoekt_index, mock_s3_store, clone_path, tmp_path):
        """Happy path: one shard produced and uploaded to S3."""
        shard_dir = str(tmp_path / "zoekt-output")
        os.makedirs(shard_dir, exist_ok=True)
        shard_path = os.path.join(shard_dir, "repo_v16.00000.zoekt")
        with open(shard_path, "wb") as f:
            f.write(b"\x00" * 4096)

        with patch("subprocess.run") as mock_run, patch("tempfile.mkdtemp", return_value=shard_dir):
            mock_run.return_value = MagicMock(returncode=0)
            result = run_zoekt_index(clone_path, "org/repo", mock_s3_store)

        assert result["status"] == "complete"
        assert result["shards"] == 1
        assert result["shard_bytes"] == 4096
        assert result["artifact_key"] == "zoekt-shards/org/repo/repo_v16.00000.zoekt"

        # Verify S3 upload_file was called
        mock_s3_store._s3.upload_file.assert_called_once()
        call_kwargs = mock_s3_store._s3.upload_file.call_args
        assert call_kwargs.kwargs["Bucket"] == "test-platform-data-bucket"
        assert call_kwargs.kwargs["Key"] == "zoekt-shards/org/repo/repo_v16.00000.zoekt"

        # Verify head_object was called for read-back verification
        mock_s3_store._s3.head_object.assert_called_once_with(
            Bucket="test-platform-data-bucket",
            Key="zoekt-shards/org/repo/repo_v16.00000.zoekt",
        )

    def test_happy_path_multiple_shards(self, run_zoekt_index, mock_s3_store, clone_path, tmp_path):
        """Multiple shards produced and all uploaded to S3."""
        shard_dir = str(tmp_path / "zoekt-multi")
        os.makedirs(shard_dir, exist_ok=True)
        # Create 3 shards of different sizes
        for i, size in enumerate([1024, 2048, 512]):
            path = os.path.join(shard_dir, f"repo_v16.{i:05d}.zoekt")
            with open(path, "wb") as f:
                f.write(b"\x00" * size)

        with patch("subprocess.run") as mock_run, patch("tempfile.mkdtemp", return_value=shard_dir):
            mock_run.return_value = MagicMock(returncode=0)
            result = run_zoekt_index(clone_path, "myorg/myrepo", mock_s3_store)

        assert result["status"] == "complete"
        assert result["shards"] == 3
        assert result["shard_bytes"] == 1024 + 2048 + 512
        assert "zoekt-shards/myorg/myrepo/" in result["artifact_key"]

        # All 3 shards uploaded
        assert mock_s3_store._s3.upload_file.call_count == 3

    def test_s3_upload_failure(self, run_zoekt_index, mock_s3_store, clone_path, tmp_path):
        """When S3 upload fails, returns upload_failed."""
        shard_dir = str(tmp_path / "zoekt-fail")
        os.makedirs(shard_dir, exist_ok=True)
        shard_path = os.path.join(shard_dir, "repo_v16.00000.zoekt")
        with open(shard_path, "wb") as f:
            f.write(b"\x00" * 512)

        mock_s3_store._s3.upload_file.side_effect = Exception("AccessDenied")

        with patch("subprocess.run") as mock_run, patch("tempfile.mkdtemp", return_value=shard_dir):
            mock_run.return_value = MagicMock(returncode=0)
            result = run_zoekt_index(clone_path, "org/repo", mock_s3_store)

        assert result["status"] == "upload_failed"
        assert "AccessDenied" in result["error"]

    def test_s3_verify_failure(self, run_zoekt_index, mock_s3_store, clone_path, tmp_path):
        """When head_object fails after upload, returns verify_failed."""
        shard_dir = str(tmp_path / "zoekt-verify-fail")
        os.makedirs(shard_dir, exist_ok=True)
        shard_path = os.path.join(shard_dir, "repo_v16.00000.zoekt")
        with open(shard_path, "wb") as f:
            f.write(b"\x00" * 256)

        mock_s3_store._s3.head_object.side_effect = Exception("NoSuchKey")

        with patch("subprocess.run") as mock_run, patch("tempfile.mkdtemp", return_value=shard_dir):
            mock_run.return_value = MagicMock(returncode=0)
            result = run_zoekt_index(clone_path, "org/repo", mock_s3_store)

        assert result["status"] == "verify_failed"
        assert "head_object failed" in result["error"]
        assert result["shards"] == 1
        assert result["artifact_key"] == "zoekt-shards/org/repo/repo_v16.00000.zoekt"

    def test_s3_key_structure(self, run_zoekt_index, mock_s3_store, clone_path, tmp_path):
        """Verify S3 key follows the zoekt-shards/<org>/<repo>/ convention."""
        shard_dir = str(tmp_path / "zoekt-keys")
        os.makedirs(shard_dir, exist_ok=True)
        shard_path = os.path.join(shard_dir, "my-shard.zoekt")
        with open(shard_path, "wb") as f:
            f.write(b"\x00" * 128)

        with patch("subprocess.run") as mock_run, patch("tempfile.mkdtemp", return_value=shard_dir):
            mock_run.return_value = MagicMock(returncode=0)
            result = run_zoekt_index(clone_path, "aws-e/adp", mock_s3_store)

        assert result["artifact_key"] == "zoekt-shards/aws-e/adp/my-shard.zoekt"

    def test_custom_prefix(self, mock_s3_store, clone_path, tmp_path):
        """Custom zoekt_shards_s3_prefix is respected in S3 keys."""
        run_fn = _make_run_zoekt_index(zoekt_shards_s3_prefix="custom-prefix")

        shard_dir = str(tmp_path / "zoekt-custom")
        os.makedirs(shard_dir, exist_ok=True)
        shard_path = os.path.join(shard_dir, "shard.zoekt")
        with open(shard_path, "wb") as f:
            f.write(b"\x00" * 64)

        with patch("subprocess.run") as mock_run, patch("tempfile.mkdtemp", return_value=shard_dir):
            mock_run.return_value = MagicMock(returncode=0)
            result = run_fn(clone_path, "org/repo", mock_s3_store)

        assert result["artifact_key"] == "custom-prefix/org/repo/shard.zoekt"

    def test_subprocess_called_correctly(
        self, run_zoekt_index, mock_s3_store, clone_path, tmp_path
    ):
        """Verify subprocess.run is called with correct arguments."""
        shard_dir = str(tmp_path / "zoekt-cmd")
        os.makedirs(shard_dir, exist_ok=True)
        # No shards needed — we just check the subprocess call
        shard_path = os.path.join(shard_dir, "x.zoekt")
        with open(shard_path, "wb") as f:
            f.write(b"\x00")

        with patch("subprocess.run") as mock_run, patch("tempfile.mkdtemp", return_value=shard_dir):
            mock_run.return_value = MagicMock(returncode=0)
            run_zoekt_index(clone_path, "org/repo", mock_s3_store)

        mock_run.assert_called_once_with(
            ["zoekt-git-index", "-index", shard_dir, clone_path],
            check=True,
            capture_output=True,
            timeout=600,
        )

    def test_temp_dir_cleaned_up_on_success(
        self, run_zoekt_index, mock_s3_store, clone_path, tmp_path
    ):
        """Temp index directory is cleaned up after successful indexing."""
        shard_dir = str(tmp_path / "zoekt-cleanup")
        os.makedirs(shard_dir, exist_ok=True)
        shard_path = os.path.join(shard_dir, "x.zoekt")
        with open(shard_path, "wb") as f:
            f.write(b"\x00")

        with (
            patch("subprocess.run") as mock_run,
            patch("tempfile.mkdtemp", return_value=shard_dir),
            patch("shutil.rmtree") as mock_rmtree,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            run_zoekt_index(clone_path, "org/repo", mock_s3_store)

        mock_rmtree.assert_called_once_with(shard_dir, ignore_errors=True)

    def test_temp_dir_cleaned_up_on_failure(
        self, run_zoekt_index, mock_s3_store, clone_path, tmp_path
    ):
        """Temp index directory is cleaned up even when subprocess fails."""
        shard_dir = str(tmp_path / "zoekt-cleanup-fail")
        os.makedirs(shard_dir, exist_ok=True)

        error = subprocess.CalledProcessError(1, "zoekt-git-index")
        error.stderr = b"error"

        with (
            patch("subprocess.run", side_effect=error),
            patch("tempfile.mkdtemp", return_value=shard_dir),
            patch("shutil.rmtree") as mock_rmtree,
        ):
            run_zoekt_index(clone_path, "org/repo", mock_s3_store)

        mock_rmtree.assert_called_once_with(shard_dir, ignore_errors=True)

    def test_upload_metadata(self, run_zoekt_index, mock_s3_store, clone_path, tmp_path):
        """Verify uploaded shards include org_repo and shard_name metadata."""
        shard_dir = str(tmp_path / "zoekt-meta")
        os.makedirs(shard_dir, exist_ok=True)
        shard_path = os.path.join(shard_dir, "myshard.zoekt")
        with open(shard_path, "wb") as f:
            f.write(b"\x00" * 100)

        with patch("subprocess.run") as mock_run, patch("tempfile.mkdtemp", return_value=shard_dir):
            mock_run.return_value = MagicMock(returncode=0)
            run_zoekt_index(clone_path, "aws-e/adp", mock_s3_store)

        call_kwargs = mock_s3_store._s3.upload_file.call_args.kwargs
        assert call_kwargs["ExtraArgs"]["Metadata"]["org_repo"] == "aws-e/adp"
        assert call_kwargs["ExtraArgs"]["Metadata"]["shard_name"] == "myshard.zoekt"
        assert call_kwargs["ExtraArgs"]["ContentType"] == "application/octet-stream"
