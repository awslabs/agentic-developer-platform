"""Shape + gating-wording guard for the codex-bridge SKILL.md (issue #2705).

The skill's *only* runtime gate against firing autonomously is the wording of
its frontmatter `description` (that's what the SDK Skill tool matches on) plus
the body instructions. This test locks that wording so a future edit can't
silently weaken the human-in-the-loop gate — which is the primary control the
issue's negative/gating test protects.

Frontmatter is parsed without PyYAML (not a runtime dep of this image) — the
file uses a simple `key: >-`/`key: value` block, so a minimal parser suffices.
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_MD = (
    HERE.parent.parent / "skills" / "codex-bridge" / "SKILL.md"
)


def _split_frontmatter(text: str) -> tuple[str, str]:
    assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    _, fm, body = text.split("---\n", 2)
    return fm, body


def _parse_frontmatter(fm: str) -> dict:
    """Parse the minimal `name:` / `description: >-` block used by the skill."""
    result: dict[str, str] = {}
    key = None
    buf: list[str] = []
    for line in fm.splitlines():
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")) and ":" in line:
            if key is not None:
                result[key] = " ".join(buf).strip()
            k, _, rest = line.partition(":")
            key = k.strip()
            rest = rest.strip()
            buf = [] if rest in (">-", "|", ">", "|-", "") else [rest]
        else:
            buf.append(line.strip())
    if key is not None:
        result[key] = " ".join(buf).strip()
    return result


def test_skill_file_exists() -> None:
    assert SKILL_MD.exists(), f"missing {SKILL_MD}"


def test_frontmatter_is_name_and_description_only() -> None:
    fm, _ = _split_frontmatter(SKILL_MD.read_text())
    keys = set(_parse_frontmatter(fm).keys())
    # Agent Skills standard: no tools schema in frontmatter.
    assert keys == {"name", "description"}, f"unexpected frontmatter keys: {keys}"


def test_name_is_codex_bridge() -> None:
    fm, _ = _split_frontmatter(SKILL_MD.read_text())
    assert _parse_frontmatter(fm)["name"] == "codex-bridge"


def test_description_encodes_the_explicit_trigger_gate() -> None:
    fm, _ = _split_frontmatter(SKILL_MD.read_text())
    desc = _parse_frontmatter(fm)["description"].lower()
    # The gate: only when explicitly asked, never autonomously.
    assert "only" in desc
    assert "explicit" in desc
    assert "codex" in desc
    assert "never invoke it autonomously" in desc


def test_body_instructs_review_before_commit() -> None:
    _, body = _split_frontmatter(SKILL_MD.read_text())
    low = body.lower()
    assert "run-codex.sh" in body
    assert "proposal" in low  # output treated as a proposal
    assert "git diff" in low  # reviewer reads the diff before committing
