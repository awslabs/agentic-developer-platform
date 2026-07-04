"""Drift lint for the Codex-distilled persona fileset (issue #2891, spike #2839).

The codex-bridge wrapper renders `rules/personas/codex-distilled/<AGENT_TYPE>.md`
as `AGENTS.md` into the delegation cwd so Codex output is persona-calibrated
(spike design note §3–§4). Because Codex runs with the sandbox bypassed, WHAT
reaches it is security-relevant. These files are hand-authored (NOT extracted
from the full persona at runtime) and must carry ONLY the persona's mindset +
conventions / quality bar — never identity, outer-loop workflow, or
mention/trigger content (§4.2).

This lint pins that contract so a careless edit can't leak the forbidden
material or blow the token budget:
  * a distilled file exists for each shipped persona;
  * each file is <= 2 KB (bounded token cost per delegation);
  * no file contains identity/outer-loop/mention markers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# rules/personas/codex-distilled/ lives at the repo module root, two levels up
# from agent-worker-image/tests/.
DISTILLED_DIR = (
    Path(__file__).resolve().parents[2] / "rules" / "personas" / "codex-distilled"
)

# v1 ships developer / reviewer / operations only (issue Non-goals).
PERSONAS = ["developer", "reviewer", "operations"]

MAX_BYTES = 2048

# Identity / outer-loop / mention markers that must NEVER reach Codex (§4.2).
# Matched case-insensitively.
FORBIDDEN_SUBSTRINGS = [
    "@agent-",
    "adp-trigger",
    "you are",
    "post a plan",
    "open a pr",
]


@pytest.mark.parametrize("persona", PERSONAS)
def test_distilled_file_exists(persona: str) -> None:
    f = DISTILLED_DIR / f"{persona}.md"
    assert f.is_file(), f"missing distilled persona file: {f}"


@pytest.mark.parametrize("persona", PERSONAS)
def test_distilled_file_within_size_cap(persona: str) -> None:
    f = DISTILLED_DIR / f"{persona}.md"
    size = f.stat().st_size
    assert size <= MAX_BYTES, f"{f} is {size} bytes, exceeds {MAX_BYTES}-byte cap"


@pytest.mark.parametrize("persona", PERSONAS)
def test_distilled_file_has_no_forbidden_content(persona: str) -> None:
    f = DISTILLED_DIR / f"{persona}.md"
    text = f.read_text().lower()
    for bad in FORBIDDEN_SUBSTRINGS:
        assert bad not in text, (
            f"{f} contains forbidden identity/outer-loop/mention marker: {bad!r}"
        )


@pytest.mark.parametrize("persona", PERSONAS)
def test_distilled_file_is_not_empty(persona: str) -> None:
    f = DISTILLED_DIR / f"{persona}.md"
    assert f.read_text().strip(), f"{f} is empty — must carry mindset + conventions"
