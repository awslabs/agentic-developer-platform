"""Multi-language SCIP indexing orchestrator.

Detects languages in a repository, resolves dependencies per language,
and runs the appropriate scip-<lang> indexer to produce a .scip file.

Supported languages (Phase 1 + Phase 2):
  - Python: scip-python (npm @sourcegraph/scip-python)
  - TypeScript/JavaScript: scip-typescript (npm @sourcegraph/scip-typescript)
  - Go: scip-go (go install)
  - Ruby: scip-ruby (native binary)
  - C#: scip-dotnet (dotnet tool)

Deferred (not in current corpus — D7):
  - Java/Kotlin/Scala: scip-java (JVM binary, not npm)

Design points:
  - Dep resolution is MANDATORY — without it monikers degrade to `local`
  - scip-python is npm (`@sourcegraph/scip-python`), NOT pip
  - scip-python --environment takes a JSON file, not a venv dir (EISDIR if dir)
  - Fail-loud: code-bearing repo with 0 edges → ERROR
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("scip_indexer")

# Language file extensions for detection
LANG_EXTENSIONS: dict[str, list[str]] = {
    "python": [".py"],
    "typescript": [".ts", ".tsx"],
    "javascript": [".js", ".jsx"],
    "go": [".go"],
    "java": [".java"],
    "kotlin": [".kt", ".kts"],
    "scala": [".scala"],
    "ruby": [".rb"],
    "csharp": [".cs"],
}

# Directories to skip during language detection
SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "vendor",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        "target",
        ".gradle",
        "bin",
        "obj",
        ".next",
        ".nuxt",
    }
)

# Minimum file count to consider a language "present"
MIN_FILES_FOR_LANG = 1


@dataclass
class IndexResult:
    """Result of indexing a single language in a repo."""

    language: str
    scip_path: str | None = None  # Path to generated .scip file
    success: bool = False
    dep_resolution: str = "skipped"  # ok, failed, skipped
    error: str | None = None
    file_count: int = 0


@dataclass
class IndexingReport:
    """Full indexing report for a repository."""

    repo: str
    languages_detected: list[str] = field(default_factory=list)
    results: list[IndexResult] = field(default_factory=list)
    combined_scip_path: str | None = None  # Final merged .scip if multiple

    @property
    def any_success(self) -> bool:
        return any(r.success for r in self.results)

    @property
    def successful_languages(self) -> list[str]:
        return [r.language for r in self.results if r.success]


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------


def detect_languages(clone_path: str) -> dict[str, int]:
    """Detect programming languages in a repository by file extension.

    Returns a dict of language → file count, sorted by count descending.
    Only includes languages with >= MIN_FILES_FOR_LANG files.
    """
    counts: dict[str, int] = {}

    for root, dirs, files in os.walk(clone_path):
        # Prune skip directories
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for f in files:
            ext = Path(f).suffix.lower()
            for lang, extensions in LANG_EXTENSIONS.items():
                if ext in extensions:
                    counts[lang] = counts.get(lang, 0) + 1
                    break

    # Filter to languages with minimum file count
    result = {lang: count for lang, count in counts.items() if count >= MIN_FILES_FOR_LANG}
    # Sort by count descending
    return dict(sorted(result.items(), key=lambda x: -x[1]))


# ---------------------------------------------------------------------------
# Per-language dependency resolution
# ---------------------------------------------------------------------------


def _resolve_python_deps(clone_path: str) -> tuple[bool, str]:
    """Resolve Python dependencies: create venv + install from requirements/setup.

    Returns (success, detail_message).
    """
    venv_path = os.path.join(clone_path, ".scip-venv")

    # Create virtualenv
    try:
        subprocess.run(
            ["python3", "-m", "venv", venv_path],
            capture_output=True,
            timeout=60,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return False, f"venv creation failed: {e}"

    pip = os.path.join(venv_path, "bin", "pip")

    # Try requirements.txt first
    req_file = None
    for candidate in ["requirements.txt", "requirements/base.txt", "requirements/dev.txt"]:
        path = os.path.join(clone_path, candidate)
        if os.path.isfile(path):
            req_file = path
            break

    if req_file:
        try:
            subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
                [pip, "install", "-r", req_file, "--quiet", "--no-warn-script-location"],
                capture_output=True,
                timeout=300,
                cwd=clone_path,
            )
            return True, f"installed from {os.path.basename(req_file)}"
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log.warning("pip install from %s failed: %s", req_file, e)

    # Try setup.py / pyproject.toml
    if os.path.isfile(os.path.join(clone_path, "pyproject.toml")) or os.path.isfile(
        os.path.join(clone_path, "setup.py")
    ):
        try:
            subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
                [pip, "install", "-e", ".", "--quiet", "--no-warn-script-location"],
                capture_output=True,
                timeout=300,
                cwd=clone_path,
            )
            return True, "installed from project root"
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log.warning("pip install -e . failed: %s", e)

    # No deps found — still try (moniker quality may degrade)
    return False, "no requirements.txt or pyproject.toml found"


def _resolve_typescript_deps(clone_path: str) -> tuple[bool, str]:
    """Resolve TypeScript/JavaScript dependencies: npm install.

    Returns (success, detail_message).
    """
    package_json = os.path.join(clone_path, "package.json")
    if not os.path.isfile(package_json):
        return False, "no package.json found"

    try:
        subprocess.run(
            ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"],
            capture_output=True,
            timeout=300,
            cwd=clone_path,
            check=True,
        )
        return True, "npm install succeeded"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, f"npm install failed: {e}"


def _resolve_go_deps(clone_path: str) -> tuple[bool, str]:
    """Resolve Go dependencies: go mod download.

    Returns (success, detail_message).
    """
    go_mod = os.path.join(clone_path, "go.mod")
    if not os.path.isfile(go_mod):
        return False, "no go.mod found"

    try:
        subprocess.run(
            ["go", "mod", "download"],
            capture_output=True,
            timeout=300,
            cwd=clone_path,
            check=True,
        )
        return True, "go mod download succeeded"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, f"go mod download failed: {e}"


def _resolve_java_deps(clone_path: str) -> tuple[bool, str]:
    """Resolve Java/Kotlin/Scala dependencies via Gradle or Maven.

    Returns (success, detail_message).
    """
    # Try Gradle first
    gradlew = os.path.join(clone_path, "gradlew")
    build_gradle = os.path.join(clone_path, "build.gradle")
    build_gradle_kts = os.path.join(clone_path, "build.gradle.kts")

    if os.path.isfile(gradlew) or os.path.isfile(build_gradle) or os.path.isfile(build_gradle_kts):
        gradle_cmd = gradlew if os.path.isfile(gradlew) else "gradle"
        if os.path.isfile(gradlew):
            os.chmod(gradlew, 0o755)
        try:
            subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
                [gradle_cmd, "dependencies", "--no-daemon", "-q"],
                capture_output=True,
                timeout=600,
                cwd=clone_path,
                check=True,
            )
            return True, "gradle dependencies resolved"
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            log.warning("Gradle dep resolution failed: %s", e)

    # Try Maven
    pom_xml = os.path.join(clone_path, "pom.xml")
    if os.path.isfile(pom_xml):
        mvnw = os.path.join(clone_path, "mvnw")
        maven_cmd = mvnw if os.path.isfile(mvnw) else "mvn"
        if os.path.isfile(mvnw):
            os.chmod(mvnw, 0o755)
        try:
            subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
                [maven_cmd, "dependency:resolve", "-q"],
                capture_output=True,
                timeout=600,
                cwd=clone_path,
                check=True,
            )
            return True, "maven dependencies resolved"
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            return False, f"maven dependency:resolve failed: {e}"

    return False, "no build.gradle or pom.xml found"


def _resolve_ruby_deps(clone_path: str) -> tuple[bool, str]:
    """Resolve Ruby dependencies: bundle install (best-effort for Sorbet).

    Returns (success, detail_message).
    """
    gemfile = os.path.join(clone_path, "Gemfile")
    if not os.path.isfile(gemfile):
        return False, "no Gemfile found"

    try:
        subprocess.run(
            ["bundle", "install", "--quiet", "--jobs=4"],
            capture_output=True,
            timeout=300,
            cwd=clone_path,
            check=True,
        )
        return True, "bundle install succeeded"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, f"bundle install failed: {e}"


def _resolve_csharp_deps(clone_path: str) -> tuple[bool, str]:
    """Resolve C# dependencies: dotnet restore.

    Returns (success, detail_message).
    """
    # Look for .sln or .csproj
    has_project = False
    for ext in [".sln", ".csproj"]:
        for f in Path(clone_path).rglob(f"*{ext}"):
            has_project = True
            break
        if has_project:
            break

    if not has_project:
        return False, "no .sln or .csproj found"

    try:
        subprocess.run(
            ["dotnet", "restore", "--verbosity", "quiet"],
            capture_output=True,
            timeout=300,
            cwd=clone_path,
            check=True,
        )
        return True, "dotnet restore succeeded"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, f"dotnet restore failed: {e}"


# Dispatch table: language → dep resolution function
DEP_RESOLVERS: dict[str, callable] = {
    "python": _resolve_python_deps,
    "typescript": _resolve_typescript_deps,
    "javascript": _resolve_typescript_deps,  # Same npm-based resolution
    "go": _resolve_go_deps,
    "java": _resolve_java_deps,
    "kotlin": _resolve_java_deps,  # Same JVM build tools
    "scala": _resolve_java_deps,
    "ruby": _resolve_ruby_deps,
    "csharp": _resolve_csharp_deps,
}


# ---------------------------------------------------------------------------
# Per-language SCIP indexing
# ---------------------------------------------------------------------------


def _ensure_pyright_section(clone_path: str) -> None:
    """Ensure pyproject.toml has a [tool.pyright] section if it exists.

    scip-python (some versions) hard-fails when pyproject.toml is present but
    lacks [tool.pyright]. We append an empty section to the cloned copy (which
    is disposable — cleanup_indexing_artifacts handles the clone).
    """
    pyproject_path = os.path.join(clone_path, "pyproject.toml")
    if not os.path.isfile(pyproject_path):
        return

    try:
        with open(pyproject_path, "r") as f:
            content = f.read()
    except OSError:
        return

    # Check if [tool.pyright] already exists (case-sensitive, per TOML spec)
    if "[tool.pyright]" in content:
        return

    # Append an empty pyright section
    with open(pyproject_path, "a") as f:
        f.write("\n[tool.pyright]\n")
    log.info("Appended empty [tool.pyright] section to %s", pyproject_path)


def _index_python(clone_path: str) -> tuple[str | None, str | None]:
    """Run scip-python on a Python repo.

    Note: scip-python is an npm package (@sourcegraph/scip-python), NOT pip.
    Instead of passing --environment (which expects a JSON array of package
    entries that's coupled to scip-python internals), we put the venv's bin/
    on PATH so scip-python discovers packages via its own default flow.
    """
    scip_output = os.path.join(clone_path, "index.scip")

    # Ensure pyproject.toml has [tool.pyright] if it exists (scip-python needs it)
    _ensure_pyright_section(clone_path)

    # Build subprocess environment: put venv bin on PATH if venv exists,
    # letting scip-python discover packages via its default discovery flow.
    # This avoids the --environment JSON shape coupling that caused #3132.
    venv_path = os.path.join(clone_path, ".scip-venv")
    proc_env = os.environ.copy()
    if os.path.isdir(venv_path):
        venv_bin = os.path.join(venv_path, "bin")
        proc_env["PATH"] = venv_bin + ":" + proc_env.get("PATH", "")
        proc_env["VIRTUAL_ENV"] = venv_path

    # Raise Node.js heap limit for scip-python: the default ~2 GB max-old-space-size
    # causes OOM on large repos (e.g. 1,202-file Vibe-Trading dies at 1,989 MB during
    # "Parse and emit SCIP"). 4096 MB is comfortably above the observed death point
    # and below the worker pod memory limit. Use setdefault so operators can override
    # via pod env. (#3149)
    proc_env.setdefault("NODE_OPTIONS", "--max-old-space-size=4096")

    cmd = ["scip-python", "index", "--project-name", os.path.basename(clone_path)]
    cmd.extend(["--output", scip_output, clone_path])

    try:
        result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
            cmd,
            capture_output=True,
            timeout=1800,
            cwd=clone_path,
            env=proc_env,
        )
        if result.returncode == 0 and os.path.isfile(scip_output):
            return scip_output, None
        stderr = result.stderr.decode("utf-8", errors="replace")[:500]
        return None, f"scip-python exited {result.returncode}: {stderr}"
    except FileNotFoundError:
        return None, "scip-python not found in PATH"
    except subprocess.TimeoutExpired:
        return None, "scip-python timed out (1800s)"


def _index_typescript(clone_path: str) -> tuple[str | None, str | None]:
    """Run scip-typescript on a TypeScript/JavaScript repo.

    Uses --infer-tsconfig for JavaScript repos without tsconfig.json.
    """
    scip_output = os.path.join(clone_path, "index.scip")

    cmd = ["scip-typescript", "index"]

    # If no tsconfig.json, use --infer-tsconfig
    tsconfig = os.path.join(clone_path, "tsconfig.json")
    if not os.path.isfile(tsconfig):
        cmd.append("--infer-tsconfig")

    cmd.extend(["--output", scip_output])

    try:
        result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
            cmd,
            capture_output=True,
            timeout=600,
            cwd=clone_path,
        )
        if result.returncode == 0 and os.path.isfile(scip_output):
            return scip_output, None
        stderr = result.stderr.decode("utf-8", errors="replace")[:500]
        return None, f"scip-typescript exited {result.returncode}: {stderr}"
    except FileNotFoundError:
        return None, "scip-typescript not found in PATH"
    except subprocess.TimeoutExpired:
        return None, "scip-typescript timed out (600s)"


def _index_go(clone_path: str) -> tuple[str | None, str | None]:
    """Run scip-go on a Go repo."""
    scip_output = os.path.join(clone_path, "index.scip")

    cmd = ["scip-go", "--output", scip_output]

    try:
        result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
            cmd,
            capture_output=True,
            timeout=600,
            cwd=clone_path,
        )
        if result.returncode == 0 and os.path.isfile(scip_output):
            return scip_output, None
        stderr = result.stderr.decode("utf-8", errors="replace")[:500]
        return None, f"scip-go exited {result.returncode}: {stderr}"
    except FileNotFoundError:
        return None, "scip-go not found in PATH"
    except subprocess.TimeoutExpired:
        return None, "scip-go timed out (600s)"


def _index_java(clone_path: str) -> tuple[str | None, str | None]:
    """Run scip-java on a Java/Kotlin/Scala repo."""
    scip_output = os.path.join(clone_path, "index.scip")

    cmd = ["scip-java", "index", "--output", scip_output]

    try:
        result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
            cmd,
            capture_output=True,
            timeout=900,
            cwd=clone_path,
        )
        if result.returncode == 0 and os.path.isfile(scip_output):
            return scip_output, None
        stderr = result.stderr.decode("utf-8", errors="replace")[:500]
        return None, f"scip-java exited {result.returncode}: {stderr}"
    except FileNotFoundError:
        return None, "scip-java not found in PATH"
    except subprocess.TimeoutExpired:
        return None, "scip-java timed out (900s)"


def _index_ruby(clone_path: str) -> tuple[str | None, str | None]:
    """Run scip-ruby on a Ruby repo (Sorbet-based, best-effort on untyped)."""
    scip_output = os.path.join(clone_path, "index.scip")

    cmd = ["scip-ruby", "--output", scip_output]

    try:
        result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
            cmd,
            capture_output=True,
            timeout=600,
            cwd=clone_path,
        )
        if result.returncode == 0 and os.path.isfile(scip_output):
            return scip_output, None
        stderr = result.stderr.decode("utf-8", errors="replace")[:500]
        return None, f"scip-ruby exited {result.returncode}: {stderr}"
    except FileNotFoundError:
        return None, "scip-ruby not found in PATH"
    except subprocess.TimeoutExpired:
        return None, "scip-ruby timed out (600s)"


def _index_csharp(clone_path: str) -> tuple[str | None, str | None]:
    """Run scip-dotnet on a C# repo."""
    scip_output = os.path.join(clone_path, "index.scip")

    cmd = ["scip-dotnet", "index", "--output", scip_output]

    try:
        result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
            cmd,
            capture_output=True,
            timeout=600,
            cwd=clone_path,
        )
        if result.returncode == 0 and os.path.isfile(scip_output):
            return scip_output, None
        stderr = result.stderr.decode("utf-8", errors="replace")[:500]
        return None, f"scip-dotnet exited {result.returncode}: {stderr}"
    except FileNotFoundError:
        return None, "scip-dotnet not found in PATH"
    except subprocess.TimeoutExpired:
        return None, "scip-dotnet timed out (600s)"


# Dispatch table: language → indexer function
INDEXERS: dict[str, callable] = {
    "python": _index_python,
    "typescript": _index_typescript,
    "javascript": _index_typescript,  # scip-typescript handles JS with --infer-tsconfig
    "go": _index_go,
    "java": _index_java,
    "kotlin": _index_java,  # scip-java handles Kotlin/Scala
    "scala": _index_java,
    "ruby": _index_ruby,
    "csharp": _index_csharp,
}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _consolidate_languages(detected: list[str]) -> list[str]:
    """Deduplicate language list by indexer family.

    JVM languages (java/kotlin/scala) share a single indexer ("java").
    TypeScript and JavaScript share a single indexer ("typescript" preferred).

    Returns a deduplicated list preserving order of first occurrence.
    """
    seen_indexers: set[str] = set()
    consolidated: list[str] = []
    jvm_langs = {"java", "kotlin", "scala"}

    for lang in detected:
        # Map to canonical indexer language
        if lang in jvm_langs:
            canonical = "java"
        elif lang == "javascript":
            canonical = "typescript"
        else:
            canonical = lang

        if canonical not in seen_indexers and canonical in INDEXERS:
            seen_indexers.add(canonical)
            consolidated.append(canonical)

    return consolidated


def index_repo(clone_path: str, repo: str, languages: list[str] | None = None) -> IndexingReport:
    """Run SCIP indexing for all detected (or specified) languages in a repo.

    Indexes ALL supported languages (not just the primary). For each language:
      1. Resolve dependencies (mandatory — monikers degrade without it)
      2. Run the scip-<lang> indexer
      3. Report success/failure

    Fail-soft: a language whose indexer errors or times out is logged and skipped;
    the repo still gets the languages that succeeded.

    Args:
        clone_path: Path to the cloned repository
        repo: Repository identifier (e.g., "org/repo-name")
        languages: Optional list of languages to index (auto-detected if None)

    Returns:
        IndexingReport with per-language results
    """
    report = IndexingReport(repo=repo)

    # Detect languages if not specified
    if languages is None:
        lang_counts = detect_languages(clone_path)
        report.languages_detected = list(lang_counts.keys())
        log.info("Detected languages in %s: %s", repo, lang_counts)
    else:
        report.languages_detected = languages

    if not report.languages_detected:
        log.warning("No supported languages detected in %s", repo)
        return report

    # Consolidate to unique indexer languages (e.g., TS+JS → typescript only)
    langs_to_index = _consolidate_languages(report.languages_detected)
    log.info("Languages to index for %s: %s", repo, langs_to_index)

    # Index each language independently (fail-soft per language)
    for lang in langs_to_index:
        # Step 1: Resolve dependencies
        dep_resolver = DEP_RESOLVERS.get(lang)
        dep_ok = False
        dep_detail = "no resolver"

        if dep_resolver:
            dep_ok, dep_detail = dep_resolver(clone_path)
            if dep_ok:
                log.info("Dep resolution for %s (%s): %s", repo, lang, dep_detail)
            else:
                log.warning(
                    "Dep resolution failed for %s (%s): %s — indexing anyway (degraded monikers)",
                    repo,
                    lang,
                    dep_detail,
                )

        # Step 2: Run indexer
        indexer = INDEXERS.get(lang)
        if not indexer:
            result = IndexResult(
                language=lang,
                error=f"No indexer available for {lang}",
                dep_resolution="ok" if dep_ok else "failed",
            )
            report.results.append(result)
            continue

        try:
            scip_path, error = indexer(clone_path)
        except Exception as e:
            log.error(
                "Indexer crashed for %s (%s): %s — skipping language",
                repo,
                lang,
                e,
            )
            result = IndexResult(
                language=lang,
                error=f"Indexer exception: {e}",
                dep_resolution="ok" if dep_ok else "failed",
            )
            report.results.append(result)
            continue

        # Rename index.scip to a per-language path so the next indexer doesn't
        # overwrite it. All _index_*() functions write to clone_path/index.scip.
        canonical_scip = os.path.join(clone_path, "index.scip")
        if scip_path and scip_path == canonical_scip and os.path.isfile(scip_path):
            unique_path = os.path.join(clone_path, f"index.{lang}.scip")
            os.rename(scip_path, unique_path)
            scip_path = unique_path

        result = IndexResult(
            language=lang,
            scip_path=scip_path,
            success=scip_path is not None,
            dep_resolution="ok" if dep_ok else "failed",
            error=error,
        )
        report.results.append(result)

        if scip_path:
            # combined_scip_path holds the first successful .scip (backward compat)
            if report.combined_scip_path is None:
                report.combined_scip_path = scip_path
            log.info("SCIP index produced for %s (%s): %s", repo, lang, scip_path)
        else:
            log.error("SCIP indexing failed for %s (%s): %s", repo, lang, error)

    return report


def cleanup_indexing_artifacts(clone_path: str) -> None:
    """Remove indexing artifacts (.scip-venv, node_modules added by us, etc.)."""
    venv_path = os.path.join(clone_path, ".scip-venv")
    if os.path.isdir(venv_path):
        shutil.rmtree(venv_path, ignore_errors=True)

    env_file = os.path.join(clone_path, ".scip-environment.json")
    if os.path.isfile(env_file):
        os.unlink(env_file)
