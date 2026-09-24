"""FastAPI routes for vault credential and identity CRUD, plus magic-link flow.

Issue #135: Vault Phase 2a — Credential + Identity CRUD
Issue #446: Vault Phase 2b — Magic-link identity linking flow

Endpoints:
  GET    /auth/credentials                   — list caller's credentials (metadata only)
  POST   /auth/credentials                   — register a new credential
  PUT    /auth/credentials/{id}              — idempotently register under a caller UUID
  PATCH  /auth/credentials/{id}              — update label / expires_at / strict
  DELETE /auth/credentials/{id}              — delete DB row + SM secret
  GET    /auth/identities                    — list caller's linked identities
  DELETE /auth/identities/{id}               — unlink an identity
  POST   /auth/identities/{provider}/link    — issue magic-link for a known signed-in user
  GET    /auth/link/magic                    — magic-link landing page (validate + confirm)

All endpoints require Cognito JWT except GET /auth/link/magic which validates the
token independently (redirects to Cognito if the user is not signed in).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.identity.providers import is_linkable_provider
from src.shared.identity.verification import (
    MAGIC_LINK_CONFIRMED,
    SELF_ASSERTED,
    delivery_proves_ownership,
    is_proven,
)
from src.shared.models.audit import AuditLog
from src.shared.models.organization import User
from src.shared.models.vault import MagicLinkNonce, UserIdentity
from src.shared.services.secrets_manager import SecretsManagerHelper

from .magic_link import (
    ChannelContextMismatchError,
    ClaimNotBoundToNonceError,
    NonceAlreadyConsumedError,
    NonceNotFoundError,
    TargetUserMismatchError,
    TokenExpiredError,
    TokenInvalidError,
    consume_nonce,
    verify_token,
)
from .middleware import get_current_user_context
from .vault_schemas import VALID_SCOPES, CredentialCreate, CredentialResponse, CredentialUpdate, IdentityResponse
from .vault_service import (
    CredentialNotFoundError,
    DuplicateCredentialError,
    IdentityNotFoundError,
    InsufficientPrivilegesError,
    InvalidScopeConfigError,
    create_credential,
    delete_credential,
    list_credentials,
    list_identities,
    unlink_identity,
    update_credential,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["vault"])

# Module-level SM helper (real boto3 client); tests replace via dependency injection.
_sm_helper = SecretsManagerHelper()


def get_secrets_manager() -> SecretsManagerHelper:
    """FastAPI dependency that returns the SecretsManagerHelper instance.

    Overridden in tests to inject a mock.
    """
    return _sm_helper


async def _resolve_user_id_in_context(token_context, db: AsyncSession) -> None:
    """Resolve Cognito sub → Postgres users.id AND the effective org, in place.

    TokenContext.user_id holds the Cognito sub for human users (the JWT `sub`
    claim), but user_credentials.user_id is a FK on users.id.  Without this
    resolution, vault_service._visible_credential_filter compares sub vs UUID
    and silently returns zero rows.  Same bug that #567 fixed on the
    aws_connect_routes side — applying it here too.

    Service accounts don't hit this path (their JWT sub IS the users.id).
    If no users row matches (shouldn't happen for registered humans), leave
    the context untouched — the downstream query will return empty, which is
    the safe behavior.

    Issue #5264: the org has to be resolved here for exactly the same reason the
    user does.  ``POST /auth/credentials/aws/connect`` WRITES its row with
    ``resolve_effective_org_id``, which falls back to ``users.org_id`` when the
    token carries no ``custom:org_id`` claim (#600).  These routes READ with the
    raw claim.  With a CLI password sign-in — which produces no org claim at all —
    the create path and the list/delete paths disagreed about which tenant the
    caller was in, so a connection the user had just made was absent from
    ``adp aws list`` and 404 on ``adp aws disconnect``, while the create path
    still saw it and refused the duplicate name.  Every retry leaked another
    invisible row, its secret and its IAM role, with no product path to reclaim
    any of them.

    Resolved in this one helper, not per-route, because every credential route
    already calls it: that is what stops the next route from re-introducing the
    split.  It NARROWS nothing and WIDENS nothing — the filter still requires
    org_id equality, and ``caller.org_id`` now simply holds the caller's real org
    instead of an empty string.  Another org's credential stays invisible.
    """
    if token_context.account_type != "human":
        return
    from src.shared.identity.workspaces import workspace_user

    user = await workspace_user(db, token_context.user_id, token_context.org_id, username=token_context.cognito_username)
    if user is not None:
        token_context.user_id = user.id
        # Prefer the user row we just resolved: it is the same row
        # resolve_effective_org_id's fallback reads, without a second query.
        if not token_context.org_id and user.org_id:
            token_context.org_id = user.org_id
        return
    # No users row matched under the claimed org. That is the case an org-less
    # token produces when the sub is only findable via login_user, so try the
    # login lookup before giving up — otherwise the empty-claim caller stays
    # unresolved and every credential route keeps returning nothing.
    #
    # Deliberately NOT resolve_effective_org_id: that raises 409 when the caller
    # has no org, and a list endpoint must keep answering with an empty list
    # rather than start erroring on a surface that worked. Leaving the context
    # untouched is the existing, safe behaviour for that caller.
    #
    # Gated on the claim being ABSENT. If a token asserts an org and no user
    # matches in it, that caller is not a member of the org they claimed, and
    # substituting an identity resolved by sub alone would cross a tenant
    # boundary to do it. Only the org-less token — the case this fixes — takes
    # this path.
    if token_context.org_id:
        return
    from src.shared.identity.workspaces import login_user

    login = await login_user(db, token_context.user_id)
    if login is not None and login.org_id:
        token_context.user_id = login.id
        token_context.org_id = login.org_id


# ---------------------------------------------------------------------------
# Credential endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/credentials",
    response_model=list[CredentialResponse],
    summary="List credentials visible to the caller",
    description=(
        "Returns credential metadata for all credentials the caller can see. "
        "Secret values and ARNs are never returned. "
        "Optionally filter by scope: user | team | org | domain_app."
    ),
)
async def list_credentials_endpoint(
    scope: str | None = Query(None, description="Filter by scope: user | team | org | domain_app"),
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[CredentialResponse]:
    if scope is not None and scope not in VALID_SCOPES:
        raise HTTPException(status_code=400, detail={"error": "invalid_scope", "message": f"scope must be one of {VALID_SCOPES}"})

    await _resolve_user_id_in_context(token_context, db)
    creds = await list_credentials(db, token_context, scope_filter=scope)
    return [CredentialResponse.from_model(c) for c in creds]


@router.post(
    "/credentials",
    response_model=CredentialResponse,
    status_code=201,
    summary="Register a new credential",
    description=(
        "Stores the raw value in AWS Secrets Manager and records metadata in the database. "
        "scope_hint defaults to 'user'. Non-user scopes require admin role."
    ),
)
async def create_credential_endpoint(
    data: CredentialCreate,
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
) -> CredentialResponse:
    try:
        await _resolve_user_id_in_context(token_context, db)
        cred = await create_credential(data, db, token_context, sm)
        return CredentialResponse.from_model(cred)
    except InsufficientPrivilegesError as exc:
        raise HTTPException(status_code=403, detail={"error": "insufficient_privileges", "message": str(exc)})
    except InvalidScopeConfigError as exc:
        raise HTTPException(status_code=422, detail={"error": "invalid_scope_config", "message": str(exc)})
    except DuplicateCredentialError:
        # Service layer already cleaned up the orphaned SM secret (F2) and
        # rolled back the DB session.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "duplicate_credential",
                "message": "A credential with this service and label already exists. Delete it first to re-register.",
            },
        )
    except Exception:
        logger.exception("Unexpected error creating credential for user=%s", token_context.user_id)
        raise HTTPException(status_code=500, detail={"error": "create_failed", "message": "Failed to create credential"})


@router.put(
    "/credentials/{credential_id}",
    response_model=CredentialResponse,
    status_code=201,
    summary="Idempotently register a credential",
    description=(
        "Stores a credential under a caller-generated operation UUID. Repeating the same request "
        "with the same UUID returns the original metadata without writing another secret."
    ),
)
async def put_credential_endpoint(
    credential_id: UUID = Path(..., description="Caller-generated credential operation UUID"),
    data: CredentialCreate = ...,
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
) -> CredentialResponse:
    try:
        await _resolve_user_id_in_context(token_context, db)
        cred = await create_credential(data, db, token_context, sm, credential_id=str(credential_id))
        return CredentialResponse.from_model(cred)
    except InsufficientPrivilegesError as exc:
        raise HTTPException(status_code=403, detail={"error": "insufficient_privileges", "message": str(exc)})
    except InvalidScopeConfigError as exc:
        raise HTTPException(status_code=422, detail={"error": "invalid_scope_config", "message": str(exc)})
    except DuplicateCredentialError:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "operation_conflict",
                "message": (
                    "Credential operation conflicts with existing state. Its secret may already exist even if "
                    "metadata is absent; retain the operation UUID and retry only this operation with the same inputs."
                ),
            },
        )
    except Exception:
        logger.exception("Unexpected idempotent credential create for user=%s", token_context.user_id)
        raise HTTPException(status_code=500, detail={"error": "create_failed", "message": "Failed to create credential"})


@router.patch(
    "/credentials/{credential_id}",
    response_model=CredentialResponse,
    summary="Update credential metadata",
    description=(
        "Update label, expires_at, or strict. The value cannot be changed via PATCH — delete and re-register for audit clarity. "
        "Modifying an org- or team-scoped (shared) credential requires an organization administrator role."
    ),
)
async def update_credential_endpoint(
    credential_id: str = Path(..., description="Credential ID"),
    data: CredentialUpdate = ...,
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> CredentialResponse:
    try:
        await _resolve_user_id_in_context(token_context, db)
        cred = await update_credential(credential_id, data, db, token_context)
        return CredentialResponse.from_model(cred)
    except CredentialNotFoundError:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Credential not found"})
    except InsufficientPrivilegesError as exc:
        # Issue #3989: without this mapping the shared-scope admin gate in
        # update_credential would fall through to the bare `except Exception`
        # below and surface as a 500 — failing closed, but not with the 403 the
        # caller (and the tests) require.
        raise HTTPException(status_code=403, detail={"error": "insufficient_privileges", "message": str(exc)})
    except Exception:
        logger.exception("Unexpected error updating credential %s", credential_id)
        raise HTTPException(status_code=500, detail={"error": "update_failed", "message": "Failed to update credential"})


@router.delete(
    "/credentials/{credential_id}",
    status_code=204,
    response_model=None,
    summary="Delete a credential",
    description=(
        "Synchronously deletes the database row and the AWS Secrets Manager secret. The secret is deleted WITH AWS's recovery window "
        "(7-30 days), not force-deleted. Returns 204 on success, 404 if not found or not owned by caller, 403 when deleting a shared "
        "(org/team) credential without an organization administrator role."
    ),
)
async def delete_credential_endpoint(
    credential_id: str = Path(..., description="Credential ID"),
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
) -> None:
    try:
        await _resolve_user_id_in_context(token_context, db)
        await delete_credential(credential_id, db, token_context, sm)
    except CredentialNotFoundError:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Credential not found"})
    except InsufficientPrivilegesError as exc:
        # Issue #3989: see the PATCH handler — required so the shared-scope gate
        # returns 403 rather than a 500 from the catch-all below.
        raise HTTPException(status_code=403, detail={"error": "insufficient_privileges", "message": str(exc)})
    except Exception:
        logger.exception("Unexpected error deleting credential %s", credential_id)
        raise HTTPException(status_code=500, detail={"error": "delete_failed", "message": "Failed to delete credential"})


# ---------------------------------------------------------------------------
# Identity endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/identities",
    response_model=list[IdentityResponse],
    summary="List linked identities",
    description="Returns all external identities (Slack, GitHub, etc.) linked to the caller.",
)
async def list_identities_endpoint(
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[IdentityResponse]:
    await _resolve_user_id_in_context(token_context, db)
    identities = await list_identities(db, token_context)
    return [IdentityResponse.from_model(i) for i in identities]


@router.delete(
    "/identities/{identity_id}",
    status_code=204,
    response_model=None,
    summary="Unlink an identity",
    description=("Removes the user_identities row (unlinks the external identity). Returns 204 on success, 404 if not found or not owned by caller."),
)
async def unlink_identity_endpoint(
    identity_id: str = Path(..., description="Identity ID"),
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        await _resolve_user_id_in_context(token_context, db)
        await unlink_identity(identity_id, db, token_context)
    except IdentityNotFoundError:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Identity not found"})
    except Exception:
        logger.exception("Unexpected error unlinking identity %s", identity_id)
        raise HTTPException(status_code=500, detail={"error": "unlink_failed", "message": "Failed to unlink identity"})


# ---------------------------------------------------------------------------
# Magic-link issuance (Cognito-authed user adds a new identity they own)
# ---------------------------------------------------------------------------


class MagicLinkIssueRequest(BaseModel):
    """Body for POST /auth/identities/{provider}/link."""

    provider_user_id: str
    channel_context: str | None = None


class MagicLinkIssueResponse(BaseModel):
    """Outcome of a link request — never a credential (#5664, A10).

    This used to be ``magic_link_url``: the caller named an account and the
    platform handed back the very token that "confirms" the claim. The requester
    could therefore complete both halves of the handshake, so the resulting row
    was recorded as verified without the claimed account ever being contacted.

    What the caller gets now is the *status* of their claim. When the claim is
    unproven that status says so explicitly, which is the honest answer and also
    the only one that cannot be replayed.
    """

    status: str
    provider: str
    provider_user_id: str
    verification_method: str
    verified_at: str | None = None
    identity_id: str | None = None
    # How the user can turn an unproven claim into a proven link. Instructions,
    # not a credential.
    next_step: str | None = None


def _get_magic_link_secret() -> str:
    """Resolve the magic-link signing key — and only that key.

    Issue #5656 (A05): previously `magic_link_secret or token_secret_key`. The
    fallback made the identity-linking key and the session-signing key the same
    secret whenever the former was unset, so the two could not be rotated
    independently and an unset key was invisible. Callers treat "" as
    503 not_configured, which is the intended behaviour for a missing key: refuse
    to issue rather than issue something signed with the session key.
    """
    return get_settings().magic_link_secret


# NOTE (#5664, A10): this module no longer builds magic-link URLs or mints nonces.
# It used to, on the user-facing claim route, and handing that URL back to the
# claimant is what made the "verification" circular. Link construction now lives
# only on the internal issuance path (``src/internal/routes.py``), whose caller
# delivers it in-channel to the claimed account. Keeping the builder here would
# invite a future caller to re-open the circle, so it is deliberately absent
# rather than left unused.


async def _append_audit(
    db: AsyncSession,
    *,
    event_type: str,
    org_id: str,
    actor_id: str | None,
    details: dict | None,
) -> None:
    log = AuditLog(org_id=org_id, event_type=event_type, actor_id=actor_id, details=details)
    db.add(log)


_OUT_OF_BAND_NEXT_STEP = (
    "Send a message from this account in a connected channel. ADP will deliver a "
    "confirmation link to that account; confirming it there proves you control it."
)


def _require_linkable_provider(provider: str) -> None:
    """Reject a non-linkable provider BEFORE any state is written (#5664, A10).

    Two distinct refusals collapse into one here:

    * an unknown value (typo, probe, path-traversal attempt), and
    * one of the INTERNAL setup namespaces (`github_install`,
      `github_app_register`), which are not identities at all.

    The second case was a privilege escalation, not a validation gap. `provider`
    arrives as a free-form path segment and used to flow straight into
    ``store_nonce``, so any signed-in user could mint a nonce in the admin
    namespace — and that nonce is the SOLE authenticator on
    ``register_app_callback``, which overwrites the deployment's shared GitHub App
    credentials, the webhook signing secret and the GitHub sign-in secret. One
    ordinary account was therefore enough to take over the inbound trust path and
    break every existing tenant's connection at the same time.

    Why here and not the ORM validator: ``UserIdentity.validate_provider`` fires
    when the identity row is written, which on this flow is a LATER request. By
    then ``store_nonce`` has committed the row and the token has already been
    handed to the caller — the escalation is complete before the validator ever
    runs.

    The message deliberately does not enumerate the internal namespaces; both
    cases return the same ``unsupported_provider`` so the response cannot be used
    to discover them.
    """
    if not is_linkable_provider(provider):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unsupported_provider",
                "message": "That identity provider is not supported for linking.",
            },
        )


@router.post(
    "/identities/{provider}/link",
    response_model=MagicLinkIssueResponse,
    status_code=201,
    summary="Issue a magic-link to add a new identity",
    description=(
        "Records that the caller claims an external account. The claim is stored "
        "as UNPROVEN (`self_asserted`, no `verified_at`) and grants nothing.\n\n"
        "Proof of ownership is established out-of-band: send a message from the "
        "claimed account in an ADP-connected channel, and the platform delivers a "
        "confirmation link to that account. Confirming it there records "
        "`magic_link_confirmed`. This endpoint deliberately does not return a "
        "confirmation link, because a link returned to the claimant proves nothing "
        "about the account being claimed."
    ),
)
async def issue_identity_magic_link(
    provider: str = Path(..., description="Identity provider: slack | github | whatsapp | discord"),
    body: MagicLinkIssueRequest = ...,
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> MagicLinkIssueResponse:
    """Record an unproven claim. Never mints a confirmation credential (#5664, A10).

    This endpoint used to close a full circle with no proof anywhere in it: the
    caller chose ``provider_user_id``, the response handed back the magic link, and
    confirming that link wrote ``verified_at``. Every step was performed by the
    person making the claim, so "verified" only ever meant "the requester can read
    their own HTTP response". Any signed-in user could therefore have an arbitrary
    external account recorded as verifiably theirs.

    The issue allows two remedies — derive the account id from a completed provider
    handshake, or deliver confirmation out-of-band to the claimed account and never
    return it to the requester. This route takes the second, because a completed
    handshake is not available here: the caller is authenticated to ADP, not to
    Slack or Discord, so there is no provider-signed assertion about the account
    they are naming. (Where such an assertion *does* exist the platform already
    uses it — ``admin/onboarding/handler.py`` reads the immutable GitHub id out of
    the signed Cognito claims, never from a request body.)

    Out-of-band delivery is not new machinery. The ingest path already does it:
    when a provider-authenticated inbound event arrives from an unrecognised
    account, ``/internal/v1/issue-magic-link`` mints the token and the ingest
    Lambda posts it back **in-channel** to that account
    (``gateway/lambdas/ingest/handler.py``, ``_handle_unresolved_user``). Only
    someone who can read that channel can complete it, which is what makes it
    evidence. Nonce issuance therefore belongs solely to that path, and this route
    records the claim and points the user at it.

    Consequence worth stating plainly: because the in-channel path is now the only
    minter, "a nonce exists" implies "it was delivered to the claimed account".
    That invariant is structural rather than a rule someone has to remember, which
    is also why no backfill of stored rows is required.

    The claim row is still written, deliberately: recording it as ``self_asserted``
    keeps an attempt to claim someone else's account visible and auditable,
    whereas dropping it silently would hide exactly that. It sets no
    ``verified_at``, and ``is_proven()`` rejects it, so no consumer can mistake the
    claim for evidence.
    """
    # Provider validity is a property of the request alone, so it is settled
    # before anything else — including before any persistence, so the answer to
    # "is this provider linkable" cannot vary with deployment config.
    _require_linkable_provider(provider)

    # The caller's row supplies team_id, which UserIdentity requires.
    user = (await db.execute(select(User).where(User.id == token_context.user_id))).scalar_one_or_none()
    team_id = user.team_id if user else ""

    existing = (
        await db.execute(
            select(UserIdentity).where(
                UserIdentity.org_id == token_context.org_id,
                UserIdentity.provider == provider,
                UserIdentity.provider_user_id == body.provider_user_id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        # Already claimed in this tenant. Never overwrite, and never disclose whose
        # it is — a claim probe must not become an account-enumeration oracle.
        # Echoing the caller's OWN row is not a leak.
        if existing.user_id == token_context.user_id:
            return MagicLinkIssueResponse(
                status="already_linked",
                provider=provider,
                provider_user_id=body.provider_user_id,
                verification_method=existing.verification_method,
                verified_at=existing.verified_at.isoformat() if existing.verified_at else None,
                identity_id=existing.id,
                next_step=(None if is_proven(existing.verification_method) else _OUT_OF_BAND_NEXT_STEP),
            )
        raise HTTPException(
            status_code=409,
            detail={
                "error": "identity_already_linked",
                "message": f"Provider identity {provider}:{body.provider_user_id} is already linked.",
            },
        )

    claim = UserIdentity(
        org_id=token_context.org_id,
        user_id=token_context.user_id,
        team_id=team_id,
        provider=provider,
        provider_user_id=body.provider_user_id,
        provider_username=None,
        # Unproven by construction, and verified_at left NULL rather than stamped:
        # the pair is what every trust-aware consumer reads.
        verification_method=SELF_ASSERTED,
        verified_at=None,
    )
    db.add(claim)

    await _append_audit(
        db,
        event_type="identity_claim_recorded",
        org_id=token_context.org_id,
        actor_id=token_context.user_id,
        details={
            "provider": provider,
            "provider_user_id": body.provider_user_id,
            "channel_context": body.channel_context,
            "verification_method": SELF_ASSERTED,
            "source": "user_initiated",
        },
    )

    try:
        await db.commit()
        await db.refresh(claim)
    except Exception as exc:
        # Lost a race against a concurrent claim for the same account.
        await db.rollback()
        logger.warning("Identity claim conflict provider=%s provider_user_id=%s: %s", provider, body.provider_user_id, exc)
        raise HTTPException(
            status_code=409,
            detail={
                "error": "identity_already_linked",
                "message": f"Provider identity {provider}:{body.provider_user_id} is already linked.",
            },
        )

    logger.info(
        "Identity claim recorded (unproven) user=%s provider=%s provider_user_id=%s",
        token_context.user_id,
        provider,
        body.provider_user_id,
    )
    return MagicLinkIssueResponse(
        status="claim_recorded_unverified",
        provider=provider,
        provider_user_id=body.provider_user_id,
        verification_method=SELF_ASSERTED,
        verified_at=None,
        identity_id=claim.id,
        next_step=_OUT_OF_BAND_NEXT_STEP,
    )


# ---------------------------------------------------------------------------
# Magic-link landing page
# ---------------------------------------------------------------------------


@router.get(
    "/link/magic",
    summary="Magic-link landing page — validate token and confirm identity link",
    description=(
        "Validates the signed token.  If the user is not Cognito-authenticated, "
        "redirects to the Cognito hosted UI (which loops back here after login).  "
        "Returns JSON with confirmation details when authenticated but not yet confirmed.\n\n"
        "POST to this endpoint (with the same ?token= param) to confirm the link."
    ),
    responses={
        200: {"description": "Token valid; returns confirmation details"},
        302: {"description": "Redirect to Cognito for login"},
        400: {"description": "Token expired, invalid, or already consumed"},
        403: {"description": "Logged-in user does not match token target_user_id"},
    },
)
async def magic_link_landing_get(
    request: Request,
    token: str,
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Validate the token and return confirmation details.

    The caller is already Cognito-authenticated (FastAPI dependency ensures it).
    If the user is *not* authenticated, the ``get_current_user_context`` dependency
    raises a 401 which the frontend should intercept and redirect to Cognito,
    then loop back to this URL.
    """
    secret = _get_magic_link_secret()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail={"error": "not_configured", "message": "Magic-link signing key not configured"},
        )

    try:
        payload = verify_token(token, secret)
    except TokenExpiredError:
        raise HTTPException(status_code=400, detail={"error": "token_expired", "message": "Magic-link token has expired"})
    except TokenInvalidError as exc:
        raise HTTPException(status_code=400, detail={"error": "token_invalid", "message": str(exc)})

    # A token minted before #5664, or one carrying an internal setup namespace,
    # must not be honoured on the identity surface either. The nonce store is
    # shared, so the landing page has to re-check the namespace rather than
    # assume issuance validated it.
    _require_linkable_provider(payload["provider"])

    # Verify nonce exists and is not consumed (do not consume yet — just peek)
    jti = payload["jti"]
    stmt = select(MagicLinkNonce).where(MagicLinkNonce.jti == jti)
    result = await db.execute(stmt)
    nonce = result.scalar_one_or_none()

    if nonce is None:
        raise HTTPException(status_code=400, detail={"error": "token_invalid", "message": "Token nonce not found"})
    if nonce.consumed_at is not None:
        raise HTTPException(status_code=400, detail={"error": "token_already_used", "message": "Magic-link has already been used"})
    if nonce.expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
        raise HTTPException(status_code=400, detail={"error": "token_expired", "message": "Magic-link token has expired"})

    # target_user_id check
    if payload.get("target_user_id") and payload["target_user_id"] != token_context.user_id:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "user_mismatch",
                "message": ("This magic-link was issued for a different user. Please sign in as the correct account to proceed."),
            },
        )

    return {
        "status": "pending_confirmation",
        "provider": payload["provider"],
        "provider_user_id": payload["provider_user_id"],
        "channel_context": payload.get("channel_context"),
        "linking_to_user": token_context.user_id,
        "jti": jti,
    }


@router.post(
    "/link/magic",
    summary="Confirm magic-link identity linking",
    status_code=201,
    description=(
        "Consumes the nonce and writes the user_identities row.  "
        "Returns 201 on success with the new identity record.  "
        "Replays (nonce already consumed) return 400.  "
        "Wrong signed-in user returns 403."
    ),
    responses={
        201: {"description": "Identity linked successfully"},
        400: {"description": "Token expired, invalid, or already used"},
        403: {"description": "Logged-in user does not match token target"},
        409: {"description": "Provider identity already linked to another user"},
    },
)
async def magic_link_landing_post(
    token: str,
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Confirm the link: consume the nonce and write the user_identities row."""
    secret = _get_magic_link_secret()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail={"error": "not_configured", "message": "Magic-link signing key not configured"},
        )

    try:
        payload = verify_token(token, secret)
    except TokenExpiredError:
        await _append_audit(
            db,
            event_type="magic_link_failed",
            org_id=token_context.org_id,
            actor_id=token_context.user_id,
            details={"reason": "token_expired", "token_preview": token[:20]},
        )
        await db.commit()
        raise HTTPException(status_code=400, detail={"error": "token_expired", "message": "Magic-link token has expired"})
    except TokenInvalidError as exc:
        await _append_audit(
            db,
            event_type="magic_link_failed",
            org_id=token_context.org_id,
            actor_id=token_context.user_id,
            details={"reason": "token_invalid", "error": str(exc)},
        )
        await db.commit()
        raise HTTPException(status_code=400, detail={"error": "token_invalid", "message": str(exc)})

    jti = payload["jti"]
    channel_context = payload.get("channel_context")

    # The same closed allowlist the GET landing page and both minters apply. The
    # nonce store is shared with the platform-admin setup namespaces, so a confirm
    # route that skipped this check would be a second way onto that surface.
    _require_linkable_provider(payload["provider"])

    try:
        nonce = await consume_nonce(
            jti=jti,
            channel_context=channel_context,
            consuming_user_id=token_context.user_id,
            db=db,
            # Bind the signed claims to the stored row before spending the nonce.
            claimed_provider=payload["provider"],
            claimed_provider_user_id=payload["provider_user_id"],
        )
    except TokenExpiredError:
        await _append_audit(
            db,
            event_type="magic_link_failed",
            org_id=token_context.org_id,
            actor_id=token_context.user_id,
            details={"reason": "nonce_expired", "jti": jti},
        )
        await db.commit()
        raise HTTPException(status_code=400, detail={"error": "token_expired", "message": "Magic-link token has expired"})
    except NonceAlreadyConsumedError:
        await _append_audit(
            db,
            event_type="magic_link_failed",
            org_id=token_context.org_id,
            actor_id=token_context.user_id,
            details={"reason": "replay", "jti": jti},
        )
        await db.commit()
        raise HTTPException(status_code=400, detail={"error": "token_already_used", "message": "Magic-link has already been used"})
    except NonceNotFoundError:
        raise HTTPException(status_code=400, detail={"error": "token_invalid", "message": "Token nonce not found"})
    except ClaimNotBoundToNonceError as exc:
        await _append_audit(
            db,
            event_type="magic_link_failed",
            org_id=token_context.org_id,
            actor_id=token_context.user_id,
            details={
                "reason": "claim_not_bound_to_nonce",
                "jti": jti,
                "claimed_provider": payload.get("provider"),
                "claimed_provider_user_id": payload.get("provider_user_id"),
            },
        )
        await db.commit()
        raise HTTPException(status_code=400, detail={"error": "token_invalid", "message": str(exc)})
    except ChannelContextMismatchError as exc:
        await _append_audit(
            db,
            event_type="magic_link_failed",
            org_id=token_context.org_id,
            actor_id=token_context.user_id,
            details={"reason": "channel_context_mismatch", "jti": jti, "error": str(exc)},
        )
        await db.commit()
        raise HTTPException(status_code=400, detail={"error": "channel_context_mismatch", "message": str(exc)})
    except TargetUserMismatchError as exc:
        await _append_audit(
            db,
            event_type="magic_link_failed",
            org_id=token_context.org_id,
            actor_id=token_context.user_id,
            details={
                "reason": "target_user_mismatch",
                "jti": jti,
                "target": payload.get("target_user_id"),
                "consumer": token_context.user_id,
            },
        )
        await db.commit()
        raise HTTPException(status_code=403, detail={"error": "user_mismatch", "message": str(exc)})

    # Authoritative values come from the NONCE ROW, never from the token. The two
    # were just proven equal, so this is not a behaviour change — it removes the
    # token as a source of truth so a future edit cannot reintroduce one.
    provider = nonce.provider
    provider_user_id = nonce.provider_user_id

    # Does confirming this link actually prove the confirmer owns the account?
    #
    # Two facts have to hold, and both are read from the stored nonce rather than
    # inferred from which route minted it:
    #
    # * delivery was private to the claimed account. The ingest path posts the link
    #   back into the SAME conversation the triggering message came from, which for
    #   a public channel or an issue thread is readable by everyone in it. That
    #   proves channel access, not account ownership.
    # * the nonce named the platform user it was for. An internal nonce carries
    #   target_user_id=None precisely so the recipient can choose their account on
    #   the landing page — which means whoever reaches the link first can consume
    #   it. Publicly readable AND unbound is the squatting path itself.
    #
    # When either fails the link is still recorded, as self_asserted with no
    # verified_at, so a genuine user is not blocked and an attempt stays auditable.
    # is_proven() rejects it, so it grants nothing.
    delivered_privately = delivery_proves_ownership(nonce.delivery_method)
    bound_to_a_user = nonce.target_user_id is not None
    ownership_proven = delivered_privately and bound_to_a_user

    if ownership_proven:
        confirmed_method = MAGIC_LINK_CONFIRMED
        confirmed_at = datetime.now(UTC)
    else:
        confirmed_method = SELF_ASSERTED
        confirmed_at = None
        logger.warning(
            "Magic-link confirmed without ownership proof jti=%s delivery=%r bound=%s — recording as %s",
            jti,
            nonce.delivery_method,
            bound_to_a_user,
            SELF_ASSERTED,
        )

    # Fetch the user row to get team_id (required by UserIdentity)
    user_stmt = select(User).where(User.id == token_context.user_id)
    user_result = await db.execute(user_stmt)
    user = user_result.scalar_one_or_none()
    team_id = user.team_id if user else ""

    # Who, if anyone, already holds this (provider, provider_user_id) in this
    # tenant? The unique index from migration 021 allows exactly one holder, so
    # this single row decides between upgrade, recovery and refusal.
    holder = (
        await db.execute(
            select(UserIdentity).where(
                UserIdentity.org_id == token_context.org_id,
                UserIdentity.provider == provider,
                UserIdentity.provider_user_id == provider_user_id,
            )
        )
    ).scalar_one_or_none()

    if holder is not None and holder.user_id == token_context.user_id:
        # The caller's own row. Confirming is the evidence it was missing, so
        # upgrade in place rather than inserting a second row and colliding with
        # the unique index.
        identity = holder
        identity.verification_method = confirmed_method
        identity.verified_at = confirmed_at
    elif holder is not None:
        # Someone ELSE holds it. Whether this is recoverable depends entirely on
        # what their claim is worth:
        #
        # * an UNPROVEN claim is a squat. Before this issue, a user could assert
        #   any account and the row stood; the real owner then had no way to claim
        #   their own identity, which is a lockout the fix must not preserve. A
        #   caller who has now PROVEN ownership takes the identity over, and the
        #   squatter's row is deleted rather than left to shadow it.
        # * a PROVEN link is never transferred. Someone else demonstrated control
        #   of this account, and no later confirmation overrides that — otherwise
        #   the recovery path would itself become the takeover path. It is refused
        #   and an operator resolves it.
        if is_proven(holder.verification_method) or not ownership_proven:
            await _append_audit(
                db,
                event_type="identity_link_refused",
                org_id=token_context.org_id,
                actor_id=token_context.user_id,
                details={
                    "reason": ("held_by_proven_link" if is_proven(holder.verification_method) else "claimant_has_no_proof"),
                    "provider": provider,
                    "provider_user_id": provider_user_id,
                    "holder_verification_method": holder.verification_method,
                },
            )
            await db.commit()
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "identity_already_linked",
                    "message": f"Provider identity {provider}:{provider_user_id} is already linked.",
                },
            )

        logger.warning(
            "Proven claim reclaiming an unproven-held identity provider=%s provider_user_id=%s from_user=%s to_user=%s",
            provider,
            provider_user_id,
            holder.user_id,
            token_context.user_id,
        )
        await _append_audit(
            db,
            event_type="identity_claim_reclaimed",
            org_id=token_context.org_id,
            actor_id=token_context.user_id,
            details={
                "provider": provider,
                "provider_user_id": provider_user_id,
                "displaced_user_id": holder.user_id,
                "displaced_verification_method": holder.verification_method,
            },
        )
        # Delete then flush BEFORE inserting: the unique index is on
        # (provider, provider_user_id, org_id), so both rows would exist at once
        # without the flush and the insert would violate it.
        await db.delete(holder)
        await db.flush()
        identity = UserIdentity(
            org_id=token_context.org_id,
            user_id=token_context.user_id,
            team_id=team_id,
            provider=provider,
            provider_user_id=provider_user_id,
            provider_username=None,
            verification_method=confirmed_method,
            verified_at=confirmed_at,
        )
        db.add(identity)
    else:
        identity = UserIdentity(
            org_id=token_context.org_id,
            user_id=token_context.user_id,
            team_id=team_id,
            provider=provider,
            provider_user_id=provider_user_id,
            provider_username=None,
            # MAGIC_LINK_CONFIRMED only when delivery was private AND the nonce
            # named this user; otherwise SELF_ASSERTED with verified_at NULL.
            verification_method=confirmed_method,
            verified_at=confirmed_at,
        )
        db.add(identity)

    await _append_audit(
        db,
        event_type="magic_link_consumed",
        org_id=token_context.org_id,
        actor_id=token_context.user_id,
        details={
            "jti": jti,
            "provider": provider,
            "provider_user_id": provider_user_id,
            "channel_context": channel_context,
        },
    )
    await _append_audit(
        db,
        event_type="identity_linked",
        org_id=token_context.org_id,
        actor_id=token_context.user_id,
        details={
            "provider": provider,
            "provider_user_id": provider_user_id,
            "verification_method": identity.verification_method,
            "delivery_method": nonce.delivery_method,
            "ownership_proven": ownership_proven,
        },
    )

    try:
        # One commit covers the nonce consumption, the identity row and the audit
        # trail. consume_nonce deliberately left its UPDATE pending so that a
        # failure here cannot burn the nonce without linking anything.
        await db.commit()
        await db.refresh(identity)
    except Exception as exc:
        # UNIQUE constraint violation — provider identity already linked
        logger.warning("Identity already linked provider=%s provider_user_id=%s: %s", provider, provider_user_id, exc)
        raise HTTPException(
            status_code=409,
            detail={
                "error": "identity_already_linked",
                "message": f"Provider identity {provider}:{provider_user_id} is already linked to a user.",
            },
        )

    logger.info(
        "Identity linked via magic-link user=%s provider=%s provider_user_id=%s method=%s",
        token_context.user_id,
        provider,
        provider_user_id,
        identity.verification_method,
    )
    return {
        # Reported honestly: a confirmation that could not establish ownership is
        # "linked_unverified", not "linked". A caller that treats the two the same
        # is making its own choice; it is not being told the claim was proven.
        "status": "linked" if ownership_proven else "linked_unverified",
        "identity_id": identity.id,
        "provider": provider,
        "provider_user_id": provider_user_id,
        "verification_method": identity.verification_method,
        "verified_at": identity.verified_at.isoformat() if identity.verified_at else None,
        "next_step": (None if ownership_proven else _OUT_OF_BAND_NEXT_STEP),
    }
