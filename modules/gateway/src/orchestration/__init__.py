"""Delivery-loop orchestration engine.

Exposes the node-state vocabulary and transition rules (`state`), the graph-store
tables (`models`), and tenant-scoped data access over them (`repository`).

Deliberately exports **no** FastAPI router. Promotion state is reachable only
from the operator plane; `tests/orchestration/test_internal_plane_guard.py`
fails the build if any internal-plane route ever reads or writes these tables.

Submodules are not imported here: `state` is pure logic with no dependencies, and
importing `models` eagerly would pull SQLAlchemy into every consumer of the
vocabulary. Import what you need directly.
"""
