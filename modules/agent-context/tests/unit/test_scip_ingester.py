"""Unit tests for SCIP-native ingestion pipeline.

Tests the core logic: enclosing-scope resolution, edge kind classification,
moniker parsing, CSV generation, and language detection.
"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
from pathlib import Path


# Add the ingestion image directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "images" / "ingestion"))

from scip_ingester import (
    Edge,
    SCIPGraph,
    SymbolNode,
    build_graph,
    classify_edge_kind,
    is_local_symbol,
    parse_moniker_kind,
    parse_moniker_module,
    parse_moniker_name,
)
from scip_indexer import detect_languages
from scip_neptune_csv import generate_csv, _make_node_id, _make_edge_id
from scip_proto.scip_pb2 import (
    Document,
    Index,
    Metadata,
    Occurrence,
    ToolInfo,
    ROLE_DEFINITION,
    ROLE_IMPORT,
    ROLE_READ_ACCESS,
)


# ---------------------------------------------------------------------------
# Moniker parsing tests
# ---------------------------------------------------------------------------


class TestMonikerParsing:
    """Test SCIP moniker parsing utilities."""

    def test_parse_name_function(self):
        """Function moniker → short name."""
        symbol = "scip-python python Agent-Reach 0.1 src/agent.py/AgentRunner#run()."
        assert parse_moniker_name(symbol) == "run"

    def test_parse_name_class(self):
        """Class moniker → short name."""
        symbol = "scip-python python Agent-Reach 0.1 src/agent.py/AgentRunner#"
        assert parse_moniker_name(symbol) == "AgentRunner"

    def test_parse_name_module(self):
        """Module moniker → short name (path-based)."""
        symbol = "scip-python python requests 2.28 src/api/"
        assert parse_moniker_name(symbol) == "api"

    def test_parse_name_simple(self):
        """Simple symbol → name is the symbol itself."""
        assert parse_moniker_name("foo") == "foo"

    def test_parse_module_with_path(self):
        """Module extraction from moniker with path segments."""
        symbol = "scip-python python pkg 0.1 src/utils/helpers.py/do_thing()."
        module = parse_moniker_module(symbol)
        assert "src/utils/helpers.py" in module

    def test_parse_module_empty(self):
        """Short monikers produce empty module."""
        assert parse_moniker_module("local 42") == ""

    def test_parse_kind_function(self):
        """Descriptor ending in `().` → function."""
        assert parse_moniker_kind("pkg python foo 0.1 bar/baz().") == "function"

    def test_parse_kind_class(self):
        """Descriptor ending in `#` → class."""
        assert parse_moniker_kind("pkg python foo 0.1 bar/MyClass#") == "class"

    def test_parse_kind_module(self):
        """Descriptor ending in `/` → module."""
        assert parse_moniker_kind("pkg python foo 0.1 bar/") == "module"

    def test_parse_kind_variable(self):
        """Descriptor ending in `.` (but not `().`) → variable."""
        assert parse_moniker_kind("pkg python foo 0.1 bar/x.") == "variable"


# ---------------------------------------------------------------------------
# Edge kind classification tests
# ---------------------------------------------------------------------------


class TestEdgeClassification:
    """Test edge kind classification from callee descriptor."""

    def test_function_call(self):
        """Function moniker → CALLS."""
        assert classify_edge_kind("scip-python python pkg 0.1 mod/func().") == "CALLS"

    def test_class_reference(self):
        """Class-only moniker → REFERENCES."""
        assert classify_edge_kind("scip-python python pkg 0.1 mod/MyClass#") == "REFERENCES"

    def test_method_call(self):
        """Method on a type → CALLS (contains `().`)."""
        assert classify_edge_kind("scip-python python pkg 0.1 mod/Class#method().") == "CALLS"

    def test_member_access(self):
        """Member access with # in middle → CALLS."""
        assert classify_edge_kind("scip-python python pkg 0.1 mod/Class#field.") == "CALLS"

    def test_empty_symbol(self):
        """Empty symbol → REFERENCES."""
        assert classify_edge_kind("") == "REFERENCES"

    def test_module_reference(self):
        """Module reference → REFERENCES."""
        assert classify_edge_kind("scip-python python requests 2.28/") == "REFERENCES"


# ---------------------------------------------------------------------------
# Local symbol detection
# ---------------------------------------------------------------------------


class TestLocalSymbol:
    """Test local symbol detection."""

    def test_local_symbol(self):
        assert is_local_symbol("local 42") is True

    def test_local_symbol_with_number(self):
        assert is_local_symbol("local 123") is True

    def test_non_local_symbol(self):
        assert is_local_symbol("scip-python python pkg 0.1 mod/func().") is False

    def test_empty_symbol(self):
        assert is_local_symbol("") is False


# ---------------------------------------------------------------------------
# Graph construction tests
# ---------------------------------------------------------------------------


def _make_test_index() -> Index:
    """Build a minimal SCIP Index for testing enclosing-scope resolution."""
    index = Index()
    index.metadata = Metadata()
    index.metadata.tool_info = ToolInfo(name="scip-python", version="0.6.6")

    # Create a document with definitions and references
    doc = Document()
    doc.relative_path = "src/main.py"
    doc.language = "python"

    # Definition: function `caller_func` at line 5 (0-indexed)
    caller_def = Occurrence()
    caller_def.symbol = "scip-python python myrepo 0.1 src/main.py/caller_func()."
    caller_def.symbol_roles = ROLE_DEFINITION
    caller_def.range = [5, 4, 5, 15]  # line 5, col 4-15

    # Definition: function `another_func` at line 20
    another_def = Occurrence()
    another_def.symbol = "scip-python python myrepo 0.1 src/main.py/another_func()."
    another_def.symbol_roles = ROLE_DEFINITION
    another_def.range = [20, 4, 20, 16]

    # Reference to `helper_func` inside `caller_func` (line 8)
    ref1 = Occurrence()
    ref1.symbol = "scip-python python myrepo 0.1 src/utils.py/helper_func()."
    ref1.symbol_roles = ROLE_READ_ACCESS
    ref1.range = [8, 4, 8, 15]

    # Reference to `SomeClass` inside `another_func` (line 22)
    ref2 = Occurrence()
    ref2.symbol = "scip-python python myrepo 0.1 src/models.py/SomeClass#"
    ref2.symbol_roles = ROLE_READ_ACCESS
    ref2.range = [22, 10, 22, 19]

    # Import (should be skipped)
    import_occ = Occurrence()
    import_occ.symbol = "scip-python python requests 2.28 api/"
    import_occ.symbol_roles = ROLE_IMPORT
    import_occ.range = [1, 0, 1, 15]

    # Local symbol reference (should be skipped)
    local_ref = Occurrence()
    local_ref.symbol = "local 42"
    local_ref.symbol_roles = ROLE_READ_ACCESS
    local_ref.range = [9, 4, 9, 10]

    doc.occurrences = [caller_def, another_def, ref1, ref2, import_occ, local_ref]
    index.documents = [doc]
    return index


class TestBuildGraph:
    """Test graph construction with enclosing-scope resolution."""

    def test_basic_graph_construction(self):
        """Build graph from test index and verify edges."""
        index = _make_test_index()
        graph = build_graph(index, "org/myrepo")

        # Should have nodes for all definitions + referenced callees
        assert graph.node_count >= 3  # caller_func, another_func, helper_func, SomeClass

        # Should have 2 edges (ref1 → helper_func, ref2 → SomeClass)
        assert graph.edge_count == 2

    def test_enclosing_scope_resolution(self):
        """Verify references are attributed to the correct enclosing definition."""
        index = _make_test_index()
        graph = build_graph(index, "org/myrepo")

        # Edge from caller_func → helper_func (ref at line 8, enclosed by def at line 5)
        caller_to_helper = [
            e for e in graph.edges if "caller_func" in e.caller_id and "helper_func" in e.callee_id
        ]
        assert len(caller_to_helper) == 1
        assert caller_to_helper[0].edge_kind == "CALLS"  # helper_func ends with ().

        # Edge from another_func → SomeClass (ref at line 22, enclosed by def at line 20)
        another_to_class = [
            e for e in graph.edges if "another_func" in e.caller_id and "SomeClass" in e.callee_id
        ]
        assert len(another_to_class) == 1
        assert another_to_class[0].edge_kind == "REFERENCES"  # SomeClass ends with #

    def test_imports_skipped(self):
        """Import occurrences should not produce edges."""
        index = _make_test_index()
        graph = build_graph(index, "org/myrepo")

        import_edges = [e for e in graph.edges if "requests" in e.callee_id]
        assert len(import_edges) == 0

    def test_local_symbols_skipped(self):
        """Local symbols should not produce edges."""
        index = _make_test_index()
        graph = build_graph(index, "org/myrepo")

        local_edges = [e for e in graph.edges if "local" in e.callee_id]
        assert len(local_edges) == 0

    def test_self_references_skipped(self):
        """Self-references (symbol referencing itself) should not produce edges."""
        index = Index()
        index.metadata = Metadata()
        index.metadata.tool_info = ToolInfo(name="test", version="1.0")

        doc = Document()
        doc.relative_path = "test.py"

        # Definition
        def_occ = Occurrence()
        def_occ.symbol = "pkg python test 0.1 test.py/func()."
        def_occ.symbol_roles = ROLE_DEFINITION
        def_occ.range = [0, 0, 0, 10]

        # Self-reference (recursive call)
        self_ref = Occurrence()
        self_ref.symbol = "pkg python test 0.1 test.py/func()."
        self_ref.symbol_roles = ROLE_READ_ACCESS
        self_ref.range = [5, 4, 5, 8]

        doc.occurrences = [def_occ, self_ref]
        index.documents = [doc]

        graph = build_graph(index, "org/test")
        assert graph.edge_count == 0

    def test_line_normalization(self):
        """SCIP 0-indexed lines → 1-indexed in output."""
        index = _make_test_index()
        graph = build_graph(index, "org/myrepo")

        # caller_func defined at 0-indexed line 5 → 1-indexed line 6
        caller_node = [n for n in graph.nodes.values() if "caller_func" in n.symbol_id]
        assert len(caller_node) == 1
        assert caller_node[0].line == 6  # 5 + 1

    def test_edge_deduplication(self):
        """Duplicate edges (same caller→callee) should be deduplicated."""
        index = Index()
        index.metadata = Metadata()
        index.metadata.tool_info = ToolInfo(name="test", version="1.0")

        doc = Document()
        doc.relative_path = "test.py"

        def_occ = Occurrence()
        def_occ.symbol = "pkg python test 0.1 test.py/caller()."
        def_occ.symbol_roles = ROLE_DEFINITION
        def_occ.range = [0, 0, 0, 10]

        # Two references to the same callee
        ref1 = Occurrence()
        ref1.symbol = "pkg python test 0.1 other.py/callee()."
        ref1.symbol_roles = ROLE_READ_ACCESS
        ref1.range = [3, 0, 3, 10]

        ref2 = Occurrence()
        ref2.symbol = "pkg python test 0.1 other.py/callee()."
        ref2.symbol_roles = ROLE_READ_ACCESS
        ref2.range = [5, 0, 5, 10]

        doc.occurrences = [def_occ, ref1, ref2]
        index.documents = [doc]

        graph = build_graph(index, "org/test")
        assert graph.edge_count == 1  # Deduplicated


# ---------------------------------------------------------------------------
# Neptune CSV generation tests
# ---------------------------------------------------------------------------


class TestCSVGeneration:
    """Test Neptune CSV file generation from SCIPGraph."""

    def _make_test_graph(self) -> SCIPGraph:
        """Create a simple graph for CSV testing."""
        graph = SCIPGraph(repo="org/test-repo")
        graph.nodes["pkg python test 0.1 a.py/func_a()."] = SymbolNode(
            symbol_id="pkg python test 0.1 a.py/func_a().",
            name="func_a",
            module="a.py",
            file="a.py",
            line=10,
            kind="function",
            repo="org/test-repo",
        )
        graph.nodes["pkg python test 0.1 b.py/func_b()."] = SymbolNode(
            symbol_id="pkg python test 0.1 b.py/func_b().",
            name="func_b",
            module="b.py",
            file="b.py",
            line=5,
            kind="function",
            repo="org/test-repo",
        )
        graph.nodes["pkg python test 0.1 c.py/MyClass#"] = SymbolNode(
            symbol_id="pkg python test 0.1 c.py/MyClass#",
            name="MyClass",
            module="c.py",
            file="c.py",
            line=1,
            kind="class",
            repo="org/test-repo",
        )
        graph.edges = [
            Edge(
                caller_id="pkg python test 0.1 a.py/func_a().",
                callee_id="pkg python test 0.1 b.py/func_b().",
                edge_kind="CALLS",
                file="a.py",
                line=12,
            ),
            Edge(
                caller_id="pkg python test 0.1 a.py/func_a().",
                callee_id="pkg python test 0.1 c.py/MyClass#",
                edge_kind="REFERENCES",
                file="a.py",
                line=14,
            ),
        ]
        return graph

    def test_csv_generation_creates_files(self):
        """CSV generation creates vertices.csv and edges.csv."""
        graph = self._make_test_graph()
        with tempfile.TemporaryDirectory() as tmpdir:
            output = generate_csv(graph, tmpdir)

            assert os.path.isfile(output.vertices_path)
            assert os.path.isfile(output.edges_path)
            assert output.vertex_count == 3
            assert output.edge_count == 2
            assert output.calls_count == 1
            assert output.references_count == 1

    def test_csv_vertex_format(self):
        """Vertex CSV has correct headers and content."""
        graph = self._make_test_graph()
        with tempfile.TemporaryDirectory() as tmpdir:
            output = generate_csv(graph, tmpdir)

            with open(output.vertices_path) as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            assert len(rows) == 3
            # Check headers
            assert "~id" in reader.fieldnames
            assert "~label" in reader.fieldnames
            assert "symbol_id:String" in reader.fieldnames
            assert "name:String" in reader.fieldnames
            assert "module:String" in reader.fieldnames
            assert "file:String" in reader.fieldnames
            assert "line:Int" in reader.fieldnames
            assert "kind:String" in reader.fieldnames
            assert "repo:String" in reader.fieldnames

            # All vertices have label "Symbol"
            for row in rows:
                assert row["~label"] == "Symbol"
                assert row["repo:String"] == "org/test-repo"

    def test_csv_edge_format(self):
        """Edge CSV has correct headers and content."""
        graph = self._make_test_graph()
        with tempfile.TemporaryDirectory() as tmpdir:
            output = generate_csv(graph, tmpdir)

            with open(output.edges_path) as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            assert len(rows) == 2
            # Check a CALLS edge
            calls_edges = [r for r in rows if r["~label"] == "CALLS"]
            assert len(calls_edges) == 1
            assert calls_edges[0]["repo:String"] == "org/test-repo"

            # Check a REFERENCES edge
            ref_edges = [r for r in rows if r["~label"] == "REFERENCES"]
            assert len(ref_edges) == 1

    def test_csv_calls_only_filter(self):
        """calls_only=True filters out REFERENCES edges."""
        graph = self._make_test_graph()
        with tempfile.TemporaryDirectory() as tmpdir:
            output = generate_csv(graph, tmpdir, calls_only=True)

            assert output.edge_count == 1
            assert output.calls_count == 1
            assert output.references_count == 0

            # Only 2 vertices (the ones connected by CALLS)
            assert output.vertex_count == 2

    def test_node_id_deterministic(self):
        """Same symbol_id always produces the same ~id."""
        id1 = _make_node_id("pkg python test 0.1 mod/func().", "org/repo")
        id2 = _make_node_id("pkg python test 0.1 mod/func().", "org/repo")
        assert id1 == id2

    def test_node_id_differs_by_symbol(self):
        """Different symbol_ids produce different ~ids."""
        id1 = _make_node_id("pkg python test 0.1 mod/func_a().", "org/repo")
        id2 = _make_node_id("pkg python test 0.1 mod/func_b().", "org/repo")
        assert id1 != id2

    def test_edge_id_deterministic(self):
        """Same edge parameters always produce the same edge ~id."""
        id1 = _make_edge_id("caller_sym", "callee_sym", "CALLS")
        id2 = _make_edge_id("caller_sym", "callee_sym", "CALLS")
        assert id1 == id2


# ---------------------------------------------------------------------------
# Language detection tests
# ---------------------------------------------------------------------------


class TestLanguageDetection:
    """Test language detection from file extensions."""

    def test_python_detection(self):
        """Detect Python files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "main.py").touch()
            Path(tmpdir, "utils.py").touch()
            Path(tmpdir, "test.py").touch()

            langs = detect_languages(tmpdir)
            assert "python" in langs
            assert langs["python"] == 3

    def test_typescript_detection(self):
        """Detect TypeScript files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "app.ts").touch()
            Path(tmpdir, "component.tsx").touch()

            langs = detect_languages(tmpdir)
            assert "typescript" in langs
            assert langs["typescript"] == 2

    def test_go_detection(self):
        """Detect Go files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "main.go").touch()

            langs = detect_languages(tmpdir)
            assert "go" in langs

    def test_skip_node_modules(self):
        """Files in node_modules should be skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            nm = Path(tmpdir, "node_modules", "pkg")
            nm.mkdir(parents=True)
            Path(nm, "index.js").touch()
            Path(tmpdir, "app.js").touch()

            langs = detect_languages(tmpdir)
            assert langs.get("javascript", 0) == 1  # Only app.js, not node_modules

    def test_skip_git_directory(self):
        """Files in .git should be skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            git_dir = Path(tmpdir, ".git", "hooks")
            git_dir.mkdir(parents=True)
            Path(git_dir, "pre-commit.py").touch()
            Path(tmpdir, "main.py").touch()

            langs = detect_languages(tmpdir)
            assert langs.get("python", 0) == 1

    def test_multi_language_repo(self):
        """Detect multiple languages, sorted by count."""
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(5):
                Path(tmpdir, f"mod{i}.py").touch()
            for i in range(3):
                Path(tmpdir, f"app{i}.ts").touch()
            Path(tmpdir, "main.go").touch()

            langs = detect_languages(tmpdir)
            keys = list(langs.keys())
            assert keys[0] == "python"  # Most files
            assert keys[1] == "typescript"

    def test_empty_repo(self):
        """Empty directory returns no languages."""
        with tempfile.TemporaryDirectory() as tmpdir:
            langs = detect_languages(tmpdir)
            assert langs == {}


# ---------------------------------------------------------------------------
# Protobuf decoder tests
# ---------------------------------------------------------------------------


class TestProtobufDecoder:
    """Test the hand-rolled SCIP protobuf decoder."""

    def test_index_init(self):
        """Index can be initialized with defaults."""
        index = Index()
        assert index.documents == []
        assert index.metadata.tool_info.name == ""

    def test_occurrence_roles(self):
        """Occurrence role flags work correctly."""
        occ = Occurrence()
        occ.symbol_roles = ROLE_DEFINITION | ROLE_READ_ACCESS
        assert occ.symbol_roles & ROLE_DEFINITION
        assert occ.symbol_roles & ROLE_READ_ACCESS
        assert not (occ.symbol_roles & ROLE_IMPORT)

    def test_parse_empty_bytes(self):
        """Parsing empty bytes produces an empty index."""
        index = Index()
        index.ParseFromString(b"")
        assert index.documents == []
