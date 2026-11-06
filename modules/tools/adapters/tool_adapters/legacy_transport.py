"""Pin legacy provider egress to a public address for a fixed HTTPS host."""

import ipaddress
import socket

import httpx
from fastapi import HTTPException


def provider_destination(url: str, host: str) -> httpx.URL:
    parsed = httpx.URL(url)
    if (
        parsed.scheme != "https"
        or parsed.host != host
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise HTTPException(503, detail={"error": "provider_egress_unavailable"})
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError("untrusted provider address")
        return parsed.copy_with(host=addresses[0][4][0])
    except (OSError, ValueError):
        raise HTTPException(503, detail={"error": "provider_egress_unavailable"}) from None
