"""Prove exact source archive hashes through Debian's signed archive index."""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

tools = Path(__file__).parent
lock = json.loads((tools / "source-lock.json").read_text())
keyring = Path("/usr/share/keyrings/debian-archive-keyring.gpg")
output = Path("/out/source-authentication")
output.mkdir(parents=True, exist_ok=True)
for index in Path("/var/lib/apt/lists").glob("*_main_source_Sources*"):
    raw = subprocess.check_output(["/usr/lib/apt/apt-helper", "cat-file", str(index)])
    stanza = next((entry for entry in raw.decode().split("\n\n")
                   if entry.startswith("Package: openssh\n")
                   and f"\nVersion: {lock['version']}\n" in entry), None)
    if stanza is None:
        continue
    release = index.with_name(index.name.split("_main_source_Sources")[0] + "_InRelease")
    signature = subprocess.run(
        ["gpgv", "--keyring", str(keyring), "--status-fd", "1", str(release)],
        check=True, capture_output=True, text=True,
    )
    if any(f"[GNUPG:] {bad}" in signature.stdout for bad in
           ("EXPKEYSIG", "EXPSIG", "REVKEYSIG", "BADSIG", "ERRSIG", "KEYEXPIRED")):
        raise SystemExit("Debian archive signature has an invalid/expired signer")
    digest = hashlib.sha256(raw).hexdigest()
    expected_line = rf"^\s+{digest}\s+{len(raw)}\s+main/source/Sources$"
    if not re.search(expected_line, release.read_text(), re.MULTILINE):
        raise SystemExit("Sources content is not bound to signed InRelease")
    sha_section = stanza.split("Checksums-Sha256:\n", 1)[1]
    sha_section = re.split(r"\n[^ ]", sha_section, maxsplit=1)[0]
    for name, expected in lock["source_sha256"].items():
        if not re.search(rf"^ {expected}\s+\d+\s+{re.escape(name)}$", sha_section, re.MULTILINE):
            raise SystemExit(f"Source archive not bound to authenticated Sources: {name}")
    for path in (index, release, keyring):
        shutil.copyfile(path, output / path.name)
    receipt = {
        "source_package": "openssh", "version": lock["version"],
        "trust_anchor": "Debian archive keyring from immutable builder base",
        "keyring_sha256": hashlib.sha256(keyring.read_bytes()).hexdigest(),
        "InRelease_sha256": hashlib.sha256(release.read_bytes()).hexdigest(),
        "Sources_uncompressed_sha256": digest, "Sources_uncompressed_size": len(raw),
        "Sources_cache_file": index.name,
        "Sources_cache_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
        "signature_status": signature.stdout, "source_stanza": stanza,
        "authenticated_archive_sha256": lock["source_sha256"],
    }
    (output / "verified-source-chain.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"source_authentication": "passed", "source": lock["version"],
                      "Sources_sha256": digest}))
    break
else:
    raise SystemExit("Exact source version absent from authenticated Debian indexes")
