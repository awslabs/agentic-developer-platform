"""Private write-ahead checkpoints for idempotent Product admission only.

This never retries a provider Start. A lost Product response may only replay the
same endpoint, actor token, request body and Product idempotency key. A retained
acknowledgement is recovered by GET under fresh Product authority.
"""

from hashlib import sha256
import json
import os
from pathlib import Path
import stat


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class Journal:
    def __init__(self, directory, context, *, max_bytes=256 * 1024):
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("journal byte limit must be positive")
        self.max_bytes = max_bytes
        self.directory = Path(directory)
        if (not self.directory.is_absolute()
                or any(path.is_symlink() for path in (self.directory, *self.directory.parents))):
            raise ValueError("bootstrap journal requires a private absolute path")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        metadata = self.directory.stat()
        if not self.directory.is_dir() or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ValueError("bootstrap journal must remain private")
        self.retain("context.json", {"schema_version": 1, "context": context})

    def read(self, name):
        try:
            descriptor = os.open(self.directory / name, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or metadata.st_mode & 0o077 or metadata.st_size > self.max_bytes):
                raise ValueError("bootstrap checkpoint is unsafe")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                return json.load(stream)
        finally:
            os.close(descriptor)

    def retain(self, name, value):
        content = encoded(value)
        if len(content) > self.max_bytes:
            raise ValueError("bootstrap checkpoint exceeds its bound")
        try:
            descriptor = os.open(self.directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if self.read(name) != value:
                raise ValueError("bootstrap checkpoint conflicts with retained intent; reconcile original work")
            return
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def post(self, reader, selection, path, body, key, *, headers=None):
        if path not in {"binding-operations", "runs"}:
            raise ValueError("bootstrap journal supports only Product setup and admission")
        identity_field = "operation_id" if path == "binding-operations" else "run_id"
        descriptor = {"gateway_url": selection["gateway_url"], "scope": selection["scope"],
                      "path": path, "idempotency_key": key, "body": body}
        if headers is not None:
            if set(headers) != {"If-Match"} or not isinstance(headers["If-Match"], str):
                raise ValueError("bootstrap journal permits only the selected binding revision header")
            descriptor["headers"] = headers
        stem = sha256(encoded({"scope": selection["scope"], "path": path})).hexdigest()
        receipt = self.read(stem + "-ack.json")
        existing = self.read(stem + "-intent.json")
        token = os.environ.get(selection["token_env"])
        if not token:
            raise ValueError("bootstrap owner credential unavailable")
        intent = {"schema_version": 1, **descriptor, "actor_token_sha256": sha256(token.encode()).hexdigest()}
        if receipt is not None:
            if not isinstance(existing, dict) or any(existing.get(field) != value for field, value in descriptor.items()):
                raise ValueError("bootstrap acknowledgement differs from original intent")
            if not isinstance(receipt, dict) or set(receipt) != {identity_field} or not isinstance(receipt[identity_field], str):
                raise ValueError("bootstrap acknowledgement is invalid")
            from urllib.parse import quote

            current = reader.get(path + "/" + quote(receipt[identity_field], safe=""))
            if current.get(identity_field) != receipt[identity_field]:
                raise ValueError("bootstrap current authority returned another work identity")
            return current
        # Token binding prevents a changed principal from getting a fresh actor-
        # scoped idempotency namespace after an uncertain response.
        self.retain(stem + "-intent.json", intent)
        response = reader.client.post(reader.path + "/" + path, json=body,
                                      headers={"Idempotency-Key": key, **(headers or {})})
        response.raise_for_status()
        value = response.json()
        identity = value.get(identity_field)
        if not isinstance(identity, str) or not identity or len(identity) > 128:
            raise ValueError("bootstrap admitted response lacks a bounded work identity")
        self.retain(stem + "-ack.json", {identity_field: identity})
        return value
