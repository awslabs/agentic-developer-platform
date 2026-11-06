"""The gateway mounts the versioned owner API rather than a helper-only route."""

from src.app import create_app


def test_gateway_mounts_owner_api_and_polling():
    paths = {route.path for route in create_app().routes}
    assert {
        "/cloud-desktops",
        "/cloud-desktops/{desktop_id}",
        "/cloud-desktops/{desktop_id}/operations/{operation_id}",
        "/cloud-desktops/{desktop_id}/resources",
    } <= paths
