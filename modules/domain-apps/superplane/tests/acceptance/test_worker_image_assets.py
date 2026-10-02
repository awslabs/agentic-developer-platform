"""Offline module packaging checks for explicit Superplane asset staging.

The core agent image stages only core assets. These tests opt in to the
Superplane source tree to verify its personas, skills, and collision safety
without building an image or requiring cloud credentials.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# tests/acceptance/[0] tests/[1] superplane/[2] domain-apps/[3] modules/[4] root/[5]
_REPO_ROOT = Path(__file__).resolve().parents[5]

_WORKER_IMAGE_DIR = _REPO_ROOT / "modules" / "agent-factory" / "agent-worker-image"
_STAGE_SCRIPT = _WORKER_IMAGE_DIR / "stage-personas.sh"
_DOCKERFILE = _WORKER_IMAGE_DIR / "Dockerfile"
_WORKER_IMAGE_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "agent-worker-image.yml"

_SUPERPLANE_AGENT = _REPO_ROOT / "modules" / "domain-apps" / "superplane" / "agent"
_CORE_PERSONAS = _REPO_ROOT / "modules" / "agent-factory" / "rules" / "personas"
_CORE_SKILLS = _REPO_ROOT / "modules" / "agent-factory" / "skills"
_DOMAIN_APPS = _REPO_ROOT / "modules" / "domain-apps"

# The assets this unit ships. Named explicitly rather than globbed from the source
# directory: a glob would make this test tautological — it would assert that whatever
# happens to be in the source tree is in the explicit module stage, and would pass unchanged if a
# persona were deleted.
_EXPECTED_PERSONAS = ("superplane-operator", "superplane-researcher")
_EXPECTED_SKILLS = ("superplane", "skypilot")


@pytest.fixture(scope="module")
def staged_tree(tmp_path_factory) -> Path:
    """Run the real staging script over the real source tree.

    Explicitly stage this module alongside the core inputs. The core image does
    not copy this module. Scoped so the staging script runs once.
    """
    if shutil.which("bash") is None:  # pragma: no cover - bash is present in CI
        pytest.skip("bash unavailable")

    source = tmp_path_factory.mktemp("source")
    stage = tmp_path_factory.mktemp("stage")

    shutil.copytree(_CORE_PERSONAS, source / "agent-factory" / "personas")
    shutil.copytree(_CORE_SKILLS, source / "agent-factory" / "skills")
    shutil.copytree(_SUPERPLANE_AGENT, source / "domain-apps" / "superplane" / "agent")

    result = subprocess.run(
        ["bash", str(_STAGE_SCRIPT), str(source), str(stage)],
        capture_output=True,
        text=True,
        timeout=120,
        # Handled explicitly below so the failure message carries the script's
        # stdout/stderr — a bare CalledProcessError would hide why staging failed.
        check=False,
    )
    assert result.returncode == 0, (
        f"stage-personas.sh failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return stage


def test_staging_produced_a_populated_tree(staged_tree: Path) -> None:
    """Guard against a vacuous pass.

    Every assertion below is about presence. If staging silently produced nothing,
    they would all fail loudly — but the *override* tests would pass trivially, so
    the tree's sanity is asserted first.
    """
    personas = list((staged_tree / "personas").glob("*.md"))
    skills = [d for d in (staged_tree / "skills").iterdir() if d.is_dir()]
    assert len(personas) >= 10, f"only {len(personas)} personas staged"
    assert len(skills) >= 2, f"only {len(skills)} skills staged"


@pytest.mark.parametrize("persona", _EXPECTED_PERSONAS)
def test_superplane_persona_is_staged_when_selected(
    staged_tree: Path, persona: str
) -> None:
    """The selected module persona reaches the flat staging namespace."""
    staged = staged_tree / "personas" / f"{persona}.md"
    assert staged.is_file(), (
        f"{persona}.md did not reach the staged persona tree. `stage-personas.sh` "
        f"globs `<domain>/agent/personas/*.md` — a file in a subdirectory is "
        f"silently dropped."
    )
    # Non-empty and the expected persona, not a stray file of the same name.
    text = staged.read_text(encoding="utf-8")
    assert f"@agent-{persona}" in text, (
        f"{persona}.md is staged but does not identify itself as @agent-{persona}"
    )


@pytest.mark.parametrize("skill", _EXPECTED_SKILLS)
def test_superplane_skill_is_staged_when_selected(
    staged_tree: Path, skill: str
) -> None:
    """The selected module skill reaches staging with its SKILL.md."""
    staged = staged_tree / "skills" / skill
    assert staged.is_dir(), f"skill {skill!r} did not reach the staged skill tree"
    skill_md = staged / "SKILL.md"
    assert skill_md.is_file(), f"{skill}/SKILL.md missing from the staged tree"
    body = skill_md.read_text(encoding="utf-8")
    assert body.startswith("---"), f"{skill}/SKILL.md has no YAML frontmatter"
    assert f"name: {skill}" in body, (
        f"{skill}/SKILL.md frontmatter name does not match its directory name — "
        f"the loader keys on the frontmatter, so they must agree"
    )


def test_staged_skypilot_task_builder_runs_without_source_tree_imports(
    staged_tree: Path, tmp_path: Path
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(staged_tree / "skills/skypilot/scripts/capacity_task.py"),
            "--name",
            "issue-123",
            "--gpu",
            "H100:1",
            "--nodes",
            "1",
            "--disk-gb",
            "100",
            "--hold-seconds",
            "600",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    request = json.loads(result.stdout)
    assert request["resources"]["accelerators"] == {"H100": 1}
    assert "instance_type" not in request["resources"]
    assert "cloud" not in request["resources"]
    assert (staged_tree / "skills/skypilot/references/eks-hybrid.md").is_file()


def test_staged_superplane_persona_content_is_the_domain_pack_version(
    staged_tree: Path,
) -> None:
    """The staged bytes are the domain pack's file, not something else's.

    Byte-comparison rather than a substring check: this is what catches a same-named
    file from another source root winning the flat copy.
    """
    for persona in _EXPECTED_PERSONAS:
        source = _SUPERPLANE_AGENT / "personas" / f"{persona}.md"
        staged = staged_tree / "personas" / f"{persona}.md"
        assert staged.read_bytes() == source.read_bytes(), (
            f"staged {persona}.md differs from "
            f"modules/domain-apps/superplane/agent/personas/{persona}.md — another "
            f"source root overwrote it during staging"
        )


def test_no_core_persona_was_replaced_by_the_superplane_pack(staged_tree: Path) -> None:
    """Adding this pack must not have displaced any core persona.

    The concrete hazard: domain personas are copied flat and last, so a domain
    `developer.md` would overwrite ADP's core `developer` persona in the image with
    no error and no log line — changing the behaviour of every agent run on the
    platform. Asserted against the built tree, byte for byte, because that is where
    the overwrite would happen; the source-tree filename collision test in
    webhook-ingress catches the same hazard earlier.
    """
    for core in sorted(_CORE_PERSONAS.glob("*.md")):
        staged = staged_tree / "personas" / core.name
        assert staged.is_file(), (
            f"core persona {core.name} vanished from the staged tree"
        )
        assert staged.read_bytes() == core.read_bytes(), (
            f"core persona {core.name} was overwritten during staging by a "
            f"same-named domain persona. Namespace the domain filename."
        )


def test_no_core_skill_was_replaced_by_the_superplane_pack(staged_tree: Path) -> None:
    """Same override hazard, for skills.

    `stage-personas.sh` `rm -rf`s the target before copying a domain skill, so a
    name collision does not merge — it replaces the core skill wholesale.
    """
    for core in sorted(d for d in _CORE_SKILLS.iterdir() if d.is_dir()):
        staged = staged_tree / "skills" / core.name
        assert staged.is_dir(), f"core skill {core.name} vanished from the staged tree"
        core_md = core / "SKILL.md"
        if core_md.is_file():
            assert (staged / "SKILL.md").read_bytes() == core_md.read_bytes(), (
                f"core skill {core.name} was replaced during staging by a "
                f"same-named domain skill"
            )


def test_superplane_skill_does_not_collide_with_another_domain_pack() -> None:
    """No two domain packs may ship the same skill directory name.

    Whichever domain sorts later wins the `rm -rf` + copy, so the effective skill
    would depend on directory iteration order.
    """
    owners: dict[str, list[str]] = {}
    for domain in sorted(d for d in _DOMAIN_APPS.iterdir() if d.is_dir()):
        skills_dir = domain / "agent" / "skills"
        if not skills_dir.is_dir():
            continue
        for skill in sorted(d for d in skills_dir.iterdir() if d.is_dir()):
            owners.setdefault(skill.name, []).append(domain.name)
    collisions = {name: doms for name, doms in owners.items() if len(doms) > 1}
    assert not collisions, f"domain packs ship colliding skill names: {collisions}"


# ---------------------------------------------------------------------------
# Core image boundaries and explicit module staging.
# ---------------------------------------------------------------------------


def test_dockerfile_copies_the_staged_tree_into_the_image() -> None:
    """Core staging still reaches the runtime image's persona and skill paths."""
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    for source, dest in (
        ("/stage/personas/", "/app/personas/"),
        ("/stage/skills/", "/app/skills/"),
    ):
        pattern = re.compile(
            rf"^COPY\s+--from=stager\b[^\n]*{re.escape(source)}\s+{re.escape(dest)}",
            re.MULTILINE,
        )
        assert pattern.search(dockerfile), (
            f"Dockerfile has no `COPY --from=stager ... {source} {dest}` line — the "
            f"staged assets would not reach the image, and every other assertion in "
            f"this module would still pass."
        )


def test_core_image_stager_does_not_consume_domain_apps() -> None:
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    assert not re.search(r"^COPY\s+modules/domain-apps/", dockerfile, re.MULTILINE)
    assert re.search(
        r"^RUN\s+/stage/stage-personas\.sh\s+/source\s+/stage", dockerfile, re.MULTILINE
    )


def test_staging_without_the_optional_module_excludes_its_assets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    shutil.copytree(_CORE_PERSONAS, source / "agent-factory" / "personas")
    shutil.copytree(_CORE_SKILLS, source / "agent-factory" / "skills")
    stage = tmp_path / "stage"
    subprocess.run(["bash", str(_STAGE_SCRIPT), str(source), str(stage)], check=True)
    for persona in _EXPECTED_PERSONAS:
        assert not (stage / "personas" / f"{persona}.md").exists()
    for skill in _EXPECTED_SKILLS:
        assert not (stage / "skills" / skill).exists()


def test_module_assets_do_not_trigger_core_image_build() -> None:
    workflow = _WORKER_IMAGE_WORKFLOW.read_text(encoding="utf-8")
    assert "modules/domain-apps/*/agent/**" not in workflow
