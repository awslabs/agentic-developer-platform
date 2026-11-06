"""Persistently distrust the exact compromised emSign root in Debian CA stores."""

import hashlib
import json
import re
import ssl
import subprocess
from pathlib import Path

FINGERPRINT = "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b"
ENTRY = "mozilla/emSign_ECC_Root_CA_-_G3.crt"


def fingerprints():
    result = {
        hashlib.sha256(cert).hexdigest()
        for cert in ssl.create_default_context().get_ca_certs(binary_form=True)
    }
    # OpenSSL loads hashed-directory certificates lazily. Checking the loaded
    # bundle alone can miss a certificate still available through that directory.
    directory = ssl.get_default_verify_paths().capath
    if directory:
        for path in Path(directory).iterdir():
            if not path.is_file():
                continue
            for pem in re.findall(
                r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
                path.read_text(errors="ignore"),
                re.DOTALL,
            ):
                result.add(hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest())
    return result


def main():
    source = Path("/usr/share/ca-certificates") / ENTRY
    config = Path("/etc/ca-certificates.conf")
    before = fingerprints()
    if source.exists():
        der = ssl.PEM_cert_to_DER_cert(source.read_text())
        if hashlib.sha256(der).hexdigest() != FINGERPRINT:
            raise RuntimeError(
                "Unexpected certificate identity; CA configuration unchanged"
            )
        # Keep the exclusion across future package refreshes. Do not delete the
        # package's source certificate or remove other emSign roots.
        lines = config.read_text().splitlines()
        lines = [
            "!" + ENTRY if line.strip() in {ENTRY, "!" + ENTRY} else line
            for line in lines
        ]
        if "!" + ENTRY not in lines:
            lines.append("!" + ENTRY)
        config.write_text("\n".join(lines) + "\n")
        subprocess.run(["update-ca-certificates", "--fresh"], check=True)
    after = fingerprints()
    if FINGERPRINT in after or after != before - {FINGERPRINT} or len(after) < 100:
        raise RuntimeError("System CA repair must remove only the compromised root")
    print(
        json.dumps(
            {
                "distrusted_sha256": FINGERPRINT,
                "other_roots_preserved": True,
                "system_roots": len(after),
            }
        )
    )


if __name__ == "__main__":
    main()
