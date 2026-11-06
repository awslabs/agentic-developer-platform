"""Smoke-check installed GitHub Lambda code in isolation, without AWS calls."""

import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def verify_archive(archive: Path) -> None:
    with tempfile.TemporaryDirectory() as directory:
        extracted = Path(directory)
        with zipfile.ZipFile(archive) as package:
            names = set(package.namelist())
            for required in (
                "handler.py",
                "common/github_actor_authority.py",
                "jwt/__init__.py",
                "cryptography/__init__.py",
                "cffi/__init__.py",
            ):
                if required not in names:
                    raise ValueError(f"GitHub Lambda is missing {required}")
            package.extractall(extracted)

        script = """
import pathlib
import sys
sys.path.insert(0, sys.argv[1])
from common import github_actor_authority
import handler
import jwt
import cryptography
import cffi
for module in (handler, github_actor_authority, jwt, cryptography, cffi):
    if not pathlib.Path(module.__file__).resolve().is_relative_to(sys.argv[1]):
        raise AssertionError('module escaped installed Lambda')
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                       serialization.PublicFormat.SubjectPublicKeyInfo)
token = jwt.encode({'sub': 'fixture'}, private, algorithm='RS256')
assert jwt.decode(token, public, algorithms=['RS256']) == {'sub': 'fixture'}
assert callable(handler.handler) and callable(github_actor_authority._account_id)
"""
        subprocess.run(
            [sys.executable, "-I", "-c", script, str(extracted)],
            cwd=extracted,
            check=True,
            timeout=30,
        )


if __name__ == "__main__":
    verify_archive(Path(sys.argv[1]))
    print("GitHub Lambda installed JWT/crypto and handler smoke passed")
