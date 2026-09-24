"""Tests for the live-control gateway path — Issue #3960.

The story ships an authenticated control path with **no working verbs**, so the
tests that matter most are about what the path refuses. Four groups:

**Authorization.** Unknown run, another tenant's run, and a same-tenant run the
caller does not own must be indistinguishable — all 404, no detail that separates
them. This is the enumeration-oracle defence, and it is asserted on the response
*body* as well as the status, because a helpful ``detail`` string reintroduces the
oracle a shared status code was chosen to remove.

**Status mapping.** 401 → 404 → 503 → 410 → 501 → 409 is an ordering, not a set.
Each test pins one boundary by constructing a row that satisfies every earlier
check and fails exactly one, so a reordering of the gate cannot pass.

**Transport hardening.** The destination validator is the SSRF boundary. It is
tested with the addresses an attacker would actually reach for — loopback, IMDS,
a public IP, a hostname — plus the fail-closed case of an unconfigured CIDR list.

**Non-leakage.** The bearer token and the pod address must appear in no response
body. Asserted against the serialised response rather than the model, since a leak
would arrive through serialisation.

Both adapters (activity and orchestration) are driven through the same scenarios,
because the reason the shared service exists is that two authorization paths drift
apart. A test that only covered one would not notice.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.activity.control_schemas import MAX_INSTRUCTION_CHARS, MAX_REQUEST_BYTES
from src.activity.control_service import (
    POD_OUTCOME_STATUSES,
    SUPPORTED_ACTIONS,
    UNMAPPED_POD_STATUS,
    ControlError,
    ControlService,
    ControlTarget,
    validate_control_destination,
)
from src.activity.routes import get_control_service
from src.activity.routes import router as activity_router
from src.agentauth.envelope import SIGNING_KEY_ENV, SIGNING_KEY_ID_ENV
from src.auth.dependencies import get_current_user
from src.orchestration.controls import get_run_control_service
from src.orchestration.controls import router as orchestration_router
from src.shared.database import get_db
from tests.agentauth.test_envelope import _keypair

CANONICAL_USER_ID = "canonical-abc-999"
TENANT_ID = "org-tenant-001"
RUN_ID = "msg-run-001"
POD_IP = "10.42.3.17"
CONTROL_PORT = 8770
TOKEN = "pod-minted-control-token-value"

# A CIDR set that contains POD_IP. Passed explicitly everywhere rather than left
# to the ambient environment, so no test depends on a deployment's config.
ENV = {
    "AGENT_AUTHORITY_ENABLED": "true",
    "AGENT_AUTHORITY_TABLE": "test-authority",
    SIGNING_KEY_ENV: _keypair()[0],
    SIGNING_KEY_ID_ENV: "test-control-key",
    "FEATURE_AGENT_CONTROL_ENABLED": "true",
    "AGENT_CONTROL_CLUSTER_POD_CIDRS": "10.42.0.0/16",
    "AGENT_CONTROL_PORT": str(CONTROL_PORT),
}
ENV_FLAG_OFF = {**ENV, "FEATURE_AGENT_CONTROL_ENABLED": "false"}

ALL_ACTIONS = ["pause", "resume", "steer", "abort"]
COMMAND_ID = "3f8c1d64-1c1e-4a5f-9b2a-77c0d3a1b2e5"


def valid_body(action: str) -> dict:
    """The minimal *valid* body for a verb.

    Steer needs an instruction; the other three do not. Kept in one helper so the
    status-mapping tests below exercise the gate rather than accidentally passing
    through the body validator — a bare ``{"command_id": ...}`` on steer is a
    legitimate 400 and would mask whichever gate the test meant to assert.
    """
    if action == "steer":
        return {"command_id": COMMAND_ID, "instruction": "prefer the smaller refactor"}
    return {"command_id": COMMAND_ID}


def row(
    *,
    status: str = "in_progress",
    user_id: str = CANONICAL_USER_ID,
    tenant_id: str = TENANT_ID,
    address: str | None = POD_IP,
    port: int | None = CONTROL_PORT,
    token: str | None = TOKEN,
    generation: int = 1,
    expires_in_minutes: int = 30,
    root_human_id: str | None = None,
    **extra,
) -> dict:
    """Build a webhook-events row as the DynamoDB resource API returns one.

    Numbers are ``Decimal`` because that is what the resource API yields; using
    ``int`` here would hide a coercion bug that only appears against real DynamoDB.
    """
    item: dict = {
        "event_id": RUN_ID,
        "arrived_at": "2026-09-12T10:00:00Z",
        "status": status,
        "user_id": user_id,
        "tenant_id": tenant_id,
    }
    if root_human_id is not None:
        item["root_human_id"] = root_human_id
    if address is not None:
        item["control_address"] = address
    if port is not None:
        item["control_port"] = Decimal(port)
    if token is not None:
        item["control_token"] = token
    item["control_generation"] = Decimal(generation)
    if expires_in_minutes is not None:
        expiry = datetime(2026, 9, 12, 12, 0, tzinfo=UTC) + timedelta(minutes=expires_in_minutes)
        item["control_token_expires_at"] = expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
    item.update(extra)
    return item


def make_table(items: list[dict] | None = None) -> MagicMock:
    table = MagicMock()
    table.query = MagicMock(return_value={"Items": items if items is not None else []})
    return table


def make_service(
    *,
    items: list[dict] | None = None,
    env: dict | None = None,
    pod_status: int = 200,
    pod_body: dict | None = None,
    transport_error: Exception | None = None,
    table: MagicMock | None = None,
) -> ControlService:
    """Build a ControlService with a stubbed table, pod and clock.

    ``now`` is fixed so token-expiry behaviour is deterministic rather than
    dependent on how long the suite takes to run.
    """
    client = MagicMock()
    if transport_error is not None:
        client.request = AsyncMock(side_effect=transport_error)
    else:
        response = httpx.Response(
            pod_status,
            json=pod_body if pod_body is not None else {"ok": True, "generation": 1},
            request=httpx.Request("GET", "http://10.42.3.17:8770/agent/ping"),
        )
        client.request = AsyncMock(return_value=response)

    authority = MagicMock()
    authority.authority.load_execution.return_value = None
    return ControlService(
        table=table if table is not None else make_table(items),
        http_client=client,
        env=env if env is not None else ENV,
        now=lambda: datetime(2026, 9, 12, 12, 0, tzinfo=UTC),
        authority_store=authority,
    )


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.scalar = AsyncMock(return_value=CANONICAL_USER_ID)
    return db


def build_client(service: ControlService, user, mock_db, *, orchestration: bool = False) -> TestClient:
    """Wire one adapter's router with the given service and identity."""
    app = FastAPI()
    app.include_router(orchestration_router if orchestration else activity_router)

    async def override_user():
        return user

    async def override_db():
        return mock_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_run_control_service if orchestration else get_control_service] = lambda: service
    return TestClient(app)


# The orchestration router carries a `/orchestration` prefix; the activity router
# carries none. Built from the routers' own registration rather than hardcoded
# twice, so a prefix change cannot leave these tests asserting against paths that
# no longer exist (they would 404 and several assertions would still pass).
ORCH_PREFIX = orchestration_router.prefix


def ping_path(orchestration: bool) -> str:
    if orchestration:
        return f"{ORCH_PREFIX}/runs/{RUN_ID}/ping"
    return f"/activity/invocations/{RUN_ID}/agent/ping"


def state_path(orchestration: bool) -> str:
    if orchestration:
        return f"{ORCH_PREFIX}/runs/{RUN_ID}/state"
    return f"/activity/invocations/{RUN_ID}/agent/state"


def command_path(action: str, orchestration: bool) -> str:
    if orchestration:
        return f"{ORCH_PREFIX}/runs/{RUN_ID}/{action}"
    return f"/activity/invocations/{RUN_ID}/agent/{action}"


BOTH_ADAPTERS = pytest.mark.parametrize("orchestration", [False, True], ids=["activity-adapter", "orchestration-adapter"])


def build_unauthenticated_client(service: ControlService, mock_db) -> TestClient:
    """Wire both routers with the REAL auth dependency left in place.

    Every other test overrides ``get_current_user``, which is what makes the
    authorization tests readable — but it also means none of them prove the routes
    are actually behind authentication. A route accidentally registered without
    the dependency would pass every test in this file. This client exists to catch
    that, so it deliberately does not override the identity.
    """
    app = FastAPI()
    app.include_router(activity_router)
    app.include_router(orchestration_router)

    async def override_db():
        return mock_db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_control_service] = lambda: service
    app.dependency_overrides[get_run_control_service] = lambda: service
    return TestClient(app)


# ===========================================================================
# 401 sits above everything (AC-S3, FR-7.9)
# ===========================================================================


class TestUnauthenticated:
    @BOTH_ADAPTERS
    def test_ping_and_state_require_authentication(self, orchestration, mock_db):
        client = build_unauthenticated_client(make_service(items=[row()]), mock_db)

        assert client.get(ping_path(orchestration)).status_code == 401
        assert client.get(state_path(orchestration)).status_code == 401

    @BOTH_ADAPTERS
    @pytest.mark.parametrize("action", ALL_ACTIONS)
    def test_every_verb_requires_authentication(self, action, orchestration, mock_db):
        """401 outranks 501: an anonymous caller learns nothing about the verb set."""
        client = build_unauthenticated_client(make_service(items=[row()]), mock_db)

        response = client.post(command_path(action, orchestration), json=valid_body(action))

        assert response.status_code == 401

    def test_an_unauthenticated_call_never_reaches_the_row(self, mock_db):
        """No DynamoDB read on an anonymous request — auth precedes resolution."""
        table = make_table([row()])
        service = make_service(table=table)
        client = build_unauthenticated_client(service, mock_db)

        client.get(ping_path(False))

        table.query.assert_not_called()
        service._http_client.request.assert_not_awaited()

    def test_a_non_bearer_authorization_header_is_401(self, mock_db):
        client = build_unauthenticated_client(make_service(items=[row()]), mock_db)

        response = client.get(ping_path(False), headers={"Authorization": "Basic dXNlcjpwYXNz"})

        assert response.status_code == 401


# ===========================================================================
# The story's central claim: no verb works (AC-S1, AC-S2)
# ===========================================================================


class TestSupportedVerbBoundary:
    def test_supported_actions_are_the_implemented_verbs(self):
        """The single switch that makes an unimplemented verb a 501.

        Abort joins the set in #3963, which delivers its cancellation path and the
        terminal finalization that reports the run as deliberately stopped. Steer
        stays out: its runtime proof is a later story, and a verb in this set with
        no transport behind it answers 200 for work that never happens.
        """
        assert SUPPORTED_ACTIONS == frozenset({"pause", "resume", "abort"})

    @BOTH_ADAPTERS
    @pytest.mark.parametrize("action", ["steer"])
    def test_authorized_verb_returns_501(self, action, orchestration, regular_user, mock_db):
        client = build_client(make_service(items=[row()]), regular_user, mock_db, orchestration=orchestration)

        response = client.post(command_path(action, orchestration), json=valid_body(action))

        assert response.status_code == 501

    @BOTH_ADAPTERS
    @pytest.mark.parametrize("action", ALL_ACTIONS)
    def test_501_reaches_no_pod(self, action, orchestration, regular_user, mock_db):
        """A 501 is a promise that nothing happened — including no transport call."""
        service = make_service(items=[row()])
        client = build_client(service, regular_user, mock_db, orchestration=orchestration)

        client.post(command_path(action, orchestration), json=valid_body(action))

        service._http_client.request.assert_not_awaited()

    @BOTH_ADAPTERS
    def test_all_four_verbs_are_routed_by_both_adapters(self, orchestration, regular_user, mock_db, monkeypatch):
        """Neither adapter may offer a different verb set than the other.

        The orchestration seam originally lacked ``resume``; two adapters with
        different verb sets is how one of them ends up with a weaker gate.
        """
        # Disable the verbs explicitly to isolate route existence from the live
        # authorization store's legitimate opaque 404 refusal.
        monkeypatch.setattr("src.activity.control_service.SUPPORTED_ACTIONS", frozenset())
        client = build_client(make_service(items=[row()]), regular_user, mock_db, orchestration=orchestration)

        for action in ALL_ACTIONS:
            response = client.post(command_path(action, orchestration), json=valid_body(action))
            assert response.status_code != 404, f"{action} is not routed"

    def test_only_implemented_capabilities_survive_the_pod_claim(self):
        """A compromised or newer worker must not produce a button the gateway 501s.

        The gateway intersects the pod's claim with its own SUPPORTED_ACTIONS, so
        the pod is not trusted as the source of truth for what the gateway routes.
        """
        service = make_service(
            items=[row()],
            pod_body={
                "state": "running",
                "capabilities": {"pause": True, "resume": True, "steer": True, "abort": True},
                "verification_key_ids": [ENV[SIGNING_KEY_ID_ENV]],
                "commands": [],
            },
        )

        state = _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert state.capabilities.pause is True
        assert state.capabilities.resume is True
        assert state.capabilities.steer is False
        assert state.capabilities.abort is True

    @pytest.mark.parametrize("key_ids", [None, [], ["retired-key"], "test-control-key", [1], ["test-control-key"] * 9])
    def test_unverifiable_worker_never_advertises_controls(self, key_ids):
        service = make_service(
            items=[row()],
            pod_body={
                "state": "running",
                "capabilities": {"pause": True, "resume": True},
                "verification_key_ids": key_ids,
                "commands": [],
            },
        )
        state = _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))
        assert not state.capabilities.pause and not state.capabilities.resume
        assert state.reason == "worker control verification is unavailable"

    def test_worker_key_rotation_changes_capabilities_without_restart(self):
        def state(keys):
            service = make_service(
                items=[row()],
                pod_body={"state": "running", "capabilities": {"pause": True, "resume": True}, "verification_key_ids": keys},
            )
            return _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert state(["test-control-key", "next"]).capabilities.pause
        assert not state(["next"]).capabilities.pause
        assert state(["test-control-key"]).capabilities.resume


# ===========================================================================
# Authorization: tenant AND owner, with one indistinguishable 404 (AC-S3)
# ===========================================================================


class TestAuthorization:
    @BOTH_ADAPTERS
    @pytest.mark.parametrize(
        "items,case",
        [
            ([], "unknown-run"),
            ([row(tenant_id="org-other-tenant")], "cross-tenant"),
            ([row(user_id="canonical-someone-else")], "same-tenant-non-owner"),
        ],
    )
    def test_three_failures_are_indistinguishable(self, items, case, orchestration, regular_user, mock_db):
        """404 for all three. A 403 would confirm the run exists."""
        client = build_client(make_service(items=items), regular_user, mock_db, orchestration=orchestration)

        response = client.get(ping_path(orchestration))

        assert response.status_code == 404, case

    @BOTH_ADAPTERS
    def test_the_404_bodies_are_identical(self, orchestration, regular_user, mock_db):
        """The oracle would return through a helpful detail string."""
        bodies = []
        for items in ([], [row(tenant_id="org-other")], [row(user_id="canonical-other")]):
            client = build_client(make_service(items=items), regular_user, mock_db, orchestration=orchestration)
            bodies.append(client.get(ping_path(orchestration)).text)

        assert len(set(bodies)) == 1, f"404 bodies differ and leak existence: {bodies}"

    @pytest.mark.parametrize("action", ALL_ACTIONS)
    def test_every_verb_rejects_a_cross_tenant_caller(self, action, regular_user, mock_db):
        """Including abort — the verb whose authorization must not wait for its story."""
        client = build_client(make_service(items=[row(tenant_id="org-other")]), regular_user, mock_db)

        response = client.post(command_path(action, False), json=valid_body(action))

        assert response.status_code == 404

    @pytest.mark.parametrize("action", ALL_ACTIONS)
    def test_every_verb_rejects_a_same_tenant_non_owner(self, action, regular_user, mock_db):
        """A colleague aborting your run is the same attack with a smaller radius."""
        client = build_client(make_service(items=[row(user_id="canonical-colleague")]), regular_user, mock_db)

        response = client.post(command_path(action, False), json=valid_body(action))

        assert response.status_code == 404

    def test_tenant_match_alone_is_not_enough(self):
        """Pins the `and` that replaced get_invocation's `elif`."""
        service = make_service(items=[row(user_id="canonical-colleague")])

        with pytest.raises(ControlError) as exc:
            service.resolve_target(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert exc.value.status_code == 404

    def test_owner_match_alone_is_not_enough(self):
        service = make_service(items=[row(tenant_id="org-other")])

        with pytest.raises(ControlError) as exc:
            service.resolve_target(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert exc.value.status_code == 404

    def test_chain_attributed_run_authorizes_its_root_human(self):
        """An agent-initiated run is controllable by the human it is attributed to."""
        service = make_service(items=[row(user_id="agent-service-account", root_human_id=CANONICAL_USER_ID)])

        target = service.resolve_target(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert target.run_id == RUN_ID

    @pytest.mark.parametrize("missing", ["user_id", "tenant_id", "run_id"])
    def test_missing_identity_is_never_a_lenient_path(self, missing):
        """A blank identity must not authorize on whichever half was supplied."""
        service = make_service(items=[row()])
        args = {"run_id": RUN_ID, "user_id": CANONICAL_USER_ID, "tenant_id": TENANT_ID}
        args[missing] = ""

        with pytest.raises(ControlError) as exc:
            service.resolve_target(args.pop("run_id"), **args)

        assert exc.value.status_code == 404

    def test_row_lookup_uses_the_base_table_key(self):
        """Queried on event_id, not scanned and not through a GSI."""
        table = make_table([row()])
        service = make_service(table=table)

        service.resolve_target(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        table.query.assert_called_once()
        table.scan.assert_not_called()

    def test_missing_table_reports_not_found_rather_than_500(self):
        """A deploy-order gap must not distinguish 'table missing' from 'run missing'."""
        table = MagicMock()
        table.query = MagicMock(side_effect=ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "Query"))
        service = make_service(table=table)

        with pytest.raises(ControlError) as exc:
            service.resolve_target(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert exc.value.status_code == 404

    def test_unexpected_dynamo_errors_are_not_swallowed_as_404(self):
        """Throttling is not 'not found'; masking it would hide a real outage."""
        table = MagicMock()
        table.query = MagicMock(side_effect=ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query"))
        service = make_service(table=table)

        with pytest.raises(ClientError):
            service.resolve_target(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)


# ===========================================================================
# Status mapping and its ordering (AC-S4, AC-F1, FR-7.9)
# ===========================================================================


class TestStatusMapping:
    @BOTH_ADAPTERS
    def test_flag_off_returns_503_after_authorization(self, orchestration, regular_user, mock_db):
        client = build_client(make_service(items=[row()], env=ENV_FLAG_OFF), regular_user, mock_db, orchestration=orchestration)

        assert client.get(ping_path(orchestration)).status_code == 503

    @BOTH_ADAPTERS
    def test_flag_off_still_hides_an_unowned_run_behind_404(self, orchestration, regular_user, mock_db):
        """404 outranks 503: the feature's state must not leak to a non-owner."""
        client = build_client(
            make_service(items=[row(tenant_id="org-other")], env=ENV_FLAG_OFF),
            regular_user,
            mock_db,
            orchestration=orchestration,
        )

        assert client.get(ping_path(orchestration)).status_code == 404

    @pytest.mark.parametrize("action", ALL_ACTIONS)
    def test_flag_off_outranks_unsupported_verb(self, action, regular_user, mock_db):
        """503, not 501: a deployment with the feature off answers uniformly."""
        client = build_client(make_service(items=[row()], env=ENV_FLAG_OFF), regular_user, mock_db)

        response = client.post(command_path(action, False), json=valid_body(action))

        assert response.status_code == 503

    @pytest.mark.parametrize("status", ["complete", "failed", "rejected", "no_op", "budget_stopped"])
    @pytest.mark.parametrize("action", ALL_ACTIONS)
    def test_terminal_run_returns_410_outranking_501(self, status, action, regular_user, mock_db):
        """'Already over' is more actionable than 'not built yet'."""
        client = build_client(make_service(items=[row(status=status)]), regular_user, mock_db)

        response = client.post(command_path(action, False), json=valid_body(action))

        assert response.status_code == 410

    def test_terminal_status_wins_even_with_a_live_token(self):
        """Teardown is fail-soft, so a lingering token must not re-enable control."""
        service = make_service(items=[row(status="complete", token=TOKEN, expires_in_minutes=60)])

        with pytest.raises(ControlError) as exc:
            service.authorize_command(RUN_ID, "abort", user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert exc.value.status_code == 410

    def test_unregistered_run_is_409_not_500(self):
        """No address is the expected state for a pre-feature run, not an error."""
        service = make_service(items=[row(address=None, token=None, port=None)])

        state = _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert state.available is False
        assert state.state == "unavailable"

    def test_expired_token_is_treated_as_unavailable(self):
        service = make_service(items=[row(expires_in_minutes=-5)])

        state = _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert state.available is False
        assert "expired" in (state.reason or "")

    def test_malformed_expiry_counts_as_expired(self):
        """An unreadable bound is not a bound."""
        service = make_service(items=[row(control_token_expires_at="not-a-timestamp")])
        target = service.resolve_target(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert service.token_is_live(target) is False

    def test_absent_expiry_counts_as_expired(self):
        service = make_service(items=[row(expires_in_minutes=None)])
        target = service.resolve_target(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert service.token_is_live(target) is False

    def test_terminal_state_is_answered_without_contacting_the_pod(self):
        """The pod is gone; dialling it would spend a timeout to learn nothing."""
        service = make_service(items=[row(status="complete")])

        state = _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert state.state == "terminal"
        service._http_client.request.assert_not_awaited()

    def test_transport_failure_reports_unavailable_not_dead(self):
        """Loss of contact is not evidence of exit (the liveness discipline)."""
        service = make_service(items=[row()], transport_error=httpx.ConnectError("refused"))

        result = _run(service.ping(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert result.available is False
        assert result.reason == "control listener unreachable"

    def test_pod_rejection_does_not_become_a_gateway_500(self):
        service = make_service(items=[row()], pod_status=401)

        result = _run(service.ping(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert result.available is False


# ===========================================================================
# Body validation (AC-S5, AC-S7)
# ===========================================================================


class TestBodyValidation:
    def test_oversized_body_is_413_before_parsing(self, regular_user, mock_db):
        client = build_client(make_service(items=[row()]), regular_user, mock_db)
        payload = json.dumps({"command_id": COMMAND_ID, "reason": "x" * (MAX_REQUEST_BYTES + 256)})

        response = client.post(command_path("pause", False), content=payload, headers={"content-type": "application/json"})

        assert response.status_code == 413

    def test_malformed_json_is_400_not_422(self, regular_user, mock_db):
        client = build_client(make_service(items=[row()]), regular_user, mock_db)

        response = client.post(command_path("pause", False), content='{"command_id": ', headers={"content-type": "application/json"})

        assert response.status_code == 400

    def test_json_array_body_is_400(self, regular_user, mock_db):
        client = build_client(make_service(items=[row()]), regular_user, mock_db)

        response = client.post(command_path("pause", False), content="[]", headers={"content-type": "application/json"})

        assert response.status_code == 400

    @pytest.mark.parametrize("field", ["actor", "target", "token", "control_address", "tenant_id"])
    def test_unknown_fields_are_rejected_loudly(self, field, regular_user, mock_db):
        """Silently dropping an override attempt returns success to the attempt.

        The caller would believe their actor/destination override took effect.
        """
        client = build_client(make_service(items=[row()]), regular_user, mock_db)

        response = client.post(command_path("pause", False), json={"command_id": COMMAND_ID, field: "injected"})

        assert response.status_code == 400

    def test_non_uuid_command_id_is_400(self, regular_user, mock_db):
        client = build_client(make_service(items=[row()]), regular_user, mock_db)

        response = client.post(command_path("pause", False), json={"command_id": "not-a-uuid"})

        assert response.status_code == 400

    def test_missing_command_id_is_400(self, regular_user, mock_db):
        client = build_client(make_service(items=[row()]), regular_user, mock_db)

        response = client.post(command_path("pause", False), json={})

        assert response.status_code == 400

    def test_empty_steer_instruction_is_400(self, regular_user, mock_db):
        client = build_client(make_service(items=[row()]), regular_user, mock_db)

        response = client.post(command_path("steer", False), json={"command_id": COMMAND_ID, "instruction": ""})

        assert response.status_code == 400

    def test_oversized_steer_instruction_is_400(self, regular_user, mock_db):
        client = build_client(make_service(items=[row()]), regular_user, mock_db)

        response = client.post(
            command_path("steer", False),
            json={"command_id": COMMAND_ID, "instruction": "x" * (MAX_INSTRUCTION_CHARS + 1)},
        )

        assert response.status_code == 400

    def test_malformed_body_is_400_even_for_an_unsupported_verb(self, regular_user, mock_db):
        """Validation precedes the verb gate, so 400 outranks 501 here (W1-05)."""
        client = build_client(make_service(items=[row()]), regular_user, mock_db)

        response = client.post(command_path("abort", False), json={"command_id": "nope"})

        assert response.status_code == 400

    def test_validation_failure_reaches_no_pod(self, regular_user, mock_db):
        service = make_service(items=[row()])
        client = build_client(service, regular_user, mock_db)

        client.post(command_path("abort", False), json={"command_id": "nope"})

        service._http_client.request.assert_not_awaited()


# ===========================================================================
# Transport hardening — the SSRF boundary (AC-S6, ADR-10, FR-7.3)
# ===========================================================================


class TestDestinationValidation:
    def test_accepts_a_pod_ip_inside_the_configured_cidr(self):
        assert validate_control_destination(POD_IP, CONTROL_PORT, env=ENV) is not None

    @pytest.mark.parametrize(
        "address,why",
        [
            ("127.0.0.1", "loopback would dial the gateway's own process"),
            ("::1", "ipv6 loopback"),
            ("0.0.0.0", "unspecified"),
            ("169.254.169.254", "instance metadata — is_private is False for it"),
            ("fe80::1", "ipv6 link-local"),
            ("224.0.0.1", "multicast"),
            ("8.8.8.8", "public address in a pod field means the row is wrong"),
            ("10.99.0.5", "private but outside the configured pod CIDR"),
        ],
    )
    def test_rejects_dangerous_destinations(self, address, why):
        with pytest.raises(ControlError) as exc:
            validate_control_destination(address, CONTROL_PORT, env=ENV)

        assert exc.value.status_code == 409, why

    @pytest.mark.parametrize(
        "address",
        [
            "pod.cluster.local",
            "http://10.42.3.17",
            "10.42.3.17:8770",
            "10.42.3.17/../admin",
            "",
            "not an address",
        ],
    )
    def test_rejects_anything_that_is_not_a_literal_ip(self, address):
        """Parse-first leaves no DNS-rebinding window between check and connect."""
        with pytest.raises(ControlError) as exc:
            validate_control_destination(address, CONTROL_PORT, env=ENV)

        assert exc.value.status_code == 409

    @pytest.mark.parametrize("port", [80, 443, 8080, 10250, 22, 0])
    def test_rejects_any_port_but_the_configured_one(self, port):
        """Pinned so a rewritten row cannot redirect the gateway at the kubelet."""
        with pytest.raises(ControlError):
            validate_control_destination(POD_IP, port, env=ENV)

    def test_unconfigured_cidr_list_refuses_everything(self):
        """Fail closed: an unset allowlist must not mean 'anything goes'.

        The reason it reports "not configured" rather than "outside the range" is
        operational, and the distinction is asserted deliberately: an operator who
        forgot the variable in a new environment sees a misconfiguration, not a
        message telling them the pod's own address is out of range — which would
        send them looking at the worker instead of at their own config.
        """
        with pytest.raises(ControlError) as exc:
            validate_control_destination(POD_IP, CONTROL_PORT, env={**ENV, "AGENT_CONTROL_CLUSTER_POD_CIDRS": ""})

        assert exc.value.status_code == 409
        assert "not configured" in exc.value.detail

    def test_entirely_malformed_cidr_list_refuses_everything(self):
        """An unparseable allowlist must not widen into an empty-means-any one."""
        with pytest.raises(ControlError):
            validate_control_destination(POD_IP, CONTROL_PORT, env={**ENV, "AGENT_CONTROL_CLUSTER_POD_CIDRS": "garbage,also-garbage"})


class TestTransportBehaviour:
    @pytest.mark.parametrize(
        ("reported", "expected"),
        [
            (["bbbbbbbbbbbbbbbb", "aaaaaaaaaaaaaaaa"], ["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"]),
            ([], []),
            (["secret-looking-unstructured-content"], None),
            ("aaaaaaaaaaaaaaaa", None),
            (["aaaaaaaaaaaaaaaa"] * 9, None),
        ],
    )
    def test_ping_exposes_only_bounded_public_key_ids(self, reported, expected):
        service = make_service(items=[row()], pod_body={"ok": True, "generation": 1, "verification_key_ids": reported})
        result = _run(service.ping(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))
        assert result.available
        assert result.verification_key_ids == expected

    def test_does_not_follow_redirects(self):
        """A redirect is a destination the validator never saw."""
        service = make_service(items=[row()])

        _run(service.ping(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert service._http_client.request.await_args.kwargs["follow_redirects"] is False

    def test_sets_a_finite_timeout(self):
        """A hung pod must not hold a gateway worker for a browser poll."""
        service = make_service(items=[row()])

        _run(service.ping(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        timeout = service._http_client.request.await_args.kwargs["timeout"]
        assert timeout.connect is not None
        assert timeout.read is not None

    def test_dials_the_registered_pod_over_plain_http_on_the_pinned_port(self):
        service = make_service(items=[row()])

        _run(service.ping(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        url = service._http_client.request.await_args.args[1]
        assert url == f"http://{POD_IP}:{CONTROL_PORT}/agent/ping"

    def test_presents_the_bearer_token_and_the_generation(self):
        service = make_service(items=[row(generation=4)])

        _run(service.ping(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        headers = service._http_client.request.await_args.kwargs["headers"]
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert headers["X-Adp-Control-Generation"] == "4"

    def test_a_tampered_address_is_refused_before_any_socket_opens(self):
        """CIDR membership is checked even though the worker wrote the row itself.

        Trusting the registration alone would trust whatever a compromised worker
        put in its own row.
        """
        service = make_service(items=[row(address="169.254.169.254")])

        with pytest.raises(ControlError) as exc:
            _run(service.ping(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert exc.value.status_code == 409
        service._http_client.request.assert_not_awaited()


# ===========================================================================
# Non-leakage of the private half (AC-S7, FR-1.8)
# ===========================================================================


class TestNoLeakage:
    @BOTH_ADAPTERS
    def test_state_response_contains_neither_token_nor_address(self, orchestration, regular_user, mock_db):
        """Asserted on the serialised body, since a leak arrives via serialisation."""
        client = build_client(make_service(items=[row()]), regular_user, mock_db, orchestration=orchestration)

        body = client.get(state_path(orchestration)).text

        assert TOKEN not in body
        assert POD_IP not in body

    @BOTH_ADAPTERS
    def test_ping_response_contains_neither_token_nor_address(self, orchestration, regular_user, mock_db):
        client = build_client(make_service(items=[row()]), regular_user, mock_db, orchestration=orchestration)

        body = client.get(ping_path(orchestration)).text

        assert TOKEN not in body
        assert POD_IP not in body

    def test_error_responses_do_not_leak_the_destination(self, regular_user, mock_db):
        client = build_client(make_service(items=[row(address="169.254.169.254")]), regular_user, mock_db)

        body = client.get(ping_path(False)).text

        assert "169.254.169.254" not in body

    def test_control_target_is_not_a_pydantic_model(self):
        """So FastAPI cannot serialise it out of a handler by accident."""
        import pydantic

        assert not issubclass(ControlTarget, pydantic.BaseModel)

    def test_a_pod_that_echoes_private_fields_cannot_leak_them(self):
        """The pod body is re-projected field by field, never passed through."""
        service = make_service(
            items=[row()],
            pod_body={
                "state": "running",
                "capabilities": {},
                "commands": [],
                "control_token": TOKEN,
                "control_address": POD_IP,
                "secret_field": "should-not-appear",
            },
        )

        state = _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))
        serialised = state.model_dump_json()

        assert TOKEN not in serialised
        assert "should-not-appear" not in serialised


# ===========================================================================
# The state projection
# ===========================================================================


class TestStateProjection:
    def test_projects_phase_and_commands(self):
        service = make_service(
            items=[row()],
            pod_body={
                "state": "running",
                "capabilities": {},
                "active_tool_count": 2,
                "updated_at": "2026-09-12T11:59:00Z",
                "commands": [{"command_id": COMMAND_ID, "action": "steer", "status": "pending", "accepted_at": "t"}],
            },
        )

        state = _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert state.state == "running"
        assert state.active_tool_count == 2
        assert len(state.commands) == 1
        assert state.commands[0].command_id == COMMAND_ID

    def test_unknown_phase_falls_back_to_unavailable(self):
        """An unrecognised phase must not pass through to the UI as truth."""
        service = make_service(items=[row()], pod_body={"state": "wormhole", "capabilities": {}, "commands": []})

        state = _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert state.state == "unavailable"

    def test_unknown_command_status_becomes_unknown_not_delivered(self):
        """`unknown` is honest; `delivered` would be a lie about reaching the model."""
        service = make_service(
            items=[row()],
            pod_body={
                "state": "running",
                "capabilities": {},
                "commands": [{"command_id": COMMAND_ID, "action": "pause", "status": "teleported"}],
            },
        )

        state = _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert state.commands[0].status == "unknown"

    def test_malformed_pod_body_does_not_crash_the_read(self):
        service = make_service(items=[row()], pod_body=["not", "a", "dict"])

        state = _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert state.state == "unavailable"

    def test_non_integer_active_tool_count_becomes_none(self):
        """Never a fabricated 0 — that would assert quiescence."""
        service = make_service(
            items=[row()],
            pod_body={"state": "running", "capabilities": {}, "commands": [], "active_tool_count": "lots"},
        )

        state = _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert state.active_tool_count is None

    def test_boolean_is_not_accepted_as_a_tool_count(self):
        """bool is an int subclass in Python; True must not become a count of 1."""
        service = make_service(
            items=[row()],
            pod_body={"state": "running", "capabilities": {}, "commands": [], "active_tool_count": True},
        )

        state = _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert state.active_tool_count is None

    def test_malformed_command_entries_are_dropped(self):
        service = make_service(
            items=[row()],
            pod_body={
                "state": "running",
                "capabilities": {},
                "commands": [
                    "not-a-dict",
                    {"action": "pause", "status": "pending"},
                    {"command_id": COMMAND_ID, "action": "teleport", "status": "pending"},
                    {"command_id": COMMAND_ID, "action": "pause", "status": "pending"},
                ],
            },
        )

        state = _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert len(state.commands) == 1

    def test_decimal_port_and_generation_are_coerced(self):
        """DynamoDB numbers arrive as Decimal through the resource API."""
        service = make_service(items=[row(generation=7)])

        target = service.resolve_target(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert target.port == CONTROL_PORT
        assert target.generation == 7
        assert isinstance(target.port, int)

    def test_cleared_registration_reads_as_unregistered(self):
        """Teardown writes removals, but an empty string must not look registered."""
        service = make_service(items=[row(address="", token="")])

        target = service.resolve_target(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert target.is_registered is False

    def test_non_numeric_port_reads_as_unregistered(self):
        """A garbled numeric field must not become a port the gateway dials."""
        service = make_service(items=[row(port=None, control_port="not-a-number")])

        target = service.resolve_target(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert target.port is None
        assert target.is_registered is False

    def test_most_recent_row_wins_for_a_reused_event_id(self):
        """Queried newest-first, so a retried delivery cannot resurrect an old pod.

        The base table is keyed (event_id, arrived_at). If an id ever carries two
        rows, control must act on the newest — an older row's address belongs to a
        pod that has already exited and whose IP is now someone else's.
        """
        newest = row(address="10.42.9.9", status="in_progress")
        table = make_table([newest, row(address="10.42.1.1", status="complete")])
        service = make_service(table=table)

        target = service.resolve_target(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert table.query.call_args.kwargs["ScanIndexForward"] is False
        assert target.address == "10.42.9.9"


class TestTransportFailureModes:
    def test_timeout_is_reported_as_unreachable_on_the_state_path(self):
        """The state path needs its own coverage: a poll must degrade, not 500."""
        service = make_service(items=[row()], transport_error=httpx.ReadTimeout("timed out"))

        state = _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert state.available is False
        assert state.reason == "control listener unreachable"
        assert state.state == "unavailable"

    def test_pod_rejection_on_the_state_path_is_reported_not_raised(self):
        service = make_service(items=[row()], pod_status=403)

        state = _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert state.available is False
        assert state.state == "unavailable"

    def test_a_bad_destination_on_the_state_path_still_raises_409(self):
        """ControlError is re-raised rather than folded into 'unreachable'.

        The two are materially different: unreachable is a transient pod problem,
        409 is a registration the gateway refuses to dial. Collapsing them would
        make an SSRF-rejected row look like a slow pod and hide the rejection.
        """
        service = make_service(items=[row(address="127.0.0.1")])

        with pytest.raises(ControlError) as exc:
            _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert exc.value.status_code == 409

    def test_a_non_json_pod_body_yields_an_unavailable_state(self):
        """A pod serving HTML (a misrouted proxy, say) must not crash the read."""
        client = MagicMock()
        client.request = AsyncMock(
            return_value=httpx.Response(
                200,
                content=b"<html>not json</html>",
                headers={"content-type": "text/html"},
                request=httpx.Request("GET", "http://10.42.3.17:8770/agent/state"),
            )
        )
        service = ControlService(
            table=make_table([row()]),
            http_client=client,
            env=ENV,
            now=lambda: datetime(2026, 9, 12, 12, 0, tzinfo=UTC),
        )

        state = _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert state.state == "unavailable"
        assert state.commands == []

    def test_ping_on_an_unregistered_run_contacts_no_pod(self):
        service = make_service(items=[row(address=None, token=None, port=None)])

        result = _run(service.ping(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert result.available is False
        assert result.reason == "run has no live control registration"
        service._http_client.request.assert_not_awaited()

    def test_ipv6_pod_address_is_bracketed_in_the_url(self):
        """An unbracketed IPv6 host makes the port part of the address.

        The result would not be a failed request — it would be a request to a
        different place, which is exactly what the destination check exists to
        prevent.
        """
        service = make_service(
            items=[row(address="fd00:42::17")],
            env={**ENV, "AGENT_CONTROL_CLUSTER_POD_CIDRS": "fd00:42::/64"},
        )

        _run(service.ping(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        url = service._http_client.request.await_args.args[1]
        assert url == f"http://[fd00:42::17]:{CONTROL_PORT}/agent/ping"


class TestConfiguration:
    def test_default_control_port_applies_when_unset(self):
        """The default is the value the NetworkPolicy and ScaledJob agree on."""
        from src.activity.control_service import DEFAULT_CONTROL_PORT, _configured_port

        assert _configured_port({}) == DEFAULT_CONTROL_PORT

    def test_malformed_port_falls_back_rather_than_raising(self):
        """A typo in one env var must not take down an otherwise-healthy read path."""
        from src.activity.control_service import DEFAULT_CONTROL_PORT, _configured_port

        assert _configured_port({"AGENT_CONTROL_PORT": "eighty-eight"}) == DEFAULT_CONTROL_PORT

    @pytest.mark.parametrize("value", ["True ", "1", "yes", "enabled", "", "false", "0", "on"])
    def test_the_flag_is_read_strictly(self, value):
        """Anything that is not the word "true" is off — the strict contract (AC-F1).

        ``"True "`` is in the list on purpose: the reader lowercases but does not
        strip, so a trailing space in a ConfigMap leaves the feature off. That is
        the safe direction, and it is asserted so nobody "fixes" it into leniency.
        """
        from src.activity.control_service import _is_flag_enabled

        assert _is_flag_enabled({"FEATURE_AGENT_CONTROL_ENABLED": value}) is False

    @pytest.mark.parametrize("value", ["TRUE", "True"])
    def test_case_variants_enable_on_the_gateway_side_only(self, value):
        """Documents a real asymmetry between the two independent readers.

        The gateway matches the repo's ``features/routes.py::_is_enabled_strict``,
        which lowercases, so ``TRUE`` enables here. The worker's readers
        (``entrypoint.py::_is_agent_control_enabled`` and the listener's flag
        check) compare byte-exactly, so ``TRUE`` leaves the *pod* off.

        The asymmetry is deliberately in this direction. Gateway-lenient means a
        mis-cased flag yields "gateway routes, pod has no listener" → an honest
        409 unavailable. Gateway-strict would mean "pod is listening, gateway
        refuses" — a bound port with nothing able to use it, which is the state
        with attack surface and no capability. If these are ever unified, unify
        toward the byte-exact reader and update this test; do not make the worker
        more lenient than the gateway.
        """
        from src.activity.control_service import _is_flag_enabled

        assert _is_flag_enabled({"FEATURE_AGENT_CONTROL_ENABLED": value}) is True

    def test_an_absent_flag_is_off(self):
        from src.activity.control_service import _is_flag_enabled

        assert _is_flag_enabled({}) is False

    def test_an_exact_true_enables(self):
        from src.activity.control_service import _is_flag_enabled

        assert _is_flag_enabled({"FEATURE_AGENT_CONTROL_ENABLED": "true"}) is True

    def test_malformed_cidr_entries_are_skipped_without_widening_the_list(self):
        """One bad entry must neither break the good ones nor admit everything."""
        env = {**ENV, "AGENT_CONTROL_CLUSTER_POD_CIDRS": "not-a-cidr, 10.42.0.0/16 ,"}

        assert validate_control_destination(POD_IP, CONTROL_PORT, env=env) is not None
        with pytest.raises(ControlError):
            validate_control_destination("10.99.0.5", CONTROL_PORT, env=env)

    def test_a_naive_expiry_timestamp_is_read_as_utc(self):
        """A worker that omits the Z must not have its token read as far-future.

        Python compares naive and aware datetimes by raising, so treating the
        value as UTC is what keeps an unusual-but-parseable timestamp from turning
        into a 500 on a hot poll path.
        """
        service = make_service(items=[row(control_token_expires_at="2026-09-12T12:30:00")])
        target = service.resolve_target(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert service.token_is_live(target) is True

    def test_a_naive_expiry_in_the_past_is_still_expired(self):
        service = make_service(items=[row(control_token_expires_at="2026-09-12T11:30:00")])
        target = service.resolve_target(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert service.token_is_live(target) is False

    def test_unavailable_reason_reports_terminal_before_registration(self):
        """Called directly: the read paths short-circuit terminal before reaching it.

        Order matters even so — a terminal run that still carries a registration
        must report "terminal", not "expired", or the UI would invite a retry.
        """
        service = make_service(items=[row(status="complete")])
        target = service.resolve_target(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert service.unavailable_reason(target) == "run has reached a terminal state"

    def test_a_healthy_registration_has_no_unavailable_reason(self):
        service = make_service(items=[row()])
        target = service.resolve_target(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert service.unavailable_reason(target) is None

    def test_the_fallback_client_disables_proxies_and_redirects(self, monkeypatch):
        """With no injected client the service builds one that trusts no env.

        An inherited ``HTTP_PROXY`` would route in-cluster control traffic — bearer
        token included — through whatever the environment names, which both leaks
        the credential and defeats the destination check that just passed.
        """
        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def request(self, *args, **kwargs):
                return httpx.Response(
                    200,
                    json={"ok": True},
                    request=httpx.Request("GET", "http://10.42.3.17:8770/agent/ping"),
                )

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        service = ControlService(
            table=make_table([row()]),
            http_client=None,
            env=ENV,
            now=lambda: datetime(2026, 9, 12, 12, 0, tzinfo=UTC),
        )

        result = _run(service.ping(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert result.available is True
        assert captured["trust_env"] is False
        assert captured["follow_redirects"] is False
        assert captured["timeout"].connect is not None

    def test_the_table_is_resolved_from_the_environment_when_not_injected(self, monkeypatch):
        """The production construction path: env var names the table, no hardcode."""
        resource = MagicMock()
        monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", "adp-test-webhook-events")

        ControlService(dynamodb_resource=resource, env=ENV)

        resource.Table.assert_called_once_with("adp-test-webhook-events")

    def test_an_explicit_table_name_overrides_the_environment(self, monkeypatch):
        resource = MagicMock()
        monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", "from-env")

        ControlService(dynamodb_resource=resource, table_name="explicit-table", env=ENV)

        resource.Table.assert_called_once_with("explicit-table")


class TestTheGateWhenAVerbIsEnabled:
    """The 409 branch, reachable only once a later story adds a verb.

    Patching ``SUPPORTED_ACTIONS`` is not testing a fiction: it is the exact
    single-line change S2 makes, so this proves the gate the *next* story inherits
    rather than leaving its unreachable branch unproven until then. It also pins
    the ordering claim that 409 sits *below* 501 — with the verb supported, an
    unregistered run stops answering 501 and starts answering 409.
    """

    @pytest.fixture
    def pause_enabled(self, monkeypatch):
        monkeypatch.setattr("src.activity.control_service.SUPPORTED_ACTIONS", frozenset({"pause"}))

    def test_an_unregistered_run_is_409_once_the_verb_exists(self, pause_enabled):
        service = make_service(items=[row(address=None, token=None, port=None)])

        with pytest.raises(ControlError) as exc:
            service.authorize_command(RUN_ID, "pause", user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert exc.value.status_code == 409
        assert exc.value.detail == "run has no live control registration"

    def test_an_expired_registration_is_409_once_the_verb_exists(self, pause_enabled):
        service = make_service(items=[row(expires_in_minutes=-1)])

        with pytest.raises(ControlError) as exc:
            service.authorize_command(RUN_ID, "pause", user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert exc.value.status_code == 409

    def test_a_supported_verb_on_a_live_run_passes_the_gate(self, pause_enabled):
        service = make_service(items=[row()])

        target = service.authorize_command(RUN_ID, "pause", user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert target.run_id == RUN_ID

    def test_a_still_unsupported_verb_remains_501(self, pause_enabled):
        """Enabling one verb must not enable its siblings."""
        service = make_service(items=[row()])

        with pytest.raises(ControlError) as exc:
            service.authorize_command(RUN_ID, "abort", user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert exc.value.status_code == 501

    def test_authorization_still_precedes_the_enabled_verb(self, pause_enabled):
        """The dangerous case: a working verb must not weaken the owner check."""
        service = make_service(items=[row(user_id="canonical-someone-else")])

        with pytest.raises(ControlError) as exc:
            service.authorize_command(RUN_ID, "pause", user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID)

        assert exc.value.status_code == 404

    def test_capabilities_report_the_enabled_verb_and_only_it(self, pause_enabled):
        service = make_service(
            items=[row()],
            pod_body={
                "state": "running",
                "capabilities": {"pause": True, "abort": True},
                "verification_key_ids": [ENV[SIGNING_KEY_ID_ENV]],
                "commands": [],
            },
        )

        state = _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert state.capabilities.pause is True
        assert state.capabilities.abort is False

    def test_a_pod_that_does_not_claim_the_verb_reports_it_false(self, pause_enabled):
        """The intersection cuts both ways: an older pod must not appear capable."""
        service = make_service(
            items=[row()],
            pod_body={"state": "running", "capabilities": {}, "commands": []},
        )

        state = _run(service.get_state(RUN_ID, user_id=CANONICAL_USER_ID, tenant_id=TENANT_ID))

        assert state.capabilities.pause is False


# ===========================================================================
# Pod outcome → caller status (revival-design §2, the 429 in the story's
# 400/401/404/409/410/413/429/501/503 list)
# ===========================================================================


class TestPodOutcomeStatusMapping:
    """The translation from a worker command outcome to a caller status.

    Reviewed as blocker B3: the worker's listener can answer 429 `queue_full`
    when its 10-slot pending queue is saturated, and the gateway had no word for
    it anywhere. That is not a cosmetic gap. 429 is the one signal the worker can
    send that means "wait, the run is fine"; arriving as an unmapped 5xx it means
    "something broke", and a client that should slow down retries instead —
    against a queue that is full precisely because of retries.

    Tested on the service rather than only through a route because the mapping
    has to be the *shared* one. Finding F1 in the same review was request
    validation that had landed on one adapter only; a status mapping written at
    one edge drifts the same way, and then the two adapters disagree about what a
    full queue means.
    """

    @pytest.mark.parametrize(
        "pod_status,expected",
        [
            (200, 200),
            (202, 202),
            (400, 400),
            (409, 409),
            (413, 413),
            (429, 429),
            (501, 501),
        ],
    )
    def test_known_outcomes_keep_their_meaning(self, pod_status, expected):
        service = make_service(items=[row()])
        response = httpx.Response(pod_status, request=httpx.Request("POST", "http://10.42.3.17:8770/agent/pause"))

        assert service.status_for_pod_outcome(response) == expected

    def test_queue_full_is_429_and_not_a_server_error(self):
        """The blocker, stated as the one assertion that would have caught it.

        `control-listener.ts` answers 429 `{error: 'queue_full'}` at its queue
        bound. Anything in the 5xx range here tells a saturated client the server
        is broken, which is both wrong and actively harmful: 5xx invites a retry,
        and the retry is what filled the queue.
        """
        service = make_service(items=[row()])
        response = httpx.Response(429, json={"error": "queue_full"}, request=httpx.Request("POST", "http://10.42.3.17:8770/agent/steer"))

        status = service.status_for_pod_outcome(response)

        assert status == 429
        assert status < 500, "a full queue is back-pressure, not a server fault"

    @pytest.mark.parametrize("pod_status", [200, 202, 400, 409, 413, 429, 501])
    def test_the_mapping_is_declared_for_every_status_the_listener_can_send(self, pod_status):
        """Pins the table against the listener's actual `writeJson` call sites.

        The worker emits 200, 202, 400, 401, 409, 413, 429, 501 and 404. The two
        omitted here are deliberate and asserted separately below: 401 and 404
        describe the *gateway's* relationship with the pod, not the caller's
        request, so forwarding them would mislead the caller.
        """
        assert pod_status in POD_OUTCOME_STATUSES

    @pytest.mark.parametrize("pod_status", [401, 403, 404, 500, 502, 301, 418])
    def test_unmapped_statuses_become_502_and_are_not_forwarded(self, pod_status):
        """The worker does not get to choose the status a browser receives.

        401 is the dangerous one and the reason this is a denylist-by-default
        rather than a pass-through: forwarding a pod's 401 to the dashboard would
        read as "your session expired" and log the operator out mid-run, when what
        actually happened is that the gateway presented a stale token to the pod.
        403 would read as "you lack permission" for a run the caller owns. Both
        are gateway↔pod problems being reported as caller problems.

        502 rather than 500 so an operator reading gateway logs is pointed
        upstream instead of hunting a gateway fault.
        """
        service = make_service(items=[row()])
        response = httpx.Response(pod_status, request=httpx.Request("POST", "http://10.42.3.17:8770/agent/pause"))

        assert service.status_for_pod_outcome(response) == UNMAPPED_POD_STATUS == 502

    def test_no_mapped_status_is_a_redirect(self):
        """A 3xx from a pod is never followed and never forwarded.

        `follow_redirects=False` stops the gateway acting on one; this stops it
        handing the browser a redirect to a destination the SSRF validator never
        saw.
        """
        assert not any(300 <= status < 400 for status in POD_OUTCOME_STATUSES.values())

    def test_both_adapters_resolve_the_same_mapping(self):
        """The F1 lesson applied to B3: one table, not one per edge.

        Both adapters depend on `ControlService`, so a mapping that lives on the
        service is necessarily shared. Asserted rather than left to inspection
        because the failure is silent — two edges with two tables agree on the day
        they are written.
        """
        from src.activity.routes import get_control_service
        from src.orchestration.controls import get_run_control_service

        activity_service = get_control_service()
        orchestration_service = get_run_control_service()
        response = httpx.Response(429, request=httpx.Request("POST", "http://10.42.3.17:8770/agent/pause"))

        assert activity_service.status_for_pod_outcome(response) == orchestration_service.status_for_pod_outcome(response) == 429


def _run(coro):
    """Drive a coroutine to completion without requiring an asyncio plugin."""
    import asyncio

    return asyncio.run(coro)
