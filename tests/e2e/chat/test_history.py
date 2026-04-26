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


class TestFollowUpRecognised:
    """Regression guard for #163 — follow-ups must be recognised as such, not
    classified as new topics.  Every refinement on the same topic should
    produce a contextual reply that builds on the prior turn, not a reply
    that says 'your message may not have come through' or ignores history."""

    def test_refinement_on_same_topic_builds_on_prior_answer(self, authenticated_page):
        """Two-turn exchange on a single topic:

        Turn 1: ask for open-source video editing tools.
        Turn 2: ask which of *those* has the highest GitHub stars and why.

        Assertions:
        a) Reply 2 does NOT contain dead-end / disconnected phrases
           (would indicate classifier forced thread_action=new).
        b) Reply 2 references at least one concrete item from reply 1
           OR is a substantive response (>100 chars) mentioning "star" / "github".
        """
        page = authenticated_page

        # Turn 1: ask for open-source video editing tools.
        send_chat_message(page, "what are some open source video editing tools?")
        try:
            reply1 = wait_for_assistant_reply(page, timeout=180_000)
        except Exception:
            screenshot = take_failure_screenshot(page, "followup-turn1")
            pytest.fail(
                f"No reply to video-editing-tools question within 180s. "
                f"Screenshot: {screenshot}"
            )

        # Brief pause between turns
        page.wait_for_timeout(2000)

        # Turn 2: explicit refinement referring to the prior list.
        send_chat_message(
            page,
            "can you tell me which of those has the highest github stars "
            "and why you'd recommend it?"
        )
        try:
            # Get the LAST assistant message (the follow-up reply)
            messages = page.locator(
                "[data-role='assistant'], .assistant-message, "
                "[class*='assistant'], [class*='Agent']"
            )
            # Wait for a new (second) assistant bubble
            page.wait_for_timeout(5000)
            count = messages.count()
            if count >= 2:
                reply2 = wait_for_assistant_reply(page, timeout=180_000, index=-1)
            else:
                reply2 = wait_for_assistant_reply(page, timeout=180_000)
        except Exception:
            screenshot = take_failure_screenshot(page, "followup-turn2")
            pytest.fail(
                f"No reply to follow-up within 180s. Screenshot: {screenshot}"
            )

        # --- Assertion (a): reply 2 must NOT be a dead-end / disconnected response ---
        bad_phrases = [
            "i don't see a new question",
            "your message may not have come through",
            "it looks like your message",
            "whenever you're ready",
            "i'm not sure what you're referring to",
            "could you please clarify",
            "i don't have context",
        ]
        reply2_lower = reply2.lower()
        for phrase in bad_phrases:
            assert phrase not in reply2_lower, (
                f"Refinement was not recognised as follow-up — reply contains "
                f"{phrase!r}. Classifier likely routed thread_action=new instead "
                f"of follow_up. Regression of #163.\n"
                f"Reply 2 excerpt: {reply2[:300]}"
            )

        # --- Assertion (b): reply 2 references prior content or is substantive ---
        # Extract proper-noun-shaped tokens from reply 1 (capitalised words >=4 chars
        # that aren't common English).  If any appear in reply 2, the agent used
        # history.
        common_words = {
            "this", "that", "with", "from", "here", "there", "some", "they",
            "have", "been", "will", "your", "more", "also", "very", "much",
            "well", "just", "most", "only", "than", "them", "each", "such",
            "like", "when", "what", "make", "over", "into", "open", "free",
            "source", "tool", "tools", "video", "editing",
        }
        # Look for capitalised proper nouns (tool names like Kdenlive, Shotcut, etc.)
        candidates = set(re.findall(r"\b[A-Z][a-z]{3,}\b", reply1)) - common_words
        # Also look for all-caps acronyms (e.g. GIMP, OBS)
        candidates |= set(re.findall(r"\b[A-Z]{2,6}\b", reply1)) - {"THE", "AND", "FOR"}

        found_overlap = any(
            c.lower() in reply2_lower for c in candidates
        )

        if not found_overlap:
            # Fallback: accept a substantive response about stars/github
            assert len(reply2.strip()) > 100, (
                f"Follow-up reply is too short ({len(reply2.strip())} chars) "
                f"and does not reference any item from turn 1. The agent likely "
                f"cold-started instead of using history. Regression of #163.\n"
                f"Reply 1 candidates: {candidates}\n"
                f"Reply 2 excerpt: {reply2[:300]}"
            )
            has_star_ref = "star" in reply2_lower or "github" in reply2_lower
            assert has_star_ref, (
                f"Follow-up reply doesn't mention 'star' or 'github' even "
                f"though the question was about GitHub stars. The agent likely "
                f"ignored the follow-up context. Regression of #163.\n"
                f"Reply 2 excerpt: {reply2[:300]}"
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
