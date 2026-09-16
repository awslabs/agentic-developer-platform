"""Inventory tests (#5156).

Proves the write-ahead inventory survives the failure modes the issue names:
crash before create, crash after create, foreign inventory, path traversal and
stale ownership. Network-free.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from tests.e2e.orchestration.inventory import (
    CREATED,
    DELETED,
    INVENTORY_VERSION,
    PLANNED,
    RECONCILE_FAILED,
    FixtureRecord,
    ForeignInventoryError,
    Inventory,
    InventoryError,
    inventory_path,
    new_qualification_id,
    restore_inventory,
    sanitize_evidence,
    verify_ownership,
)

QUAL_ID = "q-0123456789abcdef"
OTHER_QUAL_ID = "q-fedcba9876543210"
TAGS = {
    "adp:qualification-id": QUAL_ID,
    "adp:qualification-environment": "dev",
    "adp:managed-by": "tests.e2e.orchestration",
}


@pytest.fixture
def inventory(artifact_dir) -> Inventory:
    return Inventory.create(artifact_dir, QUAL_ID, "dev")


def _plan(inv: Inventory, fixture_id: str = "org", kind: str = "organization") -> FixtureRecord:
    return inv.record_planned(
        fixture_id=fixture_id,
        kind=kind,
        intended_identity=f"qual-{fixture_id}",
        ownership_tags=TAGS,
        idempotency_token=f"{QUAL_ID}-{fixture_id}",
    )


class TestQualificationId:
    def test_minted_ids_are_unique(self):
        """Two runs must never share an inventory."""
        assert len({new_qualification_id() for _ in range(200)}) == 200

    def test_minted_id_is_accepted_as_a_path(self, artifact_dir):
        inventory_path(artifact_dir, new_qualification_id())


class TestPathTraversal:
    @pytest.mark.parametrize(
        "malicious",
        [
            "../../../etc/adp",
            "..",
            "q-../../escape",
            "/absolute/path",
            "q-abc/../../../tmp",
            "q-abc\x00cut",
            "",
            "q-short",
        ],
    )
    def test_traversal_or_malformed_id_is_refused(self, artifact_dir, malicious):
        """A crafted id must not write outside the artifact directory."""
        with pytest.raises(InventoryError):
            inventory_path(artifact_dir, malicious)

    def test_resolved_path_stays_under_the_artifact_root(self, artifact_dir):
        path = inventory_path(artifact_dir, QUAL_ID)
        assert path.is_relative_to(artifact_dir.resolve())

    def test_traversal_is_refused_on_create_and_load(self, artifact_dir):
        with pytest.raises(InventoryError):
            Inventory.create(artifact_dir, "../escape", "dev")
        with pytest.raises(InventoryError):
            Inventory.load(artifact_dir, "../escape", "dev")


class TestWriteAheadOrdering:
    def test_planned_entry_is_on_disk_before_any_resource_exists(self, inventory, artifact_dir):
        """The intent must be durable BEFORE the provider is called.

        This is what makes a crash-before-create recoverable: the entry names a
        resource that may or may not exist, so resume knows to go and ask.
        """
        _plan(inventory)
        document = json.loads(inventory.path.read_text())
        entry = document["fixtures"][0]
        assert entry["state"] == PLANNED
        assert entry["observed_resource_id"] is None
        assert entry["intended_identity"] == "qual-org"
        assert entry["idempotency_token"] == f"{QUAL_ID}-org"

    def test_observed_resource_id_is_recorded_after_create(self, inventory):
        _plan(inventory)
        inventory.mark_created("org", "org-real-123")
        reloaded = Inventory.load(inventory.path.parent.parent, QUAL_ID, "dev")
        assert reloaded.get("org").state == CREATED
        assert reloaded.get("org").observed_resource_id == "org-real-123"

    def test_inventory_is_created_before_the_first_fixture(self, artifact_dir):
        """An empty inventory exists from the start, so a crash always has a file."""
        inv = Inventory.create(artifact_dir, QUAL_ID, "dev")
        assert inv.path.is_file()
        assert json.loads(inv.path.read_text())["fixtures"] == []

    def test_duplicate_fixture_id_is_refused(self, inventory):
        """Two entries for one fixture would make ownership ambiguous."""
        _plan(inventory)
        with pytest.raises(InventoryError, match="already recorded"):
            _plan(inventory)

    def test_marking_created_without_a_resource_id_is_refused(self, inventory):
        """An empty id would look created while naming nothing."""
        _plan(inventory)
        with pytest.raises(InventoryError, match="without a resource id"):
            inventory.mark_created("org", "")

    def test_unknown_fixture_cannot_be_updated(self, inventory):
        with pytest.raises(InventoryError, match="not in inventory"):
            inventory.mark_created("ghost", "x-1")


class TestCrashSimulation:
    def test_crash_before_create_leaves_a_planned_intent(self, artifact_dir):
        """Simulates dying between the inventory write and the provider call.

        The next process must find the intent and treat it as unresolved.
        """
        inv = Inventory.create(artifact_dir, QUAL_ID, "dev")
        _plan(inv)
        del inv  # the process dies here; nothing was created

        recovered = Inventory.load(artifact_dir, QUAL_ID, "dev")
        assert [r.fixture_id for r in recovered.unresolved] == ["org"]
        assert recovered.get("org").state == PLANNED
        assert recovered.get("org").observed_resource_id is None

    def test_crash_after_create_is_indistinguishable_and_still_unresolved(self, artifact_dir):
        """Dying AFTER the provider created the resource looks the same on disk.

        That is precisely why resume must ask the provider rather than assume:
        this entry may correspond to a real, leaked resource.
        """
        inv = Inventory.create(artifact_dir, QUAL_ID, "dev")
        _plan(inv)
        # Provider created the resource; the process dies before mark_created.
        del inv

        recovered = Inventory.load(artifact_dir, QUAL_ID, "dev")
        record = recovered.get("org")
        assert record.state == PLANNED
        assert record in recovered.unresolved
        # The token survives, so a retry can be deduplicated by the provider.
        assert record.idempotency_token == f"{QUAL_ID}-org"

    def test_atomic_write_survives_an_interrupted_flush(self, inventory, monkeypatch):
        """A crash mid-write must leave the PREVIOUS inventory, not a stub.

        A truncated inventory would strand real resources with no record.
        """
        _plan(inventory)
        inventory.mark_created("org", "org-real-123")
        good = inventory.path.read_text()

        def boom(*args, **kwargs):
            raise KeyboardInterrupt("interrupted mid-flush")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(KeyboardInterrupt):
            _plan(inventory, "team", "team")

        # The file is byte-identical to before the failed write.
        assert inventory.path.read_text() == good
        assert json.loads(inventory.path.read_text())["fixtures"][0]["observed_resource_id"] == "org-real-123"

    def test_no_temp_files_are_left_behind_after_a_failed_flush(self, inventory, monkeypatch):
        _plan(inventory)
        monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError):
            _plan(inventory, "team", "team")
        leftovers = [p.name for p in inventory.path.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_interrupted_cleanup_keeps_an_accurate_record(self, artifact_dir):
        """Each delete is flushed, so an interruption never loses track.

        Fixture A is recorded deleted; B is still created and gets cleaned up
        on the next pass rather than being forgotten.
        """
        inv = Inventory.create(artifact_dir, QUAL_ID, "dev")
        for name in ("org", "team"):
            _plan(inv, name, name)
            inv.mark_created(name, f"{name}-real")
        inv.mark_deleted("org", detail="ownership verified before delete")
        del inv  # interrupted after the first delete

        recovered = Inventory.load(artifact_dir, QUAL_ID, "dev")
        assert [r.fixture_id for r in recovered.unresolved] == ["team"]
        assert recovered.live_resource_count() == 1


class TestForeignInventory:
    def test_inventory_from_another_tool_is_refused(self, inventory):
        """Only records this harness wrote may be acted on."""
        document = json.loads(inventory.path.read_text())
        document["managed_by"] = "somebody-elses-script"
        inventory.path.write_text(json.dumps(document))
        with pytest.raises(ForeignInventoryError, match="managed by"):
            Inventory.load(inventory.path.parent.parent, QUAL_ID, "dev")

    def test_inventory_recording_another_qualification_is_refused(self, inventory):
        document = json.loads(inventory.path.read_text())
        document["qualification_id"] = OTHER_QUAL_ID
        inventory.path.write_text(json.dumps(document))
        with pytest.raises(ForeignInventoryError, match="records qualification"):
            Inventory.load(inventory.path.parent.parent, QUAL_ID, "dev")

    def test_inventory_from_another_environment_is_refused(self, inventory):
        """Its resource ids name another account's resources."""
        with pytest.raises(ForeignInventoryError, match="another environment"):
            Inventory.load(inventory.path.parent.parent, QUAL_ID, "staging")

    def test_unknown_inventory_version_is_refused(self, inventory):
        """Guessing at another layout could misread which resources exist."""
        document = json.loads(inventory.path.read_text())
        document["inventory_version"] = INVENTORY_VERSION + 1
        inventory.path.write_text(json.dumps(document))
        with pytest.raises(InventoryError, match="version"):
            Inventory.load(inventory.path.parent.parent, QUAL_ID, "dev")

    def test_corrupt_inventory_is_refused_not_reset(self, inventory):
        """Silently starting over would abandon real resources."""
        inventory.path.write_text("{truncated")
        with pytest.raises(InventoryError, match="not valid JSON"):
            Inventory.load(inventory.path.parent.parent, QUAL_ID, "dev")

    def test_entry_with_unknown_state_is_refused(self, inventory):
        document = json.loads(inventory.path.read_text())
        document["fixtures"] = [
            {
                "fixture_id": "org",
                "kind": "organization",
                "intended_identity": "qual-org",
                "state": "who-knows",
            }
        ]
        inventory.path.write_text(json.dumps(document))
        with pytest.raises(InventoryError, match="unknown state"):
            Inventory.load(inventory.path.parent.parent, QUAL_ID, "dev")

    def test_missing_inventory_is_reported_clearly(self, artifact_dir):
        with pytest.raises(InventoryError, match="no inventory"):
            Inventory.load(artifact_dir, QUAL_ID, "dev")

    def test_creating_over_an_existing_inventory_is_refused(self, inventory, artifact_dir):
        """Starting over would orphan whatever the first run created."""
        with pytest.raises(InventoryError, match="already exists"):
            Inventory.create(artifact_dir, QUAL_ID, "dev")


class TestOwnershipVerification:
    def test_matching_tags_prove_ownership(self):
        record = FixtureRecord("org", "organization", "qual-org", CREATED, TAGS, observed_resource_id="org-1")
        owned, _ = verify_ownership(record, dict(TAGS), TAGS)
        assert owned is True

    def test_unreadable_tags_do_not_prove_ownership(self):
        """A failed tag read must never authorize a delete."""
        record = FixtureRecord("org", "organization", "qual-org", CREATED, TAGS, observed_resource_id="org-1")
        owned, reason = verify_ownership(record, None, TAGS)
        assert owned is False
        assert "could not be read" in reason

    def test_untagged_resource_does_not_prove_ownership(self):
        record = FixtureRecord("org", "organization", "qual-org", CREATED, TAGS, observed_resource_id="org-1")
        owned, reason = verify_ownership(record, {}, TAGS)
        assert owned is False
        assert "no tags" in reason

    def test_stale_ownership_from_another_qualification_is_refused(self):
        """A recycled identity from an older run must not be deleted.

        The resource carries a real, valid-looking tag set — just a different
        qualification id. Deleting it would destroy another run's fixture.
        """
        stale = dict(TAGS, **{"adp:qualification-id": OTHER_QUAL_ID})
        record = FixtureRecord("org", "organization", "qual-org", CREATED, TAGS, observed_resource_id="org-1")
        owned, reason = verify_ownership(record, stale, TAGS)
        assert owned is False
        assert "mismatch" in reason
        assert OTHER_QUAL_ID in reason

    def test_ownership_from_another_environment_is_refused(self):
        other_env = dict(TAGS, **{"adp:qualification-environment": "staging"})
        record = FixtureRecord("org", "organization", "qual-org", CREATED, TAGS, observed_resource_id="org-1")
        owned, _ = verify_ownership(record, other_env, TAGS)
        assert owned is False

    def test_record_without_an_observed_id_is_never_owned(self):
        """Nothing verifiable exists yet, so there is nothing safe to delete."""
        record = FixtureRecord("org", "organization", "qual-org", PLANNED, TAGS)
        owned, reason = verify_ownership(record, dict(TAGS), TAGS)
        assert owned is False
        assert "no observed resource id" in reason


class TestSecretHygiene:
    def test_secret_like_ownership_tag_is_refused(self, inventory):
        """The inventory is an uploaded artifact; a secret there outlives the run."""
        with pytest.raises(InventoryError, match="secret-like"):
            inventory.record_planned(
                fixture_id="org",
                kind="organization",
                intended_identity="qual-org",
                ownership_tags={"api_key": "value"},
            )

    def test_inventory_file_is_owner_readable_only(self, inventory):
        """It names real resources, so it is not world-readable."""
        mode = stat.S_IMODE(inventory.path.stat().st_mode)
        assert mode == 0o600

    def test_sanitized_evidence_drops_the_idempotency_token(self, inventory):
        """Evidence is retained for investigation minus the provider dedupe key."""
        _plan(inventory)
        inventory.mark_created("org", "org-real-123")
        evidence = sanitize_evidence(inventory.fixtures)
        assert evidence[0]["observed_resource_id"] == "org-real-123"
        assert evidence[0]["intended_identity"] == "qual-org"
        assert "idempotency_token" not in evidence[0]


class TestQueries:
    def test_unresolved_covers_planned_and_created_only(self, inventory):
        """Both states may correspond to a live resource; deleted does not."""
        for name in ("a", "b", "c"):
            _plan(inventory, name, "organization")
        inventory.mark_created("b", "b-real")
        inventory.mark_created("c", "c-real")
        inventory.mark_deleted("c")
        assert sorted(r.fixture_id for r in inventory.unresolved) == ["a", "b"]

    def test_reconcile_failed_is_retained_not_dropped(self, inventory):
        """An unresolvable fixture may be a leak, so it stays visible."""
        _plan(inventory)
        inventory.mark_reconcile_failed("org", "provider unreachable")
        assert [r.fixture_id for r in inventory.in_state(RECONCILE_FAILED)] == ["org"]
        reloaded = Inventory.load(inventory.path.parent.parent, QUAL_ID, "dev")
        assert reloaded.get("org").detail == "provider unreachable"

    def test_live_resource_count_tracks_created_fixtures(self, inventory):
        assert inventory.live_resource_count() == 0
        _plan(inventory)
        assert inventory.live_resource_count() == 0  # planned is not yet live
        inventory.mark_created("org", "org-real")
        assert inventory.live_resource_count() == 1
        inventory.mark_deleted("org")
        assert inventory.live_resource_count() == 0


class TestRestoreAcrossSeparateWorkflowRuns:
    """A resume/cleanup dispatch is a DIFFERENT workflow run with an empty
    workspace, so the originating run's inventory is not on disk. Without an
    explicit restore, its fixtures are unreachable and uncleanable — the leak the
    write-ahead inventory exists to prevent.

    These tests simulate that by writing an inventory in one directory (run 1)
    and restoring it into a fresh one (run 2), offline throughout.
    """

    def _completed_run(self, root, qualification_id: str = QUAL_ID, environment: str = "dev"):
        """Stand in for run 1: an inventory with one live fixture."""
        inv = Inventory.create(root, qualification_id, environment)
        inv.record_planned(
            fixture_id="org",
            kind="organization",
            intended_identity="qual-org",
            ownership_tags=dict(TAGS),
            idempotency_token=f"{qualification_id}-org",
        )
        inv.mark_created("org", "org-real-123")
        return inv

    def test_restore_makes_a_previous_runs_inventory_loadable(self, tmp_path):
        """The round trip: run 1's artifact becomes run 2's working inventory."""
        run1 = tmp_path / "run1-artifacts"
        run1.mkdir()
        self._completed_run(run1)

        run2 = tmp_path / "run2-artifacts"
        run2.mkdir()
        # Run 2 starts with nothing: the fixture is unreachable until restored.
        with pytest.raises(InventoryError, match="no inventory"):
            Inventory.load(run2, QUAL_ID, "dev")

        restore_inventory(run2, QUAL_ID, run1, "dev")

        recovered = Inventory.load(run2, QUAL_ID, "dev")
        assert recovered.get("org").observed_resource_id == "org-real-123"
        assert recovered.get("org").state == CREATED

    def test_restore_finds_the_inventory_nested_in_a_downloaded_artifact(self, tmp_path):
        """An unpacked artifact nests the tree; the layout must not be assumed."""
        nested = tmp_path / "download" / "qualification-run-9" / "artifacts"
        nested.mkdir(parents=True)
        self._completed_run(nested)

        run2 = tmp_path / "run2"
        run2.mkdir()
        restore_inventory(run2, QUAL_ID, tmp_path / "download", "dev")

        assert Inventory.load(run2, QUAL_ID, "dev").get("org").observed_resource_id == "org-real-123"

    def test_restore_refuses_a_missing_archive(self, tmp_path):
        """Cleanup must fail loudly, not silently act on an empty inventory."""
        run2 = tmp_path / "run2"
        run2.mkdir()
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(InventoryError, match="must be restored before resume or cleanup"):
            restore_inventory(run2, QUAL_ID, empty, "dev")

    def test_restore_refuses_a_nonexistent_restore_root(self, tmp_path):
        run2 = tmp_path / "run2"
        run2.mkdir()
        with pytest.raises(InventoryError, match="restore root does not exist"):
            restore_inventory(run2, QUAL_ID, tmp_path / "absent", "dev")

    def test_restore_refuses_another_environments_archive(self, tmp_path):
        """Its resource ids name another account's resources."""
        run1 = tmp_path / "run1"
        run1.mkdir()
        self._completed_run(run1, environment="staging")
        run2 = tmp_path / "run2"
        run2.mkdir()

        with pytest.raises(ForeignInventoryError, match="another environment"):
            restore_inventory(run2, QUAL_ID, run1, "dev")
        assert not (run2 / QUAL_ID).exists(), "a refused archive must not land on disk"

    def test_restore_refuses_a_foreign_archive(self, tmp_path):
        """An inventory written by another tool is never adopted."""
        run1 = tmp_path / "run1"
        run1.mkdir()
        inv = self._completed_run(run1)
        document = json.loads(inv.path.read_text())
        document["managed_by"] = "somebody-elses-harness"
        inv.path.write_text(json.dumps(document))

        run2 = tmp_path / "run2"
        run2.mkdir()
        with pytest.raises(ForeignInventoryError):
            restore_inventory(run2, QUAL_ID, run1, "dev")
        assert not (run2 / QUAL_ID).exists()

    def test_restore_refuses_a_mismatched_version(self, tmp_path):
        run1 = tmp_path / "run1"
        run1.mkdir()
        inv = self._completed_run(run1)
        document = json.loads(inv.path.read_text())
        document["inventory_version"] = INVENTORY_VERSION + 1
        inv.path.write_text(json.dumps(document))

        run2 = tmp_path / "run2"
        run2.mkdir()
        with pytest.raises(InventoryError, match="version"):
            restore_inventory(run2, QUAL_ID, run1, "dev")

    def test_restore_refuses_to_overwrite_this_runs_inventory(self, tmp_path):
        """Overwriting could roll back deletions this run already recorded."""
        run1 = tmp_path / "run1"
        run1.mkdir()
        self._completed_run(run1)

        run2 = tmp_path / "run2"
        run2.mkdir()
        current = self._completed_run(run2)
        current.mark_deleted("org", detail="already cleaned up here")

        with pytest.raises(InventoryError, match="refusing to overwrite"):
            restore_inventory(run2, QUAL_ID, run1, "dev")
        # The local record of the deletion survives.
        assert Inventory.load(run2, QUAL_ID, "dev").get("org").state == DELETED

    def test_restore_refuses_an_ambiguous_archive(self, tmp_path):
        """Two inventories for one qualification: refuse rather than pick one."""
        download = tmp_path / "download"
        (download / "a").mkdir(parents=True)
        (download / "b").mkdir(parents=True)
        self._completed_run(download / "a")
        self._completed_run(download / "b")

        run2 = tmp_path / "run2"
        run2.mkdir()
        with pytest.raises(InventoryError, match="refusing to guess"):
            restore_inventory(run2, QUAL_ID, download, "dev")

    def test_restore_ignores_an_archive_for_a_different_qualification(self, tmp_path):
        run1 = tmp_path / "run1"
        run1.mkdir()
        self._completed_run(run1, qualification_id=OTHER_QUAL_ID)

        run2 = tmp_path / "run2"
        run2.mkdir()
        with pytest.raises(InventoryError, match="must be restored"):
            restore_inventory(run2, QUAL_ID, run1, "dev")

    def test_a_restored_inventory_is_written_with_owner_only_permissions(self, tmp_path):
        """It names real resources, so the restored copy keeps 0600 too."""
        run1 = tmp_path / "run1"
        run1.mkdir()
        self._completed_run(run1)
        run2 = tmp_path / "run2"
        run2.mkdir()

        restored = restore_inventory(run2, QUAL_ID, run1, "dev")

        assert stat.S_IMODE(os.stat(restored).st_mode) == 0o600

    def test_a_restored_inventory_can_then_be_cleaned_up(self, tmp_path, valid_config):
        """End to end: restore in run 2, then cleanup deletes the run-1 fixture."""
        from tests.e2e.orchestration.fixtures import cleanup
        from tests.e2e.orchestration.test_fixtures import FakeProvider

        run1 = tmp_path / "run1"
        run1.mkdir()
        self._completed_run(run1)

        run2 = tmp_path / "run2"
        run2.mkdir()
        restore_inventory(run2, QUAL_ID, run1, "dev")

        provider = FakeProvider()
        provider.resources["org-real-123"] = dict(TAGS)
        outcome = cleanup(Inventory.load(run2, QUAL_ID, "dev"), valid_config, {"organization": provider})

        assert outcome.deleted == ("org",)
        assert provider.resources == {}, "the fixture created by run 1 is gone after run 2's cleanup"
