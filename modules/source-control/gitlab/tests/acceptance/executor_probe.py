"""Attempt bounded executor operations; trusted host evidence is still required."""
import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import socket
import tempfile

DENIED = {errno.EPERM, errno.EACCES, errno.ENOENT}
REFUSED_CASES = {"root_user", "arbitrary_image", "arbitrary_service"}
NAMESPACE_CASES = {"host_network": "net", "host_pid": "pid", "host_ipc": "ipc"}


def baseline():
    fields = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    return {"uid": os.geteuid(), "effective_capabilities": int(fields["CapEff"].strip(), 16),
            "no_new_privileges": fields["NoNewPrivs"].strip() == "1"}


def open_without_reading(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    os.close(descriptor)


def denied(operation):
    try:
        operation()
    except OSError as error:
        if error.errno not in DENIED:
            raise
        return {"result": "inaccessible", "errno": error.errno}
    return {"result": "accessible"}


def namespace(kind):
    with open(f"/proc/1/ns/{kind}", "rb") as stream:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.setns(stream.fileno(), 0) != 0:
            code = ctypes.get_errno()
            raise OSError(code, "namespace operation refused")


def mount_owned_directory():
    libc = ctypes.CDLL(None, use_errno=True)
    with tempfile.TemporaryDirectory(prefix="adp-mount-attempt-") as directory:
        path = os.fsencode(directory)
        if libc.mount(b"none", path, b"tmpfs", 0, None) != 0:
            code = ctypes.get_errno()
            raise OSError(code, "mount operation refused")
        # Only the freshly created test directory is touched if isolation fails.
        if libc.umount2(path, 0) != 0:
            raise RuntimeError("owned test mount cleanup failed")


def raw_socket():
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW):
        pass


def docker_socket():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(1)
        client.connect("/var/run/docker.sock")


def probe(case):
    observed = baseline()
    result = {"name": case, "process": observed, "observations": [],
              "trusted_host_verified": False}
    confined = observed["uid"] > 0 and observed["effective_capabilities"] == 0 and observed["no_new_privileges"]
    if not confined:
        result["probe_result"] = "unsafe_process"
        return result
    if case in REFUSED_CASES:
        # Root override may be safely ignored in favor of the configured UID.
        result["probe_result"] = "nonroot_override_constrained" if case == "root_user" else "unexpected_script_admission"
        return result
    operations = []
    if case == "privileged":
        operations = [mount_owned_directory]
    elif case in NAMESPACE_CASES:
        operations = [lambda: namespace(NAMESPACE_CASES[case])]
    elif case == "host_path":
        operations = [lambda: open_without_reading("/proc/1/root/run/adp-job-ready")]
    elif case == "host_device":
        operations = [lambda: open_without_reading("/dev/nvme0n1"), lambda: open_without_reading("/dev/xvda")]
    elif case == "added_capability":
        operations = [raw_socket]
    elif case == "docker_socket":
        operations = [docker_socket]
    elif case == "manager_credentials":
        names = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE")
        result["credential_environment_present"] = any(os.environ.get(name) for name in names)
        if result["credential_environment_present"]:
            result["probe_result"] = "credential_environment_exposed"
            return result
        operations = [lambda path=path: open_without_reading(path) for path in (
            "/runner-credentials/runner-token", "/secrets/runner-token",
            "/var/run/secrets/eks.amazonaws.com/serviceaccount/token", "/root/.aws/credentials")]
    elif case == "debug_trace":
        result["probe_result"] = "requires_independent_trace_scan"
        return result
    else:
        raise ValueError("unknown reviewed executor case")
    result["observations"] = [denied(operation) for operation in operations]
    result["probe_result"] = "inaccessible" if all(item["result"] == "inaccessible" for item in result["observations"]) else "unexpected_access"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = probe(args.case)
    args.output.write_text(json.dumps(result, sort_keys=True) + "\n")
    # No manager credential, host file content or process environment is emitted.
    print("Executor attempt recorded; independent host and provider verification required")
    return 0 if result["probe_result"] in {"inaccessible", "nonroot_override_constrained", "requires_independent_trace_scan"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
