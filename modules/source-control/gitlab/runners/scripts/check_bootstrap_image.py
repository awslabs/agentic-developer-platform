#!/usr/bin/env python3
"""Refuse a bootstrap image that cannot run the loader with its deployed UIDs."""
import re
import subprocess
import sys


PROBE = '''import datetime,hashlib,os,stat,subprocess,sys
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import boto3
from load_credentials import validate_ca,BootstrapError
assert os.getuid()==int(sys.argv[1])
assert 'CapEff:\\t0000000000000000' in open('/proc/self/status').read()
assert 'NoNewPrivs:\\t1' in open('/proc/self/status').read()
plugin=Path('/opt/fleeting/fleeting-plugin-aws')
assert hashlib.sha256(plugin.read_bytes()).hexdigest()==sys.argv[2]
assert plugin.stat().st_uid==0 and stat.S_IMODE(plugin.stat().st_mode)==0o555
assert os.access(plugin,os.X_OK) and not os.access(plugin,os.W_OK)
result=subprocess.run([str(plugin),'--version'],capture_output=True,text=True,timeout=10,check=True)
fields=dict(line.split(':',1) for line in result.stdout.splitlines() if ':' in line)
assert fields['Name'].strip()=='fleeting-plugin-aws'
assert fields['Git revision'].strip()=='bf5cbcc9'
assert fields['OS/Arch'].strip()=='linux/amd64'
key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
now=datetime.datetime.now(datetime.timezone.utc)
name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'local-fixture.test')])
cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
.serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
.not_valid_after(now+datetime.timedelta(hours=1)).add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True)
.sign(key,hashes.SHA256()))
pem=cert.public_bytes(serialization.Encoding.PEM).decode()
assert validate_ca(pem)==pem
for invalid in ['', 'not a certificate', key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()]:
 try: validate_ca(invalid)
 except BootstrapError: pass
 else: raise AssertionError('invalid CA accepted')
'''


def check(image, plugin_sha256, run=subprocess.run):
    if not isinstance(image, str) or not re.fullmatch(r"[a-z0-9][a-zA-Z0-9./_:@-]{1,255}", image):
        raise ValueError("invalid bootstrap image reference")
    if plugin_sha256 != "2090afe6b2a888d1e58990dbf7b731124956d27358ff8b8532180235d9d49ebb":
        raise ValueError("unreviewed connector checksum")
    for uid in (65532, 10001):
        result = run([
            "docker", "run", "--rm", "--network=none", "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true", "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,mode=1777",
            "--user", f"{uid}:{uid}", "--env", "AWS_EC2_METADATA_DISABLED=true",
            "--entrypoint", "python", image, "-c", PROBE, str(uid), plugin_sha256,
        ], capture_output=True, check=False, timeout=30)
        if result.returncode:
            raise ValueError("bootstrap image runtime verification failed")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3:
            raise ValueError("bootstrap image and reviewed connector checksum required")
        check(sys.argv[1], sys.argv[2])
    except Exception:
        # Never copy container or provider output into publication logs.
        raise SystemExit("bootstrap image runtime verification failed") from None
    print("bootstrap image runtime verified")
