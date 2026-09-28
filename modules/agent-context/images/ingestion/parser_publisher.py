"""Output publisher — validates untrusted parser output before storage.

Runs in the trusted publish stage (has credentials).  Reads the parser's
output manifest, validates it against the invocation binding, checks every
output path, digest and size, then stores validated results.

The parser NEVER receives publish credentials.  This module is the only
code that bridges untrusted parser output to trusted storage operations.
"""

from __future__ import annotations

import json
import logging
import os
import hashlib
import stat

from parser_capability import PublishCapability
from parser_manifest import (
    InvocationBinding,
    ManifestValidationError,
    ParseOutputManifest,
    validate_output_manifest,
)

log = logging.getLogger(__name__)


class PublicationError(RuntimeError):
    """Raised when publication fails validation."""


class OutputPublisher:
    """Validates and publishes isolated parser output.

    The publisher is instantiated with a publish capability and invocation
    binding.  It checks every output artifact before writing to storage.
    """

    def __init__(
        self,
        binding: InvocationBinding,
        publish_cap: PublishCapability | None = None,
    ):
        self._binding = binding
        self._publish_cap = publish_cap
        self._validated_bytes: dict[str, bytes] = {}

    def validate_and_collect(
        self,
        output_dir: str,
    ) -> ParseOutputManifest:
        """Read and validate the parser's output manifest.

        Raises PublicationError on any validation failure.
        Returns the validated manifest (does not yet publish).
        """
        manifest_path = os.path.join(output_dir, "output_manifest.json")

        # Bound and reject symlink/special-file manifests before parsing JSON.
        try:
            fd = os.open(manifest_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise PublicationError("Output manifest is not a regular file")
                body = stream.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise PublicationError("Output manifest exceeds byte limit")
                raw = json.loads(body)
        except (json.JSONDecodeError, OSError) as e:
            raise PublicationError(f"Output manifest unreadable: {e}") from e

        try:
            output = ParseOutputManifest.from_dict(raw)
        except (TypeError, KeyError) as e:
            raise PublicationError(f"Output manifest invalid: {e}") from e

        # Validate against binding
        try:
            validate_output_manifest(output, self._binding, output_dir)
        except ManifestValidationError as e:
            raise PublicationError(f"Output manifest validation failed: {e}") from e

        # Freeze the exact validated bytes; later consumers never reopen mutable
        # parser paths. Actual bytes, not self-reported sizes, enforce the budget.
        abs_output = os.path.realpath(output_dir)
        total = 0
        self._validated_bytes = {}
        for result in output.languages:
            if not result.success or not result.scip_path:
                continue
            file_path = os.path.join(abs_output, result.scip_path)
            if os.path.realpath(file_path) != os.path.abspath(file_path):
                raise PublicationError("Symlink in output path")
            if not os.path.realpath(file_path).startswith(abs_output + os.sep):
                raise PublicationError("Output file escapes output dir")
            if result.language in self._validated_bytes:
                raise PublicationError("Duplicate output language")
            try:
                fd = os.open(file_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise PublicationError("Output is not a regular file")
                    body = stream.read(self._binding.output_bytes_max - total + 1)
            except OSError as exc:
                raise PublicationError("Output file unreadable or symlink") from exc
            total += len(body)
            if total > self._binding.output_bytes_max:
                raise PublicationError("Actual output size exceeds limit")
            if result.output_bytes != len(body):
                raise PublicationError("Output size mismatch")
            if not result.digest or hashlib.sha256(body).hexdigest() != result.digest:
                raise PublicationError("Output digest mismatch")
            self._validated_bytes[result.language] = body
        if output.total_output_bytes != total:
            raise PublicationError("Manifest total size mismatch")

        if self._publish_cap is not None:
            cap = self._publish_cap
            if (cap.invocation_id, cap.asset_id, cap.attempt_id) != (
                self._binding.invocation_id,
                self._binding.asset_id,
                self._binding.attempt_id,
            ):
                raise PublicationError("Cross-bound publish capability")
            if total > cap.max_publish_bytes:
                raise PublicationError("Publish byte limit exceeded")
        # Validation alone does not authorize any external storage operation.
        if self._publish_cap is not None and not self._publish_cap.is_valid:
            reasons = []
            if self._publish_cap.is_expired:
                reasons.append("expired")
            if self._publish_cap.consumed:
                reasons.append("already consumed")
            if self._publish_cap.cancelled:
                reasons.append("cancelled")
            raise PublicationError(f"Publish capability invalid: {', '.join(reasons)}")

        return output

    def collect_scip_files(
        self,
        output_dir: str,
        validated_manifest: ParseOutputManifest,
    ) -> dict[str, str]:
        """Collect validated .scip file paths keyed by language.

        Returns a dict of {language: absolute_file_path} for successful
        languages with validated output.  These paths are safe to read
        after validation; they do not confer external publication authority.
        """
        verified_dir = os.path.join(output_dir, ".verified")
        os.mkdir(verified_dir, 0o700)
        scip_files = {}
        for index, (language, body) in enumerate(self._validated_bytes.items()):
            path = os.path.join(verified_dir, f"{index}.scip")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
            with os.fdopen(fd, "wb") as output:
                output.write(body)
            scip_files[language] = path
        return scip_files
