# Copyright 2026 Superplane Contributors
# Portions derived from OpenClaw (https://github.com/openclaw/openclaw)
# Licensed under the Apache License, Version 2.0
#
# Original source: OpenClaw extensions/slack/src/
#   - channel.ts, channel.runtime.ts — Slack channel lifecycle
#   - types.ts — SlackMessageEvent, SlackAppMentionEvent types
#   - inbound.contract.test.ts — Event parsing contracts
#   - threading.ts, sent-thread-cache.ts — Thread management
#   - draft-stream.ts — Streaming/typing indicators
#   - client.ts — Slack API client wrapper
#   - format.ts — Message formatting (mrkdwn)
#   - actions.ts — File download and processing
#
# Modifications:
#   - Converted from TypeScript to Python
#   - Replaced OpenClaw's plugin registry with ChannelAdapter ABC
#   - Simplified signature verification (OpenClaw delegates to Bolt SDK)
#   - Added DynamoDB-compatible serialization
#   - Removed dependency on OpenClaw's Lane Queue; returns UnifiedMessage for SQS

"""
Slack channel adapter.

Parses Slack Events API payloads into UnifiedMessage format.
Handles signature verification, thread management, and typing indicators.

Slack event types handled:
- event_callback/message: Direct messages and channel messages
- event_callback/app_mention: Bot mentions in channels
- url_verification: Slack URL challenge (handshake)

OpenClaw's Slack adapter (extensions/slack/src/) is one of the most mature,
with 104 source files handling blocks, interactive replies, message actions,
streaming, and more. We extract the core inbound parsing and verification
logic needed for the Ingest Lambda.
"""

from __future__ import annotations

import hashlib
import hmac
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
)

logger = logging.getLogger(__name__)

# Slack file type to MediaType mapping
# Extracted from OpenClaw extensions/slack/src/actions.ts
_SLACK_MEDIA_TYPE_MAP: dict[str, MediaType] = {
    "image": MediaType.IMAGE,
    "video": MediaType.VIDEO,
    "audio": MediaType.AUDIO,
}


class SlackAdapter(ChannelAdapter):
    """Slack Events API adapter.

    Extracted from OpenClaw extensions/slack/src/channel.ts and related files.
    Handles inbound event parsing, signature verification, and response formatting.

    OpenClaw's Slack integration supports:
    - Block Kit rendering (blocks-render.ts, block-kit-tables.ts)
    - Interactive replies (interactive-replies.ts)
    - Message actions (message-actions.ts, message-action-dispatch.ts)
    - Thread caching (sent-thread-cache.ts)
    - Streaming/typing (draft-stream.ts, streaming.ts)
    - File downloads (actions.ts)
    - Group policy (group-policy.ts)
    - Channel migration (channel-migration.ts)

    We extract the core event parsing for the Ingest Lambda. Rich features
    (Block Kit, interactive) are Phase 2.

    Args:
        signing_secret: Slack app signing secret for request verification.
        bot_user_id: The bot's Slack user ID (for filtering self-messages).
    """

    def __init__(self, signing_secret: str, bot_user_id: str = ""):
        self._signing_secret = signing_secret
        self._bot_user_id = bot_user_id

    @property
    def channel_type(self) -> ChannelType:
        return ChannelType.SLACK

    def verify_request(self, headers: dict[str, str], body: bytes) -> bool:
        """Verify Slack request signature using HMAC-SHA256.

        Extracted from OpenClaw extensions/slack/src/client.ts which delegates
        to @slack/bolt's signature verification. We implement it directly to
        avoid the Bolt SDK dependency in Lambda.

        Slack signature verification:
        1. Extract timestamp and signature from headers
        2. Construct basestring: "v0:{timestamp}:{body}"
        3. Compute HMAC-SHA256 with signing secret
        4. Compare with provided signature
        5. Reject if timestamp is >5 minutes old (replay protection)

        See: https://api.slack.com/authentication/verifying-requests-from-slack
        """
        timestamp = headers.get("x-slack-request-timestamp", "")
        signature = headers.get("x-slack-signature", "")

        if not timestamp or not signature:
            logger.warning("Missing Slack signature headers")
            return False

        # Replay protection: reject requests older than 5 minutes
        try:
            ts = int(timestamp)
        except ValueError:
            logger.warning("Invalid Slack timestamp: %s", timestamp)
            return False

        if abs(time.time() - ts) > 300:
            logger.warning("Slack request timestamp too old: %s", timestamp)
            return False

        # Compute expected signature
        basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
        expected = (
            "v0="
            + hmac.new(
                self._signing_secret.encode("utf-8"),
                basestring.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        )

        return hmac.compare_digest(expected, signature)

    def parse_event(self, payload: dict[str, Any]) -> UnifiedMessage | None:
        """Parse a Slack Events API payload into a UnifiedMessage.

        Extracted from OpenClaw extensions/slack/src/channel.runtime.ts and
        extensions/slack/src/inbound.contract.test.ts.

        Handles three event types:
        1. url_verification — Slack challenge handshake (returns None, handled separately)
        2. event_callback with type=message — User messages in DMs and channels
        3. event_callback with type=app_mention — Bot mentions in channels

        Events that are ignored (returns None):
        - Bot's own messages (prevents echo loops)
        - Message subtypes: message_changed, message_deleted, channel_join, etc.
        - Events without text content

        OpenClaw's inbound pipeline:
        1. Receive Slack event via HTTP POST
        2. Verify signature (client.ts)
        3. Parse event type (channel.runtime.ts)
        4. Normalize to internal format (inbound.contract.test.ts defines contracts)
        5. Route to Lane Queue by session key

        We extract steps 3-4 and route to SQS instead of Lane Queue.
        """
        event_type = payload.get("type")

        # URL verification challenge — not a real message
        if event_type == "url_verification":
            logger.debug("Slack URL verification challenge")
            return None

        if event_type != "event_callback":
            logger.debug("Ignoring Slack event type: %s", event_type)
            return None

        event = payload.get("event", {})
        inner_type = event.get("type")

        if inner_type == "message":
            return self._parse_message_event(event, payload)
        elif inner_type == "app_mention":
            return self._parse_mention_event(event, payload)
        else:
            logger.debug("Ignoring Slack inner event type: %s", inner_type)
            return None

    def _parse_message_event(
        self, event: dict[str, Any], payload: dict[str, Any]
    ) -> UnifiedMessage | None:
        """Parse a Slack message event.

        Extracted from OpenClaw extensions/slack/src/types.ts (SlackMessageEvent)
        and extensions/slack/src/channel.runtime.ts.

        OpenClaw's SlackMessageEvent type includes:
        - type, subtype, user, bot_id, text, ts, thread_ts
        - channel, channel_type ("im", "mpim", "channel", "group")
        - files[], attachments[]
        """
        # Skip bot messages to prevent echo loops
        # OpenClaw: checks bot_id and user against self
        if event.get("bot_id"):
            return None
        user_id = event.get("user", "")
        if self._bot_user_id and user_id == self._bot_user_id:
            return None

        # Skip message subtypes that aren't new user messages
        # OpenClaw filters these in channel.runtime.ts
        subtype = event.get("subtype")
        skip_subtypes = {
            "message_changed",
            "message_deleted",
            "channel_join",
            "channel_leave",
            "channel_topic",
            "channel_purpose",
            "channel_name",
            "bot_message",
            "file_comment",
            "group_join",
            "group_leave",
        }
        if subtype in skip_subtypes:
            return None

        text = event.get("text", "").strip()
        if not text and not event.get("files"):
            return None

        # Determine if DM based on channel_type
        # OpenClaw: channel-type.ts maps Slack channel types
        channel_type = event.get("channel_type", "")
        is_dm = channel_type in ("im", "mpim")

        # Parse attachments (files)
        # OpenClaw: actions.ts handles file downloads
        attachments = self._parse_files(event.get("files", []))

        # Vault Phase 5 (#138): provider identity for resolve-user.
        # Convention: provider_user_id = "<workspace_id>:<user_id>"
        workspace_id = payload.get("team_id", "")
        provider_uid = f"{workspace_id}:{user_id}" if workspace_id else user_id

        return UnifiedMessage(
            channel=ChannelType.SLACK,
            channel_id=event.get("channel", ""),
            user_id=user_id,
            user_name=event.get("user_profile", {}).get("display_name", user_id),
            thread_id=event.get("thread_ts"),
            text=text,
            role=MessageRole.USER,
            timestamp=float(event.get("ts", time.time())),
            attachments=attachments,
            is_mention=False,
            is_direct_message=is_dm,
            provider="slack",
            provider_user_id=provider_uid,
            platform_data={
                "team_id": workspace_id,
                "event_id": payload.get("event_id", ""),
                "channel_type": channel_type,
                "subtype": subtype,
            },
        )

    def _parse_mention_event(
        self, event: dict[str, Any], payload: dict[str, Any]
    ) -> UnifiedMessage | None:
        """Parse a Slack app_mention event.

        Extracted from OpenClaw extensions/slack/src/types.ts (SlackAppMentionEvent)
        and extensions/slack/src/mention-gating logic.

        App mentions are triggered when a user @mentions the bot in a channel.
        OpenClaw uses mention-gating (src/channels/mention-gating.ts) to control
        whether the bot responds to mentions vs all messages.
        """
        user_id = event.get("user", "")
        if self._bot_user_id and user_id == self._bot_user_id:
            return None

        text = event.get("text", "").strip()
        # Strip the bot mention from the text
        if self._bot_user_id:
            text = text.replace(f"<@{self._bot_user_id}>", "").strip()

        if not text:
            return None

        # Vault Phase 5 (#138): provider identity for resolve-user.
        workspace_id = payload.get("team_id", "")
        provider_uid = f"{workspace_id}:{user_id}" if workspace_id else user_id

        return UnifiedMessage(
            channel=ChannelType.SLACK,
            channel_id=event.get("channel", ""),
            user_id=user_id,
            user_name=user_id,  # Slack mentions don't include profile
            thread_id=event.get("thread_ts") or event.get("ts"),
            text=text,
            role=MessageRole.USER,
            timestamp=float(event.get("ts", time.time())),
            is_mention=True,
            is_direct_message=False,
            provider="slack",
            provider_user_id=provider_uid,
            platform_data={
                "team_id": workspace_id,
                "event_id": payload.get("event_id", ""),
            },
        )

    def _parse_files(self, files: list[dict[str, Any]]) -> list[MediaAttachment]:
        """Parse Slack file attachments into normalized MediaAttachments.

        Extracted from OpenClaw extensions/slack/src/actions.ts which handles
        file download via Slack's files API.

        Slack file objects include:
        - id, name, mimetype, filetype, size
        - url_private, url_private_download
        - thumb_* for image thumbnails
        """
        attachments = []
        for f in files:
            filetype = f.get("filetype", "")
            media_type = _SLACK_MEDIA_TYPE_MAP.get(
                _classify_slack_filetype(filetype), MediaType.DOCUMENT
            )
            attachments.append(
                MediaAttachment(
                    media_type=media_type,
                    url=f.get("url_private_download", f.get("url_private", "")),
                    filename=f.get("name"),
                    mime_type=f.get("mimetype"),
                    size_bytes=f.get("size"),
                    metadata={
                        "slack_file_id": f.get("id"),
                        "filetype": filetype,
                    },
                )
            )
        return attachments

    def format_response(
        self, text: str, thread_id: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Format a response for the Slack Web API (chat.postMessage).

        Extracted from OpenClaw extensions/slack/src/send.ts and
        extensions/slack/src/blocks-render.ts.

        OpenClaw supports rich Block Kit responses (tables, code blocks, etc.)
        via blocks-render.ts and block-kit-tables.ts. We start with simple
        mrkdwn text and add Block Kit in Phase 2.
        """
        response: dict[str, Any] = {"text": text}
        if thread_id:
            response["thread_ts"] = thread_id
        # Optional: unfurl control
        if kwargs.get("unfurl_links") is not None:
            response["unfurl_links"] = kwargs["unfurl_links"]
        if kwargs.get("unfurl_media") is not None:
            response["unfurl_media"] = kwargs["unfurl_media"]
        return response

    def send_typing_indicator(
        self, channel_id: str, thread_id: str | None = None
    ) -> dict[str, Any] | None:
        """Generate Slack typing indicator payload.

        Extracted from OpenClaw extensions/slack/src/draft-stream.ts and
        src/channels/typing.ts. OpenClaw uses Slack's chat.meMessage or
        a custom typing approach depending on the channel type.

        Note: Slack doesn't have a native "typing" API for bots in channels.
        OpenClaw uses streaming message updates as a typing proxy.
        For the Ingest Lambda, we return a reaction-based indicator instead.
        """
        return {
            "action": "add_reaction",
            "channel": channel_id,
            "name": "hourglass_flowing_sand",
            "timestamp": thread_id,
        }

    @staticmethod
    def handle_url_verification(payload: dict[str, Any]) -> dict[str, Any]:
        """Handle Slack URL verification challenge.

        This is called during Slack app setup. Returns the challenge token
        to verify endpoint ownership.

        Args:
            payload: The Slack challenge payload.

        Returns:
            Response with the challenge token.
        """
        return {"challenge": payload.get("challenge", "")}


def _classify_slack_filetype(filetype: str) -> str:
    """Classify a Slack filetype string into a media category.

    Extracted from OpenClaw's file handling logic in extensions/slack/src/actions.ts.
    """
    image_types = {
        "png",
        "jpg",
        "jpeg",
        "gif",
        "bmp",
        "svg",
        "webp",
        "ico",
        "tiff",
    }
    video_types = {"mp4", "mov", "avi", "mkv", "webm", "flv"}
    audio_types = {"mp3", "wav", "ogg", "flac", "aac", "m4a", "wma"}

    ft = filetype.lower()
    if ft in image_types:
        return "image"
    elif ft in video_types:
        return "video"
    elif ft in audio_types:
        return "audio"
    return "document"
