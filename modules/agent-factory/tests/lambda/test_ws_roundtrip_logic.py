"""
Unit tests for ws_roundtrip.py terminal frame detection and chunk reassembly.

Issue #85, Problem C: The client must recognize terminal frames and correctly
reassemble chunked responses before exiting.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add the scripts dir to sys.path so we can import the module
SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts"
)
sys.path.insert(0, SCRIPTS_DIR)


class FakeWebSocket:
    """Fake WebSocket that yields pre-configured frames then closes."""

    def __init__(self, frames: list[dict]):
        self._frames = [json.dumps(f) for f in frames]
        self._index = 0

    async def send(self, data: str):
        pass

    async def recv(self):
        if self._index >= len(self._frames):
            # Simulate connection close
            raise Exception("Connection closed")
        frame = self._frames[self._index]
        self._index += 1
        return frame

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class TestTerminalFrameDetection:
    """Problem C: roundtrip() must exit on the right frame."""

    @pytest.mark.asyncio
    async def test_single_response_frame_terminates(self):
        """A single type=response frame with content should terminate with exit 0."""
        # Import fresh to avoid stale module state
        import importlib
        import ws_roundtrip

        importlib.reload(ws_roundtrip)

        frames = [
            {"type": "response", "task_id": "t1", "content": "Hello, world!", "timestamp": "2026-04-21T00:00:00Z"},
        ]

        fake_ws = FakeWebSocket(frames)
        with patch("ws_roundtrip.websockets", create=True) as mock_ws_mod:
            # We need to test the roundtrip function directly
            # Since it uses `websockets.connect`, we mock it
            pass

        # Test the logic directly by calling roundtrip with a mock
        result = await _run_roundtrip_with_frames(ws_roundtrip, frames)
        assert result == 0

    @pytest.mark.asyncio
    async def test_status_completed_terminates(self):
        """An explicit status=completed frame should terminate with exit 0."""
        import importlib
        import ws_roundtrip

        importlib.reload(ws_roundtrip)

        frames = [
            {"type": "response", "task_id": "t1", "content": "Done!", "status": "completed", "timestamp": "2026-04-21T00:00:00Z"},
        ]

        result = await _run_roundtrip_with_frames(ws_roundtrip, frames)
        assert result == 0

    @pytest.mark.asyncio
    async def test_status_failed_terminates_with_1(self):
        """A status=failed frame should terminate with exit 1."""
        import importlib
        import ws_roundtrip

        importlib.reload(ws_roundtrip)

        frames = [
            {"type": "response", "task_id": "t1", "content": "Error occurred", "status": "failed", "timestamp": "2026-04-21T00:00:00Z"},
        ]

        result = await _run_roundtrip_with_frames(ws_roundtrip, frames)
        assert result == 1

    @pytest.mark.asyncio
    async def test_progress_frames_do_not_terminate(self):
        """Progress frames should be skipped; only the final response terminates."""
        import importlib
        import ws_roundtrip

        importlib.reload(ws_roundtrip)

        frames = [
            {"type": "progress", "task_id": "t1", "content": "thinking...", "kind": "heartbeat", "timestamp": "2026-04-21T00:00:00Z"},
            {"type": "progress", "task_id": "t1", "content": "Searching...", "kind": "tool_use", "timestamp": "2026-04-21T00:00:01Z"},
            {"type": "response", "task_id": "t1", "content": "Here's your answer", "timestamp": "2026-04-21T00:00:02Z"},
        ]

        result = await _run_roundtrip_with_frames(ws_roundtrip, frames)
        assert result == 0

    @pytest.mark.asyncio
    async def test_chunked_response_waits_for_last_chunk(self):
        """Two chunked frames — should buffer and terminate on the last chunk."""
        import importlib
        import ws_roundtrip

        importlib.reload(ws_roundtrip)

        frames = [
            {
                "type": "response",
                "task_id": "t1",
                "content": "First half of the response. ",
                "chunk_index": 1,
                "chunk_total": 2,
                "timestamp": "2026-04-21T00:00:00Z",
            },
            {
                "type": "response",
                "task_id": "t1",
                "content": "Second half of the response.",
                "chunk_index": 2,
                "chunk_total": 2,
                "timestamp": "2026-04-21T00:00:00Z",
            },
        ]

        result = await _run_roundtrip_with_frames(ws_roundtrip, frames)
        assert result == 0


async def _run_roundtrip_with_frames(ws_roundtrip_module, frames: list[dict]) -> int:
    """Helper: run roundtrip() with a fake WebSocket that yields the given frames."""

    frame_index = 0

    class FakeWS:
        async def send(self, data):
            pass

        async def recv(self):
            nonlocal frame_index
            if frame_index >= len(frames):
                # Should not reach here if terminal detection works
                await asyncio.sleep(999)
            result = json.dumps(frames[frame_index])
            frame_index += 1
            return result

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    fake_ws = FakeWS()

    # Patch websockets.connect to return our fake
    with patch.dict("sys.modules", {"websockets": MagicMock()}):
        # Re-import to get a fresh copy
        import importlib

        # We need websockets available inside the function
        ws_mod = MagicMock()
        ws_mod.connect.return_value = fake_ws
        sys.modules["websockets"] = ws_mod

        importlib.reload(ws_roundtrip_module)

        result = await ws_roundtrip_module.roundtrip(
            ws_url="wss://fake.execute-api.us-east-1.amazonaws.com/v1",
            token="fake-token",
            prompt="Test prompt",
            session_id="test-session",
            timeout=10.0,
            verbose=False,
        )

    return result
