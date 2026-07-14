"""Unit tests for Go symbol detection in _build_basic_code_index.

Issue #3300: Go type declarations (struct, interface) and methods with receivers
were not detected, causing understand/browse to miss Go symbols entirely.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Adjust import path for the ingestion scripts (same pattern as test_scip_ingester.py)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "images" / "ingestion"))

from lang_go import extract_go_func_name as _extract_go_func_name
from lang_go import extract_go_type as _extract_go_type


# ---------------------------------------------------------------------------
# _extract_go_func_name tests
# ---------------------------------------------------------------------------


class TestExtractGoFuncName:
    """Test Go function name extraction from func declarations."""

    def test_plain_function(self):
        assert _extract_go_func_name("func main() {") == "main"

    def test_function_with_return(self):
        assert (
            _extract_go_func_name("func CreateQuoteFromCount(count int) Quote {")
            == "CreateQuoteFromCount"
        )

    def test_method_with_pointer_receiver(self):
        """Methods with pointer receiver: func (s *Server) Run()"""
        assert (
            _extract_go_func_name(
                "func (s *frontendServer) homeHandler(w http.ResponseWriter, r *http.Request) {"
            )
            == "homeHandler"
        )

    def test_method_with_value_receiver(self):
        """Methods with value receiver: func (q Quote) String()"""
        assert _extract_go_func_name("func (q Quote) String() string {") == "String"

    def test_method_multiword_receiver(self):
        """Receiver with package-qualified type."""
        assert (
            _extract_go_func_name(
                "func (cs *checkoutService) PlaceOrder(ctx context.Context, req *pb.PlaceOrderRequest) (*pb.PlaceOrderResponse, error) {"
            )
            == "PlaceOrder"
        )

    def test_exported_function(self):
        assert (
            _extract_go_func_name("func NewFrontendServer() *frontendServer {")
            == "NewFrontendServer"
        )

    def test_unexported_function(self):
        assert _extract_go_func_name("func initTracing() {") == "initTracing"

    def test_function_no_parens(self):
        """Edge case: malformed line with no parens after name."""
        assert _extract_go_func_name("func orphan") == "orphan"

    def test_empty_after_func(self):
        """Edge case: just 'func ' with nothing after."""
        assert _extract_go_func_name("func ") == ""

    def test_generic_function(self):
        """Go 1.18+ generics: func Map[T any](slice []T, f func(T) T) []T."""
        assert _extract_go_func_name("func Map[T any](slice []T, f func(T) T) []T {") == "Map"

    def test_malformed_receiver_no_close_paren(self):
        """Malformed receiver without closing paren."""
        assert _extract_go_func_name("func (broken ") == ""


# ---------------------------------------------------------------------------
# _extract_go_type tests
# ---------------------------------------------------------------------------


class TestExtractGoType:
    """Test Go type declaration extraction."""

    def test_struct(self):
        name, kind = _extract_go_type("type frontendServer struct {")
        assert name == "frontendServer"
        assert kind == "struct"

    def test_struct_exported(self):
        name, kind = _extract_go_type("type Quote struct {")
        assert name == "Quote"
        assert kind == "struct"

    def test_interface(self):
        name, kind = _extract_go_type("type Handler interface {")
        assert name == "Handler"
        assert kind == "interface"

    def test_type_alias(self):
        """Named types (not struct/interface) get kind='class'."""
        name, kind = _extract_go_type("type Duration int64")
        assert name == "Duration"
        assert kind == "class"

    def test_type_block_opener(self):
        """'type (' should be skipped (it's a block opener, not a declaration)."""
        name, kind = _extract_go_type("type (")
        assert name == ""
        assert kind == ""

    def test_empty_type(self):
        """Just 'type' with nothing after (shouldn't happen, but handle gracefully)."""
        name, kind = _extract_go_type("type ")
        assert name == ""
        assert kind == ""

    def test_generic_struct(self):
        """Go 1.18+ generic struct: type Set[T comparable] struct {}"""
        name, kind = _extract_go_type("type Set[T comparable] struct {")
        assert name == "Set"
        assert kind == "struct"

    def test_generic_interface(self):
        """Go 1.18+ generic interface."""
        name, kind = _extract_go_type("type Ordered[T any] interface {")
        assert name == "Ordered"
        assert kind == "interface"

    def test_func_type(self):
        """Function type declaration."""
        name, kind = _extract_go_type("type HandlerFunc func(ResponseWriter, *Request)")
        assert name == "HandlerFunc"
        assert kind == "class"  # Not struct/interface, so fallback

    def test_struct_no_brace(self):
        """struct keyword without opening brace (single-line)."""
        name, kind = _extract_go_type("type Point struct{ X, Y int }")
        assert name == "Point"
        assert kind == "struct"


# ---------------------------------------------------------------------------
# Integration: _build_basic_code_index with Go source
# ---------------------------------------------------------------------------


class TestBuildBasicCodeIndexGo:
    """Test that _build_basic_code_index correctly indexes Go symbols."""

    @pytest.fixture
    def go_repo(self, tmp_path):
        """Create a minimal Go repo mimicking microservices-demo structure."""
        # src/frontend/main.go
        frontend_dir = tmp_path / "src" / "frontend"
        frontend_dir.mkdir(parents=True)
        (frontend_dir / "main.go").write_text(
            """package main

import (
    "fmt"
    "net/http"
)

type frontendServer struct {
    productCatalogSvcConn *grpc.ClientConn
    currencySvcConn       *grpc.ClientConn
}

func (fe *frontendServer) homeHandler(w http.ResponseWriter, r *http.Request) {
    fmt.Fprintf(w, "Hello")
}

func main() {
    fe := &frontendServer{}
    http.HandleFunc("/", fe.homeHandler)
}
"""
        )

        # src/shippingservice/quote.go
        shipping_dir = tmp_path / "src" / "shippingservice"
        shipping_dir.mkdir(parents=True)
        (shipping_dir / "quote.go").write_text(
            """package main

type Quote struct {
    Dollars uint32
    Cents   uint32
}

func CreateQuoteFromCount(count int) Quote {
    return Quote{
        Dollars: uint32(count / 10),
        Cents:   uint32(count % 10),
    }
}
"""
        )

        # src/checkoutservice/main.go
        checkout_dir = tmp_path / "src" / "checkoutservice"
        checkout_dir.mkdir(parents=True)
        (checkout_dir / "main.go").write_text(
            """package main

type checkoutService struct {
    productCatalogSvcAddr string
    shippingSvcAddr       string
}

func (cs *checkoutService) PlaceOrder(ctx context.Context) error {
    return nil
}
"""
        )

        # go.mod (so language detection works)
        (tmp_path / "go.mod").write_text("module github.com/example/demo\n\ngo 1.21\n")

        return tmp_path

    def test_detects_go_structs(self, go_repo):
        """All three failing golden-case structs must be detected."""
        symbols = self._extract_symbols_from_repo(go_repo)

        # Find the struct symbols
        struct_names = [s["name"] for s in symbols if s["type"] == "struct"]
        assert "frontendServer" in struct_names, f"frontendServer not found in {struct_names}"
        assert "Quote" in struct_names, f"Quote not found in {struct_names}"
        assert "checkoutService" in struct_names, f"checkoutService not found in {struct_names}"

    def test_detects_go_functions(self, go_repo):
        """Regular functions must still be detected."""
        symbols = self._extract_symbols_from_repo(go_repo)
        func_names = [s["name"] for s in symbols if s["type"] == "function"]
        assert "main" in func_names
        assert "CreateQuoteFromCount" in func_names

    def test_detects_go_methods(self, go_repo):
        """Methods with receivers must be detected."""
        symbols = self._extract_symbols_from_repo(go_repo)
        func_names = [s["name"] for s in symbols if s["type"] == "function"]
        assert "homeHandler" in func_names, f"homeHandler not found in {func_names}"
        assert "PlaceOrder" in func_names, f"PlaceOrder not found in {func_names}"

    def test_correct_file_paths(self, go_repo):
        """Symbols must have correct relative file paths."""
        symbols = self._extract_symbols_from_repo(go_repo)

        # Find frontendServer
        fe_symbols = [s for s in symbols if s["name"] == "frontendServer"]
        assert len(fe_symbols) == 1
        assert fe_symbols[0]["file"] == "src/frontend/main.go"

        # Find Quote
        quote_symbols = [s for s in symbols if s["name"] == "Quote"]
        assert len(quote_symbols) == 1
        assert quote_symbols[0]["file"] == "src/shippingservice/quote.go"

    def _extract_symbols_from_repo(self, repo_path) -> list[dict]:
        """Simulate what _build_basic_code_index does for Go files."""
        symbols: list[dict] = []
        clone = Path(repo_path)

        for fpath in clone.rglob("*.go"):
            if any(p in fpath.parts for p in ("vendor", ".git")):
                continue
            rel = str(fpath.relative_to(clone))
            try:
                content = fpath.read_text()
                lines = content.split("\n")
            except Exception:
                continue

            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("func "):
                    name = _extract_go_func_name(stripped)
                    if name:
                        symbols.append(
                            {"name": name, "type": "function", "file": rel, "line": i + 1}
                        )
                elif stripped.startswith("type "):
                    name, kind = _extract_go_type(stripped)
                    if name:
                        symbols.append({"name": name, "type": kind, "file": rel, "line": i + 1})

        return symbols


# ---------------------------------------------------------------------------
# Understand verb scoring with Go struct symbols
# ---------------------------------------------------------------------------


class TestUnderstandGoSymbols:
    """Verify that the understand verb correctly resolves Go struct symbols
    from the code-index fallback path (the path active when Neptune is disabled).

    This is the end-to-end scenario that issue #3300 describes.
    """

    @pytest.fixture
    def go_code_index(self):
        """A code-index.json fixture with Go struct symbols (simulates what
        _build_basic_code_index now produces for microservices-demo).
        """
        return {
            "repo": "GoogleCloudPlatform/microservices-demo",
            "analyzed_at": "2026-07-08T00:00:00Z",
            "language_stats": {"go": 15, "python": 3, "javascript": 2},
            "symbols": [
                {
                    "name": "frontendServer",
                    "type": "struct",
                    "file": "src/frontend/main.go",
                    "line": 63,
                },
                {
                    "name": "checkoutService",
                    "type": "struct",
                    "file": "src/checkoutservice/main.go",
                    "line": 66,
                },
                {
                    "name": "Quote",
                    "type": "struct",
                    "file": "src/shippingservice/quote.go",
                    "line": 23,
                },
                {
                    "name": "CreateQuoteFromCount",
                    "type": "function",
                    "file": "src/shippingservice/quote.go",
                    "line": 28,
                },
                {
                    "name": "RecommendationService",
                    "type": "class",
                    "file": "src/recommendationservice/recommendation_server.py",
                    "line": 69,
                },
            ],
            "imports": {},
            "call_graph": {},
            "dependencies": {"external": [], "internal": {}},
        }

    @pytest.fixture
    def mock_s3_client_go(self, go_code_index):
        """Mock S3 client returning the Go code-index fixture."""
        client = MagicMock()
        body_mock = MagicMock()
        body_mock.read.return_value = json.dumps(go_code_index).encode()
        client.get_object.return_value = {"Body": body_mock}
        client.exceptions = MagicMock()
        client.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
        return client

    @pytest.mark.asyncio
    async def test_understand_go_struct_frontendServer(self, mock_s3_client_go):
        """msdemo-understand-001: frontendServer struct must be found."""
        from door.structural_backend import understand

        hits = await understand(
            "microservices-demo::frontendServer",
            s3_client=mock_s3_client_go,
            bucket="test-bucket",
            prefix="code-indexes",
        )
        assert len(hits) >= 1
        assert hits[0].data["symbol"] == "frontendServer"
        assert hits[0].data["file"] == "src/frontend/main.go"
        assert hits[0].data["kind"] == "struct"

    @pytest.mark.asyncio
    async def test_understand_go_struct_checkoutService(self, mock_s3_client_go):
        """msdemo-understand-002: checkoutService struct must be found."""
        from door.structural_backend import understand

        hits = await understand(
            "microservices-demo::checkoutService",
            s3_client=mock_s3_client_go,
            bucket="test-bucket",
            prefix="code-indexes",
        )
        assert len(hits) >= 1
        assert hits[0].data["symbol"] == "checkoutService"
        assert hits[0].data["file"] == "src/checkoutservice/main.go"
        assert hits[0].data["kind"] == "struct"

    @pytest.mark.asyncio
    async def test_understand_go_struct_Quote(self, mock_s3_client_go):
        """msdemo-understand-003: Quote struct must be found."""
        from door.structural_backend import understand

        hits = await understand(
            "microservices-demo::Quote",
            s3_client=mock_s3_client_go,
            bucket="test-bucket",
            prefix="code-indexes",
        )
        assert len(hits) >= 1
        assert hits[0].data["symbol"] == "Quote"
        assert hits[0].data["file"] == "src/shippingservice/quote.go"
        assert hits[0].data["kind"] == "struct"

    @pytest.mark.asyncio
    async def test_understand_go_function_still_works(self, mock_s3_client_go):
        """Functions in Go must still resolve correctly."""
        from door.structural_backend import understand

        hits = await understand(
            "microservices-demo::CreateQuoteFromCount",
            s3_client=mock_s3_client_go,
            bucket="test-bucket",
            prefix="code-indexes",
        )
        assert len(hits) >= 1
        assert hits[0].data["symbol"] == "CreateQuoteFromCount"
        assert hits[0].data["kind"] == "function"
