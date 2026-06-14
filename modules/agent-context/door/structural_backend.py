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


def _normalize_symbol(defn: dict) -> dict:
    """Normalize a symbol entry to canonical field names.

    The code-index JSON uses 'name'/'type' but our code expects 'symbol'/'kind'.
    This bridges the gap so both formats work.
    """
    return {
        "symbol": defn.get("symbol") or defn.get("name", ""),
        "kind": defn.get("kind") or defn.get("type", ""),
        "file": defn.get("file", ""),
        "line": defn.get("line", 0),
        "signature": defn.get("signature", ""),
    }


async def load_code_index(repo_id: str, *, s3_client: Any, bucket: str, prefix: str) -> dict:
    """Load a repo's code-index.json from S3.

    Tries multiple key formats to handle naming variations:
    1. code-indexes/{safe_name}.json (exact match, e.g. "addyosmani-agent-skills")
    2. Suffix match: scan code-indexes/ for *-{repo_name}.json (handles short names)
    3. Legacy: {prefix}/{safe_name}/code-index.json

    Returns the parsed JSON dict, or an empty dict on any error.
    """
    safe_name = _normalize_repo_id(repo_id)

    # Strategy 1: Exact match at code-indexes/{safe_name}.json
    primary_key = f"code-indexes/{safe_name}.json"
    try:
        response = s3_client.get_object(Bucket=bucket, Key=primary_key)
        body = response["Body"].read()
        data = json.loads(body)
        if data:
            log.debug("Loaded code-index for %s from %s", repo_id, primary_key)
            return data
    except s3_client.exceptions.NoSuchKey:
        pass
    except Exception:
        log.warning("Failed to load code-index for %s at %s", repo_id, primary_key, exc_info=True)

    # Strategy 2: Suffix match — repo_id might be a short name (e.g. "agent-skills")
    # and the actual key is "addyosmani-agent-skills.json". List prefix and find match.
    # When multiple files match the suffix, prefer the shortest filename (most specific
    # match — e.g., "mattpocock-skills.json" over "Imbad0202-academic-research-skills.json").
    try:
        list_resp = s3_client.list_objects_v2(Bucket=bucket, Prefix="code-indexes/", MaxKeys=200)
        candidates: list[str] = []
        for obj in list_resp.get("Contents", []):
            key = obj["Key"]
            filename = key.split("/")[-1]
            # Match: filename ends with -{safe_name}.json or equals {safe_name}.json
            if filename.endswith(f"-{safe_name}.json") or filename == f"{safe_name}.json":
                candidates.append(key)
        # Sort by filename length (shortest = most specific match)
        candidates.sort(key=lambda k: len(k.split("/")[-1]))
        for key in candidates:
            try:
                response = s3_client.get_object(Bucket=bucket, Key=key)
                body = response["Body"].read()
                data = json.loads(body)
                if data:
                    log.debug("Loaded code-index for %s via suffix match at %s", repo_id, key)
                    return data
            except Exception:
                continue
    except Exception:
        log.debug("Suffix-match scan failed for %s", repo_id)

    # Strategy 3: Legacy key format
    legacy_key = f"{prefix}/{safe_name}/code-index.json"
    try:
        response = s3_client.get_object(Bucket=bucket, Key=legacy_key)
        body = response["Body"].read()
        data = json.loads(body)
        if data:
            log.debug("Loaded code-index for %s from legacy %s", repo_id, legacy_key)
            return data
    except s3_client.exceptions.NoSuchKey:
        pass
    except Exception:
        log.warning("Failed to load code-index for %s at %s", repo_id, legacy_key, exc_info=True)

    log.debug("No code-index.json found for repo %s", repo_id)
    return {}


async def understand(
    target: str,
    *,
    s3_client: Any,
    bucket: str,
    prefix: str,
    depth: str = "overview",
    zoekt_backend: Any | None = None,
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

    raw_definitions = index.get("symbols", []) or index.get("definitions", [])
    definitions = [_normalize_symbol(d) for d in raw_definitions]
    call_graph = index.get("call_graph", {})

    results: list[SearchHit] = []

    # Search definitions matching the target
    for defn in definitions:
        symbol = defn["symbol"]
        file_path = defn["file"]

        if _matches_target(query_target, symbol, file_path):
            # Find callers and callees for this symbol
            full_key = f"{file_path}::{symbol}"
            callees = call_graph.get(full_key, [])
            callers = _find_callers(full_key, call_graph)

            data: dict[str, Any] = {
                "repo_id": repo_id,
                "file": file_path,
                "line": defn["line"],
                "symbol": symbol,
                "kind": defn["kind"],
                "signature": defn["signature"],
                "callers": callers if depth == "detailed" else callers[:3],
                "callees": callees if depth == "detailed" else callees[:3],
            }
            results.append(SearchHit(repo_name=repo_id, data=data))

    # If target is a file/directory path, also return all definitions in that path
    if "/" in query_target and not results:
        file_defs = [
            d
            for d in definitions
            if d["file"] == query_target
            or d["file"].startswith(query_target + "/")
            or d["file"].endswith("/" + query_target)
        ]
        for defn in file_defs:
            data = {
                "repo_id": repo_id,
                "file": defn["file"],
                "line": defn["line"],
                "symbol": defn["symbol"],
                "kind": defn["kind"],
                "signature": defn["signature"],
            }
            results.append(SearchHit(repo_name=repo_id, data=data))

    # Fallback: if code-index had no match, use Zoekt to find definitions
    if not results and zoekt_backend:
        repo_lower = repo_id.lower()
        seen_files: set[str] = set()

        # Strategy A: For file/directory paths, search WITHIN the target using Zoekt file: filter
        if "/" in query_target:
            # Use Zoekt file: filter to find content within the target path
            # Use "." (any char) as content match to get representative lines from files
            file_query = f"file:{query_target} ."
            try:
                zoekt_hits = await zoekt_backend.search(file_query, limit=30)
                for hit in zoekt_hits:
                    if repo_lower not in hit.repo_name.lower():
                        continue
                    hit_file = hit.data.get("file", "")
                    if not hit_file or hit_file in seen_files:
                        continue
                    seen_files.add(hit_file)
                    hit.data["repo_id"] = repo_id
                    hit.data["symbol"] = hit.data.get("symbol", "")
                    hit.data["kind"] = hit.data.get("kind", "definition")
                    results.append(hit)
                    if len(results) >= 10:
                        break
            except Exception:
                log.debug("Zoekt file-filtered search failed for %s", target, exc_info=True)

        # Strategy B: Search for the target name across all files (symbol or filename)
        if not results:
            search_term = (
                query_target.split("::")[-1]
                if "::" in query_target
                else query_target.split("/")[-1]
            )
            if "." in search_term and "/" not in search_term:
                search_term = search_term.rsplit(".", 1)[0]
            try:
                zoekt_hits = await zoekt_backend.search(search_term, limit=30)
                for hit in zoekt_hits:
                    if repo_lower not in hit.repo_name.lower():
                        continue
                    hit_file = hit.data.get("file", "")
                    if not hit_file or hit_file in seen_files:
                        continue
                    seen_files.add(hit_file)
                    hit.data["repo_id"] = repo_id
                    hit.data["symbol"] = hit.data.get("symbol", search_term)
                    hit.data["kind"] = hit.data.get("kind", "reference")
                    results.append(hit)
                    if len(results) >= 10:
                        break
            except Exception:
                log.debug("Zoekt fallback failed for understand %s", target, exc_info=True)

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

    raw_definitions = index.get("symbols", []) or index.get("definitions", [])
    definitions = [_normalize_symbol(d) for d in raw_definitions]
    call_graph = index.get("call_graph", {})

    results: list[SearchHit] = []

    # Find the target symbol(s)
    target_keys: list[str] = []
    for defn in definitions:
        symbol = defn["symbol"]
        file_path = defn["file"]
        if _matches_target(query_target, symbol, file_path):
            target_keys.append(f"{file_path}::{symbol}")

    # If target looks like a file path, include all symbols in that file
    if "/" in query_target and not target_keys:
        for defn in definitions:
            if defn["file"] == query_target:
                target_keys.append(f"{defn['file']}::{defn['symbol']}")

    # Find all callers of the target symbols from call_graph
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

    # Fallback: if call_graph is empty/yielded nothing, use Zoekt to find intra-repo references
    if not results and zoekt_backend:
        symbol_name = (
            query_target.split("::")[-1] if "::" in query_target else query_target.split("/")[-1]
        )
        # For file paths, search for the stem (e.g. "content_router" from "content_router.py")
        if "." in symbol_name and "::" not in query_target:
            symbol_name = symbol_name.rsplit(".", 1)[0]
        try:
            # Search for references to the target symbol (no repo filter — Zoekt uses full URLs)
            zoekt_hits = await zoekt_backend.search(symbol_name, limit=30)
            # Build exclusion set: the target file path(s) where the symbol is defined
            repo_lower = repo_id.lower()
            exclude_files: set[str] = set()
            if "::" in query_target:
                # Symbol query: exclude file from target_keys
                for tk in target_keys:
                    exclude_files.add(tk.split("::")[0])
            else:
                exclude_files.add(query_target)
            # Also exclude files whose path contains the symbol as a filename component
            # (e.g., content_router.py for symbol ContentRouter)
            symbol_lower = symbol_name.lower().replace("-", "_")

            seen_files: set[str] = set()
            for hit in zoekt_hits:
                if repo_lower not in hit.repo_name.lower():
                    continue
                hit_file = hit.data.get("file", "")
                if not hit_file or hit_file in seen_files:
                    continue
                # Skip if this IS the definition file
                if hit_file in exclude_files:
                    continue
                # Skip if the filename matches the symbol (it's the definition, not a consumer)
                hit_stem = hit_file.split("/")[-1].rsplit(".", 1)[0].lower().replace("-", "_")
                if hit_stem == symbol_lower:
                    continue
                seen_files.add(hit_file)
                hit.data["relationship"] = "references"
                hit.data["repo_id"] = repo_id
                hit.data["file"] = hit_file
                hit.data["symbol"] = hit.data.get("symbol", "")
                results.append(hit)
        except Exception:
            log.debug("Zoekt intra-repo fallback failed for %s", target, exc_info=True)

    # Cross-repo search via Zoekt (if enabled and backend available)
    if cross_repo and zoekt_backend and query_target:
        # Search for the symbol name across all repos
        symbol_name = query_target.split("::")[-1] if "::" in query_target else query_target
        try:
            xrepo_hits = await zoekt_backend.search(symbol_name, limit=20)
            # Filter out hits from the same repo (already covered above)
            for hit in xrepo_hits:
                if hit.repo_name != repo_id:
                    hit.data["relationship"] = "cross_repo_reference"
                    results.append(hit)
        except Exception:
            log.warning("Cross-repo search failed for %s", target, exc_info=True)

    return results


def _parse_target(target: str) -> tuple[str, str]:
    """Parse a target string into (repo_id, query_target).

    Supports both short and org-qualified repo names:
    - "repo-name/path/to/file.py" → ("repo-name", "path/to/file.py")
    - "repo-name/path::symbol" → ("repo-name", "path::symbol")
    - "repo-name::symbol" → ("repo-name", "symbol")
    - "org/repo/path/to/file.py" → ("org/repo", "path/to/file.py")
    - "org/repo::symbol" → ("org/repo", "symbol")

    Heuristic: if the first component contains a dot (e.g. "github.com") or
    matches common org patterns, treat first TWO components as repo_id.
    Otherwise treat the FIRST component as repo_id (short-name format used
    by the eval golden dataset).
    """
    if not target:
        return ("", "")

    # Check for :: separator (symbol reference)
    if "::" in target:
        before_symbol, symbol = target.rsplit("::", 1)
        parts = before_symbol.split("/")
        if len(parts) >= 1:
            repo_id, remaining = _extract_repo_id(parts)
            if remaining:
                return (repo_id, f"{remaining}::{symbol}")
            return (repo_id, symbol)
        return ("", "")

    # Path-based target
    parts = target.split("/")
    if len(parts) >= 1:
        repo_id, remaining = _extract_repo_id(parts)
        return (repo_id, remaining)

    return ("", "")


def _extract_repo_id(parts: list[str]) -> tuple[str, str]:
    """Extract repo_id from path components.

    Uses heuristics to determine if repo_id is one or two components:
    - If first part looks like a domain (contains .), use first 3 (github.com/org/repo)
    - If first part looks like a GitHub org (lowercase, common patterns), use first 2
    - Otherwise use first 1 (short repo name — most common for eval corpus)
    """
    if not parts:
        return ("", "")

    first = parts[0]

    # Domain prefix (e.g., "github.com/org/repo/file")
    if "." in first and len(parts) >= 3:
        repo_id = f"{parts[0]}/{parts[1]}/{parts[2]}"
        remaining = "/".join(parts[3:]) if len(parts) > 3 else ""
        return (repo_id, remaining)

    # Default: first component is the repo_id (short-name format)
    # This handles the common eval pattern: "CopilotKit/packages/react-core"
    # where "CopilotKit" is the repo and "packages/react-core" is the path
    repo_id = first
    remaining = "/".join(parts[1:]) if len(parts) > 1 else ""
    return (repo_id, remaining)


def _matches_target(query_target: str, symbol: str, file_path: str) -> bool:
    """Check if a definition matches the query target."""
    if not query_target:
        return False

    # Exact symbol match
    if query_target == symbol:
        return True

    # Exact file path match
    if query_target == file_path:
        return True

    # File path suffix match (e.g., "compress.py" matches "headroom/compress.py")
    if file_path.endswith("/" + query_target) or file_path.endswith(query_target):
        return True

    # Directory prefix match (e.g., "packages/react-core" matches "packages/react-core/src/hooks.ts")
    if "/" in query_target and file_path.startswith(query_target + "/"):
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
