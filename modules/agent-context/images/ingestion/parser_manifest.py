"""Typed input/output manifests for the isolated parser pipeline.

Every isolated parser invocation is bound by immutable manifests:

- **ParseInputManifest**: server-owned description of what the parser receives
  (source digest, allowed languages, resource limits, expected output paths).
- **ParseOutputManifest**: parser-produced description of what it created
  (per-language results, output paths, digests, byte counts).
- **InvocationBinding**: ties an input manifest to a specific asset, attempt
  and authorised scope so the publisher can reject cross-asset or replayed
  results.

The parser image reads the input manifest from a fixed path, executes within
the declared bounds, and writes the output manifest.  The publisher validates
the output manifest against the invocation binding before any storage operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any

# ---------------------------------------------------------------------------
# Input manifest — server-owned, trusted
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceLimits:
    """Resource bounds the parser must not exceed."""

    cpu_millicores: int = 2000
    memory_mib: int = 4096
    deadline_seconds: int = 600
    output_bytes_max: int = 512 * 1024 * 1024  # 512 MiB


@dataclass(frozen=True)
class ParseInputManifest:
    """Immutable description of what the isolated parser receives.

    Created by the trusted fetch stage, consumed by the parser entrypoint.
    The parser MUST NOT modify this; the publisher cross-checks it.
    """

    # Identity
    invocation_id: str
    asset_id: str
    attempt_id: str

    # Source binding
    source_dir: str  # path inside the parser container
    source_digest: str  # sha256 of the prepared input tree

    # Parser configuration
    allowed_languages: list[str]
    output_dir: str  # bounded output path inside the container

    # Limits
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)

    # Expected output file names (parser writes these under output_dir)
    expected_outputs: list[str] = field(default_factory=lambda: ["output_manifest.json"])

    # Timestamp
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ParseInputManifest:
        limits = data.get("resource_limits", {})
        if isinstance(limits, dict):
            data = {**data, "resource_limits": ResourceLimits(**limits)}
        return cls(**data)

    @classmethod
    def from_json(cls, raw: str) -> ParseInputManifest:
        return cls.from_dict(json.loads(raw))


# ---------------------------------------------------------------------------
# Output manifest — parser-produced, untrusted until validated
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LanguageResult:
    """Per-language indexing result within an output manifest."""

    language: str
    success: bool
    scip_path: str | None = None  # relative to output_dir
    dep_resolution: str = "unknown"
    error: str | None = None
    file_count: int = 0
    output_bytes: int = 0
    digest: str | None = None  # sha256 of the .scip file


@dataclass(frozen=True)
class ParseOutputManifest:
    """Parser-produced description of what it created.

    Written by the parser entrypoint to ``<output_dir>/output_manifest.json``.
    UNTRUSTED: the publisher validates every field before acting on it.
    """

    # Must match the input manifest
    invocation_id: str
    asset_id: str
    attempt_id: str

    # Results
    languages: list[LanguageResult] = field(default_factory=list)
    status: str = "complete"
    error: str | None = None
    total_output_bytes: int = 0

    # Timestamp
    completed_at: float = field(default_factory=time.time)

    @property
    def any_success(self) -> bool:
        return any(r.success for r in self.languages)

    @property
    def successful_languages(self) -> list[str]:
        return [r.language for r in self.languages if r.success]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ParseOutputManifest:
        langs = data.get("languages", [])
        if langs and isinstance(langs[0], dict):
            data = {**data, "languages": [LanguageResult(**lr) for lr in langs]}
        return cls(**data)

    @classmethod
    def from_json(cls, raw: str) -> ParseOutputManifest:
        return cls.from_dict(json.loads(raw))


# ---------------------------------------------------------------------------
# Invocation binding — ties manifests to an asset scope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InvocationBinding:
    """Server-owned binding between a parse invocation and its asset scope.

    The publisher uses this to reject cross-asset, replayed or expired results.
    """

    invocation_id: str
    asset_id: str
    attempt_id: str
    source_digest: str

    # Allowed output constraints
    allowed_output_prefixes: list[str] = field(default_factory=list)
    output_bytes_max: int = 512 * 1024 * 1024

    # Timing
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0  # 0 = no expiry (test-only); production requires expiry

    # State
    cancelled: bool = False

    @property
    def is_expired(self) -> bool:
        if self.expires_at <= 0:
            return False
        return time.time() > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Validation helpers — used by the publisher to check untrusted output
# ---------------------------------------------------------------------------


class ManifestValidationError(ValueError):
    """Raised when an output manifest fails validation."""


def validate_output_manifest(
    output: ParseOutputManifest,
    binding: InvocationBinding,
    output_dir: str,
) -> list[str]:
    """Validate an untrusted output manifest against a server-owned binding.

    Returns a list of violation descriptions (empty = valid).
    Raises ManifestValidationError on critical violations that should abort.
    """
    violations: list[str] = []

    # Identity match
    if output.invocation_id != binding.invocation_id:
        violations.append(
            f"invocation_id mismatch: output={output.invocation_id} binding={binding.invocation_id}"
        )
    if output.asset_id != binding.asset_id:
        violations.append(f"asset_id mismatch: output={output.asset_id} binding={binding.asset_id}")
    if output.attempt_id != binding.attempt_id:
        violations.append(
            f"attempt_id mismatch: output={output.attempt_id} binding={binding.attempt_id}"
        )

    # Expiry / cancellation
    if binding.is_expired:
        violations.append("binding expired")
    if binding.cancelled:
        violations.append("binding cancelled")

    # Output size
    if output.total_output_bytes > binding.output_bytes_max:
        violations.append(
            f"output size {output.total_output_bytes} exceeds limit {binding.output_bytes_max}"
        )

    # Validate individual language results
    abs_output = os.path.abspath(output_dir)
    for lang_result in output.languages:
        if lang_result.scip_path is not None:
            _validate_output_path(lang_result.scip_path, abs_output, violations)

    if violations:
        raise ManifestValidationError("; ".join(violations))

    return violations


def _validate_output_path(
    rel_path: str,
    abs_output_dir: str,
    violations: list[str],
) -> None:
    """Check that an output path stays within the output directory.

    Rejects: traversal (../), absolute paths, symlinks, null bytes.
    """
    if "\x00" in rel_path:
        violations.append(f"null byte in output path: {rel_path!r}")
        return

    if os.path.isabs(rel_path):
        violations.append(f"absolute output path: {rel_path}")
        return

    # Normalise and check for traversal
    normalised = PurePosixPath(rel_path)
    parts = normalised.parts
    if ".." in parts:
        violations.append(f"traversal in output path: {rel_path}")
        return

    # Resolve against the output dir and confirm containment
    full_path = os.path.normpath(os.path.join(abs_output_dir, rel_path))
    if not full_path.startswith(abs_output_dir + os.sep) and full_path != abs_output_dir:
        violations.append(f"output path escapes output dir: {rel_path} -> {full_path}")
        return

    # If the file exists, reject symlinks
    if os.path.lexists(full_path) and os.path.islink(full_path):
        violations.append(f"symlink in output: {rel_path} -> {os.readlink(full_path)}")


def compute_tree_digest(directory: str) -> str:
    """Compute a deterministic SHA-256 digest of a directory tree.

    Covers file paths (sorted) and contents. Used to bind the source
    input to the invocation so the publisher can detect substitution.
    """
    h = hashlib.sha256()
    for root, dirs, files in os.walk(directory, topdown=True):
        dirs.sort()
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, directory)
            # Skip symlinks — they should not exist in prepared input
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
                # Unreadable file — include the path but not content
                h.update(b"<unreadable>")
    return h.hexdigest()


def compute_file_digest(filepath: str) -> str:
    """Compute SHA-256 digest of a single file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
