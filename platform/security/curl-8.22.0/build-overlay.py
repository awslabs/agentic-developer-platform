#!/usr/bin/env python3
"""Build a curl repair over a reviewed immutable candidate, preserving its user."""

import argparse
import json
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("component", choices=["deepwiki", "worker", "skypilot"])
parser.add_argument("--tag", required=True)
parser.add_argument(
    "--docker-config", help="Optional isolated Docker authentication directory"
)
args = parser.parse_args()
root = Path(__file__).resolve().parent
candidate = json.loads((root / "bases.json").read_text())[args.component]
command = ["docker"]
if args.docker_config:
    command += ["--config", args.docker_config]
command += [
    "build",
    "--platform",
    "linux/amd64",
    "--target",
    "runtime",
    "--build-arg",
    "BASE=" + candidate["reference"],
    "--build-arg",
    "RUNTIME_USER=" + candidate["user"],
    "-t",
    args.tag,
    str(root),
]
subprocess.run(command, check=True)
