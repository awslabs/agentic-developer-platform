"""Encrypt original native recovery state for transfer to a fresh private root.

The caller must close and quiesce the original runner before sealing its state.
The protected workflow supplies a 32-byte key separately from retained evidence.
This module preserves authority; only the recovery consumer may use it. Requires
cryptography==50.0.0, matching the gateway's existing dependency lock.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath
from uuid import UUID


SCHEMA = "atlassian-recovery-archive/v1"
MAX_ENTRIES = 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_JSON_BYTES = 12 * 1024 * 1024
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _decode(value):
    def unique(pairs):
        result = {}
        for name, item in pairs:
            if name in result:
                raise ValueError("Ambiguous recovery archive")
            result[name] = item
        return result

    def invalid(_):
        raise ValueError("Invalid recovery archive number")

    return json.loads(value, object_pairs_hook=unique, parse_constant=invalid)


def _binding(binding, key):
    fields = {
        "client",
        "run_id",
        "owner_id",
        "gateway_revision",
        "gateway_sha256",
        "transport_sha256",
        "plan_sha256",
        "original_calls",
    }
    if (
        type(key) is not bytes
        or len(key) != 32
        or type(binding) is not dict
        or set(binding) != fields
        or type(binding["client"]) is not str
        or binding["client"] not in {"claude-code", "codex"}
        or any(
            type(binding[name]) is not str or not 0 < len(binding[name]) <= 128
            for name in ("run_id", "owner_id")
        )
        or any(
            type(binding[name]) is not str or not re.fullmatch(pattern, binding[name])
            for name, pattern in (
                ("gateway_revision", r"[0-9a-f]{40}"),
                ("gateway_sha256", r"[0-9a-f]{64}"),
                ("transport_sha256", r"[0-9a-f]{64}"),
                ("plan_sha256", r"[0-9a-f]{64}"),
            )
        )
        or type(binding["original_calls"]) is not dict
        or not 0 < len(binding["original_calls"]) <= 64
    ):
        raise ValueError("Original recovery binding and protected archive key required")
    for operation, call in binding["original_calls"].items():
        if (
            type(operation) is not str
            or str(UUID(operation)) != operation
            or type(call) is not dict
            or set(call) != {"slot", "request_id", "request_value_sha256"}
            or type(call["slot"]) is not str
            or not re.fullmatch(r"call:[0-9]+", call["slot"])
            or type(call["request_id"]) is not str
            or str(UUID(call["request_id"])) != call["request_id"]
            or type(call["request_value_sha256"]) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", call["request_value_sha256"])
        ):
            raise ValueError("Original native call inventory required")
    return _canonical({"schema": SCHEMA, "binding": binding})


def _private_directory(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("A direct private recovery directory is required")
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    details = os.fstat(descriptor)
    if details.st_uid != os.getuid() or details.st_mode & 0o077:
        os.close(descriptor)
        raise ValueError("A direct private recovery directory is required")
    return path, descriptor


def _path(value):
    if (
        type(value) is not str
        or not 0 < len(value) <= 512
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("Invalid recovery archive path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or not path.parts
        or len(path.parts) > 32
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise ValueError("Invalid recovery archive path")
    return path


def _identity(details):
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _snapshot(descriptor):
    entries, total = [], 0

    def visit(directory, prefix):
        nonlocal total
        before = os.fstat(directory)
        for name in sorted(os.listdir(directory)):
            relative = str(_path(prefix + name))
            if len(entries) >= MAX_ENTRIES:
                raise ValueError("Recovery archive inventory exceeds its bound")
            details = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (
                details.st_uid != os.getuid()
                or stat.S_ISREG(details.st_mode)
                and details.st_mode & 0o022
                or details.st_mode & (stat.S_ISUID | stat.S_ISGID)
            ):
                raise ValueError("Recovery files must remain owned and protected")
            if stat.S_ISDIR(details.st_mode):
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                try:
                    if _identity(os.fstat(child)) != _identity(details):
                        raise ValueError("Recovery directory changed during capture")
                    entries.append({"path": relative, "kind": "directory"})
                    visit(child, relative + "/")
                finally:
                    os.close(child)
            elif stat.S_ISREG(details.st_mode) and details.st_nlink == 1:
                if (
                    details.st_size > MAX_FILE_BYTES
                    or total + details.st_size > MAX_TOTAL_BYTES
                ):
                    raise ValueError("Recovery archive content exceeds its bound")
                handle = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                with os.fdopen(handle, "rb") as stream:
                    if _identity(os.fstat(handle)) != _identity(details):
                        raise ValueError("Recovery file changed during capture")
                    data = stream.read(MAX_FILE_BYTES + 1)
                    if len(data) != details.st_size or _identity(
                        os.fstat(handle)
                    ) != _identity(details):
                        raise ValueError("Recovery file changed during capture")
                total += len(data)
                entries.append(
                    {
                        "path": relative,
                        "kind": "file",
                        "executable": bool(details.st_mode & stat.S_IXUSR),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "data": base64.b64encode(data).decode(),
                    }
                )
            else:
                raise ValueError(
                    "Recovery archives permit only ordinary files and directories"
                )
        if _identity(os.fstat(directory)) != _identity(before):
            raise ValueError("Recovery directory changed during capture")

    visit(descriptor, "")
    return {"version": 1, "entries": entries}


def _checked_entries(snapshot, binding):
    if (
        type(snapshot) is not dict
        or set(snapshot) != {"version", "entries"}
        or type(snapshot["version"]) is not int
        or snapshot["version"] != 1
        or type(snapshot["entries"]) is not list
        or not 0 < len(snapshot["entries"]) <= MAX_ENTRIES
    ):
        raise ValueError("Invalid recovery snapshot")
    seen, directories, files, total = set(), {"."}, {}, 0
    for entry in snapshot["entries"]:
        if type(entry) is not dict or not {"path", "kind"} <= entry.keys():
            raise ValueError("Invalid recovery snapshot entry")
        path = _path(entry["path"])
        if str(path) in seen:
            raise ValueError("Duplicate recovery snapshot path")
        seen.add(str(path))
        if entry["kind"] == "directory" and set(entry) == {"path", "kind"}:
            directories.add(str(path))
        elif (
            entry["kind"] == "file"
            and set(entry) == {"path", "kind", "executable", "sha256", "data"}
            and type(entry["executable"]) is bool
            and type(entry["data"]) is str
            and len(entry["data"]) <= (MAX_FILE_BYTES + 2) // 3 * 4
        ):
            data = base64.b64decode(entry["data"], validate=True)
            total += len(data)
            if (
                len(data) > MAX_FILE_BYTES
                or total > MAX_TOTAL_BYTES
                or hashlib.sha256(data).hexdigest() != entry["sha256"]
            ):
                raise ValueError("Recovery snapshot content differs")
            files[str(path)] = (data, entry["executable"])
        else:
            raise ValueError("Invalid recovery snapshot entry")
    if any(str(PurePosixPath(name).parent) not in directories for name in seen):
        raise ValueError("Recovery snapshot directory inventory differs")
    try:
        runtime = _decode(files["native-runtime.json"][0])
        transport = _decode(files["native-transport.json"][0])
        reference = transport["reference"]
        native_run = reference["run_id"]
        if type(native_run) is not str or str(UUID(native_run)) != native_run:
            raise ValueError("Invalid original native session")
        journal = _decode(files[native_run + "/attempts.json"][0])
        calls = [entry for entry in journal["attempts"] if entry["kind"] == "call"]
        inventory = {
            entry["operation_id"]: {
                key: entry[key]
                for key in ("slot", "request_id", "request_value_sha256")
            }
            for entry in calls
        }
        if (
            any(
                runtime[name] != binding[name]
                for name in (
                    "client",
                    "owner_id",
                    "run_id",
                    "gateway_revision",
                    "transport_sha256",
                )
            )
            or _digest(transport) != binding["transport_sha256"]
            or reference["source_revision"] != binding["gateway_revision"]
            or _digest(reference["gateway_origin"]) != binding["gateway_sha256"]
            or journal["binding_sha256"] != binding["transport_sha256"]
            or len(calls) != len(inventory)
            or inventory != binding["original_calls"]
        ):
            raise ValueError("Recovery snapshot belongs to another original admission")
    except (KeyError, TypeError) as error:
        raise ValueError(
            "Complete original native recovery records required"
        ) from error
    return directories, files


def seal(root, *, binding, key):
    """Preserve a quiescent original client root; never export its plaintext."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    associated = _binding(binding, key)
    _, descriptor = _private_directory(root)
    try:
        snapshot = _snapshot(descriptor)
    finally:
        os.close(descriptor)
    _checked_entries(snapshot, binding)
    plaintext = _canonical(snapshot)
    if len(plaintext) > MAX_JSON_BYTES:
        raise ValueError("Recovery snapshot exceeds its bound")
    nonce = os.urandom(12)
    envelope = {
        "schema": SCHEMA,
        "binding_sha256": _digest(binding),
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(
            AESGCM(key).encrypt(nonce, plaintext, associated)
        ).decode(),
    }
    payload = _canonical(envelope)
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ValueError("Encrypted recovery archive exceeds its bound")
    return payload


def restore(payload, *, parent, binding, key):
    """Authenticate everything before creating a new root; never replace a root."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    associated = _binding(binding, key)
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_ARCHIVE_BYTES:
        raise ValueError("Bounded encrypted recovery archive required")
    envelope = _decode(payload)
    if (
        type(envelope) is not dict
        or set(envelope) != {"schema", "binding_sha256", "nonce", "ciphertext"}
        or envelope["schema"] != SCHEMA
        or envelope["binding_sha256"] != _digest(binding)
        or type(envelope["nonce"]) is not str
        or type(envelope["ciphertext"]) is not str
    ):
        raise ValueError("Encrypted recovery archive binding differs")
    nonce = base64.b64decode(envelope["nonce"], validate=True)
    ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
    if len(nonce) != 12 or len(ciphertext) > MAX_JSON_BYTES + 16:
        raise ValueError("Invalid recovery archive ciphertext")
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, associated)
    except InvalidTag as error:
        raise ValueError("Recovery archive authentication failed") from error
    directories, files = _checked_entries(_decode(plaintext), binding)
    parent, descriptor = _private_directory(parent)
    try:
        root = Path(tempfile.mkdtemp(prefix="restored-native-", dir=parent))
        try:
            for name in sorted(
                directories - {"."}, key=lambda value: (value.count("/"), value)
            ):
                (root / name).mkdir(mode=0o700)
            for name, (data, executable) in files.items():
                handle = os.open(
                    root / name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o700 if executable else 0o600,
                )
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            for name in sorted(
                directories, key=lambda value: value.count("/"), reverse=True
            ):
                handle = os.open(
                    root / name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
                try:
                    os.fsync(handle)
                finally:
                    os.close(handle)
            os.fsync(descriptor)
            return root
        except BaseException:
            shutil.rmtree(root)
            raise
    finally:
        os.close(descriptor)
