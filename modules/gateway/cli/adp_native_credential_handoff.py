#!/usr/bin/env python3
"""Bounded, nonsecret Unix IPC for a human-selected native credential host.

Local identity hashes partition routing only. The coordinator authenticates its
own native JWT with the issuer; no MCP-supplied field grants server authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import struct
import time
import urllib.parse
import uuid
from pathlib import Path

import adp_common as common

VERSION = 1
MAX_PACKET = 16384
PRIVATE_TOOLS = frozenset({"credentials___assume_role", "credentials___materialize"})
STATES = frozenset({"pending", "created", "awaiting_approval", "approved", "challenged", "unknown", "delivered", "denied", "expired", "acknowledged"})
TERMINAL_CODES = frozenset(
    {
        "child_exited",
        "child_start_failed",
        "host_cancelled",
        "host_failed",
        "host_disconnected",
        "delivery_rejected",
        "local_lifetime_ended",
        "delivery_acknowledgement_lost",
    }
)
CODES = TERMINAL_CODES | frozenset(
    {
        "native_private_host_required",
        "native_host_unavailable",
        "native_profile_unavailable",
        "native_host_busy",
        "native_request_pending",
        "native_delivery_not_confirmed",
        "native_delivery_unavailable",
        "native_host_changed",
        "native_request_conflict",
        "native_arguments_invalid",
        "native_private_artifact_unavailable",
        "native_cancel_requested",
        "existing_request_requires_status_read",
        "local_approval_window_ended",
        "authentication_required",
        "permission_denied",
        "native_delivery_conflict",
        "native_delivery_expired",
        "unexpected_redirect",
    }
)
IDS = ("request_id", "execution_id", "receipt_id")


class UnavailableError(Exception):
    def __init__(self, code="native_private_host_required"):
        self.code = code if code in CODES else "native_host_unavailable"
        super().__init__(self.code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError
    return value


def hexadecimal(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def native_only():
    if any(
        key in os.environ
        for key in (
            "ADP_NATIVE_TEST_TRANSPORT_FILE",
            "ADP_CREW_TOOLS_URL",
            "ADP_CREW_TOOLS_TOKEN",
            "ADP_CREW_RUN_ID",
            "SIGV4_PROXY_TARGET",
            "SIGV4_PROXY_PORT",
            "ADP_RUN_CREDENTIAL",
            "ADP_RUN_CREDENTIAL_FILE",
        )
    ):
        raise UnavailableError()


def binding_for(base, token):
    # Decode only for local routing, never as an authorization decision. A fresh
    # successful /config call is required independently inside the coordinator.
    native_only()
    claims = common._jwt_claims(token)
    if (
        not base.startswith("https://")
        or token.startswith("adpctx1~")
        or claims.get("token_use") != "access"
        or type(claims.get("exp")) is not int
        or claims["exp"] <= time.time()
        or any(not isinstance(claims.get(key), str) or not claims[key] for key in ("iss", "sub", "client_id", "jti"))
    ):
        raise UnavailableError("authentication_required")
    resolved = common.deployment()
    return digest(
        {
            "gateway": base,
            "deployment_id": resolved.id if resolved else "",
            "issuer": claims["iss"],
            "subject": claims["sub"],
            "client_id": claims["client_id"],
        }
    )


def current_binding():
    try:
        native_only()
        base = common.gateway_url()
        return binding_for(base, common.access_token())
    except (common.CliError, OSError, ValueError, KeyError, TypeError):
        raise UnavailableError("authentication_required") from None


def socket_path(binding):
    if not hexadecimal(binding):
        raise ValueError
    root = common.private_directory(Path("/dev/shm") / ("adp-native-mcp-" + str(os.getuid())))
    directory = common.private_directory(root / binding[:24])
    return directory / "host.sock"


def check_socket(path):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise UnavailableError()
    return info.st_dev, info.st_ino


def check_peer(channel):
    _, uid, _ = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    if uid != os.getuid():
        raise UnavailableError()


def receive(channel):
    packet, _, flags, _ = channel.recvmsg(MAX_PACKET)
    if not packet or flags & socket.MSG_TRUNC:
        raise ValueError

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    result = json.loads(packet, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if not isinstance(result, dict):
        raise ValueError
    return result


def send(channel, value):
    packet = canonical(value)
    if len(packet) > MAX_PACKET or channel.send(packet) != len(packet):
        raise ValueError


def validate_arguments(tool, arguments):
    allowed = {"service", "label"} | ({"purpose"} if tool == "credentials___assume_role" else set())
    if (
        tool not in PRIVATE_TOOLS
        or not isinstance(arguments, dict)
        or arguments.keys() - allowed
        or (tool == "credentials___materialize" and "service" not in arguments)
    ):
        raise UnavailableError("native_arguments_invalid")
    for key, value in arguments.items():
        if not isinstance(value, str) or len(value) > (512 if key == "purpose" else 128) or "\0" in value or (key != "purpose" and not value):
            raise UnavailableError("native_arguments_invalid")


def project_result(value, request_id=None):
    """Reject unknown fields rather than ever reflecting a private response."""
    if not isinstance(value, dict):
        raise ValueError
    if "state" in value:
        if value == {"state": "connected"}:
            pass
        elif value.get("state") == "ready" and set(value) == {"state", "tools"}:
            if not isinstance(value["tools"], list) or not value["tools"] or any(tool not in PRIVATE_TOOLS for tool in value["tools"]):
                raise ValueError
        elif value.get("state") == "unavailable" and set(value) == {"state", "code"} and value.get("code") in CODES:
            pass
        else:
            raise ValueError
        return dict(value)
    allowed = {
        *IDS,
        "status",
        "expires_at",
        "snapshot",
        "delivery_state",
        "code",
        "cleanup",
        "exit_code",
        "acknowledgement",
        "acknowledgment",
        "acknowledgment_source",
        "approval_url",
    }
    if set(value) - allowed or value.get("request_id") != request_id:
        raise ValueError
    for key in IDS:
        if key in value:
            identifier(value[key])
    validators = {
        "status": lambda v: isinstance(v, str) and v in STATES,
        "expires_at": lambda v: type(v) is int,
        "snapshot": lambda v: v == "" or hexadecimal(v),
        "delivery_state": lambda v: isinstance(v, str) and v in {"completed", "unknown", "failed", "cancelled", "expired", "unavailable"},
        "code": lambda v: isinstance(v, str) and v in CODES,
        "cleanup": lambda v: isinstance(v, str) and v in {"complete", "unknown"},
        "exit_code": lambda v: type(v) is int and -255 <= v <= 255,
        "acknowledgement": lambda v: isinstance(v, str) and v in {"confirmed", "unknown"},
    }
    if any(not valid(value[key]) for key, valid in validators.items() if key in value):
        raise ValueError
    if ("acknowledgment" in value) != ("acknowledgment_source" in value):
        raise ValueError
    if "acknowledgment" in value:
        ack = value["acknowledgment"]
        required = {*IDS, "type", "delivery_state", "code", "cleanup"}
        if (
            value["acknowledgment_source"] != "client_reported"
            or not isinstance(ack, dict)
            or not required <= ack.keys() <= required | {"exit_code"}
            or ack["type"] != "result"
            or any(ack[key] != value.get(key) for key in IDS)
            or ack["code"] not in TERMINAL_CODES
            or ack["delivery_state"] not in {"completed", "unknown", "failed", "cancelled", "expired"}
        ):
            raise ValueError
        project_result({k: v for k, v in ack.items() if k != "type"}, request_id)
    if "approval_url" in value:
        url = urllib.parse.urlsplit(value["approval_url"])
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path != "/credentials/native/" + value.get("receipt_id", "")
            or "receipt_id" not in value
        ):
            raise ValueError
    return dict(value)


def rpc(binding, action, *, instance=None, request_id=None, tool=None, arguments=None):
    try:
        native_only()
        path = socket_path(binding)
        original_socket = check_socket(path)
        request = {"version": VERSION, "binding": binding, "action": action}
        if action not in {"ready", "connect"}:
            request.update(instance=identifier(instance), request_id=identifier(request_id))
        if action == "submit":
            validate_arguments(tool, arguments)
            request.update(tool=tool, arguments=arguments)
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as channel:
            channel.settimeout(20)
            channel.connect(str(path))
            check_peer(channel)
            if check_socket(path) != original_socket:
                raise UnavailableError("native_host_changed")
            send(channel, request)
            response = receive(channel)
        if (
            set(response) != {"version", "binding", "instance", "result"}
            or type(response["version"]) is not int
            or response["version"] != VERSION
            or response["binding"] != binding
            or (instance is not None and response["instance"] != instance)
        ):
            raise ValueError
        identifier(response["instance"])
        result = project_result(response["result"], request_id)
        if result.get("state") == "unavailable":
            raise UnavailableError(result["code"])
        if action == "ready" and result.get("state") != "ready" or action == "connect" and result.get("state") != "connected":
            raise ValueError
        if action in {"submit", "status", "cancel"} and "status" not in result:
            raise ValueError
        return {"instance": response["instance"], "result": result}
    except UnavailableError:
        raise
    except (common.CliError, OSError, ValueError, KeyError, TypeError, AttributeError):
        raise UnavailableError() from None


def ready():
    try:
        binding = current_binding()
        response = rpc(binding, "ready")
        if response["result"].get("state") != "ready":
            raise UnavailableError()
        return {"binding": binding, **response}
    except (common.CliError, OSError, ValueError, KeyError, TypeError):
        raise UnavailableError() from None


def descriptors(names):
    result = []
    for tool in sorted(names):
        properties = {"service": {"type": "string"}, "label": {"type": "string"}}
        if tool == "credentials___assume_role":
            properties["purpose"] = {"type": "string"}
        result.append(
            {
                "name": tool,
                "description": (
                    "Request private credential delivery to a command independently selected by the human. "
                    "Requires exact browser approval. Returns only delivery metadata; use adp___operation_status for progress."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "additionalProperties": False,
                    "required": ["service"] if tool == "credentials___materialize" else [],
                    "x-adp-schema-version": "1.0",
                },
            }
        )
    return result
