#!/usr/bin/env python3
"""Post or update a sticky PR comment with security scan findings summary.

Uses the GitHub CLI (gh) to upsert a comment identified by a marker.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


COMMENT_MARKER = "<!-- security-scan:summary -->"

SEVERITY_EMOJI = {
    "critical": "\U0001f534",  # red circle
    "high": "\U0001f7e0",      # orange circle
    "medium": "\U0001f7e1",    # yellow circle
    "low": "\U0001f7e1",       # yellow circle
}


def build_comment_body(summary: dict) -> str:
    """Build markdown comment body from summary data."""
    lines = [
        COMMENT_MARKER,
        "",
        "## Security Scan Results",
        "",
    ]

    total_new = sum(v.get("new_count", 0) for v in summary.values())
    total_resolved = sum(v.get("resolved_count", 0) for v in summary.values())
    total_stable = sum(v.get("stable_count", 0) for v in summary.values())

    if total_new == 0:
        lines.append("**No new security findings detected.**")
    else:
        lines.append(f"**{total_new} new finding(s)** detected in this PR.")

    lines.append("")
    lines.append("### Per-tool Summary")
    lines.append("")
    lines.append("| Tool | New | Resolved | Baseline |")
    lines.append("|------|-----|----------|----------|")

    for tool, data in sorted(summary.items()):
        new_count = data.get("new_count", 0)
        resolved_count = data.get("resolved_count", 0)
        stable_count = data.get("stable_count", 0)
        new_str = f"**{new_count}**" if new_count > 0 else "0"
        lines.append(f"| {tool} | {new_str} | {resolved_count} | {stable_count} |")

    lines.append("")

    # Detail new findings per tool
    for tool, data in sorted(summary.items()):
        new_findings = data.get("new", [])
        if not new_findings:
            continue
        lines.append(f"### {tool} - New Findings")
        lines.append("")
        for finding in new_findings[:20]:  # Cap at 20 per tool
            lines.append(f"- `{finding}`")
        if len(new_findings) > 20:
            lines.append(f"- ... and {len(new_findings) - 20} more")
        lines.append("")

    lines.append("---")
    lines.append("*Baseline-tolerated findings are not shown. See `.github/security/` for baselines.*")

    return "\n".join(lines)


def find_existing_comment(repo: str, pr: str) -> str | None:
    """Find existing security scan comment by marker."""
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{pr}/comments", "--paginate"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    try:
        comments = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    for comment in comments:
        if COMMENT_MARKER in comment.get("body", ""):
            return str(comment["id"])

    return None


def post_or_update_comment(repo: str, pr: str, body: str) -> None:
    """Create or update the sticky PR comment."""
    existing_id = find_existing_comment(repo, pr)

    if existing_id:
        subprocess.run(
            [
                "gh", "api",
                f"repos/{repo}/issues/comments/{existing_id}",
                "--method", "PATCH",
                "--field", f"body={body}",
            ],
            check=True,
            capture_output=True,
        )
    else:
        subprocess.run(
            [
                "gh", "api",
                f"repos/{repo}/issues/{pr}/comments",
                "--method", "POST",
                "--field", f"body={body}",
            ],
            check=True,
            capture_output=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Post security scan summary to PR")
    parser.add_argument("--summary", required=True, help="Path to summary JSON")
    parser.add_argument("--repo", required=True, help="Repository (owner/name)")
    parser.add_argument("--pr", required=True, help="PR number")
    args = parser.parse_args()

    summary_path = Path(args.summary)
    if not summary_path.exists():
        print("No summary file found, skipping comment")
        sys.exit(0)

    summary = json.loads(summary_path.read_text())
    body = build_comment_body(summary)
    post_or_update_comment(args.repo, args.pr, body)
    print(f"Posted security scan comment to PR #{args.pr}")


if __name__ == "__main__":
    main()
