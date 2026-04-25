"""
History framing tests (scenarios 11-12).

11. No topic bleed across turns: greeting after time-ask should not reference time.
12. Agent doesn't re-run prior work: second task shouldn't re-fetch first task's data.
"""

from __future__ import annotations

import re
import time

import pytest

from .helpers import (
    HISTORY_BLEED_PHRASES,
    get_worker_logs,
    inject_tokens_and_navigate,
    send_chat_message,
    take_failure_screenshot,
    wait_for_assistant_reply,
)


class TestNoTopicBleed:
    """Scenario 11: no topic bleed across turns in a single conversation."""

    def test_greeting_after_time_ask_has_no_time_content(self, authenticated_page):
        """In a single conversation:
        1. Send 'what is the current time in UK', wait for reply.
        2. Send 'hello'.
        3. Assert: the response to 'hello' contains no time-of-day content
           and does NOT open with history-referencing phrases.

        Regression of #121 + #129.
        """
        page = authenticated_page

        # Turn 1: ask for time
        send_chat_message(page, "what is the current time in UK")
        try:
            reply1 = wait_for_assistant_reply(page, timeout=120_000)
        except Exception:
            screenshot = take_failure_screenshot(page, "history-turn1")
            pytest.fail(
                f"No reply to time question within 120s. Screenshot: {screenshot}"
            )

        # Wait a moment between turns
        page.wait_for_timeout(2000)

        # Turn 2: simple greeting
        send_chat_message(page, "hello")
        try:
            # Get the LAST assistant message (should be the greeting reply)
            page.wait_for_timeout(15_000)  # Wait for direct_response
            messages = page.locator(
                "[data-role='assistant'], .assistant-message, "
                "[class*='assistant'], [class*='Agent']"
            )
            count = messages.count()
            assert count >= 2, (
                f"Expected at least 2 assistant messages (time + greeting), got {count}"
            )
            reply2 = messages.nth(count - 1).inner_text()
        except Exception:
            screenshot = take_failure_screenshot(page, "history-turn2")
            pytest.fail(
                f"Could not get greeting reply. Screenshot: {screenshot}"
            )

        reply2_lower = reply2.lower()

        # Assert: no time-of-day content in the greeting reply
        time_indicators = [
            r"\d{1,2}:\d{2}",  # HH:MM pattern
            "gmt", "bst", "utc",
            "current time", "right now it's",
            "the time is", "it is currently",
        ]
        for pattern in time_indicators:
            if re.search(pattern, reply2_lower):
                screenshot = take_failure_screenshot(page, "history-bleed")
                pytest.fail(
                    f"Greeting reply contains time-related content (pattern: {pattern}). "
                    f"Reply: {reply2[:200]}... "
                    f"Topic bleed regression (#121 + #129). Screenshot: {screenshot}"
                )

        # Assert: no history-referencing phrases
        found_bleed = [
            p for p in HISTORY_BLEED_PHRASES if p.lower() in reply2_lower
        ]
        assert not found_bleed, (
            f"Greeting reply references prior turns: {found_bleed}. "
            f"Reply: {reply2[:200]}... "
            f"History framing regression (#121 + #129)."
        )


class TestAgentNoRerunPriorWork:
    """Scenario 12: agent doesn't re-run prior work in multi-turn conversation."""

    @pytest.mark.slow
    def test_second_task_doesnt_refetch_first_task_data(self, authenticated_page):
        """In a single conversation:
        1. Send 'list 5 trending github repos', wait for reply.
        2. Send 'what is the current time in UK', wait for reply.
        3. Assert: worker logs for the second task show <=1 WebFetch
           (the time API), not also github/trending fetches.

        Regression of #121.
        """
        page = authenticated_page

        # Turn 1: trending repos (long_running)
        send_chat_message(page, "list 5 trending github repos")
        try:
            wait_for_assistant_reply(page, timeout=180_000)
        except Exception:
            screenshot = take_failure_screenshot(page, "rerun-turn1")
            pytest.fail(
                f"No reply to trending repos within 180s. Screenshot: {screenshot}"
            )

        page.wait_for_timeout(3000)

        # Record time before second task (for log window)
        t_before_turn2 = time.time()

        # Turn 2: time query (should NOT re-fetch trending repos)
        send_chat_message(page, "what is the current time in UK")
        try:
            wait_for_assistant_reply(page, timeout=120_000)
        except Exception:
            screenshot = take_failure_screenshot(page, "rerun-turn2")
            pytest.fail(
                f"No reply to time query within 120s. Screenshot: {screenshot}"
            )

        # Check worker logs for the second task window
        # Look for WebFetch tool calls in the worker logs
        page.wait_for_timeout(5000)  # Let logs propagate
        logs = get_worker_logs(
            filter_pattern="tool_use: WebFetch",
            window_seconds=int(time.time() - t_before_turn2) + 30,
        )

        # Filter to only logs after turn 2 was sent
        # Count WebFetch calls — should be <=1 (just the time API)
        github_fetches = [
            line for line in logs
            if "github" in line.lower() or "trending" in line.lower()
        ]

        assert len(github_fetches) == 0, (
            f"Worker re-fetched github/trending data during the time query. "
            f"Found {len(github_fetches)} github-related WebFetch calls in logs. "
            f"Regression of #121 (agent re-running prior work)."
        )
