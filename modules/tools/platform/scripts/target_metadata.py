"""Canonical revision of verified tools-platform target metadata."""

from datetime import datetime
import hashlib
import json


def target_revision(path):
    return target_revision_values(
        [json.loads(line) for line in path.read_text().splitlines()]
    )


def target_revision_values(targets):
    """Canonical revision for independently read target metadata, shared by release/evaluation."""
    values = []
    for target in targets:
        # DYNAMIC MCP targets discover on tools/list; their last update binds
        # configuration, while the release separately binds the live listing.
        # DEFAULT MCP/OpenAPI targets bind their cached-schema synchronization.
        # Lambda targets bind their last configuration update.
        try:
            mcp = target.get("targetConfiguration", {}).get("mcp", {})
            if mcp.get("mcpServer", {}).get("listingMode") == "DYNAMIC":
                timestamp = datetime.fromisoformat(target["updatedAt"])
            elif target.get("lastSynchronizedAt"):
                timestamp = datetime.fromisoformat(target["lastSynchronizedAt"])
            elif mcp.get("lambda"):
                timestamp = datetime.fromisoformat(target["updatedAt"])
            else:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                "Target synchronization timestamp is unavailable or invalid"
            ) from None
        if timestamp.tzinfo is None:
            raise ValueError("Synchronization timestamp has no timezone")
        values.append((target["targetId"], timestamp.isoformat()))
    values.sort()
    if not values:
        raise ValueError("No synchronized targets")
    return hashlib.sha256(
        json.dumps(values, separators=(",", ":")).encode()
    ).hexdigest()
