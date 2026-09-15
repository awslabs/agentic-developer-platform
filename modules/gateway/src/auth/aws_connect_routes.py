"""AWS account connect flow — CloudFormation Quick-Create pattern.

Issue #562: Self-serve AWS account connect UI.

Endpoints:
  POST /auth/credentials/aws/connect — start a connect flow (pending credential)
  POST /auth/credentials/aws/verify  — verify the stack was created (STS AssumeRole)
  POST /auth/credentials/aws/import  — register/reuse a role that already exists (#5182)
  GET  /auth/credentials/aws/{id}/setup — re-read a saved connection's setup (#5182)

Reuses existing endpoints for list (GET /auth/credentials) and delete
(DELETE /auth/credentials/{id}) — no reimplementation needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.internal.sts_assume_service import STSAssumeError, assume_role
from src.shared.database import get_db
from src.shared.models.vault import UserCredential
from src.shared.schemas.auth import TokenContext
from src.shared.services.routing_probe import (
    ROUTING_REASON_PROBE_INCONCLUSIVE as _ROUTING_REASON_PROBE_INCONCLUSIVE,
)
from src.shared.services.routing_probe import (
    ROUTING_REASON_USER_PINNED as _ROUTING_REASON_USER_PINNED,
)
from src.shared.services.routing_probe import (
    probe_assumable_for_any_principal,
)
from src.shared.services.secrets_manager import SecretsManagerHelper

from .aws_connect_setup import connect_setup_download
from .cfn_template import build_launch_url, compute_role_arn, read_role_template
from .middleware import get_current_user_context
from .org_id_resolver import resolve_effective_org_id
from .vault_routes import get_secrets_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/credentials/aws", tags=["aws-connect"])


async def _resolve_user_id(cognito_sub: str, db: AsyncSession, *, org_id: str = "", username: str = "") -> str:
    """Resolve a Cognito sub (what TokenContext.user_id actually holds) to
    the Postgres `users.id` UUID required by user_credentials.user_id FK.

    Raises 404 if no matching Postgres user exists (shouldn't happen for a
    registered user — but defensive in case someone signed in without going
    through onboarding).
    """
    from src.shared.identity.workspaces import login_user, workspace_user

    user = await workspace_user(db, cognito_sub, org_id, username=username) if org_id else await login_user(db, cognito_sub)
    if user is None:
        raise HTTPException(
            status_code=404,
            detail={
                "reason": "user_not_found",
                "hint": "Your account isn't registered in this tenant yet. Complete onboarding first.",
            },
        )
    return user.id


async def _owned_connection(credential_id: str, db: AsyncSession, db_user_id: str, org_id: str) -> UserCredential:
    """Fetch one personal AWS connection **owned by this caller**, or 404.

    Ownership is expressed as part of the query rather than as a comparison
    afterwards: another user's — or another org's — connection id simply does not
    match, so there is no state to leak and no check to forget. Issue #5182 needs
    exactly this lookup on three paths (verify, setup, import reuse), and three
    hand-copied WHERE clauses is how one of them ends up missing a column.
    """
    cred = await db.scalar(
        select(UserCredential).where(
            UserCredential.id == credential_id,
            UserCredential.user_id == db_user_id,
            UserCredential.org_id == org_id,
            UserCredential.credential_type == "aws_role",
        )
    )
    if cred is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Credential not found"},
        )
    return cred


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


def _validate_account_id(v: str) -> str:
    """AWS account IDs are 12 digits."""
    if not v.isdigit() or len(v) != 12:
        raise ValueError("AWS account IDs must be exactly 12 digits")
    return v


def _validate_nickname(v: str) -> str:
    """Nickname must be non-empty and reasonable length."""
    v = v.strip()
    if not v:
        raise ValueError("Nickname cannot be empty")
    if len(v) > 64:
        raise ValueError("Nickname must be 64 characters or fewer")
    return v


class ConnectStartRequest(BaseModel):
    """Body for POST /auth/credentials/aws/connect."""

    nickname: str
    account_id: str
    role_name: str = "ADP-Agent-Role"

    validate_account_id = field_validator("account_id")(_validate_account_id)
    validate_nickname = field_validator("nickname")(_validate_nickname)


class ConnectStartResponse(BaseModel):
    credential_id: str
    launch_url: str


class ConnectVerifyRequest(BaseModel):
    """Body for POST /auth/credentials/aws/verify."""

    credential_id: str
    #: Issue #5182. Re-run the real assume even on an already-verified row.
    #: ``adp aws verify`` exists to answer "does this work *now*", which a
    #: replayed verdict cannot. Defaults False so the UI's connect flow — where
    #: a cached success is exactly the right answer to a double-click — is
    #: unchanged.
    fresh: bool = False


class ConnectImportRequest(BaseModel):
    """Body for POST /auth/credentials/aws/import (Issue #5182).

    Registers a role the user's account **already has**, instead of provisioning
    one. ``external_id`` is optional because a role may trust ADP without the
    confused-deputy guard; when supplied it is stored and used for every later
    assume, exactly as a provisioned role's generated one is.
    """

    nickname: str
    account_id: str
    role_arn: str
    external_id: str | None = None
    default_region: str = "us-east-1"

    validate_account_id = field_validator("account_id")(_validate_account_id)
    validate_nickname = field_validator("nickname")(_validate_nickname)

    @field_validator("role_arn")
    @classmethod
    def validate_role_arn(cls, v: str) -> str:
        """An IAM role ARN, and nothing else — not a user, not a wildcard."""
        v = v.strip()
        if not re.fullmatch(r"arn:aws(?:-[a-z]+)*:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}", v):
            raise ValueError("role_arn must be an IAM role ARN, for example arn:aws:iam::123456789012:role/MyRole")
        return v

    @field_validator("default_region")
    @classmethod
    def validate_region(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", v):
            raise ValueError("default_region must be an AWS region, for example us-east-1")
        return v


class ConnectImportResponse(BaseModel):
    credential_id: str
    account_id: str
    role_arn: str
    #: True when an existing connection for the same role was returned rather
    #: than a new one created. A retried import must not leave two connections
    #: to one role behind (contract §7 item 5).
    reused: bool


class ConnectSetupResponse(BaseModel):
    """Setup material for a saved connection (Issue #5182).

    Deliberately not a general secret export: the only sensitive value it returns
    is the ExternalId of the caller's own pending/verified connection, which is
    already inside the launch URL the connect flow hands the same caller, and
    which is useless without the ability to create a role in their account.
    """

    credential_id: str
    account_id: str
    role_arn: str
    region: str
    status: str
    launch_url: str
    download_filename: str
    download_base64: str


class ConnectVerifyResponse(BaseModel):
    status: str  # "verified" | "failed"
    reason: str | None = None
    # Issue #4742: whether this connection's role can serve as a Bedrock routing
    # destination — i.e. it is assumable for any platform principal, not pinned
    # to the one user who created it. None when not determined (e.g. the assume
    # itself failed, or an idempotent re-verify of a row predating this field).
    routing_capable: bool | None = None
    # Machine-readable explanation when routing_capable is False. The R4 admin UI
    # renders it; keep the vocabulary stable.
    routing_reason: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/connect",
    response_model=ConnectStartResponse,
    status_code=201,
    summary="Start an AWS account connect flow",
    description=(
        "Creates a pending credential row and returns a CloudFormation Quick-Create "
        "URL. The user opens the URL in their AWS Console to create the IAM role."
    ),
)
async def connect_start(
    data: ConnectStartRequest,
    token_context: TokenContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
) -> ConnectStartResponse:
    # Resolve Cognito sub → Postgres users.id (FK on user_credentials.user_id)
    db_user_id = await _resolve_user_id(token_context.user_id, db, org_id=token_context.org_id, username=token_context.cognito_username)

    # Resolve effective org_id — falls back to users.org_id when token is empty
    # (Issue #600: GitHub-federated users may have empty org_id in token)
    effective_org_id = await resolve_effective_org_id(token_context, db)

    # Generate a unique external ID for confused-deputy protection
    external_id = str(uuid.uuid4())

    # Compute the expected role ARN
    role_arn = compute_role_arn(data.account_id, data.nickname)

    # Build the SM secret payload (same shape as #481 consumer expects)
    secret_payload = json.dumps(
        {
            "role_arn": role_arn,
            "external_id": external_id,
            "account_id": data.account_id,
            "default_region": "us-east-1",
        }
    )

    # Store in Secrets Manager
    secret_arn: str = await asyncio.to_thread(
        sm.create_secret,
        "aws",
        data.nickname,
        secret_payload,
        user_sub=token_context.user_id,
    )

    # Create the DB row with status=pending in scopes JSON
    cred = UserCredential(
        org_id=effective_org_id,
        user_id=db_user_id,
        service="aws",
        credential_type="aws_role",
        label=data.nickname,
        secret_arn=secret_arn,
        scopes={
            "account_id": data.account_id,
            "role_arn": role_arn,
            "status": "pending",
        },
    )
    db.add(cred)
    await db.commit()
    await db.refresh(cred)

    # Build the launch URL. templateURL is signed and scoped to this credential.
    # UserSessionTag must equal what the STS service sends at assume-role time —
    # which is the Postgres users.id (set by assume_role_routes.py passing
    # body.user_id=users.id), not the Cognito sub. Get it wrong and the trust
    # policy's RequestTag condition will AccessDenied every call.
    launch_url = build_launch_url(
        credential_id=cred.id,
        nickname=data.nickname,
        external_id=external_id,
        account_id=data.account_id,
        user_id=db_user_id,
        role_name=data.role_name,
    )

    logger.info(
        "AWS connect flow started credential_id=%s user=%s account=%s",
        cred.id,
        token_context.user_id,
        data.account_id,
    )

    return ConnectStartResponse(credential_id=cred.id, launch_url=launch_url)


@router.post(
    "/verify",
    response_model=ConnectVerifyResponse,
    summary="Verify the AWS role was created",
    description=("Attempts STS AssumeRole with the pending credential's external ID and session tags. On success, marks the credential as verified."),
)
async def connect_verify(
    data: ConnectVerifyRequest,
    token_context: TokenContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
) -> ConnectVerifyResponse:
    # Resolve Cognito sub → Postgres users.id for the scoped lookup
    db_user_id = await _resolve_user_id(token_context.user_id, db, org_id=token_context.org_id, username=token_context.cognito_username)

    # Resolve effective org_id — falls back to users.org_id when token is empty
    # (Issue #600: GitHub-federated users may have empty org_id in token)
    effective_org_id = await resolve_effective_org_id(token_context, db)

    cred = await _owned_connection(data.credential_id, db, db_user_id, effective_org_id)

    # Idempotency: already verified → no-op. Replay the stored routing
    # classification rather than re-probing (rows written before #4742 simply
    # have no value, which the None default reports honestly).
    #
    # Issue #5182: `fresh=True` opts out. `adp aws verify` is asked precisely
    # when the caller doubts the stored verdict, so replaying it would answer a
    # different question. The default keeps the UI path unchanged.
    if not data.fresh and cred.scopes and cred.scopes.get("status") == "verified":
        return ConnectVerifyResponse(
            status="verified",
            routing_capable=cred.scopes.get("routing_capable"),
            routing_reason=cred.scopes.get("routing_reason"),
        )

    # Read secret payload to get role_arn and external_id
    secret_value: str = await asyncio.to_thread(sm.get_secret, cred.secret_arn)
    secret_data = json.loads(secret_value)

    role_arn = secret_data["role_arn"]
    external_id = secret_data.get("external_id")

    # Attempt STS AssumeRole using the existing service. user_id here must
    # match what the trust policy's RequestTag condition expects — the
    # Postgres users.id, not the Cognito sub (same as connect_start).
    try:
        await asyncio.to_thread(
            assume_role,
            role_arn=role_arn,
            external_id=external_id,
            session_duration_seconds=900,  # minimum for verify
            default_region=secret_data.get("default_region", "us-east-1"),
            user_id=db_user_id,
            agent_id="connect-verify",
            task_id="verify",
            label=cred.label,
        )
    except STSAssumeError as exc:
        # Map STS error codes to user-friendly reasons
        reason = _sts_error_to_reason(exc.code)
        logger.warning(
            "AWS connect verify failed credential_id=%s code=%s",
            cred.id,
            exc.code,
        )
        # Issue #5182: a fresh check that fails on a row still labelled verified
        # must clear the label, or the connection stays green on a pass that is
        # no longer true and every consumer keeps trusting it. Only the fresh
        # path does this — the cached path never gets here.
        if data.fresh and cred.scopes and cred.scopes.get("status") == "verified":
            downgraded = dict(cred.scopes)
            downgraded["status"] = "pending"
            downgraded.pop("verified_at", None)
            downgraded["routing_capable"] = False
            downgraded["routing_reason"] = ROUTING_REASON_PROBE_INCONCLUSIVE
            cred.scopes = downgraded
            await db.commit()
        return ConnectVerifyResponse(status="failed", reason=reason)

    # The assume works. Now classify WHICH kind of role it is — read-only v1
    # (single-user) or routing-capable v2 — so the routing registry and the admin
    # dropdowns can filter on it. A probe failure never downgrades `status`: a v1
    # connection is perfectly valid for its own read-only purpose.
    routing_capable, routing_reason = await _probe_routing_capability(
        role_arn=role_arn,
        external_id=external_id,
        default_region=secret_data.get("default_region", "us-east-1"),
        user_id=db_user_id,
        label=cred.label,
    )

    # Success — update the scopes JSON to verified
    updated_scopes = dict(cred.scopes) if cred.scopes else {}
    updated_scopes["status"] = "verified"
    updated_scopes["verified_at"] = datetime.now(UTC).isoformat()
    updated_scopes["routing_capable"] = routing_capable
    if routing_reason is not None:
        updated_scopes["routing_reason"] = routing_reason
    else:
        updated_scopes.pop("routing_reason", None)
    cred.scopes = updated_scopes
    await db.commit()

    logger.info(
        "AWS connect verified credential_id=%s user=%s role_arn=%s routing_capable=%s",
        cred.id,
        token_context.user_id,
        role_arn,
        routing_capable,
    )

    return ConnectVerifyResponse(
        status="verified",
        routing_capable=routing_capable,
        routing_reason=routing_reason,
    )


@router.post(
    "/import",
    response_model=ConnectImportResponse,
    status_code=201,
    summary="Register an IAM role that already exists",
    description=(
        "Records a role the user has already created (or had an administrator create) as a personal AWS connection, "
        "without provisioning anything. Verification is a separate call — registering a role does not assert it works."
    ),
)
async def connect_import(
    data: ConnectImportRequest,
    token_context: TokenContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
) -> ConnectImportResponse:
    """Register or reuse a personal connection for an existing role (Issue #5182).

    The connect flow can only describe a role *it* names: it derives the ARN from
    the nickname, so a role called anything else cannot be registered at all. This
    path takes the ARN as the input instead.

    Two refusals, both before anything is written:

    * the ARN's embedded account not matching ``account_id`` — the caller has
      mixed up two accounts, and storing either interpretation would produce a
      connection that fails every assume;
    * the nickname already naming a *different* role for this user — the vault's
      uniqueness domain is ``(user, service, label)``, so silently reusing it
      would repoint an existing connection at another account.

    Re-importing the same ARN returns the existing connection (``reused=True``)
    rather than a second one, so a retried or repeated command converges.
    """
    db_user_id = await _resolve_user_id(token_context.user_id, db, org_id=token_context.org_id, username=token_context.cognito_username)
    effective_org_id = await resolve_effective_org_id(token_context, db)

    if data.role_arn.split(":")[4] != data.account_id:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "account_mismatch",
                "message": "The role ARN belongs to a different AWS account than the one given. Nothing was registered.",
            },
        )

    existing = (
        await db.scalars(
            select(UserCredential).where(
                UserCredential.user_id == db_user_id,
                UserCredential.org_id == effective_org_id,
                UserCredential.service == "aws",
                UserCredential.credential_type == "aws_role",
            )
        )
    ).all()

    for cred in existing:
        if (cred.scopes or {}).get("role_arn") == data.role_arn:
            logger.info("AWS connect import reused credential_id=%s user=%s", cred.id, token_context.user_id)
            return ConnectImportResponse(credential_id=cred.id, account_id=data.account_id, role_arn=data.role_arn, reused=True)

    if any(cred.label == data.nickname for cred in existing):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "duplicate_nickname",
                "message": "A different AWS connection already uses that name. Choose another name, or disconnect the existing one first.",
            },
        )

    secret_arn: str = await asyncio.to_thread(
        sm.create_secret,
        "aws",
        data.nickname,
        json.dumps(
            {
                "role_arn": data.role_arn,
                "external_id": data.external_id or "",
                "account_id": data.account_id,
                "default_region": data.default_region,
            }
        ),
        user_sub=token_context.user_id,
    )

    cred = UserCredential(
        org_id=effective_org_id,
        user_id=db_user_id,
        service="aws",
        credential_type="aws_role",
        label=data.nickname,
        secret_arn=secret_arn,
        scopes={
            "account_id": data.account_id,
            "role_arn": data.role_arn,
            # Imported, not provisioned: ADP has not assumed this role yet, so it
            # starts pending exactly like a Quick-Create row and becomes usable
            # only once /verify proves it.
            "status": "pending",
            "source": "imported_role",
        },
    )
    db.add(cred)
    await db.commit()
    await db.refresh(cred)

    logger.info(
        "AWS connect import registered credential_id=%s user=%s account=%s",
        cred.id,
        token_context.user_id,
        data.account_id,
    )
    return ConnectImportResponse(credential_id=cred.id, account_id=data.account_id, role_arn=data.role_arn, reused=False)


@router.get(
    "/{credential_id}/setup",
    response_model=ConnectSetupResponse,
    summary="Re-read a saved connection's role setup",
    description=(
        "Returns the CloudFormation template, parameters and instructions for a connection the caller already owns, "
        "reusing its stored ExternalId. Use this to resume an interrupted setup or to hand provisioning to an AWS administrator."
    ),
)
async def connect_setup(
    credential_id: str,
    response: Response,
    token_context: TokenContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
) -> ConnectSetupResponse:
    """Resume setup for a saved connection without rotating its ExternalId (Issue #5182).

    A regenerated ExternalId (or session tag) would describe a role that is not
    the one ADP verifies against, so this reads both back out of the connection's
    stored secret rather than minting new ones. That is also why the launch URL is
    rebuilt here instead of being persisted: a signed template URL expires, the
    parameters it carries do not.

    Refuses an imported role (409). Its trust policy was written by somebody else
    and is not the v1 template, so shipping that template as "the setup for this
    connection" would be a false instruction.
    """
    db_user_id = await _resolve_user_id(token_context.user_id, db, org_id=token_context.org_id, username=token_context.cognito_username)
    effective_org_id = await resolve_effective_org_id(token_context, db)
    response.headers["Cache-Control"] = "no-store"

    cred = await _owned_connection(credential_id, db, db_user_id, effective_org_id)
    scopes = cred.scopes or {}

    secret_data = json.loads(await asyncio.to_thread(sm.get_secret, cred.secret_arn))
    external_id = secret_data.get("external_id")
    role_arn = secret_data["role_arn"]
    region = secret_data.get("default_region", "us-east-1")

    if scopes.get("source") == "imported_role" or not external_id or role_arn != compute_role_arn(secret_data["account_id"], cred.label):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "not_provisionable",
                "message": (
                    "This connection points at a role ADP did not provision, so it has no setup package. "
                    "Verify the existing role instead, or connect a new account to provision one."
                ),
            },
        )

    launch_url = build_launch_url(
        credential_id=cred.id,
        nickname=cred.label,
        external_id=external_id,
        account_id=secret_data["account_id"],
        user_id=db_user_id,
        region=region,
    )
    template = await asyncio.to_thread(read_role_template)

    logger.info("AWS connect setup re-read credential_id=%s user=%s", cred.id, token_context.user_id)
    return ConnectSetupResponse(
        credential_id=cred.id,
        account_id=secret_data["account_id"],
        role_arn=role_arn,
        region=region,
        status=str(scopes.get("status") or "pending"),
        launch_url=launch_url,
        download_filename=f"adp-aws-{secret_data['account_id']}.zip",
        download_base64=connect_setup_download(
            launch_url=launch_url,
            account_id=secret_data["account_id"],
            role_arn=role_arn,
            region=region,
            template=template,
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Machine-readable reason a verified connection is not usable as a Bedrock
#: routing destination. Stable vocabulary — the admin UI renders these.
#:
#: Issue #4745 moved the definitions into ``src/shared/services/routing_probe.py``
#: alongside the probe that produces them, and re-exports them here so #4742's
#: callers and tests keep importing them from where they were introduced. Two
#: literals in two modules is a vocabulary that drifts.
ROUTING_REASON_USER_PINNED = _ROUTING_REASON_USER_PINNED
ROUTING_REASON_PROBE_INCONCLUSIVE = _ROUTING_REASON_PROBE_INCONCLUSIVE

#: The v1/v2 assumability classifier, now shared. Issue #4745 (R4) needs the same
#: check at mapping-save time, and design note §6.7 item 1 is explicit that there
#: must be exactly ONE assume probe: *"Reuse it; do not write a second assume
#: probe. Two probes with different conditions is how 'verified here, broken
#: there' happens."* So the implementation moved to
#: :mod:`src.shared.services.routing_probe` and this name is an alias, not a copy.
#:
#: Note what the admin path adds on top and this one deliberately does NOT: the
#: ``bedrock:InvokeModel`` capability probe (§6.7 item 3). A connection being
#: classified here may be a perfectly valid read-only v1 credential, and a missing
#: Bedrock permission is not a defect in *that* purpose — it only disqualifies the
#: role as a routing destination, which is a question only the routing surface asks.
_probe_routing_capability = probe_assumable_for_any_principal


def _sts_error_to_reason(code: str) -> str:
    """Map STS error codes to user-friendly messages."""
    mapping = {
        "NoSuchEntity": "The IAM role has not been created yet. Please ensure the CloudFormation stack completed successfully.",
        "AccessDenied": "The role's trust policy rejected the assume request. Please verify the stack finished creating.",
        "AccessDeniedException": "The role's trust policy rejected the assume request. Please verify the stack finished creating.",
        "MalformedPolicyDocument": "The role's trust policy is malformed. Please delete and recreate the stack.",
        "RegionDisabledException": "The target region is disabled. Please check your AWS account settings.",
    }
    return mapping.get(code, f"Verification failed: {code}. Please check the CloudFormation stack status in your AWS Console.")
