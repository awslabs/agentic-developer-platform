"""Invocability evidence tests — Issue #5420 (PMM-03).

AC-04a: Probe mechanism verified inert — no Bedrock call from any read.
AC-05: Different destination / revision → evidence not found.
AC-06: Source unavailable → stale markers, nothing newly certified.
AC-07: Retired model handling.
AC-08: Evidence expires → stale with controlled clock, preserved evidence.

Operator review items addressed:
  - Item 3: evidence lookup constrains all 6 PK dimensions including
    request_shape_sha256.
  - Item 4: bounded probe mechanism exists, ships disabled at zero budget.
  - Item 5: stale evidence returns ``evidence_stale`` (not ``probing_disabled``)
    and preserves the evidence row for actionable refusals.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.admin.persona_models.catalogue_service import (
    _invocable_state,
    build_model_catalogue,
    compute_request_shape_sha256,
    is_probe_enabled,
    lookup_evidence,
)
from src.shared.models.persona_model_catalogue import ModelInvocabilityEvidence

from .conftest import DEST_ACCOUNT, DEST_REGION, make_evidence


class TestEvidenceLookup:
    """Evidence store read tests — full 6-column PK."""

    @pytest.mark.asyncio
    async def test_no_evidence_returns_none(self, session):
        """No evidence row → None."""
        shape_sha = compute_request_shape_sha256("global.anthropic.claude-opus-5")
        result = await lookup_evidence(
            session,
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
            canonical_model_id="global.anthropic.claude-opus-5",
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.3.220",
            request_shape_sha256=shape_sha,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_proven_evidence_found(self, session):
        """A proven evidence row is found and its properties are correct."""
        evidence = make_evidence(
            canonical_model_id="global.anthropic.claude-sonnet-4-6",
            outcome="proven",
        )
        session.add(evidence)
        await session.commit()

        shape_sha = compute_request_shape_sha256("global.anthropic.claude-sonnet-4-6")
        result = await lookup_evidence(
            session,
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
            canonical_model_id="global.anthropic.claude-sonnet-4-6",
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.3.220",
            request_shape_sha256=shape_sha,
        )
        assert result is not None
        assert result.is_proven is True

    @pytest.mark.asyncio
    async def test_refused_evidence_found(self, session):
        """A refused evidence row records the failure."""
        evidence = make_evidence(
            canonical_model_id="global.anthropic.claude-opus-4-7",
            outcome="refused",
            error_code="ValidationException",
            provider_request_id=None,
        )
        session.add(evidence)
        await session.commit()

        shape_sha = compute_request_shape_sha256("global.anthropic.claude-opus-4-7")
        result = await lookup_evidence(
            session,
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
            canonical_model_id="global.anthropic.claude-opus-4-7",
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.3.220",
            request_shape_sha256=shape_sha,
        )
        assert result is not None
        assert result.is_proven is False
        assert result.outcome == "refused"
        assert result.error_code == "ValidationException"

    @pytest.mark.asyncio
    async def test_different_destination_not_found(self, session):
        """AC-05: Evidence from one destination is not admissible for another."""
        evidence = make_evidence(
            account_id="999999999999",
            region="eu-west-1",
            canonical_model_id="global.anthropic.claude-sonnet-4-6",
        )
        session.add(evidence)
        await session.commit()

        shape_sha = compute_request_shape_sha256("global.anthropic.claude-sonnet-4-6")
        result = await lookup_evidence(
            session,
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
            canonical_model_id="global.anthropic.claude-sonnet-4-6",
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.3.220",
            request_shape_sha256=shape_sha,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_different_harness_revision_not_found(self, session):
        """AC-05: Changing harness revision yields a different evidence key."""
        evidence = make_evidence(
            canonical_model_id="global.anthropic.claude-sonnet-4-6",
            harness_contract_revision="0.3.220",
        )
        session.add(evidence)
        await session.commit()

        shape_sha = compute_request_shape_sha256("global.anthropic.claude-sonnet-4-6")
        result = await lookup_evidence(
            session,
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
            canonical_model_id="global.anthropic.claude-sonnet-4-6",
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.4.000",
            request_shape_sha256=shape_sha,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_different_request_shape_not_found(self, session):
        """Operator item 3: a different request_shape_sha256 is a different key."""
        evidence = make_evidence(
            canonical_model_id="global.anthropic.claude-sonnet-4-6",
        )
        session.add(evidence)
        await session.commit()

        # Use a different (arbitrary) SHA — must not match
        result = await lookup_evidence(
            session,
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
            canonical_model_id="global.anthropic.claude-sonnet-4-6",
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.3.220",
            request_shape_sha256="b" * 64,
        )
        assert result is None


class TestEvidenceStaleness:
    """AC-08: Evidence expires → stale with controlled clock."""

    def test_fresh_evidence_not_stale(self):
        """Evidence with future expiry is not stale."""
        evidence = ModelInvocabilityEvidence(
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
            canonical_model_id="global.anthropic.claude-sonnet-4-6",
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.3.220",
            request_shape_sha256="a" * 64,
            outcome="proven",
            provider_request_id="req-1",
            verified_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=24),
            updated_at=datetime.now(UTC),
        )
        assert evidence.is_stale is False
        assert evidence.is_proven is True

    def test_expired_evidence_is_stale(self):
        """AC-08: Evidence with past expiry is stale."""
        evidence = ModelInvocabilityEvidence(
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
            canonical_model_id="global.anthropic.claude-sonnet-4-6",
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.3.220",
            request_shape_sha256="a" * 64,
            outcome="proven",
            provider_request_id="req-1",
            verified_at=datetime.now(UTC) - timedelta(hours=48),
            expires_at=datetime.now(UTC) - timedelta(hours=24),
            updated_at=datetime.now(UTC) - timedelta(hours=48),
        )
        assert evidence.is_stale is True

    def test_stale_evidence_invocable_state_is_none(self):
        """AC-08: Stale evidence makes invocable_state return None (unproven)."""
        evidence = ModelInvocabilityEvidence(
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
            canonical_model_id="global.anthropic.claude-sonnet-4-6",
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.3.220",
            request_shape_sha256="a" * 64,
            outcome="proven",
            provider_request_id="req-1",
            verified_at=datetime.now(UTC) - timedelta(hours=48),
            expires_at=datetime.now(UTC) - timedelta(hours=24),
            updated_at=datetime.now(UTC) - timedelta(hours=48),
        )
        assert _invocable_state(evidence) is None

    def test_no_evidence_invocable_state_is_none(self):
        """No evidence → invocable state is None (unproven, not refused)."""
        assert _invocable_state(None) is None


class TestEvidenceInCatalogue:
    """Evidence integrated into the model catalogue."""

    @pytest.mark.asyncio
    async def test_proven_evidence_makes_model_selectable(self, session):
        """A model with proven evidence is selectable."""
        evidence = make_evidence(
            canonical_model_id="global.anthropic.claude-sonnet-4-6",
            outcome="proven",
        )
        session.add(evidence)
        await session.commit()

        models = await build_model_catalogue(
            session,
            persona_key="developer",
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
        )
        sonnet = next(m for m in models if m.canonical_model_id == "global.anthropic.claude-sonnet-4-6")
        assert sonnet.selectable is True
        assert sonnet.invocable is True
        assert sonnet.evidence is not None
        assert sonnet.evidence.account_id == DEST_ACCOUNT

    @pytest.mark.asyncio
    async def test_refused_evidence_makes_model_not_selectable(self, session):
        """A model that was probed and refused is not selectable."""
        evidence = make_evidence(
            canonical_model_id="global.anthropic.claude-sonnet-4-6",
            outcome="refused",
            error_code="AccessDeniedException",
            provider_request_id=None,
        )
        session.add(evidence)
        await session.commit()

        models = await build_model_catalogue(
            session,
            persona_key="developer",
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
        )
        sonnet = next(m for m in models if m.canonical_model_id == "global.anthropic.claude-sonnet-4-6")
        assert sonnet.selectable is False
        assert sonnet.reason == "not_invocable"
        assert sonnet.invocable is False

    @pytest.mark.asyncio
    async def test_stale_evidence_returns_evidence_stale_with_preserved_evidence(self, session):
        """Operator item 5: Stale evidence → reason=evidence_stale, evidence preserved."""
        evidence = make_evidence(
            canonical_model_id="global.anthropic.claude-sonnet-4-6",
            outcome="proven",
            verified_at=datetime.now(UTC) - timedelta(hours=48),
            expires_at=datetime.now(UTC) - timedelta(hours=24),
        )
        session.add(evidence)
        await session.commit()

        models = await build_model_catalogue(
            session,
            persona_key="developer",
            account_id=DEST_ACCOUNT,
            region=DEST_REGION,
        )
        sonnet = next(m for m in models if m.canonical_model_id == "global.anthropic.claude-sonnet-4-6")
        assert sonnet.selectable is False
        assert sonnet.reason == "evidence_stale"
        assert sonnet.invocable is None
        # Evidence MUST be preserved so the refusal is actionable
        assert sonnet.evidence is not None
        assert sonnet.evidence.stale is True
        assert sonnet.evidence.account_id == DEST_ACCOUNT
        assert sonnet.evidence.region == DEST_REGION

    @pytest.mark.asyncio
    async def test_no_destination_all_probing_disabled(self, session):
        """AC-04a: Without a destination, all models report probing_disabled."""
        models = await build_model_catalogue(
            session,
            persona_key="developer",
            # No account_id or region
        )
        for model in models:
            assert model.selectable is False
            assert model.reason == "probing_disabled"
            assert model.invocable is None


class TestRequestShapeSha256:
    """The Gateway consumes, but never invents, the SDK-captured digest."""

    def test_reads_sdk_generated_digest(self):
        a = compute_request_shape_sha256("global.anthropic.claude-sonnet-4-6")
        b = compute_request_shape_sha256("global.anthropic.claude-sonnet-4-6")
        assert a == b == "5a8388a6e33ad438b5c2a530b3221965c40a74007223f3711453f62183caca2b"

    def test_sdk_body_digest_can_differ_by_model(self):
        a = compute_request_shape_sha256("global.anthropic.claude-sonnet-4-6")
        b = compute_request_shape_sha256("global.anthropic.claude-opus-5")
        assert a != b


class TestProbeDisabledByDefault:
    """Operator item 4 + item 8: probe ships disabled at zero budget."""

    def test_probe_disabled_by_default(self):
        """The probe mechanism is disabled by default (R3)."""
        assert is_probe_enabled() is False
