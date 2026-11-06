"""Dev Box Crew tools under the host's signed model identity (#8380).

Kiro Crew on an EC2 Dev Box reaches models through the loopback adapter, which signs
each call with the instance role and carries the gateway-minted model token. This
module gives the same adapter a tools path: the host posts the shim's request to
``POST /crew/tools`` (API Gateway ``/agent/crew/tools``), the gateway proves the caller
is the current host of the owner's live run exactly as the model route does, mints a
short-lived **host assertion** bound to tenant, owner, run, box, instance and
generation, and relays to the tools façade with that assertion as the only identity.
The façade forwards it to ``/tools/authorize`` where :func:`authenticate_crew_host`
re-resolves the owner's live target before the ordinary policy, consent, entitlement,
budget and receipt logic runs under the owner. Nothing here needs ``adp login`` on
the box; access ends with the lease.
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from src.agentauth.chat_tools import READ_PATH, WRITE_PATH, facade_endpoint
from src.crew import model_token
from src.crew.host_routes import host_run_route
from src.crew.targets import CrewLeaseFencedError, CrewOwner, CrewTargetMissingError, ResolvedCrewTarget
from src.proxy.routes import get_token_context, set_agent_run_id_from_header
from src.shared.schemas.auth import TokenContext

router = APIRouter(tags=["crew"])

ASSERTION_HEADER = "x-adp-crew-host-assertion"
ASSERTION_LIFETIME_SECONDS = 30
_ASSERTION_PREFIX = b"adp-crew-host-tools-v1:"
_ASSERTION_CLAIMS = {"v", "tenant", "owner", "run", "box", "instance", "generation", "nbf", "exp"}
RELAY_TIMEOUT_SECONDS = 25


class HostToolsRequest(BaseModel):
    """The shim's request as the host adapter forwards it; run identity comes from headers."""

    model_config = ConfigDict(extra="forbid")
    method: Literal["GET", "POST"]
    path: str = Field(min_length=1, max_length=2048)
    body: dict | None = None


@dataclass(frozen=True)
class CrewHostToolsIdentity:
    """The authorize plane's view of a Dev Box host caller; equality is the re-check."""

    tenant_id: str
    user_id: str
    run_id: str
    instance_id: str
    box_id: str
    generation: int
    expires_at: int


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _denied(status: int = 403) -> HTTPException:
    return HTTPException(status, detail={"error": "crew_model_credential_denied"})


async def current_host_target(context: TokenContext, run_id: str | None, *, now: int) -> tuple[CrewOwner, ResolvedCrewTarget]:
    """The owner and live target of the host calling with its instance identity, or a refusal.

    Same proof as ``refresh_crew_model_token``: an IAM service caller with a Dev Box instance
    identity, a run row of kind ``ec2-instance``, and an owner target whose current host run
    and instance are exactly the caller's.
    """
    if context.auth_source != "iam" or context.account_type != "service" or not context._dev_box_instance_id or not run_id:
        raise _denied()
    from src.agentauth.chat_crew_run import current_crew_store

    row = await asyncio.to_thread(current_crew_store().read_run, run_id)
    if row is None or row.get("target_kind") != "ec2-instance":
        raise _denied()
    owner = CrewOwner(row["tenant_id"], row["user_id"])
    from src.crew.proxy import get_target_resolver

    try:
        target = await get_target_resolver().resolve(owner, now=now)
    except (CrewTargetMissingError, CrewLeaseFencedError):
        raise HTTPException(409, detail={"error": "lease_fenced"}) from None
    if target.host_run_id != run_id or target.host_instance_ref != context._dev_box_instance_id:
        raise _denied()
    # #9024: the human behind this host call, for the cost record's conversation list.
    context._crew_owner_user_id = owner.user_id
    return owner, target


def _assertion_claims(owner: CrewOwner, target: ResolvedCrewTarget, run_id: str) -> dict:
    return {
        "v": 1,
        "tenant": owner.tenant_id,
        "owner": owner.user_id,
        "run": run_id,
        "box": target.lease.target_ref,
        "instance": target.host_instance_ref,
        "generation": target.lease.generation,
    }


def mint_host_tools_assertion(owner: CrewOwner, target: ResolvedCrewTarget, run_id: str, *, now: int) -> str:
    """A 30-second HMAC assertion of the verified host caller for the façade hop."""
    if (
        target.lease.target_kind != "ec2-instance"
        or target.lease.tenant_id != owner.tenant_id
        or target.lease.user_id != owner.user_id
        or not target.host_instance_ref
        or target.host_run_id != run_id
        or target.lease.expires_at <= now
    ):
        raise HTTPException(409, detail={"error": "lease_fenced"})
    payload = json.dumps(
        {**_assertion_claims(owner, target, run_id), "nbf": now, "exp": min(now + ASSERTION_LIFETIME_SECONDS, target.lease.expires_at)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    signature = hmac.digest(model_token._secret(), _ASSERTION_PREFIX + payload, hashlib.sha256)
    return f"{_encode(payload)}.{_encode(signature)}"


def verify_host_tools_assertion(assertion: str, *, now: int) -> dict:
    """Structural and signature checks only; the live-target check is :func:`authenticate_crew_host`."""
    if not isinstance(assertion, str) or not assertion or len(assertion) > 2048:
        raise HTTPException(403, "tool authority refused")
    try:
        encoded, signed = assertion.split(".")
        payload, signature = _decode(encoded), _decode(signed)
        if not hmac.compare_digest(signature, hmac.digest(model_token._secret(), _ASSERTION_PREFIX + payload, hashlib.sha256)):
            raise ValueError("invalid signature")
        claims = json.loads(payload)
        if (
            not isinstance(claims, dict)
            or set(claims) != _ASSERTION_CLAIMS
            or claims["v"] != 1
            or type(claims["nbf"]) is not int
            or type(claims["exp"]) is not int
            or claims["nbf"] > now
            or claims["exp"] <= now
            or claims["exp"] - claims["nbf"] > ASSERTION_LIFETIME_SECONDS
            or type(claims["generation"]) is not int
            or any(not isinstance(claims[key], str) or not claims[key] for key in ("tenant", "owner", "run", "box", "instance"))
        ):
            raise ValueError("invalid claims")
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
        raise HTTPException(403, "tool authority refused") from None
    return claims


async def authenticate_crew_host(request: Request) -> CrewHostToolsIdentity:
    """Identity for ``/tools/authorize`` when the façade forwards a host assertion.

    The assertion alone is not enough: the owner's target must still resolve to the same
    run, instance and generation right now, so a stopped or replaced box loses tools with
    its lease even inside the assertion's 30 seconds.
    """
    assertions = request.headers.getlist(ASSERTION_HEADER)
    run_ids = request.headers.getlist("x-agent-runid")
    if len(assertions) != 1 or len(run_ids) != 1 or request.headers.get("x-adp-run-credential") or request.headers.get("x-adp-chat-capability"):
        raise HTTPException(403, "ambiguous tool identity")
    now = int(time.time())
    claims = verify_host_tools_assertion(assertions[0], now=now)
    if claims["run"] != run_ids[0]:
        raise HTTPException(403, "tool authority refused")
    owner = CrewOwner(claims["tenant"], claims["owner"])
    from src.crew.proxy import get_target_resolver

    try:
        target = await get_target_resolver().resolve(owner, now=now)
    except (CrewTargetMissingError, CrewLeaseFencedError):
        raise HTTPException(404, "unknown_run") from None
    if (
        target.lease.target_kind != "ec2-instance"
        or target.lease.expires_at <= now
        or any(claims[key] != value for key, value in _assertion_claims(owner, target, claims["run"]).items())
    ):
        raise HTTPException(404, "unknown_run")
    return CrewHostToolsIdentity(
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        run_id=claims["run"],
        instance_id=claims["instance"],
        box_id=claims["box"],
        generation=claims["generation"],
        expires_at=target.lease.expires_at,
    )


def _validate_tools_path(method: str, path: str, body: dict | None) -> str:
    parsed = urlsplit(path)
    pattern = READ_PATH if method == "GET" else WRITE_PATH
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or not pattern.fullmatch(parsed.path)
        or "%" in parsed.path
        or (method == "GET" and body is not None)
    ):
        raise HTTPException(400, "invalid tools route")
    return path


@router.post(host_run_route("/crew/tools"))
async def host_tools_request(
    body: HostToolsRequest,
    request: Request,
    context: Annotated[TokenContext, Depends(get_token_context)],
    run_id: Annotated[str | None, Depends(set_agent_run_id_from_header)],
    token: str | None = Header(default=None, alias="X-Adp-Crew-Model-Token"),
):
    now = int(time.time())
    owner, target = await current_host_target(context, run_id, now=now)
    claims = await model_token.verify_model_token(token or "", target, now=now)
    if claims["run"] != run_id:
        raise _denied()
    _validate_tools_path(body.method, body.path, body.body)
    listing_key = (owner.tenant_id, owner.user_id, run_id) if body.method == "GET" and body.path == "/tools" else None
    if listing_key is not None:
        cached = _cached_listing(listing_key, now)
        if cached is not None:
            return JSONResponse({"status": 200, "body": cached}, headers={"Cache-Control": "no-store"})
    endpoint = facade_endpoint()
    assertion = mint_host_tools_assertion(owner, target, run_id, now=now)
    headers = {ASSERTION_HEADER: assertion, "x-agent-runid": run_id}
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=RELAY_TIMEOUT_SECONDS) as client:
            response = await client.request(
                body.method,
                endpoint + body.path.removeprefix("/tools"),
                headers=headers,
                **({"json": body.body} if body.method == "POST" else {}),
            )
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"status": 502, "body": {"error": "tools response unavailable; recover before retrying"}})
    if listing_key is not None and response.status_code == 200 and isinstance(payload, dict):
        _remember_listing(listing_key, payload, now)
    return JSONResponse({"status": response.status_code, "body": payload}, headers={"Cache-Control": "no-store"})


# The tools listing is what Claude Code asks for every time Kiro Crew spawns it for a
# turn, and on dev it cost 3.8–12 s per listing through the façade's authority round
# trip (#8223 Dev Box latency). A listing is a function of the owner's effective policy
# and changes rarely, so the relay remembers the last successful one per (tenant,
# owner, run) for LISTING_CACHE_TTL_SECONDS. Only the exact catalogue read is cached:
# tool calls, operation reads and artifacts always reach the façade, and a different
# run (new generation or re-bound host) never shares an entry.
LISTING_CACHE_TTL_SECONDS = 60
LISTING_CACHE_MAX_ENTRIES = 1024
_LISTING_CACHE: dict[tuple[str, str, str], tuple[int, dict]] = {}


def _cached_listing(key: tuple[str, str, str], now: int) -> dict | None:
    entry = _LISTING_CACHE.get(key)
    if entry is None:
        return None
    expires_at, payload = entry
    if expires_at <= now:
        _LISTING_CACHE.pop(key, None)
        return None
    return payload


def _remember_listing(key: tuple[str, str, str], payload: dict, now: int) -> None:
    if len(_LISTING_CACHE) >= LISTING_CACHE_MAX_ENTRIES:
        for stale in [k for k, (expires_at, _) in _LISTING_CACHE.items() if expires_at <= now]:
            _LISTING_CACHE.pop(stale, None)
        if len(_LISTING_CACHE) >= LISTING_CACHE_MAX_ENTRIES:
            _LISTING_CACHE.pop(next(iter(_LISTING_CACHE)), None)
    _LISTING_CACHE[key] = (now + LISTING_CACHE_TTL_SECONDS, payload)
