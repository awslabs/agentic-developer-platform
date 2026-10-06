"""Package the unchanged Debian Binwalk payload for the worker's fixed Python."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

version = "2.4.3+dfsg1-2+deb13u1"
for package in ("binwalk", "python3-binwalk"):
    assert subprocess.check_output(["dpkg-query", "-W", "-f=${Version}", package], text=True) == version
root = Path("/package")
site = root / "opt/adp-binwalk"
site.mkdir(parents=True)
receipts = []
for name in ("binwalk", "binwalk-2.4.3.egg-info"):
    source = Path("/usr/lib/python3/dist-packages") / name
    for path in source.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            dest = site / name / path.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            assert hashlib.sha256(dest.read_bytes()).hexdigest() == checksum
            receipts.append({"source": str(path), "installed": "/" + str(dest.relative_to(root)), "sha256": checksum})
assert receipts
launcher = Path("/usr/bin/binwalk").read_bytes()
assert launcher.startswith(b"#! /usr/bin/python3\n")
binary = root / "usr/bin/binwalk"
binary.parent.mkdir(parents=True)
# Match Debian's original dependency visibility: the old interpreter saw only
# Binwalk, not the worker's optional capstone/GUI packages. Those change Binwalk's
# default module selection. Keep an isolated payload path and skip site startup.
rewritten = launcher.replace(b"#! /usr/bin/python3\n", b"#! /usr/local/bin/python3 -S\n", 1)
assert rewritten.count(b"import sys\n") == 1
rewritten = rewritten.replace(b"import sys\n", b'import sys\nsys.path.insert(0, "/opt/adp-binwalk")\n', 1)
binary.write_bytes(rewritten)
binary.chmod(0o755)
for name in ("binwalk", "python3-binwalk"):
    shutil.copytree(Path("/usr/share/doc") / name, root / "usr/share/doc" / name)
manual = Path("/usr/share/man/man1/binwalk.1.gz")
# Debian slim excludes manuals through dpkg path-exclude. Preserve one if present.
if manual.exists():
    (root / manual.relative_to("/")).parent.mkdir(parents=True)
    shutil.copy2(manual, root / manual.relative_to("/"))
(root / "opt/adp-security").mkdir(parents=True)
(root / "opt/adp-security/binwalk.json").write_text(json.dumps({
    "debian_version": version, "launcher_before_sha256": hashlib.sha256(launcher).hexdigest(),
    "launcher_after_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    "change": "Use the official worker Python 3.13.16 interpreter; application payload unchanged; private module path preserves original dependency visibility",
    "files": receipts,
}, indent=2) + "\n")
(root / "DEBIAN").mkdir()
(root / "DEBIAN/control").write_text(f"""Package: binwalk
Version: {version}+adp1
Section: utils
Priority: optional
Architecture: all
Maintainer: ADP Security <security@example.invalid>
Depends: libmagic1t64
Description: Debian Binwalk payload for the maintained ADP Python runtime
 Requires the worker's preinstalled /usr/local/bin/python3 (3.13.16).
 This artifact is only intended for that exact worker base.
""")
