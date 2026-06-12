"""Door — the MCP query-layer permission filter.

Every search result passes through this module before reaching the caller.
The filter enforces repo-grain ACL (mirrored from GitHub) and fails closed:
if the caller's identity cannot be resolved, they see nothing.
"""
