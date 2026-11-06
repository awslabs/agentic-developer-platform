"""Real AWS session shape must not bypass account, role or private-edge checks."""

import hashlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tool_adapters.atlassian_route import require_gateway
from tool_adapters.caller_identity import matches_role

ROLE = "arn:aws:iam::123456789012:role/service/adp-tools-gateway"
SESSION = "arn:aws:sts::123456789012:assumed-role/adp-tools-gateway/gateway-session-123"


@pytest.mark.parametrize("caller", [ROLE, SESSION])
def test_private_edge_accepts_only_the_configured_role(caller):
    config = SimpleNamespace(
        observation=SimpleNamespace(
            read_schemas={},
            read_schemas_sha256={},
            gateway_role_arn=ROLE,
            edge_provenance_sha256=hashlib.sha256(b"private-edge").hexdigest(),
        )
    )

    def request(headers):
        return Request({"type": "http", "headers": headers})

    role_header = (b"x-caller-identity", caller.encode())
    edge_header = (b"x-adp-edge-provenance", b"private-edge")
    require_gateway(request([role_header, edge_header]), config)
    for headers in (
        [role_header],
        [role_header, edge_header, role_header],
        [role_header, (b"x-adp-edge-provenance", b"forged")],
    ):
        with pytest.raises(HTTPException) as error:
            require_gateway(request(headers), config)
        assert error.value.status_code == 403


@pytest.mark.parametrize(
    "caller",
    [
        SESSION.replace("123456789012", "999999999999"),
        SESSION.replace("adp-tools-gateway", "adp-tools-gateway-other"),
        SESSION.replace("arn:aws:", "arn:aws-cn:"),
        SESSION.replace("assumed-role/", "user/"),
        SESSION.replace("/gateway-session-123", "/"),
        SESSION + "/extra",
        SESSION + "\n",
        " " + SESSION,
        SESSION.replace("adp-tools-gateway/", "service/adp-tools-gateway/"),
        "arn:aws:iam::123456789012:role/other/adp-tools-gateway",
        None,
        123,
    ],
)
def test_foreign_or_malformed_session_is_refused(caller):
    assert not matches_role(caller, ROLE)
