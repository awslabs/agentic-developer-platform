"""Tests for post-security-pr-comment.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from post_security_pr_comment import COMMENT_MARKER, build_comment_body


def test_build_comment_no_new_findings():
    """Comment body shows 'no new findings' when all counts are zero."""
    summary = {
        "checkov": {"new_count": 0, "resolved_count": 0, "stable_count": 5, "new": []},
        "semgrep": {"new_count": 0, "resolved_count": 0, "stable_count": 3, "new": []},
    }
    body = build_comment_body(summary)
    assert COMMENT_MARKER in body
    assert "No new security findings" in body
    assert "| checkov |" in body
    assert "| semgrep |" in body


def test_build_comment_with_new_findings():
    """Comment body shows new findings count and details."""
    summary = {
        "checkov": {
            "new_count": 2,
            "resolved_count": 1,
            "stable_count": 5,
            "new": ["CKV_AWS_18:main.tf:10", "CKV_AWS_21:s3.tf:5"],
        },
        "semgrep": {"new_count": 0, "resolved_count": 0, "stable_count": 0, "new": []},
    }
    body = build_comment_body(summary)
    assert "2 new finding(s)" in body
    assert "CKV_AWS_18:main.tf:10" in body
    assert "checkov - New Findings" in body


def test_build_comment_caps_findings_at_20():
    """Findings list is capped at 20 per tool."""
    summary = {
        "bandit": {
            "new_count": 25,
            "resolved_count": 0,
            "stable_count": 0,
            "new": [f"finding-{i}" for i in range(25)],
        },
    }
    body = build_comment_body(summary)
    assert "finding-19" in body
    assert "finding-20" not in body
    assert "and 5 more" in body


def test_build_comment_contains_marker():
    """Comment always contains the sticky marker."""
    summary = {"checkov": {"new_count": 0, "resolved_count": 0, "stable_count": 0, "new": []}}
    body = build_comment_body(summary)
    assert body.startswith(COMMENT_MARKER)


def test_build_comment_table_format():
    """Comment contains properly formatted markdown table."""
    summary = {
        "checkov": {"new_count": 1, "resolved_count": 2, "stable_count": 3, "new": ["x"]},
    }
    body = build_comment_body(summary)
    assert "| Tool | New | Resolved | Baseline |" in body
    assert "| checkov | **1** | 2 | 3 |" in body
