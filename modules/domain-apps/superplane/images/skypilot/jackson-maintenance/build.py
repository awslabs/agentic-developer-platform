"""Build real shaded classes offline, then replace the exact original Ray library."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from replace_jar import merge

ROOT = Path(__file__).resolve().parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original_jar", type=Path)
    args = parser.parse_args()
    lock = json.loads((ROOT / "artifact-lock.json").read_text())
    if sha(args.original_jar) != lock["original_jar_sha256"]:
        raise ValueError("unexpected original Ray jar")
    for artifact in lock["artifacts"]:
        if sha(ROOT / "artifacts" / artifact["filename"]) != artifact["sha256"]:
            raise ValueError("vendor input hash mismatch")
    tools = json.loads((ROOT / "tool-lock.json").read_text())["files"]
    actual = {
        str(path.relative_to(ROOT / ".m2")): sha(path)
        for path in (ROOT / ".m2").rglob("*")
        if path.is_file() and path.suffix in (".jar", ".pom")
    }
    if actual != tools:
        raise ValueError("build dependencies differ; run prepare.py with a clean cache")
    shutil.rmtree(ROOT / "target", ignore_errors=True)
    command = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--user",
        "1000:1000",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs",
        "/tmp:rw,size=512m",
        "-e",
        "MAVEN_CONFIG=/tmp/.m2",
        "--mount",
        "type=bind,src=" + str(ROOT) + ",dst=/work",
        "-w",
        "/work",
        lock["toolchain"],
        "mvn",
        "-B",
        "-o",
        "-Duser.home=/tmp",
        "-Dmaven.repo.local=/work/.m2",
        "package",
    ]
    subprocess.run(command, check=True)
    for component in ("core", "databind", "annotations"):
        artifact = f"jackson-{component}"
        expected = next(
            i["sha256"]
            for i in lock["artifacts"]
            if i["filename"] == artifact + "-2.18.11.jar"
        )
        if (
            sha(
                ROOT
                / ".m2/com/fasterxml/jackson/core"
                / artifact
                / "2.18.11"
                / (artifact + "-2.18.11.jar")
            )
            != expected
        ):
            raise ValueError("Maven did not use the locked vendor artifact")
    output = ROOT / "artifacts/ray_dist.jar"
    receipt = merge(
        args.original_jar, ROOT / "target/ray-jackson-shaded-2.18.11.jar", output
    )
    receipt.update(
        toolchain=lock["toolchain"],
        command=command,
        tool_lock_sha256=sha(ROOT / "tool-lock.json"),
        vendor_lock_sha256=sha(ROOT / "artifact-lock.json"),
        pom_sha256=sha(ROOT / "pom.xml"),
    )
    (ROOT / "artifacts/build-receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n"
    )
    expected_file = ROOT / "replacement-lock.json"
    if expected_file.exists() and json.loads(expected_file.read_text())[
        "sha256"
    ] != sha(output):
        raise ValueError("replacement build differs from reviewed artifact")
    print(
        json.dumps(
            {
                key: receipt[key]
                for key in (
                    "replacement_sha256",
                    "replacement_entries",
                    "unrelated_entries_preserved",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
