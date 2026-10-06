"""Ordinary compatibility checks on the installed offline maintenance candidate."""

import ctypes
import importlib.metadata
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile

import git
import sky
import ray
import OpenSSL
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
import urllib3
from urllib3.response import HTTPResponse

root = Path(__file__).resolve().parent
lock = json.loads((root / "artifact-lock.json").read_text())
for item in lock["packages"]:
    actual = subprocess.check_output(
        ["dpkg-query", "-W", "-f=${Version}", item["package"]],
        text=True,
    )
    assert actual == item["version"], (item["package"], actual)
for item in lock["wheels"]:
    assert importlib.metadata.version(item["package"]) == item["version"]
for name, version in lock["python_versions"].items():
    assert importlib.metadata.version(name) == version

context = ssl.create_default_context()
assert context.verify_mode == ssl.CERT_REQUIRED
assert context.check_hostname
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
message = b"ordinary SkyPilot compatibility check"
signature = key.sign(message, padding.PKCS1v15(), hashes.SHA256())
key.public_key().verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
assert HTTPResponse(body=b'{"ok": true}', status=200).json() == {"ok": True}
assert urllib3.util.parse_url("https://example.com:443/demo").host == "example.com"

pcre = ctypes.CDLL("libpcre2-8.so.0")
pcre.pcre2_compile_8.argtypes = [
    ctypes.c_char_p,
    ctypes.c_size_t,
    ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_size_t),
    ctypes.c_void_p,
]
pcre.pcre2_compile_8.restype = ctypes.c_void_p
pcre.pcre2_code_free_8.argtypes = [ctypes.c_void_p]
error, offset = ctypes.c_int(), ctypes.c_size_t()
pattern = b"^[a-z]+[0-9]+$"
compiled = pcre.pcre2_compile_8(
    pattern, len(pattern), 0, ctypes.byref(error), ctypes.byref(offset), None
)
assert compiled
pcre.pcre2_code_free_8(compiled)
with tempfile.TemporaryDirectory(prefix="adp-vendor-check-") as temporary:
    base = Path(temporary)
    repo = git.Repo.init(base / "repo")
    with repo.config_writer() as config:
        config.set_value("user", "name", "ADP compatibility")
        config.set_value("user", "email", "compatibility@example.invalid")
    file = Path(repo.working_tree_dir) / "hello.txt"
    file.write_text("hello\n")
    repo.index.add(["hello.txt"])
    commit = repo.index.commit("ordinary compatibility commit")
    assert commit.author.name == "ADP compatibility"
    assert repo.head.commit.hexsha == commit.hexsha
    subprocess.run(
        [
            sys.executable,
            "-m",
            "virtualenv",
            "--no-download",
            "--no-periodic-update",
            "--app-data",
            str(base / "app-data"),
            str(base / "venv"),
        ],
        check=True,
    )
    subprocess.run(
        [
            str(base / "venv/bin/python"),
            "-c",
            "import sys; assert sys.prefix != sys.base_prefix",
        ],
        check=True,
    )
    subprocess.run(
        [str(base / "venv/bin/python"), "-m", "pip", "--version"], check=True
    )
    subprocess.run(
        [
            "/bin/bash",
            "-c",
            'source "$1"; test "$VIRTUAL_ENV" = "$2"',
            "activation-check",
            str(base / "venv/bin/activate"),
            str(base / "venv"),
        ],
        check=True,
    )

assert sky.__version__ == "0.12.3"
assert ray.__version__ == "2.58.0"
assert OpenSSL.SSL.Context(OpenSSL.SSL.TLS_METHOD)
subprocess.run(
    [sys.executable, "-m", "pip", "check"],
    check=True,
    env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
)
print(
    json.dumps(
        {
            "result": "PASS",
            "checks": [
                "locked installed versions",
                "TLS certificate verification defaults",
                "cryptography RSA sign/verify using system OpenSSL",
                "urllib3 valid response/url",
                "PCRE ordinary pattern compilation",
                "GitPython local repository commit/read",
                "virtualenv offline creation and shell activation",
                "SkyPilot/Ray/pyOpenSSL import",
                "full installed dependency constraints",
            ],
            "openssl": ssl.OPENSSL_VERSION,
        },
        indent=2,
    )
)
