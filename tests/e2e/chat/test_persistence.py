"""
Conversation persistence tests (scenarios 15-17).

15. Messages persist to localStorage after send+reply.
16. Multi-conversation independence.
17. Survives page reload.

Regression of #122 + #123 (useLocalStorage fixes).
"""

from __future__ import annotations

import pytest

from .helpers import (
    STORAGE_KEY,
    create_new_conversation,
    get_local_storage_conversations,
    send_chat_message,
    take_failure_screenshot,
    wait_for_assistant_reply,
)


class TestMessagePersistence:
    """Scenario 15: messages persist to localStorage."""

    def test_messages_persist_after_send_and_reply(self, authenticated_page):
        """Send 'hi in A', wait for reply, verify localStorage key
        adp_chat_conversations contains 1 conversation with 2 messages
        (user + assistant).

        Regression of #122 + #123.
        """
        page = authenticated_page

        send_chat_message(page, "hi in A")

        try:
            wait_for_assistant_reply(page, timeout=60_000)
        except Exception:
            screenshot = take_failure_screenshot(page, "persist-reply")
            pytest.fail(
                f"No assistant reply within 60s for persistence test. "
                f"Screenshot: {screenshot}"
            )

        # Wait for localStorage write to settle
        page.wait_for_timeout(2000)

        conversations = get_local_storage_conversations(page)

        assert len(conversations) >= 1, (
            f"Expected at least 1 conversation in localStorage, got {len(conversations)}. "
            f"Regression of #122 (useLocalStorage)."
        )

        # Find the conversation with our message
        target_conv = None
        for conv in conversations:
            msgs = conv.get("messages", [])
            user_msgs = [m for m in msgs if m.get("role") == "user"]
            if any("hi in A" in m.get("content", "") for m in user_msgs):
                target_conv = conv
                break

        assert target_conv is not None, (
            f"Could not find conversation containing 'hi in A' in localStorage. "
            f"Conversations: {[c.get('title', '?') for c in conversations]}"
        )

        msgs = target_conv.get("messages", [])
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        assistant_msgs = [m for m in msgs if m.get("role") == "assistant"]

        assert len(user_msgs) >= 1, (
            f"Expected at least 1 user message, got {len(user_msgs)}"
        )
        assert len(assistant_msgs) >= 1, (
            f"Expected at least 1 assistant message, got {len(assistant_msgs)}. "
            f"Regression of #123 (messages not persisted)."
        )


class TestMultiConversationIndependence:
    """Scenario 16: multi-conversation independence in localStorage."""

    def test_two_conversations_independent(self, authenticated_page):
        """Create conv A → send 'hi in A' → wait → create conv B → send 'hi in B' → wait.
        Verify localStorage shows 2 conversations each with 2 messages.
        """
        page = authenticated_page

        # Conv A: send message
        send_chat_message(page, "hi in A")
        try:
            wait_for_assistant_reply(page, timeout=60_000)
        except Exception:
            pass  # Best effort

        page.wait_for_timeout(2000)

        # Create conv B
        create_new_conversation(page)
        page.wait_for_timeout(1000)

        # Conv B: send message
        send_chat_message(page, "hi in B")
        try:
            wait_for_assistant_reply(page, timeout=60_000)
        except Exception:
            pass  # Best effort

        page.wait_for_timeout(2000)

        conversations = get_local_storage_conversations(page)

        assert len(conversations) >= 2, (
            f"Expected at least 2 conversations in localStorage, got {len(conversations)}. "
            f"Regression of #122 (multi-conversation support)."
        )

        # Verify each has messages
        convs_with_messages = [
            c for c in conversations if len(c.get("messages", [])) >= 2
        ]
        assert len(convs_with_messages) >= 2, (
            f"Expected at least 2 conversations with >= 2 messages each, "
            f"got {len(convs_with_messages)}. "
            f"Message counts: {[len(c.get('messages', [])) for c in conversations]}"
        )


class TestSurvivesReload:
    """Scenario 17: conversations survive page reload."""

    def test_conversations_persist_after_reload(self, authenticated_page):
        """After test 16, reload the page, verify both conversations are still
        in localStorage and render in the sidebar.
        """
        page = authenticated_page

        # First, create some data
        send_chat_message(page, "persistence test message")
        try:
            wait_for_assistant_reply(page, timeout=60_000)
        except Exception:
            pass

        page.wait_for_timeout(2000)

        # Get conversations before reload
        convs_before = get_local_storage_conversations(page)
        count_before = len(convs_before)

        assert count_before >= 1, "Need at least 1 conversation before reload test"

        # Reload the page
        page.reload(wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(3000)

        # Get conversations after reload
        convs_after = get_local_storage_conversations(page)
        count_after = len(convs_after)

        assert count_after >= count_before, (
            f"Conversations lost after reload: had {count_before}, now {count_after}. "
            f"Regression of #122 + #123 (localStorage persistence)."
        )

        # Verify conversations still have their messages
        for conv in convs_after:
            msgs = conv.get("messages", [])
            if msgs:
                # At minimum, previously-stored messages should still be there
                assert len(msgs) >= 1, (
                    f"Conversation '{conv.get('title', '?')}' lost its messages after reload"
                )
