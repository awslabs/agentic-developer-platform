"""Bounded repository metadata checks shared by capture and setup diagnostics."""

import re

MAX_CONTENT = 6 * 1024 * 1024
MAX_ARCHIVE = 12 * 1024 * 1024
MAX_FILES = 1000
# Only callers with approved repository capture authority opt into these limits.
# Documents and callers that omit limits retain the existing small bounds.
REPOSITORY_MAX_BYTES = 1024 * 1024 * 1024
REPOSITORY_MAX_FILES = 100000
MAX_TREE_BYTES = 16 * 1024 * 1024
MAX_TREE_ENTRIES = 100000
MAX_DIAGNOSTIC_PATHS = 20


def validate_exclusions(exclusions):
    if not isinstance(exclusions, list) or len(exclusions) > 100:
        raise ValueError("invalid_exclusions")
    for excluded in exclusions:
        if not isinstance(excluded, str):
            raise ValueError("invalid_exclusions")
        path = excluded.rstrip("/")
        if (
            not path
            or len(excluded) > 1024
            or path.startswith("/")
            or "\\" in path
            or "//" in excluded
            or any(character in path for character in "*?[:")
            or any(ord(character) < 32 for character in path)
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise ValueError("invalid_exclusions")


def excluded_path(path, exclusions):
    return any(path == excluded or path.startswith(excluded.rstrip("/") + "/") for excluded in exclusions)


def selected_repository_metadata(tree, tree_sha, exclusions):
    """Return the exact selected regular blobs, or no partial inventory at all."""
    validate_exclusions(exclusions)
    if (
        not isinstance(tree_sha, str)
        or not re.fullmatch(r"[a-f0-9]{40}", tree_sha)
        or not isinstance(tree, dict)
        or tree.get("sha") != tree_sha
        or tree.get("truncated") is not False
        or not isinstance(tree.get("tree"), list)
        or len(tree["tree"]) > MAX_TREE_ENTRIES
    ):
        raise ValueError("source_metadata_incomplete")
    selected, seen, excluded_count = [], set(), 0
    for item in tree["tree"]:
        path = item.get("path") if isinstance(item, dict) else None
        if (
            not isinstance(path, str)
            or not path
            or len(path) > 1024
            or path.startswith("/")
            or "\\" in path
            or ":" in path.split("/", 1)[0]
            or any(ord(character) < 32 for character in path)
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or path in seen
        ):
            raise ValueError("source_metadata_invalid")
        seen.add(path)
        if item.get("type") == "tree":
            continue
        if excluded_path(path, exclusions):
            excluded_count += 1
            continue
        if (
            item.get("type") != "blob"
            or item.get("mode") not in {"100644", "100755"}
            or not isinstance(item.get("sha"), str)
            or not re.fullmatch(r"[a-f0-9]{40}", item["sha"])
            or type(item.get("size")) is not int
            or item["size"] < 0
        ):
            raise ValueError("unsupported_source_entry")
        selected.append(item)
    return selected, excluded_count


def canonical_archive_bytes(selected):
    """Exact ZIP_STORED size, including ZIP64 records for large file counts."""
    return 22 + (76 if len(selected) > 65535 else 0) + sum(item["size"] + 76 + 2 * len(item["path"].encode("utf-8")) for item in selected)


def repository_capture_preflight(tree, tree_sha, exclusions, *, max_bytes=MAX_CONTENT, max_files=MAX_FILES):
    """Describe capture feasibility, never scan coverage or execution readiness."""
    if type(max_bytes) is not int or type(max_files) is not int or max_bytes < 1 or max_files < 1:
        raise ValueError("invalid_capture_limits")
    byte_limit, file_limit = min(max_bytes, REPOSITORY_MAX_BYTES), min(max_files, REPOSITORY_MAX_FILES)
    selected, excluded_count = selected_repository_metadata(tree, tree_sha, exclusions)
    total_bytes = sum(item["size"] for item in selected)
    # canonical_repository writes a seekable ZIP_STORED archive with no extras.
    archive_bytes = canonical_archive_bytes(selected)
    oversized = sorted(
        (
            {"path": item["path"], "bytes": item["size"]}
            for item in selected
            if item["size"] + 98 + 2 * len(item["path"].encode("utf-8")) > byte_limit
        ),
        key=lambda item: (-item["bytes"], item["path"]),
    )
    reasons = []
    if not selected:
        reasons.append("empty_source_selection")
    if len(selected) > file_limit:
        reasons.append("source_file_count_exceeded")
    if total_bytes > byte_limit:
        reasons.append("source_content_bytes_exceeded")
    if archive_bytes > byte_limit:
        reasons.append("source_archive_bytes_exceeded")
    return {
        "status": "exceeds_limits" if reasons else "within_limits",
        "metadata_coverage": "complete",
        "coverage_scope": "selected_paths" if exclusions else "whole_repository",
        "selected_files": len(selected),
        "selected_bytes": total_bytes,
        "archive_bytes": archive_bytes,
        "excluded_entries": excluded_count,
        "exclusions": exclusions,
        "limits": {"max_bytes": byte_limit, "max_files": file_limit},
        "reasons": reasons,
        "oversized_files": oversized[:MAX_DIAGNOSTIC_PATHS],
        "oversized_file_count": len(oversized),
        "oversized_files_truncated": len(oversized) > MAX_DIAGNOSTIC_PATHS,
    }
