#!/usr/bin/env python3
"""Rebuild maintenance binaries; requires git and Go 1.26.8 on PATH."""

import json
import os
import pathlib
import subprocess

root = pathlib.Path(__file__).resolve().parent
out = pathlib.Path("rebuild-output").resolve()
out.mkdir(exist_ok=True)
assert subprocess.check_output(["go", "version"]).decode().split()[2] == "go1.26.8"
repos = {
    "syft": "anchore/syft",
    "trivy": "aquasecurity/trivy",
    "scip": "sourcegraph/scip-go",
    "tsgo": "microsoft/typescript-go",
}
packages = {
    "syft": "./cmd/syft",
    "trivy": "./cmd/trivy",
    "scip": "./cmd/scip-go",
    "tsgo": "./cmd/tsgo",
}
for name, pin in json.loads((root / "tool-sources.json").read_text()).items():
    src = out / (name + "-source")
    subprocess.run(
        ["git", "clone", "https://github.com/" + repos[name] + ".git", str(src)], check=True
    )
    subprocess.run(["git", "checkout", pin["revision"]], cwd=src, check=True)
    for f in ["go.mod", "go.sum"]:
        (src / f).write_bytes((root / "locks" / name / f).read_bytes())
    env = dict(os.environ, CGO_ENABLED="0", GOOS="linux", GOARCH="amd64")
    args = ["go", "build", "-p=1", "-trimpath"]
    if name == "trivy":
        env["GOEXPERIMENT"] = "jsonv2"
        args += [
            "-ldflags=-X github.com/aquasecurity/trivy/pkg/version/app.ver=0.74.0-adp-security1"
        ]
    if name == "tsgo":
        args += ["-tags=noembed"]
    dest = {"scip": "scip-go", "tsgo": "tsc"}.get(name, name)
    subprocess.run(args + ["-o", str(out / dest), packages[name]], cwd=src, env=env, check=True)
