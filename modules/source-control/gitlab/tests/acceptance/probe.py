#!/usr/bin/env python3
"""Run bounded network probes inside an owned qualification job, without AWS credentials."""
import argparse
import errno
import http.client
import ipaddress
import json
import socket
import ssl
import time
from pathlib import Path


def expected_result(target, result):
    if target.get("expected") == "disabled_by_provider":
        return (target.get("name") == "metadata_ipv6"
                and (target.get("address"), target.get("port"), target.get("kind")) == ("fd00:ec2::254", 80, "tcp")
                and result.get("connection_attempted") is True
                and result.get("transport_connected") is False
                and ((result.get("result") == "blocked" and result.get("error_kind") == "timeout")
                     or (result.get("result") == "unreachable" and result.get("errno") in {
                         errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EAFNOSUPPORT,
                         errno.EADDRNOTAVAIL, errno.ENETDOWN})))
    return result.get("result") == target.get("expected")


def probe(target, *, ca_file=None):
    """Use fixed operator-resolved addresses; DNS failure is not isolation evidence."""
    address = str(ipaddress.ip_address(target["address"]))
    port = target["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid probe port")
    kind = target["kind"]
    if kind not in {"tcp", "https", "dns"}:
        raise ValueError("invalid probe protocol")
    started = time.monotonic()
    reachable = False
    transport_connected = False
    attempting_connection = False
    result = "probe_error"
    failure = {}
    try:
        if kind == "dns":
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            with socket.socket(family, socket.SOCK_DGRAM) as client:
                client.settimeout(3)
                # Query a fixed example.test name. A matching reply proves an
                # alternate resolver can be reached even if it returns NXDOMAIN.
                query = bytes.fromhex("415001000001000000000000") + b"\x07example\x04test\x00\x00\x01\x00\x01"
                attempting_connection = True
                client.connect((address, port))
                client.send(query)
                reply = client.recv(512)
                # Any response from the connected target proves UDP reachability;
                # a malformed reply must not be reported as a blocked network.
                reachable = reply is not None
                transport_connected = reachable
        elif kind == "https":
            host = target["hostname"]
            context = ssl.create_default_context()
            if ca_file:
                context.load_verify_locations(cafile=ca_file)
            attempting_connection = True
            with socket.create_connection((address, port), timeout=3) as raw:
                transport_connected = True
                with context.wrap_socket(raw, server_hostname=host) as secure:
                    path = target.get("path", "/")
                    if not path.startswith("/") or any(c in host + path for c in "\r\n"):
                        raise ValueError("invalid HTTPS probe target")
                    secure.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode("ascii"))
                    response = http.client.HTTPResponse(secure)
                    response.begin()
                    reachable = response.status == target["expected_status"]
                    result = "allowed" if reachable else "unexpected_http_status"
        else:
            attempting_connection = True
            with socket.create_connection((address, port), timeout=3):
                transport_connected = True
                reachable = True
        if kind != "https":
            result = "allowed" if reachable else "blocked"
    except (TimeoutError, socket.timeout) as error:
        failure = {"error_kind": "timeout", "errno": error.errno}
        result = ("reachable_protocol_error" if transport_connected else
                  "blocked" if attempting_connection else "probe_error")
    except (OSError, ssl.SSLError, http.client.HTTPException) as error:
        failure = {"error_kind": type(error).__name__, "errno": getattr(error, "errno", None)}
        # A TLS/protocol failure after TCP connected proves network reachability.
        # Refusal, missing routes and local CA-file failures do not establish a
        # firewall denial either. Only a timed-out network attempt may be blocked.
        result = ("reachable_protocol_error" if transport_connected else
                  "unreachable" if attempting_connection else "probe_error")
    return {"name": target["name"], "result": result,
            "transport_connected": transport_connected,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            **({"connection_attempted": attempting_connection, **failure}
               if target["name"] == "metadata_ipv6" else {})}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--ca-file")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixtures = json.loads(args.fixtures.read_text())
    targets = fixtures["targets"]
    if not isinstance(targets, list) or not 1 <= len(targets) <= 32:
        raise ValueError("a bounded reviewed target set is required")
    results = [probe(target, ca_file=args.ca_file) for target in targets]
    args.output.write_text(json.dumps({"run_id": fixtures["run_id"], "probes": results}) + "\n")
    # A trusted verifier also checks independent API/host observations. Job
    # output alone never establishes tenant assignment or completed cleanup.
    return int(any(not expected_result(target, row) for row, target in zip(results, targets)))


if __name__ == "__main__":
    raise SystemExit(main())
