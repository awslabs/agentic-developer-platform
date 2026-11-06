"""Security regressions exercise real matching and bounded worker admission."""

import asyncio
import subprocess
import sys
import threading
import time

import pytest

from src.chat_logging.scrub_executor import ScrubCapacityError, run_scrub
from src.chat_logging.scrubber import SCRUB_LIMIT_PLACEHOLDER, RegexScrubber


@pytest.mark.parametrize("kind", ["", "RSA ", "EC ", "DSA ", "OPENSSH ", "ENCRYPTED "])
def test_key_blocks_and_unterminated_keys_are_redacted(kind):
    start, end = f"-----BEGIN {kind}PRIVATE KEY-----", f"-----END {kind}PRIVATE KEY-----"
    scrubber = RegexScrubber()
    assert scrubber.scrub_text(f"before {start} secret {end} after").content == "before [REDACTED:PRIVATE_KEY] after"
    assert scrubber.scrub_text(f"before {start} secret").content == "before [REDACTED:PRIVATE_KEY]"


def test_repeated_openers_finish_in_bounded_subprocess():
    # The old regex takes several seconds on 112 KB. A hard subprocess timeout
    # makes a regression fail instead of wedging the test runner/event loop.
    code = """
from src.chat_logging.scrubber import RegexScrubber
s = RegexScrubber()
for n in (40, 400, 4000):
    assert s.scrub_text('-----BEGIN PRIVATE KEY-----\\n' * n).content == '[REDACTED:PRIVATE_KEY]'
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=2)


def test_oversize_traversal_and_depth_never_pass_raw_content():
    s = RegexScrubber()
    s.max_string_chars = 30
    assert s.scrub_text("secret " * 100).content == SCRUB_LIMIT_PLACEHOLDER
    s.max_items = 3
    assert "private" not in str(s.scrub_dict({"values": ["private"] * 10}).content)
    s.max_items = 100
    assert "private" not in str(s.scrub_dict({"nested": {"key": "private"}}, max_depth=0).content)
    s.max_total_chars = 10
    assert "private" not in str(s.scrub_dict({"a": "private", "b": "private"}).content)


async def test_regex_timeout_releases_event_loop_and_redacts_entire_value():
    s = RegexScrubber({"adversarial": (r"(a+)+$", "unused")})
    s.timeout_seconds = 0.08
    ticks = []
    stop = False

    async def heartbeat():
        while not stop:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.005)

    task = asyncio.create_task(heartbeat())
    try:
        result = await run_scrub(s.scrub_text, "a" * 100_000 + "!")
        assert result.content == SCRUB_LIMIT_PLACEHOLDER
        assert len(ticks) >= 3
        assert max(b - a for a, b in zip(ticks, ticks[1:])) < 0.1
    finally:
        stop = True
        await task


async def test_cancellation_does_not_release_capacity_while_cpu_work_runs():
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()

    def block(index):
        entered[index].set()
        assert release.wait(5)

    tasks = [asyncio.create_task(run_scrub(block, i)) for i in range(2)]
    try:
        for event in entered:
            assert await asyncio.to_thread(event.wait, 2)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        with pytest.raises(ScrubCapacityError):
            await run_scrub(lambda: None)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
