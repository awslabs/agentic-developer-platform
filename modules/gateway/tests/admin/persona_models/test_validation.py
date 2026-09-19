"""Shared validation function tests — Issue #5420 (PMM-03).

AC-03: Alias resolution in validation path.
AC-04: Fail-closed — no evidence → refused.
AC-07: Retired model rejected.

Operator review items addressed:
  - Item 1: validate_selection is strictly fail-closed.  The only acceptable
    success is backed by a fresh, exact-key evidence row.
  - Item 5: stale evidence → evidence_stale (not probing_disabled).
  - Item 6: principal_kind uses ``service_account`` (not ``service``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.admin.persona_models.catalogue_schemas import SelectionRejection, SelectionResult
from src.admin.persona_models.catalogue_service import validate_selection

from .conftest import DEST_ACCOUNT, DEST_REGION, make_evidence


class TestValidateSelectionFailClosed:
    """Operator item 1: validate_selection is strictly fail-closed."""

    @pytest.mark.asyncio
    async def test_no_evidence_refuses_probing_disabled(self, session):
        """A known model with no evidence is refused, not accepted."""
        result = await validate_selection(
            session,
            org_id="org-1",
            principal_kind="human",
            canonical_principal_id="user-1",
            persona_key="developer",
            model="opus46",
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
        )
        assert isinstance(result, SelectionRejection)
        assert result.reason == "probing_disabled"

    @pytest.mark.asyncio
    async def test_no_destination_refuses(self, session):
        """Without destination context, validation refuses."""
        result = await validate_selection(
            session,
            org_id="org-1",
            principal_kind="human",
            canonical_principal_id="user-1",
            persona_key="developer",
            model="opus46",
        )
        assert isinstance(result, SelectionRejection)
        assert result.reason == "probing_disabled"

    @pytest.mark.asyncio
    async def test_fresh_proven_evidence_succeeds(self, session):
        """The ONLY success path: fresh proven evidence exists for the full key."""
        evidence = make_evidence(
            canonical_model_id="global.anthropic.claude-opus-4-6-v1",
            outcome="proven",
        )
        session.add(evidence)
        await session.commit()

        result = await validate_selection(
            session,
            org_id="org-1",
            principal_kind="human",
            canonical_principal_id="user-1",
            persona_key="developer",
            model="opus46",
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
        )
        assert isinstance(result, SelectionResult)
        assert result.canonical_model_id == "global.anthropic.claude-opus-4-6-v1"
        assert result.compatibility_class == "claude-agent-sdk"
        assert result.harness_contract_revision == "0.3.220"
        assert result.evidence_verified_at is not None

    @pytest.mark.asyncio
    async def test_stale_evidence_refuses_with_evidence_stale(self, session):
        """Operator item 5: stale evidence → evidence_stale, not probing_disabled."""
        evidence = make_evidence(
            canonical_model_id="global.anthropic.claude-opus-4-6-v1",
            outcome="proven",
            verified_at=datetime.now(UTC) - timedelta(hours=48),
            expires_at=datetime.now(UTC) - timedelta(hours=24),
        )
        session.add(evidence)
        await session.commit()

        result = await validate_selection(
            session,
            org_id="org-1",
            principal_kind="human",
            canonical_principal_id="user-1",
            persona_key="developer",
            model="opus46",
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
        )
        assert isinstance(result, SelectionRejection)
        assert result.reason == "evidence_stale"

    @pytest.mark.asyncio
    async def test_refused_evidence_returns_not_invocable(self, session):
        """A probe that recorded refusal → not_invocable."""
        evidence = make_evidence(
            canonical_model_id="global.anthropic.claude-opus-4-6-v1",
            outcome="refused",
            error_code="AccessDeniedException",
            provider_request_id=None,
        )
        session.add(evidence)
        await session.commit()

        result = await validate_selection(
            session,
            org_id="org-1",
            principal_kind="human",
            canonical_principal_id="user-1",
            persona_key="developer",
            model="opus46",
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
        )
        assert isinstance(result, SelectionRejection)
        assert result.reason == "not_invocable"

    @pytest.mark.asyncio
    async def test_evidence_wrong_request_shape_refuses(self, session):
        """Evidence with a different request_shape_sha256 does not satisfy validation."""
        evidence = make_evidence(
            canonical_model_id="global.anthropic.claude-opus-4-6-v1",
            outcome="proven",
            request_shape_sha256="b" * 64,  # Wrong shape
        )
        session.add(evidence)
        await session.commit()

        result = await validate_selection(
            session,
            org_id="org-1",
            principal_kind="human",
            canonical_principal_id="user-1",
            persona_key="developer",
            model="opus46",
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
        )
        assert isinstance(result, SelectionRejection)
        assert result.reason == "probing_disabled"


class TestValidateSelectionGates:
    """Earlier gates — alias resolution, persona, harness, retirement, allowlist."""

    @pytest.mark.asyncio
    async def test_unknown_model_rejected(self, session):
        """An unknown model is rejected with reason unknown_model."""
        result = await validate_selection(
            session,
            org_id="org-1",
            principal_kind="human",
            canonical_principal_id="user-1",
            persona_key="developer",
            model="gpt-4-turbo",
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
        )
        assert isinstance(result, SelectionRejection)
        assert result.reason == "unknown_model"

    @pytest.mark.asyncio
    async def test_unknown_persona_rejected(self, session):
        """An unknown persona is rejected with reason unknown_persona."""
        result = await validate_selection(
            session,
            org_id="org-1",
            principal_kind="human",
            canonical_principal_id="user-1",
            persona_key="nonexistent",
            model="opus46",
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
        )
        assert isinstance(result, SelectionRejection)
        assert result.reason == "unknown_persona"

    @pytest.mark.asyncio
    async def test_non_configurable_persona_rejected(self, session):
        """pt-superpower is not configurable → rejected."""
        result = await validate_selection(
            session,
            org_id="org-1",
            principal_kind="human",
            canonical_principal_id="user-1",
            persona_key="pt-superpower",
            model="opus46",
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
        )
        assert isinstance(result, SelectionRejection)
        assert result.reason == "unknown_persona"

    @pytest.mark.asyncio
    async def test_bare_alias_rejected(self, session):
        """AC-03: A bare alias like 'sonnet' is rejected."""
        result = await validate_selection(
            session,
            org_id="org-1",
            principal_kind="human",
            canonical_principal_id="user-1",
            persona_key="developer",
            model="sonnet",
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
        )
        assert isinstance(result, SelectionRejection)
        assert result.reason == "unknown_model"

    @pytest.mark.asyncio
    async def test_fable_rejected(self, session):
        """Fable 5.1 is not in the catalogue and is rejected (§7, #2300)."""
        result = await validate_selection(
            session,
            org_id="org-1",
            principal_kind="human",
            canonical_principal_id="user-1",
            persona_key="developer",
            model="anthropic.claude-fable-5-1",
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
        )
        assert isinstance(result, SelectionRejection)
        assert result.reason == "unknown_model"


class TestValidateSelectionPrincipalKind:
    """Operator item 6: principal_kind uses ``service_account``."""

    @pytest.mark.asyncio
    async def test_service_account_kind_accepted(self, session):
        """The canonical principal kind for service callers is service_account."""
        evidence = make_evidence(
            canonical_model_id="global.anthropic.claude-sonnet-4-6",
            outcome="proven",
        )
        session.add(evidence)
        await session.commit()

        # service_account is the correct kind (matches PMM-02)
        result = await validate_selection(
            session,
            org_id="org-1",
            principal_kind="service_account",
            canonical_principal_id="svc-1",
            persona_key="developer",
            model="global.anthropic.claude-sonnet-4-6",
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
        )
        assert isinstance(result, SelectionResult)

    @pytest.mark.asyncio
    async def test_reason_codes_are_stable_strings(self, session):
        """Reason codes are the §6.3 vocabulary strings, not prose."""
        # unknown_model
        r1 = await validate_selection(
            session,
            org_id="org-1",
            principal_kind="human",
            canonical_principal_id="user-1",
            persona_key="developer",
            model="nonexistent",
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
        )
        assert isinstance(r1, SelectionRejection)
        assert r1.reason == "unknown_model"

        # unknown_persona
        r2 = await validate_selection(
            session,
            org_id="org-1",
            principal_kind="human",
            canonical_principal_id="user-1",
            persona_key="nonexistent",
            model="global.anthropic.claude-sonnet-4-6",
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
        )
        assert isinstance(r2, SelectionRejection)
        assert r2.reason == "unknown_persona"
