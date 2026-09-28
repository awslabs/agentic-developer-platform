"""Offline JOSE signing, rejection and exact tarfile backport acceptance."""

import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jose import jwt, JWTError
from jose.backends import ECKey
from jose.backends.cryptography_backend import CryptographyECKey

assert importlib.util.find_spec("ecdsa") is None
assert ECKey is CryptographyECKey
for algorithm, key, public in (
    (
        "HS256",
        "synthetic-long-signing-key-for-offline-tests",
        "synthetic-long-signing-key-for-offline-tests",
    ),
    ("ES256", (ec_key := ec.generate_private_key(ec.SECP256R1())), ec_key.public_key()),
    (
        "RS256",
        (rsa_key := rsa.generate_private_key(public_exponent=65537, key_size=2048)),
        rsa_key.public_key(),
    ),
):
    token = jwt.encode(
        {"sub": "fixture", "aud": "offline", "exp": 4102444800},
        key,
        algorithm=algorithm,
    )
    assert (
        jwt.decode(token, public, algorithms=[algorithm], audience="offline")["sub"]
        == "fixture"
    )
    try:
        jwt.decode(token, public, algorithms=[algorithm], audience="wrong-audience")
    except JWTError:
        pass
    else:
        raise AssertionError("wrong audience accepted")
    header, payload, signature = token.split(".")
    invalid = ".".join(
        (header, payload, ("A" if signature[0] != "A" else "B") + signature[1:])
    )
    try:
        jwt.decode(invalid, public, algorithms=[algorithm], audience="offline")
    except JWTError:
        pass
    else:
        raise AssertionError("invalid signature accepted")
manifest = json.loads(
    Path("/opt/adp-security/api-high/tarfile-manifest.json").read_text()
)
assert (
    hashlib.sha256(Path(tarfile.__file__).read_bytes()).hexdigest() == manifest["after"]
)
for filter_name in ("data", "tar"):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dest = root / "dest"
        dest.mkdir()
        (root / "escape").write_bytes(b"outside")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            entry = tarfile.TarInfo("a/escape")
            entry.size = 5
            archive.addfile(entry, io.BytesIO(b"decoy"))
            entry = tarfile.TarInfo("a/b/s")
            entry.type, entry.linkname = tarfile.SYMTYPE, "../escape"
            archive.addfile(entry)
            entry = tarfile.TarInfo("s")
            entry.type, entry.linkname = tarfile.LNKTYPE, "a/b/s"
            archive.addfile(entry)
        buffer.seek(0)
        with tarfile.open(fileobj=buffer) as archive:
            archive.extractall(dest, filter=filter_name)
        assert not (dest / "s").is_symlink()
        assert (dest / "s").read_bytes() == b"decoy"
        assert (root / "escape").read_bytes() == b"outside"
print(
    json.dumps(
        {
            "HS256": "passed",
            "ES256": "passed",
            "RS256": "passed",
            "invalid_tokens": "rejected",
            "tarfile": manifest["after"],
        }
    )
)
