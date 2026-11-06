"""Write ephemeral owner enrollment inputs received over a private local channel."""

import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


def trusted_directory(root: Path, trusted_uid: int) -> None:
    if root.is_symlink():
        raise ValueError("untrusted host enrollment directory")
    root.mkdir(mode=0o755, exist_ok=True)
    directory = root.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != trusted_uid or directory.st_mode & 0o022:
        raise ValueError("untrusted host enrollment directory")


def trusted_file(path: Path, trusted_uid: int) -> bytes:
    observed = path.lstat()
    if not stat.S_ISREG(observed.st_mode) or observed.st_uid != trusted_uid or observed.st_mode & 0o077:
        raise ValueError("untrusted host enrollment file")
    return path.read_bytes()


@contextmanager
def enrollment_lock(root: Path, trusted_uid: int):
    trusted_directory(root, trusted_uid)
    lock_fd = os.open(root / ".enrollment.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        observed = os.fstat(lock_fd)
        if not stat.S_ISREG(observed.st_mode) or observed.st_uid != trusted_uid or observed.st_mode & 0o077:
            raise ValueError("untrusted host enrollment lock")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(lock_fd)


def derived_public_key(private_key: Path) -> bytes:
    result = subprocess.run(
        ["openssl", "pkey", "-in", str(private_key), "-pubout"], capture_output=True, check=False,
    )
    if result.returncode:
        raise ValueError("host enrollment key changed")
    return result.stdout


def generate_key(*, root: Path = Path("/run/adp"), trusted_uid: int = 0) -> bytes:
    private_key = root / "dev-box-private-key"
    public_key = root / "dev-box-public-key"
    with enrollment_lock(root, trusted_uid):
        for pattern in (".host-key-*", ".host-public-*"):
            for orphan in root.glob(pattern):
                trusted_file(orphan, trusted_uid)
                orphan.unlink()
        if private_key.exists() or private_key.is_symlink():
            trusted_file(private_key, trusted_uid)
            derived = derived_public_key(private_key)
            if public_key.exists() or public_key.is_symlink():
                if trusted_file(public_key, trusted_uid) != derived:
                    raise ValueError("host enrollment key changed")
                return derived
        else:
            if public_key.exists() or public_key.is_symlink():
                raise ValueError("host enrollment key changed")
            key_fd, key_name = tempfile.mkstemp(prefix=".host-key-", dir=root)
            os.close(key_fd)
            try:
                generated = subprocess.run(
                    ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072", "-out", key_name],
                    capture_output=True, check=False,
                )
                if generated.returncode:
                    raise ValueError("host key generation unavailable")
                os.chmod(key_name, 0o600)
                derived = derived_public_key(Path(key_name))
                os.replace(key_name, private_key)
            finally:
                Path(key_name).unlink(missing_ok=True)
        public_fd, public_name = tempfile.mkstemp(prefix=".host-public-", dir=root)
        try:
            with os.fdopen(public_fd, "wb") as output:
                output.write(derived)
                output.flush()
                os.fsync(output.fileno())
            os.replace(public_name, public_key)
        finally:
            Path(public_name).unlink(missing_ok=True)
        return derived


def rotate_key(expected_digest: str, *, root: Path = Path("/run/adp"), trusted_uid: int = 0) -> bytes:
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise ValueError("invalid host enrollment key digest")
    with enrollment_lock(root, trusted_uid):
        receipt = root / "crew-ready"
        if receipt.exists() or receipt.is_symlink():
            raise ValueError("host run is still ready")
        private_key = root / "dev-box-private-key"
        public_key = root / "dev-box-public-key"
        marker = root / "dev-box-rotation"
        previous = (trusted_file(marker, trusted_uid)
                    if marker.exists() or marker.is_symlink() else None)
        already_rotated = False
        if private_key.exists() or private_key.is_symlink():
            trusted_file(private_key, trusted_uid)
            derived = derived_public_key(private_key)
            if public_key.exists() or public_key.is_symlink():
                if trusted_file(public_key, trusted_uid) != derived:
                    raise ValueError("host enrollment key changed")
            if hashlib.sha256(derived).hexdigest() != expected_digest:
                if previous != expected_digest.encode("ascii"):
                    raise ValueError("host enrollment key changed")
                already_rotated = True
        elif public_key.exists() or public_key.is_symlink():
            raise ValueError("host enrollment key changed")
        elif previous != expected_digest.encode("ascii"):
            raise ValueError("host enrollment key changed")
        if previous != expected_digest.encode("ascii"):
            marker_fd, marker_name = tempfile.mkstemp(prefix=".host-rotation-", dir=root)
            try:
                with os.fdopen(marker_fd, "wb") as output:
                    output.write(expected_digest.encode("ascii"))
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(marker_name, marker)
            finally:
                Path(marker_name).unlink(missing_ok=True)
        challenge = root / "dev-box-enrollment"
        if challenge.exists() or challenge.is_symlink():
            trusted_file(challenge, trusted_uid)
            challenge.unlink()
        if not already_rotated:
            public_key.unlink(missing_ok=True)
            private_key.unlink(missing_ok=True)
    return generate_key(root=root, trusted_uid=trusted_uid)


def receive_sealed(ciphertext: bytes, *, root: Path = Path("/run/adp"), trusted_uid: int = 0) -> None:
    trusted_directory(root, trusted_uid)
    if not isinstance(ciphertext, bytes) or not 0 < len(ciphertext) <= 1024:
        raise ValueError("invalid sealed host enrollment input")
    private_key = root / "dev-box-private-key"
    trusted_file(private_key, trusted_uid)
    public_key = trusted_file(root / "dev-box-public-key", trusted_uid)
    if derived_public_key(private_key) != public_key:
        raise ValueError("host enrollment key changed")
    decrypted = subprocess.run(
        ["openssl", "pkeyutl", "-decrypt", "-inkey", str(private_key),
         "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256"],
        input=ciphertext, capture_output=True, check=False,
    )
    if decrypted.returncode:
        raise ValueError("sealed host enrollment input refused")
    try:
        challenge = decrypted.stdout.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("sealed host enrollment input refused") from None
    write_enrollment(challenge, public_key, root=root, trusted_uid=trusted_uid, require_private_key=True)


def write_enrollment(challenge: str, public_key: bytes, *, root: Path = Path("/run/adp"), trusted_uid: int = 0,
                     require_private_key: bool = False) -> None:
    if (not isinstance(challenge, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", challenge)
            or not isinstance(public_key, bytes) or not 0 < len(public_key) <= 8192):
        raise ValueError("invalid host enrollment input")
    with enrollment_lock(root, trusted_uid):
        private_key = root / "dev-box-private-key"
        if require_private_key and not (private_key.exists() or private_key.is_symlink()):
            raise ValueError("host enrollment key changed")
        if private_key.exists() or private_key.is_symlink():
            trusted_file(private_key, trusted_uid)
            if derived_public_key(private_key) != public_key:
                raise ValueError("host enrollment key changed")
        for name, content in (("dev-box-enrollment", challenge.encode()), ("dev-box-public-key", public_key)):
            destination = root / name
            try:
                destination.lstat()
            except FileNotFoundError:
                pass
            else:
                trusted_file(destination, trusted_uid)
            temporary_fd, temporary_name = tempfile.mkstemp(prefix=".enrollment-", dir=root)
            try:
                with os.fdopen(temporary_fd, "wb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary_name, destination)
            finally:
                Path(temporary_name).unlink(missing_ok=True)


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Host enrollment requires root")
    try:
        if sys.argv[1:] == ["--generate-key"]:
            sys.stdout.buffer.write(generate_key())
            return
        if len(sys.argv) == 3 and sys.argv[1] == "--rotate-key":
            sys.stdout.buffer.write(rotate_key(sys.argv[2]))
            return
        sealed = sys.argv[1:] == ["--sealed"]
        if sys.argv[1:] not in ([], ["--sealed"]):
            raise ValueError("invalid host enrollment input")
        raw = sys.stdin.buffer.read(12_289)
        if len(raw) > 12_288:
            raise ValueError("invalid host enrollment input")
        if sealed:
            receive_sealed(base64.b64decode(raw, validate=True))
            return
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"challenge", "public_key_b64"}:
            raise ValueError("invalid host enrollment input")
        public_key = base64.b64decode(payload["public_key_b64"], validate=True)
        write_enrollment(payload["challenge"], public_key)
    except (ValueError, TypeError, binascii.Error, OSError):
        raise SystemExit("Invalid host enrollment input") from None


if __name__ == "__main__":
    main()
