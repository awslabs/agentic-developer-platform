"""Client address asserted by our ingress, never by a viewer's forwarded header."""

import hmac
from ipaddress import ip_address

from fastapi import HTTPException, Request

from src.shared.config import get_settings


def login_source(request: Request) -> str:
    secret = get_settings().apigw_provenance_secret
    proofs = request.headers.getlist("x-adp-client-provenance")
    proof = proofs[0] if len(proofs) == 1 else ""
    if secret and proof and hmac.compare_digest(proof.encode(), secret.encode()):
        try:
            addresses = request.headers.getlist("x-adp-client-ip")
            if len(addresses) != 1:
                raise ValueError("Ambiguous client address")
            return str(ip_address(addresses[0]))
        except ValueError:
            raise HTTPException(400, "Invalid ingress client address") from None
    # Direct/local deployments retain transport-based quotas. Never accept XFF,
    # nor group missing transport metadata into a shared internet-wide bucket.
    if request.client and request.client.host:
        return request.client.host
    raise HTTPException(503, "Client address unavailable")
