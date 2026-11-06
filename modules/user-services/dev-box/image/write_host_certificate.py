"""Generate a generation-scoped, private-IP certificate on the Crew box."""

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path


def trusted(path, uid):
    observed = path.lstat()
    if not stat.S_ISREG(observed.st_mode) or observed.st_uid != uid or observed.st_mode & 0o022:
        raise ValueError("untrusted Crew host file")
    return path.read_bytes()


def openssl(*args):
    return subprocess.run(["openssl", *args], check=True, capture_output=True).stdout


def fingerprint(certificate):
    return hashlib.sha256(openssl("x509", "-in", str(certificate), "-outform", "DER")).hexdigest()


def generate_certificate(*, authority_file=Path("/run/adp/crew-host.json"),
                         root=Path("/run/adp/crew-host"), uid=0, group_id=None,
                         private_ip=None, generation=None):
    if authority_file.exists():
        authority = json.loads(trusted(authority_file, uid))
        if ((private_ip is not None and private_ip != authority["private_ip"])
                or (generation is not None and generation != authority["lease_generation"])):
            raise ValueError("Crew host authority changed")
        private_ip, generation = authority["private_ip"], authority["lease_generation"]
    if private_ip is None or generation is None:
        raise ValueError("Crew host address and generation required")
    address = ipaddress.ip_address(private_ip)
    networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if not isinstance(address, ipaddress.IPv4Address) or not any(
        address in ipaddress.ip_network(network) for network in networks
    ):
        raise ValueError("Crew host address must be RFC1918")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("invalid Crew host generation")
    if root.is_symlink():
        raise ValueError("untrusted Crew host directory")
    root.mkdir(mode=0o750, parents=True, exist_ok=True)
    observed = root.lstat()
    if not stat.S_ISDIR(observed.st_mode) or observed.st_uid != uid or observed.st_mode & 0o027:
        raise ValueError("untrusted Crew host directory")
    if group_id is not None:
        os.chown(root, uid, group_id)
        token_directory = root / "model-token"
        if token_directory.is_symlink():
            raise ValueError("untrusted Crew model token directory")
        token_directory.mkdir(mode=0o770, exist_ok=True)
        token_stat = token_directory.lstat()
        if (not stat.S_ISDIR(token_stat.st_mode) or token_stat.st_uid != uid
                or token_stat.st_mode & 0o007):
            raise ValueError("untrusted Crew model token directory")
        os.chown(token_directory, uid, group_id)
        os.chmod(token_directory, 0o770)
    lock_fd = os.open(root / ".certificate.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        private_key, certificate, marker = (root / name for name in ("server.key", "server.pem", "generation"))
        present = [path.exists() or path.is_symlink() for path in (private_key, certificate, marker)]
        if any(present):
            if not all(present):
                raise ValueError("incomplete Crew host certificate")
            saved = trusted(marker, uid).decode()
            if saved == f"{generation}:{address}":
                trusted(private_key, uid)
                trusted(certificate, uid)
                san = openssl("x509", "-in", str(certificate), "-noout", "-ext", "subjectAltName")
                if b"IP Address:" + str(address).encode() not in san:
                    raise ValueError("Crew host certificate address mismatch")
                if openssl("pkey", "-in", str(private_key), "-pubout") != openssl(
                    "x509", "-in", str(certificate), "-pubkey", "-noout"
                ):
                    raise ValueError("Crew host certificate key mismatch")
                return fingerprint(certificate)
        with tempfile.TemporaryDirectory(prefix=".certificate-", dir=root) as staging:
            staged = Path(staging)
            key, cert = staged / "server.key", staged / "server.pem"
            openssl("req", "-x509", "-newkey", "rsa:3072", "-nodes", "-sha256", "-days", "30",
                    "-subj", "/CN=adp-crew-host", "-addext", f"subjectAltName=IP:{address}",
                    "-keyout", str(key), "-out", str(cert))
            os.chmod(key, 0o640)
            os.chmod(cert, 0o644)
            if group_id is not None:
                os.chown(key, uid, group_id)
            (staged / "generation").write_text(f"{generation}:{address}")
            os.chmod(staged / "generation", 0o600)
            result = fingerprint(cert)
            os.replace(key, private_key)
            os.replace(cert, certificate)
            os.replace(staged / "generation", marker)
        return result
    finally:
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fingerprint", action="store_true")
    parser.add_argument("--private-ip")
    parser.add_argument("--generation", type=int)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Crew host certificate requires root")
    import grp
    result = generate_certificate(group_id=grp.getgrnam("agent").gr_gid,
                                  private_ip=args.private_ip, generation=args.generation)
    if args.fingerprint:
        print(result)


if __name__ == "__main__":
    main()
