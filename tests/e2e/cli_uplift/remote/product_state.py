"""Keep Product create intent and cleanup ownership in one private instance file."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

import common


class ProductState:
    def __init__(self, work_dir, run_id):
        self.directory = Path(work_dir)
        self.run_id = run_id
        common.require(
            self.directory.is_absolute()
            and self.directory.is_dir()
            and self.directory.stat().st_uid == os.getuid()
            and stat.S_IMODE(self.directory.stat().st_mode) == 0o700
            and isinstance(run_id, str)
            and bool(run_id),
            "Product state needs an owned, private evaluation directory",
        )
        self.path = self.directory / "product-ownership.json"

    def _read(self):
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return None
        common.require(
            stat.S_ISREG(metadata.st_mode)
            and stat.S_IMODE(metadata.st_mode) == 0o600
            and metadata.st_uid == os.getuid(),
            "Product ownership record is not a private evaluator file",
        )
        try:
            record = json.loads(self.path.read_text())
        except (OSError, ValueError):
            raise common.RemoteError("Product ownership record is unreadable") from None
        common.require(
            isinstance(record, dict)
            and record.get("run_id") == self.run_id
            and isinstance(record.get("document"), dict)
            and all(
                record.get(field) == record["document"].get(field)
                for field in (
                    "run_id",
                    "tenant_id",
                    "actor_id",
                    "source_revision",
                    "idempotency_key",
                )
            )
            and all(
                isinstance(record.get(field), str) and bool(record[field])
                for field in (
                    "tenant_id",
                    "actor_id",
                    "source_revision",
                    "idempotency_key",
                )
            ),
            "Product ownership record differs from its durable intent",
        )
        receipt = record["document"].get("receipt")
        common.require(
            (
                receipt is None
                and not any(field in record for field in ("product_id", "project_id"))
            )
            or (
                isinstance(receipt, dict)
                and all(
                    isinstance(receipt.get(field), str)
                    and bool(receipt[field])
                    and record.get(field) == receipt[field]
                    for field in ("product_id", "project_id")
                )
            ),
            "Product receipt differs from its cleanup ownership",
        )
        return record

    def load(self, key):
        common.require(
            key == f"lifecycle-{self.run_id}",
            "Product state key differs from the evaluation",
        )
        record = self._read()
        if record is None:
            return None, None
        return record["document"], {
            field: record[field]
            for field in (
                "run_id",
                "tenant_id",
                "actor_id",
                "source_revision",
                "idempotency_key",
                "product_id",
                "project_id",
            )
            if field in record
        }

    def save(self, key, document, manifest=None):
        common.require(
            key == f"lifecycle-{self.run_id}",
            "Product state key differs from the evaluation",
        )
        existing = self._read()
        common.require(
            isinstance(document, dict)
            and isinstance(manifest, dict)
            and manifest.get("run_id") == self.run_id
            and all(
                isinstance(manifest.get(field), str)
                and bool(manifest[field])
                and manifest[field] == document.get(field)
                for field in (
                    "run_id",
                    "tenant_id",
                    "actor_id",
                    "source_revision",
                    "idempotency_key",
                )
            )
            and (
                existing is None
                or all(
                    existing[field] == manifest[field]
                    for field in (
                        "run_id",
                        "tenant_id",
                        "actor_id",
                        "source_revision",
                        "idempotency_key",
                    )
                )
            ),
            "Product create intent differs from the original ownership",
        )
        receipt = document.get("receipt")
        common.require(
            (
                receipt is None
                and not any(field in manifest for field in ("product_id", "project_id"))
            )
            or (
                isinstance(receipt, dict)
                and all(
                    isinstance(receipt.get(field), str)
                    and bool(receipt[field])
                    and manifest.get(field) == receipt[field]
                    for field in ("product_id", "project_id")
                )
            ),
            "Product receipt differs from its cleanup ownership",
        )
        common.require(
            existing is None
            or all(
                field not in existing or existing[field] == (receipt or {}).get(field)
                for field in ("product_id", "project_id")
            ),
            "Product receipt differs from the original ownership",
        )
        record = {
            **manifest,
            "document": document,
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix=".product-ownership-", dir=self.directory
        )
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
