#!/usr/bin/env python3
"""
WebSocket round-trip test client for the agent-gateway.

Usage:
    python3 modules/agent-factory/scripts/ws_roundtrip.py "Your prompt here"

Auto-discovers WS URL + Cognito credentials from Terraform output or
environment variables.  Sends a prompt, prints progress frames as they
arrive, reassembles chunked responses, and exits cleanly when the
terminal frame lands.

Exit codes:
    0  — completed successfully
    1  — task failed or error
    2  — timeout (no terminal frame within --timeout seconds)

Chunk reassembly contract (Issue #85, Problem A):
    - Frames without ``chunk_total`` are complete on their own.
    - When ``chunk_total > 1``, concatenate ``content`` from consecutive
      frames with matching ``task_id`` in ``chunk_index`` order.
    - Last chunk: ``chunk_index == chunk_total``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from typing import Any


def _tf_output(key: str) -> str:
    """Try to read a Terraform output value."""
    try:
        result = subprocess.run(
            ["terraform", "output", "-raw", key],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=os.path.join(
                os.path.dirname(__file__), "..", "infra"
            ),
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def discover_ws_url() -> str:
    """Discover WebSocket URL from env or Terraform."""
    url = os.environ.get("WS_URL", "") or _tf_output("ws_api_endpoint")
    if not url:
        print("ERROR: Set WS_URL or run from a directory with terraform outputs", file=sys.stderr)
        sys.exit(1)
    return url


def discover_token() -> str:
    """Discover auth token from env or Cognito."""
    token = os.environ.get("WS_TOKEN", "")
    if token:
        return token

    # Try Cognito admin auth
    pool_id = os.environ.get("COGNITO_USER_POOL_ID", "") or _tf_output("cognito_user_pool_id")
    client_id = os.environ.get("COGNITO_CLIENT_ID", "") or _tf_output("cognito_client_id")
    email = os.environ.get("TEST_USER_EMAIL", "adp-test@example.com")
    password = os.environ.get("TEST_USER_PASSWORD", "")

    if pool_id and client_id and password:
        try:
            import boto3

            client = boto3.client("cognito-idp", region_name="us-east-1")
            resp = client.admin_initiate_auth(
                UserPoolId=pool_id,
                ClientId=client_id,
                AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": email, "PASSWORD": password},
            )
            return resp["AuthenticationResult"]["AccessToken"]
        except Exception as e:
            print(f"WARNING: Cognito auth failed: {e}", file=sys.stderr)

    print("WARNING: No auth token available. Connection may be rejected.", file=sys.stderr)
    return ""


async def roundtrip(
    ws_url: str,
    token: str,
    prompt: str,
    session_id: str,
    timeout: float = 360.0,
    verbose: bool = False,
) -> int:
    """Send a prompt and wait for the terminal response.

    Returns 0 on success, 1 on failure.
    """
    try:
        import websockets
    except ImportError:
        print("ERROR: pip install websockets", file=sys.stderr)
        return 1

    url = f"{ws_url}?token={token}" if token else ws_url
    start = time.monotonic()

    # Chunk reassembly buffer: task_id -> {chunks: dict[int, str], total: int}
    chunk_buffers: dict[str, dict[str, Any]] = {}

    print(f"[ws] Connecting to {ws_url}...")
    async with websockets.connect(url) as ws:
        # Send the prompt
        payload = json.dumps({
            "action": "message",
            "text": prompt,
            "session_id": session_id,
        })
        await ws.send(payload)
        elapsed = time.monotonic() - start
        print(f"[ws] [{elapsed:6.1f}s] Sent prompt ({len(prompt)} chars)")

        # Receive loop
        while True:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                print(f"[ws] TIMEOUT after {timeout}s — no terminal frame received")
                return 2

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 30.0))
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - start
                print(f"[ws] [{elapsed:6.1f}s] (waiting...)")
                continue

            elapsed = time.monotonic() - start

            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[ws] [{elapsed:6.1f}s] Non-JSON frame: {raw[:200]}")
                continue

            frame_type = frame.get("type", "")
            status = frame.get("status") or frame_type
            task_id = frame.get("task_id", "")
            chunk_index = frame.get("chunk_index")
            chunk_total = frame.get("chunk_total")

            # Print frame summary
            if frame_type == "progress":
                kind = frame.get("kind", "")
                content_preview = (frame.get("content", "") or "")[:80]
                print(f"[ws] [{elapsed:6.1f}s] <progress> kind={kind} {content_preview}")
                continue

            if frame_type == "response" or frame_type == "notification":
                content = frame.get("content", "")

                # Handle chunked frames
                if chunk_total is not None and chunk_total > 1:
                    print(
                        f"[ws] [{elapsed:6.1f}s] <{frame_type}> chunk {chunk_index}/{chunk_total} "
                        f"({len(content)} chars)"
                    )
                    buf = chunk_buffers.setdefault(task_id, {"chunks": {}, "total": chunk_total})
                    buf["chunks"][chunk_index] = content

                    # Not the last chunk yet — keep waiting
                    if chunk_index != chunk_total:
                        continue

                    # Last chunk — reassemble
                    assembled_parts = []
                    for i in range(1, chunk_total + 1):
                        assembled_parts.append(buf["chunks"].get(i, ""))
                    content = "".join(assembled_parts)
                    print(f"[ws] [{elapsed:6.1f}s] Reassembled {chunk_total} chunks ({len(content)} chars)")
                else:
                    content_preview = content[:200]
                    print(
                        f"[ws] [{elapsed:6.1f}s] <{status}> ({len(content)} chars) "
                        f"{content_preview}{'...' if len(content) > 200 else ''}"
                    )

                if verbose:
                    print(f"\n{'='*60}\n{content}\n{'='*60}\n")

            # Terminal condition: either explicit status='completed'/'failed', OR a
            # type='response' frame with non-empty content (final reply) — the WS
            # router doesn't always set a status field.
            # For chunked responses, only terminate when the last chunk arrives.
            is_chunked_incomplete = (
                chunk_total is not None
                and chunk_total > 1
                and chunk_index != chunk_total
            )

            is_terminal = (
                frame.get("status") in ("completed", "failed")
                or (frame_type == "response" and frame.get("content") and not is_chunked_incomplete)
            )

            if is_terminal:
                elapsed = time.monotonic() - start
                final_status = frame.get("status", "completed")
                print(f"[ws] [{elapsed:6.1f}s] Terminal frame received (status={final_status})")
                return 0 if final_status != "failed" else 1

            # Unknown frame type — log and continue
            if frame_type not in ("progress", "response", "notification"):
                print(f"[ws] [{elapsed:6.1f}s] Unknown frame type={frame_type}: {raw[:200]}")


def main():
    parser = argparse.ArgumentParser(description="WebSocket round-trip test client")
    parser.add_argument("prompt", help="The prompt to send")
    parser.add_argument("--session-id", default=f"adhoc-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--timeout", type=float, default=360.0, help="Max wait in seconds (default: 360)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print full response content")
    args = parser.parse_args()

    ws_url = discover_ws_url()
    token = discover_token()

    print(f"[ws] Session: {args.session_id}")
    print(f"[ws] Timeout: {args.timeout}s")

    exit_code = asyncio.run(
        roundtrip(ws_url, token, args.prompt, args.session_id, args.timeout, args.verbose)
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
