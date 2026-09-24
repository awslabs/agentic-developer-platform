"""Mixed provenance must preserve local spend/defaults without fusing strangers."""

from datetime import date
from decimal import Decimal

import pytest

from src.budget.person_ledger import (
    read_person_partition_spend,
    resolve_applicable_person_limits,
    resolve_member_partitions,
    resolve_person_identity,
    resolve_person_subs,
    resolve_person_team_keys,
)
from src.shared.models.budget import BudgetUsage, PersonBudgetConfig, PersonBudgetDefault
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.budget import PeriodType


@pytest.mark.parametrize(
    "home_method,foreign_method",
    [("oauth", "channel_placement"), ("channel_placement", "oauth"), ("oauth", "oauth")],
)
async def test_asymmetric_proof_cannot_acquire_foreign_spend_defaults_or_person_config(db_session, home_method, foreign_method):
    db = db_session
    for org_id, method, spend, daily in [("home", home_method, "20", "10"), ("foreign", foreign_method, "900", "1")]:
        db.add(Organization(id=org_id, name=org_id))
        await db.flush()
        db.add(User(id=f"user-{org_id}", org_id=org_id, team_id="", email=f"{org_id}@example.test", is_shadow=method == "channel_placement"))
        await db.flush()
        db.add_all(
            [
                UserIdentity(
                    user_id=f"user-{org_id}", org_id=org_id, team_id="", provider="github", provider_user_id="123", verification_method=method
                ),
                TenantMembership(user_id=f"user-{org_id}", tenant_id=org_id, role="member"),
                BudgetUsage(
                    org_id=org_id,
                    entity_type="root_user",
                    entity_id=f"user-{org_id}",
                    period_type="monthly",
                    period_start=date(2026, 9, 1),
                    total_cost_usd=Decimal(spend),
                ),
                PersonBudgetDefault(
                    scope_type="org", scope_id_org=org_id, period_type="daily", budget_amount_usd=Decimal(daily), authored_by_user_id="operator"
                ),
            ]
        )
    db.add_all(
        [
            PersonBudgetDefault(
                scope_type="org", scope_id_org="home", period_type="monthly", budget_amount_usd=Decimal("50"), authored_by_user_id="operator"
            ),
            PersonBudgetConfig(
                person_anchor="github:123",
                period_type="monthly",
                budget_amount_usd=Decimal("500"),
                enforcement_mode="hard",
                authored_by_user_id="operator",
            ),
        ]
    )
    await db.commit()

    anchor, person_ids = await resolve_person_identity(db, "user-home")
    fused = home_method == foreign_method == "oauth"
    assert person_ids == (["user-foreign", "user-home"] if fused else ["user-home"])
    assert anchor == ("github:123" if home_method == "oauth" else "users:user-home")
    partitions = await resolve_member_partitions(db, person_ids, None)
    assert partitions == (["foreign", "home"] if fused else ["home"])
    subs = await resolve_person_subs(db, person_ids)
    total = Decimal("0")
    for org_id in partitions:
        cloud, direct = await read_person_partition_spend(db, org_id, person_ids, subs, PeriodType.MONTHLY, date(2026, 9, 1))
        total += cloud + direct
    assert total == Decimal("920" if fused else "20")
    limits = await resolve_applicable_person_limits(
        db, person_anchor=anchor, org_ids=partitions, team_keys=await resolve_person_team_keys(db, person_ids)
    )
    assert limits["daily"].amount == Decimal("1" if fused else "10")
    assert limits["monthly"].amount == Decimal("500" if home_method == "oauth" else "50")
