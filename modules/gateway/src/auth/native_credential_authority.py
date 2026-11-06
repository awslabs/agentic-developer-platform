"""Current native-human authority and real providers for private credential delivery."""

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC
from decimal import Decimal
from urllib.parse import urlsplit

import boto3
from botocore.config import Config as BotoConfig
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import MultipleResultsFound

from src.internal.tool_entitlements import effective_entitlement
from src.internal.tools_policy import ToolsPolicyStore
from src.shared.config import get_settings
from src.shared.identity.workspaces import memberships_for_login
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import TeamMembership, User
from src.shared.models.vault import UserCredential

from .aws_connection_authority import verified_connection_evidence
from .cognito_jwt import get_cognito_validator
from .native_credential_wire import AUDIENCE, VERSION, digest


def refused(code, status=403):
    return HTTPException(status, {"error": code}, headers={"Cache-Control": "no-store"})


def require(condition, code="native_credential_authority_changed", status=403):
    if not condition:
        raise refused(code, status)


def https_origin(value):
    parsed = urlsplit(value)
    return bool(
        value
        and parsed.scheme == "https"
        and parsed.netloc
        and not parsed.username
        and not parsed.password
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and value == f"https://{parsed.netloc}"
    )


@dataclass(frozen=True)
class NativeConfig:
    issuer: str
    key_id: str
    key_arn: str
    approval_origin: str
    native_client_id: str
    browser_client_id: str

    @classmethod
    def configured(cls):
        settings = get_settings()
        value = cls(
            os.getenv("ADP_NATIVE_CREDENTIALS_ISSUER", ""),
            os.getenv("ADP_NATIVE_CREDENTIALS_KEY_ID", ""),
            os.getenv("ADP_NATIVE_CREDENTIALS_KEY_ARN", ""),
            os.getenv("ADP_NATIVE_CREDENTIALS_APPROVAL_ORIGIN", ""),
            settings.cognito_cli_client_id,
            settings.cognito_client_id,
        )
        require(
            os.getenv("ADP_NATIVE_CREDENTIALS_ENABLED") == "true"
            and https_origin(value.issuer)
            and https_origin(value.approval_origin)
            and value.key_id
            and ":key/" in value.key_arn
            and value.native_client_id
            and value.browser_client_id
            and value.native_client_id != value.browser_client_id,
            "native_credentials_unavailable",
            503,
        )
        return value


@dataclass(frozen=True)
class NativeActor:
    sub: str
    tenant_id: str
    client_id: str
    session_id: str
    expires_at: int
    token: str = field(repr=False)
    username: str = ""

    def fresh(self):
        require(time.time() < self.expires_at, "native_session_expired", 401)


class NativeProviders:
    """No SDK retry can create a second provider issuance after an unknown outcome."""

    def __init__(self):
        self.region = get_settings().aws_region

    def client(self, service, region=None):
        return boto3.client(
            service, region_name=region or self.region, config=BotoConfig(connect_timeout=5, read_timeout=15, retries={"total_max_attempts": 1})
        )

    def current_session(self, actor):
        result = self.client("cognito-idp").get_user(AccessToken=actor.token)
        attrs = {a["Name"]: a["Value"] for a in result.get("UserAttributes", [])}
        require(attrs.get("sub") == actor.sub, "native_human_session_required", 401)
        actor.fresh()

    def policy(self, tenant_id, user_id):
        from src.agentauth.routes import get_agent_runtime

        store = get_agent_runtime().store
        return ToolsPolicyStore(table_name=store.table, client=store.client).get(tenant_id=tenant_id, owner_id=user_id)

    def public_key(self, config):
        result = self.client("kms").get_public_key(KeyId=config.key_arn)
        key = serialization.load_der_public_key(result["PublicKey"])
        require(
            result.get("KeyId") == config.key_arn
            and result.get("KeyUsage") == "SIGN_VERIFY"
            and "RSASSA_PSS_SHA_256" in result.get("SigningAlgorithms", [])
            and isinstance(key, rsa.RSAPublicKey)
            and key.key_size >= 2048,
            "native_credentials_unavailable",
            503,
        )
        return key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def sign(self, config, message):
        result = self.client("kms").sign(
            KeyId=config.key_arn, Message=hashlib.sha256(message).digest(), MessageType="DIGEST", SigningAlgorithm="RSASSA_PSS_SHA_256"
        )
        require(result.get("KeyId") == config.key_arn, "native_credentials_unavailable", 503)
        return result["Signature"]

    def secret_version(self, arn):
        response = self.secret_client(arn).describe_secret(SecretId=arn)
        versions = [key for key, stages in response.get("VersionIdsToStages", {}).items() if "AWSCURRENT" in stages]
        require(len(versions) == 1, "native_credential_version_changed")
        return versions[0]

    def secret(self, arn, version):
        response = self.secret_client(arn).get_secret_value(SecretId=arn, VersionId=version)
        require(response.get("VersionId") == version and set(response) & {"SecretString", "SecretBinary"}, "native_credential_version_changed")
        return response["SecretString"].encode() if "SecretString" in response else bytes(response["SecretBinary"])

    def secret_client(self, arn):
        match = re.fullmatch(r"arn:aws(?:-[a-z]+)*:secretsmanager:([a-z0-9-]+):[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+", arn)
        require(match is not None, "native_credential_type_unavailable")
        return self.client("secretsmanager", match[1])

    def assume(self, *, role_arn, external_id, user_id, receipt_id):
        from .broker_session import broker_session_context

        name, tags = broker_session_context(user_id=user_id, agent_id=None, task_id=None, human_receipt_id=receipt_id)
        result = self.client("sts").assume_role(RoleArn=role_arn, ExternalId=external_id, RoleSessionName=name, DurationSeconds=900, Tags=tags)
        # Verify AWS's returned role identity before accepting the credentials.
        from types import SimpleNamespace

        from .aws_connection_authority import require_assumed_identity

        require_assumed_identity(SimpleNamespace(assumed_role_arn=result.get("AssumedRoleUser", {}).get("Arn")), role_arn)
        return result["Credentials"]


async def authenticated(request: Request, config: NativeConfig, providers: NativeProviders, *, browser=False):
    headers = request.headers.getlist("authorization")
    require(len(headers) == 1 and headers[0].startswith("Bearer "), "native_human_session_required", 401)
    token = headers[0][7:]
    require(len(token) <= 16384 and not token.startswith("adpctx1~"), "native_human_session_required", 401)
    try:
        claims = await asyncio.to_thread(get_cognito_validator().validate_token, token)
        client_id = config.browser_client_id if browser else config.native_client_id
        require(
            claims.token_use == "access"
            and claims.client_id == client_id
            and claims.account_type in {None, "human"}
            and claims.username
            and claims.org_id
            and claims.jti
            and claims.auth_time > 0,
            "native_human_session_required",
            401,
        )
        actor = NativeActor(
            claims.sub,
            claims.org_id,
            claims.client_id,
            digest(dict(issuer=claims.iss, client_id=claims.client_id, sub=claims.sub, jti=claims.jti)),
            claims.exp,
            token,
            username=claims.username,
        )
        actor.fresh()
        await asyncio.to_thread(providers.current_session, actor)
        return actor
    except HTTPException:
        raise
    except Exception:
        raise refused("native_human_session_required", 401) from None


async def canonical_human(db, actor):
    actor.fresh()
    # Workspace placements keep the unique Cognito subject on the login row.
    # Require that real login first: the shared resolver's legacy user-id alias is
    # not sufficient authority for native credential delivery.
    login = await db.scalar(
        select(User)
        .where(User.cognito_sub == actor.sub, User.user_kind == "human", User.is_shadow.is_(False))
        .execution_options(populate_existing=True)
    )
    require(login is not None, "native_current_membership_required")
    try:
        resolved_login, memberships = await memberships_for_login(db, actor.sub, username=actor.username, refresh=True)
    except (ValueError, MultipleResultsFound):
        raise refused("native_current_membership_required") from None
    require(resolved_login is not None and resolved_login.id == login.id, "native_current_membership_required")
    placement = memberships.get(actor.tenant_id)
    require(placement is not None, "native_current_membership_required")
    row = await db.scalar(
        select(User)
        .join(TenantMembership, TenantMembership.user_id == User.id)
        .join(TeamMembership, TeamMembership.user_id == User.id)
        .where(
            User.org_id == actor.tenant_id,
            User.id == placement[0].id,
            User.user_kind == "human",
            User.is_shadow.is_(False),
            TenantMembership.tenant_id == actor.tenant_id,
            TenantMembership.is_active.is_(True),
            TenantMembership.revoked_at.is_(None),
            TeamMembership.org_id == actor.tenant_id,
            TeamMembership.team_id == User.team_id,
        )
        .execution_options(populate_existing=True)
    )
    require(row is not None, "native_current_membership_required")
    actor.fresh()
    return row


def credential_fingerprint(row):
    fields = {
        key: str(getattr(row, key) or "")
        for key in (
            "id",
            "org_id",
            "user_id",
            "team_id",
            "domain_app_id",
            "service",
            "credential_type",
            "label",
            "secret_arn",
            "provider",
            "strict",
            "agent_consent",
            "aws_external_id",
            "aws_verified_version_id",
            "aws_verified_binding",
            "aws_verification_attempt",
        )
    }
    for key in ("expires_at", "aws_verified_at"):
        value = getattr(row, key)
        fields[key] = value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).isoformat() if value else ""
    fields["scopes"] = json.dumps(row.scopes, sort_keys=True, separators=(",", ":"))
    return digest(fields)


async def current_authority(db, *, actor, providers, credential_id, operation, service, label):
    user = await canonical_human(db, actor)
    policy = await asyncio.to_thread(providers.policy, user.org_id, user.id)
    permission = "credentials." + operation
    require(isinstance(policy, dict) and policy.get("status") == "active", "native_tool_not_consented")
    for key in ("allowed_tools", "consented_tools"):
        values = policy.get(key)
        require(type(values) is list and all(type(value) is str for value in values) and permission in values, "native_tool_not_consented")
    revision = policy.get("revision")
    require(
        type(revision) in {int, Decimal} and (type(revision) is int or revision.is_finite()) and revision >= 0 and revision == int(revision),
        "native_tool_not_consented",
    )
    entitled, _ = await effective_entitlement(db, tenant_id=user.org_id, team_id=user.team_id, tool_name=permission)
    require(entitled, "native_tool_not_entitled")
    credential = await db.scalar(
        select(UserCredential)
        .where(
            UserCredential.id == credential_id,
            UserCredential.org_id == user.org_id,
            UserCredential.user_id == user.id,
            UserCredential.team_id.is_(None),
            UserCredential.domain_app_id.is_(None),
            UserCredential.service == service,
            UserCredential.label == label,
        )
        .execution_options(populate_existing=True)
    )
    require(credential is not None and credential.agent_consent is True, "native_selected_credential_not_consented")
    require(
        credential.expires_at is None or credential.expires_at.replace(tzinfo=credential.expires_at.tzinfo or UTC).timestamp() > time.time(),
        "native_credential_expired",
    )
    from src.shared.domain_provider_contract import reserved

    require(not reserved(credential.id), "native_credential_type_unavailable")
    if operation == "assume_role":
        require(credential.credential_type == "aws_role", "native_credential_type_unavailable")
        verified_connection_evidence(credential)
        require(
            credential.expires_at is None
            or credential.expires_at.replace(tzinfo=credential.expires_at.tzinfo or UTC).timestamp() > time.time() + 900,
            "native_credential_lifetime_unavailable",
        )
    else:
        require(credential.credential_type in {"ssh_key", "certificate", "config_file"}, "native_credential_type_unavailable")
    actor.fresh()
    return user, credential, str(int(revision))


async def public_configuration(config, providers):
    key = await asyncio.to_thread(providers.public_key, config)
    return dict(
        version=VERSION, issuer=config.issuer, audience=AUDIENCE, key_id=config.key_id, issuer_key_pem=key, approval_origin=config.approval_origin
    )
