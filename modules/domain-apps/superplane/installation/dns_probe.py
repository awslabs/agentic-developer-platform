"""Standard-library DNS readiness probe, executed inside a restricted image pod."""

import ipaddress
import json
import secrets
import socket
import struct
import sys
from pathlib import Path


def query(address, name, protocol):
    ident = secrets.randbelow(65536)
    labels = name.rstrip(".").split(".")
    question = b"".join(bytes([len(label)]) + label.encode("ascii") for label in labels)
    question += b"\0" + struct.pack("!HH", 1, 1)
    packet = struct.pack("!HHHHHH", ident, 0x100, 1, 0, 0, 0) + question
    family = (
        socket.AF_INET6
        if ipaddress.ip_address(address).version == 6
        else socket.AF_INET
    )
    kind = socket.SOCK_DGRAM if protocol == "UDP" else socket.SOCK_STREAM
    with socket.socket(family, kind) as connection:
        connection.settimeout(3)
        connection.connect((address, 53))
        if protocol == "UDP":
            connection.send(packet)
            response = connection.recv(65535)
        else:
            connection.sendall(struct.pack("!H", len(packet)) + packet)

            def receive(size):
                data = b""
                while len(data) < size:
                    part = connection.recv(size - len(data))
                    if not part:
                        raise ValueError("Incomplete DNS response")
                    data += part
                return data

            response = receive(struct.unpack("!H", receive(2))[0])
    response_id, flags, questions, answers, _, _ = struct.unpack(
        "!HHHHHH", response[:12]
    )
    if not (
        response_id == ident
        and flags & 0x8000
        and not flags & 0x020F
        and questions == 1
        and answers > 0
        and response[12 : 12 + len(question)] == question
    ):
        raise ValueError("DNS did not return a complete successful answer")


def verify(expected, names):
    resolvers = [
        line.split()[1]
        for line in Path("/etc/resolv.conf").read_text().splitlines()
        if line.split() and line.split()[0] == "nameserver"
    ]
    if not resolvers or (expected and resolvers != [expected]):
        raise ValueError("Pod resolver differs from the selected platform resolver")
    for resolver in resolvers:
        for name in names:
            for protocol in ("UDP", "TCP"):
                query(resolver, name, protocol)
    return {
        "verified": True,
        "resolvers": resolvers,
        "names": names,
        "protocols": ["UDP", "TCP"],
    }


if __name__ == "__main__":
    try:
        print(json.dumps(verify(sys.argv[1], sys.argv[2:])))
    except Exception:
        print(json.dumps({"verified": False}))
        raise SystemExit(1)
