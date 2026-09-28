"""
Shared fixtures for Knowledge Layer unit tests.

Provides:
- fake_vector_store: in-memory S3 Vectors substitute
- fake_acl_store: in-memory permission store (Postgres substitute)
- fake_dependency_store: in-memory dependencies table
- fixture_repo: path to the seeded fixture repo
- two_principals: two distinct CallerIdentity objects for isolation tests
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    """Path to the shared fixtures directory."""
    return FIXTURES_DIR


# ---------------------------------------------------------------------------
# Fake Vector Store (S3 Vectors substitute)
# ---------------------------------------------------------------------------


@dataclass
class FakeVectorStore:
    """In-memory vector store matching the S3 Vectors put/query contract.

    Used for unit/component tests where real S3 Vectors is unavailable.
    Production code should accept a VectorStoreProtocol that this class satisfies.
    """

    _indexes: dict[str, list[dict]] = field(default_factory=dict)

    def put_vectors(self, index_name: str, vectors: list[dict[str, Any]]) -> None:
        """Store vectors. Each vector: {key, embedding, metadata}."""
        if index_name not in self._indexes:
            self._indexes[index_name] = []
        self._indexes[index_name].extend(vectors)

    def query_vectors(
        self,
        index_name: str,
        query_vector: list[float],
        top_k: int = 10,
        filter_metadata: dict[str, Any] | None = None,
    ) -> list[dict]:
        """Naive cosine-similarity search over stored vectors."""
        if index_name not in self._indexes:
            return []
        vectors = self._indexes[index_name]

        # Apply metadata filter if provided
        if filter_metadata:
            vectors = [
                v
                for v in vectors
                if all(v.get("metadata", {}).get(k) == val for k, val in filter_metadata.items())
            ]

        # Compute cosine similarity (simplified: dot product for normalized vectors)
        def _dot(a: list[float], b: list[float]) -> float:
            return sum(x * y for x, y in zip(a, b))

        scored = []
        for v in vectors:
            emb = v.get("embedding", [])
            if len(emb) == len(query_vector):
                score = _dot(query_vector, emb)
            else:
                score = 0.0
            scored.append((score, v))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {"key": item["key"], "score": score, "metadata": item.get("metadata", {})}
            for score, item in scored[:top_k]
        ]

    def delete_vectors(self, index_name: str, keys: list[str]) -> None:
        """Remove vectors by key."""
        if index_name in self._indexes:
            self._indexes[index_name] = [
                v for v in self._indexes[index_name] if v.get("key") not in keys
            ]

    def list_indexes(self) -> list[str]:
        """List all index names."""
        return list(self._indexes.keys())


@pytest.fixture
def fake_vector_store() -> FakeVectorStore:
    """Fresh in-memory vector store."""
    return FakeVectorStore()


# ---------------------------------------------------------------------------
# Fake ACL Store (Postgres permissions substitute)
# ---------------------------------------------------------------------------


@dataclass
class FakeACLStore:
    """In-memory ACL store simulating the Postgres permissions table.

    Schema: repo_id -> set of principal_ids that have access.
    Public repos have principal_id = "__public__".
    """

    PUBLIC = "__public__"
    _acls: dict[str, set[str]] = field(default_factory=dict)

    def grant(self, repo_id: str, principal_id: str) -> None:
        """Grant a principal access to a repo."""
        self._acls.setdefault(repo_id, set()).add(principal_id)

    def revoke(self, repo_id: str, principal_id: str) -> None:
        """Revoke a principal's access to a repo."""
        if repo_id in self._acls:
            self._acls[repo_id].discard(principal_id)

    def set_public(self, repo_id: str) -> None:
        """Mark a repo as public (visible to all)."""
        self.grant(repo_id, self.PUBLIC)

    def get_accessible_repos(self, principal_id: str) -> set[str]:
        """Return repo_ids this principal can access (including public repos)."""
        accessible = set()
        for repo_id, principals in self._acls.items():
            if principal_id in principals or self.PUBLIC in principals:
                accessible.add(repo_id)
        return accessible

    def can_access(self, principal_id: str, repo_id: str) -> bool:
        """Check if a principal can access a specific repo."""
        if repo_id not in self._acls:
            return False  # Unknown repo → deny (fail-closed)
        principals = self._acls[repo_id]
        return principal_id in principals or self.PUBLIC in principals

    def filter_results(
        self, principal_id: str | None, results: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Filter search results by principal's access. Fail-closed on None principal."""
        if principal_id is None:
            return []  # Fail-closed: unknown identity → empty
        return [r for r in results if self.can_access(principal_id, r.get("repo_id", ""))]


@pytest.fixture
def fake_acl_store() -> FakeACLStore:
    """Fresh in-memory ACL store."""
    return FakeACLStore()


# ---------------------------------------------------------------------------
# Fake Dependency Store (Postgres dependencies table substitute)
# ---------------------------------------------------------------------------


@dataclass
class DependencyRow:
    """A row in the dependencies table."""

    repo_id: str
    package: str  # purl coordinate, e.g. "pkg:pypi/requests"
    version: str
    source: str = "lockfile"  # "lockfile" | "manifest" | "image"


@dataclass
class VulnerabilityRow:
    """A row in the vulnerabilities table."""

    vuln_id: str  # e.g. "CVE-2023-32681"
    package: str
    version_range: str  # affected version range
    severity: str = "HIGH"
    source: str = "osv"  # "osv" | "trivy"


@dataclass
class FakeDependencyStore:
    """In-memory substitute for the Postgres dependencies + vulnerabilities tables."""

    _dependencies: list[DependencyRow] = field(default_factory=list)
    _vulnerabilities: list[VulnerabilityRow] = field(default_factory=list)
    _issues_filed: list[dict[str, Any]] = field(default_factory=list)

    def add_dependency(self, row: DependencyRow) -> None:
        """Insert a dependency row."""
        self._dependencies.append(row)

    def add_vulnerability(self, row: VulnerabilityRow) -> None:
        """Insert a vulnerability row."""
        self._vulnerabilities.append(row)

    def reverse_lookup(self, package: str, version: str) -> list[str]:
        """Which repos use this exact package@version?"""
        return [
            d.repo_id for d in self._dependencies if d.package == package and d.version == version
        ]

    def get_dependencies_for_repo(self, repo_id: str) -> list[DependencyRow]:
        """Get all dependencies for a repo."""
        return [d for d in self._dependencies if d.repo_id == repo_id]

    def file_issue(self, repo_id: str, vuln_id: str, details: dict[str, Any]) -> None:
        """Record that an issue was filed for a vulnerability in a repo."""
        self._issues_filed.append({"repo_id": repo_id, "vuln_id": vuln_id, "details": details})

    def issue_already_filed(self, repo_id: str, vuln_id: str) -> bool:
        """Check if an issue was already filed (idempotency)."""
        return any(i["repo_id"] == repo_id and i["vuln_id"] == vuln_id for i in self._issues_filed)


@pytest.fixture
def fake_dependency_store() -> FakeDependencyStore:
    """Fresh in-memory dependency store."""
    return FakeDependencyStore()


# ---------------------------------------------------------------------------
# Principal fixtures for isolation tests
# ---------------------------------------------------------------------------


@dataclass
class FakePrincipal:
    """A test principal (user/agent identity)."""

    principal_id: str
    display_name: str


@pytest.fixture
def principal_a() -> FakePrincipal:
    """First test principal."""
    return FakePrincipal(principal_id=str(uuid.uuid4()), display_name="user-alice")


@pytest.fixture
def principal_b() -> FakePrincipal:
    """Second test principal (different user, same org)."""
    return FakePrincipal(principal_id=str(uuid.uuid4()), display_name="user-bob")


@pytest.fixture
def principal_other_org() -> FakePrincipal:
    """Third test principal (different org entirely)."""
    return FakePrincipal(principal_id=str(uuid.uuid4()), display_name="user-eve-other-org")
