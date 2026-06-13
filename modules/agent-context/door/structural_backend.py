"""Structural backend — read side for code-index.json (understand + impact verbs).

Reads pre-computed structural indexes from S3 (written by the ingestion pipeline's
cgc analyze step). Each repo has a code-index.json containing:
- definitions: list of symbols (function, class, etc.) with file/line/kind/signature
- call_graph: map of symbol → list of callers

This module provides importable functions (not welded to the server) so it can
later route through the AgentCore Gateway as a packaging change.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .acl import SearchHit

log = logging.getLogger(__name__)


def _normalize_repo_id(repo_id: str) -> str:
    """Normalize repo_id for S3 key lookup (org/repo → org-repo)."""
    return repo_id.replace("/", "-")


async def load_code_index(repo_id: str, *, s3_client: Any, bucket: str, prefix: str) -> dict:
    """Load a repo's code-index.json from S3.

    Returns the parsed JSON dict, or an empty dict on any error.
    """
    safe_name = _normalize_repo_id(repo_id)
    s3_key = f"{prefix}/{safe_name}/code-index.json"

    try:
        response = s3_client.get_object(Bucket=bucket, Key=s3_key)
        body = response["Body"].read()
        return json.loads(body)
    except s3_client.exceptions.NoSuchKey:
        log.debug("No code-index.json for repo %s at %s", repo_id, s3_key)
        return {}
    except Exception:
        log.warning("Failed to load code-index.json for %s", repo_id, exc_info=True)
        return {}


async def understand(
    target: str,
    *,
    s3_client: Any,
    bucket: str,
    prefix: str,
    depth: str = "overview",
) -> list[SearchHit]:
    """Understand a symbol, file, or module from the structural index.

    Parameters
    ----------
    target:
        Symbol or file path to understand. Format: "repo_id/path" or "repo_id::symbol".
    s3_client:
        boto3 S3 client.
    bucket:
        S3 bucket name.
    prefix:
        S3 key prefix for code-indexes.
    depth:
        Level of detail: "overview" (default) or "detailed".

    Returns
    -------
    List of SearchHit with structural information (definitions, callers, callees).
    """
    # Parse target: "org/repo/file" or "org/repo::symbol"
    repo_id, query_target = _parse_target(target)
    if not repo_id:
        return []

    index = await load_code_index(repo_id, s3_client=s3_client, bucket=bucket, prefix=prefix)
    if not index:
        return []

    definitions = index.get("definitions", [])
    call_graph = index.get("call_graph", {})

    results: list[SearchHit] = []

    # Search definitions matching the target
    for defn in definitions:
        symbol = defn.get("symbol", "")
        file_path = defn.get("file", "")

        if _matches_target(query_target, symbol, file_path):
            # Find callers and callees for this symbol
            full_key = f"{file_path}::{symbol}"
            callees = call_graph.get(full_key, [])
            callers = _find_callers(full_key, call_graph)

            data: dict[str, Any] = {
                "repo_id": repo_id,
                "file": file_path,
                "line": defn.get("line", 0),
                "symbol": symbol,
                "kind": defn.get("kind", ""),
                "signature": defn.get("signature", ""),
                "callers": callers if depth == "detailed" else callers[:3],
                "callees": callees if depth == "detailed" else callees[:3],
            }
            results.append(SearchHit(repo_name=repo_id, data=data))

    # If target is a file path, also return all definitions in that file
    if "/" in query_target and not results:
        file_defs = [d for d in definitions if d.get("file", "") == query_target]
        for defn in file_defs:
            data = {
                "repo_id": repo_id,
                "file": defn.get("file", ""),
                "line": defn.get("line", 0),
                "symbol": defn.get("symbol", ""),
                "kind": defn.get("kind", ""),
                "signature": defn.get("signature", ""),
            }
            results.append(SearchHit(repo_name=repo_id, data=data))

    return results


async def impact(
    target: str,
    *,
    s3_client: Any,
    bucket: str,
    prefix: str,
    cross_repo: bool = False,
    zoekt_backend: Any | None = None,
) -> list[SearchHit]:
    """Analyse what would be affected by changing a symbol, file, or pattern.

    Parameters
    ----------
    target:
        Symbol or file to analyze impact for. Format: "repo_id/path" or "repo_id::symbol".
    s3_client:
        boto3 S3 client.
    bucket:
        S3 bucket name.
    prefix:
        S3 key prefix for code-indexes.
    cross_repo:
        If True, also search for cross-repo references via Zoekt.
    zoekt_backend:
        Optional ZoektSearchBackend for cross-repo search.

    Returns
    -------
    List of SearchHit representing callers/dependents of the target.
    """
    repo_id, query_target = _parse_target(target)
    if not repo_id:
        return []

    index = await load_code_index(repo_id, s3_client=s3_client, bucket=bucket, prefix=prefix)
    if not index:
        return []

    definitions = index.get("definitions", [])
    call_graph = index.get("call_graph", {})

    results: list[SearchHit] = []

    # Find the target symbol(s)
    target_keys: list[str] = []
    for defn in definitions:
        symbol = defn.get("symbol", "")
        file_path = defn.get("file", "")
        if _matches_target(query_target, symbol, file_path):
            target_keys.append(f"{file_path}::{symbol}")

    # If target looks like a file path, include all symbols in that file
    if "/" in query_target and not target_keys:
        for defn in definitions:
            if defn.get("file", "") == query_target:
                target_keys.append(f"{defn['file']}::{defn['symbol']}")

    # Find all callers of the target symbols
    for key in target_keys:
        callers = _find_callers(key, call_graph)
        for caller_ref in callers:
            # Parse caller reference (format: "file::symbol")
            parts = caller_ref.split("::", 1)
            caller_file = parts[0] if parts else ""
            caller_symbol = parts[1] if len(parts) > 1 else ""

            data: dict[str, Any] = {
                "repo_id": repo_id,
                "file": caller_file,
                "symbol": caller_symbol,
                "relationship": "calls",
                "target": key,
            }
            results.append(SearchHit(repo_name=repo_id, data=data))

    # Cross-repo search via Zoekt (if enabled and backend available)
    if cross_repo and zoekt_backend and query_target:
        # Search for the symbol name across all repos
        symbol_name = query_target.split("::")[-1] if "::" in query_target else query_target
        try:
            xrepo_hits = await zoekt_backend.search(symbol_name, limit=20)
            # Filter out hits from the same repo (already covered by call graph)
            for hit in xrepo_hits:
                if hit.repo_name != repo_id:
                    hit.data["relationship"] = "cross_repo_reference"
                    results.append(hit)
        except Exception:
            log.warning("Cross-repo search failed for %s", target, exc_info=True)

    return results


def _parse_target(target: str) -> tuple[str, str]:
    """Parse a target string into (repo_id, query_target).

    Formats:
    - "org/repo::symbol" → ("org/repo", "symbol")
    - "org/repo/path/to/file.py" → ("org/repo", "path/to/file.py")
    - "org/repo/path::symbol" → ("org/repo", "path::symbol")
    """
    if not target:
        return ("", "")

    # Check for :: separator (symbol reference)
    if "::" in target:
        # "org/repo::symbol" or "org/repo/file::symbol"
        before_symbol, symbol = target.rsplit("::", 1)
        # Extract repo_id (first two path components)
        parts = before_symbol.split("/")
        if len(parts) >= 2:
            repo_id = f"{parts[0]}/{parts[1]}"
            remaining = "/".join(parts[2:]) if len(parts) > 2 else ""
            if remaining:
                return (repo_id, f"{remaining}::{symbol}")
            return (repo_id, symbol)
        return ("", "")

    # Path-based target: "org/repo/path/to/file"
    parts = target.split("/")
    if len(parts) >= 2:
        repo_id = f"{parts[0]}/{parts[1]}"
        remaining = "/".join(parts[2:]) if len(parts) > 2 else ""
        return (repo_id, remaining)

    return ("", "")


def _matches_target(query_target: str, symbol: str, file_path: str) -> bool:
    """Check if a definition matches the query target."""
    if not query_target:
        return False

    # Exact symbol match
    if query_target == symbol:
        return True

    # File path match
    if query_target == file_path:
        return True

    # Partial match (symbol contains target or target contains symbol)
    if query_target.lower() in symbol.lower():
        return True

    # "file::symbol" format
    if "::" in query_target:
        file_part, sym_part = query_target.rsplit("::", 1)
        if file_path.endswith(file_part) and sym_part == symbol:
            return True

    return False


def _find_callers(target_key: str, call_graph: dict[str, list[str]]) -> list[str]:
    """Find all symbols that call the target (reverse lookup in call graph)."""
    callers: list[str] = []
    for caller, callees in call_graph.items():
        if target_key in callees:
            callers.append(caller)
    return callers
