"""
Response Router — WebSocket

Pushes agent responses to WebSocket connections via API Gateway
Management API. Resolves the *active* connection_id from the sessions
table so reconnected clients receive in-flight replies.  Handles stale
connections (GoneException) with cleanup.

Frame chunking (Issue #85, Problem A)
-------------------------------------
API Gateway WebSocket silently drops frames near the single-frame ceiling
(~32 KB with permessage-deflate). To guarantee delivery of large responses
(e.g. a 27 KB itinerary), ``route()`` splits any JSON-wrapped payload
exceeding ``MAX_FRAME_BYTES`` (24 KB) into numbered chunks:

    { "type": "response",
      "task_id": "...",
      "content": "<chunk>",
      "chunk_index": 1,
      "chunk_total": 3,
      "timestamp": "..." }

Client reassembly contract:
- Frames *without* ``chunk_total`` are complete on their own (backward-compat).
- When ``chunk_total > 1``, the client concatenates ``content`` from frames
  with matching ``task_id`` in ``chunk_index`` order.  The last chunk has
  ``chunk_index == chunk_total``.
- This applies to ``response``, ``notification``, and ``progress`` frame types.

Downstream channels (Slack, REST, future WhatsApp) should follow the same
shape if they ever need to split payloads.
"""

import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Conservative max frame size in bytes.  API Gateway's hard limit is 128 KB,
# but frames >32 KB can be silently dropped by permessage-deflate fragmentation.
# 24 KB gives headroom for JSON envelope overhead (~500 bytes) plus safety margin.
MAX_FRAME_BYTES = 24 * 1024  # 24 KB

# Overhead budget for JSON envelope fields (type, task_id, timestamp, chunk_*).
# We reserve this many bytes from MAX_FRAME_BYTES for the non-content fields,
# then fill the rest with content.
_ENVELOPE_OVERHEAD = 512


class WebSocketRouter:
    def __init__(self, ws_api_endpoint: str, sessions_table: Any = None):
        self._ws_api_endpoint = ws_api_endpoint
        self._sessions_table = sessions_table
        self._client: Any = None

    def _get_client(self):
        if self._client is None:
            endpoint = self._ws_api_endpoint.replace("wss://", "https://")
            self._client = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)
        return self._client

    def _resolve_connection_id(self, metadata: dict[str, Any]) -> str:
        """Look up the *active* connection_id from the sessions table.

        The SQS metadata carries a snapshot of the connection_id captured at
        enqueue time, but the client may have disconnected and reconnected
        since then.  The ingest Lambda writes the fresh connection_id to the
        sessions table on every ``get_or_create_session`` call, so the row
        always holds the current value.

        Falls back to the metadata snapshot when:
        - No sessions table is configured.
        - The session row doesn't exist (TTL expired / race).
        - The session row has no ``connection_id`` field.
        """
        fallback = metadata.get("connection_id", "")
        session_id = metadata.get("session_id", "")

        if not self._sessions_table or not session_id:
            return fallback

        try:
            resp = self._sessions_table.get_item(
                Key={"session_id": session_id},
                ProjectionExpression="connection_id",
                ConsistentRead=True,
            )
            item = resp.get("Item", {})
            active = item.get("connection_id", "")
            if active:
                if active != fallback:
                    logger.info(
                        "Resolved active connection_id=%s (metadata had %s) for session=%s",
                        active, fallback, session_id,
                    )
                return active
        except Exception as e:
            logger.warning("Session lookup failed for %s, using metadata connection_id: %s", session_id, e)

        return fallback

    def route(self, content: str, metadata: dict[str, Any], task_id: str) -> bool:
        """Route a response frame to the WebSocket client.

        Builds the JSON frame, checks if it exceeds MAX_FRAME_BYTES, and
        either sends it as-is or splits content into numbered chunks.
        Returns True if all frames were sent successfully.
        """
        connection_id = self._resolve_connection_id(metadata)
        if not connection_id:
            logger.warning("No connection_id for WebSocket routing (task=%s)", task_id)
            return False

        # Default frame type is `response` (final reply). Progress frames set
        # response_type=progress + carry kind/turn so the UI can render them
        # as ephemeral status lines instead of appending to the transcript.
        frame_type = metadata.get("response_type", "response")
        timestamp = datetime.now(timezone.utc).isoformat()

        # Build extra fields for progress frames.
        extra: dict[str, Any] = {}
        if frame_type == "progress":
            if metadata.get("progress_kind"):
                extra["kind"] = metadata["progress_kind"]
            if metadata.get("progress_turn"):
                extra["turn"] = metadata["progress_turn"]
        # Forward the upstream status ("completed" / "failed" / "notification")
        # so clients can distinguish the final reply from intermediate acks
        # without relying on content heuristics.
        if metadata.get("status"):
            extra["status"] = metadata["status"]

        # Build the full frame to measure its size.
        frame: dict[str, Any] = {
            "type": frame_type,
            "task_id": task_id,
            "content": content,
            "timestamp": timestamp,
            **extra,
        }
        payload = json.dumps(frame)

        # If the payload fits in a single frame, send as-is (no chunk_* fields).
        if len(payload.encode("utf-8")) <= MAX_FRAME_BYTES:
            return self._send_frame(payload, connection_id, task_id, metadata)

        # Split content into chunks that each keep the wrapped frame under limit.
        chunks = self._split_content(content, frame_type, task_id, timestamp, extra)
        logger.info(
            "Splitting large payload (%d bytes) into %d chunks for task=%s",
            len(payload.encode("utf-8")), len(chunks), task_id,
        )

        all_ok = True
        for chunk_payload in chunks:
            if not self._send_frame(chunk_payload, connection_id, task_id, metadata):
                all_ok = False
                break  # Stop sending on first failure (connection gone)
        return all_ok

    def _split_content(
        self,
        content: str,
        frame_type: str,
        task_id: str,
        timestamp: str,
        extra: dict[str, Any],
    ) -> list[str]:
        """Split content into chunks, each yielding a JSON payload under MAX_FRAME_BYTES."""
        # Calculate max content bytes per chunk.  We measure the envelope size
        # with a sample chunk frame (chunk_index/chunk_total add ~40 bytes).
        max_content_bytes = MAX_FRAME_BYTES - _ENVELOPE_OVERHEAD

        # Encode content to UTF-8, then split by byte boundaries that respect
        # character boundaries (don't split multi-byte chars).
        content_bytes = content.encode("utf-8")
        total_bytes = len(content_bytes)
        chunk_count = max(1, math.ceil(total_bytes / max_content_bytes))

        # Split content into roughly equal byte-sized pieces.
        chunk_contents: list[str] = []
        offset = 0
        for i in range(chunk_count):
            # Target end position for this chunk.
            target_end = offset + max_content_bytes
            if target_end >= total_bytes:
                # Last chunk gets everything remaining.
                chunk_contents.append(content_bytes[offset:].decode("utf-8", errors="replace"))
                break
            # Walk back from target_end to find a valid UTF-8 boundary.
            end = target_end
            while end > offset and (content_bytes[end] & 0xC0) == 0x80:
                end -= 1
            if end == offset:
                # Extremely unlikely: single char > max_content_bytes. Force split.
                end = target_end
            chunk_contents.append(content_bytes[offset:end].decode("utf-8", errors="replace"))
            offset = end

        # Build the JSON payloads.
        chunk_total = len(chunk_contents)
        payloads: list[str] = []
        for idx, chunk_text in enumerate(chunk_contents, start=1):
            frame: dict[str, Any] = {
                "type": frame_type,
                "task_id": task_id,
                "content": chunk_text,
                "chunk_index": idx,
                "chunk_total": chunk_total,
                "timestamp": timestamp,
                **extra,
            }
            payloads.append(json.dumps(frame))
        return payloads

    def _send_frame(
        self,
        payload: str,
        connection_id: str,
        task_id: str,
        metadata: dict[str, Any],
    ) -> bool:
        """Send a single JSON payload to the WebSocket connection."""
        try:
            self._get_client().post_to_connection(
                ConnectionId=connection_id,
                Data=payload.encode("utf-8"),
            )
            logger.info("Sent to WebSocket connection=%s (task=%s)", connection_id, task_id)
            return True
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "GoneException":
                logger.info("WebSocket connection %s is gone — cleaning up", connection_id)
                self._cleanup_connection(connection_id, metadata.get("session_id", ""))
                return False
            logger.error("WebSocket send failed: %s", e)
            return False

    def _cleanup_connection(self, connection_id: str, session_id: str = ""):
        """Remove stale connection_id from the session record.

        Clears the ``connection_id`` field so the next delivery attempt will
        resolve a fresh value (or fall back gracefully).
        """
        if not self._sessions_table or not session_id:
            return
        try:
            # Only clear if the stored value still matches the stale one — a
            # concurrent reconnect may have already written a fresh id.
            self._sessions_table.update_item(
                Key={"session_id": session_id},
                UpdateExpression="REMOVE connection_id",
                ConditionExpression="connection_id = :stale",
                ExpressionAttributeValues={":stale": connection_id},
            )
            logger.info("Cleared stale connection_id=%s from session=%s", connection_id, session_id)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                logger.debug("connection_id already updated for session=%s — skip cleanup", session_id)
            else:
                logger.warning("Connection cleanup failed for session=%s: %s", session_id, e)
        except Exception as e:
            logger.warning("Connection cleanup failed for session=%s: %s", session_id, e)
