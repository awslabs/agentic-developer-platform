# Copyright 2026 Superplane Contributors
# Portions derived from OpenClaw (https://github.com/openclaw/openclaw)
# Licensed under the Apache License, Version 2.0
#
# Original source: OpenClaw src/channels/web/ and ui/
#   - WebSocket-based web chat interface
#   - Real-time message streaming
#
# Modifications:
#   - Converted from TypeScript WebSocket to API Gateway WebSocket format
#   - Simplified to JSON message parsing (no streaming in ingest)
#   - Added JWT-based authentication for Cognito users

"""
WebChat channel adapter.

Parses WebSocket messages from API Gateway WebSocket API into UnifiedMessage format.
This is the simplest adapter as we control both sides of the protocol.

OpenClaw's web chat (src/channels/web/) uses a WebSocket connection directly
to the gateway process. For Superplane, web chat messages arrive via
API Gateway WebSocket API and are routed to the Ingest Lambda.

Message format (client -> server):
{
    "action": "message",
    "text": "Hello agent",
    "session_id": "optional-session-id",
    "attachments": [{"url": "...", "type": "image", "filename": "..."}],
    "persona": "intent-refinement"
}

`persona` (#4208) is OPTIONAL and UNTRUSTED. It pins the agent persona for the
turn, bypassing the server-side classifier. This adapter only carries the raw
value through to platform_data; the handler validates it against an allowlist
and REJECTS the message if it does not match. Never treat it as safe here.

The WebSocket connection is authenticated via Cognito JWT token
passed during the $connect route.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .base import (
    ChannelAdapter,
    ChannelType,
    MediaAttachment,
    MediaType,
    MessageRole,
    UnifiedMessage,
    effective_tenant_id,
)

logger = logging.getLogger(__name__)


class WebChatAdapter(ChannelAdapter):
    """WebChat adapter for API Gateway WebSocket API.

    Extracted from OpenClaw src/channels/web/ and adapted for
    API Gateway WebSocket integration.

    API Gateway WebSocket events include:
    - $connect: WebSocket connection (auth handled here)
    - $disconnect: WebSocket disconnection
    - $default / "message": User messages

    The requestContext from API Gateway provides:
    - connectionId: Unique WebSocket connection ID
    - authorizer: Claims from Cognito JWT (if configured)
    - identity: Source IP and user agent
    """

    def __init__(self) -> None:
        pass

    @property
    def channel_type(self) -> ChannelType:
        return ChannelType.WEBCHAT

    def verify_request(self, headers: dict[str, str], body: bytes) -> bool:
        """Verify WebChat request.

        Authentication is handled at the API Gateway level via Cognito
        authorizer on the $connect route. By the time a message reaches
        the Lambda, it's already authenticated.
        """
        return True

    def parse_event(self, payload: dict[str, Any]) -> UnifiedMessage | None:
        """Parse an API Gateway WebSocket event.

        The payload structure from API Gateway:
        {
            "requestContext": {
                "connectionId": "abc123",
                "routeKey": "$default",
                "authorizer": {"claims": {"sub": "user-id", "email": "..."}},
                "eventType": "MESSAGE",
                "connectedAt": 1234567890
            },
            "body": "{\"action\": \"message\", \"text\": \"Hello\"}"
        }
        """
        request_context = payload.get("requestContext", {})
        route_key = request_context.get("routeKey", "")

        # Skip connect/disconnect events
        if route_key in ("$connect", "$disconnect"):
            return None

        # Parse the message body
        body = payload.get("body", {})
        if isinstance(body, str):
            import json

            try:
                body = json.loads(body)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Failed to parse WebChat message body")
                return None

        action = body.get("action", "message")
        if action not in ("message", "sendMessage"):
            logger.debug("Ignoring WebChat action: %s", action)
            return None

        text = body.get("text", "").strip()
        if not text and not body.get("attachments"):
            return None

        # Extract user info from Cognito authorizer claims.
        # The Cognito sub is the only stable user identifier — connectionId
        # changes on every WebSocket reconnect and must NEVER be used as
        # ownerUserId, otherwise the LCM store permanently locks out
        # reconnected users from their own sessions.  See issue #88.
        claims = request_context.get("authorizer", {}).get("claims", {})
        user_id = claims.get("sub")
        if not user_id:
            logger.error(
                "WebChat message from connection %s has no resolvable user sub — dropping. "
                "Check that $connect authorizer ran and persisted claims.",
                request_context.get("connectionId", "?"),
            )
            return None
        user_name = claims.get("email", claims.get("cognito:username", user_id))
        connection_id = request_context.get("connectionId", "")

        # Parse attachments
        attachments = self._parse_attachments(body.get("attachments", []))

        return UnifiedMessage(
            channel=ChannelType.WEBCHAT,
            channel_id=connection_id,
            user_id=user_id,
            user_name=user_name,
            thread_id=body.get("session_id"),
            text=text,
            role=MessageRole.USER,
            timestamp=time.time(),
            attachments=attachments,
            is_mention=True,  # WebChat messages are always directed at the bot
            is_direct_message=True,
            # Vault Phase 5 (#138): WebChat is already resolved via Cognito $connect.
            # Set provider for optional consistency call to resolve-user.
            provider="cognito",
            provider_user_id=user_id,
            platform_data={
                "connection_id": connection_id,
                "connected_at": request_context.get("connectedAt"),
                "source_ip": request_context.get("identity", {}).get("sourceIp", ""),
                # Stage A (#184): propagate extended identity claims for downstream
                # ownership validation and audit logging.
                # Issue #5268: nothing in the JWT sign-in path ever SETS
                # `custom:tenant_id` -- the api-authorizer only reads it, with a
                # "" default -- so for a natively signed-in user the tenant was
                # structurally always empty. handler.handle_long_running refuses
                # to enqueue without one, so every native user's hosted chat
                # failed before enqueue: no inference, no invocation row, and
                # nothing visible in the product. Slack users were unaffected,
                # which is why the feature looked like it worked.
                #
                # Substitute the org, exactly as the Slack path does deliberately
                # ("Slack supplies a workspace ID, not an ADP tenant. Use the
                # server-resolved organization for the registered run
                # capability.").  Both values are read from the same trusted
                # `claims` dict here -- never from the client body -- because
                # this ends up on the row that authorizes a worker to inherit
                # its owner's Bedrock destination, so a wrong org would be
                # cross-tenant spend rather than a cosmetic mislabel.
                #
                # An explicit tenant claim still wins, and an unusable org
                # ("" / "default") is deliberately NOT substituted: the handler's
                # guard must still reject those, or an empty `tenant_id` would
                # land as the invocation row's GSI1PK and be unqueryable.
                "tenant_id": effective_tenant_id(claims),
                "org_id": claims.get("custom:org_id", ""),
                "team_id": claims.get("custom:team_id", ""),
                "department_id": claims.get("custom:department_id", ""),
                "account_type": claims.get("custom:account_type", ""),
                "role": claims.get("custom:role", ""),
                # Stage C (#186): artifact ID attachments from the upload flow.
                # The frontend sends string IDs ["art_xxx", ...] in the sendMessage
                # payload. These are forwarded to the worker via SQS so it can
                # inject them into the system prompt.
                "attachment_ids": [
                    a for a in body.get("attachments", [])
                    if isinstance(a, str) and a.startswith("art_")
                ],
                # Issue #4208: optional client-supplied persona pin. UNTRUSTED —
                # carried through verbatim so the handler can validate it against
                # the allowlist and reject. Absent/blank => classifier decides.
                "requested_persona": (
                    body.get("persona") if isinstance(body.get("persona"), str) else ""
                ),
            },
        )

    def _parse_attachments(self, attachments: list[dict[str, Any] | str]) -> list[MediaAttachment]:
        """Parse WebChat attachments.

        Client sends attachments as pre-uploaded S3 URLs:
        [{"url": "s3://...", "type": "image", "filename": "screenshot.png"}]

        String elements (artifact IDs like "art_xxx") are skipped here;
        they are handled separately via platform_data.attachment_ids.
        """
        result = []
        for att in attachments:
            if isinstance(att, str):
                continue  # artifact ID — handled separately in platform_data.attachment_ids
            type_str = att.get("type", "document")
            try:
                media_type = MediaType(type_str)
            except ValueError:
                media_type = MediaType.DOCUMENT

            result.append(
                MediaAttachment(
                    media_type=media_type,
                    url=att.get("url", ""),
                    filename=att.get("filename"),
                    mime_type=att.get("mime_type"),
                    size_bytes=att.get("size"),
                )
            )
        return result

    def format_response(
        self, text: str, thread_id: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Format a response for WebSocket delivery via API Gateway.

        The response is sent back to the client via API Gateway's
        @connections POST endpoint.
        """
        response: dict[str, Any] = {
            "action": "message",
            "text": text,
            "timestamp": time.time(),
        }
        if thread_id:
            response["session_id"] = thread_id
        if kwargs.get("connection_id"):
            response["connection_id"] = kwargs["connection_id"]
        return response

    def send_typing_indicator(
        self, channel_id: str, thread_id: str | None = None
    ) -> dict[str, Any] | None:
        """Generate WebChat typing indicator.

        Sent via API Gateway @connections to show the agent is processing.
        """
        return {
            "action": "typing",
            "connection_id": channel_id,
            "timestamp": time.time(),
        }
