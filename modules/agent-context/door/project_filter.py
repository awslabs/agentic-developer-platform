"""Project-scoped query filter for the Door (MCP query layer).

Resolves a project_id to its set of repo names, verifying ownership.
The project filter is a SOFT narrowing step — it can only restrict results
to a subset of what the caller already has access to (via #1721 isolation).
It can NEVER widen visibility.

Position in the pipeline:
  Step 1: Identity extraction (CallerPrincipal)
  Step 2: #1721 isolation filter (fail-closed)
  Step 3: Project filter (this module)  <-- intersects
  Step 4: Backend query

See: docs/agent-context/design-1728-project-scoping.md §3.
"""

from __future__ import annotations

import logging
import uuid as uuid_mod
from dataclasses import dataclass
from typing import Any

from .acl import SearchHit

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectScope:
    """Resolved project scope — the set of repo names in a project."""

    project_id: str
    repo_names: frozenset[str]


class ProjectFilterError(Exception):
    """Raised when project resolution fails (not found / not owned)."""

    def __init__(self, message: str, code: str = "project_not_found"):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _is_uuid(value: str) -> bool:
    """Check if a string is a valid UUID."""
    try:
        uuid_mod.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


def resolve_project_repos(
    project_id: str,
    caller_owner_sub: str,
    db_pool: Any,
    *,
    caller_tenant_id: str = "",
) -> ProjectScope:
    """Resolve a project to its repo_name set with ownership verification.

    Parameters
    ----------
    project_id:
        Project UUID or project name (name is scoped to caller's owner_sub).
    caller_owner_sub:
        The caller's owner_sub (Cognito sub). Used for ownership check.
    db_pool:
        psycopg2 connection pool.
    caller_tenant_id:
        The caller's tenant_id. Used for team-project access (future).

    Returns
    -------
    ProjectScope with the project_id and frozenset of repo_names.

    Raises
    ------
    ProjectFilterError:
        If the project does not exist, is not owned by the caller,
        or the caller_owner_sub is empty.
    """
    if not project_id or not project_id.strip():
        raise ProjectFilterError("project_id is required", code="project_invalid")

    if not caller_owner_sub:
        raise ProjectFilterError(
            "Cannot resolve project without caller identity (owner_sub)",
            code="project_no_identity",
        )

    project_id = project_id.strip()

    # Determine lookup strategy: UUID vs name
    if _is_uuid(project_id):
        resolved_id = _resolve_by_id(project_id, caller_owner_sub, caller_tenant_id, db_pool)
    else:
        resolved_id = _resolve_by_name(project_id, caller_owner_sub, db_pool)

    if resolved_id is None:
        raise ProjectFilterError(
            f"Project '{project_id}' not found or not owned by caller",
            code="project_not_found",
        )

    # Fetch repo names for the resolved project
    repo_names = _fetch_project_repo_names(resolved_id, db_pool)

    return ProjectScope(project_id=resolved_id, repo_names=frozenset(repo_names))


def _resolve_by_id(
    project_uuid: str,
    caller_owner_sub: str,
    caller_tenant_id: str,
    db_pool: Any,
) -> str | None:
    """Look up project by UUID with ownership verification.

    Ownership rules:
    - Personal project (tenant_id IS NULL): owner_sub must match
    - Team project (tenant_id IS NOT NULL): caller must be in same tenant
    """
    query = """
        SELECT id::text FROM projects
        WHERE id = %s
          AND (
              (tenant_id IS NULL AND owner_sub = %s)
              OR (tenant_id IS NOT NULL AND tenant_id = %s)
          )
    """
    params = (project_uuid, caller_owner_sub, caller_tenant_id)

    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        db_pool.putconn(conn)


def _resolve_by_name(
    project_name: str,
    caller_owner_sub: str,
    db_pool: Any,
) -> str | None:
    """Look up project by name, scoped to caller's owner_sub.

    Name-based lookup only works for personal projects (prevents
    cross-user project name enumeration).
    """
    query = """
        SELECT id::text FROM projects
        WHERE name = %s AND owner_sub = %s
    """
    params = (project_name, caller_owner_sub)

    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        db_pool.putconn(conn)


def _fetch_project_repo_names(project_id: str, db_pool: Any) -> set[str]:
    """Fetch the repo_names for all repositories in a project.

    Joins project_repositories -> repositories to get repo_name.
    """
    query = """
        SELECT r.repo_name
        FROM project_repositories pr
        JOIN repositories r ON r.id = pr.repo_id
        WHERE pr.project_id = %s
    """
    params = (project_id,)

    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            return {row[0] for row in rows}
    finally:
        db_pool.putconn(conn)


# ---------------------------------------------------------------------------
# Pipeline integration helper
# ---------------------------------------------------------------------------


def apply_project_filter(
    hits: list[SearchHit],
    project_scope: ProjectScope | None,
) -> list[SearchHit]:
    """Intersect search hits with the project's repo set.

    Parameters
    ----------
    hits:
        ACL-filtered search hits (already narrowed by #1721 isolation).
    project_scope:
        Resolved project scope, or None if no project filter is active.

    Returns
    -------
    Hits whose repo_name is in the project's repo set.
    If project_scope is None, returns hits unchanged (passthrough).
    If project has no repos (empty set), returns [] (intersection with empty = empty).
    """
    if project_scope is None:
        return hits

    # Intersection: only keep hits whose repo is in the project
    return [hit for hit in hits if hit.repo_name in project_scope.repo_names]
