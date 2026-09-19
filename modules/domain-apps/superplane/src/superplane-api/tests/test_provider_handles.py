"""Durable provider-handle persistence and reconciliation — issue #5054 (U11c).

The cases named in the story's validation section: a crash before and after the
provider response, a durable idempotency identity, duplicate reconciliation,
foreign-workspace rejection, and unresolved-allocation retention.

## How a crash is simulated, and why that is a fair test

A crash is not mocked with a patched exception. It is simulated the way it
actually manifests: the process that recorded a handle simply never reports an
outcome, and a *different* caller (a restarted one, with a fresh session) then
looks for what was left behind. That is exactly the recovery path, so what the
test exercises is what a real restart would do.

The two crash points are distinguished because they have opposite consequences:

* **Before the provider response** — the row exists, no reference, nothing
  concluded. The recovery read must find it. This is the case upstream loses
  entirely, because it records nothing until the call returns.
* **After the provider response, before recording it** — the operation is still
  open, and reconciliation against the provider is what establishes that the
  resource exists. A blind retry here is the duplicate-spend bug.

## Fixture provenance

Provider observations are constructed through the contract's own
`ProviderObservation`, whose validation encodes the provider-response shape
(a PRESENT answer must carry the provider's own state string; an UNKNOWN answer
must carry no state and must say why). Building them through that type rather
than writing dicts to match what the service expects is what stops a fixture and
the code under test from being self-consistently wrong about a timeout.

No live provider is called. R15 acceptances 1 and 5-6 need a real provider, a
named account, a spend limit and a cleanup owner, and stay deferred.
"""

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from superplane_contracts import (
    CallOutcome,
    HandleRecord,
    OperationKind,
    ProviderHandle,
    ProviderObservation,
    ProviderPresence,
    ReconcileResult,
    Submitter,
    authorize_provider_call,
)

from app.config import settings
from app.main import app
from app.models.organization import Organization
from app.models.provider_handle import ProviderOperation
from app.models.workspace import Workspace
from app.services import provider_authority, provider_inventory
from app.services import provider_handles as handle_service
from tests.conftest import async_session_test

_OWNED_CRED = "Bearer adapter-credential-not-a-real-secret"
_FOREIGN_CRED = "Bearer other-adapter-credential-not-a-real-secret"
_WS_OWNED = "a1111111-1111-4111-8111-111111111111"
_WS_FOREIGN = "b2222222-2222-4222-8222-222222222222"

_BASE = "/internal/provider-operations"


@pytest.fixture(autouse=True)
async def workspace_identities(_setup_db):
    async with async_session_test() as session:
        for workspace in (_WS_OWNED, _WS_FOREIGN):
            org_id = uuid.uuid4()
            session.add(Organization(id=org_id, name=f"org-{workspace}"))
            await session.flush()
            session.add(
                Workspace(
                    id=uuid.UUID(workspace),
                    org_id=org_id,
                    name="dev",
                    isolation_mode="dedicated",
                )
            )
        await session.commit()


@pytest.fixture(autouse=True)
def _configure_submitters(monkeypatch):
    """Two adapters with disjoint workspace grants.

    Disjoint on purpose: it makes "authenticated, but not authorized for this
    workspace" a reachable state rather than a hypothetical one, which is what the
    cross-workspace rejection test needs.
    """
    monkeypatch.setattr(
        settings,
        "observation_submitters",
        json.dumps(
            [
                {
                    "submitter_id": "adapter-1",
                    "credential": _OWNED_CRED,
                    "signing_key": "adapter-1-signing-key-not-a-real-secret",
                    "workspaces": [_WS_OWNED],
                },
                {
                    "submitter_id": "adapter-2",
                    "credential": _FOREIGN_CRED,
                    "signing_key": "adapter-2-signing-key-not-a-real-secret",
                    "workspaces": [_WS_FOREIGN],
                },
            ]
        ),
    )


@pytest.fixture(autouse=True)
def authority_validator(monkeypatch):
    """Test adapter only; production deliberately has no B validator.

    Persistence cases use one accepted test credential. Security cases replace
    this resolver's result with independently constructed contexts.
    """

    class TestValidator:
        async def resolve(self, authority, *, submitter, handle):
            if authority != "authority-from-b":
                return None
            return provider_authority.VerifiedProviderAuthority(
                operation_id=f"operation:{handle.workspace}:{handle.idempotency_key}",
                run_id="run-1",
                attempt_id="attempt-1",
                submitter_id=submitter.submitter_id,
                handle=handle,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
                active=True,
            )

    validator = TestValidator()
    monkeypatch.setattr(provider_authority, "_validator", validator)
    return validator


@pytest.fixture(autouse=True)
def inventory_reader(monkeypatch):
    class TestInventoryReader:
        resources = tuple(
            provider_inventory.AllocationResourceIdentity(
                resource_id=name,
                provider="aws",
                provider_reference=name,
                kind="compute",
                operation_keys=frozenset({f"idem-release-{index}"}),
            )
            for index, name in enumerate(("sky-node-a", "sky-node-b"))
        )
        complete = True

        async def read(
            self,
            *,
            submitter,
            workspace,
            allocation_id,
            operation_authority,
            report_digest,
        ):
            if operation_authority != "authority-from-b":
                return None
            async with async_session_test() as session:
                owner = await session.get(Workspace, uuid.UUID(workspace))
            return provider_inventory.VerifiedAllocationInventory(
                workspace=workspace,
                org_id=str(owner.org_id),
                allocation_id=allocation_id,
                executor_id=submitter.submitter_id,
                active=True,
                attested_report_digest=report_digest,
                revision="fixture-inventory-v1",
                resources=self.resources,
                complete=self.complete,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )

    reader = TestInventoryReader()
    monkeypatch.setattr(provider_inventory, "_reader", reader)
    return reader


@pytest.fixture
async def api():
    """HTTP client for the endpoint tests."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _owned(**overrides) -> dict:
    """A record-handle body for the owned workspace."""
    body = {
        "operation_authority": "authority-from-b",
        "operation": OperationKind.PROVISION.value,
        "provider": "aws",
        "resource_name": "sky-cluster-alpha",
        "idempotency_key": "idem-alpha-0001",
        "allocation_id": "alloc-alpha",
        "workspace": _WS_OWNED,
    }
    body.update(overrides)
    return body


def _submitter(
    workspace: str = _WS_OWNED, submitter_id: str = "adapter-1"
) -> Submitter:
    return Submitter(submitter_id=submitter_id, workspaces=frozenset({workspace}))


def _handle(**overrides) -> ProviderHandle:
    fields = {
        "operation": OperationKind.PROVISION,
        "provider": "aws",
        "resource_name": "sky-cluster-alpha",
        "idempotency_key": "idem-alpha-0001",
        "allocation_id": "alloc-alpha",
        "workspace": _WS_OWNED,
    }
    fields.update(overrides)
    return ProviderHandle(**fields)


class TestDurableIdentityBeforeTheCall:
    """R15 acceptance 5: the handle is findable before the call can be lost."""

    async def test_recording_returns_a_confirmation_instant_that_authorizes_the_call(
        self, api
    ):
        """The confirmation comes from the committed write, not from a flag.

        This is the end-to-end form of the ordering rule: the caller records, gets
        an instant back, and only then does `authorize_provider_call` permit the
        provider call. The instant cannot be manufactured — `HandleRecord` refuses
        `durable=True` without one.
        """
        response = await api.post(
            _BASE, json=_owned(), headers={"Authorization": _OWNED_CRED}
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["durable"] is True
        assert payload["state"] == handle_service.OperationState.RECORDED.value

        from datetime import datetime

        confirmed_at = datetime.fromisoformat(payload["confirmed_at"])
        # Timezone-aware, because the contract refuses a naive confirmation.
        assert confirmed_at.tzinfo is not None

        record = HandleRecord(handle=_handle(), durable=True, confirmed_at=confirmed_at)
        assert authorize_provider_call(record).permitted is True

    async def test_a_call_without_a_durable_record_is_refused(self):
        """The negative half: no persistence acknowledgement, no permission.

        Asserted because the gate is only meaningful if the un-recorded case
        actually fails. A contract that permits both ways enforces nothing.
        """
        decision = authorize_provider_call(
            HandleRecord(handle=_handle(), durable=False)
        )
        assert decision.permitted is False
        assert "not durably recorded" in decision.reason

    async def test_the_row_exists_before_any_provider_reference_does(self, api):
        """The stored pre-call form carries no provider reference.

        `provider_reference` is absent at record time by design — it does not exist
        until the provider answers. A schema that required it would push the whole
        record after the call, which is the window the bug lives in.
        """
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})

        async with async_session_test() as session:
            row = await session.get(
                ProviderOperation,
                {"idempotency_key": "idem-alpha-0001", "workspace": _WS_OWNED},
            )
            assert row is not None
            assert row.provider_reference is None
            assert row.reconcile_result is None
            assert row.state == handle_service.OperationState.RECORDED.value

    async def test_a_duplicate_idempotency_key_is_refused_not_overwritten(self, api):
        """A second record for one identity conflicts.

        Overwriting would erase the pre-call form a post-crash reconciliation
        needs; silently accepting would hand out a second authorization for one
        operation.
        """
        first = await api.post(
            _BASE, json=_owned(), headers={"Authorization": _OWNED_CRED}
        )
        assert first.status_code == 201

        second = await api.post(
            _BASE,
            json=_owned(resource_name="sky-cluster-different"),
            headers={"Authorization": _OWNED_CRED},
        )
        assert second.status_code == 409

        # The original survived the conflicting attempt.
        async with async_session_test() as session:
            row = await session.get(
                ProviderOperation,
                {"idempotency_key": "idem-alpha-0001", "workspace": _WS_OWNED},
            )
            assert row.resource_name == "sky-cluster-alpha"


class TestCrashBeforeProviderResponse:
    """The recorded-but-never-concluded operation a restart must find."""

    async def test_a_restarted_caller_finds_the_operation_it_lost(self, api):
        """The recovery read returns an operation nothing concluded.

        The 'crash' is that no conclude call ever happens; a fresh request then
        looks for what was left behind. This is the case upstream loses entirely,
        because it writes nothing until the launch returns.
        """
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})

        recovered = await api.get(
            _BASE,
            params={"workspace": _WS_OWNED},
            headers={"Authorization": _OWNED_CRED},
        )
        assert recovered.status_code == 200
        rows = recovered.json()
        assert len(rows) == 1
        assert rows[0]["idempotency_key"] == "idem-alpha-0001"
        assert rows[0]["state"] == handle_service.OperationState.RECORDED.value
        # The identity a later provider re-check queries by is present.
        assert rows[0]["resource_name"] == "sky-cluster-alpha"

    @pytest.mark.parametrize("reference", [None, "provider-returned-instance-id"])
    async def test_a_lost_successful_conclusion_response_remains_recoverable(
        self, api, reference
    ):
        """Discard the conclusion response; a fresh recovery client must find the handle."""
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})
        await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json={
                "outcome": CallOutcome.SUCCEEDED.value,
                "provider_reference": reference,
                "operation_authority": "authority-from-b",
                "workspace": _WS_OWNED,
            },
            headers={"Authorization": _OWNED_CRED},
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as restarted:
            recovered = await restarted.get(
                _BASE,
                params={"workspace": _WS_OWNED},
                headers={"Authorization": _OWNED_CRED},
            )
        assert recovered.status_code == 200
        rows = recovered.json()
        assert len(rows) == 1
        assert rows[0]["provider_reference"] == reference
        assert rows[0]["resource_name"] == "sky-cluster-alpha"
        assert rows[0]["reconcile_result"] == ReconcileResult.RECONCILED_EXISTS.value

    async def test_confirmed_failure_without_resource_drops_out_of_recovery(self, api):
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})
        await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json={
                "outcome": CallOutcome.FAILED.value,
                "operation_authority": "authority-from-b",
                "workspace": _WS_OWNED,
            },
            headers={"Authorization": _OWNED_CRED},
        )
        recovered = await api.get(
            _BASE,
            params={"workspace": _WS_OWNED},
            headers={"Authorization": _OWNED_CRED},
        )
        assert recovered.json() == []


class TestCrashAfterProviderResponse:
    """R15 acceptance 6: an ambiguous outcome is established, never retried blind."""

    async def test_a_lost_response_for_a_live_resource_does_not_permit_a_replacement(
        self, api
    ):
        """The duplicate-spend bug, asserted as absent.

        The call timed out but the provider holds the resource. Reconciliation
        against the recorded handle establishes that, so the conclusion is
        'exists' and no repeat is permitted. Upstream reaches the opposite
        conclusion here and launches on the next cloud.
        """
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})

        response = await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json={
                "outcome": CallOutcome.AMBIGUOUS.value,
                "operation_authority": "authority-from-b",
                "workspace": _WS_OWNED,
                "observation": {
                    "presence": ProviderPresence.PRESENT.value,
                    "queried_by": "sky-cluster-alpha",
                    "provider_state": "RUNNING",
                },
                "provider_reference": "sky-request-9f2",
            },
            headers={"Authorization": _OWNED_CRED},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["result"] == ReconcileResult.RECONCILED_EXISTS.value
        assert payload["may_repeat_operation"] is False
        assert payload["resources_unresolved"] is True

        # The provider's identifier is now attached to the operation, so the
        # resource is no longer one nothing holds a reference to.
        async with async_session_test() as session:
            row = await session.get(
                ProviderOperation,
                {"idempotency_key": "idem-alpha-0001", "workspace": _WS_OWNED},
            )
            assert row.provider_reference == "sky-request-9f2"
            assert row.provider_state == "RUNNING"

    async def test_provider_established_absence_is_the_only_route_to_a_repeat(
        self, api
    ):
        """A repeat is permitted only after the provider confirmed absence."""
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})

        response = await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json={
                "outcome": CallOutcome.AMBIGUOUS.value,
                "operation_authority": "authority-from-b",
                "workspace": _WS_OWNED,
                "observation": {
                    "presence": ProviderPresence.ABSENT.value,
                    "queried_by": "sky-cluster-alpha",
                },
            },
            headers={"Authorization": _OWNED_CRED},
        )
        payload = response.json()
        assert payload["result"] == ReconcileResult.RETRY_PERMITTED.value
        assert payload["may_repeat_operation"] is True

    async def test_an_ambiguous_outcome_with_no_observation_permits_nothing(self, api):
        """A timeout means unknown — never permission to launch a replacement.

        The case that matters most: the provider could not be consulted at all. The
        operation stays unresolved and retained, and the response explicitly denies
        a repeat rather than defaulting to one.
        """
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})

        response = await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json={
                "outcome": CallOutcome.AMBIGUOUS.value,
                "operation_authority": "authority-from-b",
                "workspace": _WS_OWNED,
            },
            headers={"Authorization": _OWNED_CRED},
        )
        payload = response.json()
        assert payload["result"] == ReconcileResult.UNRESOLVED.value
        assert payload["may_repeat_operation"] is False
        assert payload["state"] == handle_service.OperationState.UNRESOLVED.value

    async def test_an_unresolved_operation_stays_in_the_recovery_read(self, api):
        """Unresolved is neither open-and-forgotten nor closed.

        If an unresolved operation dropped out of the recovery read, a single
        failed re-check would make it invisible — which is how 'we could not
        check' silently becomes 'there is nothing there'.
        """
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})
        await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json={
                "outcome": CallOutcome.AMBIGUOUS.value,
                "operation_authority": "authority-from-b",
                "workspace": _WS_OWNED,
                "observation": {
                    "presence": ProviderPresence.UNKNOWN.value,
                    "queried_by": "sky-cluster-alpha",
                    "detail": "provider API returned 503",
                },
            },
            headers={"Authorization": _OWNED_CRED},
        )

        recovered = await api.get(
            _BASE,
            params={"workspace": _WS_OWNED},
            headers={"Authorization": _OWNED_CRED},
        )
        rows = recovered.json()
        assert len(rows) == 1
        assert rows[0]["state"] == handle_service.OperationState.UNRESOLVED.value

    async def test_an_observation_about_another_resource_resolves_nothing(self, api):
        """A confident answer about the wrong resource establishes nothing.

        The contract checks the observation identifies the recorded operation, so a
        query by the wrong identity yields UNRESOLVED rather than a conclusion.
        """
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})

        response = await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json={
                "outcome": CallOutcome.AMBIGUOUS.value,
                "operation_authority": "authority-from-b",
                "workspace": _WS_OWNED,
                "observation": {
                    "presence": ProviderPresence.ABSENT.value,
                    "queried_by": "some-other-cluster",
                },
            },
            headers={"Authorization": _OWNED_CRED},
        )
        payload = response.json()
        assert payload["result"] == ReconcileResult.UNRESOLVED.value
        assert payload["may_repeat_operation"] is False

    async def test_acting_without_b_s_authority_is_refused(self, api):
        """A acts only under B's execution authority for an active operation.

        Both refusal layers are asserted, because a whitespace-only authority
        passes the schema's length check and would otherwise be caught by nothing
        here: the *missing* field is a 422 from the schema, while a *blank* one
        reaches the contract, which refuses it and is surfaced as a 400. Checking
        only the missing case would leave `"   "` reading as an authority.
        """
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})

        blank = await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json={
                "outcome": CallOutcome.AMBIGUOUS.value,
                "operation_authority": "   ",
                "workspace": _WS_OWNED,
            },
            headers={"Authorization": _OWNED_CRED},
        )
        assert blank.status_code == 400
        assert "operation_authority" in blank.json()["detail"]

        missing = await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json={"outcome": CallOutcome.AMBIGUOUS.value},
            headers={"Authorization": _OWNED_CRED},
        )
        assert missing.status_code == 422

        # Neither attempt concluded the operation — it is still recoverable.
        async with async_session_test() as session:
            row = await session.get(
                ProviderOperation,
                {"idempotency_key": "idem-alpha-0001", "workspace": _WS_OWNED},
            )
            assert row.state == handle_service.OperationState.RECORDED.value


class TestDuplicateReconciliation:
    """Reporting the same conclusion twice is recognised, not reapplied."""

    async def test_a_repeated_conclusion_reports_that_nothing_was_newly_applied(
        self, api
    ):
        """A recovery worker retries by nature; a retry must be harmless."""
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})
        body = {
            "outcome": CallOutcome.AMBIGUOUS.value,
            "operation_authority": "authority-from-b",
            "workspace": _WS_OWNED,
            "observation": {
                "presence": ProviderPresence.PRESENT.value,
                "queried_by": "sky-cluster-alpha",
                "provider_state": "RUNNING",
            },
        }

        first = await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json=body,
            headers={"Authorization": _OWNED_CRED},
        )
        second = await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json=body,
            headers={"Authorization": _OWNED_CRED},
        )

        assert first.json()["applied"] is True
        assert second.json()["applied"] is False
        # Same established conclusion both times.
        assert second.json()["result"] == first.json()["result"]

    async def test_a_later_worse_observation_cannot_overwrite_a_conclusion(self, api):
        """An established conclusion is not re-decided.

        Once the provider has answered, a subsequent report from a moment when it
        could not be reached must not downgrade the operation to unresolved —
        that would lose evidence the system already has.
        """
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})
        await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json={
                "outcome": CallOutcome.AMBIGUOUS.value,
                "operation_authority": "authority-from-b",
                "workspace": _WS_OWNED,
                "observation": {
                    "presence": ProviderPresence.PRESENT.value,
                    "queried_by": "sky-cluster-alpha",
                    "provider_state": "RUNNING",
                },
            },
            headers={"Authorization": _OWNED_CRED},
        )

        later = await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json={
                "outcome": CallOutcome.AMBIGUOUS.value,
                "operation_authority": "authority-from-b",
                "workspace": _WS_OWNED,
                "observation": {
                    "presence": ProviderPresence.UNKNOWN.value,
                    "queried_by": "sky-cluster-alpha",
                    "detail": "provider unreachable on the retry",
                },
            },
            headers={"Authorization": _OWNED_CRED},
        )
        assert later.json()["applied"] is False
        assert later.json()["result"] == ReconcileResult.RECONCILED_EXISTS.value

    async def test_a_second_different_provider_reference_is_refused(self, api):
        """Two references for one operation means the adapter cannot tell which
        resource it owns — so it is a conflict, not a silent overwrite."""
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})
        await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json={
                "outcome": CallOutcome.AMBIGUOUS.value,
                "operation_authority": "authority-from-b",
                "workspace": _WS_OWNED,
                "observation": {
                    "presence": ProviderPresence.UNKNOWN.value,
                    "queried_by": "sky-cluster-alpha",
                    "detail": "provider unreachable",
                },
                "provider_reference": "sky-request-first",
            },
            headers={"Authorization": _OWNED_CRED},
        )

        # Still unresolved, so a further report is processed rather than
        # short-circuited — which is what makes the reference conflict reachable.
        conflicting = await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json={
                "outcome": CallOutcome.AMBIGUOUS.value,
                "operation_authority": "authority-from-b",
                "workspace": _WS_OWNED,
                "provider_reference": "sky-request-second",
            },
            headers={"Authorization": _OWNED_CRED},
        )
        assert conflicting.status_code == 409


class TestProviderConfirmedExistenceSupersedes:
    """A concluded operation must still accept evidence that the resource exists.

    The idempotency short-circuit exists to stop a *weaker* later report undoing an
    established conclusion. Applied to every report it also dropped the *stronger*
    one, and that is the direction that costs money: a provider confirming the
    resource is there after we concluded it was not.

    These cases were absent from the first implementation of this story and are the
    two transitions a review reproduced as defects. Each asserts the consequence
    (repeat authorization withdrawn, identifier retained) rather than only the
    result string, because the result string is not what authorizes a duplicate
    launch.
    """

    async def _record(self, api):
        response = await api.post(
            _BASE, json=_owned(), headers={"Authorization": _OWNED_CRED}
        )
        assert response.status_code == 201, response.text

    async def _conclude(self, api, **body):
        """Report an outcome, defaulting the fields every report needs."""
        payload = {
            "operation_authority": "authority-from-b",
            "workspace": _WS_OWNED,
        }
        payload.update(body)
        return await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json=payload,
            headers={"Authorization": _OWNED_CRED},
        )

    @staticmethod
    def _present(**overrides) -> dict:
        """A provider answer that the resource exists, built through the contract.

        Routed through `ProviderObservation` so the fixture inherits the contract's
        own rule that a PRESENT answer must carry the provider's reported state.
        A hand-written dict could assert existence with no evidence, which is the
        shape of answer this module refuses to accept.
        """
        observation = ProviderObservation(
            presence=ProviderPresence.PRESENT,
            queried_by="sky-cluster-alpha",
            provider_state="RUNNING",
        )
        payload = {
            "presence": observation.presence.value,
            "queried_by": observation.queried_by,
            "provider_state": observation.provider_state,
        }
        payload.update(overrides)
        return payload

    async def test_provider_established_absence_is_superseded_by_presence(self, api):
        """ABSENT then PRESENT: the later, stronger answer wins.

        The realistic trigger is provider eventual consistency — a
        describe-after-create that has not listed the resource yet. The first
        answer legitimately concludes the operation and authorizes a repeat; the
        second establishes that repeating would now duplicate a live resource.
        """
        await self._record(api)
        first = await self._conclude(
            api,
            outcome=CallOutcome.AMBIGUOUS.value,
            observation={
                "presence": ProviderPresence.ABSENT.value,
                "queried_by": "sky-cluster-alpha",
            },
        )
        assert first.status_code == 200, first.text
        # Absence established, so a repeat is permitted at this point.
        assert first.json()["result"] == ReconcileResult.RETRY_PERMITTED.value
        assert first.json()["may_repeat_operation"] is True

        later = await self._conclude(
            api,
            outcome=CallOutcome.AMBIGUOUS.value,
            observation=self._present(),
            provider_reference="i-0abc123realinstance",
        )
        assert later.status_code == 200, later.text
        payload = later.json()
        assert payload["applied"] is True, "the stronger observation must be applied"
        assert payload["result"] == ReconcileResult.RECONCILED_EXISTS.value
        # The assertion that matters: the API no longer authorizes the duplicate
        # launch this epic exists to prevent.
        assert payload["may_repeat_operation"] is False
        assert payload["resources_unresolved"] is True

    async def test_a_failure_conclusion_is_superseded_by_provider_presence(self, api):
        """FAILED then PRESENT — the sequence a timeout actually produces.

        Upstream's timeout branch returns a failure, and the contract answers a
        failure with `RETRY_PERMITTED` because a refusal is the provider's own
        answer that nothing ran. So a lost response concludes the operation in a
        single step, with no ambiguous state in between. If the provider was in
        fact holding the resource, this is the only report that can correct it.
        """
        await self._record(api)
        failed = await self._conclude(api, outcome=CallOutcome.FAILED.value)
        assert failed.status_code == 200, failed.text
        assert failed.json()["state"] == handle_service.OperationState.CONCLUDED.value
        assert failed.json()["may_repeat_operation"] is True

        corrected = await self._conclude(
            api,
            outcome=CallOutcome.AMBIGUOUS.value,
            observation=self._present(),
            provider_reference="i-0abc123realinstance",
        )
        assert corrected.status_code == 200, corrected.text
        assert corrected.json()["applied"] is True
        assert corrected.json()["may_repeat_operation"] is False

    async def test_the_provider_reference_survives_the_correction(self, api):
        """The identifier must be persisted, not just reported back.

        A running resource with no stored identifier is the untracked-billing case
        this story exists to close, so the assertion reads the row rather than the
        response: a reference echoed in a reply but never written would pass a
        response-only check and still lose the resource.
        """
        await self._record(api)
        await self._conclude(api, outcome=CallOutcome.FAILED.value)
        await self._conclude(
            api,
            outcome=CallOutcome.AMBIGUOUS.value,
            observation=self._present(),
            provider_reference="i-0abc123realinstance",
        )

        async with async_session_test() as session:
            row = await session.get(
                ProviderOperation,
                {"idempotency_key": "idem-alpha-0001", "workspace": _WS_OWNED},
            )
            assert row.provider_reference == "i-0abc123realinstance"
            assert row.reconcile_result == ReconcileResult.RECONCILED_EXISTS.value
            assert row.provider_presence == ProviderPresence.PRESENT.value

    async def test_an_unknown_observation_still_cannot_undo_a_conclusion(self, api):
        """The guard the supersession rule must not have weakened.

        `UNKNOWN` means the provider could not be consulted, so it establishes
        nothing and must never overwrite a conclusion drawn from a real answer.
        Asserted alongside the superseding cases because a fix that let *any* later
        report through would pass those and break this.
        """
        await self._record(api)
        await self._conclude(
            api, outcome=CallOutcome.AMBIGUOUS.value, observation=self._present()
        )

        later = await self._conclude(
            api,
            outcome=CallOutcome.AMBIGUOUS.value,
            observation={
                "presence": ProviderPresence.UNKNOWN.value,
                "queried_by": "sky-cluster-alpha",
                "detail": "provider unreachable on the retry",
            },
        )
        assert later.json()["applied"] is False
        assert later.json()["result"] == ReconcileResult.RECONCILED_EXISTS.value

    async def test_absence_reported_after_existence_does_not_supersede(self, api):
        """The deliberately-excluded direction.

        Going from "we hold this" to "it is gone" is a lifecycle transition B owns,
        not a correction of this operation's outcome. Treating it as superseding
        would let a stale or wrong-resource absence answer clear a resource we have
        provider evidence for — the false-completion direction. Asserted so the
        asymmetry is a tested decision rather than an accident of implementation.
        """
        await self._record(api)
        await self._conclude(
            api, outcome=CallOutcome.AMBIGUOUS.value, observation=self._present()
        )

        later = await self._conclude(
            api,
            outcome=CallOutcome.AMBIGUOUS.value,
            observation={
                "presence": ProviderPresence.ABSENT.value,
                "queried_by": "sky-cluster-alpha",
            },
        )
        assert later.json()["applied"] is False
        assert later.json()["result"] == ReconcileResult.RECONCILED_EXISTS.value
        assert later.json()["may_repeat_operation"] is False

    async def test_a_present_answer_about_another_resource_does_not_reopen(self, api):
        """The supersession gate must apply the contract's own identity rule.

        A `PRESENT` answer only supersedes if the query that produced it used one of
        *this* operation's identifiers. Admitting one queried by something else is
        not merely useless: `reconcile` answers a non-identifying observation with
        `UNRESOLVED`, and that answer is then stored — so letting the report through
        replaces an established conclusion with a non-conclusion and clears
        `concluded_at`. The row lands back in the recovery read as unresolved, and
        the operator reading it is told the provider could not be consulted when in
        fact it answered about a different resource.

        Asserted against the *established absence* case specifically, because that
        is the state the supersession rule was added to correct and therefore the
        one whose gate a non-identifying report can slip through.
        """
        await self._record(api)
        established = await self._conclude(
            api,
            outcome=CallOutcome.AMBIGUOUS.value,
            observation={
                "presence": ProviderPresence.ABSENT.value,
                "queried_by": "sky-cluster-alpha",
            },
        )
        assert established.json()["result"] == ReconcileResult.RETRY_PERMITTED.value

        # PRESENT, but obtained by querying a resource this operation never named.
        later = await self._conclude(
            api,
            outcome=CallOutcome.AMBIGUOUS.value,
            observation=self._present(queried_by="an-unrelated-cluster"),
        )
        assert later.json()["applied"] is False
        assert later.json()["result"] == ReconcileResult.RETRY_PERMITTED.value
        assert later.json()["state"] == handle_service.OperationState.CONCLUDED.value

        # The stored conclusion is intact, including the instant that closed it.
        async with async_session_test() as session:
            row = await session.get(
                ProviderOperation,
                {"idempotency_key": "idem-alpha-0001", "workspace": _WS_OWNED},
            )
            assert row.state == handle_service.OperationState.CONCLUDED.value
            assert row.reconcile_result == ReconcileResult.RETRY_PERMITTED.value
            assert row.concluded_at is not None
            # The evidence columns still describe the answer that concluded it.
            assert row.observation_queried_by == "sky-cluster-alpha"
            assert row.provider_presence == ProviderPresence.ABSENT.value

    async def test_a_present_answer_queried_by_the_reported_reference_supersedes(
        self, api
    ):
        """The identity rule must not reject the reference being reported.

        A provider query legitimately uses the identifier the provider returned, and
        that reference can arrive on the very report carrying the observation. The
        identity test is applied to the handle *including* the supplied reference —
        the same handle `reconcile` receives — so this report identifies the
        operation and supersedes. Asserted because an identity check written against
        the stored row instead would refuse exactly the richest legitimate report.
        """
        await self._record(api)
        await self._conclude(
            api,
            outcome=CallOutcome.AMBIGUOUS.value,
            observation={
                "presence": ProviderPresence.ABSENT.value,
                "queried_by": "sky-cluster-alpha",
            },
        )

        later = await self._conclude(
            api,
            outcome=CallOutcome.AMBIGUOUS.value,
            provider_reference="i-0abc123realinstance",
            observation=self._present(queried_by="i-0abc123realinstance"),
        )
        assert later.json()["applied"] is True
        assert later.json()["result"] == ReconcileResult.RECONCILED_EXISTS.value
        # The repeat authorization the absence conclusion carried is withdrawn.
        assert later.json()["may_repeat_operation"] is False

        async with async_session_test() as session:
            row = await session.get(
                ProviderOperation,
                {"idempotency_key": "idem-alpha-0001", "workspace": _WS_OWNED},
            )
            assert row.provider_reference == "i-0abc123realinstance"
            assert row.reconcile_result == ReconcileResult.RECONCILED_EXISTS.value

    async def test_a_failure_contradicted_by_its_own_observation_is_refused(self, api):
        """A report that says both "it did not run" and "it is present".

        The contract answers `FAILED` from the outcome alone — its failure branch
        returns before the observation is consulted — so without this guard a
        re-decided `FAILED` report would store "repeat permitted" again even while
        carrying proof the resource exists, defeating supersession through the very
        path most likely to produce it.

        Refused rather than reinterpreted, and nothing is written: the caller has a
        correct way to express this (an ambiguous outcome with the same
        observation), and picking a winner here would make this module a second
        decision-maker over outcomes the contract owns.
        """
        await self._record(api)
        contradictory = await self._conclude(
            api, outcome=CallOutcome.FAILED.value, observation=self._present()
        )
        assert contradictory.status_code == 409, contradictory.text
        assert "present" in contradictory.json()["detail"]

        # Nothing stored: the operation is still open for a correct report.
        recovered = await api.get(
            _BASE,
            params={"workspace": _WS_OWNED},
            headers={"Authorization": _OWNED_CRED},
        )
        assert [row["state"] for row in recovered.json()] == [
            handle_service.OperationState.RECORDED.value
        ]

    async def test_a_blank_provider_reference_is_invalid_input_not_a_conflict(
        self, api
    ):
        """409 on this route means "a different reference is already recorded".

        A client must not retry past that. So answering malformed input with the
        same status would stop a recovery driver from correcting its request and
        re-reporting — it would read a fixable mistake as a permanent one.
        """
        await self._record(api)
        response = await self._conclude(
            api, outcome=CallOutcome.AMBIGUOUS.value, provider_reference="   "
        )
        assert response.status_code == 422, response.text


class TestForeignWorkspaceRejection:
    """Cross-workspace exposure is the highest-severity failure class here."""

    async def test_recording_into_another_workspace_is_refused(self, api):
        """A body-supplied workspace grants nothing."""
        response = await api.post(
            _BASE,
            json=_owned(workspace=_WS_FOREIGN),
            headers={"Authorization": _OWNED_CRED},
        )
        assert response.status_code == 403

        # Nothing was written for the workspace the caller does not hold.
        async with async_session_test() as session:
            row = await session.get(
                ProviderOperation,
                {"idempotency_key": "idem-alpha-0001", "workspace": _WS_OWNED},
            )
            assert row is None

    async def test_another_workspace_s_operation_is_invisible(self, api):
        """A foreign caller cannot read an operation by knowing its key.

        404 rather than 403: a 403 would confirm the operation exists, which is
        itself cross-tenant information.
        """
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})

        response = await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json={
                "outcome": CallOutcome.SUCCEEDED.value,
                "operation_authority": "authority-from-b",
                "workspace": _WS_OWNED,
            },
            headers={"Authorization": _FOREIGN_CRED},
        )
        assert response.status_code == 404

    async def test_the_recovery_read_refuses_a_workspace_outside_the_grant(self, api):
        """Listing another workspace's in-flight operations is refused."""
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})

        response = await api.get(
            _BASE,
            params={"workspace": _WS_OWNED},
            headers={"Authorization": _FOREIGN_CRED},
        )
        assert response.status_code == 403

    async def test_the_refusal_does_not_reveal_whether_the_workspace_exists(self, api):
        """A nonexistent and a foreign workspace are refused identically.

        Differing reasons would let a caller enumerate other tenants' workspaces by
        reading error messages.
        """
        foreign = await api.get(
            _BASE,
            params={"workspace": _WS_OWNED},
            headers={"Authorization": _FOREIGN_CRED},
        )
        nonexistent = await api.get(
            _BASE,
            params={"workspace": "ws-does-not-exist"},
            headers={"Authorization": _FOREIGN_CRED},
        )
        assert foreign.status_code == nonexistent.status_code == 403
        assert foreign.json()["detail"] == nonexistent.json()["detail"]

    async def test_an_unauthenticated_caller_is_refused(self, api):
        """No credential, no access — and a 401 rather than a validation error."""
        response = await api.post(_BASE, json=_owned())
        assert response.status_code == 401

    async def test_an_unknown_credential_is_refused(self, api):
        response = await api.post(
            _BASE,
            json=_owned(),
            headers={"Authorization": "Bearer not-a-configured-credential"},
        )
        assert response.status_code == 401

    async def test_two_workspaces_may_hold_the_same_derived_idempotency_key(self, api):
        """The collision case, which needs no attacker to happen.

        The keys the callers actually generate are derived, low-entropy strings
        rather than UUIDs: the provider adapters build `sp-<cloud>-<gpu>-<count>`
        and the cluster-join path passes a cluster name verbatim, so nothing in the
        input names a tenant. Two workspaces each provisioning one A100 on AWS
        therefore compute the *same* key, and the first implementation of this story
        refused the second one a row.

        That refusal was not a cosmetic error. No row means no `confirmed_at`, and
        the durability rule is honest about that — `authorize_provider_call` refuses
        a call whose record is not durable. So the second tenant's provisioning
        failed, for work another tenant was doing, on a reason it had no way to act
        on. This asserts both tenants get a durable record.
        """
        collision_key = "sp-aws-a100-1"

        first = await api.post(
            _BASE,
            json=_owned(idempotency_key=collision_key),
            headers={"Authorization": _OWNED_CRED},
        )
        assert first.status_code == 201, first.text

        second = await api.post(
            _BASE,
            json=_owned(workspace=_WS_FOREIGN, idempotency_key=collision_key),
            headers={"Authorization": _FOREIGN_CRED},
        )
        assert second.status_code == 201, second.text

        # Both records are durable, so both tenants may proceed with their call.
        for response in (first, second):
            assert response.json()["durable"] is True

        # ...and each sees only its own operation under that shared key.
        for credential, workspace in (
            (_OWNED_CRED, _WS_OWNED),
            (_FOREIGN_CRED, _WS_FOREIGN),
        ):
            listed = await api.get(
                _BASE,
                params={"workspace": workspace},
                headers={"Authorization": credential},
            )
            assert [row["workspace"] for row in listed.json()] == [workspace]

    async def test_a_duplicate_key_within_one_workspace_is_still_refused(self, api):
        """Scoping the identity must not weaken duplicate detection.

        The conflict *is* the duplicate detection, so the repair for the
        cross-tenant collision would be worthless if it also stopped a genuine
        repeat inside one workspace from conflicting.
        """
        collision_key = "sp-aws-a100-1"
        await api.post(
            _BASE,
            json=_owned(idempotency_key=collision_key),
            headers={"Authorization": _OWNED_CRED},
        )
        repeat = await api.post(
            _BASE,
            json=_owned(idempotency_key=collision_key),
            headers={"Authorization": _OWNED_CRED},
        )
        assert repeat.status_code == 409

    async def test_concluding_another_tenants_key_cannot_reach_its_operation(self, api):
        """Knowing a key is not access to the operation it names.

        Because keys are derived, a tenant can *guess* another tenant's key without
        trying — so the report a foreign caller gets for a key that exists must be
        identical to the one for a key that does not.
        """
        collision_key = "sp-aws-a100-1"
        await api.post(
            _BASE,
            json=_owned(idempotency_key=collision_key),
            headers={"Authorization": _OWNED_CRED},
        )

        body = {
            "outcome": CallOutcome.SUCCEEDED.value,
            "operation_authority": "authority-from-b",
            "workspace": _WS_FOREIGN,
        }
        existing_elsewhere = await api.post(
            f"{_BASE}/{collision_key}/conclude",
            json=body,
            headers={"Authorization": _FOREIGN_CRED},
        )
        never_used = await api.post(
            f"{_BASE}/never-used-key/conclude",
            json=body,
            headers={"Authorization": _FOREIGN_CRED},
        )

        # Byte-identical answers: the status and the reason both. A difference in
        # either would report whether the key is in use in a workspace the caller
        # cannot read.
        assert existing_elsewhere.status_code == never_used.status_code == 404
        assert existing_elsewhere.json()["detail"] == never_used.json()["detail"]

        # The owner's operation is untouched by the foreign attempt.
        async with async_session_test() as session:
            row = await session.get(
                ProviderOperation,
                {"idempotency_key": collision_key, "workspace": _WS_OWNED},
            )
            assert row.state == handle_service.OperationState.RECORDED.value


class TestProviderReferenceOnAConcludedOperation:
    """A repeat report's provider reference is examined, not skipped.

    The idempotency short-circuit answers a repeat from the stored row. That is
    right for the conclusion — which must not be re-decided from a later, possibly
    weaker observation — but the provider reference is the one field of a repeat
    that can carry information the row does not already have, so reading it cannot
    wait until after the decision to treat the report as a no-op.

    A review reproduced the first case below; the second was found while fixing it.
    Both lose track of a real provider resource, which is what makes them this
    story's concern rather than a status-code preference:

    * a *conflicting* reference answered "already handled" hides an operation that
      demonstrably ran twice — the only thing two references for one idempotency
      identity can mean — leaving the second resource running and unnamed;
    * a *first* reference discarded leaves provider evidence that a resource exists
      with no identifier to release it by.

    Both assert the stored row, not just the response: a reference echoed in a reply
    but never written passes a response-only check and still loses the resource.
    """

    async def _record(self, api):
        response = await api.post(
            _BASE, json=_owned(), headers={"Authorization": _OWNED_CRED}
        )
        assert response.status_code == 201, response.text

    async def _conclude(self, api, **body):
        payload = {
            "operation_authority": "authority-from-b",
            "workspace": _WS_OWNED,
            "outcome": CallOutcome.AMBIGUOUS.value,
        }
        payload.update(body)
        return await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json=payload,
            headers={"Authorization": _OWNED_CRED},
        )

    @staticmethod
    def _present() -> dict:
        """A provider answer that the resource exists, built through the contract.

        Routed through `ProviderObservation` so the fixture inherits the contract's
        rule that a PRESENT answer carries the provider's reported state, rather
        than asserting existence with no evidence.
        """
        observation = ProviderObservation(
            presence=ProviderPresence.PRESENT,
            queried_by="sky-cluster-alpha",
            provider_state="RUNNING",
        )
        return {
            "presence": observation.presence.value,
            "queried_by": observation.queried_by,
            "provider_state": observation.provider_state,
        }

    async def _stored(self) -> ProviderOperation:
        async with async_session_test() as session:
            return await session.get(
                ProviderOperation,
                {"idempotency_key": "idem-alpha-0001", "workspace": _WS_OWNED},
            )

    async def test_a_different_reference_after_a_concluded_existence_is_refused(
        self, api
    ):
        """Two references for one operation stays a conflict once concluded.

        The same report is already refused while the operation is open. Concluding
        it must not turn that refusal into a silent acceptance, because the
        conclusion being `RECONCILED_EXISTS` means a resource really is there — so a
        second, different identifier is evidence a second resource is too.
        """
        await self._record(api)
        first = await self._conclude(
            api, observation=self._present(), provider_reference="sky-request-first"
        )
        assert first.status_code == 200, first.text
        assert first.json()["result"] == ReconcileResult.RECONCILED_EXISTS.value

        second = await self._conclude(
            api, observation=self._present(), provider_reference="sky-request-second"
        )
        assert second.status_code == 409, (
            f"a second, different provider reference was accepted: {second.text}"
        )

        # The original identifier is retained and the conflict is durably visible.
        row = await self._stored()
        assert row.provider_reference == "sky-request-first"
        assert row.reconcile_result == ReconcileResult.UNRESOLVED.value
        assert [item.provider_reference for item in row.conflicts] == [
            "sky-request-second"
        ]

    async def test_a_first_reference_learned_after_the_conclusion_is_recorded(
        self, api
    ):
        """Presence can be established before the identifier is known.

        A provider check can confirm a resource exists without returning its id, so
        a later and richer query is how the identifier legitimately arrives. Dropping
        it leaves the worst pair available: evidence the resource is there, and no
        way to name it for release.
        """
        await self._record(api)
        concluded = await self._conclude(api, observation=self._present())
        assert concluded.status_code == 200, concluded.text
        assert concluded.json()["applied"] is True
        assert (await self._stored()).provider_reference is None

        later = await self._conclude(
            api,
            observation=self._present(),
            provider_reference="i-0abc123realinstance",
        )
        assert later.status_code == 200, later.text
        # No conclusion was newly applied — the established outcome is untouched.
        assert later.json()["applied"] is False
        assert later.json()["result"] == ReconcileResult.RECONCILED_EXISTS.value

        row = await self._stored()
        assert row.provider_reference == "i-0abc123realinstance", (
            "the identifier the resource must be released by was discarded"
        )
        # The conclusion itself is unchanged: only the identifier was filled in.
        assert row.reconcile_result == ReconcileResult.RECONCILED_EXISTS.value

    async def test_an_identical_repeat_still_applies_nothing(self, api):
        """The idempotency guarantee the reordering must not have broken.

        Asserted with a reference present, since that is the path that now performs
        a write: repeating the *same* reference must find nothing to record and
        leave the row as it was.
        """
        await self._record(api)
        await self._conclude(
            api, observation=self._present(), provider_reference="sky-request-first"
        )
        before = await self._stored()
        concluded_at = before.concluded_at

        repeat = await self._conclude(
            api, observation=self._present(), provider_reference="sky-request-first"
        )
        assert repeat.status_code == 200, repeat.text
        assert repeat.json()["applied"] is False

        after = await self._stored()
        assert after.provider_reference == "sky-request-first"
        # The conclusion instant is untouched, so the retry did not re-conclude.
        assert after.concluded_at == concluded_at


class TestUnresolvedAllocationRetention:
    """R15 acceptance 7: no accounting clearance while exposure is unresolved."""

    async def _record_two_resources(self, api):
        for index, name in enumerate(("sky-node-a", "sky-node-b")):
            await api.post(
                _BASE,
                json=_owned(
                    resource_name=name, idempotency_key=f"idem-release-{index}"
                ),
                headers={"Authorization": _OWNED_CRED},
            )

    async def test_an_unreachable_provider_keeps_cost_exposure_unresolved(self, api):
        """Unknown is explicitly not zero.

        The allocation is retained and the resource named, so an operator can go
        and look — an unresolved release that names nothing is unactionable.
        """
        await self._record_two_resources(api)

        response = await api.post(
            f"{_BASE}/allocations/alloc-alpha/release-assessment",
            json={
                "workspace": _WS_OWNED,
                "operation_authority": "authority-from-b",
                "observations": {
                    "sky-node-a": {
                        "presence": ProviderPresence.ABSENT.value,
                        "queried_by": "sky-node-a",
                    },
                    "sky-node-b": {
                        "presence": ProviderPresence.UNKNOWN.value,
                        "queried_by": "sky-node-b",
                        "detail": "credential expired",
                    },
                },
            },
            headers={"Authorization": _OWNED_CRED},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["state"] == "unresolved"
        assert payload["exposure"] == "unresolved"
        assert payload["may_mark_released"] is False
        assert payload["may_return_reservation_unused"] is False
        assert "sky-node-b" in payload["unresolved_resources"]

    async def test_an_empty_provider_response_cannot_clear_a_non_empty_allocation(
        self, api
    ):
        """The expected inventory comes from stored records, not observed keys.

        Deriving it from the observations would make 'the provider told us about
        nothing' equivalent to 'nothing is there', which is the false-completion
        bug.
        """
        await self._record_two_resources(api)

        response = await api.post(
            f"{_BASE}/allocations/alloc-alpha/release-assessment",
            json={
                "workspace": _WS_OWNED,
                "operation_authority": "authority-from-b",
                "observations": {},
            },
            headers={"Authorization": _OWNED_CRED},
        )
        payload = response.json()
        assert payload["state"] == "unresolved"
        assert payload["may_mark_released"] is False
        assert sorted(payload["unresolved_resources"]) == ["sky-node-a", "sky-node-b"]

    async def test_a_resource_still_present_retains_the_allocation(self, api):
        await self._record_two_resources(api)

        response = await api.post(
            f"{_BASE}/allocations/alloc-alpha/release-assessment",
            json={
                "workspace": _WS_OWNED,
                "operation_authority": "authority-from-b",
                "observations": {
                    "sky-node-a": {
                        "presence": ProviderPresence.ABSENT.value,
                        "queried_by": "sky-node-a",
                    },
                    "sky-node-b": {
                        "presence": ProviderPresence.PRESENT.value,
                        "queried_by": "sky-node-b",
                        "provider_state": "RUNNING",
                    },
                },
            },
            headers={"Authorization": _OWNED_CRED},
        )
        payload = response.json()
        assert payload["state"] == "retained"
        assert payload["exposure"] == "active"
        assert payload["may_mark_released"] is False

    async def test_release_is_reported_only_when_every_resource_is_absent(self, api):
        """The positive case, so the assessment is not trivially always-unresolved."""
        await self._record_two_resources(api)

        response = await api.post(
            f"{_BASE}/allocations/alloc-alpha/release-assessment",
            json={
                "workspace": _WS_OWNED,
                "operation_authority": "authority-from-b",
                "observations": {
                    "sky-node-a": {
                        "presence": ProviderPresence.ABSENT.value,
                        "queried_by": "sky-node-a",
                    },
                    "sky-node-b": {
                        "presence": ProviderPresence.ABSENT.value,
                        "queried_by": "sky-node-b",
                    },
                },
            },
            headers={"Authorization": _OWNED_CRED},
        )
        payload = response.json()
        assert payload["state"] == "released"
        assert payload["exposure"] == "none"
        assert payload["may_mark_released"] is True
        assert payload["may_return_reservation_unused"] is True

    async def test_assessing_another_workspace_s_allocation_is_refused(self, api):
        await self._record_two_resources(api)

        response = await api.post(
            f"{_BASE}/allocations/alloc-alpha/release-assessment",
            json={
                "workspace": _WS_OWNED,
                "operation_authority": "authority-from-b",
                "observations": {},
            },
            headers={"Authorization": _FOREIGN_CRED},
        )
        assert response.status_code == 403

    async def test_an_unknown_allocation_is_not_reported_as_released(self, api):
        """An allocation with no stored records cannot be assessed at all.

        Answering 'released' for an allocation the system knows nothing about would
        be the same false completion, reached by a different route.
        """
        response = await api.post(
            f"{_BASE}/allocations/alloc-nonexistent/release-assessment",
            json={
                "workspace": _WS_OWNED,
                "operation_authority": "authority-from-b",
                "observations": {},
            },
            headers={"Authorization": _OWNED_CRED},
        )
        assert response.status_code == 404


class TestServiceLayerDirectly:
    """A few properties that are clearer against the service than over HTTP."""

    async def test_conclusion_locks_the_row_before_deciding(self, monkeypatch):
        """Concurrent reports cannot both decide from the same pre-call state.

        SQLite ignores ``FOR UPDATE``, so this regression observes the SQLAlchemy
        request itself. Production PostgreSQL then serializes the terminal check
        and write: a second report sees the first conclusion and must pass through
        the evidence-aware idempotency rules instead of overwriting it.
        """
        submitter = _submitter()
        async with async_session_test() as session:
            await handle_service.record_handle(
                session,
                operation_authority="authority-from-b",
                submitter=submitter,
                handle=_handle(),
            )
            real_get = session.get
            lock_requests: list[bool] = []

            async def observed_get(entity, ident, **kwargs):
                if entity is ProviderOperation:
                    lock_requests.append(kwargs.get("with_for_update", False))
                return await real_get(entity, ident, **kwargs)

            monkeypatch.setattr(session, "get", observed_get)
            await handle_service.conclude_operation(
                session,
                submitter=submitter,
                workspace=_WS_OWNED,
                idempotency_key="idem-alpha-0001",
                outcome=CallOutcome.AMBIGUOUS,
                operation_authority="authority-from-b",
                observation=ProviderObservation(
                    presence=ProviderPresence.PRESENT,
                    queried_by="sky-cluster-alpha",
                    provider_state="RUNNING",
                ),
            )

        assert lock_requests == [False, True]

    async def test_the_stored_pre_call_handle_is_what_reconciliation_uses(self):
        """Reconciliation operates on the persisted form, not the caller's copy.

        A caller that passed a different resource name on the conclude call cannot
        redirect the reconciliation at another resource, because the handle is
        rebuilt from the row.
        """
        submitter = _submitter()
        async with async_session_test() as session:
            await handle_service.record_handle(
                session,
                operation_authority="authority-from-b",
                submitter=submitter,
                handle=_handle(),
            )
            row, applied, result = await handle_service.conclude_operation(
                session,
                submitter=submitter,
                workspace=_WS_OWNED,
                idempotency_key="idem-alpha-0001",
                outcome=CallOutcome.AMBIGUOUS,
                operation_authority="authority-from-b",
                observation=ProviderObservation(
                    presence=ProviderPresence.PRESENT,
                    queried_by="sky-cluster-alpha",
                    provider_state="RUNNING",
                ),
            )
        assert applied is True
        assert result is ReconcileResult.RECONCILED_EXISTS
        assert row.resource_name == "sky-cluster-alpha"

    async def test_a_provider_refusal_permits_a_repeat_without_a_re_check(self):
        """A refusal is the provider's own answer that the operation did not run.

        The one path where upstream's classification is right — and the reason the
        timeout branch sharing it is the whole defect.
        """
        submitter = _submitter()
        async with async_session_test() as session:
            await handle_service.record_handle(
                session,
                operation_authority="authority-from-b",
                submitter=submitter,
                handle=_handle(),
            )
            _, _, result = await handle_service.conclude_operation(
                session,
                submitter=submitter,
                workspace=_WS_OWNED,
                idempotency_key="idem-alpha-0001",
                outcome=CallOutcome.FAILED,
                operation_authority="authority-from-b",
                observation=None,
            )
        assert result is ReconcileResult.RETRY_PERMITTED

    async def test_a_submitter_with_no_grant_is_authorized_for_nothing(self):
        """Stated explicitly: an empty grant covers no workspace."""
        ungranted = Submitter(submitter_id="adapter-none", workspaces=frozenset())
        async with async_session_test() as session:
            with pytest.raises(handle_service.HandleRefused) as refusal:
                await handle_service.record_handle(
                    session,
                    operation_authority="authority-from-b",
                    submitter=ungranted,
                    handle=_handle(),
                )
        assert refusal.value.status_code == 403

    async def test_the_recovery_read_can_narrow_to_one_allocation(self):
        submitter = _submitter()
        async with async_session_test() as session:
            await handle_service.record_handle(
                session,
                operation_authority="authority-from-b",
                submitter=submitter,
                handle=_handle(),
            )
            await handle_service.record_handle(
                session,
                operation_authority="authority-from-b",
                submitter=submitter,
                handle=_handle(
                    idempotency_key="idem-beta-0001",
                    allocation_id="alloc-beta",
                    resource_name="sky-cluster-beta",
                ),
            )
            narrowed = await handle_service.list_unconcluded(
                session,
                submitter=submitter,
                workspace=_WS_OWNED,
                allocation_id="alloc-beta",
            )
        assert [row.idempotency_key for row in narrowed] == ["idem-beta-0001"]

    async def test_release_operations_are_recorded_like_any_other(self):
        """Ambiguity on a release is not free either.

        A lost response read as failure leads to a second release attempt; read as
        success it leaves a resource nobody is looking for. So RELEASE gets the same
        durable identity as PROVISION.
        """
        submitter = _submitter()
        async with async_session_test() as session:
            record = await handle_service.record_handle(
                session,
                operation_authority="authority-from-b",
                submitter=submitter,
                handle=_handle(
                    operation=OperationKind.RELEASE,
                    idempotency_key="idem-release-op",
                ),
            )
        assert record.durable is True
        assert authorize_provider_call(record).permitted is True


class TestVerifiedOperationAuthority:
    async def _record(self, api):
        response = await api.post(
            _BASE, json=_owned(), headers={"Authorization": _OWNED_CRED}
        )
        assert response.status_code == 201, response.text

    async def _conclude(self, api, authority="authority-from-b"):
        return await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json={
                "workspace": _WS_OWNED,
                "operation_authority": authority,
                "outcome": "failed",
            },
            headers={"Authorization": _OWNED_CRED},
        )

    async def test_no_live_validator_means_no_durable_call_authorization(
        self, api, monkeypatch
    ):
        monkeypatch.setattr(provider_authority, "_validator", None)
        response = await api.post(
            _BASE, json=_owned(), headers={"Authorization": _OWNED_CRED}
        )
        assert response.status_code == 503
        async with async_session_test() as session:
            assert (
                await session.get(
                    ProviderOperation,
                    {"workspace": _WS_OWNED, "idempotency_key": "idem-alpha-0001"},
                )
                is None
            )

    async def test_fabricated_authority_cannot_record_or_conclude(self, api):
        response = await api.post(
            _BASE,
            json=_owned(operation_authority="x"),
            headers={"Authorization": _OWNED_CRED},
        )
        assert response.status_code == 403
        await self._record(api)
        assert (await self._conclude(api, "x")).status_code == 403
        async with async_session_test() as session:
            row = await session.get(
                ProviderOperation,
                {"workspace": _WS_OWNED, "idempotency_key": "idem-alpha-0001"},
            )
            assert row.state == "recorded"
            assert row.authority_run_id == "run-1"
            assert row.authority_attempt_id == "attempt-1"

    @pytest.mark.parametrize(
        "change",
        [
            {"expires_at": datetime(2000, 1, 1, tzinfo=UTC)},
            {"expires_at": datetime(2099, 1, 1)},
            {"active": False},
            {"submitter_id": "another-executor"},
            {"operation_id": "another-operation"},
            {"run_id": "another-run"},
            {"attempt_id": "another-attempt"},
            {"handle": _handle(idempotency_key="other-key")},
            {"handle": _handle(workspace=_WS_FOREIGN)},
            {"handle": _handle(provider="nebius")},
            {"handle": _handle(allocation_id="other-allocation")},
        ],
    )
    async def test_inactive_or_differently_bound_authority_cannot_conclude(
        self, api, monkeypatch, authority_validator, change
    ):
        await self._record(api)
        trusted = await authority_validator.resolve(
            "authority-from-b", submitter=_submitter(), handle=_handle()
        )

        async def resolve(*args, **kwargs):
            return replace(trusted, **change)

        monkeypatch.setattr(authority_validator, "resolve", resolve)
        assert (await self._conclude(api)).status_code == 403
        async with async_session_test() as session:
            row = await session.get(
                ProviderOperation,
                {"workspace": _WS_OWNED, "idempotency_key": "idem-alpha-0001"},
            )
            assert row.state == "recorded"
            assert row.reconcile_result is None

    async def test_revocation_is_checked_even_for_terminal_repeat(
        self, api, monkeypatch
    ):
        await self._record(api)
        assert (await self._conclude(api)).status_code == 200
        monkeypatch.setattr(provider_authority, "_validator", None)
        assert (await self._conclude(api)).status_code == 503

    async def test_validator_outage_is_a_controlled_refusal(
        self, api, monkeypatch, authority_validator
    ):
        await self._record(api)

        async def resolve(*args, **kwargs):
            raise RuntimeError("upstream authority-from-b must not leak")

        monkeypatch.setattr(authority_validator, "resolve", resolve)
        response = await self._conclude(api)
        assert response.status_code == 503
        assert "authority-from-b" not in response.text


class TestLateSuccessAndReference:
    _record = TestProviderReferenceOnAConcludedOperation._record
    _conclude = TestProviderReferenceOnAConcludedOperation._conclude
    _stored = TestProviderReferenceOnAConcludedOperation._stored

    @pytest.mark.parametrize(
        "late",
        [
            {"outcome": "succeeded"},
            {"outcome": "succeeded", "provider_reference": "i-late"},
            {"outcome": "failed", "provider_reference": "i-late"},
            {"outcome": "ambiguous", "provider_reference": "i-late"},
        ],
    )
    async def test_late_success_or_reference_removes_retry_permission(self, api, late):
        await self._record(api)
        first = await self._conclude(api, outcome="failed")
        assert first.json()["may_repeat_operation"] is True
        response = await self._conclude(api, **late)
        assert response.status_code == 200, response.text
        assert response.json()["may_repeat_operation"] is False
        row = await self._stored()
        assert row.reconcile_result != ReconcileResult.RETRY_PERMITTED.value
        if late["outcome"] == "succeeded":
            assert row.reconcile_result == ReconcileResult.RECONCILED_EXISTS.value
        else:
            assert row.state == "unresolved"
            assert row.concluded_at is None

    async def test_first_failed_report_with_reference_needs_absence_evidence(self, api):
        await self._record(api)
        response = await self._conclude(
            api, outcome="failed", provider_reference="i-existing"
        )
        assert response.status_code == 200, response.text
        assert response.json()["may_repeat_operation"] is False
        assert (await self._stored()).state == "unresolved"
        absent = await self._conclude(
            api,
            outcome="ambiguous",
            observation={"presence": "absent", "queried_by": "i-existing"},
        )
        assert absent.status_code == 200, absent.text
        assert absent.json()["may_repeat_operation"] is True

    async def test_two_operations_sharing_name_do_not_share_absence_proof(self, api):
        for key, provider, reference in (
            ("key-aws", "aws", "i-first"),
            ("key-nebius", "nebius", "i-second"),
        ):
            response = await api.post(
                _BASE,
                json=_owned(idempotency_key=key, provider=provider),
                headers={"Authorization": _OWNED_CRED},
            )
            assert response.status_code == 201, response.text
            response = await api.post(
                f"{_BASE}/{key}/conclude",
                json={
                    "workspace": _WS_OWNED,
                    "operation_authority": "authority-from-b",
                    "outcome": "succeeded",
                    "provider_reference": reference,
                },
                headers={"Authorization": _OWNED_CRED},
            )
            assert response.status_code == 200, response.text
        response = await api.post(
            f"{_BASE}/allocations/alloc-alpha/release-assessment",
            json={
                "workspace": _WS_OWNED,
                "operation_authority": "authority-from-b",
                "observations": {
                    "sky-cluster-alpha": {
                        "presence": "absent",
                        "queried_by": "sky-cluster-alpha",
                    }
                },
            },
            headers={"Authorization": _OWNED_CRED},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["may_mark_released"] is False
        assert body["may_return_reservation_unused"] is False
        assert body["exposure"] == "unresolved"
        assert set(body["unresolved_resources"]) == {"i-first", "i-second"}

    async def test_rejecting_contradictory_presence_withdraws_prior_retry(self, api):
        await self._record(api)
        await self._conclude(api, outcome="failed")
        response = await self._conclude(
            api,
            outcome="failed",
            observation={
                "presence": "present",
                "queried_by": "sky-cluster-alpha",
                "provider_state": "RUNNING",
            },
        )
        assert response.status_code == 409
        assert (await self._stored()).reconcile_result == "unresolved"

    async def test_conflicting_reference_withdraws_prior_retry_without_overwriting_id(
        self, api
    ):
        await self._record(api)
        response = await self._conclude(
            api,
            provider_reference="i-first",
            observation={
                "presence": "absent",
                "queried_by": "i-first",
            },
        )
        assert response.json()["may_repeat_operation"] is True
        conflict = await self._conclude(api, provider_reference="i-second")
        assert conflict.status_code == 409
        row = await self._stored()
        assert row.reconcile_result == "unresolved"
        assert row.provider_reference == "i-first"
        assert [item.provider_reference for item in row.conflicts] == ["i-second"]

    async def test_exact_absence_repeat_with_reference_does_not_reapply(self, api):
        await self._record(api)
        payload = {
            "provider_reference": "i-gone",
            "observation": {
                "presence": "absent",
                "queried_by": "i-gone",
            },
        }
        first = await self._conclude(api, **payload)
        assert first.json()["applied"] is True
        before = (await self._stored()).concluded_at
        repeat = await self._conclude(api, **payload)
        assert repeat.status_code == 200, repeat.text
        assert repeat.json()["applied"] is False
        assert repeat.json()["may_repeat_operation"] is True
        assert (await self._stored()).concluded_at == before

    async def test_conflict_survives_lost_response_and_remains_in_recovery(self, api):
        await self._record(api)
        await self._conclude(api, outcome="succeeded", provider_reference="i-first")
        # Ignore the409, exactly as when the reporter loses the response/crashes.
        await self._conclude(api, outcome="succeeded", provider_reference="i-second")
        recovered = await api.get(
            _BASE,
            params={"workspace": _WS_OWNED},
            headers={"Authorization": _OWNED_CRED},
        )
        row = recovered.json()[0]
        assert row["state"] == "unresolved"
        assert row["provider_reference"] == "i-first"
        assert row["conflicting_provider_references"] == ["i-second"]
        # Redelivery is durable/idempotent, and a normal success cannot clear it.
        await self._conclude(api, outcome="succeeded", provider_reference="i-second")
        repeat = await self._conclude(
            api, outcome="succeeded", provider_reference="i-first"
        )
        assert repeat.json()["result"] == "unresolved"
        assert len((await self._stored()).conflicts) == 1
        release = await api.post(
            f"{_BASE}/allocations/alloc-alpha/release-assessment",
            json={
                "workspace": _WS_OWNED,
                "operation_authority": "authority-from-b",
                "observations": {
                    "sky-cluster-alpha": {
                        "presence": "absent",
                        "queried_by": "sky-cluster-alpha",
                    }
                },
            },
            headers={"Authorization": _OWNED_CRED},
        )
        assert release.json()["may_return_reservation_unused"] is False
        assert set(release.json()["unresolved_resources"]) == {"i-first", "i-second"}


class TestIndependentAllocationMembership:
    async def _record_and_assess(self, api, observations):
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})
        return await api.post(
            f"{_BASE}/allocations/alloc-alpha/release-assessment",
            json={
                "workspace": _WS_OWNED,
                "operation_authority": "authority-from-b",
                "observations": observations,
            },
            headers={"Authorization": _OWNED_CRED},
        )

    @pytest.mark.parametrize("volume_presence", [None, "present"])
    async def test_primary_absence_cannot_clear_other_billed_resources(
        self, api, inventory_reader, volume_presence
    ):
        inventory_reader.resources = (
            provider_inventory.AllocationResourceIdentity(
                "compute",
                "aws",
                "sky-cluster-alpha",
                "compute",
                frozenset({"idem-alpha-0001"}),
            ),
            provider_inventory.AllocationResourceIdentity(
                "volume", "aws", "vol-extra", "storage"
            ),
            provider_inventory.AllocationResourceIdentity(
                "network", "aws", "eip-extra", "network"
            ),
        )
        observations = {
            "compute": {"presence": "absent", "queried_by": "sky-cluster-alpha"},
            "network": {"presence": "absent", "queried_by": "eip-extra"},
        }
        if volume_presence:
            observations["volume"] = {
                "presence": volume_presence,
                "queried_by": "vol-extra",
                "provider_state": "AVAILABLE",
            }
        response = await self._record_and_assess(api, observations)
        assert response.status_code == 200, response.text
        assert response.json()["may_return_reservation_unused"] is False
        assert "volume" in response.json()["unresolved_resources"]
        # Dropping a resource from a subsequent complete snapshot cannot erase it.
        inventory_reader.resources = inventory_reader.resources[:1]
        response = await self._record_and_assess(
            api, {"compute": observations["compute"]}
        )
        assert response.json()["may_mark_released"] is False
        assert {"volume", "network"}.issubset(response.json()["unresolved_resources"])

    async def test_operation_names_alone_never_authorize_release(
        self, api, monkeypatch
    ):
        monkeypatch.setattr(provider_inventory, "_reader", None)
        response = await self._record_and_assess(
            api,
            {
                "sky-cluster-alpha": {
                    "presence": "absent",
                    "queried_by": "sky-cluster-alpha",
                }
            },
        )
        assert response.json()["may_mark_released"] is False
        assert response.json()["exposure"] == "unresolved"

    async def test_incomplete_authoritative_inventory_never_authorizes_release(
        self, api, inventory_reader
    ):
        inventory_reader.complete = False
        response = await self._record_and_assess(api, {})
        assert response.json()["exposure"] == "unresolved"

    async def test_workspace_display_name_is_never_operation_authority(
        self, api, monkeypatch
    ):
        monkeypatch.setattr(
            settings,
            "observation_submitters",
            json.dumps(
                [
                    {
                        "submitter_id": "adapter-1",
                        "credential": _OWNED_CRED,
                        "signing_key": "fixture",
                        "workspaces": ["dev"],
                    }
                ]
            ),
        )
        response = await api.post(
            _BASE, json=_owned(workspace="dev"), headers={"Authorization": _OWNED_CRED}
        )
        assert response.status_code == 403
        for workspace in (_WS_OWNED, _WS_FOREIGN, "dev"):
            response = await api.get(
                _BASE,
                params={"workspace": workspace},
                headers={"Authorization": _OWNED_CRED},
            )
            assert response.status_code == 403

    async def test_complete_resource_inventory_and_absence_can_release(
        self, api, inventory_reader
    ):
        inventory_reader.resources = (
            provider_inventory.AllocationResourceIdentity(
                "compute",
                "aws",
                "sky-cluster-alpha",
                "compute",
                frozenset({"idem-alpha-0001"}),
            ),
            provider_inventory.AllocationResourceIdentity(
                "volume", "aws", "vol-extra", "storage"
            ),
            provider_inventory.AllocationResourceIdentity(
                "network", "aws", "eip-extra", "network"
            ),
        )
        response = await self._record_and_assess(
            api,
            {
                item.resource_id: {
                    "presence": "absent",
                    "queried_by": item.provider_reference,
                }
                for item in inventory_reader.resources
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["may_mark_released"] is True
        assert response.json()["may_return_reservation_unused"] is True

    @pytest.mark.parametrize(
        "change",
        [
            {"workspace": _WS_FOREIGN},
            {"org_id": str(uuid.uuid4())},
            {"allocation_id": "foreign-allocation"},
            {"expires_at": datetime(2000, 1, 1, tzinfo=UTC)},
        ],
    )
    async def test_inventory_requires_current_matching_authority(
        self, api, inventory_reader, monkeypatch, change
    ):
        original = inventory_reader.read

        async def mismatched(**kwargs):
            return replace(await original(**kwargs), **change)

        monkeypatch.setattr(inventory_reader, "read", mismatched)
        response = await self._record_and_assess(api, {})
        assert response.status_code == 403

    async def test_inventory_must_include_already_known_provider_reference(
        self, api, inventory_reader
    ):
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})
        response = await api.post(
            f"{_BASE}/idem-alpha-0001/conclude",
            json={
                "workspace": _WS_OWNED,
                "outcome": "succeeded",
                "operation_authority": "authority-from-b",
                "provider_reference": "i-known-but-omitted",
            },
            headers={"Authorization": _OWNED_CRED},
        )
        assert response.status_code == 200, response.text
        response = await self._record_and_assess(
            api,
            {
                item.resource_id: {
                    "presence": "absent",
                    "queried_by": item.provider_reference,
                }
                for item in inventory_reader.resources
            },
        )
        assert response.json()["may_mark_released"] is False
        assert "i-known-but-omitted" in response.json()["unresolved_resources"]

    async def test_conflicting_resources_need_individual_absence_proof(
        self, api, inventory_reader
    ):
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})
        for reference in ("i-first", "i-second"):
            await api.post(
                f"{_BASE}/idem-alpha-0001/conclude",
                json={
                    "workspace": _WS_OWNED,
                    "outcome": "succeeded",
                    "operation_authority": "authority-from-b",
                    "provider_reference": reference,
                },
                headers={"Authorization": _OWNED_CRED},
            )
        inventory_reader.resources = tuple(
            provider_inventory.AllocationResourceIdentity(
                reference, "aws", reference, "compute"
            )
            for reference in ("i-first", "i-second")
        )
        first_only = {"i-first": {"presence": "absent", "queried_by": "i-first"}}
        response = await self._record_and_assess(api, first_only)
        assert response.json()["may_mark_released"] is False
        assert "i-second" in response.json()["unresolved_resources"]
        response = await self._record_and_assess(
            api,
            {
                **first_only,
                "i-second": {"presence": "absent", "queried_by": "i-second"},
            },
        )
        assert response.json()["may_mark_released"] is True

    @pytest.mark.parametrize("authority", ["x", " "])
    async def test_workspace_credential_cannot_fabricate_cleanup_authority(
        self, api, authority
    ):
        await api.post(_BASE, json=_owned(), headers={"Authorization": _OWNED_CRED})
        response = await api.post(
            f"{_BASE}/allocations/alloc-alpha/release-assessment",
            json={
                "workspace": _WS_OWNED,
                "operation_authority": authority,
                "observations": {
                    "sky-node-a": {"presence": "absent", "queried_by": "sky-node-a"}
                },
            },
            headers={"Authorization": _OWNED_CRED},
        )
        assert response.status_code == 403

    @pytest.mark.parametrize(
        "change",
        [
            {"active": False},
            {"executor_id": "another-executor"},
            {"attested_report_digest": "attestation-for-a-different-report"},
        ],
    )
    async def test_release_requires_active_executor_and_exact_report_attestation(
        self, api, inventory_reader, monkeypatch, change
    ):
        original = inventory_reader.read

        async def mismatched(**kwargs):
            return replace(await original(**kwargs), **change)

        monkeypatch.setattr(inventory_reader, "read", mismatched)
        response = await self._record_and_assess(api, {})
        assert response.status_code == 403

    async def test_timeout_without_reference_cannot_disappear_from_inventory(
        self, api, inventory_reader
    ):
        inventory_reader.resources = (
            provider_inventory.AllocationResourceIdentity(
                "other", "aws", "i-unrelated", "compute"
            ),
        )
        response = await self._record_and_assess(
            api,
            {
                "other": {"presence": "absent", "queried_by": "i-unrelated"},
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["may_mark_released"] is False
        assert "idem-alpha-0001" in response.json()["unresolved_resources"]
        # Explicit B mapping and an identifying provider absence check account for
        # the timed-out handle; unrelated members must still be absent as well.
        inventory_reader.resources += (
            provider_inventory.AllocationResourceIdentity(
                "timed-out",
                "aws",
                "sky-cluster-alpha",
                "compute",
                frozenset({"idem-alpha-0001"}),
            ),
        )
        response = await self._record_and_assess(
            api,
            {
                "other": {"presence": "absent", "queried_by": "i-unrelated"},
                "timed-out": {"presence": "absent", "queried_by": "sky-cluster-alpha"},
            },
        )
        assert response.json()["may_mark_released"] is True


@pytest.mark.parametrize(
    "outcome,may_release", [("failed", True), ("ambiguous", False)]
)
async def test_refused_attempt_does_not_strand_later_absent_resources(
    api, inventory_reader, outcome, may_release
):
    headers = {"Authorization": _OWNED_CRED}
    for key in ("first-attempt", "later-success"):
        recorded = await api.post(
            _BASE, json=_owned(idempotency_key=key), headers=headers
        )
        assert recorded.status_code == 201
    first = await api.post(
        f"{_BASE}/first-attempt/conclude",
        json={
            "workspace": _WS_OWNED,
            "operation_authority": "authority-from-b",
            "outcome": outcome,
        },
        headers=headers,
    )
    assert first.status_code == 200
    later = await api.post(
        f"{_BASE}/later-success/conclude",
        json={
            "workspace": _WS_OWNED,
            "operation_authority": "authority-from-b",
            "outcome": "succeeded",
            "provider_reference": "i-created",
        },
        headers=headers,
    )
    assert later.status_code == 200
    inventory_reader.resources = (
        provider_inventory.AllocationResourceIdentity(
            "i-created", "aws", "i-created", "compute"
        ),
    )
    released = await api.post(
        f"{_BASE}/allocations/alloc-alpha/release-assessment",
        json={
            "workspace": _WS_OWNED,
            "operation_authority": "authority-from-b",
            "observations": {
                "i-created": {"presence": "absent", "queried_by": "i-created"}
            },
        },
        headers=headers,
    )
    assert released.status_code == 200
    assert released.json()["may_mark_released"] is may_release
    assert released.json()["may_return_reservation_unused"] is may_release


@pytest.mark.parametrize("outcome,may_release", [("failed", True), ("ambiguous", False), ("succeeded", False)])
async def test_complete_empty_inventory_requires_conclusive_absence(api, inventory_reader, outcome, may_release):
    headers = {"Authorization": _OWNED_CRED}
    recorded = await api.post(_BASE, json=_owned(), headers=headers)
    assert recorded.status_code == 201
    concluded = await api.post(
        f"{_BASE}/idem-alpha-0001/conclude",
        json={"workspace": _WS_OWNED, "operation_authority": "authority-from-b", "outcome": outcome},
        headers=headers,
    )
    assert concluded.status_code == 200
    inventory_reader.resources = ()
    released = await api.post(
        f"{_BASE}/allocations/alloc-alpha/release-assessment",
        json={"workspace": _WS_OWNED, "operation_authority": "authority-from-b", "observations": {}},
        headers=headers,
    )
    assert released.status_code == 200, released.text
    assert released.json()["may_mark_released"] is may_release
    assert released.json()["may_return_reservation_unused"] is may_release
