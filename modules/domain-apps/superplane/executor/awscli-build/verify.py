"""Verify the pinned AWS CLI archive with the reviewed, renewed AWS key."""

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

FINGERPRINT = "FB5DB77FD5C118B80511ADA8A6310ACC4672475C"
KEY_SHA256 = "b3cef249c50f7e26254ffd91bc7453d7424247cc98c372840e70297060c0e146"
ZIP_SHA256 = "0c59444563f4df735eeb5481f6165f95dae546c33761760d8be9855d5cfe2d12"


def check_status(status):
    records = [
        line.split()[1:] for line in status.splitlines() if line.startswith("[GNUPG:] ")
    ]
    rejected = {
        "BADSIG",
        "ERRSIG",
        "EXPSIG",
        "EXPKEYSIG",
        "REVKEYSIG",
        "KEYEXPIRED",
        "SIGEXPIRED",
        "KEYREVOKED",
        "NO_PUBKEY",
        "NODATA",
        "FAILURE",
        "ERROR",
    }
    if any(record[0] in rejected for record in records):
        raise ValueError("AWS signature is expired, revoked, invalid, or unverifiable")
    good = [record for record in records if record[0] == "GOODSIG"]
    valid = [record for record in records if record[0] == "VALIDSIG"]
    if len(good) != 1 or len(valid) != 1 or valid[0][1] != FINGERPRINT:
        raise ValueError("Expected exactly one valid signature from the pinned AWS key")


def verify(archive, signature):
    key = Path(__file__).with_name("aws-signing.asc")
    if hashlib.sha256(key.read_bytes()).hexdigest() != KEY_SHA256:
        raise ValueError("AWS signing key bytes differ from the reviewed official key")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != ZIP_SHA256:
        raise ValueError("AWS CLI archive differs from the reviewed release")
    with tempfile.TemporaryDirectory(prefix="awscli-signature-") as key_home:
        gpg = ["gpg", "--no-options", "--homedir", key_home, "--batch"]
        identity = subprocess.run(
            gpg + ["--show-keys", "--with-colons", str(key)],
            check=True,
            capture_output=True,
            text=True,
        )
        fingerprints = [
            line.split(":")[9]
            for line in identity.stdout.splitlines()
            if line.startswith("fpr:")
        ]
        if fingerprints != [FINGERPRINT]:
            raise ValueError("Unexpected AWS signing key identity")
        subprocess.run(gpg + ["--import", str(key)], check=True, capture_output=True)
        result = subprocess.run(
            gpg + ["--status-fd", "1", "--verify", str(signature), str(archive)],
            check=True,
            capture_output=True,
            text=True,
        )
        check_status(result.stdout)
        print(result.stdout, end="")


if __name__ == "__main__":
    verify(Path(sys.argv[1]), Path(sys.argv[2]))
