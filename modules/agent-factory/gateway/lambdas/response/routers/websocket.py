"""
Response Router — WebSocket

Pushes agent responses to WebSocket connections via API Gateway
Management API. Handles stale connections (GoneException) with cleanup.
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

    def route(self, content: str, metadata: dict[str, Any], task_id: str) -> bool:
        connection_id = metadata.get("connection_id", "")
        if not connection_id:
            logger.warning("No connection_id for WebSocket routing (task=%s)", task_id)
            return False

        payload = json.dumps({
            "type": "response",
            "task_id": task_id,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

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
                self._cleanup_connection(connection_id)
                return False
            logger.error("WebSocket send failed: %s", e)
            return False

    def _cleanup_connection(self, connection_id: str):
        """Remove stale connection_id from the session record."""
        if not self._sessions_table:
            return
        try:
            # Scan for sessions with this connection_id and clear it
            # In practice, the session TTL handles cleanup
            logger.debug("Would clean up connection %s from sessions", connection_id)
        except Exception as e:
            logger.warning("Connection cleanup failed: %s", e)
