"""Bounded packet probe executed on the selected node/pod by the acceptance driver.

This does not provision infrastructure. Invoke through the approved node transport
or an approved ordinary Kubernetes probe workload, never on the management host
as a substitute for remote execution. Results are validated against Kubernetes and
provider identity by network_observation; stdout contains no credentials/bodies.
"""

import argparse
import ipaddress
import json
import socket
import ssl
import urllib.parse


def probe(url, *, allowed_cidrs, ca_file=None):
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("explicit credential-free probe endpoint required")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = sorted(
        {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname, port, type=socket.SOCK_STREAM
            )
        }
    )
    ranges = [ipaddress.ip_network(c, strict=True) for c in allowed_cidrs]
    if not addresses or any(
        not any(ipaddress.ip_address(a) in r for r in ranges) for a in addresses
    ):
        raise ValueError("probe DNS resolved outside approved private ranges")
    # Connect to the address just validated; no second DNS lookup can retarget it.
    results = []
    for address in addresses:
        with socket.create_connection((address, port), timeout=5) as raw:
            stream = raw
            if parsed.scheme == "https":
                context = ssl.create_default_context(cafile=ca_file)
                stream = context.wrap_socket(raw, server_hostname=parsed.hostname)
            try:
                path = parsed.path or "/"
                stream.sendall(
                    f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}\r\nConnection: close\r\n\r\n".encode(
                        "ascii"
                    )
                )
                line = b""
                while b"\n" not in line and len(line) < 128:
                    chunk = stream.recv(1)
                    if not chunk:
                        break
                    line += chunk
                parts = line.split()
                if len(parts) < 2 or not parts[0].startswith(b"HTTP/"):
                    raise ValueError("probe did not receive an HTTP response")
                status = int(parts[1])
                # EKS may correctly refuse an anonymous request while proving TLS
                # and network reachability. Ordinary Service probes require 2xx.
                allowed = 200 <= status < 300 or (
                    parsed.scheme == "https" and status in {401, 403}
                )
                if not allowed:
                    raise ValueError("probe endpoint did not respond successfully")
                results.append({"address": address, "status": status})
            finally:
                if stream is not raw:
                    stream.close()
    return {
        "url": url,
        "addresses": addresses,
        "responses": results,
        "tls_verified": parsed.scheme == "https",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--cidr", action="append", required=True)
    parser.add_argument("--ca-file")
    parser.add_argument("--source", choices=["node", "pod"], required=True)
    args = parser.parse_args()
    try:
        result = probe(args.endpoint, allowed_cidrs=args.cidr, ca_file=args.ca_file)
        print(
            json.dumps(
                {"version": 1, "nonce": args.nonce, "source": args.source, **result},
                sort_keys=True,
            )
        )
    except (OSError, ValueError, UnicodeError):
        raise SystemExit(
            "network packet probe failed; no success evidence emitted"
        ) from None


if __name__ == "__main__":
    main()
