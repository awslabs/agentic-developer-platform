"""Neptune openCypher client for the Door query layer.

Provides lazy driver initialization, availability checking, and typed
query methods for the impact and understand verbs. Falls back gracefully
when Neptune is unreachable.

See: docs/agent-context/neptune-deep-graph-design.md (Door Query Patterns)
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

_driver = None


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


def query_impact(
    repo: str,
    file: str,
    symbol_name: str,
) -> list[dict[str, Any]]:
    """Query Neptune for transitive callers of a symbol (impact analysis).

    Uses bounded variable-length path [:CALLS*1..4], capped at 100 results,
    ordered by distance (closest callers first).

    Parameters
    ----------
    repo:
        Repository identifier (e.g. "org/repo").
    file:
        File path within the repo.
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
    params = {"repo": repo, "file": file, "symbol_name": symbol_name}

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

    Parameters
    ----------
    repo:
        Repository identifier.
    file:
        File path within the repo.
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
    params = {"repo": repo, "file": file, "symbol_name": symbol_name}

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
