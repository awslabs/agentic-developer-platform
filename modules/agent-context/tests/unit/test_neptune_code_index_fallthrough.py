"""Unit tests for Neptune-to-code-index fallthrough logic (#3450).

Validates that when Neptune's resolve_symbol returns only loose/substring
matches (no exact-case name match), the understand() function also consults
the code index. If the code index has an exact-case match, it wins.

Core scenario: Neptune has no Go symbols for microservices-demo (SCIP covers
py/ts only). For `microservices-demo::Quote`, resolve_symbol returns only
`GetQuote` from Python stubs (substring match). The code index has the correct
entry: `{"symbol": "Quote", "file": "src/shippingservice/quote.go", "line": 23}`.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from door.structural_backend import (
    _check_code_index_for_exact_match,
    _extract_symbol_name,
    understand,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# _extract_symbol_name tests
# ---------------------------------------------------------------------------


class TestExtractSymbolName:
    """Tests for the symbol name extraction helper."""

    def test_double_colon_separator(self):
        assert _extract_symbol_name("src/file.go::Quote") == "Quote"

    def test_bare_symbol(self):
        assert _extract_symbol_name("checkoutService") == "checkoutService"

    def test_bare_symbol_with_colon(self):
        assert _extract_symbol_name("::Quote") == "Quote"

    def test_file_path_returns_empty(self):
        assert _extract_symbol_name("src/shippingservice/quote.go") == ""

    def test_directory_path_returns_empty(self):
        assert _extract_symbol_name("src/shippingservice") == ""


# ---------------------------------------------------------------------------
# Core fallthrough tests: Neptune loose match → code-index exact match wins
# ---------------------------------------------------------------------------


class TestNeptuneCodeIndexFallthrough:
    """Test that understand() falls through to code index when Neptune has no exact match."""

    @pytest.fixture
    def go_python_fixture(self) -> dict:
        """Load the Go/Python code-index fixture."""
        fixture_path = FIXTURES_DIR / "code-index-go-python-fixture.json"
        return json.loads(fixture_path.read_text())

    @pytest.fixture
    def mock_s3_client(self, go_python_fixture):
        """Mock S3 client that returns the Go/Python fixture data."""
        client = MagicMock()
        body_mock = MagicMock()
        body_mock.read.return_value = json.dumps(go_python_fixture).encode()
        client.get_object.return_value = {"Body": body_mock}
        client.exceptions = MagicMock()
        client.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
        return client

    @pytest.mark.asyncio
    async def test_neptune_substring_only_falls_through_to_code_index(self, mock_s3_client):
        """When Neptune returns only substring match (GetQuote), code-index exact (Quote) wins.

        This is the core bug scenario: Neptune's graph has no Go symbols,
        so resolve_symbol returns GetQuote (substring of "Quote") from Python stubs.
        The code index has the correct Quote struct in quote.go.
        """
        # Mock Neptune: enabled + available, resolve_symbol returns only GetQuote
        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch("door.neptune_client.resolve_repo_name", return_value="microservices-demo"),
            patch("door.neptune_client.query_understand", return_value=[]),
            patch(
                "door.neptune_client.resolve_symbol",
                return_value=[
                    {
                        "name": "GetQuote",
                        "file": "src/frontend/genproto/demo_pb2_grpc.py",
                        "kind": "function",
                        "symbol_id": "s1",
                    }
                ],
            ),
        ):
            # resolve_symbol returned GetQuote, so query_understand is called again
            # with that file/name. Mock it to return a record for GetQuote:
            with patch(
                "door.neptune_client.query_understand",
                side_effect=lambda repo, file, name: (
                    [
                        {
                            "symbol_name": "GetQuote",
                            "symbol_file": "src/frontend/genproto/demo_pb2_grpc.py",
                            "symbol_kind": "function",
                            "signature": "def GetQuote(self, request, context)",
                            "callees": [],
                            "callers": [],
                            "parents": [],
                            "owners": [],
                        }
                    ]
                    if name == "GetQuote"
                    else []
                ),
            ):
                results = await understand(
                    "microservices-demo::Quote",
                    s3_client=mock_s3_client,
                    bucket="test-bucket",
                    prefix="code-indexes",
                )

        # The code-index exact match should win
        assert len(results) >= 1
        top = results[0]
        assert top.data["symbol"] == "Quote"
        assert top.data["file"] == "src/shippingservice/quote.go"
        assert top.data["source"] == "code-index-fallback"

    @pytest.mark.asyncio
    async def test_neptune_exact_match_does_not_fall_through(self, mock_s3_client):
        """When Neptune has an exact-case match, code index is NOT consulted.

        This ensures we don't regress: if Neptune correctly resolves the symbol
        with an exact match, we keep that result.
        """
        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch("door.neptune_client.resolve_repo_name", return_value="microservices-demo"),
            patch(
                "door.neptune_client.query_understand",
                return_value=[
                    {
                        "symbol_name": "frontendServer",
                        "symbol_file": "src/frontend/main.go",
                        "symbol_kind": "struct",
                        "signature": "type frontendServer struct",
                        "callees": [],
                        "callers": [],
                        "parents": [],
                        "owners": [],
                    }
                ],
            ),
        ):
            results = await understand(
                "microservices-demo::frontendServer",
                s3_client=mock_s3_client,
                bucket="test-bucket",
                prefix="code-indexes",
            )

        # Neptune's exact match should be used (source=neptune)
        assert len(results) >= 1
        top = results[0]
        assert top.data["symbol"] == "frontendServer"
        assert top.data["source"] == "neptune"

    @pytest.mark.asyncio
    async def test_neptune_no_results_falls_through_normally(self, mock_s3_client):
        """When Neptune returns nothing at all, the existing full fallback path is used."""
        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch("door.neptune_client.resolve_repo_name", return_value="microservices-demo"),
            patch("door.neptune_client.query_understand", return_value=[]),
            patch("door.neptune_client.resolve_symbol", return_value=[]),
        ):
            results = await understand(
                "microservices-demo::Quote",
                s3_client=mock_s3_client,
                bucket="test-bucket",
                prefix="code-indexes",
            )

        # Falls through to full code-index path
        assert len(results) >= 1
        top = results[0]
        assert top.data["symbol"] == "Quote"
        assert top.data["file"] == "src/shippingservice/quote.go"
        assert top.data["source"] == "code-index-fallback"

    @pytest.mark.asyncio
    async def test_code_index_has_no_exact_keeps_neptune(self, mock_s3_client):
        """When code index also has no exact match, keep Neptune's ranked best."""
        # Mock S3 to return an index WITHOUT an exact "Foo" symbol
        sparse_index = {
            "repo_id": "microservices-demo",
            "definitions": [
                {
                    "symbol": "FooBar",
                    "file": "src/service/foobar.go",
                    "line": 10,
                    "kind": "function",
                    "signature": "func FooBar()",
                }
            ],
            "call_graph": {},
        }
        s3_client = MagicMock()
        body_mock = MagicMock()
        body_mock.read.return_value = json.dumps(sparse_index).encode()
        s3_client.get_object.return_value = {"Body": body_mock}
        s3_client.exceptions = MagicMock()
        s3_client.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        with (
            patch("door.neptune_client.neptune_enabled", return_value=True),
            patch("door.neptune_client.neptune_available", return_value=True),
            patch("door.neptune_client.resolve_repo_name", return_value="microservices-demo"),
            patch("door.neptune_client.query_understand", return_value=[]),
            patch(
                "door.neptune_client.resolve_symbol",
                return_value=[
                    {
                        "name": "FooBar",
                        "file": "src/service/foobar.go",
                        "kind": "function",
                        "symbol_id": "s1",
                    }
                ],
            ),
        ):
            # query_understand re-called with resolved file/name
            with patch(
                "door.neptune_client.query_understand",
                side_effect=lambda repo, file, name: (
                    [
                        {
                            "symbol_name": "FooBar",
                            "symbol_file": "src/service/foobar.go",
                            "symbol_kind": "function",
                            "signature": "func FooBar()",
                            "callees": [],
                            "callers": [],
                            "parents": [],
                            "owners": [],
                        }
                    ]
                    if name == "FooBar"
                    else []
                ),
            ):
                results = await understand(
                    "microservices-demo::Foo",
                    s3_client=s3_client,
                    bucket="test-bucket",
                    prefix="code-indexes",
                )

        # Neptune's result should be kept since code index has no exact "Foo" either
        assert len(results) >= 1
        top = results[0]
        assert top.data["symbol"] == "FooBar"
        assert top.data["source"] == "neptune"


# ---------------------------------------------------------------------------
# _check_code_index_for_exact_match unit tests
# ---------------------------------------------------------------------------


class TestCheckCodeIndexForExactMatch:
    """Direct tests for the code-index exact-match checker."""

    @pytest.fixture
    def go_python_fixture(self) -> dict:
        fixture_path = FIXTURES_DIR / "code-index-go-python-fixture.json"
        return json.loads(fixture_path.read_text())

    @pytest.fixture
    def mock_s3_client(self, go_python_fixture):
        client = MagicMock()
        body_mock = MagicMock()
        body_mock.read.return_value = json.dumps(go_python_fixture).encode()
        client.get_object.return_value = {"Body": body_mock}
        client.exceptions = MagicMock()
        client.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
        return client

    @pytest.mark.asyncio
    async def test_finds_exact_match(self, mock_s3_client):
        """Returns code-index hits when exact symbol name found."""
        results = await _check_code_index_for_exact_match(
            "microservices-demo",
            "::Quote",
            "Quote",
            "overview",
            s3_client=mock_s3_client,
            bucket="test-bucket",
            prefix="code-indexes",
        )

        assert results is not None
        assert len(results) == 1
        assert results[0].data["symbol"] == "Quote"
        assert results[0].data["file"] == "src/shippingservice/quote.go"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_exact(self, mock_s3_client):
        """Returns None when code index has no exact match for the symbol."""
        results = await _check_code_index_for_exact_match(
            "microservices-demo",
            "::NonExistent",
            "NonExistent",
            "overview",
            s3_client=mock_s3_client,
            bucket="test-bucket",
            prefix="code-indexes",
        )

        assert results is None

    @pytest.mark.asyncio
    async def test_returns_none_when_index_empty(self):
        """Returns None when code index cannot be loaded."""
        s3_client = MagicMock()
        s3_client.exceptions = MagicMock()
        s3_client.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
        s3_client.get_object.side_effect = s3_client.exceptions.NoSuchKey("not found")

        results = await _check_code_index_for_exact_match(
            "microservices-demo",
            "::Quote",
            "Quote",
            "overview",
            s3_client=s3_client,
            bucket="test-bucket",
            prefix="code-indexes",
        )

        assert results is None

    @pytest.mark.asyncio
    async def test_prefers_non_generated_among_exact_matches(self, mock_s3_client):
        """When multiple exact matches exist, non-generated file ranks first."""
        results = await _check_code_index_for_exact_match(
            "microservices-demo",
            "::PlaceOrder",
            "PlaceOrder",
            "overview",
            s3_client=mock_s3_client,
            bucket="test-bucket",
            prefix="code-indexes",
        )

        assert results is not None
        assert len(results) == 2
        # Hand-written Go file ranks above generated pb2_grpc
        assert results[0].data["file"] == "src/checkoutservice/main.go"
        assert results[1].data["file"] == "src/frontend/genproto/demo_pb2_grpc.py"
