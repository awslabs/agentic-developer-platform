"""Structural backend — read side for code-index.json (understand + impact verbs).

Primary path: Neptune openCypher queries (bounded transitive call-graph for impact,
symbol neighborhood for understand). Falls back to code-index.json from S3 when
Neptune is unreachable or disabled.

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

    Primary path: Neptune openCypher (symbol neighborhood, module topology).
    Fallback: code-index.json from S3 when Neptune is unreachable or disabled.

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

    # --- Neptune path (primary) ---
    neptune_results = await _understand_via_neptune(repo_id, query_target, depth)
    if neptune_results is not None:
        return neptune_results

    # --- Fallback: code-index.json from S3 ---
    return await _understand_via_code_index(
        repo_id,
        query_target,
        target,
        depth,
        s3_client=s3_client,
        bucket=bucket,
        prefix=prefix,
        zoekt_backend=zoekt_backend,
    )


async def _understand_via_neptune(
    repo_id: str, query_target: str, depth: str
) -> list[SearchHit] | None:
    """Attempt understand via Neptune. Returns None if Neptune unavailable.

    Returns a list of SearchHit on success, or None to signal fallback.
    None means "Neptune doesn't have this data, try S3 fallback."
    Empty list [] means "Neptune handled it but found nothing" (no fallback needed).

    Catches NeptuneQueryError and falls back to S3 (returns None) so that
    query failures don't crash the verb handler. Bug #1611.
    """
    from . import neptune_client
    from .neptune_client import NeptuneQueryError

    if not neptune_client.neptune_enabled():
        return None

    if not neptune_client.neptune_available():
        return None

    # Resolve short repo name to full org/repo format for Neptune queries
    repo_id = neptune_client.resolve_repo_name(repo_id)

    try:
        return _understand_neptune_inner(neptune_client, repo_id, query_target, depth)
    except NeptuneQueryError:
        log.error(
            "Neptune understand query error for %s / %s — falling back to S3",
            repo_id,
            query_target,
        )
        return None


def _understand_neptune_inner(
    neptune_client: Any, repo_id: str, query_target: str, depth: str
) -> list[SearchHit] | None:
    """Inner logic for _understand_via_neptune (separated for NeptuneQueryError handling)."""

    results: list[SearchHit] = []

    # Determine target type: repo-level, file, directory, or symbol
    if not query_target:
        # Repo-level target (e.g., understand("codegraph"))
        records = neptune_client.query_repo_topology(repo_id)
        if not records:
            return None  # Fall back if Neptune has no data for this repo
        for rec in records:
            data: dict[str, Any] = {
                "repo_id": repo_id,
                "module": rec.get("module_path", ""),
                "files": rec.get("files", []),
                "symbol_count": rec.get("symbol_count", 0),
                "kind": "module",
                "source": "neptune",
            }
            results.append(SearchHit(repo_name=repo_id, data=data))
        return results

    # Symbol reference (contains ::)
    if "::" in query_target:
        file_part, symbol_name = query_target.rsplit("::", 1)
        file_path = file_part if file_part else ""
        records = neptune_client.query_understand(repo_id, file_path, symbol_name)
        if not records:
            # Try symbol resolution before giving up
            resolved = neptune_client.resolve_symbol(repo_id, symbol_name)
            if resolved:
                # Re-query with the resolved file
                records = neptune_client.query_understand(
                    repo_id, resolved[0]["file"], resolved[0]["name"]
                )
        if not records:
            return None  # Fall back
        for rec in records:
            # Filter out null entries from collect() aggregations
            callees = _filter_null_collect(rec.get("callees", []))
            callers = _filter_null_collect(rec.get("callers", []))
            parents = _filter_null_collect(rec.get("parents", []))
            owners = _filter_null_collect(rec.get("owners", []))
            data = {
                "repo_id": repo_id,
                "file": rec.get("symbol_file", file_path),
                "symbol": rec.get("symbol_name", symbol_name),
                "kind": rec.get("symbol_kind", ""),
                "signature": rec.get("signature", ""),
                "callers": callers if depth == "detailed" else callers[:3],
                "callees": callees if depth == "detailed" else callees[:3],
                "parents": parents,
                "owners": owners,
                "source": "neptune",
            }
            results.append(SearchHit(repo_name=repo_id, data=data))
        return results

    # File path target
    if "." in query_target.split("/")[-1] if "/" in query_target else False:
        records = neptune_client.query_file_symbols(repo_id, query_target)
        if not records:
            return None  # Fall back
        for rec in records:
            data = {
                "repo_id": repo_id,
                "file": query_target,
                "symbol": rec.get("name", ""),
                "kind": rec.get("kind", ""),
                "line": rec.get("line", 0),
                "signature": rec.get("signature", ""),
                "source": "neptune",
            }
            results.append(SearchHit(repo_name=repo_id, data=data))
        return results

    # Directory path target (contains / but no file extension in last part)
    if "/" in query_target:
        records = neptune_client.query_dir_symbols(repo_id, query_target)
        if not records:
            # Try interpreting as module-path symbol (e.g. "cli/main")
            resolved = neptune_client.resolve_symbol(repo_id, query_target)
            if resolved:
                records = neptune_client.query_understand(
                    repo_id, resolved[0]["file"], resolved[0]["name"]
                )
                if records:
                    for rec in records:
                        callees = _filter_null_collect(rec.get("callees", []))
                        callers = _filter_null_collect(rec.get("callers", []))
                        parents = _filter_null_collect(rec.get("parents", []))
                        owners = _filter_null_collect(rec.get("owners", []))
                        data = {
                            "repo_id": repo_id,
                            "file": rec.get("symbol_file", ""),
                            "symbol": rec.get("symbol_name", ""),
                            "kind": rec.get("symbol_kind", ""),
                            "signature": rec.get("signature", ""),
                            "callers": callers if depth == "detailed" else callers[:3],
                            "callees": callees if depth == "detailed" else callees[:3],
                            "parents": parents,
                            "owners": owners,
                            "source": "neptune",
                        }
                        results.append(SearchHit(repo_name=repo_id, data=data))
                    return results
            return None  # Fall back
        for rec in records:
            data = {
                "repo_id": repo_id,
                "file": rec.get("file", ""),
                "symbol": rec.get("name", ""),
                "kind": rec.get("kind", ""),
                "line": rec.get("line", 0),
                "source": "neptune",
            }
            results.append(SearchHit(repo_name=repo_id, data=data))
        return results

    # Bare symbol name (no :: separator, no path separators)
    # With empty file, query_understand now correctly omits file from MATCH (#1587)
    records = neptune_client.query_understand(repo_id, "", query_target)
    if records:
        for rec in records:
            callees = _filter_null_collect(rec.get("callees", []))
            callers = _filter_null_collect(rec.get("callers", []))
            parents = _filter_null_collect(rec.get("parents", []))
            owners = _filter_null_collect(rec.get("owners", []))
            data = {
                "repo_id": repo_id,
                "file": rec.get("symbol_file", ""),
                "symbol": rec.get("symbol_name", query_target),
                "kind": rec.get("symbol_kind", ""),
                "signature": rec.get("signature", ""),
                "callers": callers if depth == "detailed" else callers[:3],
                "callees": callees if depth == "detailed" else callees[:3],
                "parents": parents,
                "owners": owners,
                "source": "neptune",
            }
            results.append(SearchHit(repo_name=repo_id, data=data))
        return results

    # Last resort: try resolve_symbol for fuzzy/module-path targets
    resolved = neptune_client.resolve_symbol(repo_id, query_target)
    if resolved:
        records = neptune_client.query_understand(repo_id, resolved[0]["file"], resolved[0]["name"])
        if records:
            for rec in records:
                callees = _filter_null_collect(rec.get("callees", []))
                callers = _filter_null_collect(rec.get("callers", []))
                parents = _filter_null_collect(rec.get("parents", []))
                owners = _filter_null_collect(rec.get("owners", []))
                data = {
                    "repo_id": repo_id,
                    "file": rec.get("symbol_file", ""),
                    "symbol": rec.get("symbol_name", query_target),
                    "kind": rec.get("symbol_kind", ""),
                    "signature": rec.get("signature", ""),
                    "callers": callers if depth == "detailed" else callers[:3],
                    "callees": callees if depth == "detailed" else callees[:3],
                    "parents": parents,
                    "owners": owners,
                    "source": "neptune",
                }
                results.append(SearchHit(repo_name=repo_id, data=data))
            return results

    return None  # Fall back


async def _understand_via_code_index(
    repo_id: str,
    query_target: str,
    target: str,
    depth: str,
    *,
    s3_client: Any,
    bucket: str,
    prefix: str,
    zoekt_backend: Any | None = None,
) -> list[SearchHit]:
    """Understand via code-index.json fallback (original implementation)."""
    index = await load_code_index(repo_id, s3_client=s3_client, bucket=bucket, prefix=prefix)
    if not index:
        return []

    raw_definitions = index.get("symbols", []) or index.get("definitions", [])
    definitions = [_normalize_symbol(d) for d in raw_definitions]
    call_graph = index.get("call_graph", {})

    results: list[SearchHit] = []

    # FIX: Repo-level target (empty query_target) — return all definitions as overview
    if not query_target:
        for defn in definitions[:50]:  # Cap at 50 for overview
            data: dict[str, Any] = {
                "repo_id": repo_id,
                "file": defn["file"],
                "line": defn["line"],
                "symbol": defn["symbol"],
                "kind": defn["kind"],
                "signature": defn["signature"],
                "source": "code-index-fallback",
            }
            results.append(SearchHit(repo_name=repo_id, data=data))
        return results

    # Search definitions matching the target
    for defn in definitions:
        symbol = defn["symbol"]
        file_path = defn["file"]

        if _matches_target(query_target, symbol, file_path):
            # Find callers and callees for this symbol
            full_key = f"{file_path}::{symbol}"
            callees = call_graph.get(full_key, [])
            callers = _find_callers(full_key, call_graph)

            data = {
                "repo_id": repo_id,
                "file": file_path,
                "line": defn["line"],
                "symbol": symbol,
                "kind": defn["kind"],
                "signature": defn["signature"],
                "callers": callers if depth == "detailed" else callers[:3],
                "callees": callees if depth == "detailed" else callees[:3],
                "source": "code-index-fallback",
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
                "source": "code-index-fallback",
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

    Primary path: Neptune openCypher (transitive callers via [:CALLS*1..4]).
    Fallback: code-index.json from S3 when Neptune is unreachable or disabled.

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

    # --- Neptune path (primary) ---
    neptune_results = await _impact_via_neptune(repo_id, query_target, cross_repo=cross_repo)
    if neptune_results is not None:
        return neptune_results

    # --- Fallback: code-index.json from S3 ---
    return await _impact_via_code_index(
        repo_id,
        query_target,
        target,
        s3_client=s3_client,
        bucket=bucket,
        prefix=prefix,
        cross_repo=cross_repo,
        zoekt_backend=zoekt_backend,
    )


async def _impact_via_neptune(
    repo_id: str, query_target: str, *, cross_repo: bool = False
) -> list[SearchHit] | None:
    """Attempt impact analysis via Neptune. Returns None if Neptune unavailable.

    Return semantics (Bug #1587 Fix — distinguish lookup miss from no callers):
    - None: Neptune not available OR symbol not found in Neptune → trigger S3 fallback
    - [] (empty list): symbol EXISTS in Neptune but has zero callers → true negative,
      do NOT fall back. Response should report source="neptune", verdict="no_callers".

    Catches NeptuneQueryError and falls back to S3 (returns None) so that
    query failures don't crash the verb handler. Bug #1611.
    """
    from . import neptune_client
    from .neptune_client import NeptuneQueryError

    if not neptune_client.neptune_enabled():
        return None

    if not neptune_client.neptune_available():
        return None

    # Resolve short repo name to full org/repo format for Neptune queries
    repo_id = neptune_client.resolve_repo_name(repo_id)

    try:
        return _impact_neptune_inner(neptune_client, repo_id, query_target, cross_repo=cross_repo)
    except NeptuneQueryError:
        log.error(
            "Neptune impact query error for %s / %s — falling back to S3",
            repo_id,
            query_target,
        )
        return None


def _impact_neptune_inner(
    neptune_client: Any,
    repo_id: str,
    query_target: str,
    *,
    cross_repo: bool = False,
) -> list[SearchHit] | None:
    """Inner logic for _impact_via_neptune (separated for NeptuneQueryError handling)."""

    # Parse symbol reference
    if "::" in query_target:
        file_part, symbol_name = query_target.rsplit("::", 1)
    elif "/" in query_target:
        # File path — impact on a file means impact on all symbols in it
        file_part = query_target
        symbol_name = ""
    else:
        # Bare symbol name
        file_part = ""
        symbol_name = query_target

    if not symbol_name:
        # File-level impact: get symbols in file, then query callers for each
        file_symbols = neptune_client.query_file_symbols(repo_id, file_part)
        if not file_symbols:
            return None  # Fall back — file not in Neptune

        results: list[SearchHit] = []
        for sym in file_symbols[:10]:  # Cap at 10 symbols to avoid explosion
            records = neptune_client.query_impact(repo_id, file_part, sym.get("name", ""))
            for rec in records:
                data: dict[str, Any] = {
                    "repo_id": rec.get("caller_repo", repo_id),
                    "file": rec.get("caller_file", ""),
                    "symbol": rec.get("caller_name", ""),
                    "kind": rec.get("caller_kind", ""),
                    "relationship": "calls",
                    "distance": rec.get("distance", 1),
                    "target": f"{file_part}::{sym.get('name', '')}",
                    "source": "neptune",
                }
                results.append(SearchHit(repo_name=rec.get("caller_repo", repo_id), data=data))
        # Return results (even empty []) — file exists in Neptune, this is authoritative
        return results

    # --- Symbol-level impact ---
    # First try direct query (works when file_part is non-empty or after #1587
    # fix which handles empty file in query_impact)
    records = neptune_client.query_impact(repo_id, file_part, symbol_name)

    # If no results and file was empty, try resolve_symbol to find the file
    if not records and not file_part:
        resolved = neptune_client.resolve_symbol(repo_id, symbol_name)
        if resolved:
            # Re-query with the resolved file path for a precise match
            file_part = resolved[0]["file"]
            symbol_name = resolved[0]["name"]
            records = neptune_client.query_impact(repo_id, file_part, symbol_name)

    # Determine whether the symbol exists at all (distinguishes "not found" from "no callers")
    if not records:
        exists = neptune_client.symbol_exists(repo_id, file_part, symbol_name)
        if not exists:
            # Symbol not in Neptune at all → fall back to S3
            log.debug(
                "impact: symbol %s::%s not found in Neptune repo %s, falling back",
                file_part,
                symbol_name,
                repo_id,
            )
            return None
        # Symbol exists but has no callers — this is a true negative from Neptune.
        # Return a metadata-only hit (NOT None, NOT empty []) so the server can
        # correctly report source="neptune" and verdict="no_callers". Bug #1587.
        log.debug(
            "impact: symbol %s::%s exists in Neptune repo %s but has no callers",
            file_part,
            symbol_name,
            repo_id,
        )
        # Return a sentinel hit that carries source="neptune" and a marker.
        # The server's verdict logic will see blast_radius=0 + source="neptune"
        # → verdict="no_callers" (not "symbol_not_found").
        sentinel_data: dict[str, Any] = {
            "repo_id": repo_id,
            "file": file_part,
            "symbol": symbol_name,
            "kind": "",
            "relationship": "none",
            "target": f"{file_part}::{symbol_name}",
            "source": "neptune",
            "_neptune_no_callers": True,
        }
        return [SearchHit(repo_name=repo_id, data=sentinel_data)]

    results: list[SearchHit] = []
    for rec in records:
        data: dict[str, Any] = {
            "repo_id": rec.get("caller_repo", repo_id),
            "file": rec.get("caller_file", ""),
            "symbol": rec.get("caller_name", ""),
            "kind": rec.get("caller_kind", ""),
            "relationship": "calls",
            "distance": rec.get("distance", 1),
            "target": f"{file_part}::{symbol_name}",
            "source": "neptune",
        }
        results.append(SearchHit(repo_name=rec.get("caller_repo", repo_id), data=data))

    # Cross-repo impact via symbol_id join (Neptune-native, NOT Zoekt name-match)
    if cross_repo and symbol_name:
        xrepo_records = neptune_client.query_cross_repo_impact(repo_id, file_part, symbol_name)
        for rec in xrepo_records:
            data = {
                "repo_id": rec.get("calling_repo", ""),
                "file": rec.get("calling_file", ""),
                "symbol": rec.get("calling_symbol", ""),
                "kind": rec.get("calling_kind", ""),
                "relationship": "cross_repo_reference",
                "target": f"{file_part}::{symbol_name}",
                "source": "neptune",
            }
            results.append(SearchHit(repo_name=rec.get("calling_repo", ""), data=data))

    return results


async def _impact_via_code_index(
    repo_id: str,
    query_target: str,
    target: str,
    *,
    s3_client: Any,
    bucket: str,
    prefix: str,
    cross_repo: bool = False,
    zoekt_backend: Any | None = None,
) -> list[SearchHit]:
    """Impact analysis via code-index.json fallback (original implementation)."""
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
                "source": "code-index-fallback",
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


def _filter_null_collect(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter out null/empty entries from Neptune collect() results.

    Neptune's OPTIONAL MATCH + collect(DISTINCT ...) can produce entries
    where all values are None (when no match exists). Remove those.
    """
    return [item for item in items if item and any(v is not None for v in item.values())]


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
