"""Gate 4 — service-principal restrictions — Issue #5420 (PMM-03).

Operator round-4 item 4: catalogue and save validation must both receive real
server-resolved policy inputs.  Before this, ``routes.py`` passed neither, and
``service.py`` carried an explicit "all models pass for now" comment — so the
gate reported permitted for every model unconditionally.

The Agent Registry already stores ``allowed_models`` for IAM service callers,
but before PMM-03 that value stopped at DynamoDB and was never used by model
selection.  These tests prove D3 makes that real server-resolved restriction
load-bearing for catalogue and save validation, including managed principals
with more than one active registry alias.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.admin.persona_models.catalogue_routes import (
    principal_policy_inputs,
    resolve_managed_service_restriction_policy,
    self_service_restriction_policy,
)
from src.admin.persona_models.catalogue_routes import (
    router as persona_models_router,
)
from src.admin.persona_models.catalogue_service import (
    build_model_catalogue,
    evaluate_principal_restrictions,
    validate_selection,
)
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.persona_models import ServicePrincipal, ServicePrincipalAlias

from .conftest import DEFAULT_MODEL_ID, ORG_ID, member_context


class TestPrincipalRestrictionDecisions:
    """The gate-4 decision table."""

    def test_human_with_identity_is_permitted(self):
        permitted, reason = evaluate_principal_restrictions(
            principal_kind="human",
            canonical_principal_id="sub-5420-member",
        )
        assert permitted is True
        assert reason is None

    def test_active_service_account_is_permitted(self):
        permitted, reason = evaluate_principal_restrictions(
            principal_kind="service_account",
            canonical_principal_id="svc-canonical-1",
            principal_status="active",
        )
        assert permitted is True
        assert reason is None

    @pytest.mark.parametrize("status", ["suspended", "retired"])
    def test_blocking_status_refuses(self, status):
        """A suspended or retired principal may select nothing."""
        permitted, reason = evaluate_principal_restrictions(
            principal_kind="service_account",
            canonical_principal_id="svc-canonical-1",
            principal_status=status,
        )
        assert permitted is False
        assert reason == "not_permitted"

    def test_blocking_status_applies_to_humans_too(self):
        """Status is checked before kind — it is not service-only."""
        permitted, _ = evaluate_principal_restrictions(
            principal_kind="human",
            canonical_principal_id="sub-5420-member",
            principal_status="suspended",
        )
        assert permitted is False

    def test_service_account_without_canonical_id_fails_closed(self):
        """R1: the canonical ID owns the preference.

        Without one there is no subject to validate for.  Falling back to a
        mutable name is the aliasing hazard R1 exists to remove, so this must
        refuse rather than substitute.
        """
        permitted, reason = evaluate_principal_restrictions(
            principal_kind="service_account",
            canonical_principal_id=None,
        )
        assert permitted is False
        assert reason == "not_permitted"

    def test_unknown_status_is_not_treated_as_blocking(self):
        """Only the known blocking vocabulary blocks.

        An unrecognized status must not silently deny every selection — that
        would make an enum addition an outage.
        """
        permitted, _ = evaluate_principal_restrictions(
            principal_kind="human",
            canonical_principal_id="sub-1",
            principal_status="some_future_value",
        )
        assert permitted is True


class TestPrincipalInputsFromToken:
    """Inputs are server-resolved from the token, never from a parameter."""

    def test_human_maps_to_canonical_vocabulary(self):
        kind, pid = principal_policy_inputs(member_context())
        assert kind == "human", "canonical vocabulary is human / service_account"
        assert pid == "sub-5420-member"

    def test_service_account_type_maps_to_service_account(self):
        ctx = member_context().model_copy(update={"account_type": "service"})
        kind, pid = principal_policy_inputs(ctx)
        assert kind == "service_account"
        # PMM-02's canonical_service_principal_id does not exist on the token
        # yet, so this is None and gate 4 fails closed — correct pre-merge.
        assert pid is None

    def test_unknown_account_type_takes_the_stricter_kind(self):
        """An unknown principal kind must not be treated as a human."""
        ctx = member_context().model_copy(update={"account_type": "wat"})
        kind, _ = principal_policy_inputs(ctx)
        assert kind == "service_account"

    def test_iam_service_policy_comes_from_authenticated_registry_context(self):
        ctx = member_context().model_copy(
            update={
                "account_type": "service",
                "canonical_alias_source": "agent_registry",
                "registered_allowed_models": ["*sonnet*"],
            }
        )
        pattern_sets, unavailable = self_service_restriction_policy(ctx)
        assert pattern_sets == [["*sonnet*"]]
        assert unavailable is None

    def test_missing_iam_registry_policy_fails_closed(self):
        ctx = member_context().model_copy(
            update={
                "account_type": "service",
                "canonical_alias_source": "agent_registry",
                "registered_allowed_models": None,
            }
        )
        pattern_sets, unavailable = self_service_restriction_policy(ctx)
        assert pattern_sets == []
        assert unavailable == "not_permitted"


class TestManagedServiceRestrictionResolution:
    @pytest.mark.asyncio
    async def test_resolves_every_active_registry_alias(self, session):
        principal = ServicePrincipal(
            canonical_service_principal_id="svc-canonical-1",
            org_id=ORG_ID,
            display_name="Build service",
            status="active",
            approved_by="admin-1",
        )
        session.add(principal)
        session.add_all(
            [
                ServicePrincipalAlias(
                    org_id=ORG_ID,
                    canonical_service_principal_id=principal.canonical_service_principal_id,
                    alias_source="agent_registry",
                    alias_id="builder-a",
                    registered_by="admin-1",
                ),
                ServicePrincipalAlias(
                    org_id=ORG_ID,
                    canonical_service_principal_id=principal.canonical_service_principal_id,
                    alias_source="agent_registry",
                    alias_id="builder-b",
                    registered_by="admin-1",
                ),
            ]
        )
        await session.commit()

        page = SimpleNamespace(
            items=[
                SimpleNamespace(agent_name="builder-a", status="active", allowed_models=["*sonnet*"]),
                SimpleNamespace(agent_name="builder-b", status="active", allowed_models=["global.anthropic.*"]),
            ],
            last_key=None,
        )

        async def _list_agents(**_kwargs):
            return page

        registry = SimpleNamespace(list_agents=_list_agents)
        pattern_sets, unavailable = await resolve_managed_service_restriction_policy(
            session,
            org_id=ORG_ID,
            canonical_service_principal_id=principal.canonical_service_principal_id,
            registry_service=registry,
        )
        assert pattern_sets == [["*sonnet*"], ["global.anthropic.*"]]
        assert unavailable is None

    @pytest.mark.asyncio
    async def test_missing_registry_row_is_policy_unavailable(self, session):
        principal = ServicePrincipal(
            canonical_service_principal_id="svc-canonical-missing",
            org_id=ORG_ID,
            display_name="Missing service",
            status="active",
            approved_by="admin-1",
        )
        session.add(principal)
        session.add(
            ServicePrincipalAlias(
                org_id=ORG_ID,
                canonical_service_principal_id=principal.canonical_service_principal_id,
                alias_source="agent_registry",
                alias_id="missing-builder",
                registered_by="admin-1",
            )
        )
        await session.commit()

        async def _list_agents(**_kwargs):
            return SimpleNamespace(items=[], last_key=None)

        registry = SimpleNamespace(list_agents=_list_agents)
        pattern_sets, unavailable = await resolve_managed_service_restriction_policy(
            session,
            org_id=ORG_ID,
            canonical_service_principal_id=principal.canonical_service_principal_id,
            registry_service=registry,
        )
        assert pattern_sets == []
        assert unavailable == "not_permitted"


class TestCatalogueAppliesGate4:
    @pytest.mark.asyncio
    async def test_blocked_principal_sees_every_model_unselectable(self, session):
        """AC-01: stay honest about what exists, but permit nothing.

        An empty list would be indistinguishable from "the platform has no
        models", so each row is enumerated with its refusal reason.
        """
        rows = await build_model_catalogue(
            session,
            persona_key="developer",
            account_id="111111115420",
            region="us-east-1",
            principal_kind="service_account",
            canonical_principal_id=None,
        )

        assert rows, "models must still be enumerated"
        assert all(r.selectable is False for r in rows)
        assert all(r.reason == "not_permitted" for r in rows)
        assert all(r.permitted is False for r in rows)

    @pytest.mark.asyncio
    async def test_permitted_principal_reaches_the_evidence_gate(self, session):
        """A permitted principal falls through to gate 5, not past it.

        With no evidence recorded the result is still unselectable — but for
        the *evidence* reason, which is what distinguishes gate 4 passing from
        gate 4 being absent.
        """
        rows = await build_model_catalogue(
            session,
            persona_key="developer",
            account_id="111111115420",
            region="us-east-1",
            principal_kind="service_account",
            canonical_principal_id="svc-canonical-1",
            principal_status="active",
        )

        claude_rows = [r for r in rows if r.reason != "harness_incompatible"]
        assert claude_rows
        assert all(r.reason == "probing_disabled" for r in claude_rows)

    @pytest.mark.asyncio
    async def test_registry_allowed_models_restricts_catalogue(self, session):
        rows = await build_model_catalogue(
            session,
            persona_key="developer",
            account_id="111111115420",
            region="us-east-1",
            principal_kind="service_account",
            canonical_principal_id="svc-canonical-1",
            principal_status="active",
            service_restriction_pattern_sets=[["*opus*"]],
        )
        sonnet = next(row for row in rows if row.canonical_model_id == DEFAULT_MODEL_ID)
        assert sonnet.selectable is False
        assert sonnet.permitted is False
        assert sonnet.reason == "not_permitted"


class TestValidationAppliesGate4:
    @pytest.mark.asyncio
    async def test_save_validation_refuses_blocked_principal(self, session):
        """The save path must apply the same gate as the catalogue (§6.3)."""
        result = await validate_selection(
            session,
            org_id=ORG_ID,
            principal_kind="service_account",
            canonical_principal_id="svc-canonical-1",
            principal_status="suspended",
            persona_key="developer",
            model=DEFAULT_MODEL_ID,
            account_id="111111115420",
            region="us-east-1",
        )
        assert result.reason == "not_permitted"

    @pytest.mark.asyncio
    async def test_principal_gate_precedes_evidence_gate(self, session):
        """A blocked principal is refused for policy, not for missing evidence.

        Reporting `probing_disabled` to a suspended principal would send an
        operator hunting for a probe to run when the real answer is that the
        principal is suspended.
        """
        result = await validate_selection(
            session,
            org_id=ORG_ID,
            principal_kind="service_account",
            canonical_principal_id=None,
            persona_key="developer",
            model=DEFAULT_MODEL_ID,
            account_id="111111115420",
            region="us-east-1",
        )
        assert result.reason == "not_permitted"
        assert result.reason != "probing_disabled"

    @pytest.mark.asyncio
    async def test_registry_allowed_models_restricts_save_validation(self, session):
        result = await validate_selection(
            session,
            org_id=ORG_ID,
            principal_kind="service_account",
            canonical_principal_id="svc-canonical-1",
            principal_status="active",
            persona_key="developer",
            model=DEFAULT_MODEL_ID,
            account_id="111111115420",
            region="us-east-1",
            service_restriction_pattern_sets=[["*opus*"]],
        )
        assert result.reason == "not_permitted"


class TestRouteWiresPolicyInputs:
    @pytest.mark.asyncio
    async def test_service_caller_gets_not_permitted_end_to_end(self, session):
        """The route passes real inputs — previously it passed none."""
        app = FastAPI()
        app.include_router(persona_models_router)

        async def _db():
            yield session

        ctx = member_context().model_copy(update={"account_type": "service"})
        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_current_user] = lambda: ctx

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/me/persona-models/catalog",
                params={"persona_key": "developer"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["models"], "models must still be listed"
        assert all(m["reason"] == "not_permitted" for m in body["models"])
