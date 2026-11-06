"""Private, append-only Transform release identity and observation journal."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path


def canonical(value):
    def encode(item):
        if isinstance(item, bytes):
            return {"bytes_sha256": sha256(item).hexdigest(), "length": len(item)}
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError("unsupported observation value")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=encode).encode()


def digest(value):
    return sha256(canonical(value)).hexdigest()


def private_json(path):
    path = Path(path)
    if (not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents))
            or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size > 4 * 1024 * 1024):
        raise ValueError("Transform evidence must be bounded and private")
    return json.loads(path.read_bytes())


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_private(path, value):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)) or path.parent.stat().st_mode & 0o077:
        raise ValueError("Transform evidence directory must remain private")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as out:
        out.write(canonical(value) + b"\n")
        out.flush()
        os.fsync(out.fileno())
    fsync_directory(path.parent)


class ReleaseJournal:
    """One exact attempt/fixture per directory; no restored pass flags are trusted."""

    def __init__(self, directory, context, fixture_sha256, requirements_revision):
        self.directory = Path(directory)
        self.binding = {"context": deepcopy(context), "fixture_sha256": fixture_sha256,
                        "requirements_revision": requirements_revision}
        self.binding_sha256 = digest(self.binding)
        self.stream = None
        self.records = []

    @contextmanager
    def locked(self):
        root = self.directory
        if (not root.is_absolute() or any(p.is_symlink() for p in (root, *root.parents))
                or not root.is_dir() or root.stat().st_mode & 0o077):
            raise ValueError("Transform journal must remain outside the checkout in a private directory")
        fd = os.open(root / "release-journal.jsonl", os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a+") as stream:
            if os.fstat(stream.fileno()).st_size > 4 * 1024 * 1024:
                raise ValueError("Transform journal exceeds its bound")
            if os.fstat(stream.fileno()).st_mode & 0o077:
                raise ValueError("Transform journal permissions changed")
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Transform release is already running; retain its original journal") from None
            stream.seek(0)
            raw = stream.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024 or raw and not raw.endswith("\n"):
                raise ValueError("Transform journal is incomplete or exceeds its bound")
            self.records = [json.loads(line) for line in raw.splitlines()]
            previous = None
            for number, row in enumerate(self.records):
                if (set(row) != {"sequence", "previous_sha256", "binding_sha256", "kind", "value"}
                        or row["sequence"] != number or row["previous_sha256"] != previous
                        or row["binding_sha256"] != self.binding_sha256):
                    raise ValueError("Transform journal changed or belongs to another attempt/fixture")
                previous = digest(row)
            if self.records and (self.records[0]["kind"] != "binding" or self.records[0]["value"] != self.binding):
                raise ValueError("Transform journal identity differs from validated workflow inputs")
            self.stream = stream
            try:
                if not self.records:
                    self.append("binding", self.binding)
                yield self
            finally:
                self.stream = None

    def append(self, kind, value):
        if self.stream is None:
            raise ValueError("Transform observation requires the held journal lock")
        row = {"sequence": len(self.records), "previous_sha256": digest(self.records[-1]) if self.records else None,
               "binding_sha256": self.binding_sha256, "kind": kind, "value": value}
        encoded = canonical(row).decode()
        if (len(encoded.encode()) > 1024 * 1024 or len(self.records) >= 4096
                or os.fstat(self.stream.fileno()).st_size + len(encoded.encode()) + 1 > 4 * 1024 * 1024):
            raise ValueError("Transform journal observation exceeds its bound")
        self.stream.write(encoded + "\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        fsync_directory(self.directory)
        self.records.append(json.loads(encoded))
        return digest(self.records[-1])

    private_json = staticmethod(private_json)
    write_private = staticmethod(write_private)
