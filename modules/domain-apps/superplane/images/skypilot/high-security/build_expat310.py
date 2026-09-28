"""Backport CPython's 16-byte Expat salt interface to the pinned 3.10 ABI.

Based on CPython commit 24b8f12544468e4cedf5bfbe25442fcd495391e4.
Both adapters are rebuilt together. The interpreter already initializes all
24 bytes of _Py_HashSecret.uc; the first 16 bytes match the upstream union alias.
"""

import hashlib
import json
import os
import shlex
import subprocess
import sys
import sysconfig
import tarfile
from pathlib import Path

assert sys.version_info[:3] == (3, 10, 21)
archive = Path("/tmp/Python-3.10.21.tar.xz")
assert (
    hashlib.sha256(archive.read_bytes()).hexdigest()
    == "a0da1e72132e950154eca0f6f47d5db828454700de20e5113667940d81e0db04"
)
with tarfile.open(archive) as source:
    source.extractall("/tmp/expat-source", filter="data")
root = Path("/tmp/expat-source/Python-3.10.21")
changed = {}


def replace(file, before, after):
    path = root / file
    raw = path.read_text()
    assert raw.count(before) == 1, file
    path.write_text(raw.replace(before, after))
    changed[file] = hashlib.sha256(path.read_bytes()).hexdigest()


replace(
    "Include/pyexpat.h",
    "    /* always add new stuff to the end! */",
    "    XML_Bool (*SetHashSalt16Bytes)(XML_Parser parser, const unsigned char entropy[16]);\n    /* always add new stuff to the end! */",
)
replace(
    "Modules/pyexpat.c",
    "XML_SetHashSalt(self->itself,\n                    (unsigned long)_Py_HashSecret.expat.hashsalt);",
    "XML_SetHashSalt16Bytes(self->itself, _Py_HashSecret.uc);",
)
replace(
    "Modules/pyexpat.c",
    "    capi.SetHashSalt = XML_SetHashSalt;",
    "    capi.SetHashSalt = XML_SetHashSalt;\n    capi.SetHashSalt16Bytes = XML_SetHashSalt16Bytes;",
)
replace(
    "Modules/_elementtree.c",
    "EXPAT(SetHashSalt)(self->parser,\n                           (unsigned long)_Py_HashSecret.expat.hashsalt);",
    "EXPAT(SetHashSalt16Bytes)(self->parser, _Py_HashSecret.uc);",
)
out = Path("/out")
(out / "modules").mkdir(parents=True, exist_ok=True)
headers = out / "headers"
headers.mkdir()
(headers / "pyexpat.h").write_bytes((root / "Include/pyexpat.h").read_bytes())
manifest = {
    "python_version": "3.10.21",
    "python_source_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    "soabi": sysconfig.get_config_var("SOABI"),
    "expat_version": "2.8.5",
    "entropy_backport": "24b8f12544468e4cedf5bfbe25442fcd495391e4",
    "source_sha256": changed,
    "modules": [],
}
for name in ("pyexpat", "_elementtree"):
    target = out / "modules" / (name + sysconfig.get_config_var("EXT_SUFFIX"))
    cmd = shlex.split(sysconfig.get_config_var("LDSHARED")) + shlex.split(
        sysconfig.get_config_var("CFLAGS")
    )
    cmd += [
        "-fPIC",
        "-I" + str(headers),
        "-I" + sysconfig.get_path("include"),
        "-I" + str(root / "Modules"),
        str(root / "Modules" / (name + ".c")),
        "-o",
        str(target),
    ]
    if name == "pyexpat":
        cmd += ["-lexpat"]
    subprocess.run(cmd, check=True)
    manifest["modules"].append(
        {"file": target.name, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    )
env = dict(os.environ, PYTHONPATH=str(out / "modules") + ":" + str(root / "Lib"))
subprocess.run(
    [
        sys.executable,
        "-c",
        'import pyexpat,xml.etree.ElementTree as E;assert pyexpat.EXPAT_VERSION=="expat_2.8.5";assert E.fromstring("<r>ok</r>").text=="ok"',
    ],
    env=env,
    check=True,
)
subprocess.run(
    [
        sys.executable,
        "-m",
        "test",
        "-j1",
        "test_pyexpat",
        "test_xml_etree",
        "test_xml_etree_c",
    ],
    env=env,
    check=True,
)
(out / "python-expat-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
(out / "PSF-LICENSE").write_bytes((root / "LICENSE").read_bytes())
