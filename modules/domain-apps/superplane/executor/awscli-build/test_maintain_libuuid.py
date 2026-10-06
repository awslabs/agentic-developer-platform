"""Byte/ELF validation failures use harmless synthetic files only."""

import hashlib
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "maintain_libuuid", Path(__file__).with_name("maintain_libuuid.py")
)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def payload():
    return b"\x7fELF\x02\x01" + b"\0" * 12 + b"\x3e\x00" + b"harmless fixture"


def test_exact_amd64_payload_accepted(tmp_path):
    path = tmp_path / "libuuid.so.1"
    data = payload()
    path.write_bytes(data)
    assert M.checked_payload(path, hashlib.sha256(data).hexdigest()) == data


def test_changed_library_bytes_refused(tmp_path):
    path = tmp_path / "libuuid.so.1"
    path.write_bytes(payload() + b"changed")
    with pytest.raises(ValueError, match="differ from the reviewed lock"):
        M.checked_payload(path, hashlib.sha256(payload()).hexdigest())


def test_symlink_substitution_refused(tmp_path):
    original = tmp_path / "original"
    original.write_bytes(payload())
    link = tmp_path / "libuuid.so.1"
    link.symlink_to(original)
    with pytest.raises(ValueError, match="regular library file"):
        M.checked_payload(link, hashlib.sha256(payload()).hexdigest())


@pytest.mark.parametrize("data", [b"not an ELF library", payload()[:18] + b"\xb7\x00"])
def test_authenticated_wrong_file_type_or_architecture_refused(tmp_path, data):
    path = tmp_path / "libuuid.so.1"
    path.write_bytes(data)
    with pytest.raises(ValueError, match="amd64 ELF64"):
        M.checked_payload(path, hashlib.sha256(data).hexdigest())
