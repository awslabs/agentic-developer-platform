"""
Known-issue durability test (scenario 19).

19. (INFORMATIONAL) Start a long_running task in conv A, switch to conv B,
    wait 30s, switch back to A. Currently the reply arrives on a dead
    connection and is lost.

Marked test.fixme() — when server-side persistence lands, unskip.

Regression tracking: successor to #124 (WS-based fix).
"""

from __future__ import annotations

import pytest

from .helpers import (
    create_new_conversation,
    get_local_storage_conversations,
    send_chat_message,
    take_failure_screenshot,
    wait_for_assistant_reply,
)


class TestDurabilityFlag:
    """Scenario 19: reply lost when switching conversations during long_running task."""

    @pytest.mark.xfail(
        reason=(
            "Known limitation: replies to long_running tasks are lost when the user "
            "switches conversations (dead WS connection). Server-side persistence "
            "not yet implemented. Tracking: successor to #124."
        ),
        strict=False,  # Don't fail the suite if this unexpectedly passes
    )
    def test_reply_survives_conversation_switch(self, authenticated_page):
        """Start a long_running task in conv A, switch to conv B, wait 30s,
        switch back to A. Assert the reply is present.

        Currently expected to FAIL (reply lost). When server-side persistence
        lands, this test should start passing and the xfail can be removed.
        """
        page = authenticated_page

        # Conv A: start a long_running task
        send_chat_message(
            page,
            "research the top 5 vector databases and compare their performance characteristics",
        )

        # Wait briefly for ACK
        page.wait_for_timeout(3000)

        # Switch to conv B (create new conversation)
        create_new_conversation(page)
        page.wait_for_timeout(1000)

        # Send something in conv B to keep it active
        send_chat_message(page, "hello")
        page.wait_for_timeout(5000)

        # Wait 30 seconds (the long_running reply should arrive during this time)
        page.wait_for_timeout(30_000)

        # Switch back to conv A
        # Click the first conversation in the sidebar
        sidebar_items = page.locator(
            "[class*='sidebar'] [class*='conversation'], "
            "[class*='Sidebar'] li, "
            "[class*='conv-list'] > *"
        )
        if sidebar_items.count() >= 2:
            sidebar_items.first.click()
            page.wait_for_timeout(3000)
        else:
            pytest.skip("Could not find conversation A in sidebar to switch back")

        # Check if the reply is present in conv A
        conversations = get_local_storage_conversations(page)

        # Find conv A (the one with the research message)
        conv_a = None
        for conv in conversations:
            msgs = conv.get("messages", [])
            user_msgs = [m for m in msgs if m.get("role") == "user"]
            if any("vector databases" in m.get("content", "") for m in user_msgs):
                conv_a = conv
                break

        assert conv_a is not None, "Could not find conv A with the research message"

        assistant_msgs = [
            m for m in conv_a.get("messages", [])
            if m.get("role") == "assistant"
        ]

        # This is the assertion that currently fails (known issue)
        assert len(assistant_msgs) >= 1 and any(
            len(m.get("content", "")) > 100 for m in assistant_msgs
        ), (
            f"Reply to long_running task in conv A was lost after switching to conv B. "
            f"Assistant messages: {len(assistant_msgs)}. "
            f"This is a known limitation — reply arrives on dead WS connection. "
            f"Tracking: successor to #124."
        )
