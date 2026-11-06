"""Original authority survives storage loss; substituted archives cannot restore."""

import base64
from copy import deepcopy
import hashlib
import json
import os
import shutil

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

from tests.e2e.tools import atlassian_recovery_archive as archive


NATIVE_RUN = "11111111-1111-4111-8111-111111111111"
OPERATION = "22222222-2222-4222-8222-222222222222"
REQUEST = "33333333-3333-4333-8333-333333333333"
SECRET = "owned-original-capability-must-never-appear-in-plaintext"


def write(root, name, data, *, executable=False):
    path = root / name
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o700 if executable else 0o600)


@pytest.fixture
def original(tmp_path):
    root = tmp_path / "original"
    root.mkdir(mode=0o700)
    transport = {
        "reference": {
            "run_id": NATIVE_RUN,
            "source_revision": "a" * 40,
            "gateway_origin": "https://gateway.example.test",
        },
        "access_token": "retained-original-access",
        "capability": SECRET,
    }
    call = {"slot": "call:0", "request_id": REQUEST, "request_value_sha256": "b" * 64}
    binding = {
        "client": "codex",
        "owner_id": "owner-one",
        "run_id": "campaign-one",
        "gateway_revision": "a" * 40,
        "gateway_sha256": archive._digest(transport["reference"]["gateway_origin"]),
        "transport_sha256": archive._digest(transport),
        "plan_sha256": "c" * 64,
        "original_calls": {OPERATION: call},
    }
    runtime = {
        key: binding[key]
        for key in (
            "client",
            "owner_id",
            "run_id",
            "gateway_revision",
            "transport_sha256",
        )
    }
    journal = {
        "binding_sha256": binding["transport_sha256"],
        "attempts": [
            {"kind": "call", "operation_id": OPERATION, "status": "attempted", **call}
        ],
    }
    for name, value in (
        ("native-transport.json", transport),
        ("native-runtime.json", runtime),
        (NATIVE_RUN + "/attempts.json", journal),
        ("operations.json", {"status": "pending"}),
        ("jira-fixture.json", {"owner_id": "owner-one", "cleanup_status": "pending"}),
    ):
        write(root, name, json.dumps(value).encode())
    write(
        root,
        NATIVE_RUN + "/.adp/bin/adp",
        b"#!/bin/sh\n# original installed CLI\n",
        executable=True,
    )
    (root / NATIVE_RUN / ".bedrock-gateway/tool-operations").mkdir(
        mode=0o700, parents=True
    )
    parent = tmp_path / "replacement"
    parent.mkdir(mode=0o700)
    return root, parent, binding, os.urandom(32)


def content(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_storage_loss_restores_exact_original_files_and_empty_store(original):
    root, parent, binding, key = original
    expected = content(root)
    payload = archive.seal(root, binding=binding, key=key)
    assert (
        SECRET.encode() not in payload
        and base64.b64encode(SECRET.encode()) not in payload
    )
    assert b"access_token" not in payload and base64.b64encode(key) not in payload
    shutil.rmtree(root)
    restored = archive.restore(payload, parent=parent, binding=binding, key=key)
    assert restored != root and restored.parent == parent
    assert not root.exists() and content(restored) == expected
    assert (restored / NATIVE_RUN / ".bedrock-gateway/tool-operations").is_dir()
    assert (restored / NATIVE_RUN / ".adp/bin/adp").stat().st_mode & 0o777 == 0o700
    assert all(
        path.stat().st_mode & 0o077 == 0 for path in (restored, *restored.rglob("*"))
    )
    other = archive.restore(payload, parent=parent, binding=binding, key=key)
    assert (
        other != restored
        and content(restored) == expected
        and content(other) == expected
    )


def test_each_capture_uses_a_fresh_nonce(original):
    root, _, binding, key = original
    first = archive.seal(root, binding=binding, key=key)
    second = archive.seal(root, binding=binding, key=key)
    assert first != second
    assert json.loads(first)["nonce"] != json.loads(second)["nonce"]


@pytest.mark.parametrize(
    "field",
    [
        "owner_id",
        "run_id",
        "client",
        "gateway_revision",
        "gateway_sha256",
        "transport_sha256",
        "plan_sha256",
        "original_calls",
    ],
)
def test_foreign_binding_is_rejected_before_writes(original, field):
    root, parent, binding, key = original
    payload = archive.seal(root, binding=binding, key=key)
    changed = deepcopy(binding)
    if field == "original_calls":
        changed[field][OPERATION]["request_value_sha256"] = "d" * 64
    elif field == "client":
        changed[field] = "claude-code"
    else:
        changed[field] = "d" * len(binding[field])
    with pytest.raises(ValueError):
        archive.restore(payload, parent=parent, binding=changed, key=key)
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize(
    "fault",
    ["key", "ciphertext", "nonce", "binding_digest", "duplicate_json", "truncated"],
)
def test_tampered_envelope_is_rejected_before_writes(original, fault):
    root, parent, binding, key = original
    payload = archive.seal(root, binding=binding, key=key)
    envelope = json.loads(payload)
    if fault == "key":
        key = os.urandom(32)
    elif fault in {"ciphertext", "nonce"}:
        encoded = bytearray(base64.b64decode(envelope[fault]))
        encoded[0] ^= 1
        envelope[fault] = base64.b64encode(encoded).decode()
        payload = json.dumps(envelope).encode()
    elif fault == "binding_digest":
        envelope["binding_sha256"] = "d" * 64
        payload = json.dumps(envelope).encode()
    elif fault == "duplicate_json":
        payload = b'{"schema":"' + archive.SCHEMA.encode() + b'",' + payload[1:]
    else:
        payload = payload[:20]
    with pytest.raises(ValueError):
        archive.restore(payload, parent=parent, binding=binding, key=key)
    assert list(parent.iterdir()) == []


def repack(payload, binding, key, mutate):
    envelope = json.loads(payload)
    associated = archive._binding(binding, key)
    plaintext = AESGCM(key).decrypt(
        base64.b64decode(envelope["nonce"]),
        base64.b64decode(envelope["ciphertext"]),
        associated,
    )
    snapshot = json.loads(plaintext)
    mutate(snapshot)
    nonce = os.urandom(12)
    envelope["nonce"] = base64.b64encode(nonce).decode()
    envelope["ciphertext"] = base64.b64encode(
        AESGCM(key).encrypt(nonce, archive._canonical(snapshot), associated)
    ).decode()
    return json.dumps(envelope).encode()


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/outside",
        "a/../../outside",
        "a//b",
        "a/./b",
        ".",
        "a\\b",
        "a\x00b",
    ],
)
def test_even_authenticated_unsafe_paths_never_restore(original, path):
    root, parent, binding, key = original
    payload = archive.seal(root, binding=binding, key=key)
    payload = repack(
        payload, binding, key, lambda snapshot: snapshot["entries"][0].update(path=path)
    )
    with pytest.raises(ValueError):
        archive.restore(payload, parent=parent, binding=binding, key=key)
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize(
    "fault", ["duplicate", "digest", "missing_directory", "foreign_runtime", "link"]
)
def test_authenticated_snapshot_still_requires_safe_complete_original_state(
    original, fault
):
    root, parent, binding, key = original
    payload = archive.seal(root, binding=binding, key=key)

    def mutate(snapshot):
        entries = snapshot["entries"]
        if fault == "duplicate":
            entries.append(deepcopy(entries[0]))
        elif fault == "missing_directory":
            entries.remove(
                next(entry for entry in entries if entry["path"] == NATIVE_RUN)
            )
        elif fault == "link":
            entries.append({"path": "link", "kind": "symlink", "target": "/outside"})
        else:
            entry = next(
                entry for entry in entries if entry["path"] == "native-runtime.json"
            )
            if fault == "digest":
                entry["sha256"] = "d" * 64
            else:
                runtime = json.loads(base64.b64decode(entry["data"]))
                runtime["owner_id"] = "another-owner"
                data = json.dumps(runtime).encode()
                entry.update(
                    data=base64.b64encode(data).decode(),
                    sha256=hashlib.sha256(data).hexdigest(),
                )

    with pytest.raises(ValueError):
        archive.restore(
            repack(payload, binding, key, mutate),
            parent=parent,
            binding=binding,
            key=key,
        )
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize(
    "fault",
    [
        "symlink",
        "parent_symlink",
        "hardlink",
        "fifo",
        "public_root",
        "writable_file",
        "oversize",
        "foreign_binding",
    ],
)
def test_capture_rejects_unsafe_or_foreign_source(original, fault, monkeypatch):
    root, _, binding, key = original
    if fault == "symlink":
        (root / "outside-link").symlink_to(root.parent)
    elif fault == "parent_symlink":
        link = root.parent / "root-link"
        link.symlink_to(root.parent, target_is_directory=True)
        root = link / root.name
    elif fault == "hardlink":
        os.link(root / "operations.json", root / "second-link")
    elif fault == "fifo":
        os.mkfifo(root / "fifo", mode=0o600)
    elif fault == "public_root":
        root.chmod(0o755)
    elif fault == "writable_file":
        (root / "operations.json").chmod(0o666)
    elif fault == "oversize":
        monkeypatch.setattr(archive, "MAX_FILE_BYTES", 8)
    else:
        binding["owner_id"] = "another-owner"
    with pytest.raises((ValueError, OSError)):
        archive.seal(root, binding=binding, key=key)


def test_public_restore_parent_is_rejected_without_writes(original):
    root, parent, binding, key = original
    payload = archive.seal(root, binding=binding, key=key)
    parent.chmod(0o755)
    with pytest.raises(ValueError):
        archive.restore(payload, parent=parent, binding=binding, key=key)
    assert list(parent.iterdir()) == []


def test_restore_write_failure_removes_only_its_own_partial_root(original, monkeypatch):
    root, parent, binding, key = original
    payload = archive.seal(root, binding=binding, key=key)
    unrelated = parent / "unrelated"
    unrelated.write_text("keep me")
    original_open = os.open

    def fail(path, flags, *args, **kwargs):
        if str(path).endswith("native-runtime.json") and flags & os.O_WRONLY:
            raise OSError("Injected storage failure")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(archive.os, "open", fail)
    with pytest.raises(OSError):
        archive.restore(payload, parent=parent, binding=binding, key=key)
    assert list(parent.iterdir()) == [unrelated]
    assert unrelated.read_text() == "keep me"
