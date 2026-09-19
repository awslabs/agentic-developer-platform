#!/usr/bin/env python3
"""Validate and canonicalize the SSM-backed gateway model allowlist policy."""

from __future__ import annotations

import json
import sys


SSM_UNAVAILABLE = "__ADP_SSM_UNAVAILABLE__"


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or raw == SSM_UNAVAILABLE or raw == "None":
        print("model allowlist SSM parameter is unavailable", file=sys.stderr)
        return 2
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"model allowlist SSM parameter is not valid JSON: {exc.msg}",
            file=sys.stderr,
        )
        return 2
    if not isinstance(value, dict) or not all(
        isinstance(scope, str)
        and bool(scope)
        and isinstance(patterns, list)
        and all(isinstance(pattern, str) and bool(pattern) for pattern in patterns)
        for scope, patterns in value.items()
    ):
        print(
            "model allowlist must be an object of non-empty scope keys to string lists",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
