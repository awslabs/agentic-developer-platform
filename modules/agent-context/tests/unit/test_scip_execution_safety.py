"""Execution-safety tests for SCIP ingestion (#5614, older finding #4720).

A cloned repository is untrusted input. Finding #4720 reported that dependency
resolution executed build tooling authored by the ingested repository, giving any
repo author code execution inside the ingestion worker.

These tests deliberately call the REAL resolver and indexer functions. The
existing `test_scip_multi_language.py` patches `DEP_RESOLVERS`/`INDEXERS`, so the
vulnerable code never runs there — those tests pass on both the vulnerable and
the fixed code and are therefore not execution-safety evidence.

Fixture design: every fixture is hostile in SHAPE but inert in EFFECT. A fixture
"payload" only writes a marker file into the test's own temporary directory and
exits 0. We assert on the marker's absence (and on file permissions being
unchanged) rather than on the resolver's return value, because the pre-fix code
reported success whether or not the build tooling did anything.

Each test in TestNoRepositoryCodeExecution fails on the pre-fix implementation:
  - gradlew / mvnw / build.gradle  -> marker written, wrapper chmod'd to 0755
  - setup.py via `pip install -e .` -> marker written

TestPlantedExecutableInClone covers the follow-up bypass found in review of the
first fix: the venv lived at `<clone>/.scip-venv` (a path the repository can
commit) and `_index_python` prepended its `bin/` to PATH, so a committed
`scip-python` ran instead of the real indexer. Those tests fail on the
intermediate fix as well as on the original code.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml

# Add the ingestion image directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "images" / "ingestion"))

from scip_indexer import (  # noqa: E402
    DEP_RESOLVERS,
    INDEXERS,
    REFUSAL_PREFIX,
    _dep_status,
    _index_csharp,
    _index_java,
    _index_python,
    _index_ruby,
    _index_typescript,
    _resolve_csharp_deps,
    _resolve_java_deps,
    _resolve_python_deps,
    _resolve_ruby_deps,
    _resolve_tool,
    _resolve_typescript_deps,
    _safe_env,
    cleanup_indexing_artifacts,
    index_repo,
    is_refusal,
)

_MANIFESTS = Path(__file__).resolve().parents[2] / "manifests"

# A payload that records that it ran, then exits cleanly. Inert: it only touches
# a file inside the test's temp dir. {marker} is filled in per test.
_SH_PAYLOAD = '#!/bin/sh\necho ran > "{marker}"\nexit 0\n'
_PY_PAYLOAD = 'open(r"{marker}", "w").write("ran")\n'


def _write(path: str, content: str, mode: int = 0o644) -> str:
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, mode)
    return path


class TestNoRepositoryCodeExecution:
    """The real resolvers must not execute repository-authored code."""

    def test_gradlew_is_neither_chmodded_nor_executed(self):
        """#4720 core case: repo-supplied gradlew must not run (was chmod 755 + exec)."""
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "MARKER")
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            gradlew = _write(
                os.path.join(repo, "gradlew"), _SH_PAYLOAD.format(marker=marker), mode=0o644
            )
            _write(os.path.join(repo, "build.gradle"), "// build\n")

            ok, detail = _resolve_java_deps(repo)

            assert not os.path.exists(marker), "repository gradlew was EXECUTED (RCE, #4720)"
            mode = stat.S_IMODE(os.stat(gradlew).st_mode)
            assert not mode & stat.S_IXUSR, f"gradlew was made executable (mode {oct(mode)})"
            assert mode == 0o644, f"gradlew permissions were modified: {oct(mode)}"
            assert ok is False
            assert is_refusal(detail), detail

    def test_mvnw_is_neither_chmodded_nor_executed(self):
        """The Maven wrapper is repo-authored too and must not run."""
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "MARKER")
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            mvnw = _write(os.path.join(repo, "mvnw"), _SH_PAYLOAD.format(marker=marker), mode=0o644)
            _write(os.path.join(repo, "pom.xml"), "<project/>\n")

            ok, detail = _resolve_java_deps(repo)

            assert not os.path.exists(marker), "repository mvnw was EXECUTED (RCE, #4720)"
            assert stat.S_IMODE(os.stat(mvnw).st_mode) == 0o644, "mvnw permissions were modified"
            assert ok is False
            assert is_refusal(detail), detail

    def test_build_gradle_without_wrapper_is_not_built(self):
        """With no wrapper the old code ran system `gradle` on repo-authored build logic."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            # A build file that would fail loudly if Gradle ever evaluated it.
            _write(os.path.join(repo, "build.gradle.kts"), 'error("evaluated")\n')

            ok, detail = _resolve_java_deps(repo)

            assert ok is False
            assert is_refusal(detail), detail

    def test_setup_py_is_not_evaluated(self):
        """#4720: `pip install -e .` evaluated the repository's own setup.py."""
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "MARKER")
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            _write(os.path.join(repo, "setup.py"), _PY_PAYLOAD.format(marker=marker))

            ok, detail = _resolve_python_deps(repo)

            assert not os.path.exists(marker), "repository setup.py was EVALUATED (RCE, #4720)"
            assert ok is False
            assert is_refusal(detail), detail

    def test_pyproject_build_backend_is_not_invoked(self):
        """A PEP 517 build backend in pyproject.toml must not be invoked either."""
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "MARKER")
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            _write(
                os.path.join(repo, "pyproject.toml"),
                '[build-system]\nrequires = []\nbuild-backend = "evil"\n',
            )
            _write(os.path.join(repo, "setup.py"), _PY_PAYLOAD.format(marker=marker))

            ok, _ = _resolve_python_deps(repo)

            assert not os.path.exists(marker), "pyproject build backend executed repo code"
            assert ok is False

    def test_gemfile_is_not_evaluated(self):
        """A Gemfile is a Ruby program; bundler would evaluate it."""
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "MARKER")
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            _write(os.path.join(repo, "Gemfile"), f'File.write("{marker}", "ran")\n')

            ok, detail = _resolve_ruby_deps(repo)

            assert not os.path.exists(marker), "repository Gemfile was EVALUATED"
            assert ok is False
            assert is_refusal(detail), detail

    def test_csproj_is_not_restored(self):
        """`dotnet restore` evaluates repo-authored MSBuild targets."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            _write(os.path.join(repo, "app.csproj"), "<Project/>\n")

            ok, detail = _resolve_csharp_deps(repo)

            assert ok is False
            assert is_refusal(detail), detail

    def test_refusal_does_not_depend_on_tooling_absence(self):
        """Refusal must be a decision, not an accident of the tool being missing.

        With a PATH containing a fake `gradle`/`bundle`/`dotnet` that would write a
        marker, a refusing implementation still writes nothing. This distinguishes
        a real refusal from "FileNotFoundError made it look safe".
        """
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "MARKER")
            fakebin = os.path.join(tmp, "bin")
            os.makedirs(fakebin)
            for tool in ("gradle", "mvn", "bundle", "dotnet"):
                _write(os.path.join(fakebin, tool), _SH_PAYLOAD.format(marker=marker), mode=0o755)

            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            _write(os.path.join(repo, "build.gradle"), "// build\n")
            _write(os.path.join(repo, "pom.xml"), "<project/>\n")
            _write(os.path.join(repo, "Gemfile"), "# gems\n")
            _write(os.path.join(repo, "app.csproj"), "<Project/>\n")

            original_path = os.environ.get("PATH", "")
            os.environ["PATH"] = fakebin + os.pathsep + original_path
            try:
                _resolve_java_deps(repo)
                _resolve_ruby_deps(repo)
                _resolve_csharp_deps(repo)
            finally:
                os.environ["PATH"] = original_path

            assert not os.path.exists(marker), "build tooling was invoked despite being refusable"


class TestUnsafeIndexersRefuse:
    """Indexers that drive a real project build refuse alongside their resolvers."""

    def test_scip_java_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            scip_path, error = _index_java(tmp)
            assert scip_path is None
            assert is_refusal(error), error

    def test_scip_dotnet_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            scip_path, error = _index_csharp(tmp)
            assert scip_path is None
            assert is_refusal(error), error

    def test_no_index_scip_written_by_refusing_indexer(self):
        """A refusal must not leave a bogus index behind."""
        with tempfile.TemporaryDirectory() as tmp:
            _index_java(tmp)
            assert not os.path.exists(os.path.join(tmp, "index.scip"))


class TestPlantedExecutableInClone:
    """The clone is attacker-writable: nothing in it may become an executable.

    Reviewer reproduction for #5614 (blocking bypass on head 4a5d6417). The first
    fix left the Python venv at `<clone>/.scip-venv` and `_index_python`
    prepended its `bin/` to PATH. A repository that COMMITS
    `.scip-venv/bin/scip-python` therefore had that file executed in place of the
    real indexer. `python3 -m venv` over an existing directory keeps its
    contents, and a repo with no requirements file at all is enough.
    """

    def _repo_with_planted_scip_python(self, tmp: str, marker: str) -> str:
        repo = os.path.join(tmp, "repo")
        venv_bin = os.path.join(repo, ".scip-venv", "bin")
        os.makedirs(venv_bin)
        _write(os.path.join(repo, "example.py"), "def hello():\n    return 1\n")
        _write(os.path.join(venv_bin, "scip-python"), _SH_PAYLOAD.format(marker=marker), mode=0o755)
        return repo

    def test_planted_scip_python_is_not_executed_through_index_repo(self):
        """The reviewer's reproduction, driven through the REAL orchestrator."""
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "MARKER")
            repo = self._repo_with_planted_scip_python(tmp, marker)

            report = index_repo(repo, "fixture/repo", languages=["python"])

            assert not os.path.exists(marker), (
                "repository-planted .scip-venv/bin/scip-python was EXECUTED (#5614 bypass)"
            )
            result = report.results[0]
            assert result.success is False
            assert is_refusal(result.error), result.error

    def test_planted_venv_makes_python_indexer_refuse(self):
        """Refusal happens at the indexer, with a reason naming the planted dir."""
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "MARKER")
            repo = self._repo_with_planted_scip_python(tmp, marker)

            scip_path, error = _index_python(repo)

            assert scip_path is None
            assert is_refusal(error), error
            assert ".scip-venv" in error
            assert not os.path.exists(marker)

    def test_committed_dot_venv_is_also_refused(self):
        """`.venv`/`venv` are interpreter dirs too, not just our old `.scip-venv`."""
        for planted in (".venv", "venv"):
            with tempfile.TemporaryDirectory() as tmp:
                repo = os.path.join(tmp, "repo")
                os.makedirs(os.path.join(repo, planted, "bin"))
                _write(os.path.join(repo, "app.py"), "x = 1\n")

                scip_path, error = _index_python(repo)

                assert scip_path is None, f"{planted} was used"
                assert is_refusal(error), error

    def test_no_venv_is_created_inside_the_clone(self):
        """We must not create a path in the clone that a repo can pre-seed."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            _write(os.path.join(repo, "requirements.txt"), "requests==2.31.0\n")

            _resolve_python_deps(repo)

            assert not os.path.exists(os.path.join(repo, ".scip-venv")), (
                "a venv was created inside the attacker-writable clone (#5614)"
            )
            assert os.listdir(repo) == ["requirements.txt"], "clone was mutated"

    def test_planted_node_modules_refuses_typescript_deps(self):
        """A committed node_modules supplies code scip-typescript would load."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(os.path.join(repo, "node_modules"))
            _write(os.path.join(repo, "package.json"), '{"name":"x"}\n')

            ok, detail = _resolve_typescript_deps(repo)

            assert ok is False
            assert is_refusal(detail), detail
            assert "node_modules" in detail

    def test_planted_vendor_bundle_refuses_ruby_indexer(self):
        """A committed vendor/bundle supplies gem code to Sorbet."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(os.path.join(repo, "vendor", "bundle"))
            _write(os.path.join(repo, "app.rb"), "puts 1\n")

            scip_path, error = _index_ruby(repo)

            assert scip_path is None
            assert is_refusal(error), error

    def test_sorbet_config_refuses_ruby_indexer(self):
        """sorbet/config can pass plugin options naming an executable to run."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(os.path.join(repo, "sorbet"))
            _write(os.path.join(repo, "sorbet", "config"), "--dsl-plugin-config=evil.yaml\n")

            scip_path, error = _index_ruby(repo)

            assert scip_path is None
            assert is_refusal(error), error
            assert "sorbet/config" in error


class TestPyrightConfigCannotRedirectLoading:
    """A repo-supplied pyright config names what pyright runs and imports."""

    def test_pyrightconfig_python_path_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            _write(
                os.path.join(repo, "pyrightconfig.json"),
                '{"pythonPath": "./evil/python"}\n',
            )

            scip_path, error = _index_python(repo)

            assert scip_path is None
            assert is_refusal(error), error
            assert "pythonPath" in error

    def test_pyproject_tool_pyright_venv_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            _write(
                os.path.join(repo, "pyproject.toml"),
                '[tool.pyright]\nvenvPath = "."\nvenv = "evil"\n',
            )

            scip_path, error = _index_python(repo)

            assert scip_path is None
            assert is_refusal(error), error

    def test_malformed_pyrightconfig_refuses_rather_than_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            _write(os.path.join(repo, "pyrightconfig.json"), "{not json\n")

            scip_path, error = _index_python(repo)

            assert scip_path is None
            assert is_refusal(error), error

    def test_ordinary_pyproject_without_pyright_overrides_is_allowed(self):
        """A normal pyproject.toml must NOT trigger a refusal (no over-blocking)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            _write(
                os.path.join(repo, "pyproject.toml"),
                '[project]\nname = "x"\n\n[tool.pyright]\nstrict = []\n',
            )

            scip_path, error = _index_python(repo)

            # scip-python is absent in the test env, so we expect the plain
            # not-found error — crucially NOT a refusal.
            assert not is_refusal(error), f"over-blocked an ordinary repo: {error}"


class TestSubprocessEnvironmentIsScrubbed:
    """Indexers must not inherit loader/interpreter settings pointing into the clone."""

    def test_clone_paths_are_removed_from_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(os.path.join(repo, "bin"))
            original = os.environ.get("PATH", "")
            os.environ["PATH"] = os.pathsep.join(
                [os.path.join(repo, "bin"), repo, "", ".", "/usr/bin"]
            )
            try:
                env = _safe_env(repo)
            finally:
                os.environ["PATH"] = original

            entries = env["PATH"].split(os.pathsep)
            assert entries == ["/usr/bin"], entries

    def test_loader_and_interpreter_vars_are_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            dangerous = {
                "PYTHONPATH": repo,
                "PYTHONSTARTUP": os.path.join(repo, "s.py"),
                "VIRTUAL_ENV": repo,
                "NODE_PATH": repo,
                "RUBYOPT": "-rEvil",
                "GEM_HOME": repo,
                "BUNDLE_GEMFILE": os.path.join(repo, "Gemfile"),
                "LD_PRELOAD": os.path.join(repo, "evil.so"),
            }
            saved = {k: os.environ.get(k) for k in dangerous}
            os.environ.update(dangerous)
            try:
                env = _safe_env(repo)
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

            for key in dangerous:
                assert key not in env, f"{key} leaked into the indexer environment"
            assert env["CGO_ENABLED"] == "0", "cgo would hand repo flags to a C compiler"

    def test_node_options_is_operator_config_not_a_repo_input(self):
        """NODE_OPTIONS is intentionally preserved, unlike the loader vars above.

        It is read from our own process environment, which the repository cannot
        write, and #3149 uses it to set the indexer heap size (with an operator
        override). Scrubbing it would drop operator configuration without taking
        away any capability the repository could reach.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            with patch.dict(os.environ, {"NODE_OPTIONS": "--max-old-space-size=8192"}):
                env = _safe_env(repo)
            assert env["NODE_OPTIONS"] == "--max-old-space-size=8192"

    def test_resolve_tool_rejects_a_binary_inside_the_clone(self):
        """Even on PATH, a clone-resident binary must never be selected."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            fake_bin = os.path.join(repo, "bin")
            os.makedirs(fake_bin)
            _write(os.path.join(fake_bin, "scip-python"), "#!/bin/sh\nexit 0\n", mode=0o755)

            original = os.environ.get("PATH", "")
            os.environ["PATH"] = fake_bin + os.pathsep + original
            try:
                assert _resolve_tool("scip-python", repo) is None
            finally:
                os.environ["PATH"] = original

    def test_resolve_tool_returns_absolute_trusted_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolved = _resolve_tool("sh", tmp)
            assert resolved is not None and os.path.isabs(resolved)


class TestNpmSourceIsPinned:
    """npm must not take its package source from a repository-committed .npmrc."""

    def test_registry_is_pinned_on_the_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            _write(os.path.join(repo, "package.json"), '{"name":"x"}\n')
            _write(os.path.join(repo, ".npmrc"), "registry=https://evil.invalid/\n")

            captured: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                captured.append(cmd)
                raise FileNotFoundError("npm absent in test env")

            with (
                patch("scip_indexer._resolve_tool", return_value="/trusted/bin/npm"),
                patch("subprocess.run", side_effect=fake_run),
            ):
                _resolve_typescript_deps(repo)

            assert captured, "npm was never invoked"
            cmd = captured[0]
            assert "--ignore-scripts" in cmd
            assert "--registry" in cmd
            registry = cmd[cmd.index("--registry") + 1]
            assert "evil.invalid" not in registry, "repo .npmrc chose the package source"


class TestCleanupRemovesPlantedToolDirs:
    """Cleanup must remove a repository-committed tool dir, not just ours."""

    def test_committed_scip_venv_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            planted = os.path.join(tmp, ".scip-venv", "bin")
            os.makedirs(planted)
            _write(os.path.join(planted, "scip-python"), "#!/bin/sh\nexit 0\n", mode=0o755)

            cleanup_indexing_artifacts(tmp)

            assert not os.path.exists(os.path.join(tmp, ".scip-venv"))

    def test_committed_environment_json_file_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, ".scip-environment.json"), "[]\n")

            cleanup_indexing_artifacts(tmp)

            assert not os.path.exists(os.path.join(tmp, ".scip-environment.json"))


class TestSafeFallbackPreserved:
    """Refusal must degrade indexing precision, not remove ingestion."""

    def test_safe_languages_still_have_resolvers(self):
        """Every language keeps a resolver entry (a refusing one still reports why)."""
        for lang in ("python", "typescript", "javascript", "go"):
            assert lang in DEP_RESOLVERS, f"{lang} lost its resolver"

    def test_static_indexers_are_preserved(self):
        """The statically-parsing indexers must remain available."""
        for lang in ("python", "typescript", "javascript", "go", "ruby"):
            assert lang in INDEXERS, f"{lang} lost its indexer"

    def test_npm_install_ignores_lifecycle_scripts(self):
        """npm lifecycle hooks are arbitrary commands; --ignore-scripts must stay."""
        source = (
            Path(__file__).resolve().parents[2] / "images" / "ingestion" / "scip_indexer.py"
        ).read_text()
        npm_call = source.split("def _resolve_typescript_deps")[1].split("def ")[0]
        assert "--ignore-scripts" in npm_call, "npm lifecycle scripts would execute repo code"

    def test_no_editable_install_remains_in_source(self):
        """Guard against the #4720 pattern being reintroduced."""
        source = (
            Path(__file__).resolve().parents[2] / "images" / "ingestion" / "scip_indexer.py"
        ).read_text()
        assert '"-e", "."' not in source, "`pip install -e .` reintroduced (#4720)"
        assert "os.chmod(gradlew" not in source, "gradlew chmod reintroduced (#4720)"
        assert "os.chmod(mvnw" not in source, "mvnw chmod reintroduced (#4720)"

    def test_no_clone_path_is_prepended_to_path(self):
        """Guard against the #5614 bypass: never put a clone dir on PATH."""
        source = (
            Path(__file__).resolve().parents[2] / "images" / "ingestion" / "scip_indexer.py"
        ).read_text()
        assert 'proc_env["PATH"] = venv_bin' not in source, (
            "clone venv bin re-added to PATH (#5614 bypass)"
        )
        assert 'proc_env["VIRTUAL_ENV"] = venv_path' not in source, (
            "VIRTUAL_ENV re-pointed at a clone path (#5614 bypass)"
        )

    def test_refusal_is_reported_distinctly_from_failure(self):
        """Operators must be able to tell a security refusal from broken tooling."""
        assert _dep_status(False, f"{REFUSAL_PREFIX}: nope") == "refused"
        assert _dep_status(False, "gradle not found") == "failed"
        assert _dep_status(True, "installed wheels") == "ok"

    def test_is_refusal_rejects_unrelated_details(self):
        assert not is_refusal(None)
        assert not is_refusal("")
        assert not is_refusal("npm install failed: timeout")


class TestEndToEndFailSoft:
    """A refused language must not abort ingestion of the rest of the repository."""

    def test_refused_java_does_not_block_python_indexing(self):
        """Mixed Java+Python repo: Python still indexes, Java reports refused."""
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "MARKER")
            _write(os.path.join(tmp, "gradlew"), _SH_PAYLOAD.format(marker=marker), mode=0o644)
            _write(os.path.join(tmp, "build.gradle"), "// build\n")

            def fake_python_indexer(clone_path):
                return _write(os.path.join(clone_path, "index.scip"), "scip"), None

            with patch.dict(INDEXERS, {"python": fake_python_indexer}):
                report = index_repo(tmp, "org/mixed", languages=["java", "python"])

            by_lang = {r.language: r for r in report.results}
            assert by_lang["python"].success, "safe language lost its index"
            assert by_lang["java"].success is False
            assert by_lang["java"].dep_resolution == "refused"
            assert is_refusal(by_lang["java"].error), by_lang["java"].error
            assert not os.path.exists(marker), "gradlew executed through index_repo"

    def test_refused_language_reports_refused_not_failed(self):
        """Ruby is refused at dep resolution but its static indexer still runs."""
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "MARKER")
            _write(os.path.join(tmp, "Gemfile"), f'File.write("{marker}", "ran")\n')

            def fake_ruby_indexer(clone_path):
                return _write(os.path.join(clone_path, "index.scip"), "scip"), None

            with patch.dict(INDEXERS, {"ruby": fake_ruby_indexer}):
                report = index_repo(tmp, "org/ruby", languages=["ruby"])

            result = report.results[0]
            assert result.dep_resolution == "refused", result.dep_resolution
            assert result.success, "static indexing should still proceed"
            assert not os.path.exists(marker), "Gemfile was evaluated"

    def test_ordinary_python_repo_still_indexes_with_refused_deps(self):
        """Positive path: a clean Python repo indexes; only dep precision degrades."""
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "app.py"), "def f():\n    return 1\n")
            _write(os.path.join(tmp, "requirements.txt"), "requests==2.31.0\n")

            def fake_python_indexer(clone_path):
                return _write(os.path.join(clone_path, "index.scip"), "scip"), None

            with patch.dict(INDEXERS, {"python": fake_python_indexer}):
                report = index_repo(tmp, "org/py", languages=["python"])

            result = report.results[0]
            assert result.success, "a clean Python repo must still produce an index"
            assert result.dep_resolution == "refused"
            assert report.combined_scip_path is not None

    def test_ordinary_typescript_repo_reaches_its_indexer(self):
        """A clean TS repo (no planted node_modules) is not refused at the indexer."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            _write(os.path.join(repo, "index.ts"), "export const x = 1\n")
            _write(os.path.join(repo, "package.json"), '{"name":"x"}\n')

            _, error = _index_typescript(repo)

            # scip-typescript is absent in the test env; the point is that we get
            # the plain not-found error rather than a security refusal.
            assert not is_refusal(error), f"over-blocked a clean TS repo: {error}"


class TestIngestionWorkerContainment:
    """Defence in depth: the worker pod should hold no privilege it does not need.

    This is a manifest assertion only. It proves what the repository declares,
    NOT what a live cluster is running — applying it is a separate deploy step.
    """

    def _worker_container(self) -> dict:
        raw = (_MANIFESTS / "ingestion-scaledjob.yaml").read_text()
        # Substitute ${VAR} template placeholders so the template parses as YAML.
        docs = [d for d in yaml.safe_load_all(re.sub(r"\$\{(\w+)\}", r"ph-\1", raw)) if d]
        scaled_job = next(d for d in docs if d.get("kind") == "ScaledJob")
        containers = scaled_job["spec"]["jobTargetRef"]["template"]["spec"]["containers"]
        return next(c for c in containers if c["name"] == "worker")

    def test_worker_declares_a_security_context(self):
        assert "securityContext" in self._worker_container(), (
            "ingestion worker has no securityContext (#5614 containment)"
        )

    def test_worker_cannot_escalate_privileges(self):
        sc = self._worker_container()["securityContext"]
        assert sc.get("allowPrivilegeEscalation") is False
        assert sc.get("privileged") is False

    def test_worker_drops_all_capabilities(self):
        sc = self._worker_container()["securityContext"]
        assert sc.get("capabilities", {}).get("drop") == ["ALL"]


class TestOrchestratedBoundary:
    def test_refused_node_modules_never_reaches_indexer(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name":"fixture"}')
        (tmp_path / "node_modules").mkdir()
        with patch.dict(
            INDEXERS,
            {
                "typescript": lambda _: (_ for _ in ()).throw(
                    AssertionError("refused repository reached indexer")
                )
            },
        ):
            report = index_repo(str(tmp_path), "fixture/repo", ["typescript"])
        assert not report.any_success
        assert is_refusal(report.results[0].error)

    def test_symlink_output_cannot_write_outside_clone(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "outside"
        outside.write_text("unchanged")
        (repo / "index.scip").symlink_to(outside)

        def write_index(clone):
            output = Path(clone) / "index.scip"
            output.write_text("index")
            return str(output), None

        with patch.dict(INDEXERS, {"python": write_index}):
            report = index_repo(str(repo), "fixture/repo", ["python"])
        assert outside.read_text() == "unchanged"
        assert not report.any_success
        assert is_refusal(report.results[0].error)

    def test_inherited_pyright_config_is_refused(self, tmp_path):
        (tmp_path / "pyrightconfig.json").write_text('{"extends":"base.json"}')
        (tmp_path / "base.json").write_text('{"venvPath":".","venv":"custom"}')
        with patch("scip_indexer._resolve_tool", side_effect=AssertionError("tool lookup")):
            _, error = _index_python(str(tmp_path))
        assert is_refusal(error)
