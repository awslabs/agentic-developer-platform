"""Verify the backend embedded in a Terraform 1.9.8 saved plan and local init."""

from __future__ import annotations

import json
import os
import struct
import zipfile
from pathlib import Path

from workspace_ownership import WorkspaceOwnershipError
from workspace_identity import state_key


def _deny(message: str):
    raise WorkspaceOwnershipError(message)


def _fields(data: bytes):
    """Read protobuf wire fields; Terraform v1.9.8 planfile.proto Plan.backend=13."""
    offset = 0

    def varint():
        nonlocal offset
        result = 0
        for shift in range(0, 70, 7):
            if offset >= len(data):
                _deny("Truncated saved-plan backend encoding")
            byte = data[offset]
            offset += 1
            result |= (byte & 127) << shift
            if byte < 128:
                return result
        _deny("Invalid saved-plan backend integer")

    while offset < len(data):
        tag = varint()
        field, wire = tag >> 3, tag & 7
        if not field:
            _deny("Invalid saved-plan field")
        if wire == 0:
            value = varint()
        elif wire in (1, 2, 5):
            size = varint() if wire == 2 else (8 if wire == 1 else 4)
            if offset + size > len(data):
                _deny("Truncated saved-plan field")
            value = data[offset : offset + size]
            offset += size
        else:
            _deny("Unsupported saved-plan wire encoding")
        yield field, value


def _one(data: bytes, field: int) -> bytes:
    values = [value for number, value in _fields(data) if number == field]
    if len(values) != 1 or not isinstance(values[0], bytes):
        _deny("Saved plan has missing or duplicate backend fields")
    return values[0]


def _unpack(data: bytes):
    """Strict MessagePack subset for known backend values; unknown extensions deny."""
    offset = 0

    def take(size):
        nonlocal offset
        if size < 0 or offset + size > len(data):
            _deny("Truncated saved-plan backend value")
        value = data[offset : offset + size]
        offset += size
        return value

    def integer(size):
        return int.from_bytes(take(size), "big")

    def read(depth=0):
        if depth > 20:
            _deny("Saved-plan backend nesting exceeds the supported contract")
        tag = integer(1)
        if tag <= 127:
            return tag
        if tag >= 224:
            return tag - 256
        if tag == 192:
            return None
        if tag in (194, 195):
            return tag == 195
        if tag in (202, 203):
            return struct.unpack(
                ">f" if tag == 202 else ">d", take(4 if tag == 202 else 8)
            )[0]
        if 204 <= tag <= 211:
            sizes = (1, 2, 4, 8, 1, 2, 4, 8)
            return int.from_bytes(take(sizes[tag - 204]), "big", signed=tag >= 208)
        if 160 <= tag <= 191 or tag in (217, 218, 219):
            size = tag - 160 if tag <= 191 else integer({217: 1, 218: 2, 219: 4}[tag])
            return take(size).decode("utf-8")
        if 144 <= tag <= 159 or tag in (220, 221):
            count = tag - 144 if tag <= 159 else integer(2 if tag == 220 else 4)
            if count > 1000:
                _deny("Oversized backend array")
            return [read(depth + 1) for _ in range(count)]
        if 128 <= tag <= 143 or tag in (222, 223):
            count = tag - 128 if tag <= 143 else integer(2 if tag == 222 else 4)
            if count > 1000:
                _deny("Oversized backend map")
            result = {}
            for _ in range(count):
                key = read(depth + 1)
                if not isinstance(key, str) or key in result:
                    _deny("Invalid or duplicate backend key")
                result[key] = read(depth + 1)
            return result
        _deny("Unsupported saved-plan backend value; use the maintained toolchain")

    result = read()
    if offset != len(data):
        _deny("Trailing saved-plan backend data")
    return result


def _identity(kind, config, workspace, target):
    if kind != "s3" or workspace != "default" or not isinstance(config, dict):
        _deny("A scoped S3 backend and the default Terraform workspace are required")
    key = state_key(target)
    if config.get("key") != key or config.get("encrypt") is not True:
        _deny("Backend key or encryption does not match the selected workspace")
    for name in ("bucket", "region", "dynamodb_table"):
        if not isinstance(config.get(name), str) or not config[name]:
            _deny(f"Backend {name} is required")
    allowed = {
        "bucket",
        "region",
        "key",
        "encrypt",
        "dynamodb_table",
        "allowed_account_ids",
    }
    if any(value is not None and name not in allowed for name, value in config.items()):
        _deny(
            "Backend credentials, alternate endpoints, profiles and overrides are unsupported"
        )
    if config.get("allowed_account_ids") not in (None, [target["account_id"]]):
        _deny("Backend allowed account differs from the selected account")
    return {
        "type": kind,
        "workspace": workspace,
        **{
            name: config[name]
            for name in ("bucket", "region", "key", "encrypt", "dynamodb_table")
        },
    }


def initialized_backend(module: Path, target: dict) -> dict:
    if (
        os.environ.get("TF_DATA_DIR")
        or os.environ.get("TF_WORKSPACE", "default") != "default"
    ):
        _deny("Custom Terraform data directories/workspaces are unsupported")
    workspace_file = module / ".terraform/environment"
    workspace = (
        workspace_file.read_text().strip() if workspace_file.exists() else "default"
    )
    try:
        backend = json.loads((module / ".terraform/terraform.tfstate").read_text())[
            "backend"
        ]
        return _identity(backend["type"], backend["config"], workspace, target)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise WorkspaceOwnershipError("Missing or invalid initialized backend") from exc


def verify_backend(plan_file: Path, plan: dict, module: Path, target: dict) -> dict:
    if plan.get("terraform_version") != "1.9.8":
        _deny(
            "Saved backend verification requires the maintained Terraform 1.9.8 toolchain"
        )
    try:
        with zipfile.ZipFile(plan_file) as archive:
            if (
                archive.namelist().count("tfplan") != 1
                or archive.getinfo("tfplan").file_size > 16 * 1024 * 1024
            ):
                _deny("Invalid or oversized saved plan")
            backend = _one(archive.read("tfplan"), 13)
        embedded = _identity(
            _one(backend, 1).decode(),
            _unpack(_one(_one(backend, 2), 1)),
            _one(backend, 3).decode(),
            target,
        )
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        raise WorkspaceOwnershipError(
            "Cannot verify the saved plan's embedded backend"
        ) from exc
    if embedded != initialized_backend(module, target):
        _deny(
            "Initialized backend differs from the backend embedded in the reviewed artifact"
        )
    return embedded
