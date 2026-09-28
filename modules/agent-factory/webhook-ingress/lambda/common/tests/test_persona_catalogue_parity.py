"""Drift lint for docs/agent-catalogue.md ↔ MENTION_TO_PERSONA (issue #4021).

The catalogue is the doc users read to find out which agents exist and how to
summon them. A wrong mention string there sends a user to a trigger that does
nothing — the exact PoV complaint (ADP-006) this gate exists to prevent from
recurring. Since `personas.py` is the source of truth, this test pins the two in
both directions:

  * every mention string in the catalogue table exists in MENTION_TO_PERSONA;
  * every MENTION_TO_PERSONA key appears in the catalogue table;
  * the parse found a plausible number of rows.

That last assertion is not redundant. A two-way set comparison passes vacuously
when the parse yields nothing (empty ⊆ empty, both directions), so a table
reformat that broke the parser would turn this gate green while removing all of
its coverage. Asserting the row count catches that.

Note: `webhook-ingress-ci.yml` must keep `docs/agent-catalogue.md` in its
`paths:` trigger, or a docs-only PR runs no CI and this gate never fires.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from common.personas import MENTION_TO_PERSONA

# Repo root is parents[6] from this file:
# tests/[0] common/[1] lambda/[2] webhook-ingress/[3] agent-factory/[4]
# modules/[5] <repo root>/[6]
_CATALOGUE_RELPATH = Path("docs") / "agent-catalogue.md"

# Mention strings look like `@agent-foo` inside a backticked table cell.
_MENTION_RE = re.compile(r"`(@agent-[a-z0-9-]+)`")


def _catalogue_path() -> Path:
    """Resolve the catalogue lazily.

    Kept inside a function rather than at module scope because `common/` is
    zipped into every deployed Lambda artifact (package-lambdas.sh excludes
    `<module>/tests/*` only for in-process modules), so a module-level path
    resolution against the repo root could fail at import time in a packaged
    context.
    """
    return Path(__file__).resolve().parents[6] / _CATALOGUE_RELPATH


def _parse_catalogue_mentions() -> set[str]:
    """Extract the mention strings from the catalogue's persona table.

    Only the persona table's rows are considered: a row is a markdown table line
    whose second column holds a backticked `@agent-*` string. Prose mentions
    elsewhere in the doc (routing rules, examples) are ignored so that
    documentation of *behaviour* cannot satisfy the parity check.
    """
    path = _catalogue_path()
    assert path.is_file(), f"agent catalogue not found at {path}"

    mentions: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        found = _MENTION_RE.findall(cells[1])
        mentions.update(found)
    return mentions


def test_catalogue_table_parses_non_empty() -> None:
    """Guard against a vacuous pass: the parse must find every persona's row.

    Without this, reformatting the table so the parser matches nothing would
    make both parity assertions below trivially true.
    """
    mentions = _parse_catalogue_mentions()
    assert len(mentions) == len(MENTION_TO_PERSONA), (
        f"catalogue table parsed {len(mentions)} mention rows but "
        f"MENTION_TO_PERSONA has {len(MENTION_TO_PERSONA)} entries — the table "
        f"format may have changed and broken the parser. Parsed: {sorted(mentions)}"
    )


@pytest.mark.parametrize("mention", sorted(MENTION_TO_PERSONA))
def test_every_code_mention_is_documented(mention: str) -> None:
    """A persona users cannot discover is a persona that does not get used."""
    documented = _parse_catalogue_mentions()
    assert mention in documented, (
        f"{mention} is in MENTION_TO_PERSONA but missing from "
        f"{_CATALOGUE_RELPATH} — add a row for it."
    )


def test_every_documented_mention_is_real() -> None:
    """A documented string that does not dispatch is the ADP-006 bug itself."""
    documented = _parse_catalogue_mentions()
    unknown = documented - set(MENTION_TO_PERSONA)
    assert not unknown, (
        f"{_CATALOGUE_RELPATH} documents mention string(s) {sorted(unknown)} "
        f"that are not in MENTION_TO_PERSONA — they dispatch nothing. Valid: "
        f"{sorted(MENTION_TO_PERSONA)}"
    )
