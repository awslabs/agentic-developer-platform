"""Fixed credential-free qualification program; never invoke the provider CLI.

The installed-image collector executes these exact source bytes in an isolated
Pod. Output contains probe facts and fixture digests, never credential contents.
This program is a component probe, not Product/broker or native qualification.
"""

import errno
from hashlib import sha256
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys


PROTOCOL = "transform-credential-free-isolation-v1"


def require(value, message):
    if not value:
        raise ValueError(message)


def denied_connect(address, port):
    try:
        with socket.create_connection((address, port), timeout=2):
            pass
    except OSError as error:
        # A refused connection proves a reachable host, not network isolation.
        require(isinstance(error, TimeoutError) or error.errno in {
            errno.ETIMEDOUT, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EACCES,
        }, "negative network control was inconclusive")
        return
    raise ValueError("negative network control was reachable")


LANDLOCK = r'''
import errno,json,os,subprocess,sys
from pathlib import Path
from l5_services.adapters.transform.provider_runner import restrict_provider
def require(value):
    if not value:
        raise ValueError('filesystem positive control failed')
root,home=Path('/work/source'),Path('/work/home')
restrict_provider(os.path.realpath(sys.executable),root,home,['allowed.txt'])
require((root/'allowed.txt').read_bytes()==b'original-source-v1\n')
(root/'allowed.txt').write_bytes(b'admitted-write-v1\n')
for path,mode in [(root/'readonly.txt','w'),('/work/private.txt','r'),('/proc/self/environ','r')]:
    try:
        open(path,mode).close()
    except PermissionError:
        pass
    else:
        raise ValueError('filesystem boundary did not deny access')
try:
    subprocess.run(['/bin/sh','-c','true'],check=True,timeout=2)
except PermissionError:
    pass
else:
    raise ValueError('provider shell boundary did not deny execution')
print(json.dumps({'source_read':True,'admitted_write':True,'outside_write_denied':True,
 'private_read_denied':True,'process_environment_denied':True,'shell_denied':True}))
'''


def run(selection):
    require(isinstance(selection, dict) and set(selection) == {
        "protocol", "nonce", "program_sha256", "control_addresses"}, "invalid probe selection")
    require(selection["protocol"] == PROTOCOL and isinstance(selection["nonce"], str)
            and len(selection["nonce"]) == 64, "probe identity unavailable")
    addresses = selection["control_addresses"]
    require(isinstance(addresses, list) and 1 <= len(addresses) <= 4
            and len(set(addresses)) == len(addresses)
            and all(ipaddress.ip_address(value).is_private
                    and not ipaddress.ip_address(value).is_link_local
                    and not ipaddress.ip_address(value).is_loopback for value in addresses),
            "network control must use the independently observed private EKS endpoint")
    require(os.getuid() == os.getgid() == 10000, "probe identity differs")
    require(not any(key.upper().startswith(("AWS_", "ADP_", "KUBERNETES_"))
                    for key in os.environ), "probe inherited platform identity")
    for name in ("/var/run/secrets/kubernetes.io/serviceaccount/token", "/root/.aws/credentials",
                 "/home/10000/.aws/credentials", "/work/.aws/credentials"):
        try:
            with open(name, "rb"):
                pass
        except (FileNotFoundError, PermissionError):
            continue
        raise ValueError("probe can read a credential file")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    require(int(status["CapEff"].strip(), 16) == 0 and status["Seccomp"].strip() == "2"
            and status["NoNewPrivs"].strip() == "1", "process isolation differs")
    try:
        Path("/qualification-root-write").write_bytes(b"probe")
    except OSError as error:
        require(error.errno == errno.EROFS, "root write failed for an ambiguous reason")
    else:
        raise ValueError("root filesystem is writable")
    for address in ("169.254.169.254", "169.254.170.23"):
        denied_connect(address, 80)
    for address in addresses:
        denied_connect(address, 443)
    root, home = Path("/work/source"), Path("/work/home")
    root.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    (home / "tmp").mkdir()
    (root / "allowed.txt").write_bytes(b"original-source-v1\n")
    (root / "readonly.txt").write_bytes(b"read-only-source-v1\n")
    Path("/work/private.txt").write_bytes(b"private-canary-v1\n")
    child = subprocess.run([sys.executable, "-I", "-c", LANDLOCK], capture_output=True,
                           text=True, timeout=15, check=True)
    require(len(child.stdout) < 4096 and not child.stderr, "filesystem control output differs")
    facts = json.loads(child.stdout)
    require(set(facts) == {"source_read", "admitted_write", "outside_write_denied", "private_read_denied",
                           "process_environment_denied", "shell_denied"}
            and all(value is True for value in facts.values()), "filesystem controls incomplete")
    require((root / "allowed.txt").read_bytes() == b"admitted-write-v1\n"
            and (root / "readonly.txt").read_bytes() == b"read-only-source-v1\n"
            and Path("/work/private.txt").read_bytes() == b"private-canary-v1\n",
            "independent filesystem readback changed")
    return {**selection, "observations": {**facts, "credential_free": True,
            "root_readonly": True, "capabilities_empty": True, "seccomp_filter": True,
            "no_new_privileges": True, "metadata_denied": True, "egress_denied": True},
            "source_sha256": sha256(b"original-source-v1\n").hexdigest(),
            "candidate_sha256": sha256(b"admitted-write-v1\n").hexdigest(),
            "native_calls": False, "qualified": False, "live_acceptance": "not_evaluated"}


if __name__ == "__main__":
    try:
        print(json.dumps(run(json.loads(sys.argv[1])), sort_keys=True), flush=True)
    except Exception:
        # Kernel/provider exception strings can contain private target data.
        print(json.dumps({"protocol": PROTOCOL, "outcome": "incomplete"}), flush=True)
        raise SystemExit(1) from None
