"""Pydantic response models for the health endpoint."""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Response for GET /health."""

    status: str
    version: str

    # Issue #5055 (U14), R5 acceptance 5: the environment's identity state must be
    # ASSERTED, not inferred from a code default. These two fields are what make
    # it assertable — a reader (or a deployment check) observes what the running
    # process actually has, instead of reading this repository's defaults and
    # assuming they describe the target.
    #
    # Reported on the unauthenticated health route deliberately: they are booleans
    # about configuration posture, not secrets, and their value is precisely that
    # they can be observed before you hold a credential. No issuer, client id,
    # JWKS URL or key material is exposed here.
    cognito_enabled: bool
    domain_auth_enforced: bool

    # This integration is distinct from existing strict token/domain-grant auth.
    current_identity_required: bool = False
    current_identity_reader_configured: bool = False
