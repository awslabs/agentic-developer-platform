#!/usr/bin/env python3
"""Parse JUnit XML from pytest and generate a summary table.

Usage:
    python tests/scripts/report.py /tmp/gateway-e2e.xml

Outputs a Markdown summary table suitable for pasting into a GitHub issue comment.

The report distinguishes three test modes per category:
- **live**: Tests marked ``@pytest.mark.live_only`` that hit the deployed gateway
- **integration**: Tests marked ``@pytest.mark.integration`` that exercise the ASGI app via HTTP
- **unit**: Tests marked ``@pytest.mark.unit`` that use mocks/db_session only

A warning is emitted for any category with zero live tests to prevent
false-green reports where only unit/integration tests ran.
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

# Map test file stems to category names
FILE_TO_CATEGORY = {
    "test_authentication_stories": "auth",
    "test_admin_stories": "admin",
    "test_proxy_stories": "proxy",
    "test_budget_stories": "budget",
    "test_ratelimit_stories": "ratelimit",
    "test_pool_stories": "pool",
    "test_frontend_smoke": "frontend",
}

# Heuristics to infer mode from class/test name when markers are not in XML
_LIVE_PATTERNS = re.compile(r"Live|live_only|TestLive")
_INTEGRATION_PATTERNS = re.compile(r"Integration|HTTP|RBAC|TestHTTP|TestProxy.*HTTP|TestAdmin.*RBAC")


def _infer_mode(classname: str, testname: str) -> str:
    """Best-effort mode inference from class/test naming conventions.

    JUnit XML does not embed pytest markers, so we use naming heuristics:
    - Classes containing "Live" or tests containing "live" -> live
    - Classes containing "HTTP", "Integration", "RBAC" -> integration
    - Everything else -> unit
    """
    combined = f"{classname}.{testname}"
    if _LIVE_PATTERNS.search(combined):
        return "live"
    if _INTEGRATION_PATTERNS.search(combined):
        return "integration"
    return "unit"


def _category_from_classname(classname: str) -> str:
    """Extract category from JUnit classname like 'tests.e2e.test_auth_stories.TestFoo'."""
    parts = classname.split(".")
    for part in parts:
        if part in FILE_TO_CATEGORY:
            return FILE_TO_CATEGORY[part]
    # Fallback: try matching substring
    for stem, cat in FILE_TO_CATEGORY.items():
        if stem in classname:
            return cat
    return "other"


def parse_junit(xml_path: str | Path) -> dict:
    """Parse JUnit XML and return per-category stats and failure details."""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "total": 0, "passed": 0, "failed": 0, "skipped": 0, "error": 0,
            "live": 0, "integration": 0, "unit": 0,
            "live_passed": 0, "integration_passed": 0, "unit_passed": 0,
        }
    )
    failures: dict[str, list[dict]] = defaultdict(list)

    suites = root.findall(".//testcase")

    for tc in suites:
        classname = tc.get("classname", "")
        name = tc.get("name", "")
        cat = _category_from_classname(classname)
        mode = _infer_mode(classname, name)

        stats[cat]["total"] += 1
        stats[cat][mode] += 1

        failure = tc.find("failure")
        error = tc.find("error")
        skipped = tc.find("skipped")

        if failure is not None:
            stats[cat]["failed"] += 1
            failures[cat].append({
                "test": name,
                "classname": classname,
                "mode": mode,
                "message": (failure.get("message") or "")[:200],
                "text": (failure.text or "")[:500],
            })
        elif error is not None:
            stats[cat]["error"] += 1
            stats[cat]["failed"] += 1
            failures[cat].append({
                "test": name,
                "classname": classname,
                "mode": mode,
                "message": (error.get("message") or "")[:200],
                "text": (error.text or "")[:500],
            })
        elif skipped is not None:
            stats[cat]["skipped"] += 1
        else:
            stats[cat]["passed"] += 1
            stats[cat][f"{mode}_passed"] += 1

    return {"stats": dict(stats), "failures": dict(failures)}


def render_table(data: dict) -> str:
    """Render Markdown summary table with mode breakdown."""
    stats = data["stats"]
    failures = data["failures"]

    # Canonical order
    order = ["auth", "admin", "proxy", "budget", "ratelimit", "pool", "frontend", "other"]
    categories = [c for c in order if c in stats]
    for c in sorted(stats.keys()):
        if c not in categories:
            categories.append(c)

    lines = [
        "### Summary table",
        "",
        "| Category | Total | Passed | Failed | Skipped | Live | Integ | Unit | Status |",
        "|----------|-------|--------|--------|---------|------|-------|------|--------|",
    ]

    totals = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "live": 0, "integration": 0, "unit": 0}
    zero_live_cats: list[str] = []

    for cat in categories:
        s = stats[cat]
        status = "pass" if s["failed"] == 0 else "FAIL"
        live_count = s.get("live", 0)
        integ_count = s.get("integration", 0)
        unit_count = s.get("unit", 0)

        if live_count == 0 and cat not in ("frontend", "other"):
            zero_live_cats.append(cat)

        lines.append(
            f"| {cat} | {s['total']} | {s['passed']} | {s['failed']} | {s['skipped']} "
            f"| {live_count} | {integ_count} | {unit_count} | {status} |"
        )
        totals["total"] += s["total"]
        totals["passed"] += s["passed"]
        totals["failed"] += s["failed"]
        totals["skipped"] += s["skipped"]
        totals["live"] += live_count
        totals["integration"] += integ_count
        totals["unit"] += unit_count

    lines.append(
        f"| **Total** | **{totals['total']}** | **{totals['passed']}** | **{totals['failed']}** "
        f"| **{totals['skipped']}** | **{totals['live']}** | **{totals['integration']}** "
        f"| **{totals['unit']}** | -- |"
    )

    # Mode distribution summary
    lines.append("")
    lines.append("### Mode distribution")
    lines.append(f"- **Live** (hit deployed gateway): {totals['live']} tests")
    lines.append(f"- **Integration** (ASGI in-process HTTP): {totals['integration']} tests")
    lines.append(f"- **Unit** (mocks/db_session only): {totals['unit']} tests")

    # Zero-live warnings
    if zero_live_cats:
        lines.append("")
        lines.append("### Warnings")
        for cat in zero_live_cats:
            lines.append(f"- **{cat}**: 0 live tests ran -- live coverage gap!")

    # Per-category failure details
    for cat in categories:
        if cat not in failures or not failures[cat]:
            continue
        lines.append("")
        s = stats[cat]
        lines.append(f"### {cat}")
        lines.append(f"- total: {s['total']} / passed: {s['passed']} / failed: {s['failed']} / skipped: {s['skipped']}")
        lines.append(f"- mode breakdown: live={s.get('live', 0)} / integration={s.get('integration', 0)} / unit={s.get('unit', 0)}")
        lines.append("")
        lines.append("Failed tests:")
        for f in failures[cat]:
            msg = f["message"].replace("\n", " ").strip()
            lines.append(f"- `{f['test']}` [{f['mode']}] -- {msg}")

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: python report.py <junit-xml-path>", file=sys.stderr)
        sys.exit(1)

    xml_path = sys.argv[1]
    if not Path(xml_path).exists():
        print(f"File not found: {xml_path}", file=sys.stderr)
        sys.exit(1)

    data = parse_junit(xml_path)
    print(render_table(data))


if __name__ == "__main__":
    main()
