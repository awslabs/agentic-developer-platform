"""Unproven external claims cannot grant workspace, approval or budget authority."""

import pytest

from src.budget.person_ledger import resolve_person_identity
from src.orchestration.adapters.github_comments import _resolve_platform_identity
from src.shared.identity.resolver import UnresolvableUserEntityError, _resolve_via_github_identity
from src.shared.identity.workspaces import linked_user_ids
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["self_asserted", "magic_link", "manual", "unknown", "oauth", "admin_manual"])
async def test_only_proven_claims_link_authority_across_consumers(db_session, method):
    db = db_session
    db.add_all([Organization(id="proof-home", name="Home"), Organization(id="proof-work", name="Work")])
    await db.flush()
    login = User(id="proof-login", org_id="proof-home", team_id="", email="home@example.test", cognito_sub="proof-sub")
    target = User(id="proof-target", org_id="proof-work", team_id="", email="work@example.test")
    db.add_all([login, target])
    await db.flush()
    db.add_all(
        [
            UserIdentity(user_id=login.id, org_id=login.org_id, team_id="", provider="github", provider_user_id="932", verification_method=method),
            UserIdentity(user_id=target.id, org_id=target.org_id, team_id="", provider="github", provider_user_id="932", verification_method=method),
            TenantMembership(user_id=target.id, tenant_id=target.org_id, role="org_admin"),
        ]
    )
    await db.commit()
    trusted = method in {"oauth", "admin_manual"}
    linked = await linked_user_ids(db, login, username="GitHub_932")
    assert (target.id in linked) is trusted
    context = await _resolve_platform_identity(db, org_id=target.org_id, github_user_id="932")
    assert (context is not None) is trusted
    assert await _resolve_platform_identity(db, org_id="unrelated-org", github_user_id="932") is None
    _, person_ids = await resolve_person_identity(db, login.id)
    assert (target.id in person_ids) is trusted
    if trusted:
        assert (await _resolve_via_github_identity(db, target.org_id, "932", "github:932")).id == target.id
    else:
        with pytest.raises(UnresolvableUserEntityError):
            await _resolve_via_github_identity(db, target.org_id, "932", "github:932")
