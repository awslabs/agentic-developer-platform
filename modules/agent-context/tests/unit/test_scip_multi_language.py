"""Unit tests for multi-language SCIP indexing (#2973, #3132).

Tests that index_repo() iterates all detected languages (not just the primary),
that merge_graphs() correctly unions nodes and edges, and that fail-soft behavior
skips broken languages without aborting the whole repo.

#3132 additions: Tests for _index_python() PATH-based env handling,
_ensure_pyright_section() auto-injection, and failed_languages metrics surfacing.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add the ingestion image directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "images" / "ingestion"))

from scip_indexer import index_repo, _consolidate_languages, _index_python, _ensure_pyright_section
from scip_ingester import SCIPGraph, SymbolNode, Edge, merge_graphs


# ---------------------------------------------------------------------------
# _consolidate_languages tests
# ---------------------------------------------------------------------------


class TestConsolidateLanguages:
    """Test language deduplication by indexer family."""

    def test_ts_and_js_consolidate_to_typescript(self):
        """TypeScript and JavaScript share an indexer → only typescript."""
        result = _consolidate_languages(["typescript", "javascript"])
        assert result == ["typescript"]

    def test_js_only_maps_to_typescript(self):
        """JavaScript alone maps to typescript indexer."""
        result = _consolidate_languages(["javascript"])
        assert result == ["typescript"]

    def test_jvm_languages_consolidate_to_java(self):
        """JVM langs (java, kotlin, scala) → single java entry."""
        result = _consolidate_languages(["kotlin", "java", "scala"])
        assert result == ["java"]

    def test_python_and_typescript_both_kept(self):
        """Different indexer families are kept as separate entries."""
        result = _consolidate_languages(["python", "typescript"])
        assert result == ["python", "typescript"]

    def test_python_ts_js_consolidates_js(self):
        """Python + TypeScript + JavaScript → python + typescript."""
        result = _consolidate_languages(["python", "typescript", "javascript"])
        assert result == ["python", "typescript"]

    def test_unsupported_language_dropped(self):
        """Languages without an indexer in INDEXERS are dropped."""
        result = _consolidate_languages(["python", "haskell"])
        assert result == ["python"]

    def test_empty_input(self):
        """Empty list returns empty."""
        assert _consolidate_languages([]) == []

    def test_preserves_order(self):
        """First occurrence order is preserved."""
        result = _consolidate_languages(["go", "python", "typescript"])
        assert result == ["go", "python", "typescript"]


# ---------------------------------------------------------------------------
# merge_graphs tests
# ---------------------------------------------------------------------------


class TestMergeGraphs:
    """Test graph merging (union nodes, concatenate edges)."""

    def _make_python_graph(self) -> SCIPGraph:
        """Create a small Python-language graph."""
        g = SCIPGraph(repo="org/repo")
        g.nodes["scip-python python pkg 0.1 models.py/Position#"] = SymbolNode(
            symbol_id="scip-python python pkg 0.1 models.py/Position#",
            name="Position",
            module="models.py",
            file="agent/backtest/models.py",
            line=10,
            kind="class",
            repo="org/repo",
        )
        g.nodes["scip-python python pkg 0.1 utils.py/truncate_for_display()."] = SymbolNode(
            symbol_id="scip-python python pkg 0.1 utils.py/truncate_for_display().",
            name="truncate_for_display",
            module="utils.py",
            file="agent/utils.py",
            line=5,
            kind="function",
            repo="org/repo",
        )
        g.edges.append(
            Edge(
                caller_id="scip-python python pkg 0.1 utils.py/truncate_for_display().",
                callee_id="scip-python python pkg 0.1 models.py/Position#",
                edge_kind="REFERENCES",
                file="agent/utils.py",
                line=12,
            )
        )
        return g

    def _make_typescript_graph(self) -> SCIPGraph:
        """Create a small TypeScript-language graph."""
        g = SCIPGraph(repo="org/repo")
        g.nodes["scip-ts npm pkg 1.0 src/utils.ts/formatUsd()."] = SymbolNode(
            symbol_id="scip-ts npm pkg 1.0 src/utils.ts/formatUsd().",
            name="formatUsd",
            module="src/utils.ts",
            file="frontend/src/utils.ts",
            line=3,
            kind="function",
            repo="org/repo",
        )
        g.nodes["scip-ts npm pkg 1.0 src/app.tsx/App()."] = SymbolNode(
            symbol_id="scip-ts npm pkg 1.0 src/app.tsx/App().",
            name="App",
            module="src/app.tsx",
            file="frontend/src/app.tsx",
            line=1,
            kind="function",
            repo="org/repo",
        )
        g.edges.append(
            Edge(
                caller_id="scip-ts npm pkg 1.0 src/app.tsx/App().",
                callee_id="scip-ts npm pkg 1.0 src/utils.ts/formatUsd().",
                edge_kind="CALLS",
                file="frontend/src/app.tsx",
                line=15,
            )
        )
        return g

    def test_merge_two_language_graphs(self):
        """Merging Python + TypeScript graphs produces union of both."""
        py = self._make_python_graph()
        ts = self._make_typescript_graph()

        merged = merge_graphs([py, ts])

        assert merged.node_count == 4  # 2 Python + 2 TypeScript
        assert merged.edge_count == 2  # 1 Python + 1 TypeScript
        assert merged.repo == "org/repo"

        # Python symbols present
        assert "scip-python python pkg 0.1 models.py/Position#" in merged.nodes
        assert "scip-python python pkg 0.1 utils.py/truncate_for_display()." in merged.nodes

        # TypeScript symbols present
        assert "scip-ts npm pkg 1.0 src/utils.ts/formatUsd()." in merged.nodes
        assert "scip-ts npm pkg 1.0 src/app.tsx/App()." in merged.nodes

    def test_merge_empty_list(self):
        """Empty list returns empty graph."""
        merged = merge_graphs([])
        assert merged.node_count == 0
        assert merged.edge_count == 0

    def test_merge_single_graph(self):
        """Single graph returned as-is."""
        py = self._make_python_graph()
        merged = merge_graphs([py])
        assert merged is py  # Same object

    def test_no_cross_language_symbol_id_collisions(self):
        """Symbols from different languages have distinct symbol_ids."""
        py = self._make_python_graph()
        ts = self._make_typescript_graph()
        merged = merge_graphs([py, ts])

        # All 4 nodes are distinct keys (no overwrites)
        all_ids = list(merged.nodes.keys())
        assert len(all_ids) == len(set(all_ids))

    def test_merge_preserves_edge_info(self):
        """Edges retain file and line info after merge."""
        py = self._make_python_graph()
        ts = self._make_typescript_graph()
        merged = merge_graphs([py, ts])

        py_edge = [e for e in merged.edges if e.file == "agent/utils.py"]
        ts_edge = [e for e in merged.edges if e.file == "frontend/src/app.tsx"]

        assert len(py_edge) == 1
        assert py_edge[0].line == 12
        assert len(ts_edge) == 1
        assert ts_edge[0].line == 15


# ---------------------------------------------------------------------------
# index_repo multi-language tests
# ---------------------------------------------------------------------------


class TestIndexRepoMultiLanguage:
    """Test that index_repo iterates all detected languages."""

    def test_indexes_both_python_and_typescript(self):
        """A repo with .py and .ts files indexes both languages."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create Python files
            Path(tmpdir, "main.py").write_text("class Foo: pass\n")
            Path(tmpdir, "utils.py").write_text("def bar(): pass\n")
            # Create TypeScript files
            Path(tmpdir, "app.ts").write_text("export function baz() {}\n")
            Path(tmpdir, "index.tsx").write_text("export const App = () => {}\n")

            # Mock the indexers — they return fake .scip paths
            py_scip = os.path.join(tmpdir, "index.scip.python")
            ts_scip = os.path.join(tmpdir, "index.scip.typescript")
            Path(py_scip).touch()
            Path(ts_scip).touch()

            mock_py_indexer = MagicMock(return_value=(py_scip, None))
            mock_ts_indexer = MagicMock(return_value=(ts_scip, None))
            mock_py_deps = MagicMock(return_value=(True, "ok"))
            mock_ts_deps = MagicMock(return_value=(True, "ok"))

            with (
                patch.dict(
                    "scip_indexer.INDEXERS",
                    {
                        "python": mock_py_indexer,
                        "typescript": mock_ts_indexer,
                        "javascript": mock_ts_indexer,
                    },
                ),
                patch.dict(
                    "scip_indexer.DEP_RESOLVERS",
                    {
                        "python": mock_py_deps,
                        "typescript": mock_ts_deps,
                        "javascript": mock_ts_deps,
                    },
                ),
            ):
                report = index_repo(tmpdir, "org/multi-lang")

            # Both indexers were called
            mock_py_indexer.assert_called_once_with(tmpdir)
            mock_ts_indexer.assert_called_once_with(tmpdir)

            # Report shows both languages succeeded
            assert len(report.successful_languages) == 2
            assert "python" in report.successful_languages
            assert "typescript" in report.successful_languages

            # combined_scip_path is set (first success)
            assert report.combined_scip_path is not None

    def test_fail_soft_one_language_fails(self):
        """If one language's indexer fails, the other still succeeds."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "main.py").write_text("class Foo: pass\n")
            Path(tmpdir, "app.ts").write_text("export function baz() {}\n")

            ts_scip = os.path.join(tmpdir, "index.scip.typescript")
            Path(ts_scip).touch()

            mock_py_indexer = MagicMock(return_value=(None, "scip-python not found"))
            mock_ts_indexer = MagicMock(return_value=(ts_scip, None))
            mock_py_deps = MagicMock(return_value=(False, "no venv"))
            mock_ts_deps = MagicMock(return_value=(True, "ok"))

            with (
                patch.dict(
                    "scip_indexer.INDEXERS",
                    {
                        "python": mock_py_indexer,
                        "typescript": mock_ts_indexer,
                        "javascript": mock_ts_indexer,
                    },
                ),
                patch.dict(
                    "scip_indexer.DEP_RESOLVERS",
                    {
                        "python": mock_py_deps,
                        "typescript": mock_ts_deps,
                        "javascript": mock_ts_deps,
                    },
                ),
            ):
                report = index_repo(tmpdir, "org/partial-fail")

            # TypeScript succeeded
            assert report.any_success
            assert "typescript" in report.successful_languages
            # Python failed
            assert "python" not in report.successful_languages
            # combined_scip_path points to the successful one
            assert report.combined_scip_path == ts_scip

    def test_fail_soft_indexer_exception(self):
        """If an indexer raises an exception, it's caught and skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "main.py").write_text("class Foo: pass\n")
            Path(tmpdir, "app.ts").write_text("export function baz() {}\n")

            ts_scip = os.path.join(tmpdir, "index.scip.typescript")
            Path(ts_scip).touch()

            mock_py_indexer = MagicMock(side_effect=RuntimeError("segfault"))
            mock_ts_indexer = MagicMock(return_value=(ts_scip, None))
            mock_py_deps = MagicMock(return_value=(True, "ok"))
            mock_ts_deps = MagicMock(return_value=(True, "ok"))

            with (
                patch.dict(
                    "scip_indexer.INDEXERS",
                    {
                        "python": mock_py_indexer,
                        "typescript": mock_ts_indexer,
                        "javascript": mock_ts_indexer,
                    },
                ),
                patch.dict(
                    "scip_indexer.DEP_RESOLVERS",
                    {
                        "python": mock_py_deps,
                        "typescript": mock_ts_deps,
                        "javascript": mock_ts_deps,
                    },
                ),
            ):
                report = index_repo(tmpdir, "org/crash-recover")

            # TypeScript still succeeded
            assert report.any_success
            assert "typescript" in report.successful_languages
            # Python result logged with error
            py_results = [r for r in report.results if r.language == "python"]
            assert len(py_results) == 1
            assert "segfault" in py_results[0].error

    def test_single_language_repo_unchanged(self):
        """Single-language repo behaves the same as before."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "main.py").write_text("def hello(): pass\n")

            py_scip = os.path.join(tmpdir, "index.scip")
            Path(py_scip).touch()

            mock_py_indexer = MagicMock(return_value=(py_scip, None))
            mock_py_deps = MagicMock(return_value=(True, "ok"))

            with (
                patch.dict("scip_indexer.INDEXERS", {"python": mock_py_indexer}),
                patch.dict("scip_indexer.DEP_RESOLVERS", {"python": mock_py_deps}),
            ):
                report = index_repo(tmpdir, "org/single-lang")

            assert report.any_success
            assert report.successful_languages == ["python"]
            # After rename, path is index.<lang>.scip
            expected_path = os.path.join(tmpdir, "index.python.scip")
            assert report.combined_scip_path == expected_path

    def test_js_and_ts_not_double_indexed(self):
        """TypeScript + JavaScript consolidated — indexer called once."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "app.ts").write_text("export const x = 1\n")
            Path(tmpdir, "legacy.js").write_text("module.exports = {}\n")

            ts_scip = os.path.join(tmpdir, "index.scip")
            Path(ts_scip).touch()

            mock_ts_indexer = MagicMock(return_value=(ts_scip, None))
            mock_ts_deps = MagicMock(return_value=(True, "ok"))

            with (
                patch.dict(
                    "scip_indexer.INDEXERS",
                    {"typescript": mock_ts_indexer, "javascript": mock_ts_indexer},
                ),
                patch.dict(
                    "scip_indexer.DEP_RESOLVERS",
                    {"typescript": mock_ts_deps, "javascript": mock_ts_deps},
                ),
            ):
                report = index_repo(tmpdir, "org/ts-js-repo")

            # Only called once (not twice for TS + JS)
            mock_ts_indexer.assert_called_once()
            assert report.successful_languages == ["typescript"]

    def test_multi_language_produces_distinct_scip_paths(self):
        """Regression: multiple languages must yield distinct .scip file paths.

        Real indexers all write to clone_path/index.scip. The rename logic in
        index_repo() must produce per-language paths so merge_graphs() decodes
        distinct data from each language.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create files for both languages
            Path(tmpdir, "main.py").write_text("class Foo: pass\n")
            Path(tmpdir, "app.ts").write_text("export function baz() {}\n")

            # Simulate real indexer behavior: both write to the SAME path
            # (clone_path/index.scip — the canonical path all _index_*() use)

            def fake_python_indexer(clone_path):
                """Simulates _index_python writing to index.scip."""
                path = os.path.join(clone_path, "index.scip")
                Path(path).write_bytes(b"PYTHON_SCIP_DATA")
                return (path, None)

            def fake_ts_indexer(clone_path):
                """Simulates _index_typescript writing to index.scip."""
                path = os.path.join(clone_path, "index.scip")
                Path(path).write_bytes(b"TYPESCRIPT_SCIP_DATA")
                return (path, None)

            mock_py_deps = MagicMock(return_value=(True, "ok"))
            mock_ts_deps = MagicMock(return_value=(True, "ok"))

            with (
                patch.dict(
                    "scip_indexer.INDEXERS",
                    {
                        "python": fake_python_indexer,
                        "typescript": fake_ts_indexer,
                        "javascript": fake_ts_indexer,
                    },
                ),
                patch.dict(
                    "scip_indexer.DEP_RESOLVERS",
                    {
                        "python": mock_py_deps,
                        "typescript": mock_ts_deps,
                        "javascript": mock_ts_deps,
                    },
                ),
            ):
                report = index_repo(tmpdir, "org/multi-lang-paths")

            # Both languages succeeded
            assert len(report.successful_languages) == 2

            # Paths must be DISTINCT
            successful = [r for r in report.results if r.success and r.scip_path]
            paths = [r.scip_path for r in successful]
            assert len(paths) == 2
            assert paths[0] != paths[1], f"scip_path collision: both languages point to {paths[0]}"

            # Each file must exist with distinct content
            assert os.path.isfile(paths[0])
            assert os.path.isfile(paths[1])
            contents = [Path(p).read_bytes() for p in paths]
            assert contents[0] != contents[1], (
                "Both .scip files have identical content — second language overwrote first"
            )


# ---------------------------------------------------------------------------
# #3132 regression tests: _index_python env handling, pyright section, metrics
# ---------------------------------------------------------------------------


class TestIndexPythonEnvHandling:
    """Tests that _index_python() uses PATH-based venv discovery, not --environment JSON.

    Regression for #3132: scip-python's --environment expects a JSON array of
    {name, version, files} package entries, not an {pythonPath, sitePackagesPath} object.
    The fix drops --environment entirely and puts the venv bin on PATH instead.
    """

    def test_no_environment_flag_passed(self):
        """_index_python must NOT pass --environment to scip-python (#3132)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a fake venv so the PATH logic activates
            venv_bin = os.path.join(tmpdir, ".scip-venv", "bin")
            os.makedirs(venv_bin)
            Path(venv_bin, "python3").touch()

            captured_cmd = []
            captured_env = {}

            def mock_run(cmd, **kwargs):
                captured_cmd.extend(cmd)
                captured_env.update(kwargs.get("env", {}))
                mock_result = MagicMock()
                mock_result.returncode = 1
                mock_result.stderr = b"mock failure"
                return mock_result

            with patch("subprocess.run", side_effect=mock_run):
                _index_python(tmpdir)

            # The --environment flag must NOT appear in the command
            assert "--environment" not in captured_cmd, (
                "scip-python should not receive --environment flag (#3132)"
            )

    def test_venv_bin_on_path(self):
        """When .scip-venv exists, its bin/ is prepended to PATH."""
        with tempfile.TemporaryDirectory() as tmpdir:
            venv_bin = os.path.join(tmpdir, ".scip-venv", "bin")
            os.makedirs(venv_bin)

            captured_env = {}

            def mock_run(cmd, **kwargs):
                captured_env.update(kwargs.get("env", {}))
                mock_result = MagicMock()
                mock_result.returncode = 1
                mock_result.stderr = b"mock failure"
                return mock_result

            with patch("subprocess.run", side_effect=mock_run):
                _index_python(tmpdir)

            # PATH must start with the venv bin directory
            assert captured_env.get("PATH", "").startswith(venv_bin + ":"), (
                f"PATH should start with venv bin: {captured_env.get('PATH', '')}"
            )
            # VIRTUAL_ENV should be set
            assert captured_env.get("VIRTUAL_ENV") == os.path.join(tmpdir, ".scip-venv")

    def test_no_venv_no_path_modification(self):
        """Without .scip-venv, scip-python runs with unmodified env (no crash)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            captured_env = {}

            def mock_run(cmd, **kwargs):
                captured_env.update(kwargs.get("env", {}))
                mock_result = MagicMock()
                mock_result.returncode = 1
                mock_result.stderr = b"mock failure"
                return mock_result

            with patch("subprocess.run", side_effect=mock_run):
                _index_python(tmpdir)

            # PATH should be the original system PATH (not prepended with anything)
            assert "VIRTUAL_ENV" not in captured_env

    def test_no_environment_json_file_written(self):
        """No .scip-environment.json should be created (#3132 root cause)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            venv_bin = os.path.join(tmpdir, ".scip-venv", "bin")
            os.makedirs(venv_bin)

            def mock_run(cmd, **kwargs):
                mock_result = MagicMock()
                mock_result.returncode = 1
                mock_result.stderr = b"mock failure"
                return mock_result

            with patch("subprocess.run", side_effect=mock_run):
                _index_python(tmpdir)

            env_file = os.path.join(tmpdir, ".scip-environment.json")
            assert not os.path.exists(env_file), (
                ".scip-environment.json should not be created — "
                "scip-python expects array shape we can't guarantee (#3132)"
            )


class TestEnsurePyrightSection:
    """Tests for _ensure_pyright_section() — auto-injection of [tool.pyright].

    Regression for #3132: scip-python hard-fails when pyproject.toml exists but
    lacks [tool.pyright]. The fix appends an empty section to the clone.
    """

    def test_appends_section_when_missing(self):
        """pyproject.toml without [tool.pyright] gets the section appended."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pyproject = Path(tmpdir, "pyproject.toml")
            pyproject.write_text('[project]\nname = "vibe-trading"\nversion = "0.1.0"\n')

            _ensure_pyright_section(tmpdir)

            content = pyproject.read_text()
            assert "[tool.pyright]" in content
            # Original content preserved
            assert '[project]\nname = "vibe-trading"' in content

    def test_no_duplicate_when_already_present(self):
        """If [tool.pyright] already exists, don't append a duplicate."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pyproject = Path(tmpdir, "pyproject.toml")
            original = '[project]\nname = "test"\n\n[tool.pyright]\nvenvPath = "."\n'
            pyproject.write_text(original)

            _ensure_pyright_section(tmpdir)

            content = pyproject.read_text()
            # Should remain unchanged
            assert content == original

    def test_no_pyproject_no_error(self):
        """No pyproject.toml at all — function is a no-op, no crash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # No pyproject.toml created
            _ensure_pyright_section(tmpdir)  # Should not raise

    def test_pyright_appended_before_scip_invocation(self):
        """Integration: _index_python calls _ensure_pyright_section before indexing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pyproject = Path(tmpdir, "pyproject.toml")
            pyproject.write_text('[project]\nname = "test-repo"\n')

            def mock_run(cmd, **kwargs):
                # At the time scip-python is invoked, pyproject should have pyright
                content = pyproject.read_text()
                assert "[tool.pyright]" in content, (
                    "pyright section must be added BEFORE scip-python runs"
                )
                mock_result = MagicMock()
                mock_result.returncode = 1
                mock_result.stderr = b"mock"
                return mock_result

            with patch("subprocess.run", side_effect=mock_run):
                _index_python(tmpdir)


class TestFailedLanguagesMetrics:
    """Tests that per-language failures propagate into stage result metrics (#3132).

    When one language fails but others succeed (any_success=True), the pipeline
    must record failed_languages in the result dict so stage metrics are grep-able.
    """

    def test_failed_languages_in_result(self):
        """Result dict includes failed_languages when one indexer fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "main.py").write_text("class Pos: pass\n")
            Path(tmpdir, "app.ts").write_text("export class Pos {}\n")

            ts_scip = os.path.join(tmpdir, "index.scip")
            Path(ts_scip).write_bytes(b"TS_DATA")

            mock_py_indexer = MagicMock(
                return_value=(None, "scip-python exited 1: TypeError: .map is not a function")
            )
            mock_ts_indexer = MagicMock(return_value=(ts_scip, None))
            mock_py_deps = MagicMock(return_value=(True, "ok"))
            mock_ts_deps = MagicMock(return_value=(True, "ok"))

            with (
                patch.dict(
                    "scip_indexer.INDEXERS",
                    {
                        "python": mock_py_indexer,
                        "typescript": mock_ts_indexer,
                        "javascript": mock_ts_indexer,
                    },
                ),
                patch.dict(
                    "scip_indexer.DEP_RESOLVERS",
                    {
                        "python": mock_py_deps,
                        "typescript": mock_ts_deps,
                        "javascript": mock_ts_deps,
                    },
                ),
            ):
                report = index_repo(tmpdir, "HKUDS/Vibe-Trading")

            # Report itself: TypeScript succeeded, Python failed
            assert report.any_success
            assert "typescript" in report.successful_languages
            assert "python" not in report.successful_languages

            # The failed results are accessible for metrics surfacing
            failed = [r for r in report.results if not r.success]
            assert len(failed) == 1
            assert failed[0].language == "python"
            assert "map is not a function" in failed[0].error
