"""
Bash / tool execution tests (scenarios 13-14).

13. Bash actually runs: send a bash command, verify real output in reply.
14. Per-tool-call logs emitted: worker pod logs contain tool_use and tool_result lines.
"""

from __future__ import annotations

import re
import time

import pytest

from .helpers import (
    get_worker_logs,
    send_chat_message,
    take_failure_screenshot,
    wait_for_assistant_reply,
)


class TestBashExecution:
    """Scenario 13: bash commands actually execute in the worker."""

    @pytest.mark.slow
    def test_bash_ls_returns_real_listing(self, authenticated_page):
        """Send 'run `ls -la /tmp` and tell me what's there' — wait up to 4 min.

        Assert: final reply contains real directory listing (e.g. 'workspace'
        or 'drwx'), not phrases like 'shell environment isn't configured' or
        'SHELL env var'.

        Regression of #119 + #120 (bash install in runner image).
        """
        page = authenticated_page

        send_chat_message(page, "run `ls -la /tmp` and tell me what's there")

        try:
            reply = wait_for_assistant_reply(page, timeout=240_000)
        except Exception:
            screenshot = take_failure_screenshot(page, "bash-exec")
            pytest.fail(
                f"No reply to bash command within 240s. Screenshot: {screenshot}"
            )

        reply_lower = reply.lower()

        # Assert: reply contains real directory listing indicators
        listing_indicators = [
            "drwx", "total ", "-rw", "workspace",
            "/tmp", "permission", "root",
        ]
        has_listing = any(ind in reply_lower for ind in listing_indicators)

        # Assert: reply does NOT contain failure indicators
        failure_phrases = [
            "shell environment isn't configured",
            "shell env var",
            "cannot execute",
            "bash is not available",
            "command not found",
            "unable to run",
        ]
        has_failure = any(phrase in reply_lower for phrase in failure_phrases)

        if has_failure:
            screenshot = take_failure_screenshot(page, "bash-not-working")
            pytest.fail(
                f"Bash execution failed — reply indicates shell not configured. "
                f"Reply: {reply[:500]}... "
                f"Regression of #120 (bash install). Screenshot: {screenshot}"
            )

        assert has_listing, (
            f"Reply doesn't contain directory listing indicators. "
            f"Reply: {reply[:500]}... "
            f"Bash may not have executed correctly."
        )


class TestPerToolLogs:
    """Scenario 14: per-tool-call logs emitted in worker pod."""

    @pytest.mark.slow
    def test_worker_logs_contain_tool_use_and_result(self, authenticated_page):
        """After the bash test, verify worker pod logs contain lines matching:
        - `turn \\d+ tool_use: Bash`
        - `turn \\d+ tool_result: \\w+ ok`

        Regression of #119 (observability addition).
        """
        page = authenticated_page

        # Send a bash command
        t_before = time.time()
        send_chat_message(page, "run `echo hello_world` and show me the output")

        try:
            wait_for_assistant_reply(page, timeout=240_000)
        except Exception:
            screenshot = take_failure_screenshot(page, "tool-logs")
            pytest.fail(
                f"No reply to bash command within 240s. Screenshot: {screenshot}"
            )

        # Wait for logs to propagate
        page.wait_for_timeout(10_000)

        # Fetch worker logs
        window = int(time.time() - t_before) + 60
        logs = get_worker_logs(
            filter_pattern="tool_use",
            window_seconds=window,
        )
        logs_result = get_worker_logs(
            filter_pattern="tool_result",
            window_seconds=window,
        )

        all_logs = logs + logs_result

        # Check for tool_use: Bash pattern
        tool_use_pattern = re.compile(r"turn\s+\d+\s+tool_use:\s*Bash", re.IGNORECASE)
        tool_use_matches = [l for l in all_logs if tool_use_pattern.search(l)]

        # Check for tool_result pattern
        tool_result_pattern = re.compile(r"turn\s+\d+\s+tool_result:\s*\w+\s+ok", re.IGNORECASE)
        tool_result_matches = [l for l in all_logs if tool_result_pattern.search(l)]

        assert tool_use_matches, (
            f"No 'turn N tool_use: Bash' log lines found in worker logs. "
            f"Checked {len(all_logs)} log lines in last {window}s window. "
            f"Regression of #119 (per-tool observability)."
        )

        assert tool_result_matches, (
            f"No 'turn N tool_result: ... ok' log lines found in worker logs. "
            f"Checked {len(all_logs)} log lines in last {window}s window. "
            f"Regression of #119 (per-tool observability)."
        )
