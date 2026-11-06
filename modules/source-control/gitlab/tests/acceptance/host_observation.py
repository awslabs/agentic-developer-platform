#!/usr/bin/env python3
"""Read Docker/kernel facts on a trusted job host without exporting credentials.

The caller must collect this outside the tenant job and bind the resulting
records to independent EC2/GitLab readbacks. This module never asserts a live
acceptance verdict or treats job output as host inventory.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time

PREFIX = "com.gitlab.gitlab-runner."


class ObservationError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ObservationError(code)


class DockerConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=3)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


def inspect_container(container_id):
    require(re.fullmatch(r"[0-9a-f]{64}", container_id), "container_id_invalid")
    connection = DockerConnection()
    try:
        connection.request("GET", f"/containers/{container_id}/json")
        response = connection.getresponse()
        require(response.status == 200, "container_inspection_missing")
        content = response.read(2 * 1024 * 1024 + 1)
        require(len(content) <= 2 * 1024 * 1024, "container_inspection_too_large")
        return json.loads(content)
    finally:
        connection.close()


def kernel_identity(pid, proc=Path("/proc")):
    require(type(pid) is int and pid > 0, "running_container_pid_required")
    status = (proc / str(pid) / "status").read_text()
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    uids = [int(value) for value in fields["Uid"].split()]
    capabilities = fields["CapEff"].strip()
    require(len(uids) == 4 and re.fullmatch(r"[0-9a-f]{16}", capabilities), "kernel_identity_invalid")
    # Compare actual namespace identities, not just Docker's requested modes.
    namespaces = {}
    for name in ("net", "pid", "ipc", "mnt", "user", "uts"):
        instance = (proc / str(pid) / "ns" / name).stat().st_ino
        host = (proc / "1" / "ns" / name).stat().st_ino
        namespaces[name] = {"inode": instance, "host_inode": host, "same_as_host": instance == host}
    return {"uids": uids, "effective_capabilities_hex": capabilities,
            "no_new_privileges": fields["NoNewPrivs"].strip() == "1", "namespaces": namespaces}


def enforcement_identity(root=Path("/"), *, run=subprocess.run, ready_required=True):
    """Read the actual guard's verification and kernel-owned readiness times.

The installed guard compares its pinned source, persisted policy and live nft
table and checks the real ENA interface. A missing guard cannot be interpreted
as installed isolation, including on a local Docker-only test machine.
"""
    guard = root / "usr/local/libexec/adp-job-host"
    loaded = root / "etc/adp/job-firewall.loaded"
    ready = root / "run/adp-job-ready"
    policy = root / "etc/adp/job-firewall.nft"
    before = {"guard_sha256": hashlib.sha256(guard.read_bytes()).hexdigest(),
              "policy_sha256": hashlib.sha256(policy.read_bytes()).hexdigest(),
              "denial_installed_at_unix_ns": loaded.stat().st_mtime_ns,
              "executor_ready_at_unix_ns": ready.stat().st_mtime_ns if ready.exists() else None}
    require(not ready_required or before["executor_ready_at_unix_ns"] is not None,
            "host_executor_not_ready")
    result = run([str(guard), "--verify" if ready_required else "--network-verify"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                 timeout=5, check=False, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
    require(result.returncode == 0, "host_guard_verification_failed")
    after = {"guard_sha256": hashlib.sha256(guard.read_bytes()).hexdigest(),
             "policy_sha256": hashlib.sha256(policy.read_bytes()).hexdigest(),
             "denial_installed_at_unix_ns": loaded.stat().st_mtime_ns,
             "executor_ready_at_unix_ns": ready.stat().st_mtime_ns if ready.exists() else None}
    require(before == after and 0 < before["denial_installed_at_unix_ns"]
            and (before["executor_ready_at_unix_ns"] is None
                 or before["denial_installed_at_unix_ns"] <= before["executor_ready_at_unix_ns"]),
            "host_enforcement_changed_during_observation")
    return {"status": "verified", **before}


def startup_identity(*, run=subprocess.run, enforcement=enforcement_identity):
    """Record real service lifetimes and deny policy before any job admission."""
    services = {}
    for service in ("docker.service", "adp-job-firewall.service"):
        result = run(["systemctl", "show", service, "--property=InvocationID", "--value"],
                     capture_output=True, text=True, timeout=5, check=False)
        invocation = result.stdout.strip()
        require(result.returncode == 0 and re.fullmatch(r"[0-9a-f]{32}", invocation),
                "host_service_invocation_missing")
        services[service] = invocation
    guard = enforcement(ready_required=False)
    require(guard["executor_ready_at_unix_ns"] is None, "observer_started_after_admission")
    return {"service_invocations": services, "host_enforcement": guard}


def observation(container_id, expected, *, inspect=inspect_container, kernel=kernel_identity,
                enforcement=enforcement_identity, now=time.time_ns):
    raw = inspect(container_id)
    require(raw.get("Id") == container_id, "container_identity_wrong")
    config, host, state = raw["Config"], raw["HostConfig"], raw["State"]
    labels = config.get("Labels") or {}
    require(labels.get(PREFIX + "managed") == "true", "not_a_runner_container")
    identity = {}
    for name in ("job.id", "project.id", "pipeline.id"):
        value = labels.get(PREFIX + name, "")
        require(re.fullmatch(r"[1-9][0-9]*", value), "provider_correlation_label_missing")
        identity[name] = int(value)
    source = labels.get(PREFIX + "job.sha", "")
    require(re.fullmatch(r"[0-9a-f]{40}", source), "source_correlation_label_missing")
    require(identity == {"job.id": expected["job_id"], "project.id": expected["project_id"],
                         "pipeline.id": expected["pipeline_id"]}
            and source == expected["source_revision"], "container_outside_owned_attempt")
    kind = labels.get(PREFIX + "type")
    require(kind in {"build", "predefined", "service"}, "container_kind_missing")
    require(state.get("Running") is True, "historical_kernel_observation_missing")
    identity_at_start = kernel(state["Pid"])
    try:
        guard = enforcement()
    except Exception:
        guard = {"status": "unavailable"}
    # Never serialize raw Config: Env includes CI_JOB_TOKEN; Cmd, health checks,
    # labels and registry configuration can also contain credentials or job data.
    record = {
        "schema_version": 1, "observed_at_unix_ns": now(), "source": "trusted_host_docker_and_kernel",
        "container_id": container_id, "kind": kind, "job_id": expected["job_id"],
        "project_id": expected["project_id"], "pipeline_id": expected["pipeline_id"],
        "source_revision": source, "image_id": raw["Image"], "configured_user": config["User"],
        "created_at": raw["Created"], "started_at": state["StartedAt"],
        "privileged": host["Privileged"], "cap_add": host.get("CapAdd") or [],
        "cap_drop": host.get("CapDrop") or [], "security_options": host.get("SecurityOpt") or [],
        "network_mode": host["NetworkMode"], "pid_mode": host.get("PidMode", ""),
        "ipc_mode": host["IpcMode"], "userns_mode": host.get("UsernsMode", ""),
        "devices": [{key: device[key] for key in ("PathOnHost", "PathInContainer", "CgroupPermissions")}
                    for device in host.get("Devices") or []],
        "device_requests_present": bool(host.get("DeviceRequests")),
        "mounts": [{key: mount[key] for key in ("Type", "Source", "Destination", "RW")}
                   for mount in raw.get("Mounts", [])],
        "kernel": identity_at_start,
        "host_enforcement": guard,
        "acceptance": "requires_independent_provider_and_historical_verification",
    }
    # A recycled PID or container restart cannot borrow an earlier inspection.
    current = inspect(container_id)
    require(current.get("Id") == container_id and current.get("Image") == raw["Image"]
            and current.get("State", {}).get("Running") is True
            and current["State"].get("Pid") == state["Pid"]
            and current["State"].get("StartedAt") == state["StartedAt"], "container_changed_during_observation")
    require(kernel(state["Pid"]) == identity_at_start, "kernel_identity_changed_during_observation")
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--pipeline-id", type=int, required=True)
    parser.add_argument("--source-revision", required=True)
    args = parser.parse_args()
    try:
        require(os.geteuid() == 0, "trusted_host_root_observer_required")
        require(all(value > 0 for value in (args.job_id, args.project_id, args.pipeline_id))
                and re.fullmatch(r"[0-9a-f]{40}", args.source_revision), "owned_attempt_invalid")
        result = observation(args.container_id, {"job_id": args.job_id, "project_id": args.project_id,
                             "pipeline_id": args.pipeline_id, "source_revision": args.source_revision})
    except Exception:
        # No raw Docker/transport exception can escape with environment values.
        print(json.dumps({"observation": "incomplete", "reason": "trusted_host_observation_unavailable"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
