"""Unit tests for scripts/backfill_neptune_from_s3.py (#2493).

Validates the S3-discovery and repo-resolution logic without touching AWS or
Neptune:
- latest_csv_prefixes picks the newest timestamp subdirectory per repo
- read_repo_name reads the true repo:String from the CSV (handles multi-hyphen orgs)
- backfill dry-run makes no mutations and excludes corpus-excluded repos
- backfill --apply invokes load_to_neptune once per included repo
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Load the script module by path (it lives in scripts/, not an importable package).
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_neptune_from_s3.py"
_spec = importlib.util.spec_from_file_location("backfill_neptune_from_s3", _SCRIPT)
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)


class _FakePaginator:
    """Mimics boto3 list_objects_v2 paginator returning CommonPrefixes."""

    def __init__(self, pages_by_prefix):
        self._pages_by_prefix = pages_by_prefix

    def paginate(self, Bucket, Prefix, Delimiter):  # noqa: N803 (boto3 kwarg names)
        prefixes = self._pages_by_prefix.get(Prefix, [])
        yield {"CommonPrefixes": [{"Prefix": p} for p in prefixes]}


def _make_s3(pages_by_prefix):
    s3 = MagicMock()
    s3.get_paginator.return_value = _FakePaginator(pages_by_prefix)
    return s3


def test_latest_csv_prefixes_picks_newest_timestamp():
    pages = {
        "neptune-bulk-load/": [
            "neptune-bulk-load/org-repo/",
            "neptune-bulk-load/aws-e-adp/",
        ],
        "neptune-bulk-load/org-repo/": [
            "neptune-bulk-load/org-repo/20260101T000000Z/",
            "neptune-bulk-load/org-repo/20260625T000000Z/",  # newest
            "neptune-bulk-load/org-repo/20260301T000000Z/",
        ],
        "neptune-bulk-load/aws-e-adp/": [
            "neptune-bulk-load/aws-e-adp/20260624T000000Z/",
        ],
    }
    latest = bf.latest_csv_prefixes(_make_s3(pages), "bucket")
    assert latest["org-repo"] == "neptune-bulk-load/org-repo/20260625T000000Z/"
    assert latest["aws-e-adp"] == "neptune-bulk-load/aws-e-adp/20260624T000000Z/"


def test_latest_csv_prefixes_skips_repo_without_timestamps():
    pages = {
        "neptune-bulk-load/": ["neptune-bulk-load/empty-repo/"],
        "neptune-bulk-load/empty-repo/": [],
    }
    assert bf.latest_csv_prefixes(_make_s3(pages), "bucket") == {}


def test_read_repo_name_reads_true_repo_from_csv():
    # Multi-hyphen org name that safe-encoding (org-repo) cannot disambiguate.
    csv_text = (
        "~id,~label,symbol_id:String,name:String,module:String,file:String,"
        "line:Int,kind:String,repo:String,tenant_id:String,owner_sub:String\n"
        "aws-e-adp|abc,Symbol,sym,name,mod,file.py,1,function,aws-e/adp,,\n"
    )
    s3 = MagicMock()
    body = MagicMock()
    body.read.return_value = csv_text.encode("utf-8")
    s3.get_object.return_value = {"Body": body}
    assert bf.read_repo_name(s3, "bucket", "neptune-bulk-load/aws-e-adp/ts/") == "aws-e/adp"


def test_read_repo_name_empty_csv_returns_empty():
    s3 = MagicMock()
    body = MagicMock()
    body.read.return_value = b"~id,repo:String\n"  # header only, no rows
    s3.get_object.return_value = {"Body": body}
    assert bf.read_repo_name(s3, "bucket", "prefix/") == ""


def _patch_discovery(monkeypatch, repo_map):
    """Patch latest_csv_prefixes + read_repo_name so backfill() sees repo_map.

    repo_map: {repo_safe: repo_name}
    """
    prefixes = {safe: f"neptune-bulk-load/{safe}/ts/" for safe in repo_map}
    monkeypatch.setattr(bf, "latest_csv_prefixes", lambda s3, bucket: prefixes)

    def fake_read(s3, bucket, ts_prefix):
        for safe, name in repo_map.items():
            if ts_prefix == f"neptune-bulk-load/{safe}/ts/":
                return name
        return ""

    monkeypatch.setattr(bf, "read_repo_name", fake_read)


def test_backfill_dry_run_makes_no_mutations(monkeypatch):
    _patch_discovery(monkeypatch, {"org-repo": "org/repo"})
    with patch.object(bf.boto3, "client", return_value=MagicMock()):
        with patch("scip_neptune_loader.load_to_neptune") as loader:
            rc = bf.backfill(
                bucket="b",
                neptune_endpoint="ep",
                neptune_port="8182",
                region="us-east-1",
                only_repo=None,
                apply=False,
            )
    assert rc == 0
    loader.assert_not_called()


def test_backfill_excludes_corpus_excluded_repo(monkeypatch):
    _patch_discovery(
        monkeypatch,
        {"CopilotKit-CopilotKit": "CopilotKit/CopilotKit", "org-repo": "org/repo"},
    )
    monkeypatch.setattr(bf, "download_csvs", lambda *a, **k: ("v.csv", "e.csv"))
    monkeypatch.setattr(bf, "_make_csv_output", lambda v, e: object())
    with patch.object(bf.boto3, "client", return_value=MagicMock()):
        with patch(
            "scip_neptune_loader.load_to_neptune",
            return_value={"success": True, "vertices_loaded": 1, "edges_loaded": 1},
        ) as loader:
            rc = bf.backfill(
                bucket="b",
                neptune_endpoint="ep",
                neptune_port="8182",
                region="us-east-1",
                only_repo=None,
                apply=True,
            )
    assert rc == 0
    # Only org/repo loaded; CopilotKit excluded.
    assert loader.call_count == 1


def test_backfill_single_repo_filter(monkeypatch):
    _patch_discovery(monkeypatch, {"a-b": "a/b", "c-d": "c/d"})
    monkeypatch.setattr(bf, "download_csvs", lambda *a, **k: ("v.csv", "e.csv"))
    monkeypatch.setattr(bf, "_make_csv_output", lambda v, e: object())
    with patch.object(bf.boto3, "client", return_value=MagicMock()):
        with patch(
            "scip_neptune_loader.load_to_neptune",
            return_value={"success": True, "vertices_loaded": 2, "edges_loaded": 3},
        ) as loader:
            rc = bf.backfill(
                bucket="b",
                neptune_endpoint="ep",
                neptune_port="8182",
                region="us-east-1",
                only_repo="c/d",
                apply=True,
            )
    assert rc == 0
    assert loader.call_count == 1


def test_backfill_unknown_single_repo_errors(monkeypatch):
    _patch_discovery(monkeypatch, {"a-b": "a/b"})
    with patch.object(bf.boto3, "client", return_value=MagicMock()):
        rc = bf.backfill(
            bucket="b",
            neptune_endpoint="ep",
            neptune_port="8182",
            region="us-east-1",
            only_repo="does/not-exist",
            apply=True,
        )
    assert rc == 1


def test_backfill_reports_load_failure(monkeypatch):
    _patch_discovery(monkeypatch, {"a-b": "a/b"})
    monkeypatch.setattr(bf, "download_csvs", lambda *a, **k: ("v.csv", "e.csv"))
    monkeypatch.setattr(bf, "_make_csv_output", lambda v, e: object())
    with patch.object(bf.boto3, "client", return_value=MagicMock()):
        with patch(
            "scip_neptune_loader.load_to_neptune",
            return_value={"error": "connection_failed"},
        ):
            rc = bf.backfill(
                bucket="b",
                neptune_endpoint="ep",
                neptune_port="8182",
                region="us-east-1",
                only_repo=None,
                apply=True,
            )
    assert rc == 1


def test_backfill_no_repos_found_errors(monkeypatch):
    monkeypatch.setattr(bf, "latest_csv_prefixes", lambda s3, bucket: {})
    with patch.object(bf.boto3, "client", return_value=MagicMock()):
        rc = bf.backfill(
            bucket="b",
            neptune_endpoint="ep",
            neptune_port="8182",
            region="us-east-1",
            only_repo=None,
            apply=False,
        )
    assert rc == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
