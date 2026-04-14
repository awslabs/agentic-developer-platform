# Copyright 2026 Superplane Contributors
# Portions derived from OpenClaw (https://github.com/openclaw/openclaw)
# Licensed under the Apache License, Version 2.0
#
# Channel adapters extracted from OpenClaw's extension system.
# Each adapter normalizes platform-specific messages into a UnifiedMessage format.

from .base import (
    ChannelAdapter,
    ChannelType,
    MediaAttachment,
    MediaType,
    MessageRole,
    UnifiedMessage,
)

__all__ = [
    "ChannelType",
    "MessageRole",
    "MediaType",
    "MediaAttachment",
    "UnifiedMessage",
    "ChannelAdapter",
]
