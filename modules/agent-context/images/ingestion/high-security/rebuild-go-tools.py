#!/usr/bin/env python3
"""Rebuild maintenance binaries; requires git and Go 1.26.9 on PATH."""

import json
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import time

root = pathlib.Path(__file__).resolve().parent
out = pathlib.Path("rebuild-output").resolve()
out.mkdir(exist_ok=True)
toolchain = subprocess.check_output(["go", "version"], text=True).split()[2]
repos = {
    "syft": "anchore/syft",
    "trivy": "aquasecurity/trivy",
    "scip": "sourcegraph/scip-go",
    "tsgo": "microsoft/typescript-go",
    "osv": "google/osv-scanner",
}
packages = {
    "syft": "./cmd/syft",
    "trivy": "./cmd/trivy",
    "scip": "./cmd/scip-go",
    "tsgo": "./cmd/tsgo",
    "osv": "./cmd/osv-scanner",
}
for name, pin in json.loads((root / "tool-sources.json").read_text()).items():
    if len(sys.argv) > 1 and name not in sys.argv[1:]:
        continue
    assert toolchain == pin["toolchain"], (name, toolchain)
    src = out / (name + "-source")
    src.mkdir()
    subprocess.run(["git", "init", str(src)], check=True)
    subprocess.run(["git", "fetch", "--depth=1", "https://github.com/" + repos[name] + ".git", pin["revision"]], cwd=src, check=True)
    subprocess.run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=src, check=True)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=src, text=True).strip() == pin["revision"]
    for f in ["go.mod", "go.sum"]:
        data = (root / "locks" / name / f).read_bytes()
        assert hashlib.sha256(data).hexdigest() == pin[f.replace(".", "_") + "_sha256"]
        (src / f).write_bytes(data)
    env = dict(os.environ, CGO_ENABLED="0", GOOS="linux", GOARCH="amd64", GOTOOLCHAIN="local", GOWORK="off")
    # The module proxy intermittently resets parallel HTTP/2 streams. This
    # setting applies only to downloading build inputs, not to the binaries.
    download_env = dict(env, GODEBUG="http2client=0")
    for attempt in range(3):
        result = subprocess.run(["go", "mod", "download"], cwd=src, env=download_env)
        if result.returncode == 0:
            break
        if attempt == 2:
            result.check_returncode()
        time.sleep(5)
    subprocess.run(["go", "mod", "verify"], cwd=src, env=env, check=True)
    args = ["go", "build", "-p=1", "-trimpath", "-mod=readonly", "-buildvcs=false"]
    if name == "trivy":
        env["GOEXPERIMENT"] = "jsonv2"
        args += [
            "-ldflags=-X github.com/aquasecurity/trivy/pkg/version/app.ver=0.74.0-adp-security1"
        ]
    if name == "tsgo":
        args += ["-tags=noembed"]
    dest = {"scip": "scip-go", "tsgo": "tsc", "osv": "osv-scanner"}.get(name, name)
    subprocess.run(args + ["-o", str(out / dest), packages[name]], cwd=src, env=env, check=True)
    for f in ["go.mod", "go.sum"]:
        assert hashlib.sha256((src / f).read_bytes()).hexdigest() == pin[f.replace(".", "_") + "_sha256"]
    notices = out / "licenses" / name
    notices.mkdir(parents=True, exist_ok=True)
    for path in src.iterdir():
        if path.is_file() and path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
            shutil.copyfile(path, notices / path.name)
shutil.copyfile(root / "tool-sources.json", out / "licenses/tool-sources.json")
