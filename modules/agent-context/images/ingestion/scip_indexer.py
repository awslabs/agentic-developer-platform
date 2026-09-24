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
  - Unsafe dependency resolution is refused; monikers may degrade to `local`
  - scip-python is npm (`@sourcegraph/scip-python`), NOT pip
  - scip-python --environment takes a JSON file, not a venv dir (EISDIR if dir)
  - Fail-loud: code-bearing repo with 0 edges → ERROR
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tomllib
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
    # ok | failed | refused | skipped. "refused" means we declined to run
    # repository-authored build logic (#5614) — distinct from a tooling failure.
    dep_resolution: str = "skipped"
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
# Execution-safety policy (#5614, older finding #4720)
# ---------------------------------------------------------------------------
#
# A cloned repository is UNTRUSTED INPUT, not a program to run. Any step that
# hands control to repository-authored content — a wrapper script (`gradlew`,
# `mvnw`), a build file evaluated as code (`setup.py`, `Gemfile`, MSBuild
# `.csproj` targets), or a source distribution compiled on the fly — lets the
# repository author execute code inside the ingestion worker with the worker's
# network reach and cloud identity.
#
# Rules enforced below:
#   1. A resolver/indexer that cannot avoid executing repository-authored code
#      REFUSES: it returns a refusal instead of running anything. The dangerous
#      invocation is deleted, not flag-guarded — there is no toggle that turns
#      execution back on.
#   2. Refusal is fail-soft by design. `index_repo` already treats unresolved
#      dependencies as "index anyway with degraded monikers", and refused
#      languages still receive the lexical/static ingestion stages (code search
#      and embeddings) which never execute repository content.
#   3. We never place a file WE control inside the clone, and never let a path
#      inside the clone reach a tool's executable/plugin lookup. The clone is a
#      directory the repository author fully controls; sharing a namespace with
#      it is what made the first fix incomplete (see index_repo / _safe_env).
#   4. A tool configuration file that lives in the repository and can name a
#      program, plugin or package source is itself repository-authored code
#      loading. Either the loading is disabled on the command line (which
#      overrides an in-repo config file) or the language refuses.
#
# Refusal detail strings are prefixed with REFUSAL_PREFIX so operators can tell
# a deliberate security refusal apart from a genuine tooling failure.

REFUSAL_PREFIX = "refused (untrusted-repo execution)"

# Languages whose dependency resolution requires executing repository-authored
# build logic. Value is the operator-facing reason.
_UNSAFE_DEP_RESOLUTION: dict[str, str] = {
    "java": "gradlew/mvnw wrapper scripts and build.gradle/pom.xml are repository-authored code",
    "ruby": "Gemfile is evaluated as Ruby and gem native extensions compile arbitrary code",
    "csharp": "dotnet restore evaluates repository-authored MSBuild targets and SDK resolvers",
    # Python: a wheel chosen by the repository's requirements file still runs
    # author-controlled code as soon as an interpreter starts with that
    # environment importable — .pth files in site-packages are executed at
    # startup, and scip-python starts exactly such an interpreter. Refusing only
    # sdist builds (`--only-binary`) does NOT close that, so there is no Python
    # dependency installation at all. Repo-internal symbols still resolve.
    "python": (
        "installing repository-chosen packages runs author-controlled import-time "
        "and .pth startup hooks inside the worker"
    ),
}

# Languages whose SCIP indexer drives a real project build (and therefore the
# repository's own build files) rather than statically parsing sources.
_UNSAFE_INDEXERS: dict[str, str] = {
    "java": "scip-java invokes Gradle/Maven on repository-authored build files",
    "csharp": "scip-dotnet invokes MSBuild on repository-authored project files",
}


# ---------------------------------------------------------------------------
# Clone-boundary helpers
# ---------------------------------------------------------------------------
#
# The clone is attacker-writable. Two consequences drive every helper here:
#
#   * Anything WE create must live outside the clone. The original fix created
#     a venv at `<clone>/.scip-venv`; a repository that commits files at that
#     path keeps them (venv creation over an existing directory preserves its
#     contents), and the indexer then prepended `<clone>/.scip-venv/bin` to
#     PATH — so a committed `scip-python` ran instead of the real indexer.
#   * Nothing inside the clone may reach an executable lookup. We resolve each
#     indexer to an absolute path ourselves and scrub PATH, so a planted file
#     cannot be selected even if one exists.

# Directory names an indexer/toolchain resolves executables or packages from. If
# the repository ships one, we cannot establish what the tool would load.
_PLANTABLE_TOOL_DIRS: dict[str, tuple[str, ...]] = {
    "python": (".scip-venv", ".venv", "venv"),
    "typescript": ("node_modules",),
    "ruby": ("vendor/bundle", ".bundle"),
}

# Repository-authored config files that can name a program, plugin or transform
# for the indexer to load. Presence => refuse that language's structural index.
_PLANTABLE_TOOL_CONFIGS: dict[str, tuple[str, ...]] = {
    # Sorbet reads --dir/--file and plugin options from a config file in the repo
    # and scip-ruby honours it; a plugin entry names an executable to run.
    "ruby": ("sorbet/config",),
}


def _safe_env(clone_path: str) -> dict[str, str]:
    """Build a subprocess environment that cannot resolve programs from the clone.

    Drops PATH entries that are relative, empty or inside the clone, so a file
    committed by the repository is never a candidate executable. Also clears the
    interpreter/tooling variables that would otherwise let in-repo content be
    loaded at process start.
    """
    env = os.environ.copy()

    clone_real = os.path.realpath(clone_path)
    kept: list[str] = []
    for entry in env.get("PATH", "").split(os.pathsep):
        if not entry:
            continue  # empty entry means "current directory"
        if not os.path.isabs(entry):
            continue
        real = os.path.realpath(entry)
        if real == clone_real or real.startswith(clone_real + os.sep):
            log.warning("Dropped PATH entry inside clone: %s", entry)
            continue
        kept.append(entry)
    env["PATH"] = os.pathsep.join(kept)

    # Never let the repository contribute importable/loadable content at startup.
    #
    # NODE_OPTIONS is deliberately NOT in this list. Every variable here is
    # cleared because a *path* it names could resolve into the clone; but these
    # variables are read from OUR process environment, which the repository
    # cannot write. NODE_OPTIONS is operator configuration (#3149 sets the
    # indexer heap size through it and allows an operator override), so clearing
    # it would drop a legitimate setting without removing any repository-reachable
    # capability. PATH is the one exception that is filtered rather than cleared,
    # because we still need the trusted entries.
    for var in (
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "NODE_PATH",
        "RUBYOPT",
        "RUBYLIB",
        "GEM_HOME",
        "GEM_PATH",
        "BUNDLE_GEMFILE",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
    ):
        env.pop(var, None)

    # Python must not import from the current working directory (the clone).
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONSAFEPATH"] = "1"
    # Go: `#cgo` directives in repository sources hand flags to a real compiler.
    env["CGO_ENABLED"] = "0"
    env["GOFLAGS"] = "-mod=mod"
    env["GOPROXY"] = env.get("GOPROXY", "https://proxy.golang.org,direct")
    return env


def _resolve_tool(name: str, clone_path: str) -> str | None:
    """Absolute path to a trusted indexer binary, or None if unavailable.

    Looked up against the scrubbed PATH so a binary planted in the clone can
    never satisfy the lookup.
    """
    env = _safe_env(clone_path)
    found = shutil.which(name, path=env.get("PATH", ""))
    if not found:
        return None
    real = os.path.realpath(found)
    clone_real = os.path.realpath(clone_path)
    if real == clone_real or real.startswith(clone_real + os.sep):
        log.error("Refusing %s: resolved inside the clone (%s)", name, real)
        return None
    return real


def _planted_tool_dir(clone_path: str, lang: str) -> str | None:
    """Return the first tool directory the repository itself shipped, if any."""
    for rel in _PLANTABLE_TOOL_DIRS.get(lang, ()):
        if os.path.isdir(os.path.join(clone_path, rel)):
            return rel
    return None


def _planted_tool_config(clone_path: str, lang: str) -> str | None:
    """Return the first indexer config file the repository shipped, if any."""
    for rel in _PLANTABLE_TOOL_CONFIGS.get(lang, ()):
        if os.path.exists(os.path.join(clone_path, rel)):
            return rel
    return None


# Package source for npm, pinned on the command line so a repository-committed
# .npmrc cannot repoint it (the source-admission boundary is PR #5790's).
NPM_REGISTRY = os.environ.get("SCIP_NPM_REGISTRY", "https://registry.npmjs.org/")


def _refuse_dep_resolution(lang: str) -> tuple[bool, str]:
    """Return a fail-soft refusal for a language we will not build. No execution."""
    reason = _UNSAFE_DEP_RESOLUTION.get(lang, "dependency resolution is not execution-safe")
    return _refuse_dep_resolution_reason(reason)


def _refuse_dep_resolution_reason(reason: str) -> tuple[bool, str]:
    """Return a fail-soft dep-resolution refusal with an explicit reason."""
    detail = f"{REFUSAL_PREFIX}: {reason}"
    log.warning("Dep resolution refused — %s", reason)
    return False, detail


def _refuse_indexer(lang: str) -> tuple[str | None, str | None]:
    """Return a fail-soft refusal for an indexer that would build the repo. No execution."""
    reason = _UNSAFE_INDEXERS.get(lang, "indexer is not execution-safe")
    return _refuse_indexer_reason(reason)


def _refuse_indexer_reason(reason: str) -> tuple[str | None, str | None]:
    """Return a fail-soft indexer refusal with an explicit reason."""
    detail = f"{REFUSAL_PREFIX}: {reason}"
    log.warning("SCIP indexing refused — %s", reason)
    return None, detail


def is_refusal(detail: str | None) -> bool:
    """True if a resolver/indexer detail string reports a deliberate safety refusal."""
    return bool(detail) and detail.startswith(REFUSAL_PREFIX)


def _dep_status(dep_ok: bool, dep_detail: str) -> str:
    """Map a resolver outcome to a reportable status.

    Surfaces "refused" separately from "failed" so an operator seeing degraded
    monikers can tell a deliberate security refusal from broken tooling.
    """
    if dep_ok:
        return "ok"
    return "refused" if is_refusal(dep_detail) else "failed"


# ---------------------------------------------------------------------------
# Per-language dependency resolution
# ---------------------------------------------------------------------------


def _resolve_python_deps(clone_path: str) -> tuple[bool, str]:
    """Refuse Python dependency installation — it runs author-controlled code.

    Finding #4720 was `pip install -e .`, which evaluates the ingested
    repository's own `setup.py`. Removing only that was not enough (#5614
    review):

      * The venv lived at `<clone>/.scip-venv`, a path the repository can
        commit. `python3 -m venv` over an existing directory keeps whatever is
        already there, and `_index_python` then prepended `<clone>/.scip-venv/bin`
        to PATH — so a committed `scip-python` ran instead of the real indexer.
      * Even with the venv relocated, installing a wheel named by the
        repository's requirements file executes author-controlled code: a `.pth`
        file dropped into site-packages runs at interpreter startup, and
        scip-python starts an interpreter with that environment active.
        `--only-binary :all:` prevents an sdist build, not this.

    We cannot establish that preparing Python dependencies is execution-safe in
    this worker, so we do not do it. Python is still indexed: symbols defined in
    the repository resolve normally and only cross-package references degrade to
    `local` monikers, which is the existing supported degraded mode. The
    lexical/static stages (code search, embeddings) are unaffected.
    """
    return _refuse_dep_resolution("python")


def _resolve_typescript_deps(clone_path: str) -> tuple[bool, str]:
    """Install npm dependencies without running any repository-authored code.

    Execution safety (#5614):
      * `--ignore-scripts` suppresses the lifecycle hooks (preinstall/install/
        postinstall) that npm packages conventionally use to run commands.
      * The registry is pinned on the command line, which takes precedence over
        an `.npmrc` committed in the repository. Without this, the repository
        chooses where packages are fetched from, bypassing the source-admission
        boundary owned by PR #5790.
      * A repository that ships its own `node_modules` is refused: npm would
        keep the existing tree, and scip-typescript loads code from it.
      * npm itself is resolved against a scrubbed PATH, so an `npm` committed in
        the clone is never selected.

    Unlike the JVM/Python cases this stays enabled: no repository-authored file
    is evaluated, only declarative `package.json` metadata is read, and the
    downloaded packages are never imported by the indexer (scip-typescript
    parses them, it does not execute them).
    """
    package_json = os.path.join(clone_path, "package.json")
    if not os.path.isfile(package_json):
        return False, "no package.json found"

    planted = _planted_tool_dir(clone_path, "typescript")
    if planted:
        return _refuse_dep_resolution_reason(
            f"repository ships its own {planted}/, which scip-typescript would load code from"
        )

    npm = _resolve_tool("npm", clone_path)
    if not npm:
        return False, "npm not found in PATH"

    try:
        subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
            [
                npm,
                "install",
                # Never run package lifecycle hooks — that is arbitrary execution.
                "--ignore-scripts",
                # Pin the source; overrides a repository-committed .npmrc.
                "--registry",
                NPM_REGISTRY,
                "--no-audit",
                "--no-fund",
            ],
            capture_output=True,
            timeout=300,
            cwd=clone_path,
            env=_safe_env(clone_path),
            check=True,
        )
        return True, "npm install succeeded (--ignore-scripts, pinned registry)"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, f"npm install failed: {e}"


def _resolve_go_deps(clone_path: str) -> tuple[bool, str]:
    """Download Go modules without compiling repository-authored code.

    Execution safety (#5614): `go mod download` fetches modules and does not run
    build logic — Go has no install hooks. Two controls matter anyway:
    `CGO_ENABLED=0` (via `_safe_env`) stops `#cgo` directives in repository
    sources from handing flags to a real C compiler, and the toolchain is pinned
    to the container's own version so a `go.mod` `toolchain` directive cannot
    make Go download and execute a different one.
    """
    go_mod = os.path.join(clone_path, "go.mod")
    if not os.path.isfile(go_mod):
        return False, "no go.mod found"

    go = _resolve_tool("go", clone_path)
    if not go:
        return False, "go not found in PATH"

    env = _safe_env(clone_path)
    # Refuse a repository-requested toolchain download (it would then be run).
    env["GOTOOLCHAIN"] = "local"

    try:
        subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
            [go, "mod", "download"],
            capture_output=True,
            timeout=300,
            cwd=clone_path,
            env=env,
            check=True,
        )
        return True, "go mod download succeeded (CGO disabled, local toolchain)"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, f"go mod download failed: {e}"


def _resolve_java_deps(clone_path: str) -> tuple[bool, str]:
    """Refuse JVM dependency resolution — it cannot avoid executing repo code.

    Finding #4720: the previous implementation took the repository's own
    `gradlew`/`mvnw`, chmod'd it to 0755 and executed it; with no wrapper it ran
    `gradle`/`mvn` against repository-authored `build.gradle`/`pom.xml`, which
    Gradle and Maven evaluate as build logic. Both are arbitrary code execution
    by the repository author. There is no "safe subset" of a Gradle build, so
    this refuses outright. JVM repositories still get lexical/static indexing.
    """
    return _refuse_dep_resolution("java")


def _resolve_ruby_deps(clone_path: str) -> tuple[bool, str]:
    """Refuse Ruby dependency resolution — bundler evaluates repo-authored code.

    A `Gemfile` is a Ruby program that bundler evaluates, and installing a gem
    with a native extension compiles and runs repository-controlled build code.
    Ruby repositories still get lexical/static indexing.
    """
    return _refuse_dep_resolution("ruby")


def _resolve_csharp_deps(clone_path: str) -> tuple[bool, str]:
    """Refuse C# dependency resolution — restore evaluates repo-authored MSBuild.

    `dotnet restore` evaluates the repository's `.csproj`/`.sln`, including
    custom MSBuild targets, inline tasks and SDK resolvers — all author
    controlled. C# repositories still get lexical/static indexing.
    """
    return _refuse_dep_resolution("csharp")


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


def _pyright_interpreter_override(clone_path: str) -> str | None:
    """Return the repo-supplied pyright setting that would name an interpreter.

    scip-python is pyright-based and reads the repository's own pyright config.
    `pythonPath` / `venvPath` / `venv` name an interpreter that pyright RUNS to
    enumerate the environment, and `extraPaths` / `executionEnvironments` add
    import roots inside the clone. All of them are repository-authored choices
    about what code gets loaded, so their presence means we refuse (#5614).
    """
    # Reject inheritance until the complete config chain can be checked; a
    # harmless top-level config must not delegate interpreter choice elsewhere.
    dangerous = ("pythonPath", "venvPath", "venv", "extraPaths", "executionEnvironments", "extends")

    cfg = os.path.join(clone_path, "pyrightconfig.json")
    if os.path.isfile(cfg):
        try:
            with open(cfg, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key in dangerous:
                    if key in data:
                        return f"pyrightconfig.json sets {key}"
        except (OSError, ValueError):
            # Unparseable config: we cannot establish what it asks pyright to
            # load, so treat it as unsafe rather than guessing.
            return "pyrightconfig.json is unreadable/malformed"

    pyproject = os.path.join(clone_path, "pyproject.toml")
    if os.path.isfile(pyproject):
        try:
            with open(pyproject, "rb") as f:
                doc = tomllib.load(f)
            section = doc.get("tool", {}).get("pyright", {})
            if isinstance(section, dict):
                for key in dangerous:
                    if key in section:
                        return f"pyproject.toml [tool.pyright] sets {key}"
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            # A malformed pyproject.toml is not itself an execution risk here
            # (we only fail to read settings); scip-python will report its own
            # parse error. Do not refuse on it.
            return None

    return None


def _index_python(clone_path: str) -> tuple[str | None, str | None]:
    """Run scip-python on a Python repo, loading nothing the repository supplies.

    scip-python is an npm package (@sourcegraph/scip-python), NOT pip. It is
    pyright-based, so it both resolves an interpreter and honours the
    repository's pyright configuration.

    Execution safety (#5614). The previous implementation prepended
    `<clone>/.scip-venv/bin` to PATH and set `VIRTUAL_ENV` to it. A repository
    that commits a file at that path therefore had it executed as `scip-python`.
    Now:
      * PATH is scrubbed of every clone-relative entry and `scip-python` is
        resolved to an absolute trusted path (`_safe_env` / `_resolve_tool`), so
        a planted executable cannot be selected.
      * We never add a clone path to PATH or `VIRTUAL_ENV`. There is no venv to
        add: Python dependency installation is refused (`_resolve_python_deps`).
      * A repository shipping its own interpreter/package directory, or a pyright
        config naming an interpreter or extra import roots, is refused.

    Still passes no `--environment` flag (#3132).
    """
    planted = _planted_tool_dir(clone_path, "python")
    if planted:
        return _refuse_indexer_reason(
            f"repository ships its own {planted}/, which pyright would load an interpreter "
            "and packages from"
        )

    override = _pyright_interpreter_override(clone_path)
    if override:
        return _refuse_indexer_reason(
            f"{override}, which directs pyright at repository-controlled code"
        )

    scip_python = _resolve_tool("scip-python", clone_path)
    if not scip_python:
        return None, "scip-python not found in PATH"

    scip_output = os.path.join(clone_path, "index.scip")

    # Ensure pyproject.toml has [tool.pyright] if it exists (scip-python needs it)
    _ensure_pyright_section(clone_path)

    proc_env = _safe_env(clone_path)

    # Raise Node.js heap limit for scip-python: the default ~2 GB max-old-space-size
    # causes OOM on large repos (e.g. 1,202-file Vibe-Trading dies at 1,989 MB during
    # "Parse and emit SCIP"). 4096 MB is comfortably above the observed death point
    # and below the worker pod memory limit. Use setdefault so operators can override
    # via pod env. (#3149)
    proc_env.setdefault("NODE_OPTIONS", "--max-old-space-size=4096")

    cmd = [scip_python, "index", "--project-name", os.path.basename(clone_path)]
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

    Execution safety (#5614): scip-typescript is resolved to an absolute trusted
    path and runs with a scrubbed environment, so neither a planted executable
    nor `NODE_OPTIONS`/`NODE_PATH` from the repository's tooling can inject code.
    A `tsconfig.json` may name compiler plugins, but scip-typescript uses the
    TypeScript API rather than `tsc`'s plugin host, so plugins are not loaded;
    the dependency step refuses a repository-supplied `node_modules`, which is
    where a plugin would have to come from.
    """
    scip_typescript = _resolve_tool("scip-typescript", clone_path)
    if not scip_typescript:
        return None, "scip-typescript not found in PATH"

    scip_output = os.path.join(clone_path, "index.scip")

    cmd = [scip_typescript, "index"]

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
            env=_safe_env(clone_path),
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
    """Run scip-go on a Go repo.

    Execution safety (#5614): scip-go type-checks sources rather than running a
    build, but it invokes the Go toolchain. `_safe_env` sets `CGO_ENABLED=0` so
    `#cgo` directives in repository sources cannot pass flags to a C compiler,
    and pins `GOTOOLCHAIN=local` so a `go.mod` toolchain directive cannot fetch
    and run a different toolchain. scip-go itself is resolved absolutely.
    """
    scip_go = _resolve_tool("scip-go", clone_path)
    if not scip_go:
        return None, "scip-go not found in PATH"

    scip_output = os.path.join(clone_path, "index.scip")

    cmd = [scip_go, "--output", scip_output]

    env = _safe_env(clone_path)
    env["GOTOOLCHAIN"] = "local"

    try:
        result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
            cmd,
            capture_output=True,
            timeout=600,
            cwd=clone_path,
            env=env,
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
    """Refuse JVM SCIP indexing — `scip-java index` builds the repo.

    `scip-java index` auto-detects the build tool and runs Gradle/Maven (via the
    repository's wrapper when present) to compile the project. Refusing the
    dependency resolver but still running this indexer would leave the same
    execution path open, so both refuse together (#5614 / #4720).
    """
    return _refuse_indexer("java")


def _index_ruby(clone_path: str) -> tuple[str | None, str | None]:
    """Run scip-ruby on a Ruby repo (Sorbet-based, best-effort on untyped).

    Execution safety (#5614): scip-ruby statically analyses sources — it does not
    evaluate the `Gemfile` (Ruby dependency resolution is refused separately).
    Two repository-controlled loading paths are closed here: a committed
    `sorbet/config` can pass plugin options naming an executable for Sorbet to
    run, and a committed `vendor/bundle`/`.bundle` supplies gem code; either one
    means we refuse. `_safe_env` also clears `RUBYOPT`/`RUBYLIB`/`GEM_*`, which
    would otherwise let in-repo Ruby be required at startup.
    """
    planted = _planted_tool_dir(clone_path, "ruby")
    if planted:
        return _refuse_indexer_reason(
            f"repository ships its own {planted}/, which supplies gem code to Sorbet"
        )

    planted_cfg = _planted_tool_config(clone_path, "ruby")
    if planted_cfg:
        return _refuse_indexer_reason(
            f"repository ships {planted_cfg}, which can name a Sorbet plugin to execute"
        )

    scip_ruby = _resolve_tool("scip-ruby", clone_path)
    if not scip_ruby:
        return None, "scip-ruby not found in PATH"

    scip_output = os.path.join(clone_path, "index.scip")

    cmd = [scip_ruby, "--output", scip_output]

    try:
        result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
            cmd,
            capture_output=True,
            timeout=600,
            cwd=clone_path,
            env=_safe_env(clone_path),
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
    """Refuse C# SCIP indexing — `scip-dotnet index` drives MSBuild.

    `scip-dotnet index` restores and builds the repository's project files,
    evaluating author-controlled MSBuild targets. Refused for the same reason as
    the C# dependency resolver (#5614 / #4720).
    """
    return _refuse_indexer("csharp")


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

    # Indexers and package managers write predictable paths in the checkout.
    # A committed symlink can redirect those writes into the worker filesystem.
    # Refuse structural indexing before any resolver mutates the checkout; do
    # not follow links, and leave lexical ingestion to its existing owner.
    unsafe_link = None

    def unreadable_checkout(exc):
        raise exc

    try:
        for root, dirs, files in os.walk(
            clone_path, followlinks=False, onerror=unreadable_checkout
        ):
            dirs[:] = [d for d in dirs if d != ".git"]
            for name in dirs + files:
                path = os.path.join(root, name)
                if os.path.islink(path):
                    unsafe_link = os.path.relpath(path, clone_path)
                    break
            if unsafe_link:
                break
    except OSError as exc:
        unsafe_link = f"unreadable checkout ({exc})"
    if unsafe_link:
        detail = f"{REFUSAL_PREFIX}: checkout contains symbolic link: {unsafe_link}"
        report.results = [
            IndexResult(language=lang, dep_resolution="refused", error=detail)
            for lang in langs_to_index
        ]
        return report

    # Index each language independently (fail-soft per language)
    for lang in langs_to_index:
        # Refusal due to a repository-supplied tool tree applies to the
        # indexer as well as the resolver. Check before npm creates its own
        # node_modules; the presence of that freshly installed tree is expected.
        planted = _planted_tool_dir(clone_path, lang)
        if planted:
            detail = f"{REFUSAL_PREFIX}: repository supplies {planted}"
            report.results.append(
                IndexResult(language=lang, dep_resolution="refused", error=detail)
            )
            continue

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
                dep_resolution=_dep_status(dep_ok, dep_detail),
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
                dep_resolution=_dep_status(dep_ok, dep_detail),
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
            dep_resolution=_dep_status(dep_ok, dep_detail),
            error=error,
        )
        report.results.append(result)

        if scip_path:
            # combined_scip_path holds the first successful .scip (backward compat)
            if report.combined_scip_path is None:
                report.combined_scip_path = scip_path
            log.info("SCIP index produced for %s (%s): %s", repo, lang, scip_path)
        elif is_refusal(error):
            # Not a failure: structural indexing is declined for this language,
            # and the safe lexical/static stages still cover it.
            log.warning("SCIP indexing skipped for %s (%s): %s", repo, lang, error)
        else:
            log.error("SCIP indexing failed for %s (%s): %s", repo, lang, error)

    return report


def cleanup_indexing_artifacts(clone_path: str) -> None:
    """Remove indexing artifacts left in the clone.

    We no longer create a `.scip-venv` inside the clone (#5614 — a repository can
    commit that path, and we then put it on PATH). Both paths are still removed
    if present: a repository may have committed them, and leaving repository-
    supplied executables in a clone that later ingestion stages walk is exactly
    what we are trying to avoid.
    """
    for rel in (".scip-venv", ".scip-environment.json"):
        path = os.path.join(clone_path, rel)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.unlink(path)
