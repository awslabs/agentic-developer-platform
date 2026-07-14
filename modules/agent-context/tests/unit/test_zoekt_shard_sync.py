"""Tests for zoekt shard-sync sidecar manifest logic (#3287).

Validates:
  - The zoekt.yaml manifest contains a shard-sync sidecar container
  - The sidecar sync script copies new shards and removes stale ones
  - The initContainer still performs initial population
  - The sync interval is 60s (guards against reload-loop)
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# Path to the zoekt manifest
MANIFEST_PATH = Path(__file__).parent.parent.parent / "manifests" / "zoekt.yaml"


class TestZoektManifestStructure:
    """Validate the zoekt.yaml manifest has the expected sidecar structure."""

    @pytest.fixture(autouse=True)
    def load_manifest(self):
        """Load the manifest content."""
        self.manifest = MANIFEST_PATH.read_text()

    def test_manifest_exists(self):
        """The zoekt manifest file exists."""
        assert MANIFEST_PATH.exists()

    def test_has_shard_sync_container(self):
        """Manifest includes a shard-sync sidecar container."""
        assert "name: shard-sync" in self.manifest

    def test_has_init_container(self):
        """Manifest still has the prepare-index initContainer for initial load."""
        assert "name: prepare-index" in self.manifest

    def test_shard_sync_uses_sleep_60(self):
        """Sidecar sleeps 60s between sync cycles (guards against reload-loop)."""
        assert "sleep 60" in self.manifest

    def test_shard_sync_uses_cp_u(self):
        """Sidecar uses cp -u to only copy newer files (avoids unnecessary writes)."""
        assert "cp -u" in self.manifest

    def test_shard_sync_removes_stale(self):
        """Sidecar removes shards that no longer exist on S3."""
        assert "removing stale shard" in self.manifest

    def test_shard_sync_mounts_s3_readonly(self):
        """Sidecar mounts the S3 volume as read-only."""
        # The sidecar has the same volume mounts as the initContainer
        assert "readOnly: true" in self.manifest

    def test_shard_sync_resource_limits(self):
        """Sidecar has conservative resource limits (no reload-loop risk)."""
        # Should have low CPU/memory limits
        assert 'cpu: "200m"' in self.manifest
        assert 'memory: "128Mi"' in self.manifest

    def test_issue_reference(self):
        """Manifest references the fix issue."""
        assert "#3287" in self.manifest


class TestShardSyncScript:
    """Integration test of the sync script logic using real filesystem operations.

    Simulates the sidecar's behavior: copying .zoekt files from a nested
    S3-like source directory into a flat index directory, and cleaning up
    stale shards.
    """

    @pytest.fixture(autouse=True)
    def setup_dirs(self, tmp_path):
        """Create source (simulated S3 mount) and destination (index) dirs."""
        self.s3_mount = tmp_path / "s3-shards"
        self.index_dir = tmp_path / "index"
        self.s3_mount.mkdir()
        self.index_dir.mkdir()

        # Create nested S3 structure: org/repo/shard.zoekt
        repo1_dir = self.s3_mount / "acme" / "widget"
        repo1_dir.mkdir(parents=True)
        (repo1_dir / "acme_widget_v16.00000.zoekt").write_bytes(b"shard-data-1")
        (repo1_dir / "acme_widget_v16.00001.zoekt").write_bytes(b"shard-data-2")

        repo2_dir = self.s3_mount / "acme" / "gadget"
        repo2_dir.mkdir(parents=True)
        (repo2_dir / "acme_gadget_v16.00000.zoekt").write_bytes(b"shard-data-3")

    def _run_sync_script(self):
        """Run the sync portion of the sidecar script (one cycle)."""
        script = f"""
        # Sync new/updated shards from S3 mount to flat index dir
        find {self.s3_mount} -name "*.zoekt" -exec cp -u {{}} {self.index_dir}/ \\;

        # Remove shards from index that no longer exist on S3
        for f in {self.index_dir}/*.zoekt; do
            [ -f "$f" ] || continue
            basename=$(basename "$f")
            if ! find {self.s3_mount} -name "$basename" -print -quit | grep -q .; then
                rm -f "$f"
            fi
        done
        """
        result = subprocess.run(
            ["sh", "-c", script],
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 0, f"Script failed: {result.stderr.decode()}"

    def test_initial_sync_copies_all_shards(self):
        """First sync cycle copies all shards from nested S3 structure to flat dir."""
        self._run_sync_script()

        index_files = sorted(f.name for f in self.index_dir.iterdir())
        assert index_files == [
            "acme_gadget_v16.00000.zoekt",
            "acme_widget_v16.00000.zoekt",
            "acme_widget_v16.00001.zoekt",
        ]

    def test_new_shard_detected_on_next_cycle(self):
        """A shard added after initial sync is picked up on the next cycle."""
        self._run_sync_script()

        # Simulate a new ingestion adding a shard
        new_repo = self.s3_mount / "acme" / "newrepo"
        new_repo.mkdir(parents=True)
        (new_repo / "acme_newrepo_v16.00000.zoekt").write_bytes(b"new-shard")

        self._run_sync_script()

        index_files = sorted(f.name for f in self.index_dir.iterdir())
        assert "acme_newrepo_v16.00000.zoekt" in index_files
        assert len(index_files) == 4

    def test_stale_shard_removed(self):
        """A shard removed from S3 is cleaned up from the index dir."""
        self._run_sync_script()

        # Simulate a repo being removed from S3
        stale = self.s3_mount / "acme" / "gadget" / "acme_gadget_v16.00000.zoekt"
        stale.unlink()

        self._run_sync_script()

        index_files = sorted(f.name for f in self.index_dir.iterdir())
        assert "acme_gadget_v16.00000.zoekt" not in index_files
        assert len(index_files) == 2

    def test_idempotent_no_change(self):
        """Running sync twice with no S3 changes doesn't modify files."""
        self._run_sync_script()

        # Record mtimes after first sync
        mtimes_before = {f.name: f.stat().st_mtime for f in self.index_dir.iterdir()}

        self._run_sync_script()

        # mtimes should not change (cp -u skips unchanged files)
        mtimes_after = {f.name: f.stat().st_mtime for f in self.index_dir.iterdir()}
        assert mtimes_before == mtimes_after

    def test_updated_shard_replaced(self):
        """A shard with newer content on S3 replaces the old one in index."""
        self._run_sync_script()

        # Read original content
        idx_file = self.index_dir / "acme_widget_v16.00000.zoekt"
        assert idx_file.read_bytes() == b"shard-data-1"

        # Simulate re-indexing: write newer content with newer mtime
        src_file = self.s3_mount / "acme" / "widget" / "acme_widget_v16.00000.zoekt"
        # Ensure the new write has a strictly newer mtime
        import time

        time.sleep(0.05)
        src_file.write_bytes(b"shard-data-1-updated")

        self._run_sync_script()

        assert idx_file.read_bytes() == b"shard-data-1-updated"

    def test_empty_s3_mount_clears_index(self):
        """If all shards are removed from S3, the index dir is cleared."""
        self._run_sync_script()
        assert len(list(self.index_dir.iterdir())) == 3

        # Remove all shards from S3
        shutil.rmtree(self.s3_mount)
        self.s3_mount.mkdir()

        self._run_sync_script()

        index_files = list(self.index_dir.iterdir())
        assert len(index_files) == 0

    def test_non_zoekt_files_ignored(self):
        """Files without .zoekt extension are not synced."""
        (self.s3_mount / "README.md").write_text("not a shard")
        (self.s3_mount / "metadata.json").write_text("{}")

        self._run_sync_script()

        index_files = [f.name for f in self.index_dir.iterdir()]
        assert "README.md" not in index_files
        assert "metadata.json" not in index_files
