"""Replace CPython on its existing cp310 ABI, retaining all provider packages."""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


def install(root):
    assert sys.prefix == "/usr/local" and sys.version_info[:2] == (3, 10)
    source = root / "python-runtime"
    prefix = Path("/usr/local")
    # Replace mapped executables/libraries atomically. The invoking interpreter
    # can finish on its existing mappings while child processes use the new ones.
    for relative in ("bin/python3.10", "lib/libpython3.10.so.1.0", "lib/libpython3.so"):
        destination = prefix / relative
        temporary = destination.with_name(destination.name + ".adp-new")
        shutil.copy2(source / relative, temporary)
        temporary.replace(destination)
    stdlib = prefix / "lib/python3.10"
    for path in stdlib.iterdir():
        if path.name == "site-packages":
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    shutil.copytree(source / "lib/python3.10", stdlib, dirs_exist_ok=True)
    shutil.rmtree(prefix / "include/python3.10")
    shutil.copytree(source / "include/python3.10", prefix / "include/python3.10")
    subprocess.run(
        [
            str(prefix / "bin/python3.10"),
            "-c",
            """
import sys, ssl, sqlite3, bz2, lzma, ctypes, pyexpat
import xml.etree.ElementTree as ET
assert sys.version_info[:3] == (3, 10, 22)
assert pyexpat.EXPAT_VERSION == 'expat_2.8.5'
assert ET.fromstring('<root>valid</root>').text == 'valid'
import sky, cryptography, OpenSSL, grpc, numpy
assert sky.__version__ == '0.12.3'
""",
        ],
        check=True,
    )
    subprocess.run([str(prefix / "bin/python3.10"), "-m", "pip", "check"], check=True)
    out = prefix / "share/adp-python-runtime.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "python_version": "3.10.22",
                "base": "public.ecr.aws/docker/library/python:3.10.22-slim@sha256:6ff506466f8b1e981719b468cc0023b8ef000b983cdec0eafe119d01f379e31d",
                "python_source_sha256": "c61fe9b1a1a7ec2a0b0388e664187c378a5325a89e3606d220abf5bdfccb2543",
                "binary_sha256": hashlib.sha256(
                    (prefix / "bin/python3.10").read_bytes()
                ).hexdigest(),
                "site_packages_preserved": True,
            },
            indent=2,
        )
        + "\n"
    )
