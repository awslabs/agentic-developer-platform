#!/usr/bin/env python3
"""Filter Grype SARIF only when configured package constraints are proven.

The orchestrator supplies raw SARIF generated with Grype exclusions disabled.
Package name/type come from rule metadata, never an inferred rule-ID suffix.
Blanket, ambiguous, and unsupported selectors retain their findings for review.
--raw-output preserves input bytes; --summary-output records applied selectors.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


# ---------------------------------------------------------------------------
# Selector model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IgnoreSelector:
    """One entry from ``.grype.yaml``'s ``ignore`` list."""

    vulnerability: str
    package_name: Optional[str] = None
    package_type: Optional[str] = None
    unsupported_scope: bool = False

    @classmethod
    def from_config_entry(cls, entry: dict) -> Optional["IgnoreSelector"]:
        vuln = entry.get("vulnerability")
        if not vuln:
            return None
        pkg = entry.get("package") or {}
        return cls(
            vulnerability=vuln,
            package_name=pkg.get("name"),
            package_type=pkg.get("type"),
            unsupported_scope=bool(
                set(entry) - {"vulnerability", "package"} or set(pkg) - {"name", "type"}
            ),
        )

    @property
    def is_scoped(self) -> bool:
        """True when the selector restricts suppression to a specific package."""
        return self.package_name is not None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_ignore_selectors(config_path: str) -> list[IgnoreSelector]:
    """Extract structured ignore selectors from ``.grype.yaml``."""
    path = Path(config_path)
    if not path.exists():
        return []

    with open(path) as f:
        config = yaml.safe_load(f)

    if not config or "ignore" not in config:
        return []

    selectors: list[IgnoreSelector] = []
    for entry in config["ignore"]:
        sel = IgnoreSelector.from_config_entry(entry)
        if sel is not None:
            selectors.append(sel)
    return selectors


# Backward-compatible shim used by older call-sites.
def load_ignore_cves(config_path: str) -> set[str]:
    """Return the set of vulnerability IDs (without package scope).

    This is the legacy API — prefer ``load_ignore_selectors`` for new code.
    """
    return {s.vulnerability for s in load_ignore_selectors(config_path)}


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _extract_vuln_and_suffix(rule_id: str) -> tuple[str, Optional[str]]:
    """Split a SARIF ruleId into (vulnerability_id, package_suffix | None).

    Grype formats ruleIds as ``<CVE>`` or ``<CVE>-<package>``.  The
    vulnerability portion is a well-known prefix (CVE-YYYY-NNNNN or
    GHSA-xxxx-xxxx-xxxx); the rest, if present after a ``-`` separator
    following the complete ID, is a package hint.
    """
    # CVE pattern: CVE-YYYY-NNNNN...
    cve_match = re.match(r"(CVE-\d{4}-\d+)(?=$|-)", rule_id)
    if cve_match:
        vuln_id = cve_match.group(1)
        rest = rule_id[len(vuln_id) :]
        if rest.startswith("-"):
            return vuln_id, rest[1:]
        return vuln_id, None

    # GHSA pattern: GHSA-xxxx-xxxx-xxxx
    ghsa_match = re.match(r"(GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4})(?=$|-)", rule_id)
    if ghsa_match:
        vuln_id = ghsa_match.group(1)
        rest = rule_id[len(vuln_id) :]
        if rest.startswith("-"):
            return vuln_id, rest[1:]
        return vuln_id, None

    return rule_id, None


@dataclass
class MatchResult:
    """Outcome of matching a single SARIF result against selectors."""

    suppressed: bool = False
    selector: Optional[IgnoreSelector] = None
    scope_verified: bool = False


def match_result(
    rule_id: str,
    selectors: list[IgnoreSelector],
    package_name: Optional[str] = None,
    package_type: Optional[str] = None,
) -> MatchResult:
    """Retain findings unless every configured constraint has positive evidence.

    Package identity comes from Grype's rule metadata, never a guessed suffix.
    Unsupported version/image constraints cannot silently become broader ignores.
    Blanket rules remain visible pending finding-specific baseline review.
    """
    vuln_id, _ = _extract_vuln_and_suffix(rule_id)
    for sel in selectors:
        if sel.unsupported_scope or not sel.package_name:
            continue
        if sel.vulnerability != vuln_id or package_name != sel.package_name:
            continue
        if sel.package_type is not None and package_type != sel.package_type:
            continue
        return MatchResult(True, sel, True)
    return MatchResult()


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def filter_sarif(
    sarif_path: str,
    selectors: list[IgnoreSelector],
) -> tuple[dict, list[dict]]:
    """Remove results matching ignore selectors from SARIF data.

    Returns ``(filtered_sarif, suppression_records)`` where each record
    describes one suppressed finding with the selector that matched it.
    """
    with open(sarif_path) as f:
        sarif = json.load(f)

    if not selectors:
        return sarif, []

    suppressed_records: list[dict] = []

    for run in sarif.get("runs", []):
        rules = {}
        for rule in run.get("tool", {}).get("driver", {}).get("rules", []):
            rule_id = rule.get("id")
            # Ambiguous duplicate metadata must not authorize suppression.
            rules[rule_id] = None if rule_id in rules else rule
        kept: list[dict] = []
        for r in run.get("results", []):
            rule_id = r.get("ruleId", "")
            rule = rules.get(rule_id) or {}
            help_text = rule.get("help", {}).get("text", "")
            names = re.findall(r"^Package: ([^\n]+)$", help_text, re.MULTILINE)
            types = re.findall(r"^Type: ([^\n]+)$", help_text, re.MULTILINE)
            m = match_result(
                rule_id,
                selectors,
                names[0] if len(names) == 1 else None,
                types[0] if len(types) == 1 else None,
            )
            if m.suppressed:
                record: dict = {
                    "ruleId": rule_id,
                    "selector_vulnerability": m.selector.vulnerability
                    if m.selector
                    else None,
                    "selector_package_name": m.selector.package_name
                    if m.selector
                    else None,
                    "selector_package_type": m.selector.package_type
                    if m.selector
                    else None,
                    "scope_verified": m.scope_verified,
                }
                suppressed_records.append(record)
            else:
                kept.append(r)
        run["results"] = kept

    return sarif, suppressed_records


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Filter SARIF results using .grype.yaml ignore rules"
    )
    parser.add_argument("--sarif", required=True, help="Path to SARIF file")
    parser.add_argument("--config", required=True, help="Path to .grype.yaml")
    parser.add_argument(
        "--output", required=True, help="Output path for filtered SARIF"
    )
    parser.add_argument(
        "--raw-output",
        default=None,
        help="If set, copy the unfiltered SARIF to this path before filtering",
    )
    parser.add_argument(
        "--summary-output",
        default=None,
        help="If set, write a JSON suppression summary to this path",
    )
    args = parser.parse_args()

    if not Path(args.sarif).exists():
        print(f"ERROR: SARIF file not found: {args.sarif}", file=sys.stderr)
        return 1

    # Preserve the raw (pre-filter) SARIF when requested.
    if args.raw_output:
        Path(args.raw_output).parent.mkdir(parents=True, exist_ok=True)
        raw_bytes = Path(args.sarif).read_bytes()
        Path(args.raw_output).write_bytes(raw_bytes)

    selectors = load_ignore_selectors(args.config)
    if not selectors:
        print("WARN: No ignore rules found — SARIF unchanged", file=sys.stderr)
        sarif = json.loads(Path(args.sarif).read_text())
        suppressed_records: list[dict] = []
    else:
        # Count originals for the summary line.
        original_count = 0
        with open(args.sarif) as f:
            original = json.load(f)
        for run in original.get("runs", []):
            original_count += len(run.get("results", []))

        sarif, suppressed_records = filter_sarif(args.sarif, selectors)

        filtered_count = 0
        for run in sarif.get("runs", []):
            filtered_count += len(run.get("results", []))
        removed = original_count - filtered_count

        scope_verified = sum(1 for r in suppressed_records if r["scope_verified"])
        print(
            f"Filtered SARIF: removed {removed} results "
            f"({original_count} -> {filtered_count}) "
            f"using {len(selectors)} selectors "
            f"({scope_verified}/{removed} scope-verified)"
        )

    # Write filtered SARIF.
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(sarif, f, indent=2)

    # Write suppression summary when requested.
    if args.summary_output:
        Path(args.summary_output).parent.mkdir(parents=True, exist_ok=True)
        summary = {
            "total_suppressed": len(suppressed_records),
            "scope_verified_count": sum(
                1 for r in suppressed_records if r["scope_verified"]
            ),
            "suppressed": suppressed_records,
        }
        with open(args.summary_output, "w") as f:
            json.dump(summary, f, indent=2)

    return 0


if __name__ == "__main__":
    sys.exit(main())
