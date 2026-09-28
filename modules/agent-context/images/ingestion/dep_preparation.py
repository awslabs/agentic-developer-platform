"""Controlled dependency preparation for isolated parser execution.

Runs in the trusted fetch stage (has network), prepares dependency artifacts
for offline parsing.  Enforces approved sources at the actual fetch boundary:

- npm: pinned registry, refuse scoped registries, git/file/link sources,
  lockfile redirects to unapproved origins, lifecycle scripts.
- Go: GONOSUMCHECK empty, GOPROXY pinned (no ``direct`` fallback for
  unapproved modules), refuse ``replace`` directives pointing outside
  the module, refuse toolchain downloads.
- Python: dependency installation refused (PEP 517 builds execute repo code);
  scip-python works without installed deps (pyright type inference).

The prepared output is a bounded directory containing only verified dependency
artifacts.  The parser receives this directory read-only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Approved registries — the allowlist, not a default
# ---------------------------------------------------------------------------

# npm: only the public registry.  Scoped registries (@scope -> custom URL)
# require explicit operator approval in the fetch capability.
DEFAULT_APPROVED_NPM_REGISTRIES = frozenset({"https://registry.npmjs.org"})

# Go: proxy.golang.org is the default module mirror.  No ``direct`` fallback.
DEFAULT_APPROVED_GO_PROXIES = frozenset({"https://proxy.golang.org"})

# Patterns that indicate unapproved dependency sources
_NPM_GIT_DEP = re.compile(r"^(git[+:]|github:|bitbucket:|gitlab:)")
_NPM_FILE_DEP = re.compile(r"^(file:|link:|/|\.\.?/)")


def _approved_url(value: str, registries: frozenset[str]) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and any(parsed.hostname == urlsplit(reg).hostname for reg in registries)
        )
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class DepPrepResult:
    """Result of dependency preparation for one language."""

    language: str
    success: bool
    prepared_dir: str | None = None
    detail: str = ""
    refused: bool = False
    files_count: int = 0
    total_bytes: int = 0


@dataclass
class PreparedDeps:
    """Aggregated prepared dependencies for all languages in a repo."""

    results: list[DepPrepResult] = field(default_factory=list)
    scratch_dir: str = ""

    @property
    def any_success(self) -> bool:
        return any(r.success for r in self.results)


# ---------------------------------------------------------------------------
# npm lockfile validation
# ---------------------------------------------------------------------------


def _validate_npm_lockfile(
    lockfile_path: str,
    approved_registries: frozenset[str] = DEFAULT_APPROVED_NPM_REGISTRIES,
) -> list[str]:
    """Check an npm lockfile for unapproved resolved URLs.

    Returns a list of violation descriptions (empty = valid).
    """
    violations: list[str] = []
    if not os.path.isfile(lockfile_path):
        return violations

    try:
        with open(lockfile_path) as f:
            lock_data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        violations.append(f"lockfile unreadable: {e}")
        return violations

    # npm lockfile v2/v3: packages dict
    packages = lock_data.get("packages", {})
    for pkg_path, pkg_info in packages.items():
        resolved = pkg_info.get("resolved", "")
        if not resolved:
            continue
        if not _approved_url(resolved, approved_registries):
            violations.append(f"unapproved resolved URL in lockfile: {pkg_path} -> {resolved}")

    return violations


def _validate_npm_package_json(
    package_json_path: str,
    approved_registries: frozenset[str] = DEFAULT_APPROVED_NPM_REGISTRIES,
) -> list[str]:
    """Check package.json for unapproved dependency sources.

    Rejects: git dependencies, file/link dependencies, URL dependencies
    pointing to unapproved registries, scoped registry configs.
    """
    violations: list[str] = []
    if not os.path.isfile(package_json_path):
        return violations

    try:
        with open(package_json_path) as f:
            pkg = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        violations.append(f"package.json unreadable: {e}")
        return violations

    for dep_type in ("dependencies", "devDependencies", "optionalDependencies"):
        deps = pkg.get(dep_type, {})
        if not isinstance(deps, dict):
            continue
        for name, spec in deps.items():
            if not isinstance(spec, str):
                continue
            if _NPM_GIT_DEP.match(spec):
                violations.append(f"{dep_type}.{name}: git dependency refused ({spec})")
            elif _NPM_FILE_DEP.match(spec):
                violations.append(f"{dep_type}.{name}: file/link dependency refused ({spec})")
            elif spec.startswith(("http:", "https:")) and not _approved_url(
                spec, approved_registries
            ):
                violations.append(f"{dep_type}.{name}: unapproved URL dependency ({spec})")

    return violations


# ---------------------------------------------------------------------------
# Go module validation
# ---------------------------------------------------------------------------


def _validate_go_mod(go_mod_path: str) -> list[str]:
    """Check go.mod for unapproved directives.

    Rejects: replace directives pointing outside the module, toolchain
    download directives.
    """
    violations: list[str] = []
    if not os.path.isfile(go_mod_path):
        return violations

    try:
        with open(go_mod_path) as f:
            content = f.read()
    except OSError as e:
        violations.append(f"go.mod unreadable: {e}")
        return violations

    for line in content.splitlines():
        stripped = line.strip()
        if "=>" in stripped and stripped.split("=>", 1)[1].strip().startswith(
            (".", "/", "\\", '"')
        ):
            violations.append(f"replace directive points outside module: {stripped}")
        if stripped.startswith("toolchain ") and stripped != "toolchain local":
            violations.append(f"toolchain download directive: {stripped}")

    return violations


# ---------------------------------------------------------------------------
# Per-language dependency preparers
# ---------------------------------------------------------------------------


def _prepare_typescript_deps(
    clone_path: str,
    scratch_dir: str,
    approved_registries: frozenset[str] = DEFAULT_APPROVED_NPM_REGISTRIES,
) -> DepPrepResult:
    """Prepare TypeScript/JavaScript dependencies for offline parsing.

    Runs ``npm install --ignore-scripts`` with a pinned registry into a
    bounded scratch directory.  Validates the lockfile and package.json
    before and after installation.
    """
    result = DepPrepResult(language="typescript", success=False)
    pkg_json = os.path.join(clone_path, "package.json")

    if not os.path.isfile(pkg_json):
        result.detail = "no package.json"
        result.success = True  # nothing to prepare
        return result

    # Pre-install validation
    violations = _validate_npm_package_json(pkg_json, approved_registries)
    if violations:
        result.detail = f"refused: {'; '.join(violations)}"
        result.refused = True
        return result

    # Check existing lockfile for redirects
    for lockfile in ("package-lock.json", "npm-shrinkwrap.json"):
        lf_path = os.path.join(clone_path, lockfile)
        lf_violations = _validate_npm_lockfile(lf_path, approved_registries)
        if lf_violations:
            result.detail = f"refused: {'; '.join(lf_violations)}"
            result.refused = True
            return result

    # Check for .npmrc with scoped registries
    npmrc_path = os.path.join(clone_path, ".npmrc")
    if os.path.isfile(npmrc_path):
        try:
            with open(npmrc_path) as f:
                npmrc_content = f.read()
            # Scoped registries: @scope:registry=...
            for line in npmrc_content.splitlines():
                stripped = line.strip()
                if stripped.startswith(("//", "#")):
                    continue
                if ":registry=" in stripped:
                    registry_url = stripped.split("=", 1)[1].strip()
                    if registry_url.rstrip("/") not in {r.rstrip("/") for r in approved_registries}:
                        result.detail = f"refused: scoped registry {stripped}"
                        result.refused = True
                        return result
        except OSError:
            pass

    # Prepare the npm install in a bounded scratch area
    node_modules_dir = os.path.join(scratch_dir, "node_modules")
    os.makedirs(node_modules_dir, exist_ok=True)

    # Copy only validated manifests; repository .npmrc is never executed.
    shutil.copyfile(pkg_json, os.path.join(scratch_dir, "package.json"))
    for filename in ("package-lock.json", "npm-shrinkwrap.json"):
        original = os.path.join(clone_path, filename)
        if os.path.isfile(original):
            shutil.copyfile(original, os.path.join(scratch_dir, filename))

    # Build a safe environment (no clone-resident PATH entries)
    env = _safe_dep_env(clone_path)
    env["HOME"] = scratch_dir

    registry = min(approved_registries) if approved_registries else "https://registry.npmjs.org"
    try:
        subprocess.run(
            [
                "npm",
                "install",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                f"--registry={registry}",
                f"--prefix={scratch_dir}",
            ],
            cwd=scratch_dir,
            env=env,
            check=True,
            capture_output=True,
            timeout=300,
        )
    except FileNotFoundError:
        result.detail = "npm not found"
        return result
    except subprocess.CalledProcessError as e:
        result.detail = f"npm install failed: {e.stderr[:500] if e.stderr else str(e)}"
        return result
    except subprocess.TimeoutExpired:
        result.detail = "npm install timed out"
        return result

    # Post-install: validate the generated lockfile
    post_lock = os.path.join(scratch_dir, "package-lock.json")
    post_violations = _validate_npm_lockfile(post_lock, approved_registries)
    if post_violations:
        result.detail = f"refused after install: {'; '.join(post_violations)}"
        result.refused = True
        shutil.rmtree(node_modules_dir, ignore_errors=True)
        return result

    # Count what we prepared
    total_bytes = 0
    file_count = 0
    for root, _dirs, files in os.walk(node_modules_dir):
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                total_bytes += os.path.getsize(fpath)
                file_count += 1
            except OSError:
                pass

    result.success = True
    result.prepared_dir = node_modules_dir
    result.detail = f"prepared {file_count} files ({total_bytes} bytes)"
    result.files_count = file_count
    result.total_bytes = total_bytes
    return result


def _prepare_go_deps(
    clone_path: str,
    scratch_dir: str,
    approved_proxies: frozenset[str] = DEFAULT_APPROVED_GO_PROXIES,
) -> DepPrepResult:
    """Prepare Go dependencies for offline parsing.

    Runs ``go mod download`` with pinned GOPROXY (no direct fallback),
    downloads to a bounded scratch GOMODCACHE.
    """
    result = DepPrepResult(language="go", success=False)
    go_mod = os.path.join(clone_path, "go.mod")

    if not os.path.isfile(go_mod):
        result.detail = "no go.mod"
        result.success = True  # nothing to prepare
        return result

    # Pre-download validation
    violations = _validate_go_mod(go_mod)
    if violations:
        result.detail = f"refused: {'; '.join(violations)}"
        result.refused = True
        return result

    gomod_cache = os.path.join(scratch_dir, "gomodcache")
    os.makedirs(gomod_cache, exist_ok=True)

    env = _safe_dep_env(clone_path)
    env["HOME"] = scratch_dir
    proxy = ",".join(sorted(approved_proxies))
    env.update(
        {
            "GOMODCACHE": gomod_cache,
            "GONOSUMCHECK": "",
            "GOFLAGS": "-mod=readonly",
            "GOPROXY": proxy,
            "GONOSUMDB": "",
            "GOTOOLCHAIN": "local",
            "CGO_ENABLED": "0",
        }
    )

    try:
        subprocess.run(
            ["go", "mod", "download"],
            cwd=clone_path,
            env=env,
            check=True,
            capture_output=True,
            timeout=300,
        )
    except FileNotFoundError:
        result.detail = "go not found"
        return result
    except subprocess.CalledProcessError as e:
        result.detail = f"go mod download failed: {e.stderr[:500] if e.stderr else str(e)}"
        return result
    except subprocess.TimeoutExpired:
        result.detail = "go mod download timed out"
        return result

    total_bytes = 0
    file_count = 0
    for root, _dirs, files in os.walk(gomod_cache):
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                total_bytes += os.path.getsize(fpath)
                file_count += 1
            except OSError:
                pass

    result.success = True
    result.prepared_dir = gomod_cache
    result.detail = f"prepared {file_count} files ({total_bytes} bytes)"
    result.files_count = file_count
    result.total_bytes = total_bytes
    return result


def _prepare_python_deps(clone_path: str, scratch_dir: str) -> DepPrepResult:
    """Python dependency preparation — refused.

    Python dep installation requires executing repo-authored build logic
    (setup.py, PEP 517 backends).  scip-python uses pyright type inference
    and works without installed deps.
    """
    return DepPrepResult(
        language="python",
        success=True,
        detail="deps not required (pyright type inference)",
    )


def _prepare_ruby_deps(clone_path: str, scratch_dir: str) -> DepPrepResult:
    """Ruby dependency preparation — refused.

    Gemfile is executable Ruby code; ``bundle install`` evaluates it.
    scip-ruby uses Sorbet and works without gem installation.
    """
    return DepPrepResult(
        language="ruby",
        success=True,
        detail="deps not required (Sorbet static analysis)",
    )


# Dispatch table
DEP_PREPARERS: dict[str, Any] = {
    "python": _prepare_python_deps,
    "typescript": _prepare_typescript_deps,
    "javascript": _prepare_typescript_deps,
    "go": _prepare_go_deps,
    "ruby": _prepare_ruby_deps,
}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def prepare_dependencies(
    clone_path: str,
    languages: list[str],
    *,
    scratch_base: str | None = None,
    fetch_cap=None,
    approved_npm_registries: frozenset[str] = DEFAULT_APPROVED_NPM_REGISTRIES,
    approved_go_proxies: frozenset[str] = DEFAULT_APPROVED_GO_PROXIES,
) -> PreparedDeps:
    """Prepare dependencies for all detected languages.

    Creates a bounded scratch directory, runs per-language preparers,
    and returns the aggregated results.  The scratch directory should
    be cleaned up by the caller after the parser completes.
    """
    from parser_capability import CapabilityDeniedError

    if fetch_cap is None or fetch_cap.is_expired:
        raise CapabilityDeniedError("Dependency preparation requires a live server fetch grant")
    approved_npm_registries = frozenset(fetch_cap.approved_registries) & approved_npm_registries
    approved_go_proxies = frozenset(fetch_cap.approved_registries) & approved_go_proxies
    scratch_dir = scratch_base or tempfile.mkdtemp(prefix="parser-deps-")
    prepared = PreparedDeps(scratch_dir=scratch_dir)

    for lang in languages:
        preparer = DEP_PREPARERS.get(lang)
        if preparer is None:
            prepared.results.append(
                DepPrepResult(
                    language=lang,
                    success=False,
                    refused=True,
                    detail=f"no dependency preparer for {lang}",
                )
            )
            continue

        try:
            if lang in ("typescript", "javascript"):
                if not approved_npm_registries:
                    raise CapabilityDeniedError("No registry admitted by fetch grant")
                result = preparer(clone_path, scratch_dir, approved_npm_registries)
            elif lang == "go":
                if not approved_go_proxies:
                    raise CapabilityDeniedError("No Go proxy admitted by fetch grant")
                result = preparer(clone_path, scratch_dir, approved_go_proxies)
            else:
                result = preparer(clone_path, scratch_dir)
            prepared.results.append(result)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            log.warning("Dependency preparation failed for %s: %s", lang, e)
            prepared.results.append(
                DepPrepResult(language=lang, success=False, detail=f"error: {e}")
            )

    return prepared


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_dep_env(clone_path: str) -> dict[str, str]:
    """Build a subprocess environment scrubbed of clone-resident paths.

    Similar to scip_indexer._safe_env but focused on the dep-preparation stage.
    """
    # Construct an allowlist rather than inheriting AWS/GitHub/database tokens,
    # HOME credentials, user npm config, proxy auth or interpreter hooks.
    return {
        "PATH": "/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "AWS_EC2_METADATA_DISABLED": "true",
        "CGO_ENABLED": "0",
    }


def cleanup_prepared_deps(prepared: PreparedDeps) -> None:
    """Remove the scratch directory and all prepared artifacts."""
    if prepared.scratch_dir and os.path.isdir(prepared.scratch_dir):
        shutil.rmtree(prepared.scratch_dir, ignore_errors=True)
