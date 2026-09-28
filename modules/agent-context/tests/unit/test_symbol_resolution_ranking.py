"""Unit tests for symbol resolution ranking (#3374).

Validates that:
- Exact-case + exact-name matches outrank case-insensitive and substring matches
- Generated-file definitions (pb2, pb2_grpc, pb.go) are demoted below hand-written
  sources as a tiebreaker, but NOT filtered out entirely
- The ranking is applied in both the Neptune resolve_symbol path and the
  code-index fallback path
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from door.structural_backend import (
    _is_generated_file,
    _rank_understand_results,
    _symbol_match_score,
    understand,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# _is_generated_file tests
# ---------------------------------------------------------------------------


class TestIsGeneratedFile:
    """Tests for generated-file detection."""

    def test_pb2_grpc_python(self):
        assert _is_generated_file("src/emailservice/demo_pb2_grpc.py") is True

    def test_pb2_python(self):
        assert _is_generated_file("src/emailservice/demo_pb2.py") is True

    def test_pb_go(self):
        assert _is_generated_file("src/proto/demo.pb.go") is True

    def test_grpc_pb_go(self):
        assert _is_generated_file("src/proto/demo_grpc.pb.go") is True

    def test_generated_dir(self):
        assert _is_generated_file("src/generated/models.py") is True

    def test_gen_dir(self):
        assert _is_generated_file("src/gen/types.go") is True

    def test_normal_go_file(self):
        assert _is_generated_file("src/shippingservice/quote.go") is False

    def test_normal_python_file(self):
        assert _is_generated_file("src/emailservice/main.py") is False

    def test_normal_main_go(self):
        assert _is_generated_file("src/checkoutservice/main.go") is False


# ---------------------------------------------------------------------------
# _symbol_match_score tests
# ---------------------------------------------------------------------------


class TestSymbolMatchScore:
    """Tests for the scoring function."""

    def test_exact_case_exact_name(self):
        score = _symbol_match_score("checkoutService", "checkoutService", "main.go")
        assert score == 200

    def test_case_insensitive_exact_name(self):
        score = _symbol_match_score("checkoutService", "CheckoutService", "main.py")
        assert score == 150

    def test_exact_case_substring(self):
        # "Quote" is a substring of "GetQuote" with matching case
        score = _symbol_match_score("Quote", "GetQuote", "main.go")
        assert score == 100

    def test_case_insensitive_substring(self):
        # "quote" (lowercase) contained in "GetQuote" case-insensitively
        score = _symbol_match_score("quote", "GetQuote", "main.go")
        assert score == 50

    def test_no_match(self):
        score = _symbol_match_score("Unrelated", "checkoutService", "main.go")
        assert score == 0

    def test_generated_file_penalty(self):
        # Same exact match but in a generated file gets -10
        score_normal = _symbol_match_score("CheckoutService", "CheckoutService", "src/main.py")
        score_generated = _symbol_match_score(
            "CheckoutService", "CheckoutService", "src/demo_pb2_grpc.py"
        )
        assert score_normal == 200
        assert score_generated == 190
        assert score_normal > score_generated

    def test_exact_case_beats_case_insensitive_generated(self):
        """Exact-case match in generated file still beats case-insensitive in normal."""
        # This tests that the penalty is a tiebreaker, not a filter:
        # exact-case in generated (200-10=190) > case-insensitive in normal (150)
        score_exact_gen = _symbol_match_score(
            "CheckoutService", "CheckoutService", "src/demo_pb2_grpc.py"
        )
        score_ci_normal = _symbol_match_score("checkoutservice", "CheckoutService", "src/main.py")
        assert score_exact_gen > score_ci_normal

    def test_checkoutService_go_vs_python_pb2(self):
        """Core bug case: Go struct exact-case should beat Python stub case-insensitive."""
        # Go struct: exact-case match
        score_go = _symbol_match_score(
            "checkoutService", "checkoutService", "src/checkoutservice/main.go"
        )
        # Python stub: case-insensitive match in generated file
        score_py = _symbol_match_score(
            "checkoutService", "CheckoutService", "src/emailservice/demo_pb2_grpc.py"
        )
        assert score_go > score_py
        assert score_go == 200
        assert score_py == 140  # 150 (case-insensitive exact) - 10 (generated)

    def test_quote_exact_vs_getquote_substring(self):
        """Core bug case: Quote exact match should beat GetQuote substring."""
        score_exact = _symbol_match_score("Quote", "Quote", "src/shippingservice/quote.go")
        score_substring = _symbol_match_score("Quote", "GetQuote", "src/shippingservice/main.go")
        assert score_exact > score_substring
        assert score_exact == 200
        assert score_substring == 100


# ---------------------------------------------------------------------------
# _rank_understand_results tests (with SearchHit)
# ---------------------------------------------------------------------------


class TestRankUnderstandResults:
    """Tests for ranking understand verb results."""

    def test_ranks_exact_case_first(self):
        """checkoutService (Go, exact case) ranks above CheckoutService (Python, generated)."""
        from door.acl import SearchHit

        results = [
            SearchHit(
                repo_name="microservices-demo",
                data={
                    "symbol": "CheckoutService",
                    "file": "src/emailservice/demo_pb2_grpc.py",
                    "kind": "class",
                    "source": "neptune",
                },
            ),
            SearchHit(
                repo_name="microservices-demo",
                data={
                    "symbol": "checkoutService",
                    "file": "src/checkoutservice/main.go",
                    "kind": "struct",
                    "source": "neptune",
                },
            ),
        ]

        ranked = _rank_understand_results("checkoutService", results)
        assert ranked[0].data["file"] == "src/checkoutservice/main.go"
        assert ranked[0].data["symbol"] == "checkoutService"
        assert ranked[1].data["file"] == "src/emailservice/demo_pb2_grpc.py"

    def test_ranks_exact_name_over_substring(self):
        """Quote (exact) ranks above GetQuote (substring)."""
        from door.acl import SearchHit

        results = [
            SearchHit(
                repo_name="microservices-demo",
                data={
                    "symbol": "GetQuote",
                    "file": "src/shippingservice/main.go",
                    "kind": "function",
                    "source": "neptune",
                },
            ),
            SearchHit(
                repo_name="microservices-demo",
                data={
                    "symbol": "Quote",
                    "file": "src/shippingservice/quote.go",
                    "kind": "struct",
                    "source": "neptune",
                },
            ),
        ]

        ranked = _rank_understand_results("Quote", results)
        assert ranked[0].data["symbol"] == "Quote"
        assert ranked[0].data["file"] == "src/shippingservice/quote.go"
        assert ranked[1].data["symbol"] == "GetQuote"

    def test_generated_file_demoted_as_tiebreaker(self):
        """Same symbol name in normal vs generated file: normal wins."""
        from door.acl import SearchHit

        results = [
            SearchHit(
                repo_name="microservices-demo",
                data={
                    "symbol": "PlaceOrder",
                    "file": "src/frontend/genproto/demo_pb2_grpc.py",
                    "kind": "function",
                    "source": "code-index-fallback",
                },
            ),
            SearchHit(
                repo_name="microservices-demo",
                data={
                    "symbol": "PlaceOrder",
                    "file": "src/checkoutservice/main.go",
                    "kind": "function",
                    "source": "code-index-fallback",
                },
            ),
        ]

        ranked = _rank_understand_results("PlaceOrder", results)
        assert ranked[0].data["file"] == "src/checkoutservice/main.go"
        assert ranked[1].data["file"] == "src/frontend/genproto/demo_pb2_grpc.py"

    def test_generated_not_filtered_out(self):
        """Generated-file results are demoted but NOT removed."""
        from door.acl import SearchHit

        results = [
            SearchHit(
                repo_name="microservices-demo",
                data={
                    "symbol": "CheckoutService",
                    "file": "src/emailservice/demo_pb2_grpc.py",
                    "kind": "class",
                    "source": "neptune",
                },
            ),
        ]

        ranked = _rank_understand_results("CheckoutService", results)
        assert len(ranked) == 1  # Not filtered out
        assert ranked[0].data["symbol"] == "CheckoutService"


# ---------------------------------------------------------------------------
# Code-index fallback integration test (end-to-end with fixture)
# ---------------------------------------------------------------------------


class TestCodeIndexRanking:
    """Test that code-index fallback ranks results correctly."""

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
    async def test_checkout_service_resolves_to_go_struct(self, mock_s3_client):
        """checkoutService query should rank Go struct above Python pb2_grpc stub."""
        # Disable Neptune so we hit the code-index fallback
        with patch("door.neptune_client.neptune_enabled", return_value=False):
            results = await understand(
                "microservices-demo::checkoutService",
                s3_client=mock_s3_client,
                bucket="test-bucket",
                prefix="code-indexes",
            )

        assert len(results) >= 1
        top = results[0]
        assert top.data["symbol"] == "checkoutService"
        assert top.data["file"] == "src/checkoutservice/main.go"
        # Python stubs should be present but ranked lower
        py_results = [r for r in results if "pb2_grpc" in r.data.get("file", "")]
        assert len(py_results) >= 1  # Not filtered out

    @pytest.mark.asyncio
    async def test_quote_resolves_to_go_struct(self, mock_s3_client):
        """Quote query should rank Go struct above GetQuote substring match."""
        with patch("door.neptune_client.neptune_enabled", return_value=False):
            results = await understand(
                "microservices-demo::Quote",
                s3_client=mock_s3_client,
                bucket="test-bucket",
                prefix="code-indexes",
            )

        assert len(results) >= 1
        top = results[0]
        assert top.data["symbol"] == "Quote"
        assert top.data["file"] == "src/shippingservice/quote.go"

    @pytest.mark.asyncio
    async def test_place_order_prefers_handwritten(self, mock_s3_client):
        """PlaceOrder should rank hand-written Go above generated pb2_grpc stub."""
        with patch("door.neptune_client.neptune_enabled", return_value=False):
            results = await understand(
                "microservices-demo::PlaceOrder",
                s3_client=mock_s3_client,
                bucket="test-bucket",
                prefix="code-indexes",
            )

        assert len(results) >= 1
        top = results[0]
        assert top.data["file"] == "src/checkoutservice/main.go"
        # Generated stub is present but ranked lower
        assert any("pb2_grpc" in r.data.get("file", "") for r in results[1:])


# ---------------------------------------------------------------------------
# Neptune resolve_symbol ranking tests (mocked Neptune driver)
# ---------------------------------------------------------------------------


class TestNeptuneResolveSymbolRanking:
    """Test that resolve_symbol ranks results correctly."""

    def test_exact_match_demotes_generated_file(self, monkeypatch):
        """When exact name matches both Go and pb2 files, Go ranks first."""
        monkeypatch.setenv("NEPTUNE_ENABLED", "true")

        mock_driver = MagicMock()
        mock_session = MagicMock()
        # Strategy 1 (exact name) returns both the Go struct and the Python stub
        mock_result = [
            {
                "name": "checkoutService",
                "file": "src/emailservice/demo_pb2_grpc.py",
                "kind": "class",
                "symbol_id": "s1",
            },
            {
                "name": "checkoutService",
                "file": "src/checkoutservice/main.go",
                "kind": "struct",
                "symbol_id": "s2",
            },
        ]
        mock_session.run.return_value = mock_result
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value = mock_session

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import resolve_symbol

            results = resolve_symbol("microservices-demo", "checkoutService")

        assert len(results) == 2
        # Go struct (non-generated) should rank first
        assert results[0]["file"] == "src/checkoutservice/main.go"
        assert results[1]["file"] == "src/emailservice/demo_pb2_grpc.py"

    def test_contains_match_ranks_exact_case_first(self, monkeypatch):
        """Strategy 2 (contains): exact-case exact-name ranks above substring."""
        monkeypatch.setenv("NEPTUNE_ENABLED", "true")

        mock_driver = MagicMock()
        mock_session = MagicMock()

        # Strategy 1 returns empty (no exact name match)
        # Strategy 2 returns Quote and GetQuote
        call_count = [0]

        def mock_run(cypher, params):
            call_count[0] += 1
            if call_count[0] == 1:
                # Strategy 1: no results
                return []
            else:
                # Strategy 2: contains match returns both
                return [
                    {
                        "name": "GetQuote",
                        "file": "src/shippingservice/main.go",
                        "kind": "function",
                        "symbol_id": "s1",
                    },
                    {
                        "name": "Quote",
                        "file": "src/shippingservice/quote.go",
                        "kind": "struct",
                        "symbol_id": "s2",
                    },
                ]

        mock_session.run.side_effect = mock_run
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_driver.session.return_value = mock_session

        with patch("door.neptune_client.get_neptune_driver", return_value=mock_driver):
            from door.neptune_client import resolve_symbol

            results = resolve_symbol("microservices-demo", "Quote")

        assert len(results) == 2
        # Exact-name "Quote" (score 200) ranks above substring "GetQuote" (score 100)
        assert results[0]["name"] == "Quote"
        assert results[0]["file"] == "src/shippingservice/quote.go"
        assert results[1]["name"] == "GetQuote"
