"""Upgrade only the pinned Conda OpenSSL package; preserve PyTorch/CUDA behavior."""

import hashlib
import json
import shutil
from pathlib import Path
import subprocess
import sys

EXPECTED = "500f505ad9814eba4bd787c87510c1b90f2873f8f3bdb1bdf6200c20742aa987"
package = Path(sys.argv[1])
assert hashlib.sha256(package.read_bytes()).hexdigest() == EXPECTED
prefix = Path("/opt/conda")


def inventory():
    rows = [json.loads(p.read_text()) for p in (prefix / "conda-meta").glob("*.json")]
    return {
        r["name"]: {k: r.get(k) for k in ("name", "version", "build", "sha256", "md5")}
        for r in rows
    }


def runtime():
    code = """import hashlib,json,ssl,torch
from pathlib import Path
x=torch.arange(9,dtype=torch.float32).reshape(3,3)
print(json.dumps({'torch':torch.__version__,'cuda':torch.version.cuda,
 'cudnn':torch.backends.cudnn.version(),'matrix':(x@x.T).tolist(),
 'torch_extension_sha256':hashlib.sha256(Path(torch._C.__file__).read_bytes()).hexdigest(),
 'ssl':ssl.OPENSSL_VERSION}))"""
    return json.loads(
        subprocess.check_output([str(prefix / "bin/python"), "-c", code], text=True)
    )


before = inventory()
assert before["openssl"]["version"] == "3.5.2", (
    "Review a changed input before altering its OpenSSL"
)
assert int(before["libgcc"]["version"].split(".")[0]) >= 15
assert "ca-certificates" in before
original_runtime = runtime()
# No solver-driven upgrades or network access: prerequisites are already present.
subprocess.run(
    [
        str(prefix / "bin/conda"),
        "install",
        "--yes",
        "--offline",
        "--no-deps",
        str(package),
    ],
    check=True,
)
# Conda retains the superseded executable in its extraction cache. Remove only
# that exact old package; other package caches and installed payloads stay intact.
old_cache = prefix / "pkgs" / "openssl-3.5.2-h26f9b46_0"
if old_cache.exists():
    assert json.loads((old_cache / "info/index.json").read_text())["version"] == "3.5.2"
    shutil.rmtree(old_cache)
for suffix in (".conda", ".tar.bz2"):
    (prefix / "pkgs" / (old_cache.name + suffix)).unlink(missing_ok=True)
assert not old_cache.exists()
after = inventory()
assert after["openssl"]["version"] == "3.5.8"
assert after["openssl"]["build"] == "h781a0a9_0"
assert {k: v for k, v in before.items() if k != "openssl"} == {
    k: v for k, v in after.items() if k != "openssl"
}
updated_runtime = runtime()
assert updated_runtime.pop("ssl").startswith("OpenSSL 3.5.8 ")
original_runtime.pop("ssl")
assert updated_runtime == original_runtime
subprocess.run([str(prefix / "bin/openssl"), "version"], check=True)
receipt = {
    "package_sha256": EXPECTED,
    "before": before["openssl"],
    "after": after["openssl"],
    "other_conda_packages_unchanged": True,
    "pytorch_cpu_runtime": updated_runtime,
    "gpu_execution": "not exercised; GPU driver/device required",
}
out = Path("/opt/superplane-demo/openssl-maintenance.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(receipt, indent=2) + "\n")
