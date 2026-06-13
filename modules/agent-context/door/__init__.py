"""Door — the MCP query-layer permission filter and Context MCP Server.

Every search result passes through this module before reaching the caller.
The filter enforces repo-grain ACL (mirrored from GitHub) and fails closed:
if the caller's identity cannot be resolved, they see nothing.

The server (door.server) exposes the 6-tool MCP verb surface at :5100,
wiring existing backends through the ACL filter.
"""
