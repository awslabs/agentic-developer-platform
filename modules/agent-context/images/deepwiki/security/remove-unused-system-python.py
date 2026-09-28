"""Remove the unused Debian interpreter while preserving DeepWiki's Python 3.11."""
import hashlib
import re
import subprocess
import sys
import sysconfig
from pathlib import Path

PACKAGES = {
    "python3", "python3-minimal", "python3.13", "python3.13-minimal",
    "libpython3-stdlib", "libpython3.13-stdlib", "libpython3.13-minimal",
}
stdlib = Path(sysconfig.get_path("stdlib")).resolve()
assert sys.version_info[:2] == (3, 11), "Review a changed application interpreter"
assert str(stdlib).startswith("/usr/local/lib/python3.11"), stdlib
application_python = Path(sys.executable).resolve()
assert str(application_python) in {"/opt/venv/bin/python", "/usr/local/bin/python3.11"}
owner = subprocess.run(["dpkg-query", "-S", str(application_python)], capture_output=True)
assert owner.returncode == 1, "Application Python must not belong to a removed Debian package"
before = hashlib.sha256(application_python.read_bytes()).hexdigest()
query = subprocess.check_output(
    ["dpkg-query", "-W", "-f=${binary:Package}\t${Status}\n"], text=True,
)
installed = {
    line.split("\t")[0].split(":")[0]
    for line in query.splitlines() if line.endswith("\tinstall ok installed")
}
selected = sorted(PACKAGES & installed)
if selected:
    plan = subprocess.check_output(["apt-get", "--simulate", "purge", *selected], text=True)
    removed = set(re.findall(r"^(?:Remv|Purg) ([^ :]+)", plan, re.MULTILINE))
    assert removed == set(selected), "Refusing removal of any dependent application package: " + repr(removed)
    subprocess.run(["apt-get", "purge", "-y", *selected], check=True)
assert application_python.exists()
assert hashlib.sha256(application_python.read_bytes()).hexdigest() == before
subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
print("Removed unused Debian Python packages; application interpreter unchanged:", selected)
