"""One bounded DNS/TLS attempt from the original native node, without credentials."""

import base64
import ipaddress
import socket
import ssl
import sys
from urllib.parse import urlsplit

if __package__ in {None, ""}:
    sys.path.insert(0, "/opt/superplane/node-runtime")
    import node_runner as runner
else:
    from . import node_runner as runner


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
    runner.verify_instance(contract)
    return runner.success_receipt(contract, observed)


def main(argv=None):
    return runner.entrypoint(
        "node-api-dns-tls", execute, sys.argv[1:] if argv is None else argv
    )


if __name__ == "__main__":
    raise SystemExit(main())
