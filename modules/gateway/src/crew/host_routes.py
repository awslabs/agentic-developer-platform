"""Routes a Dev Box host may call with its run identity.

``set_agent_run_id_from_header`` (src/proxy/routes.py) admits an EC2 host run only
on routes registered here; everything else answers 403 "Host run is not authorized
for this model route". Until #8223 cause 33 the allowlist was a literal inside the
gate — the model invoke and the token refresh — so the ADP tools relay added by
#8380 (``/crew/tools``) was refused on every call from the box (19-39 refusals per
chat on dev, 2026-10-21). Each host router now registers its own paths through
:func:`host_run_route` at decoration time, so a new host route cannot forget the
gate, and ``tests/proxy/test_agent_run_id.py`` enumerates the registered set.

This module is a leaf on purpose: ``src.proxy.routes`` imports it, and the crew
routers import ``src.proxy.routes`` for the gate dependency.
"""

from __future__ import annotations

HOST_RUN_ROUTE_PATHS: set[str] = set()


def host_run_route(path: str) -> str:
    """Register ``path`` as callable by a verified Dev Box host run; returns ``path``."""
    HOST_RUN_ROUTE_PATHS.add(path)
    return path
