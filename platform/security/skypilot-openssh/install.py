"""Install coordinated Debian SSH packages without baking new generated host keys."""

import grp
import os
import pwd
import shutil
import subprocess
from pathlib import Path

ssh = Path("/etc/ssh")
existing_host_files = set(ssh.glob("ssh_host_*"))
subprocess.run(
    [
        "dpkg",
        "--force-confold",
        "-i",
        "/tmp/adp-openssh/client.deb",
        "/tmp/adp-openssh/server.deb",
        "/tmp/adp-openssh/sftp-server.deb",
    ],
    env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
    check=True,
)
# Debian's postinst may generate host keys in a container image. Remove only
# newly created files in this same layer; runtime key provisioning stays with
# the existing image entrypoint/operator configuration.
for file in set(ssh.glob("ssh_host_*")) - existing_host_files:
    file.unlink()
for package in ["openssh-client", "openssh-server", "openssh-sftp-server"]:
    assert (
        subprocess.check_output(
            ["dpkg-query", "-W", "-f=${Version}", package], text=True
        )
        == "1:10.0p1-7+deb13u4+adp.security.1"
    )
shutil.rmtree("/tmp/adp-openssh")

# Kubernetes already runs this API as UID/GID1000. Native ssh requires a passwd
# entry even when Python getpass accepts USER=sky; preserve that numeric identity.
try:
    pwd.getpwuid(1000)
except KeyError:
    try:
        grp.getgrgid(1000)
    except KeyError:
        subprocess.run(["groupadd", "--gid", "1000", "sky"], check=True)
    subprocess.run(
        [
            "useradd",
            "--uid",
            "1000",
            "--gid",
            "1000",
            "--create-home",
            "--home-dir",
            "/home/sky",
            "--shell",
            "/bin/sh",
            "sky",
        ],
        check=True,
    )
