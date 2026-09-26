#!/usr/bin/env python3
"""Build tracked ingestion sources and run a disposable, restricted image gate.

No registry push, host credential forwarding, production volume, or cloud API.
Docker must expose its local Unix socket. Logs/receipts are kept in --output.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[3]
MODULE = Path("modules/agent-context")
STAGED = ("pipeline", "alembic", "personal_context")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True, help="Exact reviewed Git commit")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", args.revision + "^{commit}"], cwd=ROOT, text=True
    ).strip()
    paths = [str(MODULE / "images/ingestion"), str(MODULE / "images/shared"), str(MODULE / "tests/container")]
    paths += [str(MODULE / item) for item in STAGED]
    paths += ["modules/gateway/security/stdlib"]
    archive = subprocess.check_output(
        ["git", "archive", "--format=tar", revision, "--", *paths], cwd=ROOT
    )
    digest = hashlib.sha256(archive).hexdigest()
    receipt = {"revision": revision, "source_archive_sha256": digest, "result": "incomplete"}
    (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    with (
        tempfile.TemporaryDirectory(prefix="ingestion-gate-") as work,
        (args.output / "commands.log").open("w") as log,
    ):
        work = Path(work)
        # git archive is the only source input. No untracked files/host secrets.
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(work / "source", filter="data")
        source_root = work / "source" / MODULE
        context = source_root / "images/ingestion"
        shutil.copytree(work / "source/modules/gateway/security/stdlib", context / "security-stdlib")
        shutil.copytree(source_root / "images/shared", context / "security-build")
        for item in STAGED:
            destination = context / item
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source_root / item, destination)
        config = work / "docker-config"
        config.mkdir()
        docker = ["docker", "--host", "unix:///var/run/docker.sock", "--config", str(config)]
        # Do not inherit registry helpers, proxy credentials or remote Docker config.
        env = {"PATH": os.environ["PATH"], "HOME": str(work), "DOCKER_BUILDKIT": "1"}

        def execute(arguments, capture=False):
            log.write(json.dumps(arguments) + "\n")
            log.flush()
            result = subprocess.run(
                arguments,
                env=env,
                stdout=subprocess.PIPE if capture else log,
                stderr=log,
                text=True,
            )
            if capture and result.stdout:
                log.write(result.stdout)
                log.flush()
            if result.returncode:
                raise RuntimeError(
                    f"Command failed ({result.returncode}); see {args.output / 'commands.log'}"
                )
            return result.stdout

        image = f"adp-ingestion-validation:{revision[:12]}"
        execute(
            docker
            + [
                "build",
                "--label",
                f"org.opencontainers.image.revision={revision}",
                "--label",
                f"adp.validation.source-archive={digest}",
                "-t",
                image,
                str(context),
            ]
        )
        metadata = json.loads(execute(docker + ["image", "inspect", image], capture=True))[0]
        image_id = metadata["Id"]
        receipt["image_id"] = image_id
        receipt["image_repo_digests"] = metadata.get("RepoDigests", [])
        (args.output / "image-inspect.json").write_text(json.dumps(metadata, indent=2) + "\n")
        volume = "ingestion-validation-" + uuid.uuid4().hex
        execute(
            docker + ["volume", "create", "--label", "adp.validation=ingestion-nonroot", volume]
        )
        try:
            # Root is used ONLY to provision this new disposable fixture. It has
            # no host bind mounts, network, credentials or production data.
            seed = """from pathlib import Path
import os
root=Path('/platform-data')
for name in ['repos','code-indexes','learning','state']:
 p=root/name; p.mkdir(); os.chown(p,0,10001); p.chmod(0o2770)
p=root/'unrelated'; p.mkdir(mode=0o700); (p/'private').write_text('untouched')
"""
            execute(
                docker
                + [
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--user",
                    "0:0",
                    "--cap-drop",
                    "ALL",
                    "--cap-add",
                    "CHOWN",
                    "--security-opt",
                    "no-new-privileges=true",
                    "--mount",
                    f"type=volume,source={volume},target=/platform-data",
                    image_id,
                    "python",
                    "-c",
                    seed,
                ]
            )
            command = docker + [
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--user",
                "10001:10001",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges=true",
                "--pids-limit",
                "256",
                "--memory",
                "6g",
                "--cpus",
                "2",
                "--shm-size",
                "256m",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=2g,mode=1777",
                "--tmpfs",
                "/home/appuser:rw,nosuid,nodev,size=512m,uid=10001,gid=10001,mode=0700",
                "--mount",
                f"type=volume,source={volume},target=/platform-data",
                "--mount",
                f"type=bind,source={source_root / 'tests/container'},target=/validation,readonly",
                "-e",
                "AWS_EC2_METADATA_DISABLED=true",
                "-e",
                "OTEL_SDK_DISABLED=true",
                "-e",
                "OTEL_TRACES_EXPORTER=none",
                "-e",
                "OTEL_METRICS_EXPORTER=none",
                image_id,
                "python",
                "/validation/ingestion_runtime.py",
            ]
            output = execute(command, capture=True)
            (args.output / "runtime.log").write_text(output)
            # Verify the unrelated fixture's content, owner and mode are intact.
            verify = """from pathlib import Path
p=Path('/platform-data/unrelated')
assert p.stat().st_uid==0 and p.stat().st_gid==0 and p.stat().st_mode & 0o777 == 0o700
assert (p/'private').read_text()=='untouched'
"""
            execute(
                docker
                + [
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--user",
                    "0:0",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges=true",
                    "--mount",
                    f"type=volume,source={volume},target=/platform-data,readonly",
                    image_id,
                    "python",
                    "-c",
                    verify,
                ]
            )
            receipt["result"] = "passed"
        finally:
            execute(docker + ["volume", "rm", volume])
            (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
