"""The release lock is digest-addressed and never a tag — Issue #5041 (U2), EPIC #4910.

The bug class these tests prevent: a lock file that makes a deploy *look* reproducible
while a floating tag moves underneath it. Upstream's own SkyPilot manifests deploy
`berkeleyskypilot/skypilot:latest`, and when this lock was authored `:latest` and `:0.12.0`
resolved to different images — so this is a live failure mode, not a hypothetical one.

The second thing they guard is the two-map invariant. Images whose digest cannot exist yet
live in `pending_images` with NO digest field. If a placeholder digest or a tag ever appears
there, these tests fail: a fabricated `sha256:` is worse than a tag, because it looks
authoritative and would be believed.

The *reason* those three digests cannot exist changed with U22 (#5326) while the invariant
did not. It used to be "building them needs upstream read access ADP does not have"; the
transfer put the source in this repository, so it is now simply "no build has run yet".
Worth stating because a resolved `source_access` invites the assumption that the images
followed, and the whole point of the two maps is that source availability and a verified
digest are separate facts.
"""

from __future__ import annotations

import _release_path  # noqa: F401

import re
from pathlib import Path

import pytest
import yaml
from releases.resolve_lock import LockError, load_lock, resolved_digest

LOCK_PATH = Path(__file__).resolve().parents[1] / "releases" / "superplane.lock.yaml"

# A full 64-hex sha256 digest. Deliberately strict: `sha256:abc` starts with the right
# prefix but is not a digest, and a truncated placeholder is exactly the kind of value that
# would otherwise slip through a `startswith` check.
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# The three images this unit is responsible for building, plus the SkyPilot runtime it
# pins. Named explicitly so that dropping one from the lock fails rather than passing over
# a shorter dict.
EXPECTED_PENDING = {
    "superplane-api",
    "superplane-controller",
    "superplane-platform-monitor",
}


@pytest.fixture(scope="module")
def lock() -> dict:
    return yaml.safe_load(LOCK_PATH.read_text(encoding="utf-8"))


class TestLockParses:
    def test_lock_file_exists_and_parses(self, lock: dict) -> None:
        assert isinstance(lock, dict), "lock must be a YAML mapping"

    def test_loader_accepts_the_real_lock(self) -> None:
        # load_lock applies its own validation, so this asserts the shipped lock
        # satisfies the code path the build lanes actually use.
        assert load_lock(LOCK_PATH)["upstream"]["revision"]


class TestUpstreamRevision:
    def test_names_an_upstream_revision(self, lock: dict) -> None:
        rev = lock["upstream"]["revision"]
        assert re.fullmatch(r"[0-9a-f]{40}", rev), (
            f"revision must be a full 40-hex commit sha, got {rev!r}"
        )

    def test_names_the_upstream_repository(self, lock: dict) -> None:
        assert "AISuperPlane" in lock["upstream"]["repository"]

    def test_revision_matches_the_spike_provenance(self, lock: dict) -> None:
        """The lock and U12's parity fixtures must not drift apart.

        Both record "the pinned upstream revision". If they diverge, one of them is
        describing a different snapshot than the other and any parity claim built on
        the fixtures no longer applies to what the lock builds.
        """
        from spike.provenance import UPSTREAM_REVISION

        assert lock["upstream"]["revision"] == UPSTREAM_REVISION


class TestImagesArePinnedByDigest:
    """R2's core requirement: every resolved image entry is a digest, not a tag."""

    def test_images_map_is_present_and_non_empty(self, lock: dict) -> None:
        assert lock.get("images"), "lock records no resolved images at all"

    def test_every_image_entry_is_a_sha256_digest(self, lock: dict) -> None:
        for name, value in lock["images"].items():
            assert DIGEST_RE.match(str(value)), (
                f"images.{name} = {value!r} is not a sha256: digest"
            )

    def test_no_image_entry_is_a_tag(self, lock: dict) -> None:
        """A tag is the specific thing that must never appear here."""
        for name, value in lock["images"].items():
            v = str(value)
            assert ":latest" not in v, f"images.{name} references a floating tag"
            assert not re.match(r"^\d+\.\d+", v), (
                f"images.{name} = {v!r} looks like a version tag, not a digest"
            )

    def test_skypilot_runtime_is_pinned(self, lock: dict) -> None:
        """No floating SkyPilot release is accepted."""
        assert DIGEST_RE.match(str(lock["images"]["skypilot-api"]))

    def test_every_resolved_image_records_its_source(self, lock: dict) -> None:
        """A digest with no provenance cannot be re-resolved or audited."""
        sources = lock.get("image_sources") or {}
        for name in lock["images"]:
            assert name in sources, f"images.{name} has no image_sources entry"
            entry = sources[name]
            for field in ("registry", "repository", "tag"):
                assert entry.get(field), f"image_sources.{name}.{field} is missing"

    def test_resolved_digest_helper_agrees_with_the_file(self, lock: dict) -> None:
        for name, value in lock["images"].items():
            assert resolved_digest(name, LOCK_PATH) == str(value)


class TestPendingImagesCarryNoDigest:
    """The two-map invariant — the thing that keeps the smoke check honest."""

    def test_the_three_superplane_images_are_accounted_for(self, lock: dict) -> None:
        recorded = set(lock.get("pending_images") or {}) | set(lock.get("images") or {})
        missing = EXPECTED_PENDING - recorded
        assert not missing, f"lock does not account for {sorted(missing)}"

    def test_pending_entries_have_no_digest_field(self, lock: dict) -> None:
        for name, entry in (lock.get("pending_images") or {}).items():
            assert "digest" not in entry, (
                f"pending_images.{name} carries a digest; it must not"
            )

    def test_pending_entries_contain_no_sha256_value_anywhere(self, lock: dict) -> None:
        """Catches a placeholder digest smuggled in under a different key name."""
        for name, entry in (lock.get("pending_images") or {}).items():
            for key, value in entry.items():
                assert "sha256:" not in str(value), (
                    f"pending_images.{name}.{key} contains a sha256: value"
                )

    def test_pending_entries_name_their_blocking_gate(self, lock: dict) -> None:
        for name, entry in (lock.get("pending_images") or {}).items():
            assert entry.get("blocked_by"), (
                f"pending_images.{name} does not say what blocks it"
            )

    def test_pending_entries_name_a_build_workflow_that_exists(
        self, lock: dict
    ) -> None:
        repo_root = Path(__file__).resolve().parents[4]
        for name, entry in (lock.get("pending_images") or {}).items():
            wf = entry.get("build_workflow")
            assert wf, f"pending_images.{name} names no build workflow"
            assert (repo_root / wf).is_file(), (
                f"pending_images.{name} names {wf}, which does not exist"
            )

    def test_an_image_is_never_in_both_maps(self, lock: dict) -> None:
        overlap = set(lock.get("images") or {}) & set(lock.get("pending_images") or {})
        assert not overlap, f"{sorted(overlap)} appear as both resolved and pending"

    def test_resolving_a_pending_image_as_a_digest_fails_loudly(self) -> None:
        with pytest.raises(LockError, match="pending"):
            resolved_digest("superplane-api", LOCK_PATH)


class TestUnresolvedInputsAreRecordedNotInvented:
    """The plan records these unresolved; a plausible value here would be a fabrication."""

    def test_source_access_records_the_transferred_mechanism(self, lock: dict) -> None:
        """U22 (#5326) resolved this by transferring the source, not by inventing a grant.

        This is the one entry in this class that moved from unresolved to resolved, and it
        moved because the underlying fact changed: the source is in this repository now. The
        assertion checks the mechanism is *named* alongside the status, so a future edit
        cannot flip the status to resolved without saying what resolved it — which is exactly
        the fabrication this class exists to prevent.
        """
        access = lock["source_access"]
        assert access["status"] == "resolved"
        assert access["mechanism"], "resolved without naming a mechanism"
        assert access["resolved_by"]["issue"] == 5326

    def test_pending_images_still_carry_no_invented_digest(self, lock: dict) -> None:
        """Resolving source access did NOT make the images exist.

        The distinction the header of the lock is built on: the blocker moved from "ADP cannot
        read the source" to "no build has run yet", and neither one is a digest. Writing a
        plausible `sha256:` here would be the exact fabrication a resolved status might
        tempt someone into.
        """
        for name, entry in (lock["pending_images"] or {}).items():
            assert "digest" not in entry, (
                f"{name} carries a digest before any build ran"
            )
            assert entry["blocked_by"], f"{name} does not say what it is waiting on"

    def test_source_access_rules_out_the_two_forbidden_mechanisms(
        self, lock: dict
    ) -> None:
        ruled_out = " ".join(
            str(r.get("mechanism", "")) for r in lock["source_access"]["ruled_out"]
        ).lower()
        assert "checkout" in ruled_out, (
            "the upstream checkout must be explicitly ruled out"
        )
        assert "snapshot" in ruled_out, (
            "the reference snapshot must be explicitly ruled out"
        )

    def test_deployment_target_is_unresolved(self, lock: dict) -> None:
        assert lock["skypilot_config"]["deployment_target"]["status"] == "unresolved"

    def test_no_aws_account_id_is_invented(self, lock: dict) -> None:
        """Upstream's config.env carries upstream's account id — it must not be adopted.

        The plan records no AWS account for this EPIC, so any 12-digit account id in a
        value position here would be a guess presented as configuration.
        """
        text = LOCK_PATH.read_text(encoding="utf-8")
        values = [
            ln.split(":", 1)[1]
            for ln in text.splitlines()
            if ":" in ln and not ln.strip().startswith("#")
        ]
        for value in values:
            assert not re.search(r"\b\d{12}\b", value), (
                f"a 12-digit account id appears in a value: {value.strip()!r}"
            )


class TestSkypilotConfigurationIsPinned:
    """A pinned runtime with floating configuration is not reproducible."""

    def test_allowed_clouds_are_recorded(self, lock: dict) -> None:
        assert lock["skypilot_config"]["allowed_clouds"], "no allowed_clouds recorded"

    def test_state_backend_is_recorded(self, lock: dict) -> None:
        # postgres rather than the sqlite variant: sqlite loses cluster/job state on pod
        # replacement, which U19's handover work depends on surviving.
        assert lock["skypilot_config"]["state_backend"] == "postgres"

    def test_api_manifest_is_named(self, lock: dict) -> None:
        assert lock["skypilot_config"]["api_manifest"]


class TestSchemaCompatibility:
    def test_schema_compatibility_is_recorded(self, lock: dict) -> None:
        assert "schema" in lock, "lock records no schema compatibility"

    def test_schema_status_is_not_an_unverified_claim(self, lock: dict) -> None:
        """`status` must stay `unverified` even though the chain is now single-headed.

        UPDATED BY U13 (#5045), which repaired the chain: `single_head` flipped to true, so
        this test no longer asserts false for it.

        The important half is unchanged, and the two must not be conflated. `single_head` is
        a property of the FILES and is established offline — one head, one base, no
        duplicate ids, no dangling parent, checked by
        `src/superplane-api/tests/test_migrations.py`. `status: verified` is a property of a
        REAL DATABASE and is not established by any of that: the CI lane is credential-free
        with no PostgreSQL service, and the live smoke check plus R4 acceptance 3 are
        deferred behind an unresolved account, database access and backup target.

        So a single-headed chain whose status is still `unverified` is the correct recorded
        state, and `check_migration_contract.py` still refuses on it.
        """
        schema = lock["schema"]
        assert schema["status"] == "unverified"
        assert schema["single_head"] is True
        assert schema["compatible_with"] is None
        assert schema["blocked_by"]["unit"] == "U13"

    def test_the_chain_the_lock_describes_is_the_chain_on_disk(
        self, lock: dict
    ) -> None:
        """The recorded head/base/file-count must be re-derivable from the tree.

        Added by U13 (#5045). The lock's `observed` block is read by
        `check_migration_contract.py` and rendered into what an operator sees, so a value
        that drifts from the files misdirects whoever is debugging a refusal. Deriving it
        here means the lock cannot silently fall out of step with the chain again.
        """
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        api_root = LOCK_PATH.parents[1] / "src" / "superplane-api"
        config = Config(str(api_root / "alembic.ini"))
        config.set_main_option("script_location", str(api_root / "alembic"))
        script = ScriptDirectory.from_config(config)

        observed = lock["schema"]["observed"]
        assert script.get_heads() == [observed["head"]]
        assert script.get_bases() == [observed["base"]]
        assert observed["version_files"] == len(
            list((api_root / "alembic" / "versions").glob("*.py"))
        )
        assert observed["duplicate_revision_ids"] == {}


class TestStorySmokeCheck:
    def test_the_story_smoke_one_liner_would_exit_zero(self, lock: dict) -> None:
        """The exact predicate from the issue's smoke check."""
        assert all(str(i).startswith("sha256:") for i in lock["images"].values())
