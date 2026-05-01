"""Secrets Manager helper for user vault credentials.

Issue #134: Vault Phase 1 — secret-store substrate

Secrets are stored under the namespace:
    adp/users/<cognito_sub>/<service>-<short_uuid>

All operations are synchronous (boto3), intended to be called via
``asyncio.to_thread`` from async code paths.
"""

from __future__ import annotations

import json
import logging
import uuid

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# AWS Secrets Manager payload limit
MAX_SECRET_SIZE_BYTES = 65_536  # 64 KB


class SecretTooLargeError(Exception):
    """Raised when a secret payload exceeds the 64 KB limit."""


class SecretsManagerHelper:
    """CRUD helper for user vault secrets in AWS Secrets Manager.

    Parameters
    ----------
    region_name : str
        AWS region, e.g. ``us-east-1``.
    client : optional
        Pre-built boto3 Secrets Manager client (useful for testing / mocking).
    """

    def __init__(self, region_name: str = "us-east-1", *, client=None):
        self._client = client or boto3.client("secretsmanager", region_name=region_name)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_secret_name(user_sub: str, service: str) -> str:
        """Build a namespaced secret name with a short UUID suffix."""
        short_id = uuid.uuid4().hex[:8]
        return f"adp/users/{user_sub}/{service}-{short_id}"

    @staticmethod
    def _validate_payload_size(payload: str | bytes) -> bytes:
        """Validate and return the payload as bytes, enforcing the 64 KB limit."""
        if isinstance(payload, str):
            data = payload.encode("utf-8")
        else:
            data = payload
        if len(data) > MAX_SECRET_SIZE_BYTES:
            raise SecretTooLargeError(f"Secret payload is {len(data)} bytes, exceeding the {MAX_SECRET_SIZE_BYTES} byte limit.")
        return data

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_secret(
        self,
        user_sub: str,
        service: str,
        label: str,
        payload: str | dict,
    ) -> str:
        """Create a new secret and return its ARN.

        Parameters
        ----------
        user_sub : str
            The Cognito ``sub`` of the owning user.
        service : str
            External service name, e.g. ``github``, ``openai``.
        label : str
            Human-readable label for the credential.
        payload : str | dict
            The secret value. Dicts are JSON-serialised before storage.

        Returns
        -------
        str
            The ARN of the created secret.

        Raises
        ------
        SecretTooLargeError
            If the serialised payload exceeds 64 KB.
        """
        if isinstance(payload, dict):
            payload = json.dumps(payload)

        self._validate_payload_size(payload)
        secret_name = self._build_secret_name(user_sub, service)

        response = self._client.create_secret(
            Name=secret_name,
            Description=f"Vault credential: {label} ({service}) for user {user_sub}",
            SecretString=payload,
            Tags=[
                {"Key": "adp:user_sub", "Value": user_sub},
                {"Key": "adp:service", "Value": service},
                {"Key": "adp:label", "Value": label},
            ],
        )
        arn = response["ARN"]
        logger.info("Created secret %s for user %s / service %s", arn, user_sub, service)
        return arn

    def get_secret(self, secret_arn: str) -> str:
        """Retrieve a secret value by ARN.

        Returns
        -------
        str
            The secret string value.
        """
        response = self._client.get_secret_value(SecretId=secret_arn)
        return response["SecretString"]

    def update_secret(self, secret_arn: str, payload: str | dict) -> None:
        """Update an existing secret's value.

        Raises
        ------
        SecretTooLargeError
            If the serialised payload exceeds 64 KB.
        """
        if isinstance(payload, dict):
            payload = json.dumps(payload)

        self._validate_payload_size(payload)

        self._client.update_secret(
            SecretId=secret_arn,
            SecretString=payload,
        )
        logger.info("Updated secret %s", secret_arn)

    def delete_secret(self, secret_arn: str, *, force: bool = True) -> None:
        """Delete a secret (immediate, no recovery window when *force* is True).

        Parameters
        ----------
        force : bool
            If True, deletes without a recovery window.  Default ``True`` to
            keep test-time cleanup instant.
        """
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                kwargs: dict = {"SecretId": secret_arn}
                if force:
                    kwargs["ForceDeleteWithoutRecovery"] = True
                self._client.delete_secret(**kwargs)
                logger.info("Deleted secret %s", secret_arn)
                return
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code == "ResourceNotFoundException":
                    logger.warning("Secret %s already deleted", secret_arn)
                    return
                if attempt == max_retries:
                    raise
                logger.warning(
                    "Retry %d/%d deleting secret %s: %s",
                    attempt,
                    max_retries,
                    secret_arn,
                    exc,
                )
