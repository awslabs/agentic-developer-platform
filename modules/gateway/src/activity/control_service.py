"""One authorization and transport path for live run controls (#3960).

Two callers reach live controls: the Agent Activity routes a dashboard user hits,
and the per-run adapters on the existing orchestration seam. Both delegate here.
That is the whole point of the module — a second implementation of "may this
caller control this run?" would be two authorization checks that agree on the day
they are written, and the run-status sets under ``activity/`` already demonstrated
where that ends (three definitions of the same thing, free to disagree about the
same row). One resolver, one policy, one status mapping.

**What authorization means here.** A caller must present an authenticated session
(the route's dependency does that), belong to the run's tenant, *and* own the run
as a human — either as its ``user_id`` or as its recorded ``root_human_id`` for a
chain-attributed run. Tenant alone is not enough: a colleague in the same
organization aborting someone else's run is the cross-tenant attack with a
shorter blast radius, not an acceptable one. Nothing is read off a request
header; attribution headers exist for billing and are caller-influenced
(``TokenContext.attributed_org_id``), so reading one for access would hand the
caller their own permission check.

**Why 404 for three different failures.** Unknown run, another tenant's run, and
a same-tenant run the caller does not own all return an identical 404. A 403
would confirm the run exists, which turns the ID space into an enumeration
oracle — a caller who can tell "not yours" from "no such thing" can map another
tenant's activity by probing IDs. The flag-off 503 and the terminal 410 are only
reachable *after* authorization succeeds, so an unauthorized caller cannot learn
the feature's state or the run's lifecycle from a status code either.

**Why the internal resolver exists.** ``ActivityService.get_invocation`` returns
an ``InvocationItem``, a browser response model, and authorizes on tenant *or*
user (``elif``). Neither fits: this path needs the private control fields, which
must never be reachable from a response model, and it needs tenant *and* owner.
So :class:`ControlService` reads the raw row itself and hands back
:class:`ControlTarget`, which is a dataclass precisely so it cannot be returned
from a FastAPI route by accident.

**Why a dedicated target validator.** ``internal/credential_routes.py``
``_validate_proxy_url`` rejects private addresses — it guards calls to the public
internet, so reusing it here would reject every legitimate pod. This module wants
the mirror-image policy: the destination must be a bare IP inside a configured
cluster pod CIDR, on the configured control port, and must match the address the
worker itself registered. Both halves matter. CIDR membership without the
registration check would let any pod in the cluster be dialled by ID; the
registration check without CIDR membership would trust whatever a compromised
worker wrote into its own row (ADR-10, FR-7.3).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from pydantic import ValidationError

from src.activity.control_schemas import (
    MAX_REQUEST_BYTES,
    CommandAcknowledgement,
    ControlAction,
    ControlCapabilities,
    ControlCommandRequest,
    ControlPingResponse,
    ControlState,
    ControlStateResponse,
    ControlSteerRequest,
)
from src.activity.liveness import OBSERVED_TERMINAL_STATUSES

logger = logging.getLogger("bedrockgateway.activity.control")

# Verbs this deployment can actually perform. Started empty in S1 by design: the
# routing, authorization and transport foundation ships first, and each later
# story adds its verb here once its behaviour is proven. An authorized request for
# a verb absent from this set is a 501, and `capabilities` reports it false — so
# the dashboard never renders a control whose handler cannot honour it.
#
# Still EMPTY after S2 (#3961), deliberately. S2 built the worker-side pause
# barrier and it meets its contract, but a verb enters this set only when the
# *human dashboard path* can actually perform it end to end, and today it cannot:
# enabling a verb makes the worker listener demand a gateway-signed authorization
# envelope (#5028's `requiresEnvelope` coupling), and this path mints none — a
# logged-in operator has no grant to derive one from. Three further gaps sit
# behind that one (the pre-delivery revalidation refuses pause via
# `SUPPORTED_AGENT_ACTIONS`; envelope keys are unprovisioned unless
# `agent_authority_enabled`; and no verb-enabling evaluation artifact exists).
#
# Flipping this flag anyway would report `capabilities.pause=true` to the browser
# while `command_invocation_agent` still answers 501 — precisely what
# `ControlCapabilities` forbids: "a capability that defaulted true would advertise
# a button whose handler returns 501, and an operator who believes a run is
# pausing stops watching it."
#
# See `docs/design-notes/3961-control-authorization-intersection.md` for the
# evidence and the proposed `human_session` authority kind that would unblock it.
#
# This list and the worker's `IMPLEMENTED_CONTROL_VERBS` are deliberately
# independent, and each story owns BOTH. Widening only one side is a shipped bug
# in one of two directions: a verb the dashboard offers and the worker rejects, or
# a working worker verb the gateway answers 501 for. `agent-control-ci.yml` asserts
# the two sets agree, so the pair cannot drift silently.
#
# `steer`/`abort` stay out until S6/S4 land their own runtime proofs.
SUPPORTED_ACTIONS: frozenset[str] = frozenset()

# Feature flag. Read strictly (explicit "true" only) and read *independently* of
# the worker's own flag: a gateway that could activate worker capabilities by
# itself would let a config change on one side start listeners on the other.
FEATURE_FLAG_ENV = "FEATURE_AGENT_CONTROL_ENABLED"

# Cluster pod CIDRs the control destination must fall inside, comma-separated.
# Empty (the default) means no destination is permitted at all — fail closed. An
# empty list must not mean "anything goes": that inversion is how an unset
# variable in a fresh environment becomes an open in-cluster request forger.
CLUSTER_CIDRS_ENV = "AGENT_CONTROL_CLUSTER_POD_CIDRS"

# The one port the control listener may be dialled on. Pinned rather than taken
# from the row so a rewritten row cannot redirect the gateway at, say, the
# kubelet's port.
CONTROL_PORT_ENV = "AGENT_CONTROL_PORT"
DEFAULT_CONTROL_PORT = 8770

# Finite, short timeouts. The caller is a browser poll on a 2-second cadence, so
# a hung pod must fail fast rather than pile up gateway workers.
_CONNECT_TIMEOUT_SECONDS = 2.0
_READ_TIMEOUT_SECONDS = 5.0

# How a command outcome reported by the worker's listener becomes an HTTP status
# for the caller. Explicit rather than pass-through, and a table rather than a
# chain of `if`s, because the alternative is what #3960's review found: the
# worker's 429 had no gateway-side meaning at all, so the one back-pressure
# signal it can emit would have surfaced as an unmapped 5xx and told a saturated
# client to retry instead of to slow down.
#
# 429 is the entry that carries weight. The worker bounds its pending queue at 10
# (revival-design §2), and a client that hits that bound is being asked to wait,
# not being told the run failed. Every other entry is a status the worker and the
# gateway must agree on for the same reason the authorization gate is shared: two
# translations of one outcome drift.
#
# A status absent from this table is deliberately NOT forwarded — see
# `ControlService.status_for_pod_outcome`. Forwarding an unknown status would let
# a compromised or newer worker choose the status the browser sees, including a
# 200 for a command that was refused.
POD_OUTCOME_STATUSES: dict[int, int] = {
    200: 200,  # replayed: the recorded outcome, not a second acceptance
    202: 202,  # accepted and pending
    400: 400,  # the worker's own validation rejected the body
    409: 409,  # same command id, different content
    413: 413,  # body over the worker's cap
    429: 429,  # pending queue is full — back off, the run is healthy
    501: 501,  # this worker build cannot perform the verb
}

# The status a pod response outside that table becomes. 502 and not 500: the
# failure is upstream of the gateway, and a gateway 500 would send an operator
# reading gateway logs looking for a gateway fault.
UNMAPPED_POD_STATUS = 502

# The link-local block that carries the instance metadata service. Excluded
# explicitly and by name: an SSRF into IMDS is the highest-value target on the
# node, and `is_private` returns False for it, so a private-address check alone
# would let it through.
_METADATA_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
)

_DEFAULT_TABLE_NAME = "adp-dev-webhook-events"


class ControlError(Exception):
    """A control request that must terminate with a specific HTTP status.

    Carries the status code so the two adapters map outcomes identically. If each
    route translated failures itself, the activity path and the orchestration
    path would drift into different codes for the same condition — and the UI
    distinguishes 404 from 410 from 503 to tell the user materially different
    things ("no such run" / "already finished" / "not enabled here").
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ControlTarget:
    """The private half of a control registration.

    Deliberately a dataclass and not a Pydantic model: FastAPI will serialise a
    ``BaseModel`` returned from a handler, so making this un-returnable removes
    the failure mode where a refactor leaks ``token`` and ``address`` to the
    browser. Nothing in this module puts these fields into a response or a log —
    the token is a bearer credential and the address is the thing the
    NetworkPolicy exists to keep unreachable (FR-1.8, AC-S7).
    """

    run_id: str
    arrived_at: str
    status: str
    address: str | None
    port: int | None
    token: str | None
    generation: int | None
    token_expires_at: str | None

    @property
    def is_terminal(self) -> bool:
        """Whether the row records a positively-observed exit.

        Reuses the shared terminal set rather than a local literal, so a status
        added there (``aborted``, in a later story) revokes control here without
        a second edit. A terminal row is authoritative: control is refused even
        if a token is still present, because clearing the token at teardown is
        fail-soft and cannot be relied on alone (FR-1.15).
        """
        return self.status in OBSERVED_TERMINAL_STATUSES

    @property
    def is_registered(self) -> bool:
        """Whether a usable control registration exists.

        A row with no address is the *expected* state for every run that started
        before this feature or with the flag off — not an error (FR-1.13). The
        explicit emptiness checks matter because a truthiness guard silently
        treats an empty string the same as a missing attribute, which is the
        behaviour we want here but should be visible rather than incidental.
        """
        return bool(self.address) and bool(self.token) and self.port is not None


def _is_flag_enabled(env: dict[str, str] | None = None) -> bool:
    """Read the control flag strictly: explicit "true" enables, nothing else does.

    Matches ``features/routes.py::_is_enabled_strict``. Strict is mandatory here
    rather than stylistic — a malformed value or a lookup error must resolve to
    *off*, because failing open would enable a control channel that the ingress
    NetworkPolicy may not yet be protecting (FR-8.1).
    """
    source = os.environ if env is None else env
    value = source.get(FEATURE_FLAG_ENV)
    return value is not None and value.lower() == "true"


def _configured_port(env: dict[str, str] | None = None) -> int:
    """The single permitted control port.

    A malformed value falls back to the default rather than raising: the port is
    not a security boundary by itself (the token and the NetworkPolicy are), and
    a typo in one env var should not take down an otherwise-healthy read path.
    """
    source = os.environ if env is None else env
    raw = source.get(CONTROL_PORT_ENV)
    if not raw:
        return DEFAULT_CONTROL_PORT
    try:
        return int(raw)
    except ValueError:
        logger.warning("Malformed %s — using default control port", CONTROL_PORT_ENV)
        return DEFAULT_CONTROL_PORT


def _configured_cidrs(env: dict[str, str] | None = None) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse the permitted cluster pod CIDRs, skipping malformed entries.

    A malformed entry is dropped with a warning rather than failing the whole
    list, but note the consequence: if every entry is malformed the list is empty
    and *all* destinations are refused. That is the safe direction — an
    unparseable allowlist must not widen into an empty-means-any allowlist.
    """
    source = os.environ if env is None else env
    raw = source.get(CLUSTER_CIDRS_ENV, "")
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in raw.split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            logger.warning("Ignoring malformed CIDR in %s", CLUSTER_CIDRS_ENV)
    return networks


def validate_command_body(action: str, raw: bytes) -> str:
    """Validate a raw control command body, returning its command id.

    Lives here, not at an HTTP edge, because #3960's review found the edge
    version had been written in `activity/routes.py` only: the orchestration
    adapter's four verb routes declared no body parameter, so the size cap, the
    `extra="forbid"` rejection of `actor`/`target`/`token`, and the UUID check
    never ran there. That is latent today (no verb accepts a payload yet) and
    becomes real the moment one does — and it becomes real on the adapter nobody
    was testing. One implementation, called by both, is the same argument that
    put authorization in this module.

    The check order is load-bearing and is asserted, not assumed:

    1. **413** on the raw byte length, *before* parsing, so a hostile body never
       reaches the JSON parser.
    2. **400** for absent, unparseable, or non-object bodies.
    3. **400** for a schema violation — an unknown field is rejected loudly
       rather than dropped, because the only reason to send `actor` or `target`
       is to try to override attribution or the transport destination, and
       silently dropping it returns success to the attempt (AC-S7).
    4. **400** for a non-UUID command id, which is the idempotency key for a
       command that may abort a multi-hour run.

    All of this precedes the verb gate in :meth:`authorize_command`, so a
    malformed body is a 400 even for a verb that answers 501 (W1-05).

    Raises :class:`ControlError` so both adapters translate it identically — the
    reason a typed error exists rather than an `HTTPException` raised from a
    service.
    """
    if len(raw) > MAX_REQUEST_BYTES:
        raise ControlError(413, "control request body is too large")
    if not raw:
        raise ControlError(400, "control request body is required")
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ControlError(400, "control request body is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ControlError(400, "control request body must be a JSON object")

    try:
        if action == "steer":
            command_id = ControlSteerRequest.model_validate(parsed).command_id
        else:
            command_id = ControlCommandRequest.model_validate(parsed).command_id
    except ValidationError as exc:
        raise ControlError(400, "control request body is invalid") from exc

    try:
        uuid.UUID(command_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ControlError(400, "command_id must be a UUID") from exc
    return command_id


def validate_control_destination(
    address: str,
    port: int,
    *,
    env: dict[str, str] | None = None,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Validate a control destination before any socket is opened.

    Returns the parsed address, or raises :class:`ControlError` with 409 — the
    request was well-formed and authorized, but the recorded destination is not
    one the gateway will dial. 409 rather than 400 because the caller supplied
    nothing wrong; the *registration* is unusable.

    The order of checks is deliberate. Parsing first rejects hostnames and URLs
    outright, so no DNS lookup happens and there is no window in which a name
    resolves to one address for the check and another for the connection. The
    categorical exclusions come next, and CIDR membership last — membership is
    the allowlist, and an allowlist that a caller could satisfy with
    ``127.0.0.1`` (which is inside no cluster CIDR, but the ordering makes the
    intent explicit) is not one.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        # A URL, a hostname, or an address with a scheme or path. Rejecting these
        # is what keeps the transport target a literal IP: a caller-influenced
        # hostname is the classic SSRF pivot, and DNS rebinding defeats any
        # check performed before the connect.
        raise ControlError(409, "control registration is not a usable address") from exc

    if parsed.is_loopback:
        # Loopback would dial the *gateway's own* process, turning the proxy into
        # a confused-deputy against the gateway's internal routes.
        raise ControlError(409, "control destination is not permitted")
    if parsed.is_unspecified or parsed.is_multicast or parsed.is_reserved:
        raise ControlError(409, "control destination is not permitted")
    if any(parsed in network for network in _METADATA_NETWORKS):
        # Instance metadata and link-local. `is_private` is False for
        # 169.254.169.254, so this must be its own check.
        raise ControlError(409, "control destination is not permitted")
    if parsed.is_global:
        # A public address in a pod-address field means the row is wrong or
        # tampered with. Either way the gateway does not make egress calls on a
        # dashboard user's behalf.
        raise ControlError(409, "control destination is not permitted")

    expected_port = _configured_port(env)
    if port != expected_port:
        raise ControlError(409, "control destination port is not permitted")

    networks = _configured_cidrs(env)
    if not networks:
        # Fail closed. An unset CIDR list in a new environment must refuse every
        # destination rather than permit every destination.
        logger.warning("No cluster pod CIDRs configured — refusing control transport")
        raise ControlError(409, "control transport is not configured")
    if not any(parsed in network for network in networks):
        raise ControlError(409, "control destination is outside the cluster pod range")

    return parsed


class ControlService:
    """Resolve, authorize and forward live control requests.

    Collaborators are injected because each one is something a test must be able
    to replace without a network or a clock: the DynamoDB table, the ``httpx``
    client (the injectable-client form required by FR-7.7, not a per-call
    throwaway), and ``now`` for token-expiry tests that must not depend on
    wall-clock time or mutate a real run's fields (NFR-14).
    """

    def __init__(
        self,
        *,
        table=None,
        table_name: str | None = None,
        dynamodb_resource=None,
        http_client: httpx.AsyncClient | None = None,
        now: Callable[[], datetime] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._env = env
        self._now = now or (lambda: datetime.now(UTC))
        self._http_client = http_client
        if table is not None:
            self._table = table
        else:
            import boto3

            resource = dynamodb_resource or boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
            self._table = resource.Table(table_name or os.environ.get("WEBHOOK_EVENTS_TABLE", _DEFAULT_TABLE_NAME))

    # -- authorization ----------------------------------------------------

    def resolve_target(
        self,
        run_id: str,
        *,
        user_id: str,
        tenant_id: str,
    ) -> ControlTarget:
        """Resolve a run to its control target, or raise an identical 404.

        ``run_id`` is the invocation/event identifier Agent Activity uses. It is
        never a pod name or an orchestration node ID: a pod address is read from
        the row the worker registered, never inferred from an identifier, so a
        caller who learns *any* ID cannot turn it into a destination
        (revival-design §2).

        Both ``user_id`` and ``tenant_id`` are required. Passing only one is a
        programming error, not a lenient path, so it raises rather than
        authorizing on the half that was supplied.
        """
        if not run_id or not user_id or not tenant_id:
            raise ControlError(404, "run not found")

        try:
            response = self._table.query(
                KeyConditionExpression=Key("event_id").eq(run_id),
                ScanIndexForward=False,
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("ValidationException", "ResourceNotFoundException", "AccessDeniedException"):
                # Deploy-order gap or a missing table. Report not-found rather
                # than 500: the caller cannot act on the difference, and a 500
                # here would distinguish "table missing" from "run missing".
                logger.warning(
                    "Control row lookup failed — treating as not found",
                    extra={"run_id": run_id, "error_code": error_code},
                )
                raise ControlError(404, "run not found") from exc
            raise

        items = response.get("Items", [])
        if not items:
            raise ControlError(404, "run not found")
        row = items[0]

        # Tenant AND human owner. The `and` is the fix for the `elif` in
        # `get_invocation`: there, satisfying either check was enough, which is
        # correct for a read of your own list but would let a same-tenant
        # non-owner abort someone else's run here (FR-7.6, C10).
        if row.get("tenant_id") != tenant_id:
            raise ControlError(404, "run not found")
        if row.get("user_id") != user_id and row.get("root_human_id") != user_id:
            raise ControlError(404, "run not found")

        return ControlTarget(
            run_id=run_id,
            arrived_at=str(row.get("arrived_at", "")),
            status=str(row.get("status", "")),
            address=_optional_str(row.get("control_address")),
            port=_optional_int(row.get("control_port")),
            token=_optional_str(row.get("control_token")),
            generation=_optional_int(row.get("control_generation")),
            token_expires_at=_optional_str(row.get("control_token_expires_at")),
        )

    def require_enabled(self) -> None:
        """Refuse with 503 when the gateway's control flag is off.

        Called only after authorization, so the flag's state is never observable
        to a caller who does not own the run (AC-F1).
        """
        if not _is_flag_enabled(self._env):
            raise ControlError(503, "live run control is not enabled in this deployment")

    def token_is_live(self, target: ControlTarget) -> bool:
        """Whether the registered token is still within its recorded expiry.

        Checked at call time against an injectable clock rather than trusted
        because it was written recently. A malformed or absent expiry counts as
        expired: an unreadable bound is not a bound (FR-1.14).
        """
        if not target.token_expires_at:
            return False
        try:
            expires = datetime.fromisoformat(target.token_expires_at.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("Malformed control token expiry — treating as expired", extra={"run_id": target.run_id})
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires > self._now()

    def unavailable_reason(self, target: ControlTarget) -> str | None:
        """Why control cannot be exercised, or ``None`` when it can.

        A single place to answer this so the ping, state and command paths cannot
        disagree about whether a run is controllable, and so the reason string
        the UI shows is the reason the transport path actually acted on.
        """
        if target.is_terminal:
            return "run has reached a terminal state"
        if not target.is_registered:
            return "run has no live control registration"
        if not self.token_is_live(target):
            return "control registration has expired"
        return None

    # -- transport --------------------------------------------------------

    async def _request_pod(
        self,
        target: ControlTarget,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
    ) -> httpx.Response:
        """Make one authenticated request to the worker's control listener.

        Every hardening decision here is a rejection of a default:

        - ``follow_redirects=False`` — a redirect is a destination the validator
          never saw. Following one would move the request outside the cluster
          CIDR that was just checked.
        - ``trust_env=False`` on the fallback client — an inherited
          ``HTTP_PROXY`` would route in-cluster control traffic through whatever
          the environment names, which both leaks the bearer token and defeats
          the destination check.
        - explicit finite timeouts — a hung pod must not hold a gateway worker.

        The token travels in an ``Authorization`` header and appears in no log
        line, no trace attribute and no response body.
        """
        address = target.address or ""
        port = target.port or 0
        validate_control_destination(address, port, env=self._env)

        formatted = f"[{address}]" if ":" in address else address
        url = f"http://{formatted}:{port}{path}"
        headers = {
            "Authorization": f"Bearer {target.token}",
            # The worker rejects a request whose declared generation does not
            # match its own, so a token replayed against a *later* generation of
            # the same run fails even though the token itself parses.
            "X-Adp-Control-Generation": str(target.generation or 0),
        }
        timeout = httpx.Timeout(_READ_TIMEOUT_SECONDS, connect=_CONNECT_TIMEOUT_SECONDS)

        if self._http_client is not None:
            return await self._http_client.request(method, url, json=json_body, headers=headers, timeout=timeout, follow_redirects=False)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            return await client.request(method, url, json=json_body, headers=headers)

    # -- operations -------------------------------------------------------

    async def ping(self, run_id: str, *, user_id: str, tenant_id: str) -> ControlPingResponse:
        """The foundation slice: prove the authenticated path end to end.

        Deliberately side-effect free. It answers "could a command reach this
        run?" without being one, which is what makes it safe to expose before any
        verb works.
        """
        target = self.resolve_target(run_id, user_id=user_id, tenant_id=tenant_id)
        self.require_enabled()

        reason = self.unavailable_reason(target)
        if reason is not None:
            return ControlPingResponse(run_id=run_id, available=False, reason=reason, generation=target.generation)

        try:
            response = await self._request_pod(target, "GET", "/agent/ping")
        except ControlError:
            raise
        except httpx.HTTPError:
            # Loss of contact is not evidence of exit — the same discipline
            # `activity/liveness.py` applies to run status. A transport failure
            # means we could not learn the answer, so control is reported
            # unavailable rather than the run reported dead (NFR-8).
            logger.warning("Control ping transport failed", extra={"run_id": run_id})
            return ControlPingResponse(run_id=run_id, available=False, reason="control listener unreachable", generation=target.generation)

        if response.status_code != 200:
            return ControlPingResponse(
                run_id=run_id,
                available=False,
                reason="control listener rejected the request",
                generation=target.generation,
            )
        key_ids = None
        try:
            body = response.json()
            reported = body.get("verification_key_ids") if isinstance(body, dict) else None
            if (
                isinstance(reported, list)
                and len(reported) <= 8
                and all(isinstance(kid, str) and re.fullmatch(r"[0-9a-f]{16}", kid) for kid in reported)
            ):
                key_ids = sorted(set(reported))
        except ValueError:
            pass
        return ControlPingResponse(run_id=run_id, available=True, reason=None, generation=target.generation, verification_key_ids=key_ids)

    async def get_state(self, run_id: str, *, user_id: str, tenant_id: str) -> ControlStateResponse:
        """Serve the polled control state.

        A terminal run is answered from the row alone, with ``available=False``
        and every capability false — no pod is contacted. Two reasons: the pod is
        gone, so dialling it would spend a timeout to learn nothing; and an owner
        polling a finished run must get a definite terminal answer rather than a
        transport error that the UI would have to guess about (revival-design
        §2).

        A plain state read never starts an assistant turn. That is a property of
        the worker's handler, which serves this from recorded state and does not
        touch the SDK; it is asserted there rather than assumed here.
        """
        target = self.resolve_target(run_id, user_id=user_id, tenant_id=tenant_id)
        self.require_enabled()

        if target.is_terminal:
            return ControlStateResponse(
                run_id=run_id,
                generation=target.generation,
                available=False,
                reason="run has reached a terminal state",
                capabilities=ControlCapabilities(),
                state="terminal",
                updated_at=_iso(self._now()),
            )

        reason = self.unavailable_reason(target)
        if reason is not None:
            return ControlStateResponse(
                run_id=run_id,
                generation=target.generation,
                available=False,
                reason=reason,
                state="unavailable",
                updated_at=_iso(self._now()),
            )

        try:
            response = await self._request_pod(target, "GET", "/agent/state")
        except ControlError:
            raise
        except httpx.HTTPError:
            logger.warning("Control state transport failed", extra={"run_id": run_id})
            return ControlStateResponse(
                run_id=run_id,
                generation=target.generation,
                available=False,
                reason="control listener unreachable",
                state="unavailable",
                updated_at=_iso(self._now()),
            )

        if response.status_code != 200:
            return ControlStateResponse(
                run_id=run_id,
                generation=target.generation,
                available=False,
                reason="control listener rejected the request",
                state="unavailable",
                updated_at=_iso(self._now()),
            )
        return self._state_from_pod(run_id, target, response)

    def _state_from_pod(self, run_id: str, target: ControlTarget, response: httpx.Response) -> ControlStateResponse:
        """Rebuild the state response from the worker's payload, field by field.

        The pod's body is *not* passed through. It is re-projected onto
        ``ControlStateResponse`` so that a field the worker adds — or a private
        field a future worker bug includes — cannot reach the browser just
        because the worker sent it. Capabilities are additionally intersected
        with this gateway's ``SUPPORTED_ACTIONS``: a worker claiming a capability
        the gateway will not route must not produce a button the gateway answers
        with 501.
        """
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        raw_caps = payload.get("capabilities")
        raw_caps = raw_caps if isinstance(raw_caps, dict) else {}
        capabilities = ControlCapabilities(
            pause=bool(raw_caps.get("pause")) and "pause" in SUPPORTED_ACTIONS,
            resume=bool(raw_caps.get("resume")) and "resume" in SUPPORTED_ACTIONS,
            steer=bool(raw_caps.get("steer")) and "steer" in SUPPORTED_ACTIONS,
            abort=bool(raw_caps.get("abort")) and "abort" in SUPPORTED_ACTIONS,
        )

        state = payload.get("state")
        valid_states = ("running", "pause_requested", "paused", "abort_requested", "terminal", "unavailable")
        resolved_state: ControlState = state if state in valid_states else "unavailable"

        commands: list[CommandAcknowledgement] = []
        raw_commands = payload.get("commands")
        if isinstance(raw_commands, list):
            for entry in raw_commands:
                if not isinstance(entry, dict):
                    continue
                action = entry.get("action")
                if action not in ("pause", "resume", "steer", "abort"):
                    continue
                status = entry.get("status")
                if status not in ("pending", "delivered", "applied", "cancelled", "rejected", "unknown"):
                    status = "unknown"
                command_id = entry.get("command_id")
                if not isinstance(command_id, str) or not command_id:
                    continue
                commands.append(
                    CommandAcknowledgement(
                        command_id=command_id,
                        action=action,
                        status=status,
                        accepted_at=_optional_str(entry.get("accepted_at")),
                        delivered_at=_optional_str(entry.get("delivered_at")),
                        reason=_optional_str(entry.get("reason")),
                    )
                )

        active_tool_count = payload.get("active_tool_count")
        if not isinstance(active_tool_count, int) or isinstance(active_tool_count, bool):
            active_tool_count = None

        return ControlStateResponse(
            run_id=run_id,
            generation=target.generation,
            available=True,
            reason=None,
            capabilities=capabilities,
            state=resolved_state,
            active_tool_count=active_tool_count,
            updated_at=_optional_str(payload.get("updated_at")) or _iso(self._now()),
            commands=commands,
        )

    def authorize_command(
        self,
        run_id: str,
        action: ControlAction,
        *,
        user_id: str,
        tenant_id: str,
    ) -> ControlTarget:
        """Run the full command gate, raising the terminal status for this story.

        The check order is the contract, not an implementation detail, and each
        step is placed where it is for a reason:

        1. **404** — authorization first, so nothing below is observable to a
           caller who does not own the run.
        2. **503** — flag off. Above the unsupported-verb check because a
           deployment with the feature off should say so uniformly rather than
           report per-verb implementation status it does not run (AC-F1).
        3. **410** — terminal run. Above 501 so an owner commanding a finished
           run learns it finished; "already over" is more actionable than "not
           built yet", and it is the distinction FR-7.9 asks the UI to draw.
        4. **501** — verb not yet implemented. Reached only by an authorized,
           enabled, non-terminal request, which is exactly the case where the
           honest answer is "this verb does not work yet".
        5. **409** — enabled and supported, but this run has no reachable
           registration.

        Body validation happens in the route *before* this call, so a malformed
        payload is a 400 even for a verb that is not implemented (W1-05).
        """
        target = self.resolve_target(run_id, user_id=user_id, tenant_id=tenant_id)
        self.require_enabled()

        if target.is_terminal:
            raise ControlError(410, "run has already finished; the command was not applied")
        if action not in SUPPORTED_ACTIONS:
            raise ControlError(501, f"{action} is not implemented in this deployment")

        reason = self.unavailable_reason(target)
        if reason is not None:
            raise ControlError(409, reason)
        return target

    def status_for_pod_outcome(self, response: httpx.Response) -> int:
        """Translate a command response from the worker into a caller status.

        Separated from the transport call so the mapping is testable without a
        pod and, more importantly, so it lives on the *service* rather than in
        either adapter. #3960's review found request validation had landed in
        `activity/routes.py` only, leaving the orchestration adapter without it;
        a status mapping written at one edge would drift the same way, and the
        two would then disagree about what a saturated queue means.

        Unknown statuses collapse to 502 rather than being forwarded. The worker
        is inside the trust boundary for *its own run's state*, not for choosing
        the status code a browser receives — forwarding blindly would let a
        compromised worker answer 200 to a command it refused, or 401 to one it
        accepted, and the second is worse: it would make the dashboard log the
        operator out on a queue hiccup.
        """
        mapped = POD_OUTCOME_STATUSES.get(response.status_code)
        if mapped is None:
            logger.warning(
                "Control listener returned an unmapped status",
                extra={"pod_status": response.status_code},
            )
            return UNMAPPED_POD_STATUS
        return mapped


def _optional_str(value) -> str | None:
    """Coerce a DynamoDB attribute to a non-empty string, or ``None``.

    An empty string becomes ``None`` on purpose: a row carrying
    ``control_address: ""`` (which a cleared registration produces) must read as
    "not registered", not as an address that fails validation later.
    """
    if value is None:
        return None
    text = str(value)
    return text or None


def _optional_int(value) -> int | None:
    """Coerce a DynamoDB numeric attribute to ``int``, or ``None``.

    DynamoDB numbers arrive as ``Decimal`` through the resource API, so ``int()``
    is needed rather than an ``isinstance`` check. A non-numeric value yields
    ``None`` and the caller treats the registration as unusable.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso(moment: datetime) -> str:
    """Render a timestamp in the ``...Z`` form the rest of the activity API uses."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
