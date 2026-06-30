"""Secure verb backend — consolidated implementation.

The 7th verb in the Context MCP Server. Identifies, locates, and plans
remediation for vulnerabilities by performing a query-time composition of:
  - SBOM data (S3 CycloneDX)
  - Dependencies (PostgreSQL)
  - Vulnerabilities (PostgreSQL)
  - Neptune SCIP call graph + Zoekt code search

Entry points:
  - handle_identify() — default: full vulnerability assessment
  - handle_plan() — remediation step generation (requires cve + repo)
  - handle_verify() — post-fix re-check (requires cve)

Design: docs/agent-context/design-2447-secure-verb-architecture.md
Product story: docs/agent-context/design-2437-secure-verb-product-story.md
Consolidation: Issue #2510 (from PRs #2458–#2470, #2477)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .acl import CallerPrincipal, PostgresACLStore

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NEPTUNE_TIMEOUT_SECONDS = float(os.environ.get("NEPTUNE_TIMEOUT_SECONDS", "5.0"))
SBOM_STALE_THRESHOLD_HOURS: float = 24.0
SBOM_S3_PREFIX: str = "sbom/repos"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class AffectedTuple:
    """A single (repo, package, version, CVE) finding from the data-join."""

    repo_name: str
    repo_id: str
    package_purl: str
    package_name: str
    package_version: str
    package_ecosystem: str
    cve_id: str
    severity: str
    cvss_score: float | None
    fixed_version: str | None
    affected_versions: str


@dataclass
class UsageSite:
    """A location where a vulnerable package is used."""

    file: str
    line: int
    symbol: str
    reachability_level: str  # present | imported | called | reachable
    callers: list[dict] = field(default_factory=list)


@dataclass
class ReachabilityResult:
    """Summary of how deeply a package is exercised."""

    level: str  # present | imported | called | reachable
    confidence: str  # high | degraded | assumed
    source: str  # neptune | code-index-fallback | timeout-fallback


@dataclass
class Finding:
    """A single vulnerability finding with priority metadata."""

    cve_id: str
    severity: str = "UNKNOWN"
    cvss_score: float | None = None
    package: str = ""
    ecosystem: str = ""
    affected_version: str = ""
    fixed_version: str | None = None
    purl: str = ""
    reachability_level: str = "present"
    reachability_confidence: str = "high"
    reachability_source: str = "neptune"
    fix_available: bool = False
    priority_score: float = 0.0
    priority: str = "P3"
    repos_affected: list[str] = field(default_factory=list)
    usage_sites: list[dict] = field(default_factory=list)
    remediation: dict | None = None


# ---------------------------------------------------------------------------
# Scoring weights (from product story §5)
# ---------------------------------------------------------------------------

SEVERITY_WEIGHTS: dict[str, float] = {
    "CRITICAL": 1.0,
    "HIGH": 0.7,
    "MEDIUM": 0.4,
    "LOW": 0.1,
}

REACHABILITY_WEIGHTS: dict[str, float] = {
    "reachable": 1.0,
    "called": 0.8,
    "imported": 0.5,
    "present": 0.2,
}

FIX_AVAILABILITY_WEIGHTS: dict[bool, float] = {
    True: 1.0,
    False: 0.3,
}

PRIORITY_BUCKETS: list[tuple[str, float]] = [
    ("P0", 0.7),
    ("P1", 0.4),
    ("P2", 0.1),
    ("P3", 0.0),
]

_SEVERITY_RANK: dict[str, int] = {
    "CRITICAL": 4,
    "HIGH": 3,
    "MEDIUM": 2,
    "LOW": 1,
}

# ---------------------------------------------------------------------------
# Ecosystem constants (for action=plan)
# ---------------------------------------------------------------------------

ECOSYSTEM_LOCKFILES: dict[str, list[str]] = {
    "npm": ["package-lock.json", "yarn.lock", "pnpm-lock.yaml"],
    "pypi": ["requirements.txt", "poetry.lock", "Pipfile.lock"],
    "go": ["go.sum"],
    "maven": ["pom.xml"],
    "cargo": ["Cargo.lock"],
}

ECOSYSTEM_MANIFEST: dict[str, str] = {
    "npm": "package.json",
    "pypi": "pyproject.toml",
    "go": "go.mod",
    "maven": "pom.xml",
    "cargo": "Cargo.toml",
}

ECOSYSTEM_INSTALL_COMMANDS: dict[str, str] = {
    "npm": "npm install",
    "pypi": "pip install -r requirements.txt",
    "go": "go mod tidy",
    "maven": "mvn dependency:resolve",
    "cargo": "cargo update",
}


# ---------------------------------------------------------------------------
# Import detection patterns (Level 1 — Zoekt)
# ---------------------------------------------------------------------------

_IMPORT_PATTERNS: dict[str, list[str]] = {
    "npm": [
        r"import\s+.*from\s+['\"]({package})['\"/]",
        r"require\(\s*['\"]({package})['\"/]",
    ],
    "pypi": [
        r"^import\s+({package})",
        r"^from\s+({package})",
    ],
    "go": [
        r'["\']({package})["\']',
    ],
}


# ---------------------------------------------------------------------------
# Version range matching (stdlib only — no `packaging` dependency)
# ---------------------------------------------------------------------------

_COMPARATOR_RE = re.compile(r"^(>=|<=|>|<|==|!=)\s*(.+)$")


def _parse_version_tuple(version_str: str) -> tuple[int, ...]:
    """Parse a version string into a comparable tuple of integers."""
    version_str = version_str.lstrip("v")
    parts: list[int] = []
    for segment in re.split(r"[.\-+~]", version_str):
        match = re.match(r"(\d+)", segment)
        if match:
            parts.append(int(match.group(1)))
        else:
            break
    return tuple(parts) if parts else (0,)


def _version_satisfies_comparator(
    version: tuple[int, ...], op: str, target: tuple[int, ...]
) -> bool:
    """Check if a version tuple satisfies a single comparator."""
    if op == ">=":
        return version >= target
    elif op == "<=":
        return version <= target
    elif op == ">":
        return version > target
    elif op == "<":
        return version < target
    elif op == "==":
        return version == target
    elif op == "!=":
        return version != target
    return False


def version_in_range(version_str: str, range_spec: str) -> bool:
    """Check if a version string falls within an affected_versions range spec.

    Supports: ">=2.0.0,<2.28.1", "all", "1.2.3" (exact match).
    On parse failure, returns True (fail-open: report rather than hide).
    """
    range_spec = range_spec.strip()
    if range_spec.lower() == "all":
        return True
    if not range_spec:
        return True

    version = _parse_version_tuple(version_str)
    comparators = [c.strip() for c in range_spec.split(",") if c.strip()]

    for comp_str in comparators:
        match = _COMPARATOR_RE.match(comp_str)
        if match:
            op, target_str = match.group(1), match.group(2)
            target = _parse_version_tuple(target_str)
            if not _version_satisfies_comparator(version, op, target):
                return False
        else:
            target = _parse_version_tuple(comp_str)
            if version != target:
                return False
    return True


# ---------------------------------------------------------------------------
# Purl helpers
# ---------------------------------------------------------------------------


def _extract_package_name_from_purl(purl: str) -> str:
    """Extract package name from a purl coordinate.

    pkg:pypi/requests@2.31.0 → "requests"
    pkg:npm/%40angular/core@16.0.0 → "core"
    """
    if not purl or not purl.startswith("pkg:"):
        return purl
    remainder = purl[4:]
    remainder = remainder.split("?")[0].split("#")[0]
    remainder = remainder.split("@")[0]
    parts = remainder.split("/")
    return parts[-1] if parts else ""


def _extract_ecosystem_from_purl(purl: str) -> str:
    """Extract ecosystem from a purl coordinate.

    pkg:pypi/requests@2.31.0 → "pypi"
    """
    if not purl or not purl.startswith("pkg:"):
        return "unknown"
    remainder = purl[4:]
    slash_idx = remainder.find("/")
    if slash_idx == -1:
        return remainder.split("@")[0] if "@" in remainder else remainder
    return remainder[:slash_idx]


# ---------------------------------------------------------------------------
# Database queries (data-join layer)
# ---------------------------------------------------------------------------


def _query_vulnerabilities_by_cve(pool: Any, cve_id: str) -> list[dict]:
    """Query vulnerabilities table by CVE ID."""
    query = """
        SELECT cve_id, package, affected_versions, safe_version, severity, details
        FROM vulnerabilities
        WHERE cve_id = %s
    """
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, (cve_id,))
            rows = cur.fetchall()
            return [
                {
                    "cve_id": row[0],
                    "package": row[1],
                    "affected_versions": row[2],
                    "safe_version": row[3],
                    "severity": row[4],
                    "details": row[5],
                }
                for row in rows
            ]
    finally:
        pool.putconn(conn)


def _query_vulnerabilities_by_package(pool: Any, package_name: str) -> list[dict]:
    """Query vulnerabilities table by package name (LIKE match)."""
    query = """
        SELECT cve_id, package, affected_versions, safe_version, severity, details
        FROM vulnerabilities
        WHERE package LIKE %s
    """
    pattern = f"%/{package_name}" if "/" not in package_name else f"%{package_name}"
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, (pattern,))
            rows = cur.fetchall()
            return [
                {
                    "cve_id": row[0],
                    "package": row[1],
                    "affected_versions": row[2],
                    "safe_version": row[3],
                    "severity": row[4],
                    "details": row[5],
                }
                for row in rows
            ]
    finally:
        pool.putconn(conn)


def _query_dependencies_by_package(pool: Any, package_name: str) -> list[dict]:
    """Query dependencies table for repos using a given package name."""
    query = """
        SELECT d.repo_id, d.package_coordinate, d.version, r.repo_name
        FROM dependencies d
        JOIN repositories r ON d.repo_id = r.id
        WHERE d.package_coordinate LIKE %s
    """
    pattern = f"pkg:%/{package_name}@%"
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, (pattern,))
            rows = cur.fetchall()
            return [
                {
                    "repo_id": str(row[0]),
                    "package_coordinate": row[1],
                    "version": row[2],
                    "repo_name": row[3],
                }
                for row in rows
            ]
    finally:
        pool.putconn(conn)


def _query_dependencies_by_repo(pool: Any, repo_name: str) -> list[dict]:
    """Query all dependencies for a given repo."""
    query = """
        SELECT d.repo_id, d.package_coordinate, d.version, r.repo_name
        FROM dependencies d
        JOIN repositories r ON d.repo_id = r.id
        WHERE r.repo_name = %s
    """
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, (repo_name,))
            rows = cur.fetchall()
            return [
                {
                    "repo_id": str(row[0]),
                    "package_coordinate": row[1],
                    "version": row[2],
                    "repo_name": row[3],
                }
                for row in rows
            ]
    finally:
        pool.putconn(conn)


def _check_repo_exists(pool: Any, repo_name: str) -> bool:
    """Check if a repo exists in the repositories table."""
    query = "SELECT 1 FROM repositories WHERE repo_name = %s LIMIT 1"
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, (repo_name,))
            return cur.fetchone() is not None
    finally:
        pool.putconn(conn)


def _check_cve_exists(pool: Any, cve_id: str) -> bool:
    """Check if a CVE exists in the vulnerabilities table."""
    query = "SELECT 1 FROM vulnerabilities WHERE cve_id = %s LIMIT 1"
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, (cve_id,))
            return cur.fetchone() is not None
    finally:
        pool.putconn(conn)


# ---------------------------------------------------------------------------
# Resolution logic (data-join)
# ---------------------------------------------------------------------------


def _resolve_by_cve(pool: Any, cve_id: str) -> list[AffectedTuple]:
    """CVE → vulnerabilities → packages → reverse-lookup dependencies."""
    vulns = _query_vulnerabilities_by_cve(pool, cve_id)
    if not vulns:
        return []

    results: list[AffectedTuple] = []
    for vuln in vulns:
        package_field = vuln["package"]
        affected_versions = vuln["affected_versions"]
        package_name = _extract_package_name_from_purl(package_field)
        if not package_name:
            continue

        deps = _query_dependencies_by_package(pool, package_name)
        for dep in deps:
            dep_version = dep["version"]
            if not dep_version:
                continue
            if version_in_range(dep_version, affected_versions):
                details = vuln.get("details") or {}
                cvss_score = details.get("cvss_score") if isinstance(details, dict) else None
                results.append(
                    AffectedTuple(
                        repo_name=dep["repo_name"],
                        repo_id=dep["repo_id"],
                        package_purl=dep["package_coordinate"],
                        package_name=package_name,
                        package_version=dep_version,
                        package_ecosystem=_extract_ecosystem_from_purl(dep["package_coordinate"]),
                        cve_id=cve_id,
                        severity=vuln["severity"],
                        cvss_score=cvss_score,
                        fixed_version=vuln["safe_version"],
                        affected_versions=affected_versions,
                    )
                )
    return results


def _resolve_by_repo(pool: Any, repo_name: str) -> list[AffectedTuple]:
    """Repo → dependencies → for each dep, check vulnerabilities."""
    deps = _query_dependencies_by_repo(pool, repo_name)
    if not deps:
        return []

    results: list[AffectedTuple] = []
    seen_packages: dict[str, list[dict]] = {}
    for dep in deps:
        pkg_name = _extract_package_name_from_purl(dep["package_coordinate"])
        if pkg_name:
            seen_packages.setdefault(pkg_name, []).append(dep)

    for pkg_name, pkg_deps in seen_packages.items():
        vulns = _query_vulnerabilities_by_package(pool, pkg_name)
        for vuln in vulns:
            affected_versions = vuln["affected_versions"]
            for dep in pkg_deps:
                dep_version = dep["version"]
                if not dep_version:
                    continue
                if version_in_range(dep_version, affected_versions):
                    details = vuln.get("details") or {}
                    cvss_score = details.get("cvss_score") if isinstance(details, dict) else None
                    results.append(
                        AffectedTuple(
                            repo_name=dep["repo_name"],
                            repo_id=dep["repo_id"],
                            package_purl=dep["package_coordinate"],
                            package_name=pkg_name,
                            package_version=dep_version,
                            package_ecosystem=_extract_ecosystem_from_purl(
                                dep["package_coordinate"]
                            ),
                            cve_id=vuln["cve_id"],
                            severity=vuln["severity"],
                            cvss_score=cvss_score,
                            fixed_version=vuln["safe_version"],
                            affected_versions=affected_versions,
                        )
                    )
    return results


def _resolve_by_package(pool: Any, package_name: str) -> list[AffectedTuple]:
    """Package → dependencies (all repos) → check vulnerabilities."""
    deps = _query_dependencies_by_package(pool, package_name)
    if not deps:
        return []

    vulns = _query_vulnerabilities_by_package(pool, package_name)
    if not vulns:
        return []

    results: list[AffectedTuple] = []
    for vuln in vulns:
        affected_versions = vuln["affected_versions"]
        for dep in deps:
            dep_version = dep["version"]
            if not dep_version:
                continue
            if version_in_range(dep_version, affected_versions):
                details = vuln.get("details") or {}
                cvss_score = details.get("cvss_score") if isinstance(details, dict) else None
                results.append(
                    AffectedTuple(
                        repo_name=dep["repo_name"],
                        repo_id=dep["repo_id"],
                        package_purl=dep["package_coordinate"],
                        package_name=package_name,
                        package_version=dep_version,
                        package_ecosystem=_extract_ecosystem_from_purl(dep["package_coordinate"]),
                        cve_id=vuln["cve_id"],
                        severity=vuln["severity"],
                        cvss_score=cvss_score,
                        fixed_version=vuln["safe_version"],
                        affected_versions=affected_versions,
                    )
                )
    return results


def _intersect_findings(
    findings_a: list[AffectedTuple], findings_b: list[AffectedTuple]
) -> list[AffectedTuple]:
    """Intersect two sets by (repo_name, cve_id, package_purl) key."""
    keys_b = {(f.repo_name, f.cve_id, f.package_purl) for f in findings_b}
    return [f for f in findings_a if (f.repo_name, f.cve_id, f.package_purl) in keys_b]


def _filter_by_acl(
    findings: list[AffectedTuple],
    caller: CallerPrincipal,
    acl_store: PostgresACLStore | None,
) -> list[AffectedTuple]:
    """Filter findings to repos the caller can see. Fail-closed."""
    if not caller.is_resolved:
        return []
    if acl_store is None:
        return findings  # dev-mode: no ACL store configured
    try:
        allowed_repos = acl_store.get_allowed_repos(caller)
    except Exception:
        log.warning("secure_backend: ACL store raised, returning empty", exc_info=True)
        return []
    return [f for f in findings if f.repo_name in allowed_repos]


def resolve_input(
    *,
    db_pool: Any,
    acl_store: PostgresACLStore | None,
    caller: CallerPrincipal,
    cve: str | None = None,
    repo: str | None = None,
    package: str | None = None,
) -> tuple[list[AffectedTuple], bool, bool]:
    """Resolve secure verb inputs into affected tuples.

    Returns (findings, cve_known, repo_indexed).
    """
    cve_known = True
    repo_indexed = True

    if cve and not _check_cve_exists(db_pool, cve):
        cve_known = False
    if repo and not _check_repo_exists(db_pool, repo):
        repo_indexed = False

    finding_sets: list[list[AffectedTuple]] = []
    if cve and cve_known:
        finding_sets.append(_resolve_by_cve(db_pool, cve))
    if repo and repo_indexed:
        finding_sets.append(_resolve_by_repo(db_pool, repo))
    if package:
        finding_sets.append(_resolve_by_package(db_pool, package))

    if not finding_sets:
        findings: list[AffectedTuple] = []
    elif len(finding_sets) == 1:
        findings = finding_sets[0]
    else:
        findings = finding_sets[0]
        for other_set in finding_sets[1:]:
            findings = _intersect_findings(findings, other_set)

    findings = _filter_by_acl(findings, caller, acl_store)
    return findings, cve_known, repo_indexed


# ---------------------------------------------------------------------------
# Reachability scoring (Levels 0-3)
# ---------------------------------------------------------------------------


def _build_import_query(package_name: str, ecosystem: str) -> str:
    """Build a Zoekt regex for import detection."""
    patterns = _IMPORT_PATTERNS.get(ecosystem, _IMPORT_PATTERNS["pypi"])
    escaped_name = re.escape(package_name)
    alternation = "|".join(p.format(package=escaped_name) for p in patterns)
    return alternation


async def check_import_level(
    package_name: str,
    repo_name: str,
    ecosystem: str,
    zoekt_backend: Any,
) -> list[UsageSite]:
    """Level 1: Find import/require statements via Zoekt."""
    query = _build_import_query(package_name, ecosystem)
    try:
        hits = await zoekt_backend.search(query, repo_ids=[repo_name], limit=50)
    except Exception:
        log.warning(
            "Zoekt import search failed for %s in %s", package_name, repo_name, exc_info=True
        )
        return []

    sites: list[UsageSite] = []
    for hit in hits:
        data = hit.data
        sites.append(
            UsageSite(
                file=data.get("file", ""),
                line=data.get("line", 0),
                symbol=data.get("content", "").strip(),
                reachability_level="imported",
            )
        )
    return sites


def _query_package_called(
    repo: str, package_module: str, *, tenant_id: str | None = None
) -> list[dict]:
    """Level 2: Neptune — symbols from package that have inbound CALLS."""
    from .neptune_client import NeptuneQueryError, _build_scope_filter, get_neptune_driver

    driver = get_neptune_driver()
    if not driver:
        return []

    params: dict[str, Any] = {"repo": repo, "package_module": package_module}
    scope = _build_scope_filter("caller", tenant_id, params)

    cypher = f"""
        MATCH (caller:Symbol {{repo: $repo}})-[:CALLS]->(target:Symbol)
        WHERE target.module STARTS WITH $package_module
          AND caller.module <> target.module
          {scope}
        RETURN caller.name AS caller_name, caller.file AS caller_file,
               target.name AS target_name, target.file AS target_file
        LIMIT 50
    """
    try:
        with driver.session() as session:
            result = session.run(cypher, params)
            return [dict(record) for record in result]
    except Exception as exc:
        raise NeptuneQueryError(
            f"Neptune package-called query failed for {package_module} in {repo}"
        ) from exc


def _query_package_reachable(
    repo: str, package_module: str, *, tenant_id: str | None = None
) -> list[dict]:
    """Level 3: Neptune — bounded path from entry points to package symbols."""
    from .neptune_client import NeptuneQueryError, _build_scope_filter, get_neptune_driver

    driver = get_neptune_driver()
    if not driver:
        return []

    params: dict[str, Any] = {"repo": repo, "package_module": package_module}
    scope = _build_scope_filter("entry", tenant_id, params)

    cypher = f"""
        MATCH (entry:Symbol {{repo: $repo}})
        WHERE entry.kind IN ['function', 'method']
          AND NOT exists((:Symbol)-[:CALLS]->(entry))
          {scope}
        WITH entry
        MATCH path = (entry)-[:CALLS*1..4]->(target:Symbol)
        WHERE target.module STARTS WITH $package_module
        RETURN entry.name AS entry_name, entry.file AS entry_file,
               target.name AS target_name, target.file AS target_file,
               length(path) AS distance
        ORDER BY distance ASC
        LIMIT 20
    """
    try:
        with driver.session() as session:
            result = session.run(cypher, params)
            return [dict(record) for record in result]
    except Exception as exc:
        raise NeptuneQueryError(
            f"Neptune package-reachable query failed for {package_module} in {repo}"
        ) from exc


async def determine_reachability(
    repo_name: str,
    package_name: str,
    package_ecosystem: str,
    *,
    zoekt_backend: Any,
    neptune_driver: Any = None,
    tenant_id: str | None = None,
) -> tuple[ReachabilityResult, list[UsageSite]]:
    """Determine reachability level for a package in a repo.

    Checks levels 1→2→3 in order. Fails safe to "reachable" if Neptune
    is unavailable or times out.
    """
    from .neptune_client import NeptuneQueryError, neptune_available

    all_sites: list[UsageSite] = []

    # Level 1: Check imports via Zoekt
    import_sites = await check_import_level(
        package_name, repo_name, package_ecosystem, zoekt_backend
    )

    if not import_sites:
        return (
            ReachabilityResult(level="present", confidence="high", source="code-index-fallback"),
            [],
        )

    all_sites.extend(import_sites)

    # Levels 2-3 require Neptune
    if not neptune_available():
        for site in all_sites:
            site.reachability_level = "imported"
        return (
            ReachabilityResult(
                level="reachable", confidence="degraded", source="code-index-fallback"
            ),
            all_sites,
        )

    # Level 2: Check if package symbols are called
    try:
        called_results = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _query_package_called(repo_name, package_name, tenant_id=tenant_id),
            ),
            timeout=NEPTUNE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        log.warning(
            "Neptune timeout (%.1fs) for package-called: %s in %s",
            NEPTUNE_TIMEOUT_SECONDS,
            package_name,
            repo_name,
        )
        return (
            ReachabilityResult(level="reachable", confidence="assumed", source="timeout-fallback"),
            all_sites,
        )
    except NeptuneQueryError:
        return (
            ReachabilityResult(
                level="reachable", confidence="degraded", source="code-index-fallback"
            ),
            all_sites,
        )

    if not called_results:
        return (
            ReachabilityResult(level="imported", confidence="high", source="neptune"),
            all_sites,
        )

    # Upgrade to level 2
    for record in called_results:
        all_sites.append(
            UsageSite(
                file=record.get("caller_file", ""),
                line=0,
                symbol=record.get("target_name", ""),
                reachability_level="called",
                callers=[
                    {
                        "file": record.get("caller_file", ""),
                        "line": 0,
                        "symbol": record.get("caller_name", ""),
                        "distance": 1,
                    }
                ],
            )
        )

    # Level 3: Check if reachable from entry points
    try:
        reachable_results = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _query_package_reachable(repo_name, package_name, tenant_id=tenant_id),
            ),
            timeout=NEPTUNE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        log.warning(
            "Neptune timeout (%.1fs) for package-reachable: %s in %s",
            NEPTUNE_TIMEOUT_SECONDS,
            package_name,
            repo_name,
        )
        return (
            ReachabilityResult(level="reachable", confidence="assumed", source="timeout-fallback"),
            all_sites,
        )
    except NeptuneQueryError:
        return (
            ReachabilityResult(level="called", confidence="high", source="neptune"),
            all_sites,
        )

    if not reachable_results:
        return (
            ReachabilityResult(level="called", confidence="high", source="neptune"),
            all_sites,
        )

    # Level 3 confirmed
    for record in reachable_results:
        all_sites.append(
            UsageSite(
                file=record.get("target_file", ""),
                line=0,
                symbol=record.get("target_name", ""),
                reachability_level="reachable",
                callers=[
                    {
                        "file": record.get("entry_file", ""),
                        "line": 0,
                        "symbol": record.get("entry_name", ""),
                        "distance": record.get("distance", 0),
                    }
                ],
            )
        )

    return (
        ReachabilityResult(level="reachable", confidence="high", source="neptune"),
        all_sites,
    )


# ---------------------------------------------------------------------------
# Prioritization engine
# ---------------------------------------------------------------------------


def compute_priority_score(
    severity: str,
    reachability_level: str,
    fix_available: bool,
) -> tuple[float, str]:
    """Compute priority score and bucket (P0–P3).

    Defaults: unknown severity → 0.1, unknown reachability → 1.0 (fail-safe).
    """
    severity_w = SEVERITY_WEIGHTS.get(severity.upper(), 0.1)
    reachability_w = REACHABILITY_WEIGHTS.get(reachability_level.lower(), 1.0)
    fix_w = FIX_AVAILABILITY_WEIGHTS.get(fix_available, 0.3)

    score = severity_w * reachability_w * fix_w

    bucket = "P3"
    for label, threshold in PRIORITY_BUCKETS:
        if score >= threshold:
            bucket = label
            break

    return score, bucket


def _sort_findings(findings: list[Finding]) -> list[Finding]:
    """Sort findings by priority_score descending, severity, then CVE ID."""
    return sorted(
        findings,
        key=lambda f: (
            -f.priority_score,
            -_SEVERITY_RANK.get(f.severity.upper(), 0),
            f.cve_id,
        ),
    )


# ---------------------------------------------------------------------------
# SBOM freshness
# ---------------------------------------------------------------------------


async def get_sbom_freshness(repo_name: str, s3_client: Any, bucket: str) -> dict[str, Any]:
    """Get SBOM freshness metadata via S3 HEAD request."""
    key = f"{SBOM_S3_PREFIX}/{repo_name}/source.cdx.json"
    try:
        response = s3_client.head_object(Bucket=bucket, Key=key)
        last_modified = response["LastModified"]
        age_hours = (datetime.now(timezone.utc) - last_modified).total_seconds() / 3600
        return {
            "sbom_age_hours": round(age_hours, 1),
            "sbom_stale_warning": age_hours > SBOM_STALE_THRESHOLD_HOURS,
        }
    except Exception:
        log.debug("SBOM freshness check failed for %s (key=%s)", repo_name, key)
        return {"sbom_age_hours": None, "repo_indexed": False}


# ---------------------------------------------------------------------------
# Remediation plan generation (action=plan)
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(r"^[v^~>=<!]*(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def _parse_major(version: str) -> int | None:
    """Extract the major version number from a version string."""
    if not version:
        return None
    m = _SEMVER_RE.match(version.strip())
    if m:
        return int(m.group(1))
    return None


def _expand_remediation(finding: dict[str, Any], cve: str, repo: str) -> dict[str, Any]:
    """Expand a finding with full remediation plan details."""
    result = dict(finding)
    ecosystem = finding.get("ecosystem", "")
    affected_version = finding.get("affected_version", "")
    fixed_version = finding.get("fixed_version")
    package_name = finding.get("package", "")
    usage_sites = finding.get("usage_sites", [])

    fix_type = "version_bump" if fixed_version else "mitigation"

    # Files to change
    files_to_change: list[str] = []
    manifest = ECOSYSTEM_MANIFEST.get(ecosystem)
    if manifest:
        files_to_change.append(manifest)
    lockfiles = ECOSYSTEM_LOCKFILES.get(ecosystem, [])
    if lockfiles:
        files_to_change.append(lockfiles[0])

    # Breaking change risk
    breaking_risk = "unknown"
    if fixed_version:
        affected_major = _parse_major(affected_version)
        fixed_major = _parse_major(fixed_version)
        if affected_major is not None and fixed_major is not None:
            breaking_risk = "low" if affected_major == fixed_major else "high"

    # Complexity
    if fix_type == "mitigation":
        complexity = "complex"
    elif breaking_risk == "high":
        complexity = "moderate"
    else:
        complexity = "trivial"

    # start_here pointer
    start_here = None
    priority_order = {"reachable": 0, "called": 1, "imported": 2, "present": 3}
    sorted_sites = sorted(
        usage_sites, key=lambda s: priority_order.get(s.get("reachability_level", "present"), 99)
    )
    if sorted_sites:
        best = sorted_sites[0]
        file_path = best.get("file", "")
        line = best.get("line")
        start_here = f"{file_path}:{line}" if file_path and line else file_path or None

    # Files to verify
    files_to_verify: list[str] = []
    seen_files: set[str] = set()
    for site in usage_sites:
        fp = site.get("file", "")
        if fp and fp not in seen_files:
            seen_files.add(fp)
            files_to_verify.append(fp)
        for caller in site.get("callers", []):
            cf = caller.get("file", "")
            if cf and cf not in seen_files:
                seen_files.add(cf)
                files_to_verify.append(cf)

    # Generate steps
    steps: list[dict[str, Any]] = []
    order = 1
    if fix_type == "version_bump" and fixed_version:
        steps.append(
            {
                "order": order,
                "action": "update_dependency",
                "file": manifest or (files_to_change[0] if files_to_change else ""),
                "change": f"{package_name}: {affected_version} -> {fixed_version}",
            }
        )
        order += 1
        install_cmd = ECOSYSTEM_INSTALL_COMMANDS.get(ecosystem, "")
        if install_cmd:
            steps.append({"order": order, "action": "regenerate_lockfile", "command": install_cmd})
            order += 1
        if start_here:
            steps.append(
                {
                    "order": order,
                    "action": "verify_import_compatibility",
                    "target": start_here,
                    "check": f"{package_name} API compatibility with {fixed_version}",
                }
            )
            order += 1
    elif fix_type == "mitigation" and start_here:
        steps.append(
            {
                "order": order,
                "action": "identify_mitigation",
                "target": start_here,
                "note": f"No fix version available for {cve}. Investigate workaround at usage site.",
            }
        )
        order += 1

    steps.append(
        {
            "order": order,
            "action": "run_tests",
            "scope": f"tests touching {start_here}" if start_here else "full test suite",
        }
    )
    order += 1
    steps.append(
        {
            "order": order,
            "action": "verify_fix",
            "command": f"secure(cve='{cve}', repo='{repo}', action='verify')",
        }
    )

    result["remediation"] = {
        "fix_type": fix_type,
        "target_version": fixed_version,
        "steps": steps,
        "breaking_change_risk": breaking_risk,
        "breaking_change_details": (
            f"Major version change: {affected_version} -> {fixed_version}. "
            "API breaking changes likely."
        )
        if breaking_risk == "high"
        else None,
        "estimated_complexity": complexity,
        "files_to_change": files_to_change,
        "files_to_verify": files_to_verify,
        "start_here": start_here,
    }
    return result


# ---------------------------------------------------------------------------
# Verify logic (action=verify)
# ---------------------------------------------------------------------------


async def handle_verify(
    cve: str,
    repo: str,
    *,
    db_pool: Any | None = None,
) -> dict[str, Any]:
    """Post-fix re-check: is the CVE still present in this repo?

    Statuses: resolved, still_vulnerable, mitigated, unknown.
    """
    query_info = {"cve": cve, "repo": repo, "action": "verify"}
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    if not cve:
        return {
            "summary": "CVE identifier is required for verify action",
            "query": query_info,
            "status": "error",
            "details": {"error": "missing_cve"},
            "metadata": {"verified_at": now_iso},
        }
    if not repo:
        return {
            "summary": "Repository is required for verify action",
            "query": query_info,
            "status": "error",
            "details": {"error": "missing_repo"},
            "metadata": {"verified_at": now_iso},
        }
    if db_pool is None:
        return {
            "summary": f"Cannot verify {cve} — database not available",
            "query": query_info,
            "status": "unknown",
            "details": {"reason": "database_unavailable"},
            "metadata": {"verified_at": now_iso},
        }

    conn = None
    try:
        conn = db_pool.getconn()

        # Look up CVE
        with conn.cursor() as cur:
            cur.execute(
                "SELECT package, affected_versions, safe_version, severity "
                "FROM vulnerabilities WHERE cve_id = %s",
                (cve,),
            )
            row = cur.fetchone()
        if row is None:
            return {
                "summary": f"{cve} not found in vulnerability database",
                "query": query_info,
                "status": "unknown",
                "details": {"reason": "cve_not_in_database"},
                "metadata": {"verified_at": now_iso},
            }

        package, _affected_versions, safe_version, _severity = row

        # Check repository
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, repo_name, indexed_at FROM repositories WHERE repo_name = %s",
                (repo,),
            )
            repo_row = cur.fetchone()
        if repo_row is None:
            return {
                "summary": f"Repository {repo} not indexed",
                "query": query_info,
                "status": "unknown",
                "details": {"reason": "repo_not_indexed", "repo_indexed": False},
                "metadata": {"verified_at": now_iso},
            }

        repo_id, _repo_name, indexed_at = repo_row
        sbom_age = None
        if indexed_at is not None:
            if indexed_at.tzinfo is None:
                indexed_at = indexed_at.replace(tzinfo=timezone.utc)
            sbom_age = (datetime.now(timezone.utc) - indexed_at).total_seconds() / 3600.0

        # Check SBOM staleness
        if sbom_age is not None and sbom_age > SBOM_STALE_THRESHOLD_HOURS:
            return {
                "summary": f"SBOM for {repo} is stale ({sbom_age:.1f}h old)",
                "query": query_info,
                "status": "unknown",
                "details": {"reason": "sbom_stale", "sbom_age_hours": round(sbom_age, 1)},
                "metadata": {"sbom_age_hours": round(sbom_age, 1), "verified_at": now_iso},
            }

        # Check current dependency version
        pkg_name = _extract_package_name_from_purl(package)
        pattern = f"pkg:%/{pkg_name}@%"
        with conn.cursor() as cur:
            cur.execute(
                "SELECT version, package_coordinate FROM dependencies "
                "WHERE repo_id = %s AND package_coordinate LIKE %s "
                "ORDER BY package_coordinate LIMIT 1",
                (repo_id, pattern),
            )
            dep_row = cur.fetchone()

        if dep_row is None:
            return {
                "summary": f"{cve} resolved in {repo} (package removed)",
                "query": query_info,
                "status": "resolved",
                "details": {
                    "package": package,
                    "current_version": None,
                    "fixed_version": safe_version,
                    "vulnerability_still_present": False,
                    "resolution": "package_removed",
                },
                "metadata": {
                    "sbom_age_hours": round(sbom_age, 1) if sbom_age else None,
                    "verified_at": now_iso,
                },
            }

        current_version = dep_row[0]

        # Compare against safe_version
        if safe_version and not version_in_range(current_version, _affected_versions):
            return {
                "summary": f"{cve} resolved in {repo}",
                "query": query_info,
                "status": "resolved",
                "details": {
                    "package": package,
                    "current_version": current_version,
                    "fixed_version": safe_version,
                    "vulnerability_still_present": False,
                },
                "metadata": {
                    "sbom_age_hours": round(sbom_age, 1) if sbom_age else None,
                    "verified_at": now_iso,
                },
            }

        # Still vulnerable
        return {
            "summary": f"{cve} still vulnerable in {repo}",
            "query": query_info,
            "status": "still_vulnerable",
            "details": {
                "package": package,
                "current_version": current_version,
                "fixed_version": safe_version,
                "vulnerability_still_present": True,
                "reachability_check": "reachable",
            },
            "metadata": {
                "sbom_age_hours": round(sbom_age, 1) if sbom_age else None,
                "verified_at": now_iso,
            },
        }

    except Exception:
        log.exception("handle_verify failed for %s in %s", cve, repo)
        return {
            "summary": f"Error verifying {cve} in {repo}",
            "query": query_info,
            "status": "unknown",
            "details": {"reason": "internal_error"},
            "metadata": {"verified_at": now_iso},
        }
    finally:
        if conn is not None:
            try:
                db_pool.putconn(conn)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Public API: handle_identify (default action)
# ---------------------------------------------------------------------------


async def handle_identify(
    *,
    cve: str = "",
    repo: str = "",
    package: str = "",
    severity_min: str = "",
    reachable_only: bool = False,
    db_pool: Any | None = None,
    acl_store: PostgresACLStore | None = None,
    caller: CallerPrincipal | None = None,
    zoekt_backend: Any | None = None,
    neptune_driver: Any = None,
    s3_client: Any = None,
    s3_bucket: str = "",
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Full vulnerability assessment (default action).

    Orchestrates: resolve → reachability → prioritize → format response.
    """
    if caller is None or not caller.is_resolved:
        return {
            "summary": "Unauthorized",
            "query": {"cve": cve, "repo": repo, "package": package, "action": "identify"},
            "findings_count": 0,
            "findings": [],
        }

    if db_pool is None:
        return {
            "summary": "Database not available",
            "query": {"cve": cve, "repo": repo, "package": package, "action": "identify"},
            "findings_count": 0,
            "findings": [],
            "metadata": {"error": "database_unavailable"},
        }

    # Step 1: Resolve input to affected tuples
    affected, cve_known, repo_indexed = resolve_input(
        db_pool=db_pool,
        acl_store=acl_store,
        caller=caller,
        cve=cve or None,
        repo=repo or None,
        package=package or None,
    )

    if not affected:
        return {
            "summary": "No findings",
            "query": {"cve": cve, "repo": repo, "package": package, "action": "identify"},
            "findings_count": 0,
            "findings": [],
            "metadata": {"cve_known": cve_known, "repo_indexed": repo_indexed},
        }

    # Step 2: Determine reachability for each unique (repo, package) pair
    reachability_cache: dict[tuple[str, str], tuple[ReachabilityResult, list[UsageSite]]] = {}
    if zoekt_backend is not None:
        seen_pairs: set[tuple[str, str]] = set()
        for af in affected:
            pair = (af.repo_name, af.package_name)
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                reach_result, sites = await determine_reachability(
                    af.repo_name,
                    af.package_name,
                    af.package_ecosystem,
                    zoekt_backend=zoekt_backend,
                    neptune_driver=neptune_driver,
                    tenant_id=tenant_id,
                )
                reachability_cache[pair] = (reach_result, sites)

    # Step 3: Build findings with priority scores
    findings: list[Finding] = []
    for af in affected:
        pair = (af.repo_name, af.package_name)
        if pair in reachability_cache:
            reach, sites = reachability_cache[pair]
        else:
            reach = ReachabilityResult(
                level="reachable", confidence="assumed", source="code-index-fallback"
            )
            sites = []

        fix_available = af.fixed_version is not None and af.fixed_version != ""
        score, bucket = compute_priority_score(af.severity, reach.level, fix_available)

        finding = Finding(
            cve_id=af.cve_id,
            severity=af.severity,
            cvss_score=af.cvss_score,
            package=af.package_name,
            ecosystem=af.package_ecosystem,
            affected_version=af.package_version,
            fixed_version=af.fixed_version,
            purl=af.package_purl,
            reachability_level=reach.level,
            reachability_confidence=reach.confidence,
            reachability_source=reach.source,
            fix_available=fix_available,
            priority_score=score,
            priority=bucket,
            repos_affected=[af.repo_name],
            usage_sites=[
                {
                    "file": s.file,
                    "line": s.line,
                    "symbol": s.symbol,
                    "reachability_level": s.reachability_level,
                    "callers": s.callers,
                }
                for s in sites
            ],
        )
        findings.append(finding)

    # Step 4: Apply filters
    severity_min_rank = _SEVERITY_RANK.get(severity_min.upper(), 0) if severity_min else 0
    if severity_min_rank > 0:
        findings = [
            f for f in findings if _SEVERITY_RANK.get(f.severity.upper(), 0) >= severity_min_rank
        ]

    if reachable_only:
        findings = [f for f in findings if f.reachability_level in ("called", "reachable")]

    # Step 5: Sort by priority
    findings = _sort_findings(findings)

    # Step 6: SBOM freshness metadata
    metadata: dict[str, Any] = {"cve_known": cve_known, "repo_indexed": repo_indexed}
    if repo and s3_client and s3_bucket:
        freshness = await get_sbom_freshness(repo, s3_client, s3_bucket)
        metadata.update(freshness)

    # Format response
    findings_dicts = [
        {
            "cve_id": f.cve_id,
            "severity": f.severity,
            "cvss_score": f.cvss_score,
            "package": f.package,
            "ecosystem": f.ecosystem,
            "affected_version": f.affected_version,
            "fixed_version": f.fixed_version,
            "purl": f.purl,
            "priority": f.priority,
            "priority_score": round(f.priority_score, 3),
            "reachability": {
                "level": f.reachability_level,
                "confidence": f.reachability_confidence,
                "source": f.reachability_source,
            },
            "repos_affected": f.repos_affected,
            "usage_sites": f.usage_sites,
            "remediation": {
                "fix_available": f.fix_available,
                "fixed_version": f.fixed_version,
            },
        }
        for f in findings
    ]

    summary_parts = []
    if cve:
        summary_parts.append(cve)
    if repo:
        summary_parts.append(repo)
    if package:
        summary_parts.append(package)
    summary = f"{len(findings)} finding(s) for {' + '.join(summary_parts)}"

    return {
        "summary": summary,
        "query": {"cve": cve, "repo": repo, "package": package, "action": "identify"},
        "findings_count": len(findings),
        "findings": findings_dicts,
        "metadata": metadata,
    }


# ---------------------------------------------------------------------------
# Public API: handle_plan (action=plan)
# ---------------------------------------------------------------------------


async def handle_plan(
    *,
    cve: str,
    repo: str,
    db_pool: Any | None = None,
    acl_store: PostgresACLStore | None = None,
    caller: CallerPrincipal | None = None,
    zoekt_backend: Any | None = None,
    neptune_driver: Any = None,
    s3_client: Any = None,
    s3_bucket: str = "",
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Generate remediation steps for a (CVE, repo) pair."""
    if not cve or not repo:
        return {
            "error": "validation_error",
            "message": "action=plan requires both cve and repo parameters",
        }

    # First run identify to get the findings
    identify_result = await handle_identify(
        cve=cve,
        repo=repo,
        db_pool=db_pool,
        acl_store=acl_store,
        caller=caller,
        zoekt_backend=zoekt_backend,
        neptune_driver=neptune_driver,
        s3_client=s3_client,
        s3_bucket=s3_bucket,
        tenant_id=tenant_id,
    )

    if not identify_result.get("findings"):
        return {
            "summary": f"No findings for {cve} in {repo}",
            "query": {"cve": cve, "repo": repo, "action": "plan"},
            "findings_count": 0,
            "findings": [],
            "metadata": identify_result.get("metadata", {}),
        }

    # Expand each finding with remediation plan
    expanded = [_expand_remediation(f, cve, repo) for f in identify_result["findings"]]

    return {
        "summary": f"Remediation plan for {cve} in {repo}",
        "query": {"cve": cve, "repo": repo, "action": "plan"},
        "findings_count": len(expanded),
        "findings": expanded,
        "metadata": identify_result.get("metadata", {}),
    }
