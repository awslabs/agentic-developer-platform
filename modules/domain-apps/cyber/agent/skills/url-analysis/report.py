"""
Report generation — render URL analysis results as JSON, Markdown, and HTML.

Produces artifacts suitable for:
- GitHub issue comments (Markdown)
- SIEM ingestion (JSON)
- Analyst review (HTML)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def render_json_report(
    artifact_id: str,
    url: str,
    findings: dict[str, Any],
    verdict: dict[str, Any],
    duration_seconds: int,
) -> str:
    """Render the full structured JSON report (stage envelope format)."""
    envelope = {
        "artifact_id": artifact_id,
        "stage": "url-analysis",
        "stage_name": "url-analysis",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "ok",
        "duration_seconds": duration_seconds,
        "findings": findings,
        "verdict": verdict,
        "tool_calls": findings.get("tool_calls", 0),
        "notes": findings.get("notes", ""),
    }
    return json.dumps(envelope, indent=2, default=str)


def render_markdown_report(
    url: str,
    findings: dict[str, Any],
    verdict: dict[str, Any],
    duration_seconds: int,
) -> str:
    """Render a Markdown report suitable for GitHub issue comments."""
    severity = verdict.get("severity", "unknown")
    confidence = verdict.get("confidence", 0)
    category = verdict.get("category", "unknown")

    # Severity emoji
    severity_icon = {"malicious": "🔴", "suspicious": "🟡", "clean": "🟢"}.get(
        severity, "⚪"
    )

    lines = [
        f"## URL Analysis — {severity_icon} {severity.upper()} (confidence: {confidence}%)",
        "",
        f"**URL**: `{url}`",
        f"**Category**: {category}",
        f"**Duration**: {duration_seconds}s",
        "",
    ]

    # Verdict reasoning
    reasoning = verdict.get("reasoning", "")
    if reasoning:
        lines.extend(["### Reasoning", "", reasoning, ""])

    # Redirect chain
    redirect_chain = findings.get("redirect_chain", [])
    if redirect_chain:
        lines.extend(["### Redirect Chain", ""])
        for i, hop in enumerate(redirect_chain, 1):
            lines.append(f"{i}. `{hop}`")
        final_url = findings.get("final_url", "")
        if final_url and final_url != url:
            lines.append(f"{len(redirect_chain) + 1}. `{final_url}` (final)")
        lines.append("")

    # Network summary
    network = findings.get("network_requests", [])
    if network:
        lines.extend(
            [
                "### Network Activity",
                "",
                f"**Total requests**: {len(network)}",
                "",
                "| URL | Status | MIME |",
                "|-----|--------|------|",
            ]
        )
        for req in network[:10]:  # Show top 10
            lines.append(
                f"| `{_truncate(req.get('url', ''), 60)}` | {req.get('status', '-')} | {req.get('mime', '-')} |"
            )
        if len(network) > 10:
            lines.append(f"| ... | | ({len(network) - 10} more) |")
        lines.append("")

    # Forms detected
    forms = findings.get("forms_detected", [])
    if forms:
        lines.extend(["### Forms Detected", ""])
        for form in forms:
            fields = form.get("fields", [])
            field_types = [f.get("type", "text") for f in fields]
            lines.append(
                f"- Action: `{form.get('action', 'N/A')}` — fields: {', '.join(field_types)}"
            )
        lines.append("")

    # Auto downloads
    downloads = findings.get("auto_downloads", [])
    if downloads:
        lines.extend(["### Auto-Downloads Detected", ""])
        for dl in downloads:
            lines.append(
                f"- `{dl.get('url', 'unknown')}` (MIME: {dl.get('mime', '?')}, SHA256: `{dl.get('sha256', '?')}`)"
            )
        lines.append("")

    # Enrichment summary
    enrichment = findings.get("enrichment", {})
    if enrichment:
        lines.extend(["### Enrichment", ""])

        whois = enrichment.get("whois", {})
        if whois and not whois.get("error"):
            lines.append(
                f"- **WHOIS**: registrar={whois.get('registrar', '?')}, age={whois.get('age_days', '?')} days"
            )

        vt = enrichment.get("virustotal", {})
        if vt.get("found"):
            lines.append(
                f"- **VirusTotal**: {vt.get('malicious', 0)} malicious, {vt.get('suspicious', 0)} suspicious"
            )
        elif vt.get("skipped"):
            lines.append("- **VirusTotal**: skipped (no API key)")

        urlhaus = enrichment.get("urlhaus", {})
        if urlhaus.get("found"):
            lines.append(f"- **URLhaus**: known threat ({urlhaus.get('threat', '?')})")

        misp = enrichment.get("misp", {})
        if misp.get("found"):
            lines.append(
                f"- **MISP**: {misp.get('matching_attributes', 0)} matching attributes"
            )
        elif misp.get("skipped"):
            lines.append("- **MISP**: skipped (not configured)")

        lines.append("")

    # IOCs
    iocs = findings.get("iocs", {})
    if any(iocs.values()):
        lines.extend(["### Indicators of Compromise", ""])
        for ioc_type, values in iocs.items():
            if values:
                lines.append(f"**{ioc_type}**:")
                for v in values[:10]:
                    lines.append(f"- `{v}`")
        lines.append("")

    # MITRE ATT&CK
    mitre = verdict.get("mitre_attack", [])
    if mitre:
        lines.extend(["### MITRE ATT&CK", ""])
        for t in mitre:
            lines.append(f"- {t}")
        lines.append("")

    # Recommended actions
    actions = verdict.get("recommended_actions", [])
    if actions:
        lines.extend(["### Recommended Actions", ""])
        for action in actions:
            lines.append(f"- {action}")
        lines.append("")

    # Artifacts
    screenshots = findings.get("screenshots", [])
    if screenshots:
        lines.extend(["### Artifacts", ""])
        for s in screenshots:
            lines.append(f"- Screenshot: `{s}`")
        dom_uri = findings.get("dom_snapshot_uri", "")
        if dom_uri:
            lines.append(f"- DOM snapshot: `{dom_uri}`")
        har_uri = findings.get("har_file_uri", "")
        if har_uri:
            lines.append(f"- HAR file: `{har_uri}`")
        lines.append("")

    # Full JSON envelope (collapsed)
    lines.extend(
        [
            "<details><summary>Full JSON envelope</summary>",
            "",
            "```json",
            json.dumps(
                {"findings": findings, "verdict": verdict}, indent=2, default=str
            ),
            "```",
            "",
            "</details>",
        ]
    )

    return "\n".join(lines)


def render_html_report(
    url: str,
    findings: dict[str, Any],
    verdict: dict[str, Any],
    duration_seconds: int,
) -> str:
    """Render an HTML report for analyst review."""
    severity = verdict.get("severity", "unknown")
    confidence = verdict.get("confidence", 0)
    category = verdict.get("category", "unknown")

    severity_color = {
        "malicious": "#dc3545",
        "suspicious": "#ffc107",
        "clean": "#28a745",
    }.get(severity, "#6c757d")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>URL Analysis Report — {_html_escape(url)}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 2rem; }}
        .verdict {{ background: {severity_color}; color: white; padding: 1rem; border-radius: 4px; }}
        .section {{ margin: 1.5rem 0; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #dee2e6; padding: 0.5rem; text-align: left; }}
        th {{ background: #f8f9fa; }}
        code {{ background: #f1f3f5; padding: 0.2em 0.4em; border-radius: 3px; }}
        .ioc {{ font-family: monospace; }}
    </style>
</head>
<body>
    <h1>URL Analysis Report</h1>
    <div class="verdict">
        <h2>{severity.upper()} — Confidence: {confidence}%</h2>
        <p>Category: {category}</p>
        <p>URL: <code>{_html_escape(url)}</code></p>
    </div>

    <div class="section">
        <h3>Reasoning</h3>
        <p>{_html_escape(verdict.get("reasoning", "N/A"))}</p>
    </div>

    <div class="section">
        <h3>Analysis Duration</h3>
        <p>{duration_seconds} seconds</p>
    </div>

    <div class="section">
        <h3>Recommended Actions</h3>
        <ul>
"""
    for action in verdict.get("recommended_actions", []):
        html += f"            <li>{_html_escape(action)}</li>\n"

    html += """        </ul>
    </div>
</body>
</html>"""

    return html


def _truncate(s: str, max_len: int) -> str:
    """Truncate a string with ellipsis."""
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def _html_escape(s: str) -> str:
    """Basic HTML escaping."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )
