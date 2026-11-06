"""Install bounded, checksum-pinned tool/Python maintenance without a solver."""

import hashlib
import json
import re
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from install_python import install as install_python


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
    install_python(root)
    wheel_lock = json.loads((root / "maintenance/wheel-lock.json").read_text())
    wheel_lock += json.loads((root / "maintenance/seed-wheel-lock.json").read_text())
    pillow_lock = json.loads((root / "maintenance/pillow-wheel-lock.json").read_text())
    go = json.loads((root / "maintenance/go-lock.json").read_text())
    assert sha(root / "go-tools/kubectl") == go["kubectl"]["sha256"]
    uv = json.loads((root / "maintenance/uv-lock.json").read_text())
    before = versions()
    assert sys.prefix == "/usr/local" and sys.version_info[:2] == (3, 10)
    assert before["skypilot"] == "0.12.3"
    allowed = {
        "gitpython": {"3.1.59", "3.1.60", "3.1.62"},
        "urllib3": {"2.7.0", "2.8.0"},
        "virtualenv": {"21.3.3", "21.7.13", "21.7.13+adp1"},
        "python-discovery": {"1.3.1", "1.6.0"},
        "certifi": {"2026.5.20", "2026.7.22", "2026.10.19"},
        "uv": {uv["previous_version"], uv["version"]},
        "pip": {"23.0.1", "26.2.2"},
        "brotli": {"1.2.0", "1.2.1"},
        "pillow": {"12.2.0", "12.3.0", "12.2.0+adp1", "12.3.0+adp1"},
    }
    for name, candidates in allowed.items():
        assert before[name] in candidates, f"Unreviewed input: {name}={before[name]}"
    pillow_version = before["pillow"].split("+")[0] + "+adp1"
    wheel_lock += [w for w in pillow_lock if w["version"] == pillow_version]
    assert sum(w["name"] == "pillow" for w in wheel_lock) == 1
    for wheel in wheel_lock:
        assert sha(root / "wheels" / wheel["filename"]) == wheel["sha256"]
    for name, dest in go["installed_paths"].items():
        assert sha(root / "go-tools" / name) == go["binary_sha256"][name]
        assert sha(Path(dest)) in {
            go["binary_sha256"][name],
            go["superseded_binary_sha256"][name],
            go["previous_binary_sha256"][name],
        }, f"Unreviewed tool: {name}"
    for name, expected in uv["binary_sha256"].items():
        assert sha(Path("/usr/local/bin") / name) in {
            expected,
            uv["previous_binary_sha256"][name],
        }
        old = Path("/root/.local/bin") / name
        assert sha(old) in {
            expected,
            uv["previous_binary_sha256"][name],
            uv["superseded_binary_sha256"][name],
        }
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
            "--force-reinstall",
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
    shutil.copyfile(root / "go-tools/kubectl", "/usr/local/bin/kubectl")
    Path("/usr/local/bin/kubectl").chmod(0o755)
    shutil.copytree(
        root / "licenses", "/usr/share/licenses/skypilot-go-tools", dirs_exist_ok=True
    )
    for name in uv["binary_sha256"]:
        assert sha(Path("/usr/local/bin") / name) == uv["binary_sha256"][name]
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
    subprocess.run([sys.executable, str(out / "verify_critical.py")], check=True)
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
