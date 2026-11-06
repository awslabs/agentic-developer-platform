"""Offline checks for root-only, secret-free host enrollment file delivery."""

import base64
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "image" / "write_enrollment.py"
SPEC = importlib.util.spec_from_file_location("dev_box_write_enrollment", MODULE_PATH)
WRITER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WRITER)
CHALLENGE = "a" * 43
PUBLIC_KEY = b"approved guest public key\n"


def test_writes_only_root_accessible_ephemeral_inputs_and_reruns(tmp_path):
    root = tmp_path / "adp"
    WRITER.write_enrollment(CHALLENGE, PUBLIC_KEY, root=root, trusted_uid=os.getuid())
    challenge_file, key_file = (root / name for name in ("dev-box-enrollment", "dev-box-public-key"))
    assert challenge_file.read_bytes() == CHALLENGE.encode()
    assert key_file.read_bytes() == PUBLIC_KEY
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in (challenge_file, key_file))
    assert not list(root.glob(".enrollment-*"))
    WRITER.write_enrollment(CHALLENGE, PUBLIC_KEY, root=root, trusted_uid=os.getuid())
    assert challenge_file.read_bytes() == CHALLENGE.encode() and key_file.read_bytes() == PUBLIC_KEY


@pytest.mark.parametrize("challenge,key", [("short", PUBLIC_KEY), (CHALLENGE + "\n", PUBLIC_KEY),
                                         (CHALLENGE, b""), (CHALLENGE, b"x" * 8193)])
def test_invalid_input_never_creates_files(tmp_path, challenge, key):
    root = tmp_path / "adp"
    with pytest.raises(ValueError, match="invalid host enrollment input"):
        WRITER.write_enrollment(challenge, key, root=root, trusted_uid=os.getuid())
    assert not root.exists()


@pytest.mark.parametrize("untrusted", ["symlink", "writable_dir", "old_symlink", "old_public_file"])
def test_refuses_untrusted_enrollment_paths(tmp_path, untrusted):
    root = tmp_path / "adp"
    if untrusted == "symlink":
        root.symlink_to(tmp_path, target_is_directory=True)
    else:
        root.mkdir()
        if untrusted == "writable_dir":
            root.chmod(0o777)
        elif untrusted == "old_symlink":
            (root / "dev-box-enrollment").symlink_to(tmp_path / "other")
        else:
            old = root / "dev-box-public-key"
            old.write_bytes(PUBLIC_KEY)
            old.chmod(0o644)
    with pytest.raises(ValueError, match="untrusted host enrollment"):
        WRITER.write_enrollment(CHALLENGE, PUBLIC_KEY, root=root, trusted_uid=os.getuid())


def test_interruption_cleans_temporary_input_without_exposing_it(tmp_path, monkeypatch):
    root = tmp_path / "adp"
    WRITER.write_enrollment(CHALLENGE, PUBLIC_KEY, root=root, trusted_uid=os.getuid())

    def interrupted(*_arguments):
        raise OSError("interrupted")

    monkeypatch.setattr(WRITER.os, "replace", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        WRITER.write_enrollment("b" * 43, b"replacement key", root=root, trusted_uid=os.getuid())
    assert (root / "dev-box-enrollment").read_bytes() == CHALLENGE.encode()
    assert (root / "dev-box-public-key").read_bytes() == PUBLIC_KEY
    assert not list(root.glob(".enrollment-*"))


def test_cli_accepts_only_stdin_and_never_echoes_secrets(monkeypatch, capsys):
    captured = []
    payload = json.dumps({"challenge": CHALLENGE, "public_key_b64": base64.b64encode(PUBLIC_KEY).decode()}).encode()
    monkeypatch.setattr(WRITER.os, "geteuid", lambda: 0)
    monkeypatch.setattr(WRITER, "write_enrollment", lambda *arguments: captured.append(arguments))
    monkeypatch.setattr(sys, "argv", ["write_enrollment.py"])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload)))
    WRITER.main()
    assert captured == [(CHALLENGE, PUBLIC_KEY)] and capsys.readouterr() == ("", "")
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload + b"unsafe")))
    with pytest.raises(SystemExit, match="Invalid host enrollment input"):
        WRITER.main()
    assert len(captured) == 1
    assert CHALLENGE not in capsys.readouterr().err


def sealed_challenge(root):
    return subprocess.run(
        ["openssl", "pkeyutl", "-encrypt", "-pubin", "-inkey", str(root / "dev-box-public-key"),
         "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256"],
        input=CHALLENGE.encode(), capture_output=True, check=True,
    ).stdout


def test_generated_key_accepts_only_sealed_challenge_and_reruns(tmp_path, monkeypatch, capsys):
    root = tmp_path / "adp"
    public_key = WRITER.generate_key(root=root, trusted_uid=os.getuid())
    assert public_key == WRITER.generate_key(root=root, trusted_uid=os.getuid())
    assert public_key == (root / "dev-box-public-key").read_bytes()
    assert (root / "dev-box-private-key").stat().st_mode & 0o777 == 0o600
    ciphertext = sealed_challenge(root)
    WRITER.receive_sealed(ciphertext, root=root, trusted_uid=os.getuid())
    assert (root / "dev-box-enrollment").read_bytes() == CHALLENGE.encode()
    assert not list(root.glob(".host-*-*"))
    assert capsys.readouterr() == ("", "")
    with pytest.raises(ValueError, match="host enrollment key changed"):
        WRITER.write_enrollment(CHALLENGE, b"different public key", root=root, trusted_uid=os.getuid())
    assert (root / "dev-box-public-key").read_bytes() == public_key


def test_parallel_generation_returns_same_host_key(tmp_path):
    root = tmp_path / "adp"
    with ThreadPoolExecutor(max_workers=2) as executor:
        keys = list(executor.map(lambda _: WRITER.generate_key(root=root, trusted_uid=os.getuid()), range(2)))
    assert keys[0] == keys[1] == (root / "dev-box-public-key").read_bytes()


def test_interrupted_publication_recovers_from_private_key(tmp_path, monkeypatch):
    root = tmp_path / "adp"
    original_replace = WRITER.os.replace

    def interrupted(source, destination):
        if destination == root / "dev-box-public-key":
            raise OSError("interrupted")
        return original_replace(source, destination)

    monkeypatch.setattr(WRITER.os, "replace", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        WRITER.generate_key(root=root, trusted_uid=os.getuid())
    assert (root / "dev-box-private-key").exists()
    assert not (root / "dev-box-public-key").exists()
    monkeypatch.setattr(WRITER.os, "replace", original_replace)
    recovered = WRITER.generate_key(root=root, trusted_uid=os.getuid())
    assert recovered == (root / "dev-box-public-key").read_bytes()
    assert not list(root.glob(".host-*-*"))
    WRITER.receive_sealed(sealed_challenge(root), root=root, trusted_uid=os.getuid())


def test_interrupted_generation_removes_orphaned_private_temp(tmp_path):
    root = tmp_path / "adp"
    root.mkdir()
    orphan = root / ".host-key-abandoned"
    orphan.write_bytes(b"incomplete private key")
    orphan.chmod(0o600)
    WRITER.generate_key(root=root, trusted_uid=os.getuid())
    assert not orphan.exists()


@pytest.mark.parametrize("untrusted", ["private_mode", "public_mode", "private_symlink", "mismatched_public", "missing_private"])
def test_generated_key_refuses_untrusted_or_mismatched_files(tmp_path, untrusted):
    root = tmp_path / "adp"
    WRITER.generate_key(root=root, trusted_uid=os.getuid())
    private_key, public_key = (root / name for name in ("dev-box-private-key", "dev-box-public-key"))
    if untrusted == "private_mode":
        private_key.chmod(0o644)
    elif untrusted == "public_mode":
        public_key.chmod(0o644)
    elif untrusted == "private_symlink":
        private_key.rename(root / "old-private-key")
        private_key.symlink_to(root / "old-private-key")
    elif untrusted == "mismatched_public":
        public_key.write_bytes(b"unrelated")
    else:
        private_key.unlink()
    with pytest.raises(ValueError, match="untrusted host enrollment|host enrollment key changed"):
        WRITER.generate_key(root=root, trusted_uid=os.getuid())
    with pytest.raises((ValueError, FileNotFoundError)):
        WRITER.receive_sealed(b"ciphertext", root=root, trusted_uid=os.getuid())


def test_tampered_sealed_input_does_not_write_challenge(tmp_path):
    root = tmp_path / "adp"
    WRITER.generate_key(root=root, trusted_uid=os.getuid())
    ciphertext = sealed_challenge(root)
    with pytest.raises(ValueError, match="sealed host enrollment input refused"):
        WRITER.receive_sealed(ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]), root=root, trusted_uid=os.getuid())
    assert not (root / "dev-box-enrollment").exists()


def test_untrusted_lock_blocks_key_generation(tmp_path):
    root = tmp_path / "adp"
    root.mkdir()
    (root / ".enrollment.lock").symlink_to(tmp_path / "other")
    with pytest.raises(OSError):
        WRITER.generate_key(root=root, trusted_uid=os.getuid())
    assert not (root / "dev-box-private-key").exists()


def test_sealed_cli_takes_only_stdin_and_never_prints_challenge(tmp_path, monkeypatch, capsys):
    root = tmp_path / "adp"
    WRITER.generate_key(root=root, trusted_uid=os.getuid())
    monkeypatch.setattr(WRITER.os, "geteuid", lambda: 0)
    original_receive_sealed = WRITER.receive_sealed
    monkeypatch.setattr(WRITER, "receive_sealed", lambda ciphertext: original_receive_sealed(
        ciphertext, root=root, trusted_uid=os.getuid()))
    monkeypatch.setattr(sys, "argv", ["write_enrollment.py", "--sealed"])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(base64.b64encode(sealed_challenge(root)))))
    WRITER.main()
    assert (root / "dev-box-enrollment").read_bytes() == CHALLENGE.encode()
    assert capsys.readouterr() == ("", "")


def test_rotation_requires_fenced_receipt_and_retired_key_digest(tmp_path):
    root = tmp_path / "adp"
    old_public = WRITER.generate_key(root=root, trusted_uid=os.getuid())
    old_digest = hashlib.sha256(old_public).hexdigest()
    old_ciphertext = sealed_challenge(root)
    WRITER.receive_sealed(old_ciphertext, root=root, trusted_uid=os.getuid())
    receipt = root / "crew-ready"
    receipt.write_text("active")
    with pytest.raises(ValueError, match="still ready"):
        WRITER.rotate_key(old_digest, root=root, trusted_uid=os.getuid())
    receipt.unlink()
    with pytest.raises(ValueError, match="key changed"):
        WRITER.rotate_key("0" * 64, root=root, trusted_uid=os.getuid())
    assert (root / "dev-box-enrollment").read_bytes() == CHALLENGE.encode()
    replacement = WRITER.rotate_key(old_digest, root=root, trusted_uid=os.getuid())
    assert replacement != old_public
    assert not (root / "dev-box-enrollment").exists()
    with pytest.raises(ValueError, match="sealed host enrollment input refused"):
        WRITER.receive_sealed(old_ciphertext, root=root, trusted_uid=os.getuid())
    assert not (root / "dev-box-enrollment").exists()
    assert WRITER.rotate_key(old_digest, root=root, trusted_uid=os.getuid()) == replacement
    assert (root / "dev-box-public-key").read_bytes() == replacement


def test_interrupted_rotation_recovers_without_accepting_old_ciphertext(tmp_path):
    root = tmp_path / "adp"
    old_public = WRITER.generate_key(root=root, trusted_uid=os.getuid())
    old_ciphertext = sealed_challenge(root)
    (root / "dev-box-public-key").unlink()
    with pytest.raises(FileNotFoundError):
        WRITER.receive_sealed(old_ciphertext, root=root, trusted_uid=os.getuid())
    replacement = WRITER.rotate_key(hashlib.sha256(old_public).hexdigest(), root=root, trusted_uid=os.getuid())
    assert replacement != old_public
    assert (root / "dev-box-public-key").read_bytes() == replacement


def test_rotation_retries_only_after_verified_key_retirement(tmp_path, monkeypatch):
    root = tmp_path / "adp"
    old_public = WRITER.generate_key(root=root, trusted_uid=os.getuid())
    old_digest = hashlib.sha256(old_public).hexdigest()
    with monkeypatch.context() as interrupted:
        def fail_generate(**_options):
            raise RuntimeError("interrupted")

        interrupted.setattr(WRITER, "generate_key", fail_generate)
        with pytest.raises(RuntimeError, match="interrupted"):
            WRITER.rotate_key(old_digest, root=root, trusted_uid=os.getuid())
    assert not (root / "dev-box-private-key").exists()
    assert (root / "dev-box-rotation").read_text() == old_digest
    replacement = WRITER.rotate_key(old_digest, root=root, trusted_uid=os.getuid())
    assert replacement != old_public
    assert WRITER.rotate_key(old_digest, root=root, trusted_uid=os.getuid()) == replacement
    (root / "dev-box-public-key").unlink()
    assert WRITER.rotate_key(old_digest, root=root, trusted_uid=os.getuid()) == replacement


def test_rotation_refuses_missing_key_without_prior_retirement(tmp_path):
    root = tmp_path / "adp"
    old_public = WRITER.generate_key(root=root, trusted_uid=os.getuid())
    (root / "dev-box-private-key").unlink()
    (root / "dev-box-public-key").unlink()
    with pytest.raises(ValueError, match="key changed"):
        WRITER.rotate_key(hashlib.sha256(old_public).hexdigest(), root=root, trusted_uid=os.getuid())


def test_sealed_writer_refuses_rotation_gap_even_with_old_decryption(tmp_path, monkeypatch):
    root = tmp_path / "adp"
    WRITER.generate_key(root=root, trusted_uid=os.getuid())
    ciphertext = sealed_challenge(root)
    original_write = WRITER.write_enrollment

    def retire_before_write(challenge, public_key, **options):
        (root / "dev-box-public-key").unlink()
        (root / "dev-box-private-key").unlink()
        return original_write(challenge, public_key, **options)

    monkeypatch.setattr(WRITER, "write_enrollment", retire_before_write)
    with pytest.raises(ValueError, match="key changed"):
        WRITER.receive_sealed(ciphertext, root=root, trusted_uid=os.getuid())
    assert not (root / "dev-box-enrollment").exists()
