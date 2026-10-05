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

U22 (#5326) changed what the resolver resolves to. The source is now maintained in this
repository, so the success path is the normal path and is exercised against the REAL lock
rather than a synthetic resolved copy. The fail-closed path is still tested — against a
synthetic lock whose ``source_access`` is unresolved — because that behaviour must keep
working for any component added in the not-yet-granted state, and because deleting the test
along with the condition would leave exit 78 unexercised until the day it mattered.
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
MAINTAINED_ROOT = "modules/domain-apps/superplane"


def _unresolved_lock(tmp_path: Path, **overrides: object) -> Path:
    """A copy of the real lock with ``source_access`` pushed back to unresolved.

    The mirror image of the pre-U22 helper. Since the transfer, "resolved" is the real lock's
    state, so the synthetic copy is needed for the *failure* path instead: a component whose
    access grant has not landed must still stop with a named cause and exit 78. Without this,
    that branch would go unexercised until the day someone added such a component, and its
    first real test would be a confusing CI failure.
    """
    data = yaml.safe_load(LOCK_PATH.read_text(encoding="utf-8"))
    data["source_access"]["status"] = "unresolved"
    data["source_access"]["candidate_mechanisms"] = [
        "a machine identity with read access to the origin repository",
        "a mirror of the source inside ADP's control",
    ]
    data.update(overrides)
    path = tmp_path / "superplane.lock.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


class TestResolverIsDeterministic:
    def test_resolving_twice_yields_identical_inputs(self) -> None:
        """R2 acc. 3, to the extent an offline test can establish it."""
        first = resolve_build_inputs("superplane-api", LOCK_PATH)
        second = resolve_build_inputs("superplane-api", LOCK_PATH)
        assert first == second

    def test_resolved_inputs_carry_the_origin_revision_as_provenance(self) -> None:
        """The origin revision is still pinned — as a label, not as a fetch target."""
        inputs = resolve_build_inputs("superplane-api", LOCK_PATH)
        assert inputs.origin_revision == load_lock(LOCK_PATH)["upstream"]["revision"]
        assert re.fullmatch(r"[0-9a-f]{40}", inputs.origin_revision)

    def test_resolved_inputs_point_at_the_maintained_source(self) -> None:
        """The substance of U22: what a build reads is a directory in this repository."""
        inputs = resolve_build_inputs("superplane-api", LOCK_PATH)
        assert inputs.source_path == "src/superplane-api"
        context = REPO_ROOT / MAINTAINED_ROOT / inputs.source_path
        assert context.is_dir(), f"{context} is not in this checkout"
        assert (context / "Dockerfile").is_file(), (
            "the resolved build context has no Dockerfile, so no build could succeed"
        )

    def test_env_lines_expose_a_revision_not_a_branch(self) -> None:
        rendered = resolve_build_inputs("superplane-api", LOCK_PATH).as_env_lines()
        assert (
            f"ORIGIN_REVISION={load_lock(LOCK_PATH)['upstream']['revision']}"
            in rendered
        )
        for floating in ("main", "master", "HEAD", "latest"):
            assert f"ORIGIN_REVISION={floating}" not in rendered

    def test_env_lines_expose_a_build_context_the_lane_can_cd_into(self) -> None:
        """The lane must not have to reassemble the path from the module root by hand.

        Regression guard for a real bug in this story: ``MAINTAINED_ROOT`` briefly ended in
        ``/src`` while the lock's ``source_path`` values already began with ``src/``, so the
        resolver emitted ``.../superplane/src/src/superplane-api`` — a path that exists
        nowhere, and which would have failed inside ``docker build`` rather than here.
        """
        for name in WORKFLOWS:
            rendered = resolve_build_inputs(name, LOCK_PATH).as_env_lines()
            line = next(
                ln
                for ln in rendered.splitlines()
                if ln.startswith("SUPERPLANE_SOURCE_DIR=")
            )
            resolved = line.split("=", 1)[1]
            assert "/src/src/" not in resolved, f"doubled path segment: {resolved!r}"
            assert (REPO_ROOT / resolved).is_dir(), (
                f"{name} resolves to {resolved!r}, which is not a directory"
            )

    def test_each_image_resolves_to_its_own_ecr_repository(self) -> None:
        repos = {
            name: resolve_build_inputs(name, LOCK_PATH).ecr_repository
            for name in WORKFLOWS
        }
        assert len(set(repos.values())) == len(repos), (
            f"two images share an ECR repository: {repos}"
        )

    def test_all_three_images_resolve(self) -> None:
        for name in WORKFLOWS:
            assert isinstance(resolve_build_inputs(name, LOCK_PATH), BuildInputs)


class TestResolverFailsClosed:
    """A lane that cannot build must say why, in one place, with a distinct exit code."""

    def test_unresolved_source_access_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SourceAccessUnresolved) as exc:
            resolve_build_inputs("superplane-api", _unresolved_lock(tmp_path))
        assert "unresolved" in str(exc.value)

    def test_the_error_names_the_candidate_mechanisms(self, tmp_path: Path) -> None:
        """An operator reading the failure should learn what to obtain."""
        with pytest.raises(SourceAccessUnresolved) as exc:
            resolve_build_inputs("superplane-api", _unresolved_lock(tmp_path))
        message = str(exc.value)
        assert "machine identity" in message
        assert "checkout" in message, (
            "the error should say why the obvious approach is ruled out"
        )

    def test_cli_exit_code_distinguishes_blocked_from_broken(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """78 (blocked by a missing grant) must not be confused with 1 (unusable lock)."""
        import releases.resolve_lock as module

        monkeypatch.setattr(module, "LOCK_PATH", _unresolved_lock(tmp_path))
        assert (
            main(["resolve_lock.py", "superplane-api"]) == EXIT_SOURCE_ACCESS_UNRESOLVED
        )

    def test_cli_rejects_wrong_arity(self) -> None:
        assert main(["resolve_lock.py"]) == 1

    def test_cli_returns_one_for_an_unknown_image(self) -> None:
        """A bad argument is exit 1, distinct from the 78 that means "blocked"."""
        assert main(["resolve_lock.py", "no-such-image"]) == 1

    def test_cli_prints_env_lines_for_the_real_lock(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """The success path the lanes take, exercised against the committed lock.

        Before U22 this had to run against a synthetic resolved copy, because the real lock
        stopped at exit 78. Running it against the real one now is the point: it demonstrates
        a clean ADP checkout resolves a buildable context with no upstream access.
        """
        assert main(["resolve_lock.py", "superplane-api"]) == 0
        out = capsys.readouterr().out
        assert f"ORIGIN_REVISION={load_lock(LOCK_PATH)['upstream']['revision']}" in out
        assert f"SUPERPLANE_SOURCE_DIR={MAINTAINED_ROOT}/src/superplane-api" in out
        assert "ECR_REPO=adp-superplane-api" in out

    def test_unknown_image_raises(self) -> None:
        with pytest.raises(LockError, match="not a buildable image"):
            resolve_build_inputs("no-such-image", LOCK_PATH)

    def test_pending_entry_with_a_digest_raises(self, tmp_path: Path) -> None:
        """The two-map invariant is enforced in code, not only in the lock's tests."""
        data = yaml.safe_load(LOCK_PATH.read_text(encoding="utf-8"))
        data["pending_images"]["superplane-platform-monitor"] = data[
            "image_sources"
        ].pop("superplane-platform-monitor")
        data["images"].pop("superplane-platform-monitor")
        data["pending_images"]["superplane-platform-monitor"]["digest"] = (
            "sha256:" + "0" * 64
        )
        path = tmp_path / "lock.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        with pytest.raises(LockError, match="carries a digest"):
            resolve_build_inputs("superplane-platform-monitor", path)

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

    def test_workflow_uses_managed_python_before_installing_pyyaml(
        self, image: str, workflow: Path
    ) -> None:
        """ARC's PEP 668 system Python must not receive workflow dependencies."""
        parsed = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        steps = parsed["jobs"]["build"]["steps"]
        setup_indexes = [
            index
            for index, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("actions/setup-python@")
            and str((step.get("with") or {}).get("python-version", "")) == "3.12"
        ]
        install_indexes = [
            index
            for index, step in enumerate(steps)
            if "pyyaml" in str(step.get("run", "")).lower()
        ]
        assert len(setup_indexes) == 1, (
            f"{workflow.name} must select one managed Python 3.12 interpreter"
        )
        assert len(install_indexes) == 1, (
            f"{workflow.name} must install PyYAML exactly once"
        )
        assert setup_indexes[0] < install_indexes[0], (
            f"{workflow.name} installs PyYAML before selecting managed Python; "
            "the ARC system interpreter rejects this under PEP 668"
        )

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

    def test_workflow_tags_the_image_with_the_adp_commit(
        self, image: str, workflow: Path
    ) -> None:
        """The image tag should identify what was built.

        This inverted with U22. While the source lived upstream, the upstream revision *was*
        the identity of the built artifact and ``github.sha`` was merely the commit that
        triggered the lane — so tagging by ADP commit was the bug. Now that ADP maintains the
        source, the origin revision is frozen at the transfer point: tagging by it would make
        every subsequent fix push a different image under the same tag, quietly destroying the
        ability to say which build is running. The ADP commit is the identity, and the origin
        revision is preserved as an image label instead (releases/build-image.sh).
        """
        text = workflow.read_text(encoding="utf-8")
        assert "IMAGE_TAG,value=${{ github.sha }}" in text, (
            f"{workflow.name} does not tag by the ADP commit that produced the image"
        )
        assert "IMAGE_TAG,value=${{ env.ORIGIN_REVISION }}" not in text, (
            f"{workflow.name} tags by the frozen origin revision, so rebuilds would collide"
        )

    def test_workflow_is_triggered_by_its_maintained_source(
        self, image: str, workflow: Path
    ) -> None:
        """After the transfer, a source change must be able to cause a rebuild.

        Watching only the lock would mean a fix to ADP-maintained code never produced a new
        image — the drift that makes an owned component diverge from what runs in a cluster.
        """
        parsed = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        triggers = parsed.get("on") or parsed.get(True)
        paths = triggers["push"]["paths"]
        expected = f"{MAINTAINED_ROOT}/src/{image}/**"
        assert expected in paths, (
            f"{workflow.name} is not triggered by its own source ({expected!r} missing)"
        )
