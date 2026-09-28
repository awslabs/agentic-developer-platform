"""
Response Router — Slack

Posts agent responses to Slack channels/threads using the Slack Web API.
Retrieves bot token from AWS Secrets Manager with in-memory caching.
"""

import json
import logging
import time
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
_TOKEN_CACHE_TTL = 300  # 5 minutes


class SlackRouter:
    def __init__(self, secrets_client: Any, environment: str = "dev"):
        self._secrets_client = secrets_client
        self._environment = environment
        self._token_cache: dict[str, Any] = {"token": None, "expires_at": 0}

    def route(self, content: str, metadata: dict[str, Any], task_id: str) -> bool:
        channel_id = metadata.get("channel_id", "")
        thread_ts = metadata.get("thread_ts", metadata.get("thread_id", ""))

        if not channel_id:
            logger.warning("No channel_id for Slack routing (task=%s)", task_id)
            return False

        token = self._get_token()
        if not token:
            logger.error("No Slack token — cannot route response (task=%s)", task_id)
            return False

        payload = json.dumps({
            "channel": channel_id,
            "thread_ts": thread_ts,
            "text": content,
        }).encode("utf-8")

        req = urllib.request.Request(
            _SLACK_POST_MESSAGE_URL,
            data=payload,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
        )

        try:
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("ok"):
                    logger.info("Slack message sent to %s (task=%s)", channel_id, task_id)
                    return True
                else:
                    logger.error("Slack API error: %s (task=%s)", result.get("error"), task_id)
                    return False
        except Exception as e:
            logger.error("Slack send failed: %s (task=%s)", e, task_id)
            return False

    def _get_token(self) -> str | None:
        """Get Slack bot token from Secrets Manager with caching."""
        now = time.time()
        if self._token_cache["token"] and now < self._token_cache["expires_at"]:
            return self._token_cache["token"]

        try:
            secret_id = f"adp/{self._environment}/slack-bot-token"
            resp = self._secrets_client.get_secret_value(SecretId=secret_id)
            token = resp["SecretString"]
            self._token_cache = {"token": token, "expires_at": now + _TOKEN_CACHE_TTL}
            logger.info("Refreshed Slack token from Secrets Manager")
            return token
        except Exception as e:
            logger.error("Failed to get Slack token: %s", e)
            return None
