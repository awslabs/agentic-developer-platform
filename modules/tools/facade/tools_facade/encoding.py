"""Canonical JSON for tools assertions and durable operation input hashes."""

import json


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
