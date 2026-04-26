"""
Streaming-completion regression test.

Verifies that ``wait_for_assistant_reply`` only returns when the bubble has
finished streaming, not when a half-rendered prefix is visible.

The AG-UI event ordering bug (TEXT_MESSAGE_END before RUN_FINISHED) could leave
a streaming bubble in an intermediate state.  The old helper returned any
visible text after a fixed 2s sleep, masking this class of bug.

This test deliberately triggers a long-running task (which involves tool calls
and multi-chunk streaming) and asserts:
1. The helper waits for stable/complete text (not a prefix).
2. The returned text is substantive (not a truncated fragment).
"""

from __future__ import annotations

import time

import pytest

from .helpers import (
    send_chat_message,
    take_failure_screenshot,
    wait_for_assistant_reply,
    _get_streaming_status,
)


class TestStreamingComplete:
    """Regression guard for half-rendered bubble bug.

    The helper must return only when streaming is actually finished,
    either via data-streaming-status='complete' or via text stability.
    """

    @pytest.mark.slow
    def test_long_running_reply_is_complete_not_truncated(self, authenticated_page):
        """Send a long-running task that requires tool use (research), wait
        for the helper to return, and assert the reply is substantive and
        not a truncated streaming fragment.

        A truncated fragment would typically be:
        - Very short (<50 chars)
        - End mid-sentence (no sentence-ending punctuation)
        - Contain only an ACK with no substantive content
        """
        page = authenticated_page

        send_chat_message(
            page,
            "research the top 3 open source message queue systems, "
            "compare their throughput, and give me a recommendation"
        )

        t_start = time.monotonic()
        try:
            reply = wait_for_assistant_reply(page, timeout=180_000)
        except Exception:
            screenshot = take_failure_screenshot(page, "streaming-complete")
            pytest.fail(
                f"No assistant reply within 180s. Screenshot: {screenshot}"
            )
        elapsed = time.monotonic() - t_start

        # The reply must be substantive — not a truncated prefix
        assert len(reply.strip()) > 100, (
            f"Reply is suspiciously short ({len(reply.strip())} chars). "
            f"The helper may have returned a half-streamed prefix.\n"
            f"Reply: {reply[:200]}"
        )

        # The reply should contain multiple sentences (not just an ACK line)
        sentences = [s.strip() for s in reply.split(".") if len(s.strip()) > 10]
        assert len(sentences) >= 2, (
            f"Reply contains fewer than 2 sentences — likely a truncated "
            f"streaming fragment, not a complete response.\n"
            f"Reply: {reply[:300]}"
        )

    def test_helper_waits_for_stability_not_just_appearance(self, authenticated_page):
        """The helper should NOT return the instant text appears — it should
        wait for streaming to finish.  We verify this by checking that the
        helper took >2s after the bubble first appeared (the stability window).

        This is a weaker assertion but catches the pre-fix behaviour where
        the helper returned after a fixed 2s regardless of streaming state.
        """
        page = authenticated_page

        # Send a message that will get a multi-sentence response
        send_chat_message(page, "explain the CAP theorem with examples")

        # Watch for the bubble to appear (but don't wait for completion)
        bubble_selector = (
            "[data-role='assistant'], .assistant-message, "
            "[class*='assistant'], [class*='Agent']"
        )
        bubbles = page.locator(bubble_selector)
        t_bubble_appeared = None

        # Wait for bubble visibility
        try:
            bubbles.last.wait_for(state="visible", timeout=120_000)
            t_bubble_appeared = time.monotonic()
        except Exception:
            screenshot = take_failure_screenshot(page, "streaming-stability-wait")
            pytest.fail(
                f"No assistant bubble appeared within 120s. Screenshot: {screenshot}"
            )

        # Now get the first snapshot of text
        first_text = bubbles.last.inner_text()

        # Call the full helper (which should wait for stability/completion)
        reply = wait_for_assistant_reply(page, timeout=120_000)

        t_helper_returned = time.monotonic()

        # If the first snapshot was very short (streaming was in progress),
        # the helper should have waited significantly longer than 0s.
        if len(first_text.strip()) < 50:
            wait_after_appear = t_helper_returned - t_bubble_appeared
            assert wait_after_appear >= 1.5, (
                f"Helper returned too quickly after bubble appeared "
                f"({wait_after_appear:.1f}s). It likely returned a streaming "
                f"prefix, not a completed bubble.\n"
                f"First text ({len(first_text)} chars): {first_text[:100]}...\n"
                f"Final text ({len(reply)} chars): {reply[:100]}..."
            )

        # The final reply should be at least as long as the first snapshot
        assert len(reply) >= len(first_text), (
            f"Helper returned text shorter than initial snapshot — "
            f"something went wrong with stability detection.\n"
            f"First: {len(first_text)} chars, Final: {len(reply)} chars"
        )
