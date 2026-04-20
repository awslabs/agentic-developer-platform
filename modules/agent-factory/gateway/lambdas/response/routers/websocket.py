"""
Response Router — WebSocket

Pushes agent responses to WebSocket connections via API Gateway
Management API. Resolves the *active* connection_id from the sessions
table so reconnected clients receive in-flight replies.  Handles stale
connections (GoneException) with cleanup.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


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
        connection_id = self._resolve_connection_id(metadata)
        if not connection_id:
            logger.warning("No connection_id for WebSocket routing (task=%s)", task_id)
            return False

        # Default frame type is `response` (final reply). Progress frames set
        # response_type=progress + carry kind/turn so the UI can render them
        # as ephemeral status lines instead of appending to the transcript.
        frame_type = metadata.get("response_type", "response")
        frame: dict[str, Any] = {
            "type": frame_type,
            "task_id": task_id,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if frame_type == "progress":
            if metadata.get("progress_kind"):
                frame["kind"] = metadata["progress_kind"]
            if metadata.get("progress_turn"):
                frame["turn"] = metadata["progress_turn"]
        payload = json.dumps(frame)

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
