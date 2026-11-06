"""Run the manifest's real sync scripts against Mountpoint-style permissions.

Usage: python tests/container/zoekt_shard_permissions.py
Requires Docker and PyYAML; pulls only the manifest's pinned BusyBox image.
"""

import subprocess
import tempfile
from pathlib import Path

import yaml


def main():
    manifest = Path(__file__).resolve().parents[2] / "manifests/zoekt.yaml"
    deployment = next(yaml.safe_load_all(manifest.read_text()))
    pod = deployment["spec"]["template"]["spec"]
    init = pod["initContainers"][0]
    sync = next(c for c in pod["containers"] if c["name"] == "shard-sync")
    image = init["image"]
    assert image == sync["image"] and "@sha256:" in image

    with tempfile.TemporaryDirectory(prefix="zoekt-permissions-") as tmp:
        root = Path(tmp)

        def fixture(script):
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network=none",
                    "-v",
                    f"{root}:/fixture",
                    image,
                    "sh",
                    "-ec",
                    script,
                ],
                check=True,
                timeout=60,
            )

        def run(container, script, *, reader=False):
            security = pod.get("securityContext", {}) | container.get("securityContext", {})
            # The serving image declares USER 100:101. BusyBox defaults to root.
            uid = 100 if reader else security.get("runAsUser", 0)
            gid = 101 if reader else security.get("runAsGroup", 0)
            command = [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--user",
                f"{uid}:{gid}",
            ]
            for group in pod.get("securityContext", {}).get("supplementalGroups", []):
                command += ["--group-add", str(group)]
            command += ["--env", "SYNC_ONCE=true"]
            command += [
                "-v",
                f"{root}/source:/mnt/s3-shards:ro",
                "-v",
                f"{root}/scratch:/data",
                image,
                "sh",
                "-ec",
                script,
            ]
            subprocess.run(command, check=True, timeout=60)

        try:
            fixture("""
                mkdir -p /fixture/source/repo /fixture/scratch
                chmod 777 /fixture/scratch
                printf initial > /fixture/source/repo/initial.zoekt
                chown -R 0:10001 /fixture/source
                chmod 750 /fixture/source /fixture/source/repo
                chmod 640 /fixture/source/repo/initial.zoekt
            """)
            run(init, init["args"][0])
            run({}, 'test "$(cat /data/index/initial.zoekt)" = initial', reader=True)

            fixture("""
                rm /fixture/source/repo/initial.zoekt
                printf added > /fixture/source/repo/added.zoekt
                chown 0:10001 /fixture/source/repo/added.zoekt
                chmod 640 /fixture/source/repo/added.zoekt
            """)
            # Execute one actual sidecar cycle without the scheduling delay.
            cycle = sync["args"][0]
            run(sync, cycle)
            run(
                {},
                "test ! -e /data/index/initial.zoekt; "
                'test "$(cat /data/index/added.zoekt)" = added',
                reader=True,
            )
            run(sync, cycle)
            run({}, 'test "$(cat /data/index/added.zoekt)" = added', reader=True)
            print("PASS: initial/new shards readable by server; stale cleanup and repeat sync work")
        finally:
            fixture("rm -rf /fixture/source /fixture/scratch")


if __name__ == "__main__":
    main()
