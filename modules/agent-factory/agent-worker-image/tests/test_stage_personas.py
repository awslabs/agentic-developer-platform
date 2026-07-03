"""Tests for the core-skills staging block added in issue #2705.

stage-personas.sh previously staged skills ONLY from domain-apps. #2705 adds a
core block that stages platform-wide skills from
`<source_root>/agent-factory/skills/*`. These tests build a synthetic source
tree and assert:
  * a core skill lands in `<stage>/skills/<name>`;
  * an existing domain skill still stages (regression);
  * a domain skill of the same name OVERRIDES the core one (precedence).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAGE_SCRIPT = HERE.parent / "stage-personas.sh"


def _make_skill(root: Path, rel: str, name: str, marker: str) -> None:
    d = root / rel / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {marker}\n---\n# {name}\n"
    )


def _run_stage(source_root: Path, stage_root: Path):
    return subprocess.run(
        ["bash", str(STAGE_SCRIPT), str(source_root), str(stage_root)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_core_skill_is_staged(tmp_path: Path) -> None:
    source = tmp_path / "source"
    stage = tmp_path / "stage"
    _make_skill(source, "agent-factory/skills", "codex-bridge", "core-marker")

    result = _run_stage(source, stage)
    assert result.returncode == 0, result.stderr

    staged = stage / "skills" / "codex-bridge" / "SKILL.md"
    assert staged.exists(), result.stdout + result.stderr
    assert "core-marker" in staged.read_text()


def test_domain_skill_still_stages(tmp_path: Path) -> None:
    # Regression: touching the script must not break domain-skill staging.
    source = tmp_path / "source"
    stage = tmp_path / "stage"
    _make_skill(source, "domain-apps/cyber/agent/skills", "stage-1-triage", "domain")

    result = _run_stage(source, stage)
    assert result.returncode == 0, result.stderr
    assert (stage / "skills" / "stage-1-triage" / "SKILL.md").exists()


def test_domain_skill_overrides_core_of_same_name(tmp_path: Path) -> None:
    source = tmp_path / "source"
    stage = tmp_path / "stage"
    # Same skill name in both trees; domain must win (staged last).
    _make_skill(source, "agent-factory/skills", "dup", "core-version")
    _make_skill(source, "domain-apps/foo/agent/skills", "dup", "domain-version")

    result = _run_stage(source, stage)
    assert result.returncode == 0, result.stderr
    staged = stage / "skills" / "dup" / "SKILL.md"
    assert "domain-version" in staged.read_text()
    assert "core-version" not in staged.read_text()
