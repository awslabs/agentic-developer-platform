"""Run ordinary Java/Ray compatibility without cloud credentials or networking."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jar", type=Path, default=ROOT / "artifacts/ray_dist.jar")
    parser.add_argument("--java", choices=("17", "21"), default="17")
    parser.add_argument("--base-classes", action="store_true")
    args = parser.parse_args()
    lock = json.loads((ROOT / "artifact-lock.json").read_text())
    expected = json.loads((ROOT / "replacement-lock.json").read_text())["sha256"]
    if hashlib.sha256(args.jar.read_bytes()).hexdigest() != expected:
        raise ValueError("test subject differs from reviewed replacement")
    image = lock["test_toolchains"][args.java]
    variant = "base" if args.base_classes else "META-INF/versions/" + args.java + "/"
    command = [
        "javac",
        "--release",
        "8",
        "-cp",
        "/candidate.jar",
        "-d",
        "/tmp/classes",
        "/review/OrdinaryCompatibility.java",
    ]
    java = ["java"] + (
        ["-Djdk.util.jar.enableMultiRelease=false"] if args.base_classes else []
    )
    java += [
        "-cp",
        "/tmp/classes:/candidate.jar",
        "OrdinaryCompatibility",
        "/candidate.jar",
        variant,
    ]
    # Both commands are fixed tokens; arguments cannot introduce shell content.
    script = " ".join(command) + " && " + " ".join(java)
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--user",
            "1000:1000",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--tmpfs",
            "/tmp:rw,size=256m",
            "-e",
            "MAVEN_CONFIG=/tmp/.m2",
            "--mount",
            "type=bind,src=" + str(ROOT) + ",dst=/review,readonly",
            "--mount",
            "type=bind,src=" + str(args.jar.resolve()) + ",dst=/candidate.jar,readonly",
            image,
            "sh",
            "-c",
            script,
        ],
        check=True,
        timeout=180,
    )


if __name__ == "__main__":
    main()
