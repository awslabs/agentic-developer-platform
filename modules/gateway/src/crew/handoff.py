"""One-time Crew link minting against an owner-bound private custody listener."""

import asyncio
import hashlib
import ipaddress
import json
import re
import ssl
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import HTTPException

from src.agentauth.chat_crew_run import current_crew_store
from src.agentauth.envelope import EnvelopeError, sign_envelope
from src.crew.host_client_identity import gateway_client_identity
from src.crew.targets import CrewOwner, ResolvedCrewTarget

# The host custody mints the dashboard link by running kirocrew's CLI, whose start-up alone
# takes ~6.4 s on an m6i.xlarge Dev Box (dev 2026-10-19, #8223). 5 s cut every mint off.
# The custody allows 20 s for the CLI; this client waits long enough to receive its answer.
CREW_MINT_TIMEOUT_SECONDS = 30


def custody_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False, timeout=CREW_MINT_TIMEOUT_SECONDS)


async def crew_host_context(target: ResolvedCrewTarget) -> ssl.SSLContext:
    expected = target.host_fingerprint
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise HTTPException(503, detail={"error": "crew_host_trust_unavailable"})
    try:
        client_identity = gateway_client_identity()
        endpoint = urlsplit(target.endpoint)
        address = ipaddress.IPv4Address(endpoint.hostname or "")
        if (
            endpoint.scheme != "https"
            or endpoint.port != 8443
            or not any(address in ipaddress.ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
        ):
            raise ValueError("invalid host endpoint")
        bootstrap = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        bootstrap.check_hostname = False
        bootstrap.verify_mode = ssl.CERT_NONE
        bootstrap.load_cert_chain(certfile=client_identity.certificate_file, keyfile=client_identity.private_key_file)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(str(address), 8443, ssl=bootstrap, server_hostname=str(address)),
            timeout=CREW_MINT_TIMEOUT_SECONDS,
        )
        try:
            peer = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
            if not peer or hashlib.sha256(peer).hexdigest() != expected:
                raise ValueError("host certificate pin mismatch")
        finally:
            writer.close()
            await writer.wait_closed()
        context = ssl.create_default_context(cadata=ssl.DER_cert_to_PEM_cert(peer))
        context.load_cert_chain(certfile=client_identity.certificate_file, keyfile=client_identity.private_key_file)
        return context
    except (OSError, ValueError, ssl.SSLError, TimeoutError):
        raise HTTPException(503, detail={"error": "crew_host_trust_unavailable"}) from None


async def crew_host_client(target: ResolvedCrewTarget) -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=await crew_host_context(target), trust_env=False, timeout=CREW_MINT_TIMEOUT_SECONDS)


async def deliver_model_token(owner: CrewOwner, target: ResolvedCrewTarget, *, now: int) -> None:
    from src.crew.model_token import mint_model_token

    if target.lease.target_kind != "ec2-instance":
        raise HTTPException(409, detail={"error": "lease_fenced"})
    run_id = await active_crew_run(owner, target, now=now)
    token = await mint_model_token(owner, target, now=now)
    endpoint = urlsplit(target.endpoint)
    if (
        endpoint.scheme != "https"
        or endpoint.port != 8443
        or endpoint.path not in ("", "/")
        or endpoint.query
        or endpoint.fragment
        or endpoint.username
        or endpoint.password
    ):
        raise HTTPException(503, detail={"error": "crew_model_delivery_unavailable"})
    try:
        async with await crew_host_client(target) as client:
            response = await client.post(
                f"https://{endpoint.hostname}:8443/model-token",
                json={
                    "run_id": run_id,
                    "lease_generation": target.lease.generation,
                    "token": token,
                    "expires_at": min(now + 60, target.lease.expires_at),
                },
            )
    except httpx.HTTPError:
        raise HTTPException(503, detail={"error": "crew_model_delivery_unavailable"}) from None
    if response.status_code != 204:
        raise HTTPException(503, detail={"error": "crew_model_delivery_unavailable"})


async def active_crew_run(owner: CrewOwner, target: ResolvedCrewTarget, *, now: int) -> str:
    store = current_crew_store()
    lease = target.lease
    host = lease.target_kind == "ec2-instance"
    if host and not all((target.host_run_id, target.host_instance_ref)):
        raise HTTPException(409, detail={"error": "lease_fenced"}, headers={"Cache-Control": "no-store"})
    marker_key = f"crew-lease#{owner.tenant_id}#{lease.target_ref}#{lease.generation}" if host else f"crew-owner#{owner.tenant_id}#{owner.user_id}"
    marker = await asyncio.to_thread(
        store.table.get_item,
        Key={"event_id": marker_key, "arrived_at": "crew"},
        ConsistentRead=True,
    )
    marker = marker.get("Item") or {}
    run_id = marker.get("run_id")
    row = await asyncio.to_thread(store.read_run, run_id) if isinstance(run_id, str) and run_id else None
    expected_scope = "session" if host else "user"
    expected_ref = target.host_instance_ref if host else lease.target_ref
    if (
        not row
        or (host and (run_id != target.host_run_id or marker.get("status") != "active"))
        or (
            not host
            and (
                marker.get("executor_scope") != "user"
                or marker.get("generation") != lease.generation
                or marker.get("target_kind") != lease.target_kind
                or marker.get("target_ref") != lease.target_ref
                or marker.get("lease_expires_at", 0) <= now
            )
        )
        or row.get("run_id") != run_id
        or row.get("status") != "active"
        or row.get("tenant_id") != owner.tenant_id
        or row.get("user_id") != owner.user_id
        or row.get("root_human_id") != owner.user_id
        or row.get("kind") != "crew"
        or row.get("executor_scope") != expected_scope
        or row.get("lease_generation") != lease.generation
        or row.get("target_kind") != lease.target_kind
        or row.get("target_ref") != expected_ref
        or (host and row.get("session_id") != lease.target_ref)
        or row.get("lease_expires_at", 0) <= now
    ):
        raise HTTPException(409, detail={"error": "lease_fenced"}, headers={"Cache-Control": "no-store"})
    return run_id


async def mint_crew_link(owner: CrewOwner, target: ResolvedCrewTarget, *, now: int) -> str:
    run_id = await active_crew_run(owner, target, now=now)
    request_id = str(uuid4())
    body = json.dumps(
        {"run_id": run_id, "lease_generation": target.lease.generation, "user_id": owner.user_id, "request_id": request_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        signature = sign_envelope(
            tenant_id=owner.tenant_id,
            principal=owner.user_id,
            target_run_id=run_id,
            target_generation=target.lease.generation,
            action="dashboard.mint",
            command_id=request_id,
            request_body=body,
            authority_kind="human_session",
            audience="adp-crew-dashboard-mint",
        )
    except EnvelopeError:
        raise HTTPException(503, detail={"error": "crew_mint_unavailable"}) from None
    try:
        endpoint = urlsplit(target.endpoint)
        address = ipaddress.ip_address(endpoint.hostname or "")
        port = endpoint.port
    except ValueError:
        raise HTTPException(503, detail={"error": "crew_mint_unavailable"}) from None
    if (
        not isinstance(address, ipaddress.IPv4Address)
        or not any(
            address in network
            for network in (
                ipaddress.IPv4Network("10.0.0.0/8"),
                ipaddress.IPv4Network("172.16.0.0/12"),
                ipaddress.IPv4Network("192.168.0.0/16"),
            )
        )
        or not (
            (target.lease.target_kind == "eks-pod" and endpoint.scheme == "http" and port == 5476)
            or (target.lease.target_kind == "ec2-instance" and endpoint.scheme == "https" and port == 8443)
        )
        or endpoint.username is not None
        or endpoint.path not in ("", "/")
        or endpoint.query
        or endpoint.fragment
    ):
        raise HTTPException(503, detail={"error": "crew_mint_unavailable"})
    host = target.lease.target_kind == "ec2-instance"
    mint_url = f"https://{address}:8443/mint" if host else f"http://{address}:5477/mint"
    try:
        async with (await crew_host_client(target)) if host else custody_client() as client:
            response = await client.post(
                mint_url,
                content=body,
                headers={"Content-Type": "application/json", "X-Adp-Crew-Signature": signature},
            )
    except httpx.HTTPError:
        raise HTTPException(503, detail={"error": "crew_mint_unavailable"}) from None
    if response.status_code == 409:
        raise HTTPException(409, detail={"error": "lease_fenced"}, headers={"Cache-Control": "no-store"})
    if response.status_code != 200:
        raise HTTPException(503, detail={"error": "crew_mint_unavailable"})
    try:
        payload = response.json()
        token = payload["link_token"]
        expires = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
        if (
            type(payload["lease_generation"]) is not int
            or payload["lease_generation"] != target.lease.generation
            or not isinstance(token, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]{16,4096}", token) is None
            or expires.tzinfo is None
            or not datetime.fromtimestamp(now, UTC) < expires <= datetime.fromtimestamp(now + 305, UTC)
        ):
            raise ValueError("invalid custody response")
    except (KeyError, TypeError, ValueError):
        raise HTTPException(503, detail={"error": "crew_mint_unavailable"}) from None
    if host:
        await deliver_model_token(owner, target, now=now)
    return token
