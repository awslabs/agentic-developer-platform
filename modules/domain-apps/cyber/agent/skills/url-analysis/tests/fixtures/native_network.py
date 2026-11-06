"""Synthetic public DNS with Chromium routed only to the local fixture server."""

import socket

FIXTURE_HOST = "native-fixture.localhost"
CHROMIUM_ARGS = [f"--host-resolver-rules=MAP {FIXTURE_HOST} 127.0.0.1", "--no-proxy-server"]
resolve = socket.getaddrinfo


def fixture_dns(host, *args, **kwargs):
    if host == FIXTURE_HOST:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))]
    return resolve(host, *args, **kwargs)
