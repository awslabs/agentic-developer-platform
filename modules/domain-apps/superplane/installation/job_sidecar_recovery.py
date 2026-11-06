"""Review, then gracefully stop an inherited listener in a successful install Job.

This preserves the original Job and main-container result. It never marks a Job
successful, retries migration, deletes a Job, or changes an installation receipt.
"""

import argparse
import datetime
import hashlib
import json
import subprocess
from pathlib import Path

import boto3

from .config import LABEL, image, require


def observation(receipt, job, pod, phase):
    name = f"superplane-{phase}-{receipt['run_id']}"
    namespace = receipt["environment"]["namespace"]
    recorded = [
        x
        for x in receipt["objects"]
        if x["kind"] == "Job" and x["name"] == name and x["namespace"] == namespace
    ]
    require(len(recorded) == 1, "Exact recorded installation Job required")
    require(
        job["metadata"]["uid"] == recorded[0]["uid"]
        and job["metadata"]["name"] == name
        and job["metadata"]["namespace"] == namespace
        and job["metadata"]["labels"].get(LABEL) == receipt["installation_id"]
        and not job["metadata"].get("deletionTimestamp"),
        "Job ownership differs",
    )
    require(
        not any(
            c["type"] in {"Complete", "Failed"} and c["status"] == "True"
            for c in job.get("status", {}).get("conditions", [])
        ),
        "Job must still be nonterminal",
    )
    require(
        pod["metadata"]["namespace"] == namespace
        and pod["metadata"]["labels"].get(LABEL) == receipt["installation_id"]
        and not pod["metadata"].get("deletionTimestamp")
        and any(
            r.get("controller") is True
            and r.get("kind") == "Job"
            and r.get("uid") == recorded[0]["uid"]
            for r in pod["metadata"].get("ownerReferences", [])
        ),
        "Pod ownership differs",
    )
    containers = pod["spec"]["containers"]
    require(
        [c["name"] for c in containers] == [phase, "recovery-transport"]
        and pod["spec"]["restartPolicy"] == "Never",
        "Unexpected Job containers",
    )
    expected = image(receipt["release_lock"], "superplane-api")
    require(all(c["image"] == expected for c in containers), "Job image differs")
    main_command = (
        ["python", "-m", "app.installation", "migrate"]
        if phase == "migrate"
        else ["python", "-m", "app.installation_bootstrap"]
    )
    require(containers[0]["command"] == main_command, "Unexpected main command")
    command = containers[1]["command"]
    require(
        command
        == [
            "python",
            "-m",
            "uvicorn",
            "app.recovery_transport:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8443",
            "--ssl-certfile",
            "/recovery-tls/tls.crt",
            "--ssl-keyfile",
            "/recovery-tls/tls.key",
            "--no-access-log",
        ],
        "Unexpected recovery listener command",
    )
    statuses = {s["name"]: s for s in pod["status"].get("containerStatuses", [])}
    require(
        set(statuses) == {phase, "recovery-transport"}, "Container state incomplete"
    )
    main, listener = statuses[phase], statuses["recovery-transport"]
    require(
        main.get("restartCount") == listener.get("restartCount") == 0
        and main.get("state", {}).get("terminated", {}).get("exitCode") == 0
        and "running" in listener.get("state", {}),
        "Main must have succeeded and listener must still be running",
    )
    return {
        "job": name,
        "namespace": namespace,
        "job_uid": recorded[0]["uid"],
        "pod": pod["metadata"]["name"],
        "pod_uid": pod["metadata"]["uid"],
        "main_result": main["state"]["terminated"],
        "listener_container_id": listener["containerID"],
        "listener_command": command,
        "image": expected,
    }


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--phase", choices=["migrate", "bootstrap"], required=True)
    parser.add_argument("--approved-observation-sha256")
    args = parser.parse_args()
    receipt = json.loads(args.receipt.read_text())
    env = receipt["environment"]
    session = boto3.Session(region_name=env["region"])
    caller = session.client("sts").get_caller_identity()
    identity = env["deployment_identity"]
    require(
        caller["Account"] == env["account_id"]
        and caller["UserId"].split(":")[0] == identity["expected_role_id"],
        "Selected installer identity differs",
    )
    cluster = session.client("eks").describe_cluster(name=env["cluster"])["cluster"]
    kube = [
        "kubectl",
        "--request-timeout=30s",
        "--kubeconfig",
        str(args.receipt.parent / "kubeconfig"),
    ]

    def read(*parts):
        return json.loads(subprocess.check_output([*kube, *parts], text=True))

    config = read("config", "view", "--raw", "--minify", "-o", "json")["clusters"][0][
        "cluster"
    ]
    require(
        config["server"] == cluster["endpoint"]
        and config.get("certificate-authority-data")
        == cluster["certificateAuthority"]["data"]
        and not config.get("insecure-skip-tls-verify"),
        "Kubeconfig target differs",
    )
    name = f"superplane-{args.phase}-{receipt['run_id']}"
    ns = env["namespace"]

    def current():
        job = read("-n", ns, "get", "job", name, "-o", "json")
        pods = read("-n", ns, "get", "pods", "-l", "job-name=" + name, "-o", "json")[
            "items"
        ]
        require(len(pods) == 1, "One exact installation Job pod required")
        start = datetime.datetime.fromisoformat(
            job["status"]["startTime"].replace("Z", "+00:00")
        )
        require(
            (datetime.datetime.now(datetime.timezone.utc) - start).total_seconds()
            < job["spec"]["activeDeadlineSeconds"] - 30,
            "Job deadline too close or elapsed",
        )
        return observation(receipt, job, pods[0], args.phase)

    observed = current()
    pin = digest(observed)
    print(
        json.dumps(
            {
                "observation": observed,
                "sha256": pin,
                "action": "SIGTERM only the inherited uvicorn listener",
            }
        ),
        flush=True,
    )
    if args.approved_observation_sha256 is None:
        return
    require(
        args.approved_observation_sha256 == pin and current() == observed,
        "Current Job differs from approved observation",
    )
    program = (
        "import json,os,signal,sys; "
        'actual=open("/proc/1/cmdline","rb").read().rstrip(b"\\0").split(b"\\0"); '
        "assert [x.decode() for x in actual]==json.loads(sys.argv[1]); "
        "os.kill(1,signal.SIGTERM)"
    )
    subprocess.run(
        [
            *kube,
            "-n",
            ns,
            "exec",
            observed["pod"],
            "-c",
            "recovery-transport",
            "--",
            "python",
            "-c",
            program,
            json.dumps(observed["listener_command"]),
        ],
        check=True,
        timeout=40,
    )
    print(
        "Graceful listener shutdown requested; verify actual Job completion.",
        flush=True,
    )


if __name__ == "__main__":
    main()
