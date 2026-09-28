"""Exercise the packaged SkyPilot API without cloud credentials or network access.

Run: python verify_container.py IMAGE (requires Docker and PyYAML on the host).
Uses the deployed command and USER environment from the Kubernetes manifest,
with a temporary writable HOME and SQLite for this isolated compatibility test.
Live Postgres, IRSA and provisioning acceptance remain separate.
"""

import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    manifests = yaml.safe_load_all((root / "k8s/40-skypilot-api.yaml").read_text())
    deployment = next(d for d in manifests if d and d["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    container = next(c for c in pod["containers"] if c["name"] == "skypilot-api")
    username = next(e["value"] for e in container["env"] if e["name"] == "USER")
    name = "security-skypilot-" + uuid.uuid4().hex[:12]

    def run(*command, timeout=120):
        return subprocess.check_output(command, text=True, timeout=timeout).strip()

    def execute(code):
        return run("docker", "exec", name, "python3", "-c", code)

    def healthy():
        for _ in range(90):
            try:
                result = execute(
                    "import json,urllib.request; print(urllib.request.urlopen('http://127.0.0.1:46580/api/health',timeout=2).read().decode())"
                )
                health = json.loads(result)
                assert health["status"] == "healthy" and health["version"] == "0.12.3"
                return health
            except subprocess.CalledProcessError:
                if (
                    run("docker", "inspect", name, "--format", "{{.State.Running}}")
                    != "true"
                ):
                    raise RuntimeError(run("docker", "logs", name)) from None
                time.sleep(1)
        raise RuntimeError("API startup timeout: " + run("docker", "logs", name))

    try:
        run(
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "none",
            "--cpus",
            "2",
            "--memory",
            "3g",
            "--user",
            str(pod["securityContext"]["runAsUser"])
            + ":"
            + str(pod["securityContext"]["runAsGroup"]),
            "--tmpfs",
            "/test-home:uid=1000,gid=1000,mode=700",
            "-e",
            "HOME=/test-home",
            "-e",
            "USER=" + username,
            "-e",
            "AWS_EC2_METADATA_DISABLED=true",
            "-e",
            "SKYPILOT_DISABLE_USAGE_COLLECTION=1",
            "-e",
            "SKYPILOT_API_SERVER_ENDPOINT=http://127.0.0.1:46580",
            args.image,
            *container["command"],
            *container["args"],
        )
        healthy()
        execute(
            "import os,sys,ssl,sqlite3,bz2,lzma,ctypes,getpass; assert os.getuid()==1000; assert getpass.getuser()=='sky'; assert sys.version_info[:3]==(3,10,21)"
        )
        execute(
            "import sky; task=sky.Task.from_yaml_config({'name':'security-test','resources':{'cloud':'aws','cpus':'4+','memory':'16+'},'run':'echo ok'}); assert task.run=='echo ok'; assert str(next(iter(task.resources)).cloud).lower()=='aws'"
        )
        execute("import sky; assert sky.get(sky.status()) == []")
        run("docker", "exec", name, "python3", "-m", "pip", "check")
        kubectl = json.loads(
            run(
                "docker",
                "exec",
                name,
                "kubectl",
                "version",
                "--client=true",
                "-o",
                "json",
            )
        )
        assert kubectl["clientVersion"]["gitVersion"] == "v1.35.9"
        run("docker", "restart", "--time", "10", name)
        healthy()
        execute("import sky; assert sky.get(sky.status()) == []")
        print(
            json.dumps(
                {
                    "image": args.image,
                    "health": "passed",
                    "restart": "passed",
                    "status_request_roundtrip": "passed",
                    "task_parser": "passed",
                    "uid": 1000,
                    "python": "3.10.21",
                    "kubectl": "v1.35.9",
                    "cloud_network": "disabled",
                    "live_postgres_and_provisioning": "not tested",
                }
            )
        )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


if __name__ == "__main__":
    main()
