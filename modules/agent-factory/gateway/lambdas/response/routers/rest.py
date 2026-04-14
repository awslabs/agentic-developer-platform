"""
Response Router — REST/CLI

Writes agent responses to DynamoDB for CLI polling.
CLI clients poll GET /agent/tasks/{task_id} to retrieve results.
"""

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class RestRouter:
    def __init__(self, sessions_table: Any):
        self._sessions_table = sessions_table

    def route(self, content: str, metadata: dict[str, Any], task_id: str) -> bool:
        """Write response to DynamoDB for client polling."""
        if not self._sessions_table:
            logger.warning("No sessions table for REST routing (task=%s)", task_id)
            return False

        session_id = metadata.get("session_id", "")
        if not session_id:
            logger.warning("No session_id for REST routing (task=%s)", task_id)
            return False

        try:
            self._sessions_table.update_item(
                Key={"session_id": session_id},
                UpdateExpression=(
                    "SET last_response = :r, response_status = :s, "
                    "response_task_id = :t, updated_at = :u"
                ),
                ExpressionAttributeValues={
                    ":r": content[:10000],
                    ":s": "complete",
                    ":t": task_id,
                    ":u": int(time.time()),
                },
            )
            logger.info("REST response stored for polling (task=%s session=%s)", task_id, session_id)
            return True
        except Exception as e:
            logger.error("REST routing failed: %s (task=%s)", e, task_id)
            return False
