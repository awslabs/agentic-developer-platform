"""Secrets Manager helper for vault credentials.

Issue #134: Vault Phase 1 — secret-store substrate
Issue #440: Scope relaxation — four ownership namespaces

Secrets are stored under one of four namespaces depending on ownership scope:

    adp/users/<cognito_sub>/<service>-<short_uuid>     # user-scoped
    adp/teams/<team_id>/<service>-<short_uuid>          # team-scoped
    adp/orgs/<org_id>/<service>-<short_uuid>            # org/tenant-scoped
    adp/domain-apps/<app_id>/<org_id>/<service>-<id>   # domain-app, per-tenant

Gateway IAM policy covers all four prefixes.  Agent pods do NOT have direct
Secrets Manager access — reads always route through /internal/v1/proxy-request.

All operations are synchronous (boto3), intended to be called via
``asyncio.to_thread`` from async code paths.
"""

from __future__ import annotations

import hmac
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


class SecretOperationConflictError(Exception):
    """Raised when an idempotent operation name belongs to different input."""


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
    def _build_secret_name(
        service: str,
        *,
        user_sub: str | None = None,
        team_id: str | None = None,
        org_id: str | None = None,
        domain_app_id: str | None = None,
        operation_id: str | None = None,
    ) -> str:
        """Build a namespaced secret name with a short UUID suffix.

        Exactly one of *user_sub*, *team_id*, *org_id*, or *domain_app_id*
        must be provided.  When *domain_app_id* is used, *org_id* is also
        required (domain-app secrets are per-tenant-installed).

        Namespace layout:
            adp/users/<sub>/<service>-<id>
            adp/teams/<team_id>/<service>-<id>
            adp/orgs/<org_id>/<service>-<id>
            adp/domain-apps/<app_id>/<org_id>/<service>-<id>
        """
        # Validate mutual exclusivity of the primary owner identifiers.
        primary_owners = [x for x in (user_sub, team_id, domain_app_id) if x is not None]
        if domain_app_id is not None:
            if org_id is None:
                raise ValueError("org_id is required when domain_app_id is provided.")
            if len(primary_owners) != 1:
                raise ValueError("domain_app_id cannot be combined with user_sub or team_id.")
        elif org_id is not None:
            if len(primary_owners) != 0:
                raise ValueError("Exactly one of user_sub, team_id, org_id, or domain_app_id must be provided.")
        else:
            if len(primary_owners) != 1:
                raise ValueError("Exactly one of user_sub, team_id, org_id, or domain_app_id must be provided.")

        leaf = f"operation-{operation_id}" if operation_id else f"{service}-{uuid.uuid4().hex[:8]}"
        if user_sub is not None:
            return f"adp/users/{user_sub}/{leaf}"
        if team_id is not None:
            return f"adp/teams/{team_id}/{leaf}"
        if domain_app_id is not None:
            return f"adp/domain-apps/{domain_app_id}/{org_id}/{leaf}"
        # org-scoped
        return f"adp/orgs/{org_id}/{leaf}"

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
        service: str,
        label: str,
        payload: str | dict,
        *,
        user_sub: str | None = None,
        team_id: str | None = None,
        org_id: str | None = None,
        domain_app_id: str | None = None,
        operation_id: str | None = None,
        operation_fingerprint: str | None = None,
    ) -> str:
        """Create a new secret and return its ARN.

        Parameters
        ----------
        service : str
            External service name, e.g. ``github``, ``openai``.
        label : str
            Human-readable label for the credential.
        payload : str | dict
            The secret value. Dicts are JSON-serialised before storage.
        user_sub : str, optional
            Cognito ``sub`` of the owning user (user-scoped credential).
        team_id : str, optional
            Team ID (team-scoped credential).
        org_id : str, optional
            Org ID.  Required when ``domain_app_id`` is set; used alone
            for org-scoped credentials.
        domain_app_id : str, optional
            Domain-app identifier (domain-app-scoped credential).

        Returns
        -------
        str
            The ARN of the created secret.

        Raises
        ------
        SecretTooLargeError
            If the serialised payload exceeds 64 KB.
        ValueError
            If the owner specification is invalid.
        """
        if isinstance(payload, dict):
            payload = json.dumps(payload)

        self._validate_payload_size(payload)
        secret_name = self._build_secret_name(
            service,
            user_sub=user_sub,
            team_id=team_id,
            org_id=org_id,
            domain_app_id=domain_app_id,
            operation_id=operation_id,
        )

        # Build owner tag for audit / IAM attribute-based access control.
        if user_sub is not None:
            owner_tag = {"Key": "adp:owner_scope", "Value": "user"}
            owner_id_tag = {"Key": "adp:user_sub", "Value": user_sub}
        elif team_id is not None:
            owner_tag = {"Key": "adp:owner_scope", "Value": "team"}
            owner_id_tag = {"Key": "adp:team_id", "Value": team_id}
        elif domain_app_id is not None:
            owner_tag = {"Key": "adp:owner_scope", "Value": "domain_app"}
            owner_id_tag = {"Key": "adp:domain_app_id", "Value": domain_app_id}
        else:
            owner_tag = {"Key": "adp:owner_scope", "Value": "org"}
            owner_id_tag = {"Key": "adp:org_id", "Value": org_id or ""}

        tags = [
            owner_tag,
            owner_id_tag,
            {"Key": "adp:service", "Value": service},
            {"Key": "adp:label", "Value": label},
        ]
        if operation_id is not None:
            tags.append({"Key": "adp:operation_id", "Value": operation_id})
        if operation_fingerprint is not None:
            tags.append({"Key": "adp:operation_fingerprint", "Value": operation_fingerprint})
        try:
            response = self._client.create_secret(
                Name=secret_name,
                Description=f"Vault credential: {label} ({service})",
                SecretString=payload,
                Tags=tags,
            )
        except ClientError as exc:
            if operation_id is None or exc.response["Error"]["Code"] != "ResourceExistsException":
                raise
            description = self._client.describe_secret(SecretId=secret_name)
            existing_tags = {tag["Key"]: tag["Value"] for tag in description.get("Tags", [])}
            expected_tags = {tag["Key"]: tag["Value"] for tag in tags}
            existing_value = self._client.get_secret_value(SecretId=secret_name).get("SecretString")
            if description.get("DeletedDate") is not None or any(existing_tags.get(key) != value for key, value in expected_tags.items()):
                raise SecretOperationConflictError("Credential operation id is already in use") from exc
            if not hmac.compare_digest(
                (existing_value or "").encode("utf-8"),
                payload.encode("utf-8"),
            ):
                raise SecretOperationConflictError("Credential operation id was retried with a different value") from exc
            response = description
        arn = response["ARN"]
        logger.info("Created secret %s scope=%s service=%s", arn, owner_tag["Value"], service)
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

    def get_secret_at_version(self, secret_arn: str, version_id: str) -> tuple[str, str]:
        """Retrieve a secret value pinned to a specific version, returning (value, actual_version).

        Issue #5528 (F3). ``get_secret`` calls ``get_secret_value`` without a
        ``VersionId``, which always reads ``AWSCURRENT``. During a rotation window,
        ``AWSCURRENT`` may have advanced past the version the Gateway validated, so a
        delivery that calls ``get_secret`` could hand the caller bytes that were never
        validated.

        This method pins the read to ``version_id`` and returns the version the
        Secrets Manager actually served alongside the value. The caller (``deliver_credential``)
        checks the two match before returning material; a mismatch means the version
        changed between the "what is current" lookup and the actual read, which the
        caller must refuse rather than deliver.

        Raises ``botocore.exceptions.ClientError`` (``InvalidRequestException``) when
        the version does not exist or no longer carries any staging label, which the
        caller must treat as a refusal. Raises ``ValueError`` when Secrets Manager
        does not identify the version it actually served.
        """
        response = self._client.get_secret_value(SecretId=secret_arn, VersionId=version_id)
        served_version = response.get("VersionId")
        if not isinstance(served_version, str) or not served_version:
            raise ValueError("Secrets Manager response did not include a non-empty VersionId")
        return response["SecretString"], served_version

    def current_version_id(self, secret_arn: str) -> str | None:
        """Return the id of the secret's CURRENT version, or ``None`` if unknown.

        Issue #5528. Credential evidence has to state *which* stored value is
        current: an evidence record that says "this credential is valid" without
        naming a version cannot distinguish the value that was validated from one
        that replaced it afterwards, which is the rotation-during-execution case.

        Metadata only, deliberately. ``describe_secret`` returns version ids and
        their staging labels and never the value, so adding this cannot become a
        second way to read a secret — unlike ``get_secret`` above, whose result is
        the material itself.

        ``None`` rather than a raise or a guess when no version carries the
        ``AWSCURRENT`` label. "I could not establish the version" and "the version
        is X" are different facts, and the evidence service reports the former as
        unverified rather than substituting a plausible-looking value.
        """
        response = self._client.describe_secret(SecretId=secret_arn)
        stages = response.get("VersionIdsToStages") or {}
        if not isinstance(stages, dict):
            return None
        current = [
            version_id
            for version_id, labels in stages.items()
            if isinstance(version_id, str) and version_id and isinstance(labels, list | tuple) and "AWSCURRENT" in labels
        ]
        return current[0] if len(current) == 1 else None

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
                if code == "InvalidRequestException" and not force:
                    try:
                        description = self._client.describe_secret(SecretId=secret_arn)
                    except ClientError as describe_exc:
                        if describe_exc.response["Error"]["Code"] == "ResourceNotFoundException":
                            logger.warning("Secret %s already deleted", secret_arn)
                            return
                        raise describe_exc from exc
                    if description.get("DeletedDate") is not None:
                        logger.warning("Secret %s is already scheduled for deletion", secret_arn)
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
