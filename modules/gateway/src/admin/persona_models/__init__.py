"""Persona-model preference management — Issue #5419 (PMM-02).

Two routers:

- ``self_routes.router`` — ``/me/persona-models``, both human and service callers.
- ``routes.router`` — ``/service-principals/{id}/persona-models``, human org-admin only.
"""
