"""The deployment parameter can select only private Kubernetes service origins."""

import json
import re
import sys
from urllib.parse import urlsplit

SERVICE_HOST = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.svc(?:\.cluster\.local)?"


def consumer_targets(raw):
    targets = json.loads(raw)
    if not isinstance(targets, dict) or set(targets) - {"product-context", "engineering-insights"}:
        raise ValueError("invalid L5 consumer destinations")
    for value in targets.values():
        if not isinstance(value, str):
            raise ValueError("invalid L5 consumer origin")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or not re.fullmatch(SERVICE_HOST, parsed.hostname or "")
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
            or not re.fullmatch(r"https?://[a-z0-9.-]+(?::[0-9]+)?/?", value)
        ):
            raise ValueError("L5 consumer origin must be a private Kubernetes service")
    return targets


if __name__ == "__main__":
    try:
        print(json.dumps(consumer_targets(sys.stdin.read()), sort_keys=True, separators=(",", ":")))
    except (ValueError, TypeError):
        raise SystemExit("Invalid private L5 consumer targets; refusing deployment") from None
