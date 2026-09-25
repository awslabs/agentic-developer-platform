"""Installed native-node transport contract and immutable artifact verification.

This module uses only the Python standard library. Installed launchers run a
private, manifest-pinned interpreter with isolated imports; configuration cannot
select an executable, extra config source, credential source, or retry count.
"""

import base64
from datetime import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

NODEADM_COMMIT = "ffc658f85bb8732e130898850802b100aee507e9"
RUNTIME_ROOT = Path("/opt/superplane/node-runtime")
MANIFEST_PATH = RUNTIME_ROOT / "manifest.json"
CNI_ROOT = Path("/opt/cni/bin")
BOOTSTRAP_LIMIT = 290
PROBE_LIMIT = 40
HEX = re.compile(r"[0-9a-f]{64}")
MANIFEST_FIELDS = frozenset(
    {
        "version",
        "nodeadm_commit",
        "artifact_sha256",
        "architecture",
        "kubelet_version",
        "containerd_version",
        "cni",
        "cni_version",
        "nvidia_driver_version",
        "nvidia_runtime_version",
        "device_plugin_image",
        "ssm_agent_version",
    }
)
COMMON_FIELDS = frozenset(
    {
        "version",
        "purpose",
        "operation_id",
        "attempt_id",
        "fence_token",
        "allocation_id",
        "org_id",
        "workspace_id",
        "cluster_arn",
        "account_id",
        "region",
        "availability_zone",
        "instance_id",
        "image_id",
        "wrapper_sha256",
        "runtime_deadline",
        "nonce",
        "endpoint",
        "certificate_authority",
        "cidrs",
        "runtime_manifest",
    }
)
REQUIRED_FILES = frozenset(
    {
        "/usr/bin/nodeadm",
        "/usr/bin/nodeadm-internal",
        "/usr/bin/containerd",
        "/usr/bin/kubelet",
        "/usr/bin/nvidia-container-runtime",
        "/usr/bin/nvidia-smi",
        "/usr/bin/amazon-ssm-agent",
        "/usr/bin/systemctl",
        "/opt/superplane/bin/node-bootstrap-v1",
        "/opt/superplane/bin/node-probe-v1",
    }
)


class RunnerRefused(ValueError):
    """No success receipt may be emitted for an unproven node boundary."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def contract_digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RunnerRefused("duplicate contract field")
        result[key] = value
    return result


def decode_json(raw):
    return json.loads(
        raw,
        object_pairs_hook=_object,
        parse_constant=lambda _: (_ for _ in ()).throw(
            RunnerRefused("nonfinite number")
        ),
    )


def validate_runtime_manifest(value):
    if not isinstance(value, dict) or set(value) != MANIFEST_FIELDS:
        raise RunnerRefused("closed runtime manifest required")
    fixed = {
        "version": 1,
        "nodeadm_commit": NODEADM_COMMIT,
        "architecture": "x86_64",
        "cni": "aws-vpc-cni",
    }
    if type(value["version"]) is not int or any(
        value[k] != v for k, v in fixed.items()
    ):
        raise RunnerRefused("unsupported native runtime")
    patterns = {
        "artifact_sha256": r"[0-9a-f]{64}",
        "kubelet_version": r"v1\.[0-9]{1,2}\.[0-9]{1,3}",
        "containerd_version": r"[12]\.[0-9]{1,2}\.[0-9]{1,3}",
        "cni_version": r"v[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,3}",
        "nvidia_driver_version": r"[0-9]{2,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?",
        "nvidia_runtime_version": r"[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,3}",
        "device_plugin_image": r"sha256:[0-9a-f]{64}",
        "ssm_agent_version": r"[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,5}\.[0-9]{1,3}",
    }
    if any(
        not isinstance(value[k], str) or not re.fullmatch(p, value[k])
        for k, p in patterns.items()
    ):
        raise RunnerRefused("unresolved runtime manifest")
    if (
        value["artifact_sha256"] == "0" * 64
        or value["device_plugin_image"] == "sha256:" + "0" * 64
    ):
        raise RunnerRefused("placeholder runtime manifest")
    return dict(value)


def deadline(contract):
    value = datetime.fromisoformat(contract["runtime_deadline"])
    if value.tzinfo is None or value.utcoffset().total_seconds() != 0:
        raise RunnerRefused("original UTC deadline required")
    return value.timestamp()


def validate_node_config(config, contract):
    if not isinstance(config, dict) or set(config) != {"apiVersion", "kind", "spec"}:
        raise RunnerRefused("closed NodeConfig required")
    spec = config.get("spec")
    if (
        config["apiVersion"] != "node.eks.aws/v1alpha1"
        or config["kind"] != "NodeConfig"
        or not isinstance(spec, dict)
        or set(spec) != {"cluster", "kubelet"}
    ):
        raise RunnerRefused("NodeConfig overrides refused")
    cluster, kubelet = spec["cluster"], spec["kubelet"]
    if (
        not isinstance(cluster, dict)
        or set(cluster) != {"name", "apiServerEndpoint", "certificateAuthority", "cidr"}
        or cluster["name"] != contract["cluster_arn"].split("/", 1)[1]
        or cluster["apiServerEndpoint"] != contract["endpoint"]
        or cluster["certificateAuthority"] != contract["certificate_authority"]
    ):
        raise RunnerRefused("NodeConfig cluster identity differs")
    if ipaddress.ip_network(cluster["cidr"], strict=True).version != 4:
        raise RunnerRefused("native v1 requires IPv4 service CIDR")
    if not isinstance(kubelet, dict) or set(kubelet) != {"flags"}:
        raise RunnerRefused("extra kubelet configuration refused")
    flags = kubelet["flags"]
    if (
        not isinstance(flags, list)
        or len(flags) != 2
        or not all(isinstance(v, str) for v in flags)
    ):
        raise RunnerRefused("exact ownership flags required")
    match = re.fullmatch(
        r"--node-labels=superplane.ai/capacity=([a-z0-9][a-z0-9-]{0,62}),superplane.ai/workspace="
        + re.escape(contract["workspace_id"]),
        flags[0],
    )
    if (
        not match
        or flags[1]
        != f"--register-with-taints=superplane.ai/capacity={match[1]}:NoSchedule"
    ):
        raise RunnerRefused("node ownership flags differ")
    return config


def decode_contract(encoded, purpose):
    try:
        if not isinstance(encoded, str) or not 1 <= len(encoded) <= 16384:
            raise RunnerRefused("bounded contract required")
        raw = base64.b64decode(encoded, validate=True)
        if base64.b64encode(raw).decode() != encoded or len(raw) > 12288:
            raise RunnerRefused("canonical bounded base64 required")
        value = decode_json(raw)
        expected = COMMON_FIELDS | (
            {"node_config"} if purpose == "node-bootstrap" else set()
        )
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or raw.decode() != canonical(value)
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["purpose"] != purpose
            or purpose not in {"node-bootstrap", "node-api-dns-tls"}
        ):
            raise RunnerRefused("closed command contract required")
        for key in (
            "operation_id",
            "attempt_id",
            "allocation_id",
            "org_id",
            "workspace_id",
        ):
            if not isinstance(value[key], str) or not re.fullmatch(
                r"[A-Za-z0-9_.:#-]{1,255}", value[key]
            ):
                raise RunnerRefused("opaque original identity required")
        patterns = {
            "account_id": r"[0-9]{12}",
            "region": r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]",
            "instance_id": r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})",
            "image_id": r"ami-(?:[0-9a-f]{8}|[0-9a-f]{17})",
            "nonce": r"[0-9a-f]{64}",
            "wrapper_sha256": r"[0-9a-f]{64}",
        }
        if any(
            not isinstance(value[k], str) or not re.fullmatch(p, value[k])
            for k, p in patterns.items()
        ):
            raise RunnerRefused("native instance identity required")
        if (
            type(value["fence_token"]) is not int
            or value["fence_token"] <= 0
            or not re.fullmatch(
                re.escape(value["region"]) + r"(?:[a-z]|-[a-z0-9-]+)",
                value["availability_zone"],
            )
            or not re.fullmatch(
                r"arn:aws:eks:[a-z0-9-]+:"
                + value["account_id"]
                + r":cluster/[A-Za-z0-9][A-Za-z0-9_-]{0,99}",
                value["cluster_arn"],
            )
        ):
            raise RunnerRefused("original regional identity required")
        endpoint = urlsplit(value["endpoint"])
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.port not in {None, 443}
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or endpoint.path not in {"", "/"}
            or any(ord(c) < 33 for c in value["endpoint"])
        ):
            raise RunnerRefused("exact HTTPS API endpoint required")
        ca = base64.b64decode(value["certificate_authority"], validate=True)
        if not ca or len(ca) > 8192:
            raise RunnerRefused("bounded pinned CA required")
        cidrs = value["cidrs"]
        if (
            not isinstance(cidrs, list)
            or not 1 <= len(cidrs) <= 4
            or len(set(cidrs)) != len(cidrs)
        ):
            raise RunnerRefused("bounded private CIDRs required")
        for item in cidrs:
            network = ipaddress.ip_network(item, strict=True)
            if network.version != 4 or not any(
                network.subnet_of(ipaddress.ip_network(r))
                for r in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
            ):
                raise RunnerRefused("approved RFC1918 network required")
        validate_runtime_manifest(value["runtime_manifest"])
        if deadline(value) <= time.time():
            raise RunnerRefused("original deadline expired")
        if purpose == "node-bootstrap":
            validate_node_config(value["node_config"], value)
        return value
    except (KeyError, TypeError, AttributeError, UnicodeError, ValueError) as exc:
        raise RunnerRefused("node contract unavailable or refused") from exc


def secure_path(path, *, directory=False):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise RunnerRefused("absolute installed path required")
    for current in [*reversed(path.parents), path]:
        info = current.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise RunnerRefused("installed path is not root-owned and immutable")
        if current != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise RunnerRefused("installed directory required")
        elif not stat.S_ISREG(info.st_mode):
            raise RunnerRefused("installed regular file required")
    return path


def file_digest(path):
    path = secure_path(path)
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def tree_digest(path):
    path = secure_path(path, directory=True)
    entries = {}
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs:
            secure_path(Path(root) / name, directory=True)
        for name in files:
            file = Path(root) / name
            if file == MANIFEST_PATH:
                continue
            entries[str(file.relative_to(path))] = file_digest(file)
    if not entries:
        raise RunnerRefused("empty executable closure")
    return contract_digest(entries)


def verify_installation(contract, runner_file):
    manifest = validate_runtime_manifest(contract["runtime_manifest"])
    if platform.machine() != manifest["architecture"] or os.geteuid() != 0:
        raise RunnerRefused("installed native root runtime required")
    secure_path(MANIFEST_PATH)
    raw = MANIFEST_PATH.read_bytes()
    if (
        len(raw) > 65536
        or hashlib.sha256(raw).hexdigest() != manifest["artifact_sha256"]
    ):
        raise RunnerRefused("installed artifact manifest differs")
    artifact = decode_json(raw)
    expected_runtime = {k: v for k, v in manifest.items() if k != "artifact_sha256"}
    if (
        set(artifact) != {"version", "runtime", "files", "trees"}
        or artifact["version"] != 1
        or artifact["runtime"] != expected_runtime
        or raw.decode() != canonical(artifact)
    ):
        raise RunnerRefused("installed runtime manifest differs")
    files, trees = artifact["files"], artifact["trees"]
    if (
        not isinstance(files, dict)
        or not REQUIRED_FILES <= files.keys()
        or len(files) > 256
        or not isinstance(trees, dict)
        or not {str(RUNTIME_ROOT), str(CNI_ROOT)} <= trees.keys()
        or len(trees) > 16
    ):
        raise RunnerRefused("complete installed executable closure required")
    for paths, digest in ((files, file_digest), (trees, tree_digest)):
        for path, expected in paths.items():
            if (
                not isinstance(expected, str)
                or not HEX.fullmatch(expected)
                or digest(path) != expected
            ):
                raise RunnerRefused("installed executable closure differs")
    if Path(runner_file) != RUNTIME_ROOT / (
        "node_bootstrap_runner.py"
        if contract["purpose"] == "node-bootstrap"
        else "node_probe_runner.py"
    ):
        raise RunnerRefused("fixed installed wrapper required")
    if file_digest(runner_file) != contract["wrapper_sha256"]:
        raise RunnerRefused("wrapper artifact differs")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RunnerRefused("IMDS redirect refused")


def instance_identity():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    base = "http://169.254.169.254/latest/"
    request = urllib.request.Request(
        base + "api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
    )
    with opener.open(request, timeout=3) as response:
        token = response.read(4097).decode()
    if not token or len(token) > 4096 or "\n" in token:
        raise RunnerRefused("IMDSv2 token unavailable")
    request = urllib.request.Request(
        base + "dynamic/instance-identity/document",
        headers={"X-aws-ec2-metadata-token": token},
    )
    with opener.open(request, timeout=3) as response:
        raw = response.read(8193)
    if len(raw) > 8192:
        raise RunnerRefused("IMDS identity too large")
    return decode_json(raw)


def verify_instance(contract):
    actual = instance_identity()
    for field, key in {
        "account_id": "accountId",
        "region": "region",
        "availability_zone": "availabilityZone",
        "instance_id": "instanceId",
        "image_id": "imageId",
    }.items():
        if actual.get(key) != contract[field]:
            raise RunnerRefused("original native instance differs")
    if deadline(contract) <= time.time():
        raise RunnerRefused("original deadline expired")


def clean_environment():
    return {
        "PATH": "/usr/bin:/usr/sbin:/bin",
        "LANG": "C",
        "AWS_MAX_ATTEMPTS": "3",
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_EC2_METADATA_V1_DISABLED": "true",
    }


def fixed_command(arguments, *, timeout=5):
    return subprocess.run(
        arguments,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=clean_environment(),
        timeout=timeout,
    ).stdout.decode()


def verify_native_runtime(contract):
    runtime = contract["runtime_manifest"]
    for argv, expected in (
        (["/usr/bin/kubelet", "--version"], runtime["kubelet_version"]),
        (["/usr/bin/containerd", "--version"], runtime["containerd_version"]),
        (
            ["/usr/bin/nvidia-container-runtime", "--version"],
            runtime["nvidia_runtime_version"],
        ),
        (["/usr/bin/amazon-ssm-agent", "-version"], runtime["ssm_agent_version"]),
    ):
        output = fixed_command(argv)
        if len(output) > 8192 or not re.search(
            r"(?<![A-Za-z0-9.])"
            + ("" if expected.startswith("v") else "v?")
            + re.escape(expected)
            + r"(?![A-Za-z0-9.])",
            output,
        ):
            raise RunnerRefused("installed native runtime version differs")
    drivers = fixed_command(
        ["/usr/bin/nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"]
    ).splitlines()
    if (
        not drivers
        or len(drivers) > 32
        or any(v.strip() != runtime["nvidia_driver_version"] for v in drivers)
    ):
        raise RunnerRefused("installed GPU driver differs")


def success_receipt(contract, probe=None):
    return {
        "version": 1,
        "purpose": contract["purpose"],
        "nonce": contract["nonce"],
        **{
            k: contract[k]
            for k in (
                "instance_id",
                "account_id",
                "region",
                "availability_zone",
                "image_id",
            )
        },
        "contract_sha256": contract_digest(contract),
        "status": "succeeded",
        "probe": probe,
    }


def _child(connection, action, contract):
    os.setsid()
    try:
        result = canonical(action(contract)).encode()
        if len(result) > 8192:
            raise RunnerRefused("receipt too large")
        connection.send_bytes(result)
    except BaseException:
        # Never expose nodeadm output, IMDS tokens, environment, or arbitrary
        # exception text through SSM. Absence of success remains uncertain.
        pass
    finally:
        connection.close()


def bounded_execution(action, contract):
    """Bound DNS, file checks and subprocesses together, killing the process group.

    Systemd jobs already enqueued by nodeadm are outside this group: timeout is
    uncertain and must never authorize re-entry or release of original compute.
    """
    import multiprocessing
    import signal

    limit = BOOTSTRAP_LIMIT if contract["purpose"] == "node-bootstrap" else PROBE_LIMIT
    seconds = min(limit, deadline(contract) - time.time())
    if seconds <= 0:
        raise RunnerRefused("original deadline expired")
    end = time.monotonic() + seconds
    ctx = multiprocessing.get_context("fork")
    receiver, sender = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_child, args=(sender, action, contract))
    process.start()
    sender.close()
    try:
        if not receiver.poll(max(0, end - time.monotonic())):
            raise RunnerRefused("node command deadline exhausted")
        result = decode_json(receiver.recv_bytes(8192))
        process.join(timeout=max(0, end - time.monotonic()))
        if (
            process.is_alive()
            or process.exitcode != 0
            or deadline(contract) <= time.time()
        ):
            raise RunnerRefused("node command did not finish within original deadline")
        return result
    except (EOFError, OSError) as exc:
        raise RunnerRefused("node command produced no valid success") from exc
    finally:
        receiver.close()
        # Also reap subprocess descendants when the leader already exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            if process.is_alive():
                process.kill()
        process.join()
        process.close()


def entrypoint(purpose, action, argv):
    try:
        if len(argv) != 1:
            raise RunnerRefused("one immutable contract required")
        contract = decode_contract(argv[0], purpose)
        result = bounded_execution(action, contract)
        print(canonical(result), flush=True)
        return 0
    except (RunnerRefused, OSError, subprocess.SubprocessError):
        # A stable non-secret failure. No success-shaped partial output.
        return 1
