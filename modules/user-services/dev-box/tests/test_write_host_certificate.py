"""Offline checks for box-owned certificate generation."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "write_host_certificate", Path(__file__).resolve().parents[1] / "image" / "write_host_certificate.py",
)
WRITER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WRITER)


def authority(tmp_path, address="10.2.3.4", generation=3):
    path = tmp_path / "authority.json"
    path.write_text(json.dumps({"private_ip": address, "lease_generation": generation}))
    return path


def test_private_key_and_certificate_are_created_only_on_box(tmp_path):
    source = authority(tmp_path)
    root = tmp_path / "host"
    first = WRITER.generate_certificate(authority_file=source, root=root, uid=os.getuid())
    assert len(first) == 64
    assert first == WRITER.generate_certificate(authority_file=source, root=root, uid=os.getuid())
    assert root.joinpath("server.key").stat().st_mode & 0o777 == 0o640
    assert b"PRIVATE KEY" in root.joinpath("server.key").read_bytes()
    assert "PRIVATE KEY" not in source.read_text()
    inspection = subprocess.run(["openssl", "x509", "-in", str(root / "server.pem"),
                                 "-noout", "-ext", "subjectAltName"], check=True, capture_output=True, text=True)
    assert "IP Address:10.2.3.4" in inspection.stdout
    source.write_text(json.dumps({"private_ip": "10.2.3.4", "lease_generation": 4}))
    assert first != WRITER.generate_certificate(authority_file=source, root=root, uid=os.getuid())


@pytest.mark.parametrize("address", ["8.8.8.8", "127.0.0.1", "169.254.0.1", "::1"])
def test_invalid_address_cannot_create_host_key(tmp_path, address):
    with pytest.raises(ValueError, match="RFC1918"):
        WRITER.generate_certificate(authority_file=authority(tmp_path, address),
                                    root=tmp_path / "host", uid=os.getuid())
    assert not (tmp_path / "host").exists()


def test_untrusted_authority_and_tampered_key_fail_closed(tmp_path):
    source = authority(tmp_path)
    root = tmp_path / "host"
    WRITER.generate_certificate(authority_file=source, root=root, uid=os.getuid())
    (root / "server.key").write_text("tampered")
    with pytest.raises((subprocess.CalledProcessError, ValueError)):
        WRITER.generate_certificate(authority_file=source, root=root, uid=os.getuid())
    source.chmod(0o666)
    with pytest.raises(ValueError, match="untrusted"):
        WRITER.generate_certificate(authority_file=source, root=root, uid=os.getuid())


def test_bootstrap_with_controller_verified_address_then_requires_matching_run_authority(tmp_path):
    root = tmp_path / "host"
    authority_file = tmp_path / "authority.json"
    fingerprint = WRITER.generate_certificate(
        authority_file=authority_file, root=root, uid=os.getuid(), private_ip="10.4.0.3", generation=7,
    )
    authority_file.write_text(json.dumps({"private_ip": "10.4.0.3", "lease_generation": 7}))
    assert fingerprint == WRITER.generate_certificate(authority_file=authority_file, root=root, uid=os.getuid())
    with pytest.raises(ValueError, match="changed"):
        WRITER.generate_certificate(authority_file=authority_file, root=root, uid=os.getuid(),
                                    private_ip="10.4.0.4", generation=7)
