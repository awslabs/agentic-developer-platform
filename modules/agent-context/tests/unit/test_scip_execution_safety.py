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
    _is_unsafe_requirement,
    _resolve_csharp_deps,
    _resolve_java_deps,
    _resolve_python_deps,
    _resolve_ruby_deps,
    _sanitize_requirements,
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
            assert "declarative" in detail

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


class TestRequirementsSanitizer:
    """requirements.txt must not smuggle execution or repoint package resolution."""

    def test_editable_and_local_path_entries_are_unsafe(self):
        for line in ("-e .", "--editable .", ".", "./pkg", "../sibling", "/abs/pkg"):
            assert _is_unsafe_requirement(line), f"should be refused: {line!r}"

    def test_vcs_and_url_entries_are_unsafe(self):
        for line in (
            "git+https://example.invalid/x.git",
            "pkg @ git+https://example.invalid/x.git",
            "https://example.invalid/x.tar.gz",
            "file:///tmp/x",
        ):
            assert _is_unsafe_requirement(line), f"should be refused: {line!r}"

    def test_index_repointing_options_are_unsafe(self):
        """Repointing the index bypasses the source-admission boundary (PR #5790)."""
        for line in (
            "--index-url https://evil.invalid/simple",
            "--extra-index-url=https://evil.invalid/simple",
            "-i https://evil.invalid/simple",
            "--find-links /tmp/wheels",
            "--no-binary :all:",
        ):
            assert _is_unsafe_requirement(line), f"should be refused: {line!r}"

    def test_ordinary_pinned_requirements_are_kept(self):
        for line in ("requests==2.31.0", "flask>=2,<3", "pkg[extra]==1.0", "", "# comment"):
            assert not _is_unsafe_requirement(line), f"should be kept: {line!r}"

    def test_package_named_like_an_option_is_not_misparsed(self):
        """A package whose name starts with a kept option's letters stays kept."""
        for line in ("requests==2.31.0", "editable-tools==1.0", "invoke==2.0"):
            assert not _is_unsafe_requirement(line)

    def test_sanitized_file_drops_unsafe_lines_and_keeps_safe_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            venv = os.path.join(tmp, "venv")
            os.makedirs(venv)
            req = _write(
                os.path.join(tmp, "requirements.txt"),
                "requests==2.31.0\n-e .\n--index-url https://evil.invalid/s\nflask==3.0.0\n",
            )

            sanitized, dropped = _sanitize_requirements(req, venv)

            assert dropped == 2
            content = Path(sanitized).read_text()
            assert "requests==2.31.0" in content
            assert "flask==3.0.0" in content
            assert "-e ." not in content
            assert "evil.invalid" not in content

    def test_sanitized_copy_is_written_outside_the_clone(self):
        """We must never mutate the ingested repository."""
        with tempfile.TemporaryDirectory() as tmp:
            venv = os.path.join(tmp, "venv")
            repo = os.path.join(tmp, "repo")
            os.makedirs(venv)
            os.makedirs(repo)
            req = _write(os.path.join(repo, "requirements.txt"), "requests==2.31.0\n")
            original = Path(req).read_text()

            sanitized, _ = _sanitize_requirements(req, venv)

            assert not sanitized.startswith(repo), "sanitized copy written inside the clone"
            assert Path(req).read_text() == original, "original requirements.txt was modified"


class TestSafeFallbackPreserved:
    """Refusal must degrade indexing precision, not remove ingestion."""

    def test_safe_languages_still_have_resolvers(self):
        """Go/TypeScript resolution uses registry metadata, not repo build logic."""
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
