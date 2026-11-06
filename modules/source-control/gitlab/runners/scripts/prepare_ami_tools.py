"""Stage reviewed build tools in a new private directory without system installation."""
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import urllib.request
import zipfile

PINS = {
    "packer": ("https://releases.hashicorp.com/packer/1.16.2/packer_1.16.2_linux_amd64.zip",
               "abbfe0cf74985084112659b53e3565f264f70a20914c3b7c4f789f54f61c2414"),
    "session-manager-plugin": (
        "https://s3.amazonaws.com/session-manager-downloads/plugin/1.2.896.0/ubuntu_64bit/session-manager-plugin.deb",
        "53f51fcc67314a4bf85888a38a3967cd41f8d0f28a60a9469a63acf935ba713e"),
}


def download(name):
    url, expected = PINS[name]
    with urllib.request.urlopen(url, timeout=60) as response:
        raw = response.read(100 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("build tool checksum differs from reviewed source")
    return raw


def prepare(directory):
    if directory.exists():
        raise ValueError("build tools require a fresh directory")
    directory.mkdir(mode=0o700, parents=True)
    with zipfile.ZipFile(io.BytesIO(download("packer"))) as archive:
        (directory / "packer").write_bytes(archive.read("packer"))
    (directory / "packer").chmod(0o500)
    package = directory / "session-manager-plugin.deb"
    package.write_bytes(download("session-manager-plugin"))
    subprocess.run(["dpkg-deb", "--extract", str(package), str(directory / "ssm")],
                   capture_output=True, check=True, timeout=30)
    (directory / "session-manager-plugin").symlink_to(directory.resolve() / "ssm/usr/local/sessionmanagerplugin/bin/session-manager-plugin")
    for program, expected in (("packer", "1.16.2"), ("session-manager-plugin", "1.2.896.0")):
        result = subprocess.run([str(directory / program), "--version"], capture_output=True, text=True, check=True, timeout=30)
        if result.stdout.strip().removeprefix("Packer v") != expected:
            raise ValueError("build tool runtime version differs from reviewed pin")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("private tool directory required")
        prepare(Path(sys.argv[1]))
    except Exception:
        raise SystemExit("AMI tool preparation failed") from None
    print("Reviewed Packer and Session Manager tools verified")
