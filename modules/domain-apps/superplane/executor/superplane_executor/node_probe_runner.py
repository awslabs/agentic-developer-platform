"""One bounded DNS/TLS attempt from the original native node, without credentials."""

import base64
import ipaddress
from pathlib import Path
import re
import socket
import ssl
import stat
import sys
from urllib.parse import urlsplit

if __package__ in {None, ""}:
    sys.path.insert(0, "/opt/superplane/node-runtime")
    import node_runner as runner
else:
    from . import node_runner as runner

CRI_SOCKET = Path("/run/containerd/containerd.sock")
CRI_ENDPOINT = "unix:///run/containerd/containerd.sock"


def verify_cri_socket():
    runner.secure_path(CRI_SOCKET.parent, directory=True)
    info = CRI_SOCKET.lstat()
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or info.st_mode & 0o007
    ):
        raise runner.RunnerRefused("trusted native containerd socket required")


def cri_json(*arguments):
    verify_cri_socket()
    raw = runner.bounded_command(
        [
            "/usr/bin/crictl",
            "--config=/dev/null",
            "--runtime-endpoint=" + CRI_ENDPOINT,
            "--image-endpoint=" + CRI_ENDPOINT,
            "--timeout=5",
            *arguments,
        ]
    )
    if len(raw) > 131072:
        raise runner.RunnerRefused("local runtime observation exceeds bound")
    return runner.decode_json(raw)


def verify_device_plugin(contract):
    """Resolve CRI image IDs to immutable repository digests on this node."""
    value = cri_json(
        "ps",
        "--state=Running",
        "--label=io.kubernetes.pod.namespace=kube-system",
        "--output=json",
    )
    if not isinstance(value, dict) or set(value) != {"containers"}:
        raise runner.RunnerRefused("local container inventory unavailable")
    containers = value["containers"]
    if not isinstance(containers, list) or not 1 <= len(containers) <= 64:
        raise runner.RunnerRefused("bounded local container inventory required")
    expected = contract["runtime_manifest"]["device_plugin_image"]
    seen = set()
    for container in containers:
        if not isinstance(container, dict):
            raise runner.RunnerRefused("local container identity unavailable")
        if (
            container.get("state") != "CONTAINER_RUNNING"
            or not isinstance(container.get("labels"), dict)
            or container["labels"].get("io.kubernetes.pod.namespace") != "kube-system"
        ):
            continue
        identity, image_ref = container.get("id"), container.get("imageRef")
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"[a-f0-9]{64}", identity)
            or not isinstance(image_ref, str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_ref)
        ):
            raise runner.RunnerRefused("native CRI image ID unavailable")
        if image_ref in seen:
            continue
        seen.add(image_ref)
        if len(seen) > 16:
            raise runner.RunnerRefused("local image inventory exceeds bound")
        image = cri_json("inspecti", "--output=json", image_ref)
        status = image.get("status") if isinstance(image, dict) else None
        if not isinstance(status, dict) or status.get("id") != image_ref:
            raise runner.RunnerRefused("local image resolution differs")
        digests = status.get("repoDigests")
        if (
            not isinstance(digests, list)
            or not 1 <= len(digests) <= 32
            or any(
                not isinstance(d, str)
                or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,512}@sha256:[a-f0-9]{64}", d)
                for d in digests
            )
        ):
            raise runner.RunnerRefused("immutable local image digest unavailable")
        if any(d.rsplit("@", 1)[1] == expected for d in digests):
            # Recheck the same container, not a label-selected replacement.
            current = cri_json("inspect", "--output=json", identity)
            current = current.get("status") if isinstance(current, dict) else None
            if (
                not isinstance(current, dict)
                or current.get("id") != identity
                or current.get("state") != "CONTAINER_RUNNING"
                or current.get("imageRef") != image_ref
                or not isinstance(current.get("labels"), dict)
                or current["labels"].get("io.kubernetes.pod.namespace") != "kube-system"
            ):
                raise runner.RunnerRefused("running device plugin changed")
            return
    raise runner.RunnerRefused("approved device plugin is not running locally")


def probe(contract):
    endpoint = urlsplit(contract["endpoint"])
    ranges = [ipaddress.ip_network(v, strict=True) for v in contract["cidrs"]]
    addresses = sorted(
        {
            item[4][0]
            for item in socket.getaddrinfo(
                endpoint.hostname, 443, type=socket.SOCK_STREAM
            )
        }
    )
    if not 1 <= len(addresses) <= 8 or any(
        not any(ipaddress.ip_address(address) in network for network in ranges)
        for address in addresses
    ):
        raise runner.RunnerRefused("DNS answer differs from approved private network")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.load_verify_locations(
        cadata=base64.b64decode(contract["certificate_authority"]).decode("ascii")
    )
    responses = []
    for address in addresses:
        # Exact numeric address from the single resolution; no second hostname
        # lookup, redirect, bearer credentials, proxy, or ambient CA store.
        with socket.create_connection((address, 443), timeout=5) as raw:
            with tls.wrap_socket(raw, server_hostname=endpoint.hostname) as stream:
                stream.sendall(
                    f"GET / HTTP/1.1\r\nHost: {endpoint.hostname}\r\nConnection: close\r\n\r\n".encode(
                        "ascii"
                    )
                )
                line = b""
                while b"\n" not in line and len(line) < 128:
                    part = stream.recv(1)
                    if not part:
                        break
                    line += part
                fields = line.decode("ascii").strip().split(" ")
                if len(fields) < 2 or fields[0] not in {"HTTP/1.0", "HTTP/1.1"}:
                    raise runner.RunnerRefused("API HTTP status unavailable")
                status = int(fields[1])
                if not (200 <= status < 300 or status in {401, 403}):
                    raise runner.RunnerRefused("API transport refused")
                responses.append({"address": address, "status": status})
    return {
        "version": 1,
        "nonce": contract["nonce"],
        "source": "node",
        "url": contract["endpoint"],
        "addresses": addresses,
        "responses": responses,
        "tls_verified": True,
    }


def execute(contract):
    runner.verify_installation(contract, __file__)
    runner.verify_native_runtime(contract)
    runner.verify_instance(contract)
    observed = probe(contract)
    verify_device_plugin(contract)
    runner.verify_instance(contract)
    return runner.success_receipt(contract, observed)


def main(argv=None):
    return runner.entrypoint(
        "node-api-dns-tls", execute, sys.argv[1:] if argv is None else argv
    )


if __name__ == "__main__":
    raise SystemExit(main())
