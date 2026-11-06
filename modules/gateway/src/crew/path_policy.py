"""Canonical dashboard paths before either HTTP or WebSocket forwarding."""

import posixpath
from urllib.parse import unquote

from fastapi import HTTPException

_DENIED_PREFIXES = ("/api/token", "/api/instances", "/api/auth/mobile-link", "/crew")


def require_dashboard_path(scope: dict) -> None:
    try:
        raw = scope.get("raw_path", scope["path"].encode("utf-8"))
        if not isinstance(raw, bytes) or len(raw) > 4096:
            raise ValueError("invalid path")
        path = raw.decode("utf-8")
        if not path.startswith("/"):
            raise ValueError("invalid path")
        for _attempt in range(8):
            decoded = unquote(path, errors="strict")
            if decoded == path:
                break
            path = decoded
        if any(delimiter in path for delimiter in ("%", ";", "\\", "?", "#")) or any(ord(character) < 32 for character in path):
            raise ValueError("ambiguous path")
        canonical = ("/" + posixpath.normpath(path).lstrip("/")).casefold()
        if any(canonical == prefix or canonical.startswith(prefix + "/") for prefix in _DENIED_PREFIXES):
            raise ValueError("privileged path")
    except (UnicodeError, ValueError):
        raise HTTPException(403, detail={"error": "crew_path_denied"}, headers={"Cache-Control": "no-store"}) from None
