#!/usr/bin/env python3
"""Run the canonical nonpublishing gateway image checks without AWS credentials."""
from pathlib import Path
import subprocess

import yaml


def run_buildspec(path: Path, *, cwd: Path) -> None:
    spec = yaml.safe_load(path.read_text())
    if spec.get("version") != 0.2 or set(spec["phases"]) != {"build", "post_build"}:
        raise ValueError("Expected the gateway build/post_build smoke contract")
    commands = [command for phase in ("build", "post_build") for command in spec["phases"][phase]["commands"]]
    if not commands or not all(isinstance(command, str) for command in commands):
        raise ValueError("Smoke commands must be shell strings")
    # One shell preserves CodeBuild 0.2's working directory and variables across
    # phases. A build failure must prevent all subsequent checks from passing.
    subprocess.run(["bash", "-euo", "pipefail", "-c", "\n".join(commands)], cwd=cwd, check=True)


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    run_buildspec(root / "codebuild/bs-gateway-smoke.yml", cwd=root)
