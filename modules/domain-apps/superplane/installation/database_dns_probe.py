"""Credential-free RDS address discovery, sent to a DNS-only installer probe pod."""

import ipaddress
import json
import signal
import socket
import sys


def normalize_addresses(values):
    if not isinstance(values, list) or not 0 < len(values) <= 16:
        raise ValueError("Database DNS needs a bounded nonempty address list")
    normalized = set()
    for value in values:
        if not isinstance(value, str) or "%" in value:
            raise ValueError("Database DNS returned an invalid address")
        address = ipaddress.ip_address(value)
        if (
            address.is_unspecified
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
        ):
            raise ValueError("Database DNS returned an unusable address")
        normalized.add(str(address))
    return sorted(normalized)


def resolve(host, port):
    addresses = sorted(
        {row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    )
    return {"host": host, "port": port, "addresses": normalize_addresses(addresses)}


if __name__ == "__main__":
    try:
        # Also bounded by the maintained pod deadline and UID-attributed cleanup.
        signal.alarm(15)
        print(json.dumps(resolve(sys.argv[1], int(sys.argv[2]))))
        signal.alarm(0)
    except Exception:
        print(json.dumps({"status": "unavailable"}))
        raise SystemExit(1) from None
