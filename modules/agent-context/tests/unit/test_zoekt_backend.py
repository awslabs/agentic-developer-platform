"""
Unit tests for the Zoekt search backend (door/search_backend.py).

Tests:
- Z1: Zoekt response parsing (LineMatches and ChunkMatches formats)
- Z2: MCP contract compliance (result shape matches SearchHit + data fields)
- Z3: Repo-scoping filter construction
- Z4: Error handling (timeout, 5xx, malformed response)
- Z5: Integration with Door ACL filter
- Z6: Base64-encoded line decoding
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from door.acl import CallerPrincipal, SearchHit, filter_results
from door.search_backend import (
    ZoektSearchBackend,
    _build_repo_filter,
    _decode_line,
    _parse_zoekt_response,
)


# ===========================================================================
# Z1: Response parsing
# ===========================================================================


class TestParseZoektResponse:
    """Z1: Correctly parse Zoekt /api/search JSON responses."""

    def test_line_matches_format(self):
        """Z1a: Parse legacy LineMatches format."""
        response = {
            "Result": {
                "FileMatches": [
                    {
                        "Repository": "org/my-repo",
                        "FileName": "src/handler.py",
                        "LineMatches": [
                            {"LineNumber": 42, "Line": "def process_request(data):"},
                            {"LineNumber": 55, "Line": "    return process_request(x)"},
                        ],
                    }
                ]
            }
        }

        results = _parse_zoekt_response(response)

        assert len(results) == 2
        assert results[0].repo_name == "org/my-repo"
        assert results[0].data["file"] == "src/handler.py"
        assert results[0].data["line"] == 42
        assert results[0].data["content"] == "def process_request(data):"
        assert results[1].data["line"] == 55

    def test_chunk_matches_format(self):
        """Z1b: Parse newer ChunkMatches format."""
        response = {
            "Result": {
                "FileMatches": [
                    {
                        "Repository": "org/repo-b",
                        "FileName": "lib/utils.go",
                        "ChunkMatches": [
                            {
                                "Content": "func HandleError(err error) {",
                                "ContentStart": {"LineNumber": 10},
                            }
                        ],
                    }
                ]
            }
        }

        results = _parse_zoekt_response(response)

        assert len(results) == 1
        assert results[0].repo_name == "org/repo-b"
        assert results[0].data["file"] == "lib/utils.go"
        assert results[0].data["line"] == 10
        assert "HandleError" in results[0].data["content"]

    def test_multiple_file_matches(self):
        """Z1c: Multiple FileMatches produce multiple results."""
        response = {
            "Result": {
                "FileMatches": [
                    {
                        "Repository": "org/repo-a",
                        "FileName": "a.py",
                        "LineMatches": [{"LineNumber": 1, "Line": "import os"}],
                    },
                    {
                        "Repository": "org/repo-b",
                        "FileName": "b.py",
                        "LineMatches": [{"LineNumber": 5, "Line": "import os"}],
                    },
                ]
            }
        }

        results = _parse_zoekt_response(response)
        assert len(results) == 2
        assert results[0].data["repo_id"] == "org/repo-a"
        assert results[1].data["repo_id"] == "org/repo-b"

    def test_empty_file_matches(self):
        """Z1d: Empty FileMatches returns empty list."""
        response = {"Result": {"FileMatches": []}}
        results = _parse_zoekt_response(response)
        assert results == []

    def test_null_file_matches(self):
        """Z1e: Missing FileMatches key returns empty list."""
        response = {"Result": {}}
        results = _parse_zoekt_response(response)
        assert results == []

    def test_file_match_without_line_or_chunk_matches(self):
        """Z1f: FileMatch with no line/chunk info emits file-level hit."""
        response = {
            "Result": {
                "FileMatches": [
                    {
                        "Repository": "org/repo",
                        "FileName": "README.md",
                    }
                ]
            }
        }

        results = _parse_zoekt_response(response)
        assert len(results) == 1
        assert results[0].data["file"] == "README.md"
        assert results[0].data["line"] == 0


# ===========================================================================
# Z2: MCP contract compliance
# ===========================================================================


class TestMCPContractCompliance:
    """Z2: Results match the MCP search contract shape."""

    def test_result_has_required_fields(self):
        """Z2a: Every result has repo_id, file, line, content fields."""
        response = {
            "Result": {
                "FileMatches": [
                    {
                        "Repository": "org/service",
                        "FileName": "main.py",
                        "LineMatches": [{"LineNumber": 1, "Line": "print('hello')"}],
                    }
                ]
            }
        }

        results = _parse_zoekt_response(response)
        result = results[0]

        # SearchHit.repo_name
        assert isinstance(result.repo_name, str)
        assert result.repo_name == "org/service"

        # data dict fields (MCP contract)
        assert "repo_id" in result.data
        assert "file" in result.data
        assert "line" in result.data
        assert "content" in result.data

        # Types match contract
        assert isinstance(result.data["repo_id"], str)
        assert isinstance(result.data["file"], str)
        assert isinstance(result.data["line"], int)
        assert isinstance(result.data["content"], str)

    def test_match_type_is_exact(self):
        """Z2b: All results have match_type='exact' (distinguishes from semantic)."""
        response = {
            "Result": {
                "FileMatches": [
                    {
                        "Repository": "org/repo",
                        "FileName": "x.py",
                        "LineMatches": [{"LineNumber": 1, "Line": "x = 1"}],
                    }
                ]
            }
        }

        results = _parse_zoekt_response(response)
        assert results[0].data["match_type"] == "exact"


# ===========================================================================
# Z3: Repo-scoping filter construction
# ===========================================================================


class TestBuildRepoFilter:
    """Z3: Correctly build Zoekt repo filter regex from repo_ids."""

    def test_single_repo(self):
        """Z3a: Single repo produces anchored regex."""
        result = _build_repo_filter(["org/my-repo"])
        assert result == r"^(org/my\-repo)$"

    def test_multiple_repos(self):
        """Z3b: Multiple repos joined with pipe."""
        result = _build_repo_filter(["org/repo-a", "org/repo-b"])
        assert result == r"^(org/repo\-a|org/repo\-b)$"

    def test_special_chars_escaped(self):
        """Z3c: Regex special characters in repo names are escaped."""
        result = _build_repo_filter(["org/repo.name", "org/repo+plus"])
        assert r"repo\.name" in result
        assert r"repo\+plus" in result

    def test_empty_list(self):
        """Z3d: Empty list produces regex matching nothing."""
        result = _build_repo_filter([])
        # ^()$ matches empty string only — effectively matches nothing in practice
        assert result == "^()$"


# ===========================================================================
# Z4: Error handling
# ===========================================================================


class TestErrorHandling:
    """Z4: Backend returns empty list on errors (fail-safe)."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_returns_empty(self):
        """Z4a: Timeout returns empty list, not exception."""
        respx.get("http://zoekt:6070/api/search").mock(side_effect=httpx.ReadTimeout("timed out"))

        backend = ZoektSearchBackend("http://zoekt:6070", timeout=0.1)
        results = await backend.search("query")
        assert results == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_http_500_returns_empty(self):
        """Z4b: HTTP 500 returns empty list."""
        respx.get("http://zoekt:6070/api/search").mock(return_value=httpx.Response(500))

        backend = ZoektSearchBackend("http://zoekt:6070")
        results = await backend.search("query")
        assert results == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_http_503_returns_empty(self):
        """Z4c: HTTP 503 (service unavailable) returns empty list."""
        respx.get("http://zoekt:6070/api/search").mock(return_value=httpx.Response(503))

        backend = ZoektSearchBackend("http://zoekt:6070")
        results = await backend.search("query")
        assert results == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_malformed_json_returns_empty(self):
        """Z4d: Malformed JSON response returns empty list."""
        respx.get("http://zoekt:6070/api/search").mock(
            return_value=httpx.Response(200, text="not json")
        )

        backend = ZoektSearchBackend("http://zoekt:6070")
        results = await backend.search("query")
        assert results == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_successful_search(self):
        """Z4e: Successful response is parsed correctly."""
        response_body = json.dumps(
            {
                "Result": {
                    "FileMatches": [
                        {
                            "Repository": "org/repo",
                            "FileName": "main.py",
                            "LineMatches": [{"LineNumber": 7, "Line": "target_symbol = True"}],
                        }
                    ]
                }
            }
        )
        respx.get("http://zoekt:6070/api/search").mock(
            return_value=httpx.Response(200, text=response_body)
        )

        backend = ZoektSearchBackend("http://zoekt:6070")
        results = await backend.search("target_symbol")

        assert len(results) == 1
        assert results[0].repo_name == "org/repo"
        assert results[0].data["line"] == 7

    @pytest.mark.asyncio
    @respx.mock
    async def test_search_with_repo_scope(self):
        """Z4f: Repo-scoped search passes repos parameter."""
        response_body = json.dumps({"Result": {"FileMatches": []}})
        route = respx.get("http://zoekt:6070/api/search").mock(
            return_value=httpx.Response(200, text=response_body)
        )

        backend = ZoektSearchBackend("http://zoekt:6070")
        await backend.search("query", repo_ids=["org/repo-a"])

        assert route.called
        request = route.calls[0].request
        assert "repos" in str(request.url)


# ===========================================================================
# Z5: Integration with Door ACL filter
# ===========================================================================


class TestACLIntegration:
    """Z5: Zoekt results integrate correctly with Door's filter_results."""

    def test_acl_filters_unauthorized_repos(self):
        """Z5a: Results from unauthorized repos are dropped by ACL filter."""
        from .conftest import FakeACLStore

        # Set up ACL: user can see org/allowed but not org/private
        acl_store = FakeACLStore()
        acl_store.grant("org/allowed", "alice")

        # Simulate Zoekt returning results from both repos
        zoekt_results = [
            SearchHit(repo_name="org/allowed", data={"repo_id": "org/allowed", "file": "a.py"}),
            SearchHit(repo_name="org/private", data={"repo_id": "org/private", "file": "b.py"}),
        ]

        # The Door ACL filter needs an ACLStore with get_allowed_repos
        # We test the integration pattern: parse -> filter
        caller = CallerPrincipal(github_login="alice")

        class _ACLAdapter:
            """Adapt FakeACLStore to the ACLStore protocol."""

            def get_allowed_repos(self, principal: CallerPrincipal) -> set[str]:
                return acl_store.get_accessible_repos(principal.github_login)

        filtered = filter_results(zoekt_results, caller, _ACLAdapter())
        assert len(filtered) == 1
        assert filtered[0].repo_name == "org/allowed"

    def test_acl_empty_on_unknown_caller(self):
        """Z5b: Unknown caller gets empty results (fail-closed)."""
        zoekt_results = [
            SearchHit(repo_name="org/repo", data={"repo_id": "org/repo", "file": "x.py"}),
        ]

        class _EmptyACL:
            def get_allowed_repos(self, principal: CallerPrincipal) -> set[str]:
                return set()

        filtered = filter_results(zoekt_results, None, _EmptyACL())
        assert filtered == []


# ===========================================================================
# Z6: Line decoding
# ===========================================================================


class TestDecodeLine:
    """Z6: Handle various line encodings from Zoekt."""

    def test_plain_string(self):
        """Z6a: Plain string passed through."""
        assert _decode_line("def hello():") == "def hello():"

    def test_bytes(self):
        """Z6b: Bytes decoded as UTF-8."""
        assert _decode_line(b"import sys") == "import sys"

    def test_base64_encoded(self):
        """Z6c: Base64-encoded content is decoded."""
        import base64

        original = "def process():"
        encoded = base64.b64encode(original.encode()).decode()
        assert _decode_line(encoded) == original

    def test_non_base64_string_preserved(self):
        """Z6d: Strings that aren't valid base64 are preserved as-is."""
        text = "this is not base64!"
        assert _decode_line(text) == text
