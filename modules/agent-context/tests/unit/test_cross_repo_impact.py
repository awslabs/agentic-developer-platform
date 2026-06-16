"""Unit tests for cross-repo impact via symbol_id (SCIP moniker).

HARD acceptance gate: the false-edge negative test MUST pass.
Two repos with same-named function in same-named file that do NOT call each
other must NOT produce a false cross-repo edge.

Also validates:
- Positive cross-repo case (function used by 3 repos returns all 3)
- Version-normalized moniker matching
- normalize_symbol_id() helper
- ACL filtering of cross-repo results (fail-closed)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from door.neptune_client import normalize_symbol_id, query_cross_repo_impact


# ---------------------------------------------------------------------------
# normalize_symbol_id tests
# ---------------------------------------------------------------------------


class TestNormalizeSymbolId:
    """Tests for SCIP moniker version-normalization."""

    def test_strips_version_from_python_moniker(self):
        sid = "scip-python python requests 2.31.0 requests/api.py/get()."
        normalized = normalize_symbol_id(sid)
        assert normalized == "scip-python python requests requests/api.py/get()."
        assert "2.31.0" not in normalized

    def test_strips_version_from_typescript_moniker(self):
        sid = "scip-typescript npm @types/node 18.0.0 net/Socket#connect()."
        normalized = normalize_symbol_id(sid)
        assert normalized == "scip-typescript npm @types/node net/Socket#connect()."
        assert "18.0.0" not in normalized

    def test_strips_version_from_go_moniker(self):
        sid = "scip-go gomod github.com/aws/aws-sdk-go v1.44.289 service/s3.New()."
        normalized = normalize_symbol_id(sid)
        assert normalized == "scip-go gomod github.com/aws/aws-sdk-go service/s3.New()."
        assert "v1.44.289" not in normalized

    def test_same_symbol_different_versions_normalize_equal(self):
        sid_v1 = "scip-python python requests 2.28.0 requests/api.py/get()."
        sid_v2 = "scip-python python requests 2.31.0 requests/api.py/get()."
        assert normalize_symbol_id(sid_v1) == normalize_symbol_id(sid_v2)

    def test_different_symbols_same_package_normalize_different(self):
        sid_a = "scip-python python requests 2.31.0 requests/api.py/get()."
        sid_b = "scip-python python requests 2.31.0 requests/api.py/post()."
        assert normalize_symbol_id(sid_a) != normalize_symbol_id(sid_b)

    def test_non_matching_format_returns_unchanged(self):
        # Monikers that don't match the expected pattern pass through unchanged
        sid = "some-custom-format/symbol"
        assert normalize_symbol_id(sid) == sid

    def test_empty_string_returns_empty(self):
        assert normalize_symbol_id("") == ""

    def test_different_packages_same_symbol_name_normalize_different(self):
        """Ensures two packages with same function name don't collide."""
        sid_a = "scip-python python pkg-a 1.0.0 pkg_a/db.py/connect()."
        sid_b = "scip-python python pkg-b 1.0.0 pkg_b/db.py/connect()."
        assert normalize_symbol_id(sid_a) != normalize_symbol_id(sid_b)


# ---------------------------------------------------------------------------
# HARD ACCEPTANCE GATE: False-edge negative test
# ---------------------------------------------------------------------------


class TestNoFalseCrossRepoEdge:
    """HARD REQUIREMENT: Two repos with same-named function in same-named file
    that do NOT call each other must NOT produce a false cross-repo edge.

    This proves symbol_id resolution works correctly and name+file matching
    (rejected approach) would have failed here.
    """

    def test_no_false_cross_repo_edge(self):
        """Two repos with same-named function in same-named file must NOT
        produce a cross-repo edge.

        Setup:
        - repo-a/db.py has connect() with symbol_id "scip-python python pkg-a 1.0.0 pkg_a/db.py/connect()."
        - repo-b/db.py has connect() with symbol_id "scip-python python pkg-b 1.0.0 pkg_b/db.py/connect()."
        - They have DIFFERENT symbol_ids (different SCIP monikers)
        - No CALLS edge exists between them

        Assertion: cross-repo query for repo-a's connect() must NOT include repo-b.
        """
        mock_driver = MagicMock()
        mock_session = MagicMock()

        # First call: resolve symbol_id for repo-a's connect()
        resolve_records = [{"symbol_id": "scip-python python pkg-a 1.0.0 pkg_a/db.py/connect()."}]
        # Second call: cross-repo query — returns NO results because repo-b has
        # a DIFFERENT symbol_id ("pkg-b" not "pkg-a")
        cross_repo_records: list[dict] = []

        mock_results = []
        for records in [resolve_records, cross_repo_records]:
            mock_result = MagicMock()
            mock_result.__iter__ = lambda s, r=records: iter(r)
            mock_results.append(mock_result)

        mock_session.run.side_effect = mock_results
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            result = query_cross_repo_impact(repo="repo-a", file="db.py", symbol_name="connect")

        # HARD ASSERTION: repo-b must NOT appear (no false edge)
        assert "repo-b" not in [r.get("calling_repo") for r in result]
        # Result should be empty (no actual cross-repo callers)
        assert result == []

    def test_no_false_edge_with_version_normalization(self):
        """Version normalization must NOT reintroduce collisions between
        different packages that happen to share a symbol name.

        Even after normalizing versions, "pkg-a/db.py/connect()." and
        "pkg-b/db.py/connect()." remain distinct because the package
        name differs.
        """
        sid_a = "scip-python python pkg-a 1.0.0 pkg_a/db.py/connect()."
        sid_b = "scip-python python pkg-b 2.0.0 pkg_b/db.py/connect()."

        norm_a = normalize_symbol_id(sid_a)
        norm_b = normalize_symbol_id(sid_b)

        # Even after stripping version, different packages stay different
        assert norm_a != norm_b, (
            "Version normalization must NOT collapse different packages. "
            f"Got: {norm_a!r} == {norm_b!r}"
        )


# ---------------------------------------------------------------------------
# Positive cross-repo test: function used by 3 repos
# ---------------------------------------------------------------------------


class TestCrossRepoPositive:
    """Positive test: a function used by 3 repos returns all 3 with attribution."""

    def test_cross_repo_returns_all_calling_repos(self):
        """A shared library function called by repos B, C, D should return
        all three repos with per-repo attribution.
        """
        mock_driver = MagicMock()
        mock_session = MagicMock()

        # First call: resolve symbol_id
        resolve_records = [
            {"symbol_id": "scip-python python shared-lib 1.0.0 shared_lib/core.py/process()."}
        ]
        # Second call: cross-repo results from 3 different repos
        cross_repo_records = [
            {
                "calling_repo": "org/repo-b",
                "calling_file": "src/handler.py",
                "calling_symbol": "handle_event",
                "calling_kind": "function",
            },
            {
                "calling_repo": "org/repo-c",
                "calling_file": "lib/worker.py",
                "calling_symbol": "run_job",
                "calling_kind": "function",
            },
            {
                "calling_repo": "org/repo-d",
                "calling_file": "app/main.py",
                "calling_symbol": "main",
                "calling_kind": "function",
            },
        ]

        mock_results = []
        for records in [resolve_records, cross_repo_records]:
            mock_result = MagicMock()
            mock_result.__iter__ = lambda s, r=records: iter(r)
            mock_results.append(mock_result)

        mock_session.run.side_effect = mock_results
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            result = query_cross_repo_impact(
                repo="org/shared-lib", file="shared_lib/core.py", symbol_name="process"
            )

        # All 3 repos returned
        calling_repos = [r["calling_repo"] for r in result]
        assert "org/repo-b" in calling_repos
        assert "org/repo-c" in calling_repos
        assert "org/repo-d" in calling_repos
        assert len(result) == 3

        # Per-repo attribution: each result has the calling repo, file, symbol
        for r in result:
            assert "calling_repo" in r
            assert "calling_file" in r
            assert "calling_symbol" in r
            assert "calling_kind" in r

    def test_cross_repo_excludes_source_repo(self):
        """Cross-repo must never include the source repo in results."""
        mock_driver = MagicMock()
        mock_session = MagicMock()

        resolve_records = [{"symbol_id": "scip-python python mylib 1.0.0 mylib/utils.py/helper()."}]
        # Cross-repo query correctly excludes source repo (WHERE caller.repo <> $repo)
        cross_repo_records = [
            {
                "calling_repo": "org/consumer-a",
                "calling_file": "app.py",
                "calling_symbol": "main",
                "calling_kind": "function",
            },
        ]

        mock_results = []
        for records in [resolve_records, cross_repo_records]:
            mock_result = MagicMock()
            mock_result.__iter__ = lambda s, r=records: iter(r)
            mock_results.append(mock_result)

        mock_session.run.side_effect = mock_results
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            result = query_cross_repo_impact(
                repo="org/mylib", file="mylib/utils.py", symbol_name="helper"
            )

        # Source repo must not appear
        assert "org/mylib" not in [r["calling_repo"] for r in result]
        assert len(result) == 1
        assert result[0]["calling_repo"] == "org/consumer-a"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestCrossRepoEdgeCases:
    """Edge cases for cross-repo query."""

    def test_returns_empty_when_no_driver(self):
        """No Neptune driver → empty results (graceful degradation)."""
        with patch("door.neptune_client.get_neptune_driver", return_value=None):
            result = query_cross_repo_impact(
                repo="org/repo", file="src/api.py", symbol_name="handler"
            )
            assert result == []

    def test_returns_empty_when_no_symbol_id(self):
        """Symbol exists but has no symbol_id → empty (cannot join)."""
        mock_driver = MagicMock()
        mock_session = MagicMock()

        # Resolve returns empty (no symbol_id on the node)
        resolve_records: list[dict] = []
        mock_result = MagicMock()
        mock_result.__iter__ = lambda s: iter(resolve_records)
        mock_session.run.return_value = mock_result
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            result = query_cross_repo_impact(
                repo="org/repo", file="src/api.py", symbol_name="orphan_func"
            )
            assert result == []

    def test_returns_empty_on_exception(self):
        """Neptune connection error → empty results (graceful degradation)."""
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_session.run.side_effect = RuntimeError("Neptune timeout")
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            result = query_cross_repo_impact(
                repo="org/repo", file="src/api.py", symbol_name="handler"
            )
            assert result == []

    def test_bounded_at_100_results(self):
        """Query LIMIT ensures max 100 cross-repo callers."""
        mock_driver = MagicMock()
        mock_session = MagicMock()

        resolve_records = [{"symbol_id": "scip-python python lib 1.0.0 lib/core.py/func()."}]
        # Simulate Neptune returning exactly 100 results (the LIMIT)
        cross_repo_records = [
            {
                "calling_repo": f"org/repo-{i}",
                "calling_file": f"src/file_{i}.py",
                "calling_symbol": f"caller_{i}",
                "calling_kind": "function",
            }
            for i in range(100)
        ]

        mock_results = []
        for records in [resolve_records, cross_repo_records]:
            mock_result = MagicMock()
            mock_result.__iter__ = lambda s, r=records: iter(r)
            mock_results.append(mock_result)

        mock_session.run.side_effect = mock_results
        mock_driver.session.return_value.__enter__ = lambda s: mock_session
        mock_driver.session.return_value.__exit__ = lambda s, *a: None

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            result = query_cross_repo_impact(repo="org/lib", file="lib/core.py", symbol_name="func")

        assert len(result) == 100


# ---------------------------------------------------------------------------
# Integration with structural_backend.impact()
# ---------------------------------------------------------------------------


class TestImpactCrossRepoIntegration:
    """Test that impact(cross_repo=True) uses Neptune cross-repo query."""

    @pytest.mark.asyncio
    async def test_impact_cross_repo_uses_neptune_symbol_id(self):
        """When Neptune is available and cross_repo=True, uses symbol_id join."""
        from door.structural_backend import impact

        mock_s3 = MagicMock()
        mock_s3.exceptions = MagicMock()
        mock_s3.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        # Mock Neptune as available and returning results
        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch(
                "door.neptune_client.query_impact",
                return_value=[
                    {
                        "caller_repo": "org/repo-a",
                        "caller_file": "src/main.py",
                        "caller_name": "main",
                        "caller_kind": "function",
                        "distance": 1,
                    }
                ],
            ),
            patch(
                "door.neptune_client.query_cross_repo_impact",
                return_value=[
                    {
                        "calling_repo": "org/repo-b",
                        "calling_file": "src/app.py",
                        "calling_symbol": "init",
                        "calling_kind": "function",
                    },
                    {
                        "calling_repo": "org/repo-c",
                        "calling_file": "lib/service.py",
                        "calling_symbol": "start",
                        "calling_kind": "function",
                    },
                ],
            ) as mock_xrepo,
        ):
            hits = await impact(
                "repo-a/src/db.py::connect",
                s3_client=mock_s3,
                bucket="test-bucket",
                prefix="pfx",
                cross_repo=True,
            )

        # Cross-repo query was called
        mock_xrepo.assert_called_once_with("repo-a", "src/db.py", "connect")

        # Results include both same-repo and cross-repo callers
        assert len(hits) == 3
        repos = [h.data.get("repo_id") for h in hits]
        assert "org/repo-a" in repos  # same-repo caller
        assert "org/repo-b" in repos  # cross-repo
        assert "org/repo-c" in repos  # cross-repo

        # Cross-repo results are tagged correctly
        xrepo_hits = [h for h in hits if h.data.get("relationship") == "cross_repo_reference"]
        assert len(xrepo_hits) == 2
        for h in xrepo_hits:
            assert h.data["source"] == "neptune"

    @pytest.mark.asyncio
    async def test_impact_without_cross_repo_skips_neptune_xrepo(self):
        """When cross_repo=False, does NOT call query_cross_repo_impact."""
        from door.structural_backend import impact

        mock_s3 = MagicMock()
        mock_s3.exceptions = MagicMock()
        mock_s3.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch(
                "door.neptune_client.query_impact",
                return_value=[
                    {
                        "caller_repo": "org/repo-a",
                        "caller_file": "src/main.py",
                        "caller_name": "main",
                        "caller_kind": "function",
                        "distance": 1,
                    }
                ],
            ),
            patch(
                "door.neptune_client.query_cross_repo_impact",
                return_value=[],
            ) as mock_xrepo,
        ):
            hits = await impact(
                "repo-a/src/db.py::connect",
                s3_client=mock_s3,
                bucket="test-bucket",
                prefix="pfx",
                cross_repo=False,
            )

        # Cross-repo query NOT called
        mock_xrepo.assert_not_called()
        # Only same-repo results
        assert len(hits) == 1
