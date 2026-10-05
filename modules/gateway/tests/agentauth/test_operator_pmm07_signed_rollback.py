"""Real PostgreSQL mutation -> live admission -> signed decision across rollback."""
# ruff: noqa: F811 - imported pytest fixtures are injected by name.

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin.persona_models.posture_service import POSTURE_CHANGED_EVENT, set_runtime_posture
from src.agentauth import envelope as envelope_module
from src.agentauth import model_policy as policy_module
from src.agentauth import runtime_posture as runtime
from src.agentauth.envelope import MODEL_POLICY_AUDIENCE, SIGNING_KEY_ENV, SIGNING_KEY_ID_ENV, verify_envelope
from src.proxy.bedrock_routing import BedrockTarget, bedrock_routing_resolver
from src.shared.models.audit import AuditLog
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, Team, TeamMembership, User
from src.shared.models.persona_model_catalogue import ModelInvocabilityEvidence
from src.shared.models.persona_models import PersonaModelPolicySetting
from tests.agentauth.test_model_policy import (
    SONNET,
    _add_invocability_evidence,
    _grant,
    _live_policy_record,
    active_allowlist_revision,
    live_snapshot,
    policy_store,  # noqa: F401
)
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401


@pytest.mark.integration
async def test_audited_rollback_changes_signed_hops_without_mutating_snapshot(pg_url, policy_store, monkeypatch):
    current = datetime.now(UTC).replace(microsecond=0)
    clock = [current]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz is not None else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(policy_module, "datetime", Clock)
    monkeypatch.setattr(runtime, "datetime", Clock)
    monkeypatch.setattr(envelope_module, "datetime", Clock)
    monkeypatch.setenv(runtime.POSTURE_CACHE_TTL_ENV, "30")
    runtime.reset_posture_cache()
    engine = create_async_engine(to_async_url(pg_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            for model in (PersonaModelPolicySetting, Organization, User, TenantMembership, Team, TeamMembership, ModelInvocabilityEvidence, AuditLog):
                await conn.run_sync(model.__table__.create)
        async with sessions() as seed:
            seed.add(
                PersonaModelPolicySetting(compatibility_class="claude-agent-sdk", revision=1, enforcement_posture="report_only", posture_revision=1)
            )
            seed.add(User(id="user-a", org_id="tenant-a", team_id="team-a", email="operator@example.test", cognito_sub="operator-sub"))
            _add_invocability_evidence(seed, account_id="111111111111", outcome="proven", expires_at=current + timedelta(hours=1))
            await seed.commit()
        policy = live_snapshot(allowlist_policy_revision=active_allowlist_revision())
        record = _live_policy_record(policy_store, policy)
        original = policy_store._read("TENANT#tenant-a", "EXEC#run-live-developer")
        digest = original["model_policy_snapshot_digest"]["S"]
        key = Ed25519PrivateKey.generate()
        env = {
            SIGNING_KEY_ENV: key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
            SIGNING_KEY_ID_ENV: "operator-key",
        }
        monkeypatch.setattr(
            bedrock_routing_resolver, "resolve", AsyncMock(return_value=BedrockTarget(account_id="111111111111", region="us-east-1", rung="user"))
        )

        async def hop(seconds, expected_posture, expected_revision):
            clock[0] = current + timedelta(seconds=seconds)
            async with sessions() as session:
                result = await policy_module.bootstrap_model_policy_live(
                    session, store=policy_store, record=record, grant=_grant("github_event"), env=env
                )
            assert result["status"] == "proposed", result
            assert result["posture_verified"] is True
            decision = result["decision"]
            assert (result["posture"], decision["runtime_posture"], decision["posture_revision"]) == (
                expected_posture,
                expected_posture,
                expected_revision,
            )
            assert decision["resolved_model_id"] == SONNET
            assert decision["snapshot_digest"] == digest
            assert decision["snapshot_runtime_posture"] == "report_only"
            verify_envelope(
                result["assertion"],
                public_keys={"operator-key": key.public_key()},
                expected_run_id=record.invocation_id,
                expected_generation=1,
                expected_action="resolve_model",
                expected_command_id=digest,
                request_body=policy_module.canonical_json(decision),
                expected_audience=MODEL_POLICY_AUDIENCE,
                expected_chain_id="chain-a",
                now=clock[0],
            )
            assert policy_store._read("TENANT#tenant-a", "EXEC#run-live-developer") == original
            return result

        first = await hop(0, "report_only", 1)
        async with sessions() as operator:
            await set_runtime_posture(operator, compatibility_class="claude-agent-sdk", posture="enforcing", expected_revision=1, actor_id="user-a")
            await operator.commit()
        await hop(1, "report_only", 1)  # Another replica may still have a valid cached observation.
        enforcing = await hop(30, "enforcing", 2)
        async with sessions() as operator:
            await set_runtime_posture(operator, compatibility_class="claude-agent-sdk", posture="report_only", expected_revision=2, actor_id="user-a")
            await operator.commit()
        await hop(31, "enforcing", 2)
        rolled_back = await hop(60, "report_only", 3)
        assert len({first["assertion"], enforcing["assertion"], rolled_back["assertion"]}) == 3
        async with sessions() as audit_reader:
            audits = list(await audit_reader.scalars(select(AuditLog).where(AuditLog.event_type == POSTURE_CHANGED_EVENT)))
            assert len(audits) == 2
            assert all(a.actor_id == "user-a" for a in audits)
            assert {(a.details["before_posture_revision"], a.details["after_posture_revision"]) for a in audits} == {(1, 2), (2, 3)}
    finally:
        runtime.reset_posture_cache()
        await engine.dispose()
