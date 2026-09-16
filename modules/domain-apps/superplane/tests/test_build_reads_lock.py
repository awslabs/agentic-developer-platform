"""The build lanes resolve their ref from the lock — Issue #5041 (U2), EPIC #4910.

R2 acc. 3: an unchanged lock yields identical build inputs. The bug class is a build that
resolves a floating ref while a lock file sits beside it looking authoritative — "pinning
exists as a document and not as a mechanism", which the story calls out as failing the
whole requirement rather than a detail of it.

Two complementary things are checked:

  * the **resolver** is deterministic and refuses anything that is not digest-addressed —
    exercised by calling it, not by reading it, including against synthetic locks written to
    tmp_path so the failure paths are actually executed rather than assumed;
  * each **workflow** obtains its revision by invoking that resolver, and does not carry a
    ref of its own. A lane with `ref: main` in it would satisfy "a lock file exists" while
    building something else entirely.

Determinism is asserted by resolving twice and comparing, which is the closest an offline
test can get to acc. 3. It establishes that the *inputs* are identical for an unchanged
lock; that identical inputs produce an identical image digest additionally depends on the
build being reproducible, and is part of the deferred live criterion (U2-L2).
"""

from __future__ import annotations

import _release_path  # noqa: F401

import re
from pathlib import Path

import pytest
import yaml
from releases.resolve_lock import (
    EXIT_SOURCE_ACCESS_UNRESOLVED,
    BuildInputs,
    LockError,
    SourceAccessUnresolved,
    load_lock,
    main,
    resolve_build_inputs,
    resolved_digest,
)

MODULE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
LOCK_PATH = MODULE_ROOT / "releases" / "superplane.lock.yaml"

WORKFLOWS = {
    "superplane-api": REPO_ROOT / ".github/workflows/superplane-api-build.yml",
    "superplane-controller": REPO_ROOT
    / ".github/workflows/superplane-controller-build.yml",
    "superplane-platform-monitor": REPO_ROOT
    / ".github/workflows/superplane-monitor-build.yml",
}

RESOLVER_REL_PATH = "modules/domain-apps/superplane/releases/resolve_lock.py"


def _resolved_lock(tmp_path: Path, **overrides: object) -> Path:
    """A copy of the real lock with source_access marked resolved.

    Needed because every success path in ``resolve_build_inputs`` is gated behind the
    unresolved grant. Testing only the current state would leave the code that runs *after*
    the grant lands completely unexercised — and that is the code a future operator depends
    on working the day they get access.
    """
    data = yaml.safe_load(LOCK_PATH.read_text(encoding="utf-8"))
    data["source_access"]["status"] = "resolved"
    data.update(overrides)
    path = tmp_path / "superplane.lock.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


class TestResolverIsDeterministic:
    def test_resolving_twice_yields_identical_inputs(self, tmp_path: Path) -> None:
        """R2 acc. 3, to the extent an offline test can establish it."""
        lock = _resolved_lock(tmp_path)
        first = resolve_build_inputs("superplane-api", lock)
        second = resolve_build_inputs("superplane-api", lock)
        assert first == second

    def test_resolved_inputs_carry_the_pinned_revision(self, tmp_path: Path) -> None:
        inputs = resolve_build_inputs("superplane-api", _resolved_lock(tmp_path))
        assert inputs.upstream_revision == load_lock(LOCK_PATH)["upstream"]["revision"]
        assert re.fullmatch(r"[0-9a-f]{40}", inputs.upstream_revision)

    def test_env_lines_expose_the_revision_not_a_branch(self, tmp_path: Path) -> None:
        rendered = resolve_build_inputs(
            "superplane-api", _resolved_lock(tmp_path)
        ).as_env_lines()
        assert (
            f"UPSTREAM_REVISION={load_lock(LOCK_PATH)['upstream']['revision']}"
            in rendered
        )
        for floating in ("main", "master", "HEAD", "latest"):
            assert f"UPSTREAM_REVISION={floating}" not in rendered

    def test_each_image_resolves_to_its_own_ecr_repository(
        self, tmp_path: Path
    ) -> None:
        lock = _resolved_lock(tmp_path)
        repos = {
            name: resolve_build_inputs(name, lock).ecr_repository for name in WORKFLOWS
        }
        assert len(set(repos.values())) == len(repos), (
            f"two images share an ECR repository: {repos}"
        )

    def test_all_three_images_resolve(self, tmp_path: Path) -> None:
        lock = _resolved_lock(tmp_path)
        for name in WORKFLOWS:
            assert isinstance(resolve_build_inputs(name, lock), BuildInputs)


class TestResolverFailsClosed:
    """A lane that cannot build must say why, in one place, with a distinct exit code."""

    def test_unresolved_source_access_raises(self) -> None:
        with pytest.raises(SourceAccessUnresolved) as exc:
            resolve_build_inputs("superplane-api", LOCK_PATH)
        assert "unresolved" in str(exc.value)

    def test_the_error_names_the_candidate_mechanisms(self) -> None:
        """An operator reading the failure should learn what to obtain."""
        with pytest.raises(SourceAccessUnresolved) as exc:
            resolve_build_inputs("superplane-api", LOCK_PATH)
        message = str(exc.value)
        assert "machine identity" in message
        assert "checkout" in message, (
            "the error should say why the obvious approach is ruled out"
        )

    def test_cli_exit_code_distinguishes_blocked_from_broken(self) -> None:
        """78 (blocked by a missing grant) must not be confused with 1 (unusable lock)."""
        assert (
            main(["resolve_lock.py", "superplane-api"]) == EXIT_SOURCE_ACCESS_UNRESOLVED
        )

    def test_cli_rejects_wrong_arity(self) -> None:
        assert main(["resolve_lock.py"]) == 1

    def test_cli_returns_one_for_an_unknown_image(self) -> None:
        """A bad argument is exit 1, distinct from the 78 that means "blocked"."""
        assert main(["resolve_lock.py", "no-such-image"]) == 1

    def test_cli_prints_env_lines_when_the_grant_is_resolved(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """The success path a future operator depends on the day access lands.

        Without this, every line that runs *after* the grant is resolved would be
        untested, and the first real build would be its own first test.
        """
        import releases.resolve_lock as module

        monkeypatch.setattr(module, "LOCK_PATH", _resolved_lock(tmp_path))
        assert main(["resolve_lock.py", "superplane-api"]) == 0
        out = capsys.readouterr().out
        assert (
            f"UPSTREAM_REVISION={load_lock(LOCK_PATH)['upstream']['revision']}" in out
        )
        assert "ECR_REPO=adp-superplane-api" in out

    def test_unknown_image_raises(self, tmp_path: Path) -> None:
        with pytest.raises(LockError, match="not a buildable image"):
            resolve_build_inputs("no-such-image", _resolved_lock(tmp_path))

    def test_pending_entry_with_a_digest_raises(self, tmp_path: Path) -> None:
        """The two-map invariant is enforced in code, not only in the lock's tests."""
        data = yaml.safe_load(LOCK_PATH.read_text(encoding="utf-8"))
        data["source_access"]["status"] = "resolved"
        data["pending_images"]["superplane-api"]["digest"] = "sha256:" + "0" * 64
        path = tmp_path / "lock.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        with pytest.raises(LockError, match="carries a digest"):
            resolve_build_inputs("superplane-api", path)

    def test_missing_lock_raises(self, tmp_path: Path) -> None:
        with pytest.raises(LockError, match="cannot read"):
            load_lock(tmp_path / "absent.yaml")

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.yaml"
        path.write_text("images: [unclosed\n", encoding="utf-8")
        with pytest.raises(LockError, match="not valid YAML"):
            load_lock(path)

    def test_non_mapping_lock_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.yaml"
        path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(LockError, match="must contain a mapping"):
            load_lock(path)

    def test_lock_without_a_revision_raises(self, tmp_path: Path) -> None:
        """A lock with nothing to pin to must not silently produce a build."""
        path = tmp_path / "lock.yaml"
        path.write_text(
            yaml.safe_dump({"images": {}, "upstream": {"repository": "x"}}),
            encoding="utf-8",
        )
        with pytest.raises(LockError, match="no upstream.revision"):
            load_lock(path)

    def test_a_tag_in_the_images_map_is_rejected(self, tmp_path: Path) -> None:
        """The resolver refuses to hand a tag to a deploy as though it were a pin."""
        path = tmp_path / "lock.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "upstream": {"revision": "a" * 40},
                    "images": {"skypilot-api": "0.12.0"},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(LockError, match="not a sha256: digest"):
            resolved_digest("skypilot-api", path)

    def test_absent_image_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "lock.yaml"
        path.write_text(
            yaml.safe_dump({"upstream": {"revision": "a" * 40}, "images": {}}),
            encoding="utf-8",
        )
        with pytest.raises(LockError, match="not in the lock file"):
            resolved_digest("skypilot-api", path)


@pytest.mark.parametrize("image,workflow", sorted(WORKFLOWS.items()))
class TestWorkflowsReadTheLock:
    def test_workflow_exists(self, image: str, workflow: Path) -> None:
        assert workflow.is_file(), f"{image} has no build workflow at {workflow}"

    def test_workflow_invokes_the_resolver(self, image: str, workflow: Path) -> None:
        text = workflow.read_text(encoding="utf-8")
        assert RESOLVER_REL_PATH in text, (
            f"{workflow.name} does not invoke the lock resolver"
        )

    def test_workflow_declares_the_lock_image_it_builds(
        self, image: str, workflow: Path
    ) -> None:
        parsed = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        assert parsed["env"]["LOCK_IMAGE"] == image

    def test_workflow_is_triggered_by_the_lock_file(
        self, image: str, workflow: Path
    ) -> None:
        """Changing the pinned revision is the event that should cause a rebuild."""
        parsed = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        # PyYAML parses the bare key `on:` as the boolean True.
        triggers = parsed.get("on") or parsed.get(True)
        paths = triggers["push"]["paths"]
        assert any("superplane.lock.yaml" in p for p in paths), (
            f"{workflow.name} is not triggered by the lock"
        )

    def test_workflow_carries_no_ref_of_its_own(
        self, image: str, workflow: Path
    ) -> None:
        """A `ref:` here would let the lane build something the lock does not name."""
        for line in workflow.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert not re.match(r"^ref:\s*\S", stripped), (
                f"{workflow.name} pins its own ref: {stripped!r}"
            )

    def test_workflow_tags_the_image_with_the_pinned_revision(
        self, image: str, workflow: Path
    ) -> None:
        """The image tag should identify what was built, not which ADP commit triggered it."""
        text = workflow.read_text(encoding="utf-8")
        assert "IMAGE_TAG,value=${{ env.UPSTREAM_REVISION }}" in text, (
            f"{workflow.name} does not tag by upstream revision"
        )
        assert "IMAGE_TAG,value=${{ github.sha }}" not in text, (
            f"{workflow.name} tags by ADP commit sha"
        )
