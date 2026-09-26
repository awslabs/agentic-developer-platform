"""
Agent Gateway — Ingest Lambda

Universal front door with thread-aware concurrency:
  direct_response → answer immediately, no thread
  long_running    → per-thread serialization via SQS/KEDA
  github_actions  → create/label GitHub issue, always parallel

Stage C (#186): upload-token and upload-complete routes for user file uploads.
"""

import json
import hashlib
import logging
import os
import re
import secrets
import time
import uuid
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

from channels.base import (
    UNUSABLE_ORG_IDS,
    ChannelAdapter,
    ChannelType,
    UnifiedMessage,
)
from channels.gateway_api import GATEWAY_API_SOURCE, GatewayApiAdapter
from channels.slack import SlackAdapter
from channels.webchat import InvalidWebChatRequest, WebChatAdapter
from classifier import ClassificationResult, classify_message
from github_dispatch import create_issue_and_dispatch, label_existing_issue
from invocation_logger import log_invocation
from user_resolver import (
    ENABLE_USER_IDENTITIES,
    ResolvedUser,
    UnresolvedUser,
    resolve_user,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

INPUT_QUEUE_URL = os.environ["INPUT_QUEUE_URL"]
RESPONSE_QUEUE_URL = os.environ.get("RESPONSE_QUEUE_URL", "")
SESSIONS_TABLE = os.environ["SESSIONS_TABLE_NAME"]
REGION = os.environ.get("AWS_REGION_NAME", "us-east-1")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
SLACK_BOT_USER_ID = os.environ.get("SLACK_BOT_USER_ID", "")
# Phase 4 (#1458): Invocation logging for non-GitHub channels
WEBHOOK_EVENTS_TABLE = os.environ.get("WEBHOOK_EVENTS_TABLE", "")
# Stage C (#186): artifact bucket and catalog table for user uploads
ARTIFACTS_BUCKET = os.environ.get("ARTIFACTS_BUCKET", "")
ARTIFACTS_TABLE = os.environ.get("ARTIFACTS_TABLE", "")
# WebSocket post-back endpoint (for async responses to upload-token /
# upload-complete requests that arrive over WS). API Gateway WebSocket
# doesn't deliver synchronous Lambda responses to the client — we must
# push them back via post_to_connection.
WS_API_ENDPOINT = os.environ.get("WS_API_ENDPOINT", "")

# ─── Persona pinning (#4208) ──────────────────────────────────
# A client may pin the persona for a turn, bypassing the Bedrock classifier
# (used by the intent-intake chat, which must always land on the interviewer
# persona rather than whatever the classifier infers from the message text).
#
# The field arrives from the browser and is therefore UNTRUSTED: an arbitrary
# value here would let a client select any agent type through the chat box.
# It is validated against an explicit allowlist and a strict name pattern, and
# a non-matching value REJECTS the message — we never fall back to the
# classifier, because a silent fallback would mask a client bug (or an attack)
# as a working conversation on the wrong persona.
#
# The worker carries its own defence-in-depth check over the personas baked
# into the image (agent/src/complex-task-chat/persona-loader.ts). This Lambda
# cannot read that directory, so the allowlist is duplicated here
# deliberately; keep the two in sync when adding a pinnable persona.
PINNABLE_PERSONAS = frozenset({"intent-refinement"})

# Mirrors PERSONA_NAME_PATTERN in persona-loader.ts. Blocks path traversal
# (e.g. "../../etc/passwd") even for values that clear the allowlist check.
PERSONA_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

# ─── Session id shape (#5660 / A07) ───────────────────────────
# `session_id` arrives from the client and is used BOTH as this table's key and
# as a path segment in the artifact S3 key layout:
#
#   o/<org_id>/t/<team_id>/u/<user_id>/s/<session_id>/<task_id>/{in|out}/<file>
#
# Because it lands in a storage path, an id that is not a single harmless
# segment is a storage-scope bug. A separator or `..` escapes its own prefix,
# and a bare `o`/`t`/`u`/`s` collides with the FIXED leading segments above —
# the sweeper's prefix was `${session_id}/`, so a session named `o` deleted
# every tenant's uploads on that session's ordinary TTL expiry.
#
# Mirrors agent/src/complex-task-chat/session-id.ts; keep the two in sync.
#
# The charset is set by the id formats ALREADY IN PRODUCTION, all of which must
# keep working — an over-strict rule here would strand live conversations:
#   - the SPA's `sess-<epoch>-<rand>`  and the CLI's `sess-<uuid hex>`
#   - Slack's thread timestamp `1758441600.123456`     → `.` is required
#     (channels/slack.py passes `thread_ts` through as `thread_id`)
#   - the `session_key` fallback `webchat:C123:user-1` → `:` is required
#     (channels/base.py, used when a channel supplies no thread id)
# Neither `.` nor `:` is a path separator, so neither can widen a derived
# prefix. `/` is excluded and `..` is rejected below, so nothing can traverse.
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
RESERVED_SESSION_IDS = frozenset({"o", "t", "u", "s"})

# Reserved `session_id` key prefixes on the sessions table. `conn#<id>` holds a
# connection's durable claims, and `gateway-turn#` rows hold turn-dedupe state —
# neither is a conversation, and a client that named one could read or overwrite
# connection identity or replay a turn. The shape rule rejects `#` already, so
# this is a named second line of defence rather than the only one.
RESERVED_SESSION_KEY_PREFIXES = ("conn#", "gateway-turn#", "dedupe#")


def is_valid_session_id(session_id: Any) -> bool:
    """True when `session_id` is safe as a DDB key and a single S3 path segment."""
    if not isinstance(session_id, str):
        return False
    if not SESSION_ID_PATTERN.match(session_id):
        return False
    # The charset admits `.`, so traversal must be refused explicitly. Rejected
    # anywhere in the value, not just alone: a consumer that normalises the path
    # (checkout, sync tool, signed-URL rewriter) can resolve `a/../b` upward and
    # out of the caller's prefix.
    if ".." in session_id or session_id == ".":
        return False
    if session_id in RESERVED_SESSION_IDS:
        return False
    return not session_id.startswith(RESERVED_SESSION_KEY_PREFIXES)

sqs = boto3.client("sqs", region_name=REGION)
s3_client = boto3.client("s3", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)
sessions_table = dynamodb.Table(SESSIONS_TABLE)

_apigw_client = None


def _get_apigw_client():
    """Lazily create the apigatewaymanagementapi client. The endpoint must
    use https://, not wss://."""
    global _apigw_client
    if _apigw_client is None and WS_API_ENDPOINT:
        endpoint = WS_API_ENDPOINT.replace("wss://", "https://")
        _apigw_client = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint, region_name=REGION)
    return _apigw_client


def _send_ws_response(connection_id: str, request_id: str, payload: dict) -> None:
    """Post a response back to the WebSocket client, keyed by request_id so
    the frontend's sendWsRequest helper can correlate it.

    Best-effort: if the post fails (client disconnected, permission gap,
    endpoint misconfigured), log and continue — the Lambda's return value is
    still shaped as a REST response so existing tests/callers see no change.
    """
    client = _get_apigw_client()
    if not client or not connection_id:
        logger.warning("Cannot post WS response: client=%s connection_id=%s", bool(client), bool(connection_id))
        return
    body = {"request_id": request_id, **payload} if request_id else payload
    try:
        client.post_to_connection(ConnectionId=connection_id, Data=json.dumps(body).encode("utf-8"))
    except Exception as e:
        logger.warning("post_to_connection failed for %s: %s", connection_id, e)

ADAPTERS: dict[str, ChannelAdapter] = {
    "webchat": WebChatAdapter(),
    "slack": SlackAdapter(signing_secret=SLACK_SIGNING_SECRET, bot_user_id=SLACK_BOT_USER_ID),
    # #5331: IAM-gated operator-plane invocations from the gateway (`adp flow
    # start`). Registered as its own adapter but EMITS ChannelType.WEBCHAT, so the
    # session row it produces is indistinguishable from a browser-started one and
    # `--resume` finds either from the same GSI partition. See
    # `channels/gateway_api.py` on why a distinct channel value would split them.
    GATEWAY_API_SOURCE: GatewayApiAdapter(),
}


# ---------------------------------------------------------------------------
# Vault Phase 5 (#138): Unresolved user handling (magic-link flow)
# ---------------------------------------------------------------------------

_MAGIC_LINK_SLACK_MSG = (
    ":link: *Link your ADP account to use this bot.*\n"
    "This is a one-time setup: {magic_link_url}"
)

_MAGIC_LINK_GITHUB_MSG = (
    ":link: **Link your ADP account to use this bot.**\n\n"
    "This is a one-time setup: {magic_link_url}"
)


def _handle_unresolved_user(
    channel_name: str,
    message: UnifiedMessage,
    magic_link_url: str,
    event: dict,
) -> dict:
    """Handle an unresolved user by posting a magic-link in-channel.

    Does NOT enqueue the message for processing — returns early.
    """
    if not magic_link_url:
        magic_link_url = "(magic link unavailable — contact your admin)"

    if channel_name == "slack":
        text = _MAGIC_LINK_SLACK_MSG.format(magic_link_url=magic_link_url)
        logger.info(
            "Unresolved Slack user %s — magic link issued", message.provider_user_id
        )
    else:
        text = _MAGIC_LINK_GITHUB_MSG.format(magic_link_url=magic_link_url)
        logger.info(
            "Unresolved %s user %s — magic link issued",
            channel_name,
            message.provider_user_id,
        )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": "unresolved_user",
            "magic_link_url": magic_link_url,
            "message": text,
        }),
    }


def lambda_handler(event, context):
    route_key = event.get("requestContext", {}).get("routeKey", "")
    connection_id = event.get("requestContext", {}).get("connectionId", "")

    if route_key == "$connect":
        # API Gateway WebSocket runs the Cognito authorizer on $connect only;
        # subsequent $default invocations arrive without an authorizer context.
        # Persist the authorized claims keyed by connection_id so we can
        # reinject them on every message — otherwise the webchat adapter
        # drops all messages for lack of a resolvable sub (issue #88).
        try:
            _persist_connection_claims(connection_id, event.get("requestContext", {}).get("authorizer", {}))
        except ConnectionClaimsError as error:
            return _connection_claims_failure(event, connection_id, error, notify=False)
        return {"statusCode": 200, "body": "Connected"}
    if route_key == "$disconnect":
        _forget_connection(connection_id)
        return {"statusCode": 200, "body": "Disconnected"}

    # For WebSocket message routes, pull the claims we stashed at $connect and
    # inject them back into event.requestContext.authorizer.claims so the
    # adapter's claims.get("sub") path works without re-authenticating.
    if connection_id:
        try:
            _restore_connection_claims(event, connection_id)
        except ConnectionClaimsError as error:
            return _connection_claims_failure(event, connection_id, error, notify=True)

    # Stage C (#186): handle upload-token and upload-complete routes.
    # These arrive as WebSocket messages with action: "upload-token" or "upload-complete".
    if connection_id:
        body = parse_body(event)
        action = body.get("action", "")
        # #5615 (S16): server-issued session ids. Routed here rather than as its
        # own API Gateway route because the WS API selects on
        # `$request.body.action` with a `$default` route already present, so an
        # unlisted action reaches this Lambda unchanged.
        if action == "create-session":
            return handle_create_session(event, connection_id, body)
        if action == "upload-token":
            return handle_upload_token(event, connection_id, body)
        if action == "upload-complete":
            return handle_upload_complete(event, connection_id, body)

    channel_name, adapter = detect_channel(event)

    if channel_name == "slack":
        payload = parse_body(event)
        if payload.get("type") == "url_verification":
            return {"statusCode": 200, "body": json.dumps(SlackAdapter.handle_url_verification(payload))}
        if not adapter.verify_request(event.get("headers", {}), event.get("body", "").encode("utf-8")):
            return {"statusCode": 401, "body": "Invalid signature"}

    # webchat needs the whole event (it reads `requestContext.authorizer.claims`);
    # gateway-api needs it too, because a direct invocation's event IS the envelope
    # — there is no `body` wrapper. Only the HTTP-webhook channels have a body.
    try:
        if channel_name in ("webchat", GATEWAY_API_SOURCE):
            message = adapter.parse_event(event)
        else:
            message = adapter.parse_event(parse_body(event))
    except InvalidWebChatRequest as error:
        payload = {"error": str(error)}
        _send_ws_response(connection_id, parse_body(event).get("request_id", ""), payload)
        return {"statusCode": 400, "body": json.dumps(payload)}
    if message is None:
        return {"statusCode": 200, "body": "OK"}

    # Vault Phase 5 (#138): resolve provider identity to internal user.
    # WebChat (provider=cognito) is already resolved via Cognito $connect claims
    # and skips the resolver unless we want a consistency pre-create.
    if ENABLE_USER_IDENTITIES and message.provider and message.provider != "cognito":
        resolution = resolve_user(
            provider=message.provider,
            provider_user_id=message.provider_user_id,
            channel_context=message.platform_data.get("team_id"),
        )
        if isinstance(resolution, ResolvedUser):
            # Inject resolved identity into the message pipeline
            message.user_id = resolution.user_id
            message.platform_data["org_id"] = resolution.org_id
            # Slack supplies a workspace ID, not an ADP tenant. Use the
            # server-resolved organization for the registered run capability.
            message.platform_data["tenant_id"] = resolution.org_id
            message.platform_data["team_id"] = resolution.team_id
        elif isinstance(resolution, UnresolvedUser):
            # User not linked — send magic-link, do NOT enqueue
            return _handle_unresolved_user(
                channel_name, message, resolution.magic_link_url, event
            )
        # resolution is None → feature misconfigured or transient error; proceed
        # without blocking the user (graceful degradation).

    return handle_unified_message(message)


# ─── Connection claims persistence ────────────────────────────
# API Gateway WebSocket only runs the Cognito JWT authorizer on $connect.
# Subsequent $default / sendMessage invocations arrive without the authorizer
# context, so we stash the authorized sub/email/etc on $connect and rehydrate
# them on each message.  Keyed by connection_id (short-lived) with a TTL so
# abandoned connections get cleaned up.

CONNECTION_CLAIMS_TTL_SECONDS = 24 * 3600


class SessionOwnershipError(Exception):
    """The caller named a session that is not theirs (#5660 / A07).

    Raised instead of returning a falsy value so no caller can mistake a refusal
    for "no session found" and fall through to creating or adopting the row.
    The message deliberately does not say whether the session exists, who owns
    it, or how it differs from the caller's own — that would turn the refusal
    into an oracle for enumerating other tenants' session ids.
    """

    def __init__(self, session_id: str):
        super().__init__("Session not found or not accessible")
        self.session_id = session_id


class SessionStoreError(Exception):
    """The session owner could not be read or written safely."""


class ConnectionClaimsError(Exception):
    """A WebSocket cannot continue without a durable authenticated identity."""

    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _connection_claims_failure(
    event: dict, connection_id: str, error: ConnectionClaimsError, *, notify: bool
) -> dict:
    payload = {
        "type": "response",
        "status": "failed",
        "code": error.code,
        "error": str(error),
        "content": str(error),
    }
    if notify:
        body = parse_body(event)
        if not isinstance(body, dict):
            body = {}
        payload["session_id"] = body.get("session_id", "")
        # WebSocket proxy integrations discard Lambda return bodies. Push the
        # failure directly: Chat handles status=failed and clears its spinner;
        # upload callers use request_id/error to reject the pending request.
        _send_ws_response(connection_id, body.get("request_id", ""), payload)
    return {"statusCode": error.status_code, "body": json.dumps(payload)}


def _persist_connection_claims(connection_id: str, authorizer_ctx: dict) -> None:
    if not connection_id:
        raise ConnectionClaimsError(
            "connection_identity_missing", "Chat connection is invalid. Please reconnect.", 401
        )
    # The gateway's custom authorizer puts claims under X-Agent-* context keys;
    # also accept Cognito-JWT-native "claims" dict as a fallback.
    claims = authorizer_ctx.get("claims", {})
    sub = (
        authorizer_ctx.get("X-Agent-UserId")
        or authorizer_ctx.get("principalId")
        or claims.get("sub", "")
    )
    email = authorizer_ctx.get("X-Agent-Email") or claims.get("email", "")
    tenant_id = authorizer_ctx.get("X-Agent-Tenant") or claims.get("custom:tenant_id", "")

    # Stage A (#184): persist extended identity claims from Cognito JWT.
    # Mirror gateway's CognitoJWTValidator._parse_claims() — these are injected
    # by the Pre Token Generation Lambda as custom:* attributes.
    org_id = authorizer_ctx.get("X-Agent-OrgId") or claims.get("custom:org_id", "")
    team_id = authorizer_ctx.get("X-Agent-TeamId") or claims.get("custom:team_id", "")
    department_id = authorizer_ctx.get("X-Agent-DepartmentId") or claims.get("custom:department_id", "")
    account_type = authorizer_ctx.get("X-Agent-AccountType") or claims.get("custom:account_type", "")
    role = authorizer_ctx.get("X-Agent-Role") or claims.get("custom:role", "")

    if not sub:
        logger.warning(
            "Connection %s authorized but no sub/X-Agent-UserId in authorizer context; "
            "rejecting connection.",
            connection_id,
        )
        raise ConnectionClaimsError(
            "connection_identity_missing", "Chat identity is missing. Please sign in again.", 401
        )
    try:
        item: dict[str, Any] = {
            "session_id": f"conn#{connection_id}",
            "kind": "connection_claims",
            "sub": sub,
            "email": email,
            "tenant_id": tenant_id,
            "expires_at": int(time.time()) + CONNECTION_CLAIMS_TTL_SECONDS,
        }
        # Stage A (#184): persist extended identity claims (only non-empty).
        if org_id:
            item["org_id"] = org_id
        if team_id:
            item["team_id"] = team_id
        if department_id:
            item["department_id"] = department_id
        if account_type:
            item["account_type"] = account_type
        if role:
            item["role"] = role

        sessions_table.put_item(Item=item)
        logger.info(
            "Persisted connection claims for %s (sub=%s, org=%s, team=%s, acct_type=%s)",
            connection_id, sub, org_id or "none", team_id or "none", account_type or "none",
        )
    except Exception as e:
        logger.error("Failed to persist connection claims for %s: %s", connection_id, e)
        raise ConnectionClaimsError(
            "connection_identity_unavailable",
            "Chat could not save your sign-in session. Please reconnect and retry. "
            "If this continues, contact your ADP administrator.",
            503,
        ) from e


def _forget_connection(connection_id: str) -> None:
    if not connection_id:
        return
    try:
        sessions_table.delete_item(Key={"session_id": f"conn#{connection_id}"})
    except Exception as e:
        logger.warning("Failed to clean up connection claims for %s: %s", connection_id, e)


def _restore_connection_claims(event: dict, connection_id: str) -> None:
    """Re-inject persisted claims into event.requestContext.authorizer.claims
    so adapters can use the normal claims.get("sub") path."""
    # An authenticated native claims context needs no restoration. Real API
    # Gateway WebSocket message events omit this context after $connect.
    if event.get("requestContext", {}).get("authorizer", {}).get("claims", {}).get("sub"):
        return
    try:
        resp = sessions_table.get_item(
            Key={"session_id": f"conn#{connection_id}"}, ConsistentRead=True
        )
    except Exception as e:
        logger.error("Failed to restore connection claims for %s: %s", connection_id, e)
        raise ConnectionClaimsError(
            "connection_identity_unavailable",
            "Chat could not restore your sign-in session. Please reconnect and retry. "
            "If this continues, contact your ADP administrator.",
            503,
        ) from e
    item = resp.get("Item") or {}
    if not item.get("sub") or item.get("expires_at", 0) <= time.time():
        raise ConnectionClaimsError(
            "connection_identity_expired",
            "Your chat sign-in session has expired. Please reconnect or sign in again.",
            401,
        )
    request_context = event.setdefault("requestContext", {})
    authorizer = request_context.setdefault("authorizer", {})
    # A complete native authorizer context returned above. Restore the durable
    # identity as a unit rather than mixing it with an incomplete context.
    claims = {"sub": item["sub"]}
    authorizer["claims"] = claims
    if item.get("email"):
        claims.setdefault("email", item["email"])
    if item.get("tenant_id"):
        claims.setdefault("custom:tenant_id", item["tenant_id"])
    # Stage A (#184): restore extended identity claims.
    if item.get("org_id"):
        claims.setdefault("custom:org_id", item["org_id"])
    if item.get("team_id"):
        claims.setdefault("custom:team_id", item["team_id"])
    if item.get("department_id"):
        claims.setdefault("custom:department_id", item["department_id"])
    if item.get("account_type"):
        claims.setdefault("custom:account_type", item["account_type"])
    if item.get("role"):
        claims.setdefault("custom:role", item["role"])


# ─── Stage C (#186): Upload handlers ──────────────────────────

# Max upload size: 50 MB
UPLOAD_MAX_BYTES = 50 * 1024 * 1024
UPLOAD_TOKEN_EXPIRY = 3600  # 1 hour


def _get_upload_claims(event: dict, connection_id: str) -> dict[str, str] | None:
    """Extract identity claims for upload scoping. Returns None if required claims missing."""
    claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
    user_id = claims.get("sub", "")
    org_id = claims.get("custom:org_id", "")
    team_id = claims.get("custom:team_id", "")
    tenant_id = claims.get("custom:tenant_id", "") or org_id
    if not user_id:
        return None
    return {
        "user_id": user_id, "tenant_id": tenant_id,
        "org_id": org_id, "team_id": team_id,
    }


def _is_safe_path_segment(value: str) -> bool:
    """True when `value` cannot escape or widen its own level of the key layout."""
    return (
        isinstance(value, str)
        and bool(value)
        and bool(re.match(r"^[A-Za-z0-9_.:-]{1,128}$", value))
        and ".." not in value
    )


def _build_upload_s3_key(org_id: str, team_id: str, user_id: str,
                         session_id: str, task_id: str, filename: str) -> str:
    """Build the hierarchical S3 key for a user upload.

    #5660 (A07): every identity segment is validated, because a segment holding a
    separator would move the object out of its owner's prefix — which both hides
    it from the owner's own listing and places it inside somebody else's. Raises
    rather than falling back, since the legacy flat layout below proves nothing
    about ownership and must never be reachable for a *new* upload.
    """
    for segment in (org_id, team_id, user_id, session_id, task_id):
        if not _is_safe_path_segment(segment):
            raise ValueError("upload key segment is not a single safe path segment")
    return f"o/{org_id}/t/{team_id}/u/{user_id}/s/{session_id}/{task_id}/in/{filename}"


def _session_owner_principal(tenant_id: str, org_id: str, team_id: str,
                             user_id: str, channel: str) -> str:
    """Return the canonical owner stamped on sessions and task envelopes."""
    tenant_id = str(tenant_id or "").strip()
    org_id = str(org_id or "").strip()
    team_id = str(team_id or "").strip()
    user_id = str(user_id or "").strip()
    channel = str(channel or "").strip()
    if tenant_id.lower() in UNUSABLE_ORG_IDS or not user_id or not channel:
        raise SessionStoreError("verified session owner is incomplete")
    return json.dumps(
        [tenant_id, org_id, team_id, user_id, channel], separators=(",", ":"),
    )


def _assert_session_item_owner(item: dict | None, expected_principal: str,
                               session_id: str) -> dict:
    """Return a session row only when its complete recorded owner matches."""
    if not item or item.get("chat_task_persona"):
        # Task-backed CLI conversations use canonical Task admission only.
        # Browser/legacy ingest must not attach an untracked classifier turn.
        raise SessionOwnershipError(session_id)
    recorded_principal = str(item.get("owner_principal", "") or "")
    if not recorded_principal or recorded_principal != expected_principal:
        logger.warning(
            "OWNERSHIP REFUSED session=%s: recorded principal is missing or mismatched",
            session_id,
        )
        raise SessionOwnershipError(session_id)
    return item


def _assert_session_owned_by_caller(session_id: str, identity: dict[str, str]) -> None:
    """Raise SessionOwnershipError unless the complete caller owns the session.

    #5660 (A07): the upload routes take `session_id` from the request body, so
    without this a caller who learned another user's session id could attach
    files to that conversation and list its catalogue.

    Both upload routes require a session that ALREADY EXISTS and is owned by the
    verified caller. A missing session and a legacy row without complete
    ownership are both quarantined rather than adopted — see
    `handle_upload_token` for why "missing" must be a refusal here and not a
    create.
    """
    expected_principal = _session_owner_principal(
        identity["tenant_id"], identity["org_id"], identity["team_id"],
        identity["user_id"], "webchat",
    )
    try:
        resp = sessions_table.get_item(Key={"session_id": session_id}, ConsistentRead=True)
    except Exception as e:
        # Fail closed. An unavailable ownership record is not permission.
        logger.error("Ownership lookup failed for session %s: %s", session_id, e)
        raise SessionOwnershipError(session_id) from e

    _assert_session_item_owner(resp.get("Item"), expected_principal, session_id)


# ─── Server-issued session ids (#5615 / S16) ──────────────────
# Number of random bytes behind a minted session id. 16 bytes = 128 bits, the
# same order as the uuid4 the CLI path already mints, rendered as 32 hex chars
# so the result stays inside SESSION_ID_PATTERN and MAX_SESSION_ID_LENGTH.
SESSION_ID_ENTROPY_BYTES = 16

# A mint retries only on the vanishingly unlikely collision with an existing
# row. Bounded so a systematically failing store surfaces as an error instead of
# an unbounded loop inside a Lambda invocation.
SESSION_MINT_ATTEMPTS = 3


def _mint_session_id() -> str:
    """Return an unguessable session id.

    `secrets` (the OS CSPRNG), never `random`/`Math.random`/a timestamp. The
    browser previously built this id as `sess-<epoch-ms>-<Math.random suffix>`,
    which is predictable: most of it is the clock, and `Math.random` is not a
    cryptographic source. Ownership is still enforced separately — see
    `_create_webchat_session` — so entropy here supplements that check rather
    than replacing it. What entropy alone fixes is SQUATTING: an id a victim's
    browser was about to choose could be pre-created by somebody else, leaving
    the victim's own new conversation refused.

    Keeps the `sess-` prefix every existing reader already tolerates, so live
    conversations and the shape rule above are unaffected.
    """
    return f"sess-{secrets.token_hex(SESSION_ID_ENTROPY_BYTES)}"


def _create_webchat_session(connection_id: str, identity: dict[str, str]) -> str:
    """Mint and record a new webchat session owned by the verified caller.

    The owner is derived ONLY from `identity`, which comes from the claims the
    $connect authorizer verified and we persisted — never from the request body.
    So the row is owned from the instant it exists, and every existing ownership
    check (`get_or_create_session`, `_assert_session_owned_by_caller`) applies to
    it unchanged.

    Raises SessionStoreError if the caller's identity is incomplete or no id
    could be recorded, so a failure can never be mistaken for a created session.
    """
    expected_principal = _session_owner_principal(
        identity["tenant_id"], identity["org_id"], identity["team_id"],
        identity["user_id"], "webchat",
    )
    for _ in range(SESSION_MINT_ATTEMPTS):
        session_id = _mint_session_id()
        now = int(time.time())
        item = {
            "session_id": session_id,
            "owner_principal": expected_principal,
            "owner_user_id": identity["user_id"],
            "user_workspace": f'{identity["user_id"]}#webchat',
            "tenant_id": identity["tenant_id"],
            "connection_id": connection_id,
            "channel": "webchat",
            "messages": [],
            "threads": {},
            "created_at": now,
            "updated_at": now,
            "expires_at": now + 86400,
        }
        # Match get_or_create_session, which omits these rather than writing ""
        # so a row never claims an org/team the caller does not have.
        if identity["org_id"]:
            item["org_id"] = identity["org_id"]
        if identity["team_id"]:
            item["team_id"] = identity["team_id"]
        try:
            sessions_table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(session_id)",
            )
            return session_id
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                logger.error("Session mint failed: %s", error)
                raise SessionStoreError("session create failed") from error
            # Collision: fail closed and mint a FRESH id rather than adopting the
            # existing row. Adopting it would hand the caller a conversation they
            # may not own — exactly the bug this route exists to prevent.
            logger.warning("Minted session id collided; retrying with a fresh id")
        except Exception as error:
            logger.error("Session mint failed: %s", error)
            raise SessionStoreError("session create failed") from error
    raise SessionStoreError("could not mint a unique session id")


def handle_create_session(event: dict, connection_id: str, body: dict) -> dict:
    """WS action `create-session`: hand the client a server-issued session id.

    #5615 (S16): this is the ONLY way a webchat conversation comes into
    existence. The client sends no identifier and cannot influence the one it
    gets back. Paired with the message path's refusal to create an unknown id
    (`_handle_unified_message`), that removes client choice from session
    addressing altogether.

    The reply is posted over the socket and correlated by `request_id`, because
    API Gateway discards a WebSocket integration's return body — the same reason
    the upload routes below do it. A client that never sees the reply must retry
    and use the NEW id; a lost reply orphans an empty row that TTL reaps, which
    is why the id is never reused.
    """
    request_id = body.get("request_id", "")

    def _respond(status: int, payload: dict) -> dict:
        _send_ws_response(connection_id, request_id, payload)
        return {"statusCode": status, "body": json.dumps(payload)}

    ident = _get_upload_claims(event, connection_id)
    if not ident:
        return _respond(401, {"error": "Missing identity claims"})

    try:
        session_id = _create_webchat_session(connection_id, ident)
    except SessionStoreError as error:
        # Includes an incomplete identity (no usable tenant). Refused, not
        # downgraded to a session outside the caller's own scope.
        logger.warning("Refused create-session for %s: %s", connection_id, error)
        return _respond(503, {"error": "could not start a conversation"})

    return _respond(200, {"session_id": session_id})


def handle_upload_token(event: dict, connection_id: str, body: dict) -> dict:
    """Return a presigned PUT URL scoped to the caller's identity path.
    Stage C (#186): POST /upload-token.

    Because this is invoked from a WebSocket route, API Gateway discards the
    Lambda's return value. The actual response is posted back to the client
    via post_to_connection and correlated via request_id.
    """
    request_id = body.get("request_id", "")

    def _respond(status: int, payload: dict) -> dict:
        _send_ws_response(connection_id, request_id, payload)
        return {"statusCode": status, "body": json.dumps(payload)}

    if not ARTIFACTS_BUCKET:
        return _respond(500, {"error": "Uploads not configured"})

    ident = _get_upload_claims(event, connection_id)
    if not ident:
        return _respond(401, {"error": "Missing identity claims"})

    session_id = body.get("session_id", "")
    task_id = body.get("task_id", str(uuid.uuid4())[:8])
    filename = body.get("filename", "")
    content_type = body.get("content_type", "application/octet-stream")
    size_bytes = body.get("size_bytes", 0)

    if not session_id or not isinstance(filename, str) or not filename:
        return _respond(400, {"error": "session_id and filename required"})

    # #5660 (A07): shape first — this value becomes an S3 path segment below.
    if not is_valid_session_id(session_id):
        logger.warning("Rejected upload-token request with malformed session id %r", session_id)
        return _respond(400, {"error": "invalid session id"})

    # The upload must land under the caller's own prefix, and only a complete
    # identity can express one. Without org+team the only expressible location is
    # the legacy flat `<session>/...`, which proves nothing about who owns it and
    # is exactly what makes existing rows unauthorizable — so refuse instead of
    # writing another one.
    if not ident["org_id"] or not ident["team_id"]:
        logger.warning(
            "Refusing upload token for user=%s: incomplete identity claims (org/team missing)",
            ident["user_id"],
        )
        return _respond(403, {"error": "Uploads require a complete tenant identity"})

    if not isinstance(size_bytes, (int, float)) or isinstance(size_bytes, bool):
        return _respond(400, {"error": "Invalid file size"})
    if size_bytes and size_bytes > UPLOAD_MAX_BYTES:
        return _respond(400, {"error": f"File too large (max {UPLOAD_MAX_BYTES} bytes)"})

    # Sanitize filename — allow only alphanums, dots, hyphens, underscores
    safe_filename = "".join(c for c in filename if c.isalnum() or c in ".-_")
    if not safe_filename:
        return _respond(400, {"error": "Invalid filename"})

    try:
        s3_key = _build_upload_s3_key(
            ident["org_id"], ident["team_id"], ident["user_id"],
            session_id, task_id, safe_filename,
        )
    except ValueError:
        logger.warning("Refusing upload token: unsafe key segment for session %r", session_id)
        return _respond(400, {"error": "Invalid upload parameters"})

    # #5615 (S16): the conversation must ALREADY EXIST and be owned by this
    # caller. This route used to CREATE the row it was asked about, which made it
    # a second way to name a conversation and walked straight around the
    # server-issued-only contract: a browser-chosen id refused by the message
    # path could be created here, and then accepted there. What that restores is
    # squatting — pre-creating the id a victim is about to be issued, locking
    # them out of their own new conversation. (Another user's id was refused
    # before this change and still is; this is a contract bypass, not disclosure.)
    #
    # Nothing legitimate is lost. The row now exists from the moment the
    # conversation is STARTED (`handle_create_session`), and the browser only
    # offers the drop zone once it has an acknowledged id — so attaching a file
    # before the first message, which is what the old create-on-reserve behaviour
    # existed for, still works.
    try:
        _assert_session_owned_by_caller(session_id, ident)
    except SessionOwnershipError:
        # Same answer for "never issued" and "somebody else's", so the route is
        # not an oracle for which ids exist.
        return _respond(404, {"error": "session not found"})

    try:
        upload_url = s3_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": ARTIFACTS_BUCKET,
                "Key": s3_key,
                "ContentType": content_type,
            },
            ExpiresIn=UPLOAD_TOKEN_EXPIRY,
        )
    except Exception as e:
        logger.error("Failed to generate presigned URL: %s", e)
        return _respond(500, {"error": "Failed to generate upload URL"})

    return _respond(200, {
        "upload_url": upload_url,
        "s3_key": s3_key,
        "task_id": task_id,
        "expires_in": UPLOAD_TOKEN_EXPIRY,
    })


def handle_upload_complete(event: dict, connection_id: str, body: dict) -> dict:
    """Record a completed upload in the DDB catalog. Idempotent via sha256.
    Stage C (#186): POST /upload-complete.

    Response is posted back over WS (see handle_upload_token note).
    """
    request_id = body.get("request_id", "")

    def _respond(status: int, payload: dict) -> dict:
        _send_ws_response(connection_id, request_id, payload)
        return {"statusCode": status, "body": json.dumps(payload)}

    if not ARTIFACTS_BUCKET or not ARTIFACTS_TABLE:
        return _respond(500, {"error": "Uploads not configured"})

    ident = _get_upload_claims(event, connection_id)
    if not ident:
        return _respond(401, {"error": "Missing identity claims"})

    session_id = body.get("session_id", "")
    task_id = body.get("task_id", "")
    filename = body.get("filename", "")
    content_type = body.get("content_type", "application/octet-stream")
    size_bytes = body.get("size_bytes", 0)
    checksum = body.get("checksum", "")

    # `s3_key` is NO LONGER read from the request (#5660 / A07). It used to be
    # written verbatim into the catalogue, so a caller could complete an upload
    # naming ANY key — pointing a row they own at another tenant's object and
    # then reading it back through the artifact fetch path. The server issued the
    # key in handle_upload_token and can derive the identical one here, so the
    # client's copy is redundant as well as forgeable.
    if (
        not session_id or not isinstance(task_id, str) or not task_id
        or not isinstance(filename, str) or not filename or not checksum
    ):
        return _respond(400, {"error": "session_id, task_id, filename, and checksum required"})

    if not is_valid_session_id(session_id):
        logger.warning("Rejected upload-complete with malformed session id %r", session_id)
        return _respond(400, {"error": "invalid session id"})

    if not ident["org_id"] or not ident["team_id"]:
        logger.warning(
            "Refusing upload completion for user=%s: incomplete identity claims",
            ident["user_id"],
        )
        return _respond(403, {"error": "Uploads require a complete tenant identity"})

    try:
        _assert_session_owned_by_caller(session_id, ident)
    except SessionOwnershipError:
        return _respond(404, {"error": "session not found"})

    # Re-derive, matching handle_upload_token exactly (same sanitisation, so a
    # filename that was rewritten when the token was issued resolves to the same
    # key here rather than to a nonexistent one).
    safe_filename = "".join(c for c in filename if c.isalnum() or c in ".-_")
    if not safe_filename:
        return _respond(400, {"error": "Invalid filename"})
    try:
        s3_key = _build_upload_s3_key(
            ident["org_id"], ident["team_id"], ident["user_id"],
            session_id, task_id, safe_filename,
        )
    except ValueError:
        logger.warning("Refusing upload completion: unsafe key segment for session %r", session_id)
        return _respond(400, {"error": "Invalid upload parameters"})

    artifacts_table = dynamodb.Table(ARTIFACTS_TABLE)

    # Idempotency is valid only for the exact server-derived upload. Legacy
    # rows in this client-selectable partition are untrusted even when their
    # checksum matches.
    try:
        existing = artifacts_table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            FilterExpression="checksum = :cs",
            ExpressionAttributeValues={
                ":pk": f"session#{session_id}",
                ":prefix": "art#",
                ":cs": checksum,
            },
        )
        matching_rows = existing.get("Items", [])
        verified_rows = [candidate for candidate in matching_rows if (
            candidate.get("s3Key") == s3_key
            and candidate.get("org_id") == ident["org_id"]
            and candidate.get("team_id") == ident["team_id"]
            and candidate.get("user_id") == ident["user_id"]
        )]
        unverified_count = len(matching_rows) - len(verified_rows)
        if unverified_count:
            logger.warning(
                "Ignoring %d unverified checksum-matching artifact rows for session=%s user=%s",
                unverified_count, session_id, ident["user_id"],
            )
        if verified_rows:
            return _respond(200, {"artifact_id": verified_rows[0]["id"], "deduplicated": True})
    except Exception as e:
        logger.warning("Dedup check failed (proceeding): %s", e)

    artifact_id = f"art_{uuid.uuid4().hex[:12]}"
    now = time.time()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now))
    ttl_epoch = int(now) + 30 * 86400

    try:
        artifacts_table.put_item(Item={
            "PK": f"session#{session_id}",
            "SK": f"art#{now_iso}#{artifact_id}",
            "id": artifact_id,
            "filename": filename,
            "contentType": content_type,
            "sizeBytes": size_bytes,
            "checksum": checksum,
            "s3Key": s3_key,
            "source": "user",
            "createdAt": now_iso,
            "ttl": ttl_epoch,
            "org_id": ident["org_id"],
            "team_id": ident["team_id"],
            "user_id": ident["user_id"],
        })
    except Exception as e:
        logger.error("Failed to write artifact catalog row: %s", e)
        return _respond(500, {"error": "Failed to record upload"})

    return _respond(200, {"artifact_id": artifact_id, "deduplicated": False})


def _validate_requested_persona(message: UnifiedMessage) -> str | None:
    """Return the validated pinned persona, or None if the client pinned none.

    Raises ValueError if the client pinned something we do not allow. Callers
    must reject the message on that path — never fall back to the classifier,
    or a bad pin becomes a silently-wrong conversation.
    """
    requested = message.platform_data.get("requested_persona", "")
    if not isinstance(requested, str) or not requested.strip():
        return None

    requested = requested.strip()
    if not PERSONA_NAME_PATTERN.match(requested):
        raise ValueError("malformed persona name")
    if requested not in PINNABLE_PERSONAS:
        raise ValueError("persona not pinnable")
    return requested


def handle_unified_message(message: UnifiedMessage) -> dict:
    if message.platform_data.get("ingress") != "gateway-api":
        return _handle_unified_message(message)
    try:
        _validate_requested_persona(message)
    except ValueError:
        return _handle_unified_message(message)

    # Claim the turn before touching the session, registering spend or enqueueing.
    # A repeated message_id alone is insufficient: the run table also keys on
    # arrival time and SQS deduplicates by a newly minted task id.
    identity = [message.platform_data.get("org_id"), message.user_id, message.thread_id, message.message_id]
    key = "gateway-turn#" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps([
        message.text, message.platform_data.get("intake_repository"),
        message.platform_data.get("intake_issue"), message.platform_data.get("requested_persona"),
    ]).encode()).hexdigest()
    try:
        sessions_table.put_item(
            Item={"session_id": key, "fingerprint": fingerprint, "expires_at": int(time.time()) + 7 * 86400},
            ConditionExpression="attribute_not_exists(session_id)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        prior = sessions_table.get_item(Key={"session_id": key}, ConsistentRead=True).get("Item", {})
        if prior.get("fingerprint") != fingerprint:
            return {"statusCode": 409, "body": json.dumps({"error": "retry_token_reused"})}
        if prior.get("result"):
            return json.loads(prior["result"])
        return {"statusCode": 409, "body": json.dumps({"error": "turn_delivery_uncertain", "session_id": message.thread_id})}

    result = _handle_unified_message(message)
    # If the process dies after dispatch, the claim remains. A retry then reports
    # uncertainty rather than sending a second task. Never turn an unknown result
    # into a fresh dispatch.
    sessions_table.update_item(
        Key={"session_id": key}, UpdateExpression="SET #result = :result",
        ExpressionAttributeNames={"#result": "result"},
        ExpressionAttributeValues={":result": json.dumps(result)},
    )
    return result


def _handle_unified_message(message: UnifiedMessage) -> dict:
    now = int(time.time())
    task_id = str(uuid.uuid4())
    connection_id = message.platform_data.get("connection_id", "")
    session_id = message.thread_id or message.session_key

    # Issue #4208: validate any client-pinned persona BEFORE touching the
    # session, the classifier, or the queue. A rejected pin must have no side
    # effects at all — no history row, no Bedrock spend, no SQS message.
    try:
        pinned_persona = _validate_requested_persona(message)
    except ValueError as e:
        logger.warning(
            "Rejected pinned persona %r for session %s: %s",
            message.platform_data.get("requested_persona"), session_id, e,
        )
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "invalid persona", "session_id": session_id}),
        }

    # #5660 (A07): the id is client-supplied and becomes both a table key and an
    # S3 path segment. Refuse a bad shape before any side effect, for the same
    # reason the persona pin is validated above.
    if not is_valid_session_id(session_id):
        logger.warning("Rejected malformed session id %r", session_id)
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "invalid session id"}),
        }

    # Ensure session exists — refuses if the id belongs to another owner.
    try:
        session = get_or_create_session(session_id, connection_id, message, now)
    except SessionOwnershipError:
        # Nothing has been written, no history is returned, and the owner's live
        # connection is untouched. Indistinguishable from "no such session".
        payload = {
            "error": "session not found", "session_id": session_id,
            # #5615: TELL the client, because a WebSocket integration discards
            # this return value and the browser would otherwise hang forever on
            # a message that will never be processed. The legitimate reason an
            # owner sees this is a conversation that outlived the sessions
            # table's 24h TTL: their id is genuinely gone, and they must start a
            # new one rather than retry the same id forever.
            #
            # `type` names the RECOVERY, not the reason — it is identical for a
            # reaped id and for someone else's, so this stays the same
            # non-answer as before and is not an existence oracle.
            "type": "session_invalid",
            "content": "That conversation is no longer available. Starting a new one.",
        }
        if connection_id:
            # No request_id: a chat turn is not a request/response pair, so this
            # is an unsolicited frame like the dispatch-failure one below.
            _send_ws_response(connection_id, "", payload)
        return {"statusCode": 404, "body": json.dumps(payload)}
    except SessionStoreError:
        return {
            "statusCode": 503,
            "body": json.dumps({"error": "session ownership unavailable"}),
        }
    threads = session.get("threads", {})

    # Load recent history and session-thread summaries for classifier.
    #
    # Every thread in this session — idle or processing — is a follow-up
    # candidate. Filtering to only-currently-processing threads broke
    # follow-ups entirely: as soon as turn-1 finished, processing_task_id was
    # cleared, the thread disappeared from the classifier's view, and every
    # subsequent message got thread_action=new. The user may resume a topic
    # seconds or hours later; we don't gate on elapsed time.
    #
    # Cap by most-recent N threads to keep the classifier prompt bounded.
    # 10 is plenty — a session with >10 distinct topics is rare and the
    # classifier is unlikely to confuse a 4-hour-old thread #11 with a new
    # message that genuinely refers to thread #1.
    history = load_recent_history(session_id, limit=10)
    sorted_threads = sorted(
        threads.items(),
        key=lambda kv: int(kv[1].get("created_at", 0) or 0),
        reverse=True,
    )[:10]
    active_threads = [
        {
            "thread_id": tid,
            "topic": t.get("topic", ""),
            "path": t.get("path", ""),
            "status": "processing" if t.get("processing_task_id") else "idle",
            "created_at": int(t.get("created_at", 0) or 0),
            "github_issue_url": t.get("github_issue_url", ""),
        }
        for tid, t in sorted_threads
    ]

    if pinned_persona:
        # Issue #4208: the client pinned the persona, so the classifier has
        # nothing left to decide and we skip it — it would add its own Bedrock
        # latency to a path that already has a 10-18s cold start, and its
        # prompt explicitly refuses to carry a persona across topics, which is
        # exactly what an intake conversation needs it to do.
        #
        # Always long_running: direct_response is a tool-less, 1-2 sentence
        # classifier reply, which cannot run the interviewer or call
        # update_draft. Continue the most recent thread so a multi-turn intake
        # serializes as one conversation instead of forking a thread per turn.
        classification = ClassificationResult(
            path="long_running",
            persona=pinned_persona,
            reasoning=f"persona pinned by client: {pinned_persona}",
        )
        if message.platform_data.get("ingress") == "gateway-api":
            classification.repo = session.get("intake_repository") or message.platform_data.get("intake_repository") or None
        if active_threads:
            classification.thread_action = "follow_up"
            classification.follow_up_thread_id = active_threads[0]["thread_id"]
    else:
        # Classify with thread awareness
        classification = classify_message(
            message=message.text,
            conversation_history=history,
            active_threads=active_threads,
            channel=message.channel.value,
            user_name=message.user_name,
        )

    logger.info("Route: path=%s thread_action=%s persona=%s pinned=%s", classification.path, classification.thread_action, classification.persona, bool(pinned_persona))

    # Append message to session history (always, for all paths)
    append_message(session_id, "user", message.text, now)

    # --- Route ---

    if classification.path == "direct_response" and classification.response:
        return handle_direct_response(session_id, task_id, connection_id, message, classification, now)

    session_generation = int(session.get("created_at", 0) or 0)
    if not session_generation:
        logger.warning("OWNERSHIP REFUSED session=%s: missing session generation", session_id)
        return {
            "statusCode": 404,
            "body": json.dumps({"error": "session not found", "session_id": session_id}),
        }

    if classification.path == "github_actions":
        return handle_github_dispatch(
            session_id, task_id, connection_id, message, classification, threads,
            now, session_generation,
        )

    # long_running
    return handle_long_running(
        session_id, task_id, connection_id, message, classification, threads,
        now, session_generation,
    )


# ─── Path Handlers ────────────────────────────────────────────

def handle_direct_response(session_id, task_id, connection_id, message, classification, now):
    append_message(session_id, "assistant", classification.response, now)
    if RESPONSE_QUEUE_URL:
        _send_response(session_id, {
            "task_id": task_id, "session_id": session_id, "connection_id": connection_id,
            "channel": message.channel.value, "channel_metadata": message.platform_data,
            "result": classification.response, "status": "completed", "completed_at": now,
        }, dedup_id=f"resp_{task_id}")
    return {"statusCode": 200, "body": json.dumps({"task_id": task_id, "session_id": session_id, "status": "completed"})}


# ─── Issue #4233: chat-dispatch tenant gate ───────────────────

# Values of org_id that name no tenant. The JWT path defaults org_id to
# "default" when Cognito carries no custom:org_id claim, and adapters emit ""
# when the claim is absent entirely — neither can authorize a dispatch at a
# repo. NOTE: this gate reads `org_id`, never `tenant_id`. tenant_id is always
# "" on the github_actions path, so gating on it would be a silent no-op.
#
# Issue #5268: now defined once in channels.base, which the adapters can import
# (they cannot import this module — circular). The webchat adapter needs the
# same notion of "usable org" to decide whether to substitute the org as the
# tenant; two copies would drift, which is the bug class #5268 and #5264 both
# came from. Aliased rather than renamed so the existing gate reads unchanged.
_UNUSABLE_ORG_IDS = UNUSABLE_ORG_IDS


def _tenant_gate_denial(message: UnifiedMessage, repo_owner: str) -> str | None:
    """Why this chat message may not dispatch at `repo_owner` — None to allow.

    Layered, cheapest first (Issue #4233):

      1. **org_id fail-closed.** An absent or placeholder org_id identifies no
         tenant, so it cannot authorize a dispatch. This path used to log and
         allow, which is what made cross-tenant targeting reachable.
      2. **Org allowlist** (code-only, effective on merge). The ingest App is
         single-org by construction — Terraform sets
         `GH_APP_SECRET_PREFIX = "adp/<github_org>/gh-app-ops"` — so a target
         owner outside that org is never legitimate.
      3. **Ownership** (needs IDENTITY_INDEX_TABLE; inert until the manual
         `agent-factory-infra-apply.yml` adds the env + IAM). The caller's org
         must resolve, via the identity index, to the SAME App installation
         that covers the target owner. This asserts ownership rather than label
         equality, so an org_id that merely happens to spell the org login does
         not pass by coincidence.

    Fail-closed throughout: anything we cannot positively verify is a denial.
    """
    from github_dispatch import configured_github_org, installation_id_for_org

    org_id = str(message.platform_data.get("org_id", "") or "").strip()
    if org_id.lower() in _UNUSABLE_ORG_IDS:
        return "caller has no usable org_id"

    configured_org = configured_github_org()
    if not configured_org:
        return "the ingest Lambda's GitHub org is not configured"
    if repo_owner.strip().lower() != configured_org:
        return f"target owner {repo_owner!r} is outside org {configured_org!r}"

    # Layer 3 — only once the identity-index env has been applied. Until then
    # the org allowlist above is the whole gate, by design.
    identity_index_table = os.environ.get("IDENTITY_INDEX_TABLE", "")
    if not identity_index_table:
        logger.info(
            "Dispatch ownership layer inactive (IDENTITY_INDEX_TABLE unset); "
            "org allowlist allowed org_id=%s → %s",
            org_id, repo_owner,
        )
        return None

    try:
        from installation_resolver import resolve_installation_for_tenant

        caller_installation = resolve_installation_for_tenant(org_id)
        if caller_installation is None:
            return f"org_id {org_id!r} resolves to no GitHub App installation"
        owner_installation = installation_id_for_org(repo_owner)
        if owner_installation is None:
            return f"target owner {repo_owner!r} resolves to no GitHub App installation"
        if caller_installation != owner_installation:
            return (
                f"org_id {org_id!r} owns installation {caller_installation}, "
                f"but {repo_owner!r} belongs to installation {owner_installation}"
            )
    except Exception as e:
        # An unverifiable ownership claim is exactly what this gate exists to
        # stop — never degrade to "allow" on error.
        logger.warning(
            "Dispatch ownership check failed for org_id=%s owner=%s: %s — failing closed",
            org_id, repo_owner, e,
        )
        return "dispatch ownership could not be verified"

    return None


def handle_github_dispatch(session_id, task_id, connection_id, message, classification, threads,
                           now, session_generation):
    """Always dispatch — github_actions tasks are independent, never blocked."""
    repo_parts = (classification.repo or "").split("/", 1)
    repo_owner = repo_parts[0] if len(repo_parts) > 1 else ""
    repo_name = repo_parts[1] if len(repo_parts) > 1 else repo_parts[0] if repo_parts else ""

    # Issue #4233: no chat message may steer a dispatch at a repo outside the
    # caller's org. Runs before any GitHub call so a denied request creates no
    # issue, posts no comment, and leaves no thread record.
    #
    # Only reached when the classifier named an owner. A repo with no owner
    # ("myrepo") cannot dispatch anywhere — no installation resolves for an
    # empty owner — so it keeps falling through to long_running below rather
    # than being rejected outright, which would break a legitimate in-org ask.
    if repo_owner:
        denial = _tenant_gate_denial(message, repo_owner)
        if denial:
            logger.warning(
                "Rejected github_actions dispatch: %s (session=%s repo=%s)",
                denial, session_id, classification.repo,
            )
            send_notification(
                session_id, task_id, connection_id, message,
                "🚫 I can't act on that repository — it's outside your organization. "
                "Name a repository your organization owns and I'll pick it up.",
                now,
            )
            return {
                "statusCode": 403,
                "body": json.dumps({
                    "task_id": task_id,
                    "session_id": session_id,
                    "status": "rejected_cross_tenant",
                }),
            }

    # Follow-up to existing github thread → post comment on the issue
    if classification.thread_action == "follow_up" and classification.follow_up_thread_id:
        thread = threads.get(classification.follow_up_thread_id, {})
        issue_url = thread.get("github_issue_url", "")
        if issue_url and thread.get("github_issue_number"):
            from github_dispatch import _get_installation_token, _post_comment
            token = _get_installation_token(repo_owner)
            if token:
                _post_comment(token, repo_owner, repo_name, thread["github_issue_number"],
                              f"**Follow-up from {message.channel.value}** ({message.user_name}):\n\n{message.text}")
            notify = f"📝 Added your follow-up to {issue_url}"
            send_notification(session_id, task_id, connection_id, message, notify, now)
            return {"statusCode": 200, "body": json.dumps({"task_id": task_id, "session_id": session_id, "status": "comment_added", "issue_url": issue_url})}

    # New github task → create issue
    thread_id = str(uuid.uuid4())[:8]

    if classification.issue_number and not classification.create_issue:
        result = label_existing_issue(repo_owner, repo_name, classification.issue_number, classification.persona, classification.enriched_message or "")
    else:
        title = classification.issue_title or f"[{classification.persona}] {message.text[:80]}"
        body = classification.enriched_message or message.text
        result = create_issue_and_dispatch(repo_owner, repo_name, title, body, classification.persona, session_id, message.channel.value, message.user_name)

    if result.get("dispatched"):
        issue_url = result.get("issue_url", "")
        issue_number = result.get("issue_number", 0)

        # Create thread record
        create_thread(session_id, thread_id, task_id, classification, issue_number, issue_url)

        escalation = f"🔧 I've escalated this to a code task.\n📋 Tracking: {issue_url}\nThe @agent-{classification.persona} is working on it."
        if classification.escalation_note:
            escalation = f"{classification.escalation_note}\n📋 {issue_url}"
        send_notification(session_id, task_id, connection_id, message, escalation, now)

        return {"statusCode": 200, "body": json.dumps({"task_id": task_id, "session_id": session_id, "status": "dispatched_github", "issue_url": issue_url, "thread_id": thread_id})}

    # Fallback to long_running
    return handle_long_running(
        session_id, task_id, connection_id, message, classification, threads,
        now, session_generation,
    )


def handle_long_running(session_id, task_id, connection_id, message, classification, threads,
                        now, session_generation):
    """Per-thread serialization for long_running tasks."""

    is_follow_up = (
        classification.thread_action == "follow_up"
        and classification.follow_up_thread_id
        and classification.follow_up_thread_id in threads
    )

    if is_follow_up:
        thread_id = classification.follow_up_thread_id
        thread = threads.get(thread_id, {})

        if thread.get("processing_task_id"):
            # Thread is busy — buffer message, it'll be picked up on completion
            append_thread_message(session_id, thread_id, "user", message.text, now)
            notify = classification.escalation_note or "Your message has been queued. I'll address it once the current task completes."
            send_notification(session_id, task_id, connection_id, message, notify, now)
            return {"statusCode": 200, "body": json.dumps({"task_id": None, "session_id": session_id, "thread_id": thread_id, "status": "queued"})}

        # Idle follow-up: reuse the existing thread. Don't call create_thread
        # (it clobbers messages/topic). Just mark it as processing the new task.
        set_thread_processing(session_id, thread_id, task_id)
    else:
        # New thread
        thread_id = str(uuid.uuid4())[:8]
        create_thread(session_id, thread_id, task_id, classification)
        set_thread_processing(session_id, thread_id, task_id)

    # FIFO queues require MessageGroupId + MessageDeduplicationId. Group by
    # session_id so per-session turns serialize; different sessions stay
    # parallel. Dedup by task_id makes re-deliveries idempotent.
    # Stage A (#184): include extended identity claims in SQS payload so the
    # worker can log the full TokenContext and enforce team-aware ownership.
    # All new fields are optional — existing workers ignore unknown keys.
    pd = message.platform_data
    identity_fields: dict[str, Any] = {}
    for key in ("tenant_id", "org_id", "team_id", "department_id", "account_type", "role"):
        val = pd.get(key, "")
        if val:
            identity_fields[key] = val

    # Issue #1289: propagate cognito_sub for personal-context identity.
    # user_id IS the Cognito sub for webchat (extracted from JWT on $connect).
    # We include it explicitly as cognito_sub so the worker can set
    # X-Owner-Sub on Context MCP requests from trusted dispatch metadata.
    if message.user_id:
        identity_fields["cognito_sub"] = message.user_id

    # Carry the immutable, tenant-qualified owner authorized at enqueue time.
    # Workers echo this opaque value on every response envelope; response routing
    # and persistence never reconstruct it from client-controlled metadata.
    identity_fields["owner_principal"] = _session_owner_principal(
        pd.get("tenant_id", "") or pd.get("org_id", ""), pd.get("org_id", ""),
        pd.get("team_id", ""), message.user_id, message.channel.value,
    )
    identity_fields["session_generation"] = session_generation

    # Stage C (#186): forward artifact ID attachments to the worker so it can
    # inject them into the system prompt. The frontend sends string IDs
    # (e.g. ["art_abc123"]) in platform_data; MediaAttachment objects from
    # the adapter are ignored here (they use a different upload path).
    attachment_ids = message.platform_data.get("attachment_ids", [])

    # Phase 4 (#1458): Include message_id and arrived_at in the SQS envelope
    # so the worker can advance invocation status using the same key contract
    # as Phase 1 (event_id=message_id, arrived_at=ISO timestamp).
    arrived_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    sqs_body: dict[str, Any] = {
        "task_id": task_id, "session_id": session_id, "thread_id": thread_id,
        "connection_id": connection_id, "channel": message.channel.value,
        "mode": "chat", "agent_type": classification.persona,
        "user_id": message.user_id,
        "repo_owner": (classification.repo or "").split("/")[0] if classification.repo and "/" in classification.repo else "",
        "repo_name": (classification.repo or "").split("/")[1] if classification.repo and "/" in classification.repo else "",
        "message": message.text, "platform_data": message.platform_data, "enqueued_at": now,
        "message_id": message.message_id, "arrived_at": arrived_at,
        **identity_fields,
    }
    if attachment_ids:
        sqs_body["attachments"] = attachment_ids

    send_kwargs = dict(
        QueueUrl=INPUT_QUEUE_URL,
        MessageBody=json.dumps(sqs_body),
    )
    if INPUT_QUEUE_URL.endswith(".fifo"):
        send_kwargs["MessageGroupId"] = session_id
        send_kwargs["MessageDeduplicationId"] = task_id
    # This row authorizes the worker to inherit its human owner's Bedrock
    # destination. Persist it BEFORE dispatch; capture failure must not enqueue
    # a job that would otherwise spend using the shared worker's account.
    try:
        if not message.user_id or not pd.get("tenant_id"):
            raise ValueError("Missing chat run owner or tenant")
        registered = log_invocation(
            WEBHOOK_EVENTS_TABLE,
            event_id=message.message_id,
            arrived_at=arrived_at,
            user_id=message.user_id or "unattributed",
            channel=message.channel.value,
            topic=message.text[:120] or "(untitled)",
            persona=classification.persona,
            status="webhook_received",
            tenant_id=pd.get("tenant_id", ""),
            region=REGION,
            account_type=pd.get("account_type") or "human",
        )
        if registered is None:
            raise RuntimeError("Chat run registration unavailable")
        if os.environ.get("ADP_CHAT_MODEL_POLICY_ENABLED", "false").lower() == "true":
            from model_root_client import register_model_root

            send_kwargs["MessageBody"] = register_model_root(sqs_body, source="chat", subject=message.user_id)
        elif os.environ.get("PERSONA_MODEL_MAPPING_ENABLED", "false").lower() == "true":
            from persona_model_client import select_persona_model

            send_kwargs["MessageBody"] = json.dumps(select_persona_model(sqs_body, user_id=message.user_id))
        sqs.send_message(**send_kwargs)
    except Exception:
        set_thread_processing(session_id, thread_id, None)
        logger.exception("Chat dispatch failed before acknowledgement")
        failure = {"type": "response", "status": "failed", "session_id": session_id,
                   "error": "Could not start this persona. Please retry.",
                   "content": "Could not start this persona. Please retry."}
        if connection_id:
            # WebSocket integrations discard HTTP response bodies. Show a
            # refused selection immediately instead of leaving the UI spinning.
            _send_ws_response(connection_id, "", failure)
        return {"statusCode": 503, "body": json.dumps(failure)}

    # Always send an acknowledgement. The classifier prompt asks for
    # escalation_note on non-direct paths, but LLMs occasionally omit it —
    # fall back so the user never stares at a silent "sent" message while the
    # long_running agent spins up.
    notify = classification.escalation_note or "On it — working on this now. I'll reply here when it's ready."
    send_notification(session_id, task_id, connection_id, message, notify, now)

    return {"statusCode": 200, "body": json.dumps({"task_id": task_id, "session_id": session_id, "thread_id": thread_id, "status": "processing"})}


# ─── Helpers ──────────────────────────────────────────────────

def send_notification(session_id, task_id, connection_id, message, text, now):
    append_message(session_id, "assistant", text, now)
    if RESPONSE_QUEUE_URL:
        _send_response(session_id, {
            "task_id": task_id, "session_id": session_id, "connection_id": connection_id,
            "channel": message.channel.value, "channel_metadata": message.platform_data,
            "result": text, "status": "notification", "completed_at": now,
        }, dedup_id=f"notif_{task_id}")


def _send_response(session_id: str, body: dict, *, dedup_id: str) -> None:
    """Send a response payload to the response queue.

    Auto-wires FIFO params (MessageGroupId = session_id, MessageDeduplicationId
    = caller-supplied) when the queue URL ends with `.fifo`. FIFO preserves
    per-session ordering so AG-UI events reach the response Lambda in the
    order the worker emitted them.
    """
    kwargs = {"QueueUrl": RESPONSE_QUEUE_URL, "MessageBody": json.dumps(body)}
    if RESPONSE_QUEUE_URL.endswith(".fifo"):
        kwargs["MessageGroupId"] = session_id
        kwargs["MessageDeduplicationId"] = dedup_id
    sqs.send_message(**kwargs)


def detect_channel(event):
    # ORDER IS THE SECURITY PROPERTY HERE (#5331).
    #
    # The gateway-api envelope carries its own `user_id`/`org_id`, because its
    # transport is `lambda:InvokeFunction` and the IAM grant is what authenticates
    # it. That makes it the one envelope a WebSocket client must never be able to
    # reach: a browser that sent `{"source": "gateway-api", "user_id": "someone-
    # else"}` would otherwise be handed another person's identity.
    #
    # The `connectionId` check therefore stays FIRST and unconditional. Every API
    # Gateway WebSocket event carries one, so such a client is still a webchat
    # event and their identity still comes from their own $connect claims. The
    # gateway-api check is placed after the Slack signature check for the same
    # reason in the other direction: a signed Slack event is Slack's regardless of
    # what its body says.
    if event.get("requestContext", {}).get("connectionId"):
        return "webchat", ADAPTERS["webchat"]
    headers = event.get("headers", {})
    if headers.get("x-slack-signature") or headers.get("X-Slack-Signature"):
        return "slack", ADAPTERS["slack"]
    # A direct Lambda invocation: no requestContext, no headers, no HTTP at all.
    # Matched on an explicit discriminator rather than on the absence of the
    # markers above, because "no connectionId and no signature" is also true of a
    # malformed webchat event — and treating malformed-webchat as trusted-gateway
    # would be an identity bypass rather than a parsing quirk.
    if event.get("source") == GATEWAY_API_SOURCE:
        return GATEWAY_API_SOURCE, ADAPTERS[GATEWAY_API_SOURCE]
    body = parse_body(event)
    if body.get("type") in ("url_verification", "event_callback"):
        return "slack", ADAPTERS["slack"]
    return "webchat", ADAPTERS["webchat"]


def parse_body(event):
    body = event.get("body", {})
    if isinstance(body, str):
        try: return json.loads(body)
        except: return {}
    return body if isinstance(body, dict) else {}


# ─── Session & Thread DynamoDB Operations ─────────────────────

def _client_may_name_a_new_session(message) -> bool:
    """True when this session id legitimately originates outside the store.

    #5615 (S16): for an id the BROWSER chose, the answer is now no. Browser ids
    are issued by `handle_create_session`, so an id the store has never seen was
    never issued — and a request naming one is not authorization to create it.
    Without this, an attacker could pre-create an id a victim's browser was about
    to choose (they were derived from the clock) and lock the victim out of their
    own new conversation.

    The refusal is scoped to a CLIENT-SUPPLIED id — `message.thread_id`, which
    the webchat adapter carries through from the untrusted body — rather than to
    the channel, because three id sources reach this function and only that one
    is client-chosen:

      - No `session_id` in the body at all. The caller then falls back to
        `message.session_key`, derived from the verified sub and the connection
        (`channels/base.py`). Nothing client-chosen is in it, and it cannot name
        another user's conversation, so first contact must still create it.
      - `gateway-api` (the CLI / operator plane) already mints its ids
        SERVER-side in `gateway/src/orchestration/intake_dispatch.py`
        (`new_session_id`) and reaches this Lambda through an IAM-gated direct
        invocation. It is identified by its `ingress` provenance marker rather
        than its channel, because it deliberately emits ChannelType.WEBCHAT so
        its rows stay indistinguishable from browser-started ones (see
        `channels/gateway_api.py`).
      - Slack's session id IS the thread timestamp Slack assigns
        (`channels/slack.py`), so first contact on a thread must still create
        the row or Slack chat stops working entirely.

    Note what this does NOT rely on: an id being unguessable. Ownership is still
    enforced on every path by `_assert_session_item_owner`, and guessing an
    existing id still yields a bare "not found". Randomness supplements that
    check; it does not replace it.
    """
    if message.platform_data.get("ingress") == GATEWAY_API_SOURCE:
        return True
    if message.channel != ChannelType.WEBCHAT:
        return True
    # Only an id the browser put on the wire is refused; the server-derived
    # fallback above is not a client's choice.
    return not message.thread_id


def get_or_create_session(session_id, connection_id, message, now):
    org_id = str(message.platform_data.get("org_id", "") or "")
    tenant_id = str(message.platform_data.get("tenant_id", "") or org_id)
    team_id = str(message.platform_data.get("team_id", "") or "")
    caller_workspace = f"{message.user_id}#{message.channel.value}"
    expected_principal = _session_owner_principal(
        tenant_id, org_id, team_id, message.user_id, message.channel.value,
    )

    def verify_and_rebind(item: dict | None) -> dict:
        owned = _assert_session_item_owner(item, expected_principal, session_id)
        try:
            sessions_table.update_item(
                Key={"session_id": session_id},
                UpdateExpression="SET connection_id = :c, updated_at = :t, expires_at = :e",
                ConditionExpression="owner_principal = :owner",
                ExpressionAttributeValues={
                    ":c": connection_id, ":t": now, ":e": now + 86400,
                    ":owner": expected_principal,
                },
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                logger.warning(
                    "OWNERSHIP REFUSED session=%s: owner changed during rebind",
                    session_id,
                )
                raise SessionOwnershipError(session_id) from error
            raise SessionStoreError("session rebind failed") from error
        return owned

    try:
        resp = sessions_table.get_item(Key={"session_id": session_id}, ConsistentRead=True)
    except Exception as error:
        logger.error("Session ownership lookup failed for %s: %s", session_id, error)
        raise SessionStoreError("session lookup failed") from error

    if resp.get("Item"):
        return verify_and_rebind(resp["Item"])

    # #5615 (S16): an unknown id on the browser path was never issued by us.
    # Refuse with the SAME error the ownership check raises, so the response
    # still cannot distinguish "never existed" from "someone else's" — keeping
    # this endpoint from becoming an oracle for enumerating session ids.
    if not _client_may_name_a_new_session(message):
        logger.warning(
            "OWNERSHIP REFUSED session=%s: unknown id on a channel with server-issued ids",
            session_id,
        )
        raise SessionOwnershipError(session_id)

    item = {
        "session_id": session_id,
        "owner_principal": expected_principal,
        "owner_user_id": message.user_id,
        "user_workspace": caller_workspace,
        "tenant_id": tenant_id,
        "connection_id": connection_id, "channel": message.channel.value,
        "messages": [], "threads": {}, "created_at": now, "updated_at": now, "expires_at": now + 86400,
    }
    if team_id:
        item["team_id"] = team_id
    if org_id:
        item["org_id"] = org_id
    if message.platform_data.get("ingress") == "gateway-api":
        item["intake_repository"] = str(message.platform_data.get("intake_repository") or "")
        item["intake_issue"] = str(message.platform_data.get("intake_issue") or "")
    try:
        sessions_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(session_id)",
        )
        return item
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise SessionStoreError("session create failed") from error
    except Exception as error:
        raise SessionStoreError("session create failed") from error

    # Another caller won the first-create race. Re-read the winner and apply the
    # same full owner check as the ordinary existing-row path.
    try:
        winner = sessions_table.get_item(
            Key={"session_id": session_id}, ConsistentRead=True,
        ).get("Item")
    except Exception as error:
        raise SessionStoreError("session collision lookup failed") from error
    return verify_and_rebind(winner)


def append_message(session_id, role, content, ts):
    # Guard: reject empty / whitespace-only content
    if not content or not content.strip():
        logger.info("append_message: skipping empty content for session=%s role=%s", session_id, role)
        return

    # Dedupe: don't append if the last message is identical (same role + content
    # within a 5-second window).  Prevents the double-ack that happens when
    # both handle_direct_response and send_notification fire for the same
    # classification.
    try:
        resp = sessions_table.get_item(
            Key={"session_id": session_id},
            ProjectionExpression="messages",
        )
        messages = resp.get("Item", {}).get("messages", [])
        if messages:
            last = messages[-1]
            last_ts = float(last.get("timestamp", 0))
            if (
                last.get("role") == role
                and last.get("content") == content[:10000]
                and abs(ts - last_ts) < 5
            ):
                logger.info("append_message: deduped identical %s message for session=%s", role, session_id)
                return
    except Exception as e:
        logger.debug("append_message dedupe check failed (proceeding): %s", e)

    try:
        sessions_table.update_item(Key={"session_id": session_id},
            UpdateExpression="SET messages = list_append(if_not_exists(messages, :e), :m), updated_at = :t",
            ExpressionAttributeValues={":m": [{"role": role, "content": content[:10000], "timestamp": Decimal(str(ts))}], ":e": [], ":t": ts})
    except Exception as e:
        logger.warning("append_message failed: %s", e)


def create_thread(session_id, thread_id, task_id, classification, issue_number=None, issue_url=None):
    thread_data = {
        "topic": classification.issue_title or classification.reasoning[:100],
        "path": classification.path,
        "persona": classification.persona,
        "processing_task_id": task_id,
        "messages": [],
        "created_at": int(time.time()),
    }
    if issue_number:
        thread_data["github_issue_number"] = issue_number
    if issue_url:
        thread_data["github_issue_url"] = issue_url

    try:
        sessions_table.update_item(Key={"session_id": session_id},
            UpdateExpression="SET threads.#tid = :td",
            ExpressionAttributeNames={"#tid": thread_id},
            ExpressionAttributeValues={":td": thread_data})
    except Exception as e:
        logger.warning("create_thread failed: %s", e)


def set_thread_processing(session_id, thread_id, task_id):
    try:
        sessions_table.update_item(Key={"session_id": session_id},
            UpdateExpression="SET threads.#tid.processing_task_id = :t",
            ExpressionAttributeNames={"#tid": thread_id},
            ExpressionAttributeValues={":t": task_id})
    except Exception as e:
        logger.warning("set_thread_processing failed: %s", e)


def append_thread_message(session_id, thread_id, role, content, ts):
    try:
        sessions_table.update_item(Key={"session_id": session_id},
            UpdateExpression="SET threads.#tid.messages = list_append(if_not_exists(threads.#tid.messages, :e), :m)",
            ExpressionAttributeNames={"#tid": thread_id},
            ExpressionAttributeValues={":m": [{"role": role, "content": content[:10000], "timestamp": Decimal(str(ts))}], ":e": []})
    except Exception as e:
        logger.warning("append_thread_message failed: %s", e)


def load_recent_history(session_id, limit=10):
    try:
        resp = sessions_table.get_item(Key={"session_id": session_id}, ProjectionExpression="messages")
        msgs = resp.get("Item", {}).get("messages", [])
        return [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in msgs[-limit:]]
    except: return []
