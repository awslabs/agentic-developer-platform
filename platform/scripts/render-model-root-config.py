#!/usr/bin/env python3
"""Validate deployment model bindings and emit a safe sed/YAML string value."""
import json
import re
import sys


def render(raw: str, kind: str) -> str:
    if len(raw) > 65536:
        raise ValueError('model authority configuration too large')
    if kind == 'bindings':
        value = json.loads(raw)
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise ValueError('model bindings must be a JSON list of objects')
        text = json.dumps(value, sort_keys=True, separators=(',', ':'))
    elif kind == 'images':
        if raw and any(not re.fullmatch(r'sha256:[0-9a-f]{64}', item) for item in raw.split(',')):
            raise ValueError('chat image allowlist must contain pinned digests')
        text = raw
    else:
        raise ValueError('unknown model configuration kind')
    # ConfigMap quotes the replacement. Encode for YAML (JSON strings are a
    # YAML subset), then quote sed replacement metacharacters independently.
    return json.dumps(text)[1:-1].replace('\\', '\\\\').replace('&', '\\&').replace('|', '\\|')


if __name__ == '__main__':
    try:
        print(render(sys.stdin.read(), sys.argv[1]))
    except (ValueError, IndexError):
        sys.exit('Invalid model authority deployment configuration')
