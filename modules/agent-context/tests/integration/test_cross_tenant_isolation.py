"""Cross-tenant isolation integration tests — E8 security gate (#1777).

Prove that tenant A's content is NEVER visible to tenant B through any verb,
fail-closed. This is the blocking security gate for E8 multi-tenancy.

Test structure:
- Index content as Tenant A → query as Tenant B → assert EMPTY
- Query as Tenant A → assert results ARE returned (positive control)
- All 5 verbs exercised: search, understand, impact, browse, experience
- Fail-closed: no identity / ambiguous identity → empty / error

Security invariants validated:
1. Cross-tenant code search isolation (Zoekt → ACL filter)
2. Cross-tenant structural data isolation (understand verb)
3. Cross-tenant call-graph isolation (impact verb)
4. Cross-tenant browse isolation (browse verb)
5. Cross-tenant experience/personal context isolation
6. Fail-closed on missing identity (no headers)
7. Fail-closed on partial identity (tenant only, no GitHub)
8. Shared (public) repos remain visible to both tenants

See: design §11 (Child 8), §16 security invariants.
"""

from __future__ import annotations

import json

import pytest

from .conftest import (
    TENANT_A_ID,
    TENANT_B_ID,
    USER_A_LOGIN,
    USER_A_OWNER_SUB,
    headers_tenant_a,
    headers_tenant_b,
)


# ---------------------------------------------------------------------------
# Helper: call the /call endpoint (MCP tool invocation)
# ---------------------------------------------------------------------------


async def _call_tool(
    client,
    tool_name: str,
    arguments: dict,
    headers: dict[str, str] | None = None,
) -> dict:
    """Invoke an MCP tool via the REST /call endpoint.

    Returns the parsed JSON response body.
    """
    payload = {"name": tool_name, "arguments": arguments}
    resp = await client.post(
        "/call",
        content=json.dumps(payload),
        headers=headers or {},
    )
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    return resp.json()


# ===========================================================================
# SEARCH VERB — Cross-tenant isolation
# ===========================================================================


@pytest.mark.asyncio
class TestSearchIsolation:
    """Tenant B cannot see Tenant A's code via the search verb."""

    async def test_tenant_a_sees_own_results(self, client) -> None:
        """Positive control: Tenant A searching for their marker gets results."""
        result = await _call_tool(
            client,
            "search",
            {"query": "ISOLATION_MARKER_ALPHA", "scope": "code"},
            headers=headers_tenant_a(),
        )
        assert result["total"] > 0, (
            f"POSITIVE CONTROL FAILED: Tenant A should see their own content. Got: {result}"
        )
        # Verify all results belong to tenant A's repos
        for item in result["results"]:
            assert (
                item["repo_id"].startswith("org-alpha/") or item["repo_id"] == "community/open-lib"
            ), f"Tenant A got result from unexpected repo: {item['repo_id']}"

    async def test_tenant_b_cannot_see_tenant_a_content(self, client) -> None:
        """SECURITY GATE: Tenant B searching for Tenant A's marker gets ZERO results."""
        result = await _call_tool(
            client,
            "search",
            {"query": "ISOLATION_MARKER_ALPHA", "scope": "code"},
            headers=headers_tenant_b(),
        )
        assert result["total"] == 0, (
            f"CROSS-TENANT ISOLATION VIOLATION (search): "
            f"Tenant B ({TENANT_B_ID}) saw {result['total']} results containing "
            f"Tenant A ({TENANT_A_ID}) content. Results: {result['results']}"
        )

    async def test_tenant_a_cannot_see_tenant_b_content(self, client) -> None:
        """SECURITY GATE: Tenant A searching for Tenant B's marker gets ZERO results."""
        result = await _call_tool(
            client,
            "search",
            {"query": "ISOLATION_MARKER_BETA", "scope": "code"},
            headers=headers_tenant_a(),
        )
        assert result["total"] == 0, (
            f"CROSS-TENANT ISOLATION VIOLATION (search): "
            f"Tenant A ({TENANT_A_ID}) saw {result['total']} results containing "
            f"Tenant B ({TENANT_B_ID}) content. Results: {result['results']}"
        )

    async def test_shared_term_scoped_to_own_repos(self, client) -> None:
        """When both tenants have content matching a query, each only sees their own."""
        # Tenant A searches for shared term
        result_a = await _call_tool(
            client,
            "search",
            {"query": "SHARED_SEARCH_TERM", "scope": "code"},
            headers=headers_tenant_a(),
        )
        # Tenant B searches for same term
        result_b = await _call_tool(
            client,
            "search",
            {"query": "SHARED_SEARCH_TERM", "scope": "code"},
            headers=headers_tenant_b(),
        )

        # Verify tenant A only sees alpha repos + shared
        for item in result_a["results"]:
            assert (
                item["repo_id"].startswith("org-alpha/") or item["repo_id"] == "community/open-lib"
            ), f"Tenant A got cross-tenant result: {item['repo_id']}"

        # Verify tenant B only sees beta repos + shared
        for item in result_b["results"]:
            assert (
                item["repo_id"].startswith("org-beta/") or item["repo_id"] == "community/open-lib"
            ), f"Tenant B got cross-tenant result: {item['repo_id']}"

        # Both should see shared repo
        a_repos = {item["repo_id"] for item in result_a["results"]}
        b_repos = {item["repo_id"] for item in result_b["results"]}
        assert "community/open-lib" in a_repos, "Tenant A should see shared repo"
        assert "community/open-lib" in b_repos, "Tenant B should see shared repo"

        # Neither should see the other's private repos
        assert not any(r.startswith("org-beta/") for r in a_repos), (
            f"ISOLATION VIOLATION: Tenant A sees Tenant B repos: {a_repos}"
        )
        assert not any(r.startswith("org-alpha/") for r in b_repos), (
            f"ISOLATION VIOLATION: Tenant B sees Tenant A repos: {b_repos}"
        )

    async def test_tenant_b_sees_own_results(self, client) -> None:
        """Positive control: Tenant B searching for their marker gets results."""
        result = await _call_tool(
            client,
            "search",
            {"query": "ISOLATION_MARKER_BETA", "scope": "code"},
            headers=headers_tenant_b(),
        )
        assert result["total"] > 0, (
            f"POSITIVE CONTROL FAILED: Tenant B should see their own content. Got: {result}"
        )
        for item in result["results"]:
            assert item["repo_id"].startswith("org-beta/"), (
                f"Tenant B got result from unexpected repo: {item['repo_id']}"
            )


# ===========================================================================
# UNDERSTAND VERB — Cross-tenant isolation
# ===========================================================================


@pytest.mark.asyncio
class TestUnderstandIsolation:
    """Tenant B cannot see Tenant A's structural data via the understand verb.

    Note: Without S3 backend, understand returns "Structural index not available".
    The ACL filter still runs on any hits returned, and we verify the path is
    exercised correctly. The verb returns empty definitions gracefully.
    """

    async def test_tenant_a_understand_own_target(self, client) -> None:
        """Tenant A can attempt to understand their own repo target."""
        result = await _call_tool(
            client,
            "understand",
            {"target": "org-alpha/service-core::handle_request"},
            headers=headers_tenant_a(),
        )
        # Even without S3 backend, no cross-tenant leak
        assert "error" not in result or "Unknown tool" not in result.get("error", "")
        # definitions is the key field - should not contain cross-tenant data
        definitions = result.get("definitions", [])
        for d in definitions:
            repo = d.get("repo_id", "")
            assert repo.startswith("org-alpha/") or repo == "community/open-lib", (
                f"ISOLATION VIOLATION (understand): Tenant A got cross-tenant result: {repo}"
            )

    async def test_tenant_b_cannot_understand_tenant_a_target(self, client) -> None:
        """SECURITY GATE: Tenant B cannot see Tenant A's structural definitions."""
        result = await _call_tool(
            client,
            "understand",
            {"target": "org-alpha/service-core::handle_request"},
            headers=headers_tenant_b(),
        )
        definitions = result.get("definitions", [])
        for d in definitions:
            repo = d.get("repo_id", "")
            assert not repo.startswith("org-alpha/"), (
                f"CROSS-TENANT ISOLATION VIOLATION (understand): "
                f"Tenant B ({TENANT_B_ID}) saw definition from Tenant A repo: {repo}. "
                f"Full result: {d}"
            )


# ===========================================================================
# IMPACT VERB — Cross-tenant isolation
# ===========================================================================


@pytest.mark.asyncio
class TestImpactIsolation:
    """Tenant B cannot see Tenant A's impact/call-graph data."""

    async def test_tenant_a_impact_own_target(self, client) -> None:
        """Tenant A can attempt impact analysis on their own target."""
        result = await _call_tool(
            client,
            "impact",
            {"target": "org-alpha/service-core::handle_request", "cross_repo": False},
            headers=headers_tenant_a(),
        )
        # Should not error
        assert "error" not in result or "Unknown tool" not in result.get("error", "")
        affected = result.get("affected", [])
        for item in affected:
            repo = item.get("repo_id", "")
            assert repo.startswith("org-alpha/") or repo == "community/open-lib", (
                f"ISOLATION VIOLATION (impact): Tenant A got cross-tenant result: {repo}"
            )

    async def test_tenant_b_cannot_see_tenant_a_impact(self, client) -> None:
        """SECURITY GATE: Tenant B cannot see Tenant A's call-graph."""
        result = await _call_tool(
            client,
            "impact",
            {"target": "org-alpha/service-core::handle_request", "cross_repo": True},
            headers=headers_tenant_b(),
        )
        affected = result.get("affected", [])
        for item in affected:
            repo = item.get("repo_id", "")
            assert not repo.startswith("org-alpha/"), (
                f"CROSS-TENANT ISOLATION VIOLATION (impact): "
                f"Tenant B ({TENANT_B_ID}) saw impact data from Tenant A repo: {repo}. "
                f"Full item: {item}"
            )

    async def test_tenant_a_cannot_see_tenant_b_impact(self, client) -> None:
        """SECURITY GATE: Tenant A cannot see Tenant B's call-graph."""
        result = await _call_tool(
            client,
            "impact",
            {"target": "org-beta/platform::main", "cross_repo": True},
            headers=headers_tenant_a(),
        )
        affected = result.get("affected", [])
        for item in affected:
            repo = item.get("repo_id", "")
            assert not repo.startswith("org-beta/"), (
                f"CROSS-TENANT ISOLATION VIOLATION (impact): "
                f"Tenant A ({TENANT_A_ID}) saw impact data from Tenant B repo: {repo}. "
                f"Full item: {item}"
            )


# ===========================================================================
# BROWSE VERB — Cross-tenant isolation
# ===========================================================================


@pytest.mark.asyncio
class TestBrowseIsolation:
    """Tenant B cannot browse Tenant A's indexed content."""

    async def test_tenant_a_browse_own_repo(self, client) -> None:
        """Tenant A can attempt to browse their own repo."""
        result = await _call_tool(
            client,
            "browse",
            {"action": "ls", "uri": "org-alpha/service-core/"},
            headers=headers_tenant_a(),
        )
        assert "error" not in result or "Unknown tool" not in result.get("error", "")
        entries = result.get("entries", [])
        for entry in entries:
            repo = entry.get("repo_id", entry.get("repo", ""))
            if repo:
                assert repo.startswith("org-alpha/") or repo == "community/open-lib", (
                    f"ISOLATION VIOLATION (browse): Tenant A got cross-tenant entry: {repo}"
                )

    async def test_tenant_b_cannot_browse_tenant_a_repo(self, client) -> None:
        """SECURITY GATE: Tenant B cannot browse Tenant A's files."""
        result = await _call_tool(
            client,
            "browse",
            {"action": "ls", "uri": "org-alpha/service-core/"},
            headers=headers_tenant_b(),
        )
        entries = result.get("entries", [])
        for entry in entries:
            repo = entry.get("repo_id", entry.get("repo", ""))
            if repo:
                assert not repo.startswith("org-alpha/"), (
                    f"CROSS-TENANT ISOLATION VIOLATION (browse): "
                    f"Tenant B ({TENANT_B_ID}) browsed Tenant A content: {entry}"
                )

    async def test_tenant_a_cannot_browse_tenant_b_repo(self, client) -> None:
        """SECURITY GATE: Tenant A cannot browse Tenant B's files."""
        result = await _call_tool(
            client,
            "browse",
            {"action": "ls", "uri": "org-beta/platform/"},
            headers=headers_tenant_a(),
        )
        entries = result.get("entries", [])
        for entry in entries:
            repo = entry.get("repo_id", entry.get("repo", ""))
            if repo:
                assert not repo.startswith("org-beta/"), (
                    f"CROSS-TENANT ISOLATION VIOLATION (browse): "
                    f"Tenant A ({TENANT_A_ID}) browsed Tenant B content: {entry}"
                )


# ===========================================================================
# EXPERIENCE VERB — Cross-tenant isolation
# ===========================================================================


@pytest.mark.asyncio
class TestExperienceIsolation:
    """Tenant B cannot see Tenant A's experience/personal context entries."""

    async def test_tenant_a_save_and_recall(self, client) -> None:
        """Positive control: Tenant A can save and recall their own entry."""
        content = "alpha team uses blue-green deployments"

        # Save
        save_result = await _call_tool(
            client,
            "experience",
            {
                "action": "save",
                "persona": "developer",
                "content": content,
                "visibility": "private",
            },
            headers=headers_tenant_a(),
        )
        assert save_result.get("status") == "saved" or "id" in save_result, (
            f"Save failed for Tenant A: {save_result}"
        )

        # Recall using the same content (tests isolation, not recall quality)
        recall_result = await _call_tool(
            client,
            "experience",
            {
                "action": "recall",
                "persona": "developer",
                "query": content,
            },
            headers=headers_tenant_a(),
        )
        assert recall_result.get("total", 0) >= 1, (
            f"POSITIVE CONTROL FAILED: Tenant A should recall their own entry. Got: {recall_result}"
        )

    async def test_tenant_b_cannot_recall_tenant_a_private(self, client) -> None:
        """SECURITY GATE: Tenant B cannot recall Tenant A's private entries."""
        # Tenant A saves a private entry
        await _call_tool(
            client,
            "experience",
            {
                "action": "save",
                "persona": "developer",
                "content": "SECRET_ALPHA_PATTERN our proprietary algorithm uses FFT",
                "visibility": "private",
            },
            headers=headers_tenant_a(),
        )

        # Tenant B tries to recall with the exact same content
        recall_result = await _call_tool(
            client,
            "experience",
            {
                "action": "recall",
                "persona": "developer",
                "query": "SECRET_ALPHA_PATTERN proprietary algorithm FFT",
            },
            headers=headers_tenant_b(),
        )

        assert recall_result.get("total", 0) == 0, (
            f"CROSS-TENANT ISOLATION VIOLATION (experience/private): "
            f"Tenant B ({TENANT_B_ID}) recalled {recall_result.get('total')} entries "
            f"belonging to Tenant A ({TENANT_A_ID}). "
            f"Results: {recall_result.get('results')}"
        )

    async def test_tenant_b_cannot_recall_tenant_a_shared(self, client) -> None:
        """SECURITY GATE: Tenant B cannot recall Tenant A's shared entries."""
        # Tenant A saves a shared entry (shared within their own tenant)
        await _call_tool(
            client,
            "experience",
            {
                "action": "save",
                "persona": "operations",
                "content": "SHARED_ALPHA_OPS always restart workers after config change",
                "visibility": "shared",
            },
            headers=headers_tenant_a(),
        )

        # Tenant B tries to recall
        recall_result = await _call_tool(
            client,
            "experience",
            {
                "action": "recall",
                "persona": "operations",
                "query": "SHARED_ALPHA_OPS restart workers config change",
            },
            headers=headers_tenant_b(),
        )

        assert recall_result.get("total", 0) == 0, (
            f"CROSS-TENANT ISOLATION VIOLATION (experience/shared): "
            f"Tenant B ({TENANT_B_ID}) recalled shared entries "
            f"from Tenant A ({TENANT_A_ID}). "
            f"Results: {recall_result.get('results')}"
        )

    async def test_tenant_b_sees_own_shared(self, client) -> None:
        """Positive control: Tenant B's shared entries are visible to Tenant B."""
        content = "beta shared pattern use structured logging everywhere"

        # Save as Tenant B (shared within their tenant)
        await _call_tool(
            client,
            "experience",
            {
                "action": "save",
                "persona": "developer",
                "content": content,
                "visibility": "shared",
            },
            headers=headers_tenant_b(),
        )

        # Recall as Tenant B (exact content match — tests isolation, not recall quality)
        recall_result = await _call_tool(
            client,
            "experience",
            {
                "action": "recall",
                "persona": "developer",
                "query": content,
            },
            headers=headers_tenant_b(),
        )

        assert recall_result.get("total", 0) >= 1, (
            f"POSITIVE CONTROL FAILED: Tenant B should see their own shared entries. "
            f"Got: {recall_result}"
        )


# ===========================================================================
# FAIL-CLOSED — No identity / ambiguous identity
# ===========================================================================


@pytest.mark.asyncio
class TestFailClosed:
    """Requests with no identity or ambiguous identity get empty results.

    This validates the fail-closed security contract: when the system cannot
    determine the caller's identity, it MUST return empty — never all results.
    """

    async def test_no_headers_search_empty(self, client) -> None:
        """Search with no identity headers returns empty results (not all)."""
        result = await _call_tool(
            client,
            "search",
            {"query": "ISOLATION_MARKER_ALPHA", "scope": "code"},
            headers={},
        )
        assert result["total"] == 0, (
            f"FAIL-CLOSED VIOLATION (search, no headers): "
            f"Got {result['total']} results without identity. "
            f"Results: {result['results']}"
        )

    async def test_no_headers_understand_empty(self, client) -> None:
        """Understand with no identity headers returns empty definitions."""
        result = await _call_tool(
            client,
            "understand",
            {"target": "org-alpha/service-core::handle_request"},
            headers={},
        )
        definitions = result.get("definitions", [])
        assert len(definitions) == 0, (
            f"FAIL-CLOSED VIOLATION (understand, no headers): "
            f"Got {len(definitions)} definitions without identity. "
            f"Definitions: {definitions}"
        )

    async def test_no_headers_impact_empty(self, client) -> None:
        """Impact with no identity headers returns empty affected list."""
        result = await _call_tool(
            client,
            "impact",
            {"target": "org-alpha/service-core::handle_request"},
            headers={},
        )
        affected = result.get("affected", [])
        assert len(affected) == 0, (
            f"FAIL-CLOSED VIOLATION (impact, no headers): "
            f"Got {len(affected)} impact items without identity. "
            f"Affected: {affected}"
        )

    async def test_no_headers_browse_empty(self, client) -> None:
        """Browse with no identity headers returns empty entries."""
        result = await _call_tool(
            client,
            "browse",
            {"action": "ls", "uri": "/"},
            headers={},
        )
        entries = result.get("entries", [])
        assert len(entries) == 0, (
            f"FAIL-CLOSED VIOLATION (browse, no headers): "
            f"Got {len(entries)} entries without identity. "
            f"Entries: {entries}"
        )

    async def test_no_headers_experience_errors(self, client) -> None:
        """Experience with no identity headers returns error (fail-closed)."""
        result = await _call_tool(
            client,
            "experience",
            {
                "action": "recall",
                "persona": "developer",
                "query": "anything",
            },
            headers={},
        )
        # Experience tool requires identity headers — should error
        assert "error" in result or result.get("total", 0) == 0, (
            f"FAIL-CLOSED VIOLATION (experience, no headers): "
            f"Got results without identity. Result: {result}"
        )

    async def test_tenant_only_no_github_search_empty(self, client) -> None:
        """Tenant headers without GitHub identity → empty (fail-closed).

        Per design: tenant/owner headers alone cannot establish identity.
        The caller must have at least x-github-login or x-github-teams.
        """
        result = await _call_tool(
            client,
            "search",
            {"query": "ISOLATION_MARKER_ALPHA", "scope": "code"},
            headers={
                "X-Tenant-Id": TENANT_A_ID,
                "X-Owner-Sub": USER_A_OWNER_SUB,
            },
        )
        assert result["total"] == 0, (
            f"FAIL-CLOSED VIOLATION (search, tenant-only headers): "
            f"Got {result['total']} results with only tenant headers (no GitHub identity). "
            f"Results: {result['results']}"
        )

    async def test_github_only_no_tenant_sees_shared_only(self, client) -> None:
        """GitHub identity without tenant headers → sees only shared (no tenant repos).

        Per design: without X-Tenant-Id, caller cannot match per-tenant repos,
        but shared repos (tenant_id=NULL) with matching principals remain visible.
        """
        result = await _call_tool(
            client,
            "search",
            {"query": "SHARED_SEARCH_TERM", "scope": "code"},
            headers={
                "X-GitHub-Login": USER_A_LOGIN,
                "X-GitHub-Teams": ",".join(["org-alpha/eng"]),
                # No X-Tenant-Id or X-Owner-Sub
            },
        )
        # Should see shared repo content only (no tenant-scoped repos)
        for item in result["results"]:
            repo = item["repo_id"]
            assert repo == "community/open-lib", (
                f"FAIL-CLOSED VIOLATION (no tenant header): "
                f"Caller without X-Tenant-Id saw per-tenant repo: {repo}"
            )

    async def test_empty_github_login_fails_closed(self, client) -> None:
        """Empty X-GitHub-Login (whitespace) is treated as absent → fail-closed."""
        result = await _call_tool(
            client,
            "search",
            {"query": "ISOLATION_MARKER_ALPHA", "scope": "code"},
            headers={
                "X-GitHub-Login": "   ",  # whitespace only
                "X-Tenant-Id": TENANT_A_ID,
                "X-Owner-Sub": USER_A_OWNER_SUB,
            },
        )
        assert result["total"] == 0, (
            f"FAIL-CLOSED VIOLATION (empty login): "
            f"Got {result['total']} results with empty GitHub login. "
            f"Results: {result['results']}"
        )


# ===========================================================================
# BIDIRECTIONAL ISOLATION — Verify isolation works in both directions
# ===========================================================================


@pytest.mark.asyncio
class TestBidirectionalIsolation:
    """Comprehensive bidirectional check: A cannot see B, B cannot see A.

    This class runs the same search query for both tenants and verifies
    complete isolation in both directions simultaneously.
    """

    async def test_bidirectional_search_isolation(self, client) -> None:
        """Neither tenant can see the other's exclusive content via search."""
        # Tenant A: should see alpha marker, NOT beta marker
        alpha_for_a = await _call_tool(
            client,
            "search",
            {"query": "ISOLATION_MARKER_ALPHA", "scope": "code"},
            headers=headers_tenant_a(),
        )
        beta_for_a = await _call_tool(
            client,
            "search",
            {"query": "ISOLATION_MARKER_BETA", "scope": "code"},
            headers=headers_tenant_a(),
        )

        # Tenant B: should see beta marker, NOT alpha marker
        alpha_for_b = await _call_tool(
            client,
            "search",
            {"query": "ISOLATION_MARKER_ALPHA", "scope": "code"},
            headers=headers_tenant_b(),
        )
        beta_for_b = await _call_tool(
            client,
            "search",
            {"query": "ISOLATION_MARKER_BETA", "scope": "code"},
            headers=headers_tenant_b(),
        )

        # Assertions
        assert alpha_for_a["total"] > 0, "Tenant A should see ALPHA marker"
        assert beta_for_b["total"] > 0, "Tenant B should see BETA marker"
        assert beta_for_a["total"] == 0, (
            f"BIDIRECTIONAL VIOLATION: Tenant A saw BETA content: {beta_for_a['results']}"
        )
        assert alpha_for_b["total"] == 0, (
            f"BIDIRECTIONAL VIOLATION: Tenant B saw ALPHA content: {alpha_for_b['results']}"
        )

    async def test_shared_repo_visible_to_both(self, client) -> None:
        """Shared (public) repo content is visible to both tenants equally."""
        result_a = await _call_tool(
            client,
            "search",
            {"query": "open_function", "scope": "code"},
            headers=headers_tenant_a(),
        )
        result_b = await _call_tool(
            client,
            "search",
            {"query": "open_function", "scope": "code"},
            headers=headers_tenant_b(),
        )

        # Both should find the shared content
        assert result_a["total"] > 0, "Tenant A should see shared repo content"
        assert result_b["total"] > 0, "Tenant B should see shared repo content"

        # Both should only see it from the shared repo
        for item in result_a["results"]:
            assert item["repo_id"] == "community/open-lib"
        for item in result_b["results"]:
            assert item["repo_id"] == "community/open-lib"
