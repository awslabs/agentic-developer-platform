"""A Dev Box host run routes as its human owner (#8223 cause 32b).

The box authenticates as the platform's guest agent (org ``__platform__``), so its
attributed tenant can never equal the owner's tenant on the run row. Once the gate
has proven the instance and role, the row's server-written tenant is the binding
tenant; every other hosted caller still asserts its own.
"""

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from src.proxy.bedrock_enforcement import resolve_routing_decision
from src.proxy.bedrock_principal import BedrockRoutingIdentityError
from src.proxy.bedrock_routing import bedrock_routing_resolver
from src.proxy.bedrock_signing import bedrock_destination_signer
from src.proxy.service import _current_agent_run_id
from src.shared.schemas.auth import TokenContext
from tests.proxy import test_bedrock_enforcement as routing_fixtures
from tests.proxy.test_bedrock_enforcement import CANONICAL_USER_ID, ORG_ID, _credentials, _patch_routing_session, _seed_enforced_org_mapping

session_factory = routing_fixtures.session_factory
routing_environment = routing_fixtures.routing_environment

INSTANCE = "i-0b88826afeaa19a6e"
GUEST_ROLE = "arn:aws:iam::000000000000:role/adp-dev-box-guest"
RUN_ID = "989971ff-1748-44ee-831e-930b54d6717c"


@pytest.fixture(autouse=True)
def reset_routing():
    bedrock_routing_resolver._mappings_exist_cache = None
    token = _current_agent_run_id.set(None)
    yield
    _current_agent_run_id.reset(token)
    bedrock_routing_resolver._mappings_exist_cache = None


def host_context(*, instance: str | None = INSTANCE, role: str | None = GUEST_ROLE) -> TokenContext:
    """What the IAM gate hands the invoke route for a verified Dev Box host."""
    context = TokenContext(
        user_id="dev-box-guest-adp-dev-dev-box",
        org_id="__platform__",
        team_id="__agents__",
        department_id="",
        account_type="service",
        auth_source="iam",
        scope="internal",
        requires_host_identity=True,
        attributed_org_id="__platform__",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    context._dev_box_instance_id = instance
    context._dev_box_role_arn = role
    return context


def host_row(**updates):
    now = int(time.time())
    return {
        "kind": "crew",
        "target_kind": "ec2-instance",
        "executor_scope": "session",
        "status": "active",
        "tenant_id": ORG_ID,
        "user_id": CANONICAL_USER_ID,
        "root_human_id": CANONICAL_USER_ID,
        "is_human_rooted": True,
        "session_id": "box-1",
        "deployment_id": "dep-1",
        "target_ref": INSTANCE,
        "host_role_arn": GUEST_ROLE,
        "lease_generation": 24,
        "lease_expires_at": now + 3600,
        "expires_at": now + 86400,
        **updates,
    }


async def test_verified_host_run_routes_as_its_owner(session_factory, routing_environment):
    await _seed_enforced_org_mapping(session_factory)
    context = host_context()
    signer = AsyncMock(return_value=_credentials())
    with (
        _patch_routing_session(session_factory),
        patch.object(bedrock_destination_signer, "get_credentials", signer),
        patch("src.proxy.bedrock_principal.read_routing_run", AsyncMock(return_value=host_row())),
        patch("src.cloud_desktop.host_identity.current_host", AsyncMock(return_value=object())),
    ):
        decision = await resolve_routing_decision(context, agent_run_id=RUN_ID)
    # The owner's org mapping governs — the box's platform identity selected nothing.
    assert decision.target is not None and not decision.target.is_platform
    assert signer.call_args.kwargs["user_id"] == CANONICAL_USER_ID
    assert context._crew_root_human_id == CANONICAL_USER_ID
    assert context._crew_root_run_id == RUN_ID
    assert context._dev_box_host_run_id == RUN_ID


@pytest.mark.parametrize(
    "updates,context_kwargs,reason",
    [
        ({"target_ref": "i-0000000000000000f"}, {}, "host_fenced"),
        ({"host_role_arn": "arn:aws:iam::000000000000:role/other"}, {}, "host_fenced"),
        ({}, {"role": None}, "host_fenced"),
        ({"tenant_id": ""}, {}, "tenant_mismatch"),
        # The host branch requires root_human_id == user_id before any binding runs.
        ({"root_human_id": "someone-else"}, {}, "host_fenced"),
    ],
)
async def test_host_run_still_refused_when_identity_or_row_disagrees(session_factory, routing_environment, updates, context_kwargs, reason):
    await _seed_enforced_org_mapping(session_factory)
    with (
        _patch_routing_session(session_factory),
        patch("src.proxy.bedrock_principal.read_routing_run", AsyncMock(return_value=host_row(**updates))),
        patch("src.cloud_desktop.host_identity.current_host", AsyncMock(return_value=object())),
    ):
        with pytest.raises(BedrockRoutingIdentityError) as exc:
            await resolve_routing_decision(host_context(**context_kwargs), agent_run_id=RUN_ID)
    assert exc.value.details["reason"] == reason


async def test_non_host_worker_still_asserts_its_own_tenant(session_factory, routing_environment):
    """A pod worker gains nothing from the host branch: a tenant it does not assert still refuses."""
    await _seed_enforced_org_mapping(session_factory)
    worker = TokenContext(
        user_id="scaledjob-worker",
        org_id="__platform__",
        team_id="__agents__",
        department_id="",
        account_type="service",
        auth_source="iam",
        scope="internal",
        attributed_org_id="__platform__",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    pod_row = {k: v for k, v in host_row().items() if k not in ("target_ref", "host_role_arn")}
    pod_row.update({"target_kind": "eks-pod", "status": "in_progress", "kind": "worker"})
    with (
        _patch_routing_session(session_factory),
        patch("src.proxy.bedrock_principal.read_routing_run", AsyncMock(return_value=pod_row)),
    ):
        with pytest.raises(BedrockRoutingIdentityError) as exc:
            await resolve_routing_decision(worker, agent_run_id=RUN_ID)
    assert exc.value.details["reason"] == "tenant_mismatch"


async def test_refusal_reason_is_logged(session_factory, routing_environment, caplog):
    await _seed_enforced_org_mapping(session_factory)
    with (
        _patch_routing_session(session_factory),
        patch("src.proxy.bedrock_principal.read_routing_run", AsyncMock(return_value=host_row(target_ref="i-0000000000000000f"))),
        patch("src.cloud_desktop.host_identity.current_host", AsyncMock(return_value=object())),
        caplog.at_level("WARNING", logger="src.proxy.bedrock_enforcement"),
    ):
        with pytest.raises(BedrockRoutingIdentityError):
            await resolve_routing_decision(host_context(), agent_run_id=RUN_ID)
    assert any("reason=host_fenced" in record.getMessage() and RUN_ID in record.getMessage() for record in caplog.records)
