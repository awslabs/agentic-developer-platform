# Copyright 2026 Superplane Contributors
# Portions derived from OpenClaw (https://github.com/openclaw/openclaw)
# Licensed under the Apache License, Version 2.0
#
# Original source: OpenClaw src/channels/session.ts, src/channels/session-envelope.ts
# Extracted: Normalized message format (UnifiedMessage interface equivalent)
# Modifications: Converted from TypeScript to Python dataclasses; added DynamoDB
#   serialization; added channel-agnostic media attachment model.

"""
Base channel adapter module.

Defines the normalized message format that all channel adapters produce.
Inspired by OpenClaw's UnifiedMessage interface which normalizes messages
from 20+ platforms into a single representation for the agent runtime.

Key concepts from OpenClaw:
- Every inbound message is normalized to a common format before processing
- Channel-specific metadata is preserved in a platform_data dict
- Sessions are keyed by (channel, user_id, thread_id) triple
- Media attachments are normalized with type, URL, and optional metadata
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ChannelType(str, Enum):
    """Supported messaging channels.

    Extracted from OpenClaw's channel registry (src/channels/registry.ts).
    OpenClaw supports 24+ channels; we extract the top 5 for Superplane.
    """

    SLACK = "slack"
    WHATSAPP = "whatsapp"
    TEAMS = "teams"
    DISCORD = "discord"
    WEBCHAT = "webchat"


class MessageRole(str, Enum):
    """Message role in a conversation.

    Mirrors OpenClaw's session message roles used in Lane Queue processing.
    """

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class MediaType(str, Enum):
    """Normalized media types.

    Extracted from OpenClaw's media handling in extensions/whatsapp/src/media.ts
    and extensions/slack/src/actions.download-file.test.ts.
    """

    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"


@dataclass
class MediaAttachment:
    """A normalized media attachment.

    OpenClaw handles media per-channel (Slack files API, WhatsApp media download,
    Discord CDN). This normalizes them into a single format for downstream processing.

    Attributes:
        media_type: The type of media (image, audio, video, document).
        url: The source URL of the media (platform-specific, may require auth).
        filename: Original filename if available.
        mime_type: MIME type string (e.g., 'image/png').
        size_bytes: File size in bytes if known.
        metadata: Additional platform-specific metadata.
    """

    media_type: MediaType
    url: str
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for DynamoDB/JSON storage."""
        result = {
            "media_type": self.media_type.value,
            "url": self.url,
        }
        if self.filename:
            result["filename"] = self.filename
        if self.mime_type:
            result["mime_type"] = self.mime_type
        if self.size_bytes is not None:
            result["size_bytes"] = self.size_bytes
        if self.metadata:
            result["metadata"] = self.metadata
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MediaAttachment:
        """Deserialize from DynamoDB/JSON storage."""
        return cls(
            media_type=MediaType(data["media_type"]),
            url=data["url"],
            filename=data.get("filename"),
            mime_type=data.get("mime_type"),
            size_bytes=data.get("size_bytes"),
            metadata=data.get("metadata", {}),
        )


@dataclass
class UnifiedMessage:
    """Normalized message format for all channels.

    This is the Superplane equivalent of OpenClaw's internal message representation.
    Every channel adapter converts platform-specific events into this format before
    the message enters the processing pipeline (SQS queue -> agent pod).

    OpenClaw source: src/channels/session-envelope.ts, src/channels/session.ts
    The original uses a TypeScript interface with channel-specific metadata bags.
    We flatten it into a Python dataclass with explicit fields.

    Attributes:
        message_id: Unique message identifier (UUID).
        channel: Which platform the message came from.
        channel_id: Platform-specific channel/conversation ID.
        user_id: Platform-specific user identifier.
        user_name: Display name of the sender.
        thread_id: Thread/conversation thread ID (for threaded channels).
        text: The text content of the message.
        role: Message role (user, assistant, system, tool).
        timestamp: Unix timestamp of the message.
        attachments: List of media attachments.
        is_mention: Whether the bot was explicitly mentioned.
        is_direct_message: Whether this is a DM (not a group/channel message).
        reply_to_message_id: ID of the message being replied to.
        platform_data: Raw platform-specific data preserved for debugging/features.
    """

    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    channel: ChannelType = ChannelType.WEBCHAT
    channel_id: str = ""
    user_id: str = ""
    user_name: str = ""
    thread_id: str | None = None
    text: str = ""
    role: MessageRole = MessageRole.USER
    timestamp: float = field(default_factory=time.time)
    attachments: list[MediaAttachment] = field(default_factory=list)
    is_mention: bool = False
    is_direct_message: bool = True
    reply_to_message_id: str | None = None
    platform_data: dict[str, Any] = field(default_factory=dict)

    @property
    def session_key(self) -> str:
        """Generate a session key from channel + user + thread.

        Mirrors OpenClaw's session key format: workspace:channel:userId
        We adapt to: {channel}:{channel_id}:{user_id}[:{thread_id}]

        This key is used to route messages to the correct DynamoDB session
        and ensure per-user, per-thread isolation.
        """
        parts = [self.channel.value, self.channel_id, self.user_id]
        if self.thread_id:
            parts.append(self.thread_id)
        return ":".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for SQS/DynamoDB transport."""
        result = {
            "message_id": self.message_id,
            "channel": self.channel.value,
            "channel_id": self.channel_id,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "text": self.text,
            "role": self.role.value,
            "timestamp": self.timestamp,
            "is_mention": self.is_mention,
            "is_direct_message": self.is_direct_message,
        }
        if self.thread_id:
            result["thread_id"] = self.thread_id
        if self.reply_to_message_id:
            result["reply_to_message_id"] = self.reply_to_message_id
        if self.attachments:
            result["attachments"] = [a.to_dict() for a in self.attachments]
        if self.platform_data:
            result["platform_data"] = self.platform_data
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UnifiedMessage:
        """Deserialize from SQS/DynamoDB transport."""
        attachments = [MediaAttachment.from_dict(a) for a in data.get("attachments", [])]
        return cls(
            message_id=data.get("message_id", str(uuid.uuid4())),
            channel=ChannelType(data["channel"]),
            channel_id=data.get("channel_id", ""),
            user_id=data.get("user_id", ""),
            user_name=data.get("user_name", ""),
            thread_id=data.get("thread_id"),
            text=data.get("text", ""),
            role=MessageRole(data.get("role", "user")),
            timestamp=data.get("timestamp", time.time()),
            attachments=attachments,
            is_mention=data.get("is_mention", False),
            is_direct_message=data.get("is_direct_message", True),
            reply_to_message_id=data.get("reply_to_message_id"),
            platform_data=data.get("platform_data", {}),
        )


class ChannelAdapter(ABC):
    """Abstract base class for channel adapters.

    Each channel adapter implements two responsibilities:
    1. parse_event(): Convert platform-specific webhook/event into UnifiedMessage
    2. verify_request(): Validate that the incoming request is authentic

    OpenClaw source: src/channels/plugins/registry.ts, extensions/*/src/channel.ts
    OpenClaw uses a plugin registry pattern where each channel registers parse/send
    handlers. We simplify this into a Python ABC since we don't need dynamic plugin
    loading — channels are statically configured per Lambda deployment.

    Optional capabilities (override if supported):
    - format_response(): Convert agent response back to platform format
    - send_typing_indicator(): Show typing status in the channel
    """

    @property
    @abstractmethod
    def channel_type(self) -> ChannelType:
        """The channel type this adapter handles."""
        ...

    @abstractmethod
    def verify_request(self, headers: dict[str, str], body: bytes) -> bool:
        """Verify the authenticity of an incoming webhook request.

        Each platform has its own signature verification mechanism:
        - Slack: HMAC-SHA256 with signing secret
        - WhatsApp: Webhook verify token
        - Teams: Bot Framework JWT validation
        - Discord: Ed25519 signature verification

        Args:
            headers: HTTP request headers.
            body: Raw request body bytes.

        Returns:
            True if the request is authentic.
        """
        ...

    @abstractmethod
    def parse_event(self, payload: dict[str, Any]) -> UnifiedMessage | None:
        """Parse a platform-specific event into a UnifiedMessage.

        Returns None if the event should be ignored (e.g., bot's own messages,
        channel_join events, message_changed edits we don't care about).

        Args:
            payload: The parsed JSON payload from the webhook.

        Returns:
            A UnifiedMessage if the event should be processed, None otherwise.
        """
        ...

    def format_response(
        self, text: str, thread_id: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Format an agent response for sending back to the platform.

        Default implementation returns a simple text payload.
        Override for rich formatting (Slack blocks, Teams adaptive cards, etc.).

        Args:
            text: The response text.
            thread_id: Thread to reply in (if applicable).
            **kwargs: Additional platform-specific options.

        Returns:
            Platform-specific response payload.
        """
        return {"text": text, "thread_id": thread_id}

    def send_typing_indicator(
        self, channel_id: str, thread_id: str | None = None
    ) -> dict[str, Any] | None:
        """Generate a typing indicator payload for the platform.

        OpenClaw source: src/channels/typing.ts
        OpenClaw sends typing indicators to show the agent is processing.
        Returns None if the platform doesn't support typing indicators.

        Args:
            channel_id: The channel to show typing in.
            thread_id: The thread to show typing in (if applicable).

        Returns:
            Platform-specific typing indicator payload, or None.
        """
        return None
