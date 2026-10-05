"""The checked manifest must match the actual production-mounted admin routes."""

import json
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from src.admin.audit_operation import AuditedAdminRoute
from tests.admin.admin_audit_inventory import inventory, mounted_admin_app


def assert_inventory(app):
    actual = inventory(app)
    expected = json.loads(Path(__file__).with_name("admin_mutation_inventory.json").read_text())
    assert all(row["classification"] != "UNCLASSIFIED" for row in actual)
    assert actual == expected


def test_production_routes_match_reviewed_manifest():
    assert_inventory(mounted_admin_app())


def test_unknown_mutation_fails_gate():
    app = mounted_admin_app()

    async def unaudited():
        return {}

    app.add_api_route("/admin/new-unreviewed-mutation", unaudited, methods=["POST"])
    with pytest.raises(AssertionError):
        assert_inventory(app)


def test_removing_durable_route_wrapper_fails_gate():
    app = mounted_admin_app()
    route = next(r for r in app.routes if isinstance(r, AuditedAdminRoute) and r.audit_mutation)
    app.routes.remove(route)
    app.routes.append(APIRoute(route.path, route.endpoint, methods=route.methods))
    with pytest.raises(AssertionError):
        assert_inventory(app)
