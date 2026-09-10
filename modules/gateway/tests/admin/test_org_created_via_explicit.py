"""Issue #4842 (R6=a): org provenance is STAMPED by the creating path, never inherited.

``organizations.created_via`` is the signal the #2724 webhook auto-register gate
reads to decide whether an installing GitHub org is a tenant an ADP operator or an
authenticated flow onboarded, or a shell the platform auto-created for whoever
clicked Install on a public App. Tenant *existence* cannot carry that distinction
— the unauthenticated no-nonce install callback creates the row itself — so
provenance carries it instead.

The defect these tests close is not a wrong value. It is a value nobody chose.
Three creation paths never passed ``created_via`` and so took the model default
``"operator"``, which is inside ``TRUSTED_CREATED_VIA``. Those tenants passed the
provenance gate and were promotable — correct behaviour, reached by accident. The
column default exists to *grandfather in* pre-migration rows; it was never meant
to be the trust policy for new ones. Ruling R6 chose (a): keep the value, state it
explicitly.

Why the assertions are shaped the way they are
----------------------------------------------
``assert org.created_via == "operator"`` passes whether the code set it or the
column default did — so it cannot distinguish the fix from the bug, and would go
green against unpatched code. ``_ProvenanceProbe`` closes that: a ``before_insert``
mapper event observes the instance *before* SQLAlchemy applies column defaults, so
an unset attribute reads as ``None``. A path that relies on the default is
therefore a FAILURE here, which is the whole point.

Precedent: ``tests/migrations/test_025_org_created_via.py`` covers the column's
migration (that the default IS the backfill). This file covers the writers.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.identity.organizations_service import OrganizationsService
from src.admin.identity.schemas import OrganizationCreateRequest as IdentityOrgCreateRequest
from src.admin.onboarding.approval import approve_request
from src.admin.schemas import OrganizationCreateRequest as AdminOrgCreateRequest
from src.admin.service import AdminService
from src.shared.models.base import new_uuid
from src.shared.models.onboarding import TenantAccessRequest
from src.shared.models.organization import (
    CREATED_VIA_INSTALL_AUTOCREATE,
    CREATED_VIA_OPERATOR,
    CREATED_VIA_REGISTER_FLOW,
    TRUSTED_CREATED_VIA,
    Organization,
)

pytestmark = pytest.mark.asyncio

V2_ON = {"USER_IDENTITY_INDEX_V2_WRITE": "true"}


class _ProvenanceProbe:
    """Records ``created_via`` as the application set it, before column defaults.

    A ``before_insert`` listener fires while the instance still holds only what
    the application assigned. ``getattr`` on an unset column attribute returns
    None there, so "the code stamped it" and "the column default filled it in"
    are distinguishable — which a post-commit read of the row is not.
    """

    def __init__(self) -> None:
        self.seen: dict[str, str | None] = {}

    def __enter__(self) -> _ProvenanceProbe:
        event.listen(Organization, "before_insert", self._record, propagate=True)
        return self

    def __exit__(self, *exc_info: object) -> None:
        event.remove(Organization, "before_insert", self._record)

    def _record(self, _mapper: object, _connection: object, target: Organization) -> None:
        self.seen[target.id] = getattr(target, "created_via", None)

    def only(self) -> str | None:
        assert len(self.seen) == 1, f"expected exactly one Organization insert, saw {self.seen}"
        return next(iter(self.seen.values()))


def _pending_request(*, tenant_id: str = "acme-approved", login: str = "acmeowner") -> TenantAccessRequest:
    return TenantAccessRequest(
        id=new_uuid(),
        cognito_sub="cognito-sub-owner",
        provider="github",
        provider_user_id="90001",
        proposed_tenant_id=tenant_id,
        target_login=login,
        motivation="new workspace",
        status="pending",
    )


# ---------------------------------------------------------------------------
# Site 1: the canonical org-create route's service (D4 = Option A)
# ---------------------------------------------------------------------------


async def test_identity_create_organization_stamps_operator_explicitly(db_session: AsyncSession):
    """The canonical route states its provenance rather than inheriting it."""
    svc = OrganizationsService(db_session, identity_index=AsyncMock(), cognito_sync=AsyncMock())

    with _ProvenanceProbe() as probe:
        await svc.create_organization(IdentityOrgCreateRequest(id="canonical-org", name="Canonical Org"))

    assert probe.only() == CREATED_VIA_OPERATOR, (
        "created_via was not set by the canonical create path — it fell through to the column "
        "default, which means this tenant's trust was inherited, not decided (issue #4842, R6)."
    )

    org = (await db_session.execute(select(Organization).where(Organization.id == "canonical-org"))).scalar_one()
    assert org.created_via in TRUSTED_CREATED_VIA


# ---------------------------------------------------------------------------
# Site 2: AdminService.create_organization
#
# Still tested even though its HTTP route is now 410: the service function is
# live (it is the pre-existing admin-org writer, and #4841's admin UI work may
# route to it), so a provenance regression here would be just as silent.
# ---------------------------------------------------------------------------


async def test_admin_service_create_organization_stamps_operator_explicitly(db_session: AsyncSession):
    """AdminService states its provenance rather than inheriting it."""
    svc = AdminService(db_session)

    with _ProvenanceProbe() as probe:
        await svc.create_organization(AdminOrgCreateRequest(name="Admin Org"))

    assert probe.only() == CREATED_VIA_OPERATOR, "AdminService.create_organization relied on the column default for created_via (issue #4842, R6)."


# ---------------------------------------------------------------------------
# Site 3: access-request approval
#
# This is the path the design note did NOT find — it was discovered while
# verifying #4842 against main, which is why it gets its own test.
# ---------------------------------------------------------------------------


@patch.dict(os.environ, V2_ON)
async def test_approve_request_stamps_operator_explicitly(db_session: AsyncSession):
    """An approved access request states its provenance rather than inheriting it."""
    request = _pending_request()
    db_session.add(request)
    await db_session.flush()

    with _ProvenanceProbe() as probe:
        with patch("src.admin.onboarding.approval.sync_cognito_role_claims"):
            tenant_id = await approve_request(db=db_session, request=request, admin_sub="admin-1")

    assert tenant_id == "acme-approved"
    assert probe.only() == CREATED_VIA_OPERATOR, (
        "approve_request relied on the column default for created_via. An approved access request "
        "yields a TRUSTED tenant, so that has to be an assertion in code (issue #4842, R6)."
    )


# ---------------------------------------------------------------------------
# The negative half: a path that must NOT be trusted, and the wire contract
# ---------------------------------------------------------------------------


async def test_install_autocreate_is_not_trusted():
    """The self-created shell stays outside the trusted set.

    Without this, a well-meaning "add the missing value to TRUSTED_CREATED_VIA"
    change would silently promote every tenant created by the unauthenticated
    no-nonce install callback — which is the exact door #2724 closed.
    """
    assert CREATED_VIA_INSTALL_AUTOCREATE not in TRUSTED_CREATED_VIA
    assert TRUSTED_CREATED_VIA == frozenset({CREATED_VIA_OPERATOR, CREATED_VIA_REGISTER_FLOW})


async def test_trusted_set_matches_the_webhook_lambda_mirror():
    """``TRUSTED_CREATED_VIA`` and the Lambda's ``TRUSTED_PROVENANCE`` must agree.

    The two live in separate deploy units, so the values are a wire contract
    carried by ``POST /internal/v1/resolve-installation`` (documented at
    ``shared/models/organization.py``). Divergence is not a lint error; it is a
    gate that reads one vocabulary while the other half writes another. Read from
    the Lambda source text rather than imported — the Lambda is not on this
    module's import path, and parsing the literal set is what proves the two files
    agree.
    """
    from pathlib import Path

    lambda_src = Path(__file__).resolve().parents[3] / "agent-factory" / "webhook-ingress" / "lambda" / "common" / "gateway_client.py"
    assert lambda_src.is_file(), f"webhook Lambda mirror not found at {lambda_src}"

    text = lambda_src.read_text()
    for value in TRUSTED_CREATED_VIA:
        assert f'"{value}"' in text, f"trusted provenance {value!r} is missing from the webhook Lambda's mirror ({lambda_src})"
    assert f'"{CREATED_VIA_INSTALL_AUTOCREATE}"' in text
    # The mirror's trusted set is a literal frozenset of the two trusted names.
    assert "TRUSTED_PROVENANCE = frozenset({CREATED_VIA_OPERATOR, CREATED_VIA_REGISTER_FLOW})" in text, (
        "the webhook Lambda's TRUSTED_PROVENANCE no longer matches TRUSTED_CREATED_VIA — the two are a wire contract and must not diverge"
    )
