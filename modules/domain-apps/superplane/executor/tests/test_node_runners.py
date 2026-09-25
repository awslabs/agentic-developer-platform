"""Native command boundaries using offline contracts and disposable OS state."""

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import stat
import sys
import time
from types import SimpleNamespace
import urllib.error

import pytest

from superplane_executor import node_bootstrap_runner as bootstrap
from superplane_executor import node_probe_runner as probe
from superplane_executor import node_runner as runner


@pytest.fixture
def local_plugin(monkeypatch):
    image_id = "sha256:" + "1" * 64
    container = {
        "id": "2" * 64,
        "state": "CONTAINER_RUNNING",
        "imageRef": image_id,
        "labels": {"io.kubernetes.pod.namespace": "kube-system"},
    }
    image = {
        "id": image_id,
        "repoDigests": ["registry.example/nvidia/plugin@sha256:" + "b" * 64],
    }
    calls = []

    def read(*arguments):
        calls.append(arguments)
        if arguments[0] == "ps":
            return {"containers": [container]}
        if arguments[0] == "inspecti":
            assert arguments[-1] == image_id
            return {"status": image}
        assert arguments == ("inspect", "--output=json", container["id"])
        return {"status": dict(container)}

    monkeypatch.setattr(probe, "cri_json", read)
    return container, image, calls


def test_running_local_device_plugin_resolves_config_id_to_manifest(local_plugin):
    _, _, calls = local_plugin
    probe.verify_device_plugin(contract("node-api-dns-tls"))
    assert [c[0] for c in calls] == ["ps", "inspecti", "inspect"]


@pytest.mark.parametrize("wrong", ["digest", "state", "namespace", "image_identity"])
def test_local_device_plugin_refuses_unproven_image(local_plugin, wrong):
    container, image, _ = local_plugin
    if wrong == "digest":
        image["repoDigests"] = ["registry.example/plugin@sha256:" + "c" * 64]
    elif wrong == "state":
        container["state"] = "CONTAINER_EXITED"
    elif wrong == "namespace":
        container["labels"]["io.kubernetes.pod.namespace"] = "tenant"
    else:
        image["id"] = "sha256:" + "c" * 64
    with pytest.raises(runner.RunnerRefused):
        probe.verify_device_plugin(contract("node-api-dns-tls"))


def test_local_cri_uses_only_fixed_native_endpoint(monkeypatch):
    monkeypatch.setattr(probe, "verify_cri_socket", lambda: None)
    calls = []

    def command(arguments):
        calls.append(arguments)
        return b'{"containers":[]}'

    monkeypatch.setattr(runner, "bounded_command", command)
    assert probe.cri_json("ps", "--output=json") == {"containers": []}
    assert calls == [
        [
            "/usr/bin/crictl",
            "--config=/dev/null",
            "--runtime-endpoint=unix:///run/containerd/containerd.sock",
            "--image-endpoint=unix:///run/containerd/containerd.sock",
            "--timeout=5",
            "ps",
            "--output=json",
        ]
    ]


@pytest.mark.parametrize(
    "mode,uid,gid",
    [
        (stat.S_IFLNK | 0o777, 0, 0),
        (stat.S_IFREG | 0o600, 0, 0),
        (stat.S_IFSOCK | 0o666, 0, 0),
        (stat.S_IFSOCK | 0o660, 1000, 0),
        (stat.S_IFSOCK | 0o660, 0, 1000),
    ],
)
def test_local_cri_refuses_untrusted_socket(monkeypatch, mode, uid, gid):
    monkeypatch.setattr(runner, "secure_path", lambda *_a, **_k: None)
    monkeypatch.setattr(
        probe,
        "CRI_SOCKET",
        SimpleNamespace(
            parent=Path("/run/containerd"),
            lstat=lambda: SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid),
        ),
    )
    with pytest.raises(runner.RunnerRefused, match="socket"):
        probe.verify_cri_socket()


def test_local_cri_refuses_oversized_json(monkeypatch):
    monkeypatch.setattr(probe, "verify_cri_socket", lambda: None)
    monkeypatch.setattr(runner, "bounded_command", lambda *_: b" " * 131073)
    with pytest.raises(runner.RunnerRefused, match="bound"):
        probe.cri_json("ps", "--output=json")


def test_local_cri_capture_bounds_output_during_read():
    with pytest.raises(runner.RunnerRefused, match="bound"):
        runner.bounded_command(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 100000)"],
            limit=1024,
        )


def test_local_cri_capture_times_out_stalled_process():
    with pytest.raises(runner.RunnerRefused, match="timed out"):
        runner.bounded_command(
            [sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.1
        )


def test_local_plugin_rechecks_same_running_container(local_plugin, monkeypatch):
    original = probe.cri_json

    def changed(*arguments):
        result = original(*arguments)
        if arguments[0] == "inspect":
            result["status"]["state"] = "CONTAINER_EXITED"
        return result

    monkeypatch.setattr(probe, "cri_json", changed)
    with pytest.raises(runner.RunnerRefused, match="changed"):
        probe.verify_device_plugin(contract("node-api-dns-tls"))


def manifest():
    return {
        "version": 1,
        "nodeadm_commit": runner.NODEADM_COMMIT,
        "artifact_sha256": "a" * 64,
        "architecture": "x86_64",
        "kubelet_version": "v1.34.1",
        "containerd_version": "2.2.3",
        "cni": "aws-vpc-cni",
        "cni_version": "v1.20.1",
        "nvidia_driver_version": "580.65.06",
        "nvidia_runtime_version": "1.17.8",
        "device_plugin_image": "sha256:" + "b" * 64,
        "ssm_agent_version": "3.3.3050.0",
    }


def contract(purpose="node-bootstrap"):
    value = {
        "version": 1,
        "purpose": purpose,
        "operation_id": "operation",
        "attempt_id": "attempt",
        "fence_token": 7,
        "allocation_id": "allocation",
        "org_id": "organization",
        "workspace_id": "workspace",
        "cluster_arn": "arn:aws:eks:us-east-1:123456789012:cluster/original",
        "account_id": "123456789012",
        "region": "us-east-1",
        "availability_zone": "us-east-1a",
        "instance_id": "i-0123456789abcdef0",
        "image_id": "ami-0123456789abcdef0",
        "wrapper_sha256": "c" * 64,
        "runtime_deadline": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "nonce": "d" * 64,
        "endpoint": "https://original.example.test",
        "certificate_authority": base64.b64encode(b"public-test-ca").decode(),
        "cidrs": ["10.0.0.0/16"],
        "runtime_manifest": manifest(),
    }
    if purpose == "node-bootstrap":
        value["node_config"] = {
            "apiVersion": "node.eks.aws/v1alpha1",
            "kind": "NodeConfig",
            "spec": {
                "cluster": {
                    "name": "original",
                    "apiServerEndpoint": value["endpoint"],
                    "certificateAuthority": value["certificate_authority"],
                    "cidr": "172.20.0.0/16",
                },
                "kubelet": {
                    "flags": [
                        "--node-labels=superplane.ai/capacity=original,superplane.ai/workspace=workspace",
                        "--register-with-taints=superplane.ai/capacity=original:NoSchedule",
                    ]
                },
            },
        }
    return value


def encode(value):
    return base64.b64encode(runner.canonical(value).encode()).decode()


@pytest.mark.parametrize("purpose", ["node-bootstrap", "node-api-dns-tls"])
def test_exact_contract_retains_original_identity(purpose):
    value = contract(purpose)
    assert runner.decode_contract(encode(value), purpose) == value


@pytest.mark.parametrize(
    "field,value",
    [
        ("extra", "ignored"),
        ("fence_token", True),
        ("account_id", "foreign"),
        ("instance_id", "mi-0123456789abcdef0"),
        ("availability_zone", "us-west-2a"),
        ("runtime_deadline", "2020-01-01T00:00:00+00:00"),
        ("endpoint", "https://original.example.test/?credential=private"),
        ("cidrs", ["0.0.0.0/0"]),
    ],
)
def test_contract_refuses_unbound_or_unbounded_inputs(field, value):
    current = contract()
    current[field] = value
    with pytest.raises(runner.RunnerRefused):
        runner.decode_contract(encode(current), "node-bootstrap")


def test_duplicate_json_and_noncanonical_encoding_refused():
    raw = runner.canonical(contract())
    duplicate = raw[:-1] + ',"version":1}'
    for value in (duplicate, json.dumps(contract(), indent=2)):
        with pytest.raises(runner.RunnerRefused):
            runner.decode_contract(
                base64.b64encode(value.encode()).decode(), "node-bootstrap"
            )


@pytest.mark.parametrize(
    "override", ["instance", "containerd", "featureGates", "unknown"]
)
def test_node_config_forbids_mutating_extensions(override):
    value = contract()
    value["node_config"]["spec"][override] = {}
    with pytest.raises(runner.RunnerRefused):
        runner.decode_contract(encode(value), "node-bootstrap")


def test_extra_flags_and_mismatched_taint_refused():
    value = contract()
    flags = value["node_config"]["spec"]["kubelet"]["flags"]
    flags[1] = "--register-with-taints=superplane.ai/capacity=foreign:NoSchedule"
    with pytest.raises(runner.RunnerRefused):
        runner.decode_contract(encode(value), "node-bootstrap")
    flags.append("--container-runtime-endpoint=unix:///foreign.sock")
    with pytest.raises(runner.RunnerRefused):
        runner.decode_contract(encode(value), "node-bootstrap")


@pytest.fixture
def native_machine(monkeypatch, tmp_path):
    # Replace installed AWS/image facts, retaining real exclusive files and
    # subprocess invocation construction. This is not live image evidence.
    monkeypatch.setattr(runner, "verify_installation", lambda *_: None)
    monkeypatch.setattr(runner, "verify_native_runtime", lambda *_: None)
    monkeypatch.setattr(runner, "verify_instance", lambda *_: None)
    monkeypatch.setattr(bootstrap, "verify_bootstrap_exclusive", lambda: None)
    monkeypatch.setattr(runner, "secure_path", lambda path, **_: Path(path))
    monkeypatch.setattr(bootstrap, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(bootstrap, "CONFIG_ROOT", tmp_path / "config")
    return contract()


def test_failed_native_init_is_latched_and_cannot_repeat(native_machine, monkeypatch):
    calls = []

    def fail(args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(bootstrap.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        bootstrap.execute(native_machine)
    with pytest.raises(runner.RunnerRefused, match="already started"):
        bootstrap.execute(native_machine)
    assert len(calls) == 1
    args, options = calls[0]
    assert args[:3] == ["/usr/bin/nodeadm", "init", "--config-source"]
    assert len(args) == 4 and args[3].startswith("file://")
    assert options["env"]["AWS_MAX_ATTEMPTS"] == "3"
    assert options["stdout"] == options["stderr"] == subprocess.DEVNULL
    assert "AWS_PROFILE" not in options["env"] and "PYTHONPATH" not in options["env"]


def test_native_success_emits_execution_receipt_without_bootstrap_logs(
    native_machine, monkeypatch
):
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *_a, **_k: None)
    result = bootstrap.execute(native_machine)
    assert result == runner.success_receipt(native_machine)
    assert result["probe"] is None
    assert "node_config" not in result and "certificate_authority" not in result
    with pytest.raises(runner.RunnerRefused):
        bootstrap.execute(native_machine)


def test_preflight_transient_retry_does_not_reinvoke_init(native_machine, monkeypatch):
    seen, calls = [], []

    def identity(_):
        seen.append(True)
        if len(seen) < 3:
            raise urllib.error.URLError("temporary network failure")

    monkeypatch.setattr(runner, "verify_instance", identity)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        bootstrap.subprocess, "run", lambda *_a, **_k: calls.append(True)
    )
    assert bootstrap.execute(native_machine)["status"] == "succeeded"
    assert len(calls) == 1 and len(seen) == 5


def test_identity_mismatch_never_retries_or_creates_latch(native_machine, monkeypatch):
    def mismatch(_):
        raise runner.RunnerRefused("original native instance differs")

    monkeypatch.setattr(runner, "verify_instance", mismatch)
    with pytest.raises(runner.RunnerRefused):
        bootstrap.execute(native_machine)
    assert not bootstrap.STATE_ROOT.exists()


def test_concurrent_latch_consumes_one_attempt(native_machine):
    def claim():
        try:
            bootstrap.latch_init(native_machine)
            return True
        except runner.RunnerRefused:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: claim(), range(2))) == [False, True]
    raw = (
        bootstrap.STATE_ROOT / (native_machine["instance_id"] + ".started")
    ).read_text()
    assert json.loads(raw)["contract_sha256"] == runner.contract_digest(native_machine)


@pytest.mark.parametrize(
    "addresses", [["192.0.2.1"], [f"10.0.0.{n}" for n in range(1, 10)]]
)
def test_dns_refuses_foreign_or_oversized_answers_before_connect(
    monkeypatch, addresses
):
    monkeypatch.setattr(
        probe.socket,
        "getaddrinfo",
        lambda *_a, **_k: [(None, None, None, None, (a, 443)) for a in addresses],
    )
    monkeypatch.setattr(
        probe.socket,
        "create_connection",
        lambda *_a, **_k: pytest.fail("unapproved connection"),
    )
    with pytest.raises(runner.RunnerRefused):
        probe.probe(contract("node-api-dns-tls"))


def _stalled(_):
    time.sleep(60)


def test_parent_deadline_kills_stalled_child_without_success(monkeypatch):
    monkeypatch.setattr(runner, "PROBE_LIMIT", 0.05)
    start = time.monotonic()
    with pytest.raises(runner.RunnerRefused):
        runner.bounded_execution(_stalled, contract("node-api-dns-tls"))
    assert time.monotonic() - start < 5


def test_missing_success_receipt_is_failure():
    def fail(_):
        raise RuntimeError("private native detail")

    with pytest.raises(runner.RunnerRefused, match="no valid success"):
        runner.bounded_execution(fail, contract("node-api-dns-tls"))


def test_fixed_documents_pin_real_bytes_and_no_generic_shell():
    root = Path(__file__).resolve().parents[1] / "node-command"
    hashes = {
        "bootstrap": "02e65ce48a37bd18b64550493fcbeb1cdbcab7f57e667469e60779989417e7a2",
        "probe": "41756cc3ea53232d1f22bff56ab5de5e07e9e22fd3a9b8ad6ca372723e0842ef",
    }
    for name, expected in hashes.items():
        raw = (root / (name + "-document.json")).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected
        document = json.loads(raw)
        assert document["mainSteps"][0]["inputs"]["runCommand"] == [
            "exec /opt/superplane/bin/node-" + name + "-v1 '{{ Contract }}'"
        ]
        launcher = (root / ("node-" + name + "-v1")).read_text()
        assert "env -i" in launcher and " -I -B -S " in launcher


@pytest.mark.parametrize("executable", ["nodeadm", "nodeadm-internal"])
def test_running_native_binary_refuses_bootstrap(monkeypatch, tmp_path, executable):
    proc = tmp_path / "proc"
    (proc / "123").mkdir(parents=True)
    (proc / "123" / "exe").symlink_to("/usr/bin/" + executable)
    monkeypatch.setattr(bootstrap, "PROC_ROOT", proc)
    monkeypatch.setattr(bootstrap.os.path, "lexists", lambda _: False)

    def systemctl(args):
        if "list-jobs" in args:
            return ""
        return "masked" if "--property=LoadState" in args else "inactive"

    monkeypatch.setattr(runner, "fixed_command", systemctl)
    with pytest.raises(runner.RunnerRefused, match="another nodeadm"):
        bootstrap.verify_bootstrap_exclusive()


def test_queued_systemd_restart_is_not_absent(monkeypatch):
    monkeypatch.setattr(bootstrap.os.path, "lexists", lambda _: False)

    def systemctl(args):
        if "list-jobs" in args:
            return "19 containerd.service restart waiting\n"
        return "masked" if "--property=LoadState" in args else "inactive"

    monkeypatch.setattr(runner, "fixed_command", systemctl)
    with pytest.raises(runner.RunnerRefused, match="job is queued"):
        bootstrap.verify_bootstrap_exclusive()


@pytest.mark.parametrize(
    "drift", ["wrapper", "extra-module", "manifest", "wrapper-descriptor"]
)
def test_actual_installed_closure_drift_refuses_execution(monkeypatch, tmp_path, drift):
    root = tmp_path / "runtime"
    cni = tmp_path / "cni"
    root.mkdir()
    cni.mkdir()
    wrapper = root / "node_bootstrap_runner.py"
    wrapper.write_bytes(b"reviewed wrapper bytes")
    binary = tmp_path / "nodeadm"
    binary.write_bytes(b"reviewed native binary")
    (root / "node_runner.py").write_bytes(b"reviewed common module")
    (cni / "aws-cni").write_bytes(b"reviewed CNI binary")
    monkeypatch.setattr(runner, "RUNTIME_ROOT", root)
    monkeypatch.setattr(runner, "CNI_ROOT", cni)
    monkeypatch.setattr(runner, "MANIFEST_PATH", root / "manifest.json")
    monkeypatch.setattr(runner, "REQUIRED_FILES", {str(binary)})
    # Disposable CI directories are not installed root-owned paths. Preserve
    # actual content hashing and tree enumeration while replacing ownership.
    monkeypatch.setattr(runner, "secure_path", lambda p, **_: Path(p))
    monkeypatch.setattr(runner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runner.platform, "machine", lambda: "x86_64")
    current = contract()
    current["wrapper_sha256"] = runner.file_digest(wrapper)
    artifact = {
        "version": 1,
        "runtime": {
            k: v
            for k, v in current["runtime_manifest"].items()
            if k != "artifact_sha256"
        },
        "files": {str(binary): runner.file_digest(binary)},
        "trees": {
            str(root): runner.tree_digest(root),
            str(cni): runner.tree_digest(cni),
        },
    }
    raw = runner.canonical(artifact).encode()
    runner.MANIFEST_PATH.write_bytes(raw)
    current["runtime_manifest"]["artifact_sha256"] = hashlib.sha256(raw).hexdigest()
    runner.verify_installation(current, wrapper)
    if drift == "wrapper":
        wrapper.write_bytes(b"unreviewed replacement")
    elif drift == "extra-module":
        (root / "unreviewed.py").write_bytes(b"ambient code")
    elif drift == "manifest":
        runner.MANIFEST_PATH.write_bytes(raw + b"\n")
    else:
        current["wrapper_sha256"] = "f" * 64
    with pytest.raises(runner.RunnerRefused):
        runner.verify_installation(current, wrapper)


def test_installed_path_refuses_symlink(tmp_path):
    target = tmp_path / "binary"
    target.write_bytes(b"bytes")
    link = tmp_path / "link"
    link.symlink_to(target)
    # /tmp is deliberately writable; neither it nor its symlink child can
    # qualify as an installed immutable executable tree.
    with pytest.raises(runner.RunnerRefused):
        runner.secure_path(link)


def test_native_version_probes_accept_actual_prefixed_versions(monkeypatch):
    outputs = {
        "kubelet": "Kubernetes v1.34.1\n",
        "containerd": "containerd github.com/containerd/containerd/v2 v2.2.3 hash\n",
        "nvidia-container-runtime": "NVIDIA Container Runtime version 1.17.8\n",
        "amazon-ssm-agent": "SSM Agent version: 3.3.3050.0\n",
        "nvidia-smi": "580.65.06\n580.65.06\n",
    }
    monkeypatch.setattr(
        runner, "fixed_command", lambda args: outputs[Path(args[0]).name]
    )
    runner.verify_native_runtime(contract())
    outputs["containerd"] = "containerd v2.2.30\n"
    with pytest.raises(runner.RunnerRefused):
        runner.verify_native_runtime(contract())


@pytest.mark.parametrize("status", [200, 401, 403])
def test_probe_pins_single_dns_result_and_tls_hostname(monkeypatch, status):
    value = contract("node-api-dns-tls")
    resolutions, connections, verified = [], [], []

    def resolve(host, port, **kwargs):
        resolutions.append((host, port))
        return [(None, None, None, None, ("10.0.0.7", 443))]

    class Stream:
        def __init__(self):
            self.response = iter(f"HTTP/1.1 {status} result\r\n".encode())

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def sendall(self, raw):
            assert (
                raw
                == b"GET / HTTP/1.1\r\nHost: original.example.test\r\nConnection: close\r\n\r\n"
            )

        def recv(self, count):
            assert count == 1
            return bytes([next(self.response)])

    stream = Stream()

    def connect(address, **kwargs):
        connections.append(address)
        return stream

    class TLS:
        def load_verify_locations(self, *, cadata):
            assert cadata == "public-test-ca"

        def wrap_socket(self, raw, *, server_hostname):
            assert raw is stream
            verified.append(server_hostname)
            return stream

    monkeypatch.setattr(probe.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(probe.socket, "create_connection", connect)
    monkeypatch.setattr(probe.ssl, "SSLContext", lambda mode: TLS())
    receipt = probe.probe(value)
    assert resolutions == [("original.example.test", 443)]
    assert connections == [("10.0.0.7", 443)]
    assert verified == ["original.example.test"]
    assert receipt["responses"] == [{"address": "10.0.0.7", "status": status}]
    assert receipt["nonce"] == value["nonce"] and receipt["tls_verified"] is True


def test_imds_v2_uses_fixed_link_local_endpoint_and_no_proxy(monkeypatch):
    requests = []
    actual = {
        "accountId": "123456789012",
        "region": "us-east-1",
        "availabilityZone": "us-east-1a",
        "instanceId": "i-0123456789abcdef0",
        "imageId": "ami-0123456789abcdef0",
    }

    class Response:
        def __init__(self, raw):
            self.raw = raw

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, limit):
            return self.raw[:limit]

    class Opener:
        def open(self, request, *, timeout):
            requests.append(request)
            assert timeout == 3
            return Response(
                b"private-imds-token"
                if request.get_method() == "PUT"
                else json.dumps(actual).encode()
            )

    def opener(proxy, redirects):
        assert proxy.proxies == {}
        assert isinstance(redirects, runner._NoRedirect)
        return Opener()

    monkeypatch.setattr(runner.urllib.request, "build_opener", opener)
    runner.verify_instance(contract())
    assert [(r.get_method(), r.full_url) for r in requests] == [
        ("PUT", "http://169.254.169.254/latest/api/token"),
        ("GET", "http://169.254.169.254/latest/dynamic/instance-identity/document"),
    ]
    assert (
        dict((k.lower(), v) for k, v in requests[1].header_items())[
            "x-aws-ec2-metadata-token"
        ]
        == "private-imds-token"
    )
