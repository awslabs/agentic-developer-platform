"""Neptune openCypher client for the Door query layer.

Provides lazy driver initialization, availability checking, and typed
query methods for the impact and understand verbs. Falls back gracefully
when Neptune is unreachable.

See: docs/agent-context/neptune-deep-graph-design.md (Door Query Patterns)
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

_driver = None
_repo_name_cache: dict[str, str] = {}  # short-name → org/repo mapping


def get_neptune_driver():
    """Lazy-init Neptune driver with IAM auth and connection pooling.

    Returns the cached driver instance, or None if NEPTUNE_ENDPOINT is not set.
    Thread-safe: neo4j driver handles internal synchronization.
    """
    global _driver
    if _driver is None:
        from .neptune_auth import create_neptune_driver

        _driver = create_neptune_driver(
            max_pool=25,
            acquire_timeout=30.0,
            connection_timeout=5.0,
        )
    return _driver


def neptune_enabled() -> bool:
    """Check if Neptune feature flag is enabled."""
    return os.environ.get("NEPTUNE_ENABLED", "false").lower() in ("true", "1", "yes")


def neptune_available() -> bool:
    """Check if Neptune is reachable (for fallback decision).

    Returns False if:
    - Feature flag NEPTUNE_ENABLED is not set
    - Driver could not be created (no endpoint configured)
    - Connection test fails (Neptune unreachable)
    """
    if not neptune_enabled():
        return False

    driver = get_neptune_driver()
    if not driver:
        return False

    try:
        with driver.session() as session:
            session.run("RETURN 1").consume()
        return True
    except Exception:
        log.warning("Neptune unreachable - falling back to code-index.json")
        return False


def resolve_repo_name(repo: str) -> str:
    """Resolve a short repo name to full org/repo format used in Neptune.

    The eval golden dataset and MCP callers use short names (e.g. "headroom")
    while Neptune stores full qualified names (e.g. "chopratejas/headroom").
    This function maps between the two using a cached Neptune lookup.

    If repo already contains "/" (i.e., is already org/repo), returns it as-is.
    Returns the original name if no match found in Neptune.
    """
    if "/" in repo:
        return repo  # Already qualified

    if repo in _repo_name_cache:
        return _repo_name_cache[repo]

    driver = get_neptune_driver()
    if not driver:
        return repo

    # Find repos ending with the short name (suffix match)
    cypher = """
        MATCH (s:Symbol)
        WITH DISTINCT s.repo AS repo_name
        WHERE repo_name ENDS WITH $suffix
        RETURN repo_name
        LIMIT 5
    """
    try:
        with driver.session() as session:
            result = session.run(cypher, {"suffix": f"/{repo}"})
            matches = [record["repo_name"] for record in result]
            if len(matches) == 1:
                _repo_name_cache[repo] = matches[0]
                log.debug("Resolved repo '%s' → '%s'", repo, matches[0])
                return matches[0]
            elif matches:
                # Multiple matches — prefer shortest (most specific)
                best = min(matches, key=len)
                _repo_name_cache[repo] = best
                log.debug(
                    "Resolved repo '%s' → '%s' (from %d candidates)", repo, best, len(matches)
                )
                return best
    except Exception:
        log.debug("Failed to resolve repo name '%s' via Neptune", repo)

    _repo_name_cache[repo] = repo  # Cache miss to avoid repeated queries
    return repo


def resolve_symbol(
    repo: str,
    target: str,
) -> list[dict[str, Any]]:
    """Resolve a human-friendly target to Neptune Symbol node(s).

    Agents and eval datasets pass targets like "main", "ContentRouter",
    "cli/main", or "agent_reach.cli/main()." — none of which directly match
    the stored (repo, file, name) triple. This function maps them.

    Resolution strategies (tried in order):
    1. Exact name match: MATCH (s:Symbol {repo, name: target})
    2. Suffix/contains match: WHERE s.name CONTAINS target
    3. File-stem interpretation: treat "module.sub/func" as file-path hint

    Parameters
    ----------
    repo:
        Repository identifier (already resolved via resolve_repo_name).
    target:
        Human-friendly symbol name, bare or with path hints.

    Returns
    -------
    List of {name, file, kind, symbol_id} dicts (up to 10 matches).
    Empty list if nothing found (caller distinguishes from no-callers).
    """
    driver = get_neptune_driver()
    if not driver:
        return []

    # Clean target: strip trailing dots/parens (SCIP descriptor noise)
    clean_target = target.rstrip("().")

    # Strategy 1: Exact name match
    cypher_exact = """
        MATCH (s:Symbol {repo: $repo, name: $name})
        RETURN s.name AS name, s.file AS file, s.kind AS kind,
               s.symbol_id AS symbol_id
        LIMIT 10
    """
    try:
        with driver.session() as session:
            result = session.run(cypher_exact, {"repo": repo, "name": clean_target})
            records = [dict(r) for r in result]
            if records:
                return records
    except Exception:
        log.debug("resolve_symbol exact match failed for %s in %s", target, repo)

    # Strategy 2: Case-insensitive contains match (for partial names)
    cypher_contains = """
        MATCH (s:Symbol {repo: $repo})
        WHERE toLower(s.name) CONTAINS toLower($name)
        RETURN s.name AS name, s.file AS file, s.kind AS kind,
               s.symbol_id AS symbol_id
        ORDER BY size(s.name) ASC
        LIMIT 10
    """
    try:
        with driver.session() as session:
            result = session.run(cypher_contains, {"repo": repo, "name": clean_target})
            records = [dict(r) for r in result]
            if records:
                return records
    except Exception:
        log.debug("resolve_symbol contains match failed for %s in %s", target, repo)

    # Strategy 3: If target has "/" or ".", try interpreting as module path
    # e.g. "agent_reach.cli/main" → file contains "agent_reach/cli" + name="main"
    if "/" in target or "." in target:
        # Split on last "/" to get (path_hint, symbol_hint)
        if "/" in target:
            path_hint, symbol_hint = target.rsplit("/", 1)
        else:
            path_hint, symbol_hint = target.rsplit(".", 1)
        # Convert dots to path separators for file matching
        file_hint = path_hint.replace(".", "/")
        symbol_hint = symbol_hint.rstrip("().")

        if symbol_hint:
            cypher_path = """
                MATCH (s:Symbol {repo: $repo})
                WHERE s.file CONTAINS $file_hint AND s.name = $name
                RETURN s.name AS name, s.file AS file, s.kind AS kind,
                       s.symbol_id AS symbol_id
                LIMIT 10
            """
            try:
                with driver.session() as session:
                    result = session.run(
                        cypher_path,
                        {"repo": repo, "file_hint": file_hint, "name": symbol_hint},
                    )
                    records = [dict(r) for r in result]
                    if records:
                        return records
            except Exception:
                log.debug("resolve_symbol path match failed for %s in %s", target, repo)

    return []


def symbol_exists(repo: str, file: str, symbol_name: str) -> bool:
    """Check whether a symbol exists in Neptune (without querying callers).

    Used to distinguish "symbol not found" (lookup miss) from "symbol found
    but has no callers" (true negative).

    Parameters
    ----------
    repo:
        Repository identifier.
    file:
        File path (may be empty for name-only lookup).
    symbol_name:
        Symbol name.

    Returns
    -------
    True if at least one matching Symbol node exists.
    """
    driver = get_neptune_driver()
    if not driver:
        return False

    if file:
        cypher = """
            MATCH (s:Symbol {repo: $repo, file: $file, name: $name})
            RETURN count(s) > 0 AS exists
            LIMIT 1
        """
        params: dict[str, str] = {"repo": repo, "file": file, "name": symbol_name}
    else:
        cypher = """
            MATCH (s:Symbol {repo: $repo, name: $name})
            RETURN count(s) > 0 AS exists
            LIMIT 1
        """
        params = {"repo": repo, "name": symbol_name}

    try:
        with driver.session() as session:
            result = session.run(cypher, params)
            record = result.single()
            return bool(record and record["exists"])
    except Exception:
        log.debug("symbol_exists check failed for %s::%s in %s", file, symbol_name, repo)
        return False


def query_impact(
    repo: str,
    file: str,
    symbol_name: str,
) -> list[dict[str, Any]]:
    """Query Neptune for transitive callers of a symbol (impact analysis).

    Uses bounded variable-length path [:CALLS*1..4], capped at 100 results,
    ordered by distance (closest callers first).

    When file is empty, matches by repo+name only (allows bare symbol queries).

    Parameters
    ----------
    repo:
        Repository identifier (e.g. "org/repo").
    file:
        File path within the repo. If empty, matches any file.
    symbol_name:
        Symbol name to find callers of.

    Returns
    -------
    List of caller dicts with keys: caller_repo, caller_file, caller_name,
    caller_kind, distance. Empty list on error or no results.
    """
    driver = get_neptune_driver()
    if not driver:
        return []

    # When file is empty, omit it from the property match to avoid matching
    # only symbols with a literal empty-string file property (Bug #1587 Fix 1).
    if file:
        cypher = """
            MATCH (target:Symbol {repo: $repo, file: $file, name: $symbol_name})
            WITH target
            MATCH path = (caller:Symbol)-[:CALLS*1..4]->(target)
            WHERE caller <> target
            RETURN caller.repo AS caller_repo, caller.file AS caller_file,
                   caller.name AS caller_name, caller.kind AS caller_kind,
                   length(path) AS distance
            ORDER BY distance ASC, caller_repo, caller_file
            LIMIT 100
        """
        params: dict[str, str] = {"repo": repo, "file": file, "symbol_name": symbol_name}
    else:
        cypher = """
            MATCH (target:Symbol {repo: $repo, name: $symbol_name})
            WITH target
            MATCH path = (caller:Symbol)-[:CALLS*1..4]->(target)
            WHERE caller <> target
            RETURN caller.repo AS caller_repo, caller.file AS caller_file,
                   caller.name AS caller_name, caller.kind AS caller_kind,
                   length(path) AS distance
            ORDER BY distance ASC, caller_repo, caller_file
            LIMIT 100
        """
        params = {"repo": repo, "symbol_name": symbol_name}

    try:
        with driver.session() as session:
            result = session.run(cypher, params)
            records = [dict(record) for record in result]
            return records
    except Exception:
        log.warning(
            "Neptune impact query failed for %s::%s in %s",
            file,
            symbol_name,
            repo,
            exc_info=True,
        )
        return []


def query_understand(
    repo: str,
    file: str,
    symbol_name: str,
) -> list[dict[str, Any]]:
    """Query Neptune for a symbol's neighborhood (understand analysis).

    Returns the symbol plus its callers, callees, parents (inheritance),
    and owners (class membership).

    When file is empty, matches by repo+name only (allows bare symbol queries
    without knowing the exact file path). Bug #1587 Fix 1.

    Parameters
    ----------
    repo:
        Repository identifier.
    file:
        File path within the repo. If empty, matches any file.
    symbol_name:
        Symbol name to understand.

    Returns
    -------
    List of result dicts (typically one row per matched symbol) with keys:
    symbol_name, symbol_kind, symbol_file, signature, callees, callers,
    parents, owners. Empty list on error or no results.
    """
    driver = get_neptune_driver()
    if not driver:
        return []

    # When file is empty, omit it from the property match to avoid matching
    # only symbols with a literal empty-string file (Bug #1587 Fix 1).
    if file:
        cypher = """
            MATCH (s:Symbol {repo: $repo, file: $file, name: $symbol_name})
            OPTIONAL MATCH (s)-[:CALLS]->(callee:Symbol)
            OPTIONAL MATCH (caller:Symbol)-[:CALLS]->(s)
            OPTIONAL MATCH (s)-[:INHERITS]->(parent:Symbol)
            OPTIONAL MATCH (s)-[:MEMBER_OF]->(owner:Symbol)
            RETURN s.name AS symbol_name, s.kind AS symbol_kind, s.file AS symbol_file,
                   s.signature AS signature,
                   collect(DISTINCT {name: callee.name, file: callee.file, kind: callee.kind}) AS callees,
                   collect(DISTINCT {name: caller.name, file: caller.file, kind: caller.kind}) AS callers,
                   collect(DISTINCT {name: parent.name, file: parent.file}) AS parents,
                   collect(DISTINCT {name: owner.name, file: owner.file}) AS owners
            LIMIT 50
        """
        params: dict[str, str] = {"repo": repo, "file": file, "symbol_name": symbol_name}
    else:
        cypher = """
            MATCH (s:Symbol {repo: $repo, name: $symbol_name})
            OPTIONAL MATCH (s)-[:CALLS]->(callee:Symbol)
            OPTIONAL MATCH (caller:Symbol)-[:CALLS]->(s)
            OPTIONAL MATCH (s)-[:INHERITS]->(parent:Symbol)
            OPTIONAL MATCH (s)-[:MEMBER_OF]->(owner:Symbol)
            RETURN s.name AS symbol_name, s.kind AS symbol_kind, s.file AS symbol_file,
                   s.signature AS signature,
                   collect(DISTINCT {name: callee.name, file: callee.file, kind: callee.kind}) AS callees,
                   collect(DISTINCT {name: caller.name, file: caller.file, kind: caller.kind}) AS callers,
                   collect(DISTINCT {name: parent.name, file: parent.file}) AS parents,
                   collect(DISTINCT {name: owner.name, file: owner.file}) AS owners
            LIMIT 50
        """
        params = {"repo": repo, "symbol_name": symbol_name}

    try:
        with driver.session() as session:
            result = session.run(cypher, params)
            records = [dict(record) for record in result]
            return records
    except Exception:
        log.warning(
            "Neptune understand query failed for %s::%s in %s",
            file,
            symbol_name,
            repo,
            exc_info=True,
        )
        return []


def query_repo_topology(repo: str) -> list[dict[str, Any]]:
    """Query Neptune for repo-level module topology (understand at repo level).

    Returns module paths, their files, and symbol counts. Used when the
    understand target is a repo name (not a specific symbol).

    Parameters
    ----------
    repo:
        Repository identifier.

    Returns
    -------
    List of module dicts with keys: module_path, files, symbol_count.
    Empty list on error or no results.
    """
    driver = get_neptune_driver()
    if not driver:
        return []

    cypher = """
        MATCH (m:Module {repo: $repo})
        OPTIONAL MATCH (m)-[:CONTAINS]->(f:File)
        OPTIONAL MATCH (f)-[:DEFINES]->(s:Symbol)
        RETURN m.path AS module_path,
               collect(DISTINCT f.path) AS files,
               count(DISTINCT s) AS symbol_count
        ORDER BY module_path
        LIMIT 50
    """
    params = {"repo": repo}

    try:
        with driver.session() as session:
            result = session.run(cypher, params)
            records = [dict(record) for record in result]
            return records
    except Exception:
        log.warning(
            "Neptune repo topology query failed for %s",
            repo,
            exc_info=True,
        )
        return []


def query_file_symbols(repo: str, file_path: str) -> list[dict[str, Any]]:
    """Query Neptune for all symbols defined in a file.

    Used when understand target is a file path.

    Parameters
    ----------
    repo:
        Repository identifier.
    file_path:
        File path within the repo.

    Returns
    -------
    List of symbol dicts with keys: name, kind, line, signature.
    Empty list on error or no results.
    """
    driver = get_neptune_driver()
    if not driver:
        return []

    cypher = """
        MATCH (s:Symbol {repo: $repo, file: $file})
        RETURN s.name AS name, s.kind AS kind, s.line AS line,
               s.signature AS signature
        ORDER BY s.line
        LIMIT 50
    """
    params = {"repo": repo, "file": file_path}

    try:
        with driver.session() as session:
            result = session.run(cypher, params)
            records = [dict(record) for record in result]
            return records
    except Exception:
        log.warning(
            "Neptune file symbols query failed for %s in %s",
            file_path,
            repo,
            exc_info=True,
        )
        return []


def query_dir_symbols(repo: str, dir_path: str) -> list[dict[str, Any]]:
    """Query Neptune for symbols in files under a directory.

    Used when understand target is a directory path.

    Parameters
    ----------
    repo:
        Repository identifier.
    dir_path:
        Directory path prefix.

    Returns
    -------
    List of symbol dicts with keys: file, name, kind, line.
    """
    driver = get_neptune_driver()
    if not driver:
        return []

    cypher = """
        MATCH (s:Symbol {repo: $repo})
        WHERE s.file STARTS WITH $dir_prefix
        RETURN s.file AS file, s.name AS name, s.kind AS kind, s.line AS line
        ORDER BY s.file, s.line
        LIMIT 50
    """
    params = {"repo": repo, "dir_prefix": dir_path + "/"}

    try:
        with driver.session() as session:
            result = session.run(cypher, params)
            records = [dict(record) for record in result]
            return records
    except Exception:
        log.warning(
            "Neptune dir symbols query failed for %s in %s",
            dir_path,
            repo,
            exc_info=True,
        )
        return []


# ---------------------------------------------------------------------------
# Cross-repo query via symbol_id (SCIP moniker)
# ---------------------------------------------------------------------------

# SCIP moniker version pattern: matches the version segment in monikers like
# "scip-python python <package> <version> ..." — the version is typically the
# 4th space-separated token for Python/TS/Go schemes.
_SCIP_VERSION_RE = re.compile(r"^(scip-\w+\s+\w+\s+\S+)\s+(\S+)\s+(.*)$")


def normalize_symbol_id(symbol_id: str) -> str:
    """Normalize a SCIP symbol_id by stripping the version component.

    SCIP monikers embed the package version (e.g.,
    "scip-python python requests 2.31.0 requests/api.py/get().").
    Two repos on different versions of the same library would have non-matching
    monikers without normalization.

    Normalization: match on scheme+manager+package+descriptor, ignore version.
    Format: "scip-<lang> <manager> <package> <version> <descriptor>"
    Normalized: "scip-<lang> <manager> <package> <descriptor>"

    If the moniker doesn't match the expected pattern, returns it unchanged
    (safe fallback — exact matching still works for same-version cases).
    """
    if not symbol_id:
        return ""
    m = _SCIP_VERSION_RE.match(symbol_id)
    if m:
        prefix = m.group(1)  # "scip-python python requests"
        descriptor = m.group(3)  # "requests/api.py/get()."
        return f"{prefix} {descriptor}"
    return symbol_id


def query_cross_repo_impact(
    repo: str,
    file: str,
    symbol_name: str,
) -> list[dict[str, Any]]:
    """Query Neptune for cross-repo callers/references via symbol_id join.

    Resolves the target symbol's symbol_id (SCIP moniker), then finds all
    symbols in OTHER repos that reference a symbol with the same normalized
    symbol_id. Uses version-normalized matching to handle repos on different
    package versions.

    NEVER joins on name+file — that fabricates false edges.

    Parameters
    ----------
    repo:
        Repository of the target symbol.
    file:
        File path of the target symbol within its repo.
    symbol_name:
        Name of the target symbol.

    Returns
    -------
    List of caller dicts with keys: calling_repo, calling_file, calling_symbol,
    calling_kind. Bounded at 100 results, grouped by calling_repo.
    Empty list on error or no results.
    """
    driver = get_neptune_driver()
    if not driver:
        return []

    # Step 1: Resolve the target symbol's symbol_id
    resolve_cypher = """
        MATCH (target:Symbol {repo: $repo, file: $file, name: $symbol_name})
        WHERE target.symbol_id IS NOT NULL
        RETURN target.symbol_id AS symbol_id
        LIMIT 1
    """
    params = {"repo": repo, "file": file, "symbol_name": symbol_name}

    try:
        with driver.session() as session:
            resolve_result = session.run(resolve_cypher, params)
            resolve_records = [dict(r) for r in resolve_result]

        if not resolve_records:
            log.debug(
                "Cross-repo: no symbol_id found for %s::%s in %s",
                file,
                symbol_name,
                repo,
            )
            return []

        target_sid = resolve_records[0]["symbol_id"]
        if not target_sid:
            return []

        # Step 2: Normalize the symbol_id for version-independent matching
        normalized_sid = normalize_symbol_id(target_sid)

        # Step 3: Find cross-repo callers/references with same normalized symbol_id
        # We query for symbols that CALL a symbol whose normalized symbol_id matches,
        # excluding the source repo.
        cross_repo_cypher = """
            MATCH (target:Symbol {repo: $repo, name: $symbol_name, file: $file})
            WITH target, target.symbol_id AS target_sid
            WHERE target_sid IS NOT NULL
            MATCH (caller:Symbol)-[:CALLS]->(callee:Symbol {symbol_id: target_sid})
            WHERE caller.repo <> $repo
            RETURN DISTINCT caller.repo AS calling_repo,
                   caller.file AS calling_file,
                   caller.name AS calling_symbol,
                   caller.kind AS calling_kind
            ORDER BY calling_repo, calling_file
            LIMIT 100
        """
        cross_params = {"repo": repo, "file": file, "symbol_name": symbol_name}

        with driver.session() as session:
            result = session.run(cross_repo_cypher, cross_params)
            records = [dict(r) for r in result]

        # If exact symbol_id match returned nothing and we have a normalized form,
        # try the version-normalized query as a secondary attempt
        if not records and normalized_sid != target_sid:
            normalized_cypher = """
                MATCH (callee:Symbol)
                WHERE callee.symbol_id STARTS WITH $sid_prefix
                  AND callee.repo <> $repo
                WITH callee
                MATCH (caller:Symbol)-[:CALLS]->(callee)
                WHERE caller.repo <> $repo
                RETURN DISTINCT caller.repo AS calling_repo,
                       caller.file AS calling_file,
                       caller.name AS calling_symbol,
                       caller.kind AS calling_kind
                ORDER BY calling_repo, calling_file
                LIMIT 100
            """
            # Use the descriptor portion as prefix for fuzzy version-normalized match
            m = _SCIP_VERSION_RE.match(target_sid)
            if m:
                # The descriptor (after version) is the stable identifier
                sid_prefix = m.group(1)  # scheme+manager+package
                norm_params = {"sid_prefix": sid_prefix, "repo": repo}
                with driver.session() as session:
                    result = session.run(normalized_cypher, norm_params)
                    records = [dict(r) for r in result]

        return records

    except Exception:
        log.warning(
            "Neptune cross-repo impact query failed for %s::%s in %s",
            file,
            symbol_name,
            repo,
            exc_info=True,
        )
        return []
