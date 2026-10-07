"""Install bounded, checksum-pinned tool/Python maintenance without a solver."""

import hashlib
import importlib.metadata as metadata
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def versions():
    return {
        re.sub(r"[-_.]+", "-", d.metadata["Name"]).lower(): d.version
        for d in metadata.distributions()
    }


def main():
    if not __debug__:
        raise RuntimeError("Maintenance requires assertions")
    root = Path(sys.argv[1])
    wheel_lock = json.loads((root / "maintenance/wheel-lock.json").read_text())
    go = json.loads((root / "maintenance/go-lock.json").read_text())
    uv = json.loads((root / "maintenance/uv-lock.json").read_text())
    before = versions()
    assert sys.prefix == "/usr/local" and sys.version_info[:2] == (3, 10)
    assert before["skypilot"] == "0.12.3"
    allowed = {
        "gitpython": {"3.1.59", "3.1.60"},
        "urllib3": {"2.7.0", "2.8.0"},
        "virtualenv": {"21.3.3", "21.7.13"},
        "python-discovery": {"1.3.1", "1.6.0"},
    }
    for name, candidates in allowed.items():
        assert before[name] in candidates, f"Unreviewed input: {name}={before[name]}"
    for wheel in wheel_lock:
        assert sha(root / "wheels" / wheel["filename"]) == wheel["sha256"]
    for name, dest in go["installed_paths"].items():
        assert sha(root / "go-tools" / name) == go["binary_sha256"][name]
        assert sha(Path(dest)) in {
            go["binary_sha256"][name],
            go["superseded_binary_sha256"][name],
        }, f"Unreviewed tool: {name}"
    assert before["uv"] == uv["version"]
    for name, expected in uv["binary_sha256"].items():
        assert sha(Path("/usr/local/bin") / name) == expected
        old = Path("/root/.local/bin") / name
        assert sha(old) in {expected, uv["superseded_binary_sha256"][name]}
        if old.is_symlink():
            assert old.resolve() == Path("/usr/local/bin") / name

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--no-cache-dir",
            *[str(root / "wheels" / w["filename"]) for w in wheel_lock],
        ],
        check=True,
    )
    after = versions()
    assert {k: v for k, v in before.items() if k not in allowed} == {
        k: v for k, v in after.items() if k not in allowed
    }, "An unrelated distribution changed"
    for wheel in wheel_lock:
        assert after[wheel["name"].lower()] == wheel["version"]
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
    for name, dest in go["installed_paths"].items():
        shutil.copyfile(root / "go-tools" / name, dest)
        Path(dest).chmod(0o755)
        assert sha(Path(dest)) == go["binary_sha256"][name]
    shutil.copytree(
        root / "licenses", "/usr/share/licenses/skypilot-go-tools", dirs_exist_ok=True
    )
    for name in uv["binary_sha256"]:
        old = Path("/root/.local/bin") / name
        old.unlink()
        old.symlink_to(Path("/usr/local/bin") / name)

    requirements = Path("/opt/adp-security/requirements-security.txt")
    text = requirements.read_text()
    for wheel in wheel_lock:
        name, version = wheel["name"], wheel["version"]
        pattern = rf"^{re.escape(name)}==[^\s]+$"
        matches = re.findall(pattern, text, re.MULTILINE | re.IGNORECASE)
        assert len(matches) <= 1
        if matches:
            text = re.sub(
                pattern, f"{name}=={version}", text, flags=re.MULTILINE | re.IGNORECASE
            )
        else:
            text = text.rstrip() + f"\n{name}=={version}\n"
    requirements.write_text(text)
    out = Path("/opt/adp-security/high-maintenance")
    shutil.copytree(root / "maintenance", out, dirs_exist_ok=True)
    (out / "installation.json").write_text(
        json.dumps(
            {
                "before_versions": {k: before[k] for k in allowed},
                "after_versions": {k: after[k] for k in allowed},
                "other_distribution_versions_unchanged": True,
                "go_binary_sha256": go["binary_sha256"],
                "uv_binary_sha256": uv["binary_sha256"],
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
