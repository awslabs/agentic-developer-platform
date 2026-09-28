#!/usr/bin/env python3
"""Render the nightly security agent run report (intent #4290, unit U2).

`report.html` is produced HERE, by this script, and never authored by an
agent (FR-C33). Two properties follow from that and both are tested:

* **Purity (NFR-6).** ``build_view`` and ``render_html`` are pure functions of
  the merged ledger. No clock, no ``uuid``, no ``random``, no environment
  read, no dependence on dict insertion order. The report's timestamp arrives
  as the ledger's ``generated_at`` field. Same ledger in => byte-identical
  HTML out, in this interpreter and in the next one.
* **Allow-list (NT-11).** The view model is assembled by copying only the
  keys named in the schema's ``x-report-allowlist``. A field that reaches the
  ledger unexpectedly cannot reach the document, which is the path by which
  exploit detail would otherwise leak into something renderable. Values are
  HTML-escaped on the way out.

The only impure part is ``main``: it reads shard files from disk and writes
the output file.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from security_agent_ledger import (  # noqa: E402
    is_final,
    load_and_merge,
    load_schema,
    reconcile,
    status_counts,
)

STATUS_IN_PROGRESS = "delivery in progress"
STATUS_FINAL = "final"

_STORY_STATUS_LABELS = {
    "fixed": "fixed autonomously",
    "stuck": "halted / stuck",
    "in_progress": "in progress",
}

_REASON_LABELS = {
    "failed_run_limit": "3 failed developer runs",
    "no_transition_timeout": "24h with no state transition",
}


def build_view(merged: dict, schema: dict | None = None) -> dict:
    """Project a merged ledger onto the report's allow-listed view model.

    Pure. Every key in the result is named in ``x-report-allowlist``; nothing
    is copied from the ledger wholesale.
    """
    schema = schema or load_schema()
    allowed = set(schema["x-report-allowlist"])
    fields = merged["fields"]
    counts = status_counts(merged)

    candidate = {
        "run_date": merged["run_date"],
        "generated_at": merged["generated_at"],
        "status": STATUS_FINAL if is_final(merged) else STATUS_IN_PROGRESS,
        "identified_raw": fields.get("identified_raw", 0),
        "identified_new_after_dedup": fields.get("identified_new_after_dedup", 0),
        "stories_created": fields.get("stories_created", 0),
        "fixed": counts["fixed"],
        "stuck": counts["stuck"],
        "in_progress": counts["in_progress"],
        "run_duration_seconds": fields.get("run_duration_seconds"),
        "pentest_cost_usd": fields.get("pentest_cost_usd"),
        "daily_epic": fields.get("daily_epic"),
        "planned_sequence": list(fields.get("planned_sequence", [])),
        "stories": _build_stories(fields, schema),
        "reconciliation": _project(
            reconcile(merged), schema["x-report-reconciliation-allowlist"]
        ),
    }
    return {k: v for k, v in candidate.items() if k in allowed}


def _project(source: dict, allowlist: list[str]) -> dict:
    """Copy only allow-listed keys, in allow-list order (not dict order)."""
    return {k: source[k] for k in allowlist if k in source}


def _build_stories(fields: dict, schema: dict) -> list[dict]:
    """Per-story rows, ordered by the orchestration sequence then by number.

    The order comes from ledger data, never from dict iteration, so two
    merges of the same shards render identical rows.
    """
    statuses = fields.get("story_status", {})
    sequence = list(fields.get("planned_sequence", []))
    order = {story_id: i for i, story_id in enumerate(sequence)}

    rows = []
    for key, record in statuses.items():
        story_id = int(key)
        rows.append(
            _project(
                {
                    "story_id": story_id,
                    "status": record["status"],
                    "reason": record.get("reason"),
                },
                schema["x-report-story-allowlist"],
            )
        )
    # Sequenced stories first, in sequence; the rest by number after them.
    return sorted(
        rows, key=lambda r: (order.get(r["story_id"], len(order)), r["story_id"])
    )


def _fmt_duration(seconds: object) -> str:
    if not isinstance(seconds, int):
        return "not recorded"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _fmt_cost(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "not recorded"
    return f"${value:.2f}"


def _fmt_optional(value: object) -> str:
    return "not recorded" if value is None else str(value)


def _esc(value: object) -> str:
    """Escape for HTML text. Applied to every interpolated value."""
    return html.escape(str(value), quote=True)


def render_html(view: dict) -> str:
    """Render the view model to HTML. Pure; string in, string out."""
    rows = [
        ("Vulnerabilities identified (raw)", str(view["identified_raw"])),
        (
            "Vulnerabilities new after dedup",
            str(view["identified_new_after_dedup"]),
        ),
        ("Stories created", str(view["stories_created"])),
        ("Fixed autonomously", str(view["fixed"])),
        ("Halted / stuck", str(view["stuck"])),
        ("In progress", str(view["in_progress"])),
        ("Run duration", _fmt_duration(view.get("run_duration_seconds"))),
        ("Pentest cost", _fmt_cost(view.get("pentest_cost_usd"))),
        ("Daily EPIC", _fmt_optional(view.get("daily_epic"))),
    ]

    lines = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>Security agent run {_esc(view['run_date'])}</title>",
        "<style>",
        "body{font-family:system-ui,sans-serif;margin:2rem;max-width:60rem}",
        "table{border-collapse:collapse;margin:1rem 0}",
        "th,td{border:1px solid #ccc;padding:.4rem .8rem;text-align:left}",
        ".status{font-weight:600}",
        ".warn{color:#b00}",
        "</style>",
        "</head>",
        "<body>",
        f"<h1>Security agent run — {_esc(view['run_date'])}</h1>",
        f'<p class="status">Status: {_esc(view["status"])}</p>',
        f"<p>Ledger generated at {_esc(view['generated_at'])}</p>",
        "<h2>Summary</h2>",
        "<table>",
        "<tr><th>Metric</th><th>Value</th></tr>",
    ]
    for label, value in rows:
        lines.append(f"<tr><td>{_esc(label)}</td><td>{_esc(value)}</td></tr>")
    lines.append("</table>")

    lines.append("<h2>Stories</h2>")
    if view["stories"]:
        lines.append("<table>")
        lines.append("<tr><th>Story</th><th>Status</th><th>Reason</th></tr>")
        for story in view["stories"]:
            label = _STORY_STATUS_LABELS[story["status"]]
            reason = _REASON_LABELS.get(story.get("reason") or "", "—")
            lines.append(
                f"<tr><td>#{_esc(story['story_id'])}</td>"
                f"<td>{_esc(label)}</td><td>{_esc(reason)}</td></tr>"
            )
        lines.append("</table>")
    else:
        lines.append("<p>No stories were created for this run.</p>")

    if view["planned_sequence"]:
        sequence = " &rarr; ".join(f"#{_esc(n)}" for n in view["planned_sequence"])
        lines.append(f"<h2>Planned sequence</h2><p>{sequence}</p>")

    rec = view["reconciliation"]
    lines.append("<h2>Reconciliation</h2>")
    if rec.get("ok"):
        lines.append(
            "<p>Totals reconcile: fixed + stuck + in progress "
            f"= {_esc(rec['accounted'])} = stories created.</p>"
        )
    else:
        # Built as plain text, escaped exactly once at the interpolation.
        detail = []
        if not rec.get("identity_ok", True):
            detail.append(
                f"{rec['accounted']} stories accounted for vs "
                f"{view['stories_created']} created"
            )
        if rec.get("missing_story_ids"):
            missing = ", ".join(f"#{n}" for n in rec["missing_story_ids"])
            detail.append(f"no status recorded for {missing}")
        if rec.get("unexpected_story_ids"):
            extra = ", ".join(f"#{n}" for n in rec["unexpected_story_ids"])
            detail.append(f"status recorded for unfiled {extra}")
        if not rec.get("coverage_ok", True):
            detail.append(
                f"{rec.get('uncovered_finding_count', 0)} finding(s) "
                "not covered by any story"
            )
        lines.append(
            '<p class="warn">Totals do not reconcile: '
            f"{_esc('; '.join(detail))}.</p>"
        )

    lines.append("</body>")
    lines.append("</html>")
    return "\n".join(lines) + "\n"


def render(merged: dict, schema: dict | None = None) -> str:
    """Merged ledger -> HTML. The composed pure function."""
    return render_html(build_view(merged, schema))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the security agent run report from ledger shards"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--ledger-dir", help="directory of shard-*.json files")
    source.add_argument("--merged", help="path to an already-merged ledger JSON")
    parser.add_argument("--out", required=True, help="output HTML path")
    args = parser.parse_args(argv)

    if args.ledger_dir:
        merged = load_and_merge(args.ledger_dir)
    else:
        merged = json.loads(Path(args.merged).read_text())

    Path(args.out).write_text(render(merged))
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
