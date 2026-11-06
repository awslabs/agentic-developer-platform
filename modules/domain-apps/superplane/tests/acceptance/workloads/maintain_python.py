"""Install a solved, checksum-locked CPython patch without changing PyTorch/CUDA."""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
lock = json.loads((root / "python-runtime-lock.json").read_text())
prefix = Path("/opt/conda")


def inventory():
    return {
        r["name"]: r
        for p in (prefix / "conda-meta").glob("*.json")
        for r in [json.loads(p.read_text())]
    }


def runtime():
    return json.loads(
        subprocess.check_output(
            [
                str(prefix / "bin/python"),
                "-c",
                """
import hashlib, json, sys, ssl, sqlite3, bz2, lzma, ctypes, pyexpat, torch
from pathlib import Path
x = torch.arange(9, dtype=torch.float32).reshape(3, 3)
print(json.dumps({'python': list(sys.version_info[:3]), 'ssl': ssl.OPENSSL_VERSION,
 'torch': torch.__version__, 'cuda': torch.version.cuda, 'cudnn': torch.backends.cudnn.version(),
 'matrix': (x @ x.T).tolist(),
 'torch_extension_sha256': hashlib.sha256(Path(torch._C.__file__).read_bytes()).hexdigest()}))
""",
            ],
            text=True,
        )
    )


before = inventory()
original = runtime()
assert original["python"] == [3, 11, 13], (
    "Review a changed PyTorch base before replacing Python"
)
allowed = {p["name"] for p in lock["packages"]}
for package in lock["packages"]:
    path = root / package["fn"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == package["sha256"]
    old = package["before"]
    if old is None:
        assert package["name"] not in before
    else:
        actual = before[package["name"]]
        assert (actual["version"], actual["build"]) == (
            old["version"],
            old["build_string"],
        )
subprocess.run(
    [
        str(prefix / "bin/conda"),
        "install",
        "--yes",
        "--offline",
        "--no-deps",
        *[str(root / p["fn"]) for p in lock["packages"]],
    ],
    check=True,
)
after = inventory()
assert {k: v for k, v in before.items() if k not in allowed} == {
    k: v for k, v in after.items() if k not in allowed
}, "An unrelated Conda package changed"
for package in lock["packages"]:
    installed = after[package["name"]]
    assert (installed["version"], installed["build"]) == (
        package["version"],
        package["build"],
    )
    old = before.get(package["name"])
    if old:
        cache = prefix / "pkgs" / f"{old['name']}-{old['version']}-{old['build']}"
        if cache.exists():
            assert (
                json.loads((cache / "info/index.json").read_text())["version"]
                == old["version"]
            )
            shutil.rmtree(cache)
        for suffix in (".conda", ".tar.bz2"):
            cache.with_name(cache.name + suffix).unlink(missing_ok=True)
updated = runtime()
assert updated.pop("python") == [3, 11, 17]
assert updated.pop("ssl").startswith("OpenSSL 3.5.9 ")
original.pop("python")
original.pop("ssl")
assert updated == original, "PyTorch/CUDA behavior or its native extension changed"
out = Path("/opt/superplane-demo/python-maintenance.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(
    json.dumps(
        {
            "lock": lock,
            "other_conda_packages_unchanged": True,
            "pytorch_cpu_runtime": updated,
            "gpu_execution": "not exercised; GPU driver/device required",
        },
        indent=2,
    )
    + "\n"
)
