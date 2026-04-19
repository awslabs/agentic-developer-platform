#!/usr/bin/env python3
"""Parse JUnit XML from pytest and generate a summary table.

Usage:
    python tests/scripts/report.py /tmp/gateway-e2e.xml

Outputs a Markdown summary table suitable for pasting into a GitHub issue comment.
"""

from __future__ import annotations

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

    stats: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "error": 0})
    failures: dict[str, list[dict]] = defaultdict(list)

    # Handle both <testsuites><testsuite>... and <testsuite>... root
    suites = root.findall(".//testcase")

    for tc in suites:
        classname = tc.get("classname", "")
        name = tc.get("name", "")
        cat = _category_from_classname(classname)

        stats[cat]["total"] += 1

        failure = tc.find("failure")
        error = tc.find("error")
        skipped = tc.find("skipped")

        if failure is not None:
            stats[cat]["failed"] += 1
            failures[cat].append({
                "test": name,
                "classname": classname,
                "message": (failure.get("message") or "")[:200],
                "text": (failure.text or "")[:500],
            })
        elif error is not None:
            stats[cat]["error"] += 1
            stats[cat]["failed"] += 1
            failures[cat].append({
                "test": name,
                "classname": classname,
                "message": (error.get("message") or "")[:200],
                "text": (error.text or "")[:500],
            })
        elif skipped is not None:
            stats[cat]["skipped"] += 1
        else:
            stats[cat]["passed"] += 1

    return {"stats": dict(stats), "failures": dict(failures)}


def render_table(data: dict) -> str:
    """Render Markdown summary table."""
    stats = data["stats"]
    failures = data["failures"]

    # Canonical order
    order = ["auth", "admin", "proxy", "budget", "ratelimit", "pool", "frontend", "other"]
    categories = [c for c in order if c in stats]
    # Add any we missed
    for c in sorted(stats.keys()):
        if c not in categories:
            categories.append(c)

    lines = ["### Summary table", "", "| Category | Total | Passed | Failed | Skipped | Status |", "|----------|-------|--------|--------|---------|--------|"]

    totals = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
    for cat in categories:
        s = stats[cat]
        status = "✅" if s["failed"] == 0 else "❌"
        lines.append(f"| {cat} | {s['total']} | {s['passed']} | {s['failed']} | {s['skipped']} | {status} |")
        for k in totals:
            totals[k] += s[k]

    lines.append(f"| **Total** | **{totals['total']}** | **{totals['passed']}** | **{totals['failed']}** | **{totals['skipped']}** | — |")

    # Per-category failure details
    for cat in categories:
        if cat not in failures or not failures[cat]:
            continue
        lines.append("")
        s = stats[cat]
        lines.append(f"### {cat}")
        lines.append(f"- total: {s['total']} / passed: {s['passed']} / failed: {s['failed']} / skipped: {s['skipped']}")
        lines.append("")
        lines.append("Failed tests:")
        for f in failures[cat]:
            msg = f["message"].replace("\n", " ").strip()
            lines.append(f"- `{f['test']}` — {msg}")

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
