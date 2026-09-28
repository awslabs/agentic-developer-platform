"""Isolated parser entrypoint — runs inside the credential-free parser container.

This module is the ONLY entrypoint the parser container executes.  It:
1. Reads the input manifest from a fixed path (``/input/input_manifest.json``)
2. Runs the SCIP indexer functions on the prepared source
3. Writes the output manifest to ``<output_dir>/output_manifest.json``
4. Exits

Import discipline: this module MUST NOT import any AWS SDK, database driver,
HTTP client, or credential-bearing module.  It imports only:
- Standard library (json, os, sys, hashlib, logging, time)
- SCIP indexer functions (detect_languages, index_repo, cleanup_indexing_artifacts)
- Parser manifest types (ParseInputManifest, ParseOutputManifest, LanguageResult)

The parser has:
- No network (--network=none / NetworkPolicy deny-all)
- No AWS/GitHub/model/DB credentials
- No service-account tokens
- No shared platform-data PVC
- Read-only source mount
- Bounded private working/output space
- Dropped capabilities, no escalation, seccomp
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import subprocess
import sys
import time

# Parser manifest types — no external dependencies
from parser_manifest import (
    LanguageResult,
    ParseInputManifest,
    ParseOutputManifest,
    compute_file_digest,
)

log = logging.getLogger("isolated_parser")

# Fixed paths inside the parser container
INPUT_MANIFEST_PATH = "/input/input_manifest.json"
DEFAULT_OUTPUT_DIR = "/output"

# Forbidden imports — these must never be present in the parser's process
_FORBIDDEN_MODULES = frozenset(
    {
        "boto3",
        "botocore",
        "httpx",
        "requests",
        "urllib3",
        "gremlinpython",
        "psycopg2",
        "psycopg",
        "opensearchpy",
        "github_auth",
        "db",
        "s3_store",
        "status_callback",
        "wiki_store",
        "scip_neptune_loader",
        "scip_neptune_csv",
    }
)


def _check_import_discipline() -> list[str]:
    """Check that no forbidden modules are loaded in the process.

    Returns a list of violations.  Called before and after indexing.
    """
    violations = []
    for mod_name in _FORBIDDEN_MODULES:
        if mod_name in sys.modules:
            violations.append(f"forbidden module loaded: {mod_name}")
    return violations


def _verify_source_digest(source_dir: str, expected_digest: str) -> bool:
    """Verify the source directory matches the expected digest.

    Uses the same algorithm as parser_manifest.compute_tree_digest
    but implemented inline to avoid importing compute_tree_digest
    (which we already have access to — this is defence in depth).
    """
    h = hashlib.sha256()
    for root, dirs, files in os.walk(source_dir, topdown=True):
        dirs.sort()
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, source_dir)
            if os.path.islink(fpath):
                continue
            h.update(rel.encode("utf-8"))
            try:
                with open(fpath, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        h.update(chunk)
            except (OSError, PermissionError):
                h.update(b"<unreadable>")
    return h.hexdigest() == expected_digest


def run_parser(
    input_manifest_path: str = INPUT_MANIFEST_PATH,
) -> int:
    """Main parser execution.  Returns exit code."""
    start_time = time.time()

    # Pre-execution import discipline check
    violations = _check_import_discipline()
    if violations:
        log.error("Import discipline violation before parse: %s", violations)
        _write_error_manifest(
            DEFAULT_OUTPUT_DIR,
            error=f"import discipline violation: {'; '.join(violations)}",
        )
        return 1

    # Read input manifest
    try:
        with open(input_manifest_path) as f:
            manifest_data = json.load(f)
        manifest = ParseInputManifest.from_dict(manifest_data)
    except FileNotFoundError:
        log.error("Input manifest not found: %s", input_manifest_path)
        return 1
    except (json.JSONDecodeError, TypeError, KeyError) as e:
        log.error("Invalid input manifest: %s", e)
        return 1

    output_dir = manifest.output_dir or DEFAULT_OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)

    # Verify source digest
    if manifest.source_digest and not _verify_source_digest(
        manifest.source_dir, manifest.source_digest
    ):
        log.error("Source digest mismatch — possible substitution")
        _write_error_manifest(
            output_dir,
            invocation_id=manifest.invocation_id,
            asset_id=manifest.asset_id,
            attempt_id=manifest.attempt_id,
            error="source digest mismatch",
        )
        return 1

    # Import the SCIP indexer functions (these are the only external imports)
    try:
        from scip_indexer import (
            cleanup_indexing_artifacts,
            detect_languages,
            index_repo,
        )
    except ImportError as e:
        log.error("Failed to import SCIP indexer: %s", e)
        _write_error_manifest(
            output_dir,
            invocation_id=manifest.invocation_id,
            asset_id=manifest.asset_id,
            attempt_id=manifest.attempt_id,
            error=f"indexer import failed: {e}",
        )
        return 1

    # Post-import discipline check
    violations = _check_import_discipline()
    if violations:
        log.error("Import discipline violation after indexer import: %s", violations)
        _write_error_manifest(
            output_dir,
            invocation_id=manifest.invocation_id,
            asset_id=manifest.asset_id,
            attempt_id=manifest.attempt_id,
            error=f"import discipline violation: {'; '.join(violations)}",
        )
        return 1

    # Detect and filter languages
    # Indexers write artifacts/configuration; only the private bounded scratch
    # copy is writable. The admitted source mount remains read-only.
    os.environ["SCIP_PROJECT_VERSION"] = manifest.source_digest
    source_dir = tempfile.mkdtemp(prefix="parser-source-")
    shutil.copytree(manifest.source_dir, source_dir, dirs_exist_ok=True)
    detected = detect_languages(source_dir)
    if manifest.allowed_languages:
        allowed_set = set(manifest.allowed_languages)
        detected = {k: v for k, v in detected.items() if k in allowed_set}

    if not detected:
        log.info("No indexable languages detected")
        _write_output_manifest(
            output_dir,
            invocation_id=manifest.invocation_id,
            asset_id=manifest.asset_id,
            attempt_id=manifest.attempt_id,
            languages=[],
            status="no_languages",
        )
        return 0

    # Check deadline
    elapsed = time.time() - start_time
    remaining = manifest.resource_limits.deadline_seconds - elapsed
    if remaining <= 0:
        _write_error_manifest(
            output_dir,
            invocation_id=manifest.invocation_id,
            asset_id=manifest.asset_id,
            attempt_id=manifest.attempt_id,
            error="deadline exceeded before indexing",
        )
        return 1

    # Run the indexer
    try:
        report = index_repo(source_dir, manifest.asset_id, languages=list(detected.keys()))
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        log.error("Indexer failed: %s", e)
        _write_error_manifest(
            output_dir,
            invocation_id=manifest.invocation_id,
            asset_id=manifest.asset_id,
            attempt_id=manifest.attempt_id,
            error=f"indexer failed: {e}",
        )
        return 1

    # Collect results — move .scip files to output dir
    lang_results: list[LanguageResult] = []
    total_bytes = 0
    for idx_result in report.results:
        scip_rel_path = None
        digest = None
        output_bytes = 0

        if idx_result.success and idx_result.scip_path and os.path.isfile(idx_result.scip_path):
            # Move the .scip file to the output directory
            scip_filename = f"{idx_result.language}.scip"
            dest_path = os.path.join(output_dir, scip_filename)
            try:
                # Copy rather than move — source may be read-only

                shutil.copy2(idx_result.scip_path, dest_path)
                output_bytes = os.path.getsize(dest_path)
                total_bytes += output_bytes
                digest = compute_file_digest(dest_path)
                scip_rel_path = scip_filename
            except (OSError, shutil.Error) as e:
                log.warning("Failed to copy .scip for %s: %s", idx_result.language, e)

        lang_results.append(
            LanguageResult(
                language=idx_result.language,
                success=idx_result.success,
                scip_path=scip_rel_path,
                dep_resolution=idx_result.dep_resolution,
                error=idx_result.error,
                file_count=idx_result.file_count,
                output_bytes=output_bytes,
                digest=digest,
            )
        )

    # Check output size limit
    if total_bytes > manifest.resource_limits.output_bytes_max:
        _write_error_manifest(
            output_dir,
            invocation_id=manifest.invocation_id,
            asset_id=manifest.asset_id,
            attempt_id=manifest.attempt_id,
            error=f"output size {total_bytes} exceeds limit {manifest.resource_limits.output_bytes_max}",
        )
        return 1

    # Clean up indexing artifacts from the source (if writable)
    try:
        cleanup_indexing_artifacts(source_dir)
    except (OSError, PermissionError):
        pass  # Source may be read-only — that's fine

    # Final import discipline check
    violations = _check_import_discipline()
    if violations:
        log.warning("Import discipline violation after parse (non-fatal): %s", violations)

    # Write output manifest
    status = "complete" if any(r.success for r in lang_results) else "indexing_failed"
    _write_output_manifest(
        output_dir,
        invocation_id=manifest.invocation_id,
        asset_id=manifest.asset_id,
        attempt_id=manifest.attempt_id,
        languages=lang_results,
        status=status,
        total_bytes=total_bytes,
    )

    return 0


def _write_output_manifest(
    output_dir: str,
    *,
    invocation_id: str = "",
    asset_id: str = "",
    attempt_id: str = "",
    languages: list[LanguageResult] | None = None,
    status: str = "complete",
    total_bytes: int = 0,
) -> None:
    """Write the output manifest to the output directory."""
    manifest = ParseOutputManifest(
        invocation_id=invocation_id,
        asset_id=asset_id,
        attempt_id=attempt_id,
        languages=languages or [],
        status=status,
        total_output_bytes=total_bytes,
    )
    manifest_path = os.path.join(output_dir, "output_manifest.json")
    with open(manifest_path, "w") as f:
        f.write(manifest.to_json())
    log.info("Output manifest written: %s", manifest_path)


def _write_error_manifest(
    output_dir: str,
    *,
    invocation_id: str = "",
    asset_id: str = "",
    attempt_id: str = "",
    error: str = "",
) -> None:
    """Write an error output manifest."""
    os.makedirs(output_dir, exist_ok=True)
    manifest = ParseOutputManifest(
        invocation_id=invocation_id,
        asset_id=asset_id,
        attempt_id=attempt_id,
        status="error",
        error=error,
    )
    manifest_path = os.path.join(output_dir, "output_manifest.json")
    try:
        with open(manifest_path, "w") as f:
            f.write(manifest.to_json())
    except OSError:
        pass  # Best effort — the error is logged regardless


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    sys.exit(run_parser(os.environ.get("PARSER_INPUT_MANIFEST", INPUT_MANIFEST_PATH)))
