"""Shared Browser URL input validation."""

import re
from urllib.parse import parse_qsl, urlsplit


def validate_input(url: str) -> None:
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or len(url) > 8192:
        raise ValueError("An absolute HTTP(S) URL is required")
    if p.username is not None or p.password is not None:
        raise ValueError("Credential-bearing URLs are refused")
    if any(
        re.search(r"token|secret|password|api.?key|session|signature|^code$", k, re.I)
        for k, _ in parse_qsl(p.query)
    ):
        raise ValueError("Credential-bearing or single-use URLs are refused")

