"""Secrets Manager helper for webhook ingress Lambdas.

Fetches secrets by ARN at cold start and caches them for the lifetime of
the Lambda execution environment (warm starts reuse the cached value).
"""

import logging
import os

import boto3

logger = logging.getLogger(__name__)

REGION = os.environ.get("AWS_REGION", "us-east-1")

_secrets_cache: dict[str, str] = {}
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("secretsmanager", region_name=REGION)
    return _client


def get_secret(secret_arn: str) -> str:
    """Fetch a secret value from Secrets Manager, with in-memory caching.

    Args:
        secret_arn: The ARN (or name) of the secret to retrieve.

    Returns:
        The plaintext secret string.

    Raises:
        RuntimeError: If the secret cannot be fetched.
    """
    if not secret_arn:
        raise RuntimeError("secret_arn is empty — cannot fetch secret")

    if secret_arn in _secrets_cache:
        return _secrets_cache[secret_arn]

    try:
        client = _get_client()
        resp = client.get_secret_value(SecretId=secret_arn)
        value = resp["SecretString"]
        _secrets_cache[secret_arn] = value
        logger.info("Fetched secret from Secrets Manager: %s", secret_arn)
        return value
    except Exception as e:
        logger.error("Failed to fetch secret %s: %s", secret_arn, e)
        raise RuntimeError(f"Failed to fetch secret {secret_arn}") from e


def clear_cache() -> None:
    """Clear the secrets cache. Useful for testing."""
    _secrets_cache.clear()
