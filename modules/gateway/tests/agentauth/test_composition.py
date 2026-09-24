"""The composition root that assembles the agent authorization stack (#5028).

Two things are worth asserting here and they are easy to conflate:

- the stack **can be built** from real classes at all (until this module existed,
  the only thing that ever injected the policy's collaborators was a test fake);
- the state reader is a **projection**, not a pass-through. That is the check that
  matters, because the row it reads carries ``control_token`` and a coordinator
  able to read one could command the pod directly and bypass the policy.

Both readers reach their row by its **exact key**, and the sort key comes from the
protected authority table via an injected :class:`ExecutionLocator`. The tests
therefore inject one; a reader without a locator refuses, which is asserted below
rather than worked around, because the alternative it replaced — query ``event_id``
for the newest row — let a second row under one ``event_id`` decide what an
authorized coordinator is told.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from src.agentauth.adapter import AgentControlAdapter
from src.agentauth.composition import (
    AgentRunStateReader,
    ControlGenerationReader,
    StoreExecutionLocator,
    build_authorization_service,
    build_control_adapter,
)
from src.agentauth.execution import ExecutionRecord, ExecutionStatus
from src.agentauth.policy import AgentAuthorizationService
from src.agentauth.store import AgentAuthorityStore

EVENTS_TABLE = "composition-test-events"
AUTHORITY_TABLE = "composition-test-authority"
TENANT = "org-tenant-001"
ARRIVED_AT = "2026-09-13T12:00:00Z"
# Between the row's ``arrived_at`` and its default token expiry, so the fixture
# row is a *live* registration unless a test says otherwise.
NOW = datetime(2026, 9, 13, 12, 10, tzinfo=UTC)


@pytest.fixture
def events():
    """A real (emulated) webhook-events table with the control attributes."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=EVENTS_TABLE,
            KeySchema=[
                {"AttributeName": "event_id", "KeyType": "HASH"},
                {"AttributeName": "arrived_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "arrived_at", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield resource


def put_row(resource, **overrides):
    row = {
        "event_id": "run-a",
        "arrived_at": ARRIVED_AT,
        "tenant_id": TENANT,
        "status": "in_progress",
        "control_address": "10.0.1.5",
        "control_port": 8770,
        "control_token": "a-registered-control-token",
        "control_token_expires_at": "2026-09-13T13:00:00Z",
        "control_generation": 3,
        "updated_at": "2026-09-13T12:05:00Z",
    }
    row.update(overrides)
    resource.Table(EVENTS_TABLE).put_item(Item=row)
    return row


class StubLocator:
    """Stands in for the protected authority table's ``(tenant, arrived_at)``.

    A stub rather than a seeded second moto table because what these tests are
    about is the *reader*: that it uses the key it is given and verifies the row it
    gets back. ``StoreExecutionLocator`` — which derives that key from the
    protected record and the trusted dispatch pointer — is exercised against a real
    emulated authority table in :class:`TestStoreExecutionLocator`.
    """

    def __init__(self, *, tenant_id: str = TENANT, arrived_at: str | None = ARRIVED_AT) -> None:
        self._tenant_id = tenant_id
        self._arrived_at = arrived_at
        self.calls: list[tuple[str, str | None]] = []

    def locate(self, *, run_id: str, tenant_id: str | None = None) -> tuple[str, str] | None:
        self.calls.append((run_id, tenant_id))
        if self._arrived_at is None:
            return None
        return self._tenant_id, self._arrived_at


def state_reader(events, *, locator=None, table_name=EVENTS_TABLE, now=None):
    return AgentRunStateReader(
        table_name=table_name,
        dynamodb_resource=events,
        executions=locator if locator is not None else StubLocator(),
        now=now or (lambda: NOW),
    )


def generation_reader(events, *, locator=None, table_name=EVENTS_TABLE):
    return ControlGenerationReader(
        table_name=table_name,
        dynamodb_resource=events,
        executions=locator if locator is not None else StubLocator(),
        now=lambda: NOW,
    )


class TestStateReaderProjects:
    """The reader must not hand a delegated caller anything commandable."""

    def test_control_token_is_never_in_the_projection(self, events):
        put_row(events)
        reader = state_reader(events)

        view = reader.read_state(run_id="run-a", generation=3)

        # The public dict is what reaches the CLI. A token, address or port in it
        # would let a coordinator bypass the gateway and command the pod directly.
        serialized = view.to_public_dict()
        flattened = repr(serialized)
        assert "a-registered-control-token" not in flattened
        assert "10.0.1.5" not in flattened
        assert not hasattr(view, "token")
        assert set(serialized) == {
            "run_id",
            "generation",
            "state",
            "available",
            "reason",
            "capabilities",
            "updated_at",
            "authority_reference_id",
        }

    def test_registered_run_is_available(self, events):
        put_row(events)
        reader = state_reader(events)

        view = reader.read_state(run_id="run-a", generation=3)

        assert (view.available, view.state, view.generation) == (True, "in_progress", 3)

    def test_unregistered_run_is_unavailable_not_missing(self, events):
        put_row(events, control_address=None, control_token=None)
        reader = state_reader(events)

        view = reader.read_state(run_id="run-a", generation=3)

        assert view.available is False
        assert view.reason == "run has no live control registration"

    def test_advanced_generation_is_reported_not_silently_answered(self, events):
        # Answering about a different attempt of the same run is how a coordinator
        # acts on state belonging to a pod that no longer exists.
        put_row(events, control_generation=5)
        reader = state_reader(events)

        view = reader.read_state(run_id="run-a", generation=3)

        assert (view.available, view.reason) == (False, "run generation has advanced")
        assert view.generation == 5

    def test_capabilities_stay_empty_even_for_supported_verbs(self, events):
        """Not a stale assertion: it is stronger now that verbs ARE supported.

        This read used to be empty for the trivial reason that
        ``SUPPORTED_AGENT_ACTIONS`` held MONITOR alone. PAUSE/RESUME (#5222) and
        ABORT (#3963) have since shipped, so an implementation that derived this
        map from deployment support would now advertise three verbs — for a run
        this reader has never contacted. It must not: deployment support is one of
        three inputs, and the other two (the pod's own claim and its availability)
        are not visible here.
        """
        from src.agentauth.policy import SUPPORTED_AGENT_ACTIONS

        # Guard against this test quietly reverting to the trivial case.
        assert len(SUPPORTED_AGENT_ACTIONS) > 1, "the deployment supports more than MONITOR; this test covers that case"

        put_row(events)
        reader = state_reader(events)

        assert reader.read_state(run_id="run-a", generation=3).capabilities == {}

    def test_missing_run_reads_as_none(self, events):
        reader = state_reader(events)

        assert reader.read_state(run_id="nope", generation=0) is None

    def test_empty_run_id_reads_as_none(self, events):
        reader = state_reader(events)

        assert reader.read_state(run_id="", generation=0) is None

    def test_missing_table_reads_as_none_rather_than_raising(self, events):
        # A deploy-order gap must be indistinguishable from a run with no control
        # state, and must not surface as a 500.
        reader = state_reader(events, table_name="no-such-table")

        assert reader.read_state(run_id="run-a", generation=0) is None


class TestGenerationReader:
    def test_reads_the_registered_generation(self, events):
        put_row(events)
        reader = generation_reader(events)

        assert reader.read_generation(run_id="run-a", tenant_id=TENANT) == 3

    def test_refuses_a_row_from_another_tenant(self, events):
        # This is not the protected table, so its rows are not trusted to be in the
        # tenant the caller asked about.
        put_row(events, tenant_id="org-tenant-002")
        reader = generation_reader(events)

        assert reader.read_generation(run_id="run-a", tenant_id=TENANT) is None

    def test_unregistered_generation_is_none(self, events):
        put_row(events, control_generation=None)
        reader = generation_reader(events)

        assert reader.read_generation(run_id="run-a", tenant_id=TENANT) is None

    def test_missing_table_is_none(self, events):
        reader = generation_reader(events, table_name="no-such-table")

        assert reader.read_generation(run_id="run-a", tenant_id=TENANT) is None

    def test_a_decimal_generation_is_read_as_an_int(self, events):
        # Regression: DynamoDB's resource API returns Decimal, so an isinstance
        # check against (int, float) rejects every real row and reports 0. That
        # matches no registered generation, so the binding would have been inert
        # in production while every unit test using plain ints still passed.
        put_row(events, control_generation=Decimal("4"))
        reader = generation_reader(events)

        generation = reader.read_generation(run_id="run-a", tenant_id=TENANT)

        assert generation == 4
        assert type(generation) is int

    def test_a_non_numeric_generation_is_none(self, events):
        put_row(events, control_generation="not-a-number")
        reader = generation_reader(events)

        assert reader.read_generation(run_id="run-a", tenant_id=TENANT) is None

    def test_a_boolean_generation_is_none_not_one(self, events):
        # True is an int in Python, so a naive coercion turns it into generation 1
        # — a plausible-looking value that could match a real first attempt.
        put_row(events, control_generation=True)
        reader = generation_reader(events)

        assert reader.read_generation(run_id="run-a", tenant_id=TENANT) is None


class TestReadsTheExactAuthoritativeRow:
    """The row is chosen by the protected key, not by recency.

    The behaviour these tests pin is the fix for a real hole: an earlier revision
    queried ``event_id`` with ``Limit=1, ScanIndexForward=False`` and answered from
    the newest row. The events table's sort key is ``arrived_at``, and until the
    worker's unrestricted write grant is removed a worker can put a *second* row
    under its own ``event_id``. That second row would then decide what an authorized
    coordinator is told about the run — including making a finished run look live.
    """

    def test_a_newer_decoy_row_does_not_shadow_the_authoritative_one(self, events):
        put_row(events)
        # Sorts after the real row, so a newest-row query returns this one.
        put_row(
            events,
            arrived_at="2026-09-13T23:59:59Z",
            status="in_progress",
            control_address="10.9.9.9",
            control_token="an-attacker-planted-token",
            control_generation=3,
            updated_at="2026-09-13T23:59:59Z",
        )
        reader = state_reader(events)

        view = reader.read_state(run_id="run-a", generation=3)

        # The locator names the 12:00:00Z row, so that is the row that answers.
        # ``updated_at`` distinguishes them: the decoy carries its own, so this
        # assertion fails if the reader ever returns to taking the newest row.
        assert view.updated_at == "2026-09-13T12:05:00Z"

    def test_a_decoy_row_cannot_forge_the_control_generation(self, events):
        put_row(events, control_generation=3)
        put_row(events, arrived_at="2026-09-13T23:59:59Z", control_generation=99)
        reader = generation_reader(events)

        # 99 would bind an envelope to a generation the live listener never had.
        assert reader.read_generation(run_id="run-a", tenant_id=TENANT) == 3

    def test_a_row_in_another_tenant_is_refused(self, events):
        # This is not the protected table, so the row's own tenant_id is not
        # trusted to match the protected record's.
        put_row(events, tenant_id="org-tenant-002")
        reader = state_reader(events)

        assert reader.read_state(run_id="run-a", generation=3) is None

    def test_a_run_with_no_protected_arrived_at_refuses(self, events):
        # No authoritative sort key means there is no row this reader may claim is
        # "the" row — it must refuse rather than degrade to a newest-row query.
        put_row(events)
        reader = state_reader(events, locator=StubLocator(arrived_at=None))

        assert reader.read_state(run_id="run-a", generation=3) is None

    def test_a_reader_with_no_locator_refuses(self, events):
        put_row(events)
        reader = AgentRunStateReader(table_name=EVENTS_TABLE, dynamodb_resource=events, now=lambda: NOW)

        assert reader.read_state(run_id="run-a", generation=3) is None

    def test_a_locator_failure_refuses_rather_than_raising(self, events):
        class Broken:
            def locate(self, *, run_id, tenant_id=None):
                raise RuntimeError("authority table unavailable")

        put_row(events)
        reader = state_reader(events, locator=Broken())

        assert reader.read_state(run_id="run-a", generation=3) is None

    def test_the_generation_reader_passes_its_caller_scoped_tenant_through(self, events):
        # The resolver already has a caller-scoped tenant; the status path does not.
        # Dropping it here would have the locator resolve the tenant from the
        # dispatch pointer instead of honouring the one already authorized.
        put_row(events)
        locator = StubLocator()
        generation_reader(events, locator=locator).read_generation(run_id="run-a", tenant_id=TENANT)

        assert locator.calls == [("run-a", TENANT)]


class TestLivenessOfTheRegistration:
    """``available`` means a command could actually be delivered."""

    def test_an_expired_registration_is_not_available(self, events):
        # The pod outlived its own control credential. The address is still in the
        # row because terminal cleanup is best-effort.
        put_row(events, control_token_expires_at="2026-09-13T12:05:00Z")
        reader = state_reader(events)

        view = reader.read_state(run_id="run-a", generation=3)

        assert (view.available, view.reason) == (False, "control registration has expired")

    def test_a_registration_with_no_expiry_is_not_available(self, events):
        # A deadline that cannot be read is not an enforceable deadline.
        put_row(events, control_token_expires_at=None)
        reader = state_reader(events)

        assert reader.read_state(run_id="run-a", generation=3).available is False

    def test_an_unparseable_expiry_is_treated_as_expired(self, events):
        put_row(events, control_token_expires_at="whenever")
        reader = state_reader(events)

        assert reader.read_state(run_id="run-a", generation=3).available is False

    @pytest.mark.parametrize("status", ["complete", "failed", "skipped", "budget_stopped", "cancelled"])
    def test_a_terminal_run_is_not_available_even_with_a_live_registration(self, events, status):
        # Pod IPs are reused. Reporting a finished run as available would have a
        # coordinator plan work against whatever pod now holds that address.
        put_row(events, status=status)
        reader = state_reader(events)

        view = reader.read_state(run_id="run-a", generation=3)

        assert (view.available, view.reason) == (False, "run has reached a terminal status")


class TestStoreExecutionLocator:
    """Derives the key from protected state, never from the events row."""

    @pytest.fixture
    def authority(self, events):
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=AUTHORITY_TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        return resource

    def locator(self):
        return StoreExecutionLocator(
            store=AgentAuthorityStore(
                table_name=AUTHORITY_TABLE,
                dynamodb_client=boto3.client("dynamodb", region_name="us-east-1"),
            ),
            dynamodb_client=boto3.client("dynamodb", region_name="us-east-1"),
        )

    def seed(self, *, arrived_at=ARRIVED_AT, tenant_id=TENANT, pointer=True):
        client = boto3.client("dynamodb", region_name="us-east-1")
        store = AgentAuthorityStore(table_name=AUTHORITY_TABLE, dynamodb_client=client)
        store.put_execution(
            record=ExecutionRecord(
                invocation_id="run-a",
                tenant_id=tenant_id,
                current_attempt=1,
                status=ExecutionStatus.ACTIVE,
                current_credential_epoch=1,
                min_acceptable_credential_epoch=1,
                arrived_at=arrived_at,
            )
        )
        if pointer:
            client.put_item(
                TableName=AUTHORITY_TABLE,
                Item={
                    "pk": {"S": "INVOCATION#run-a"},
                    "sk": {"S": "DISPATCH"},
                    "tenant_id": {"S": tenant_id},
                },
            )

    def test_resolves_the_tenant_from_the_protected_dispatch_pointer(self, authority):
        # The status path arrives with a run ID alone. The tenant must come from
        # protected state, not from the events row — the events row's tenant_id is
        # worker-writable today, so trusting it would let a run relabel which
        # tenant's state it appears to be.
        self.seed()

        assert self.locator().locate(run_id="run-a") == (TENANT, ARRIVED_AT)

    def test_honours_a_caller_scoped_tenant_without_the_pointer(self, authority):
        self.seed(pointer=False)

        assert self.locator().locate(run_id="run-a", tenant_id=TENANT) == (TENANT, ARRIVED_AT)

    def test_a_run_in_another_tenant_does_not_resolve(self, authority):
        self.seed()

        assert self.locator().locate(run_id="run-a", tenant_id="org-tenant-002") is None

    def test_an_unknown_run_does_not_resolve(self, authority):
        assert self.locator().locate(run_id="run-b") is None

    def test_a_record_without_arrived_at_does_not_resolve(self, authority):
        # Records written before dispatch captured the sort key. They must refuse,
        # not fall back to guessing a row.
        self.seed(arrived_at=None)

        assert self.locator().locate(run_id="run-a") is None

    def test_arrived_at_survives_a_store_round_trip(self, authority):
        # The field is only useful if the store actually persists and parses it.
        self.seed()
        store = AgentAuthorityStore(
            table_name=AUTHORITY_TABLE,
            dynamodb_client=boto3.client("dynamodb", region_name="us-east-1"),
        )

        record = store.load_execution(invocation_id="run-a", tenant_id=TENANT)

        assert record.arrived_at == ARRIVED_AT


class TestAssembly:
    """The stack builds from real classes — not fakes — without touching AWS."""

    def test_authorization_service_is_constructible(self, events):
        service = build_authorization_service(
            authority_table="composition-test-authority",
            events_table=EVENTS_TABLE,
            dynamodb_resource=events,
        )

        assert isinstance(service, AgentAuthorizationService)

    def test_control_adapter_is_constructible(self, events):
        adapter = build_control_adapter(
            authority_table="composition-test-authority",
            events_table=EVENTS_TABLE,
            dynamodb_resource=events,
        )

        assert isinstance(adapter, AgentControlAdapter)

    def test_one_store_backs_both_grant_and_execution_protocols(self, events):
        # Two clients would be two connection pools reading one table for no
        # benefit, and two places for the table name to be configured.
        service = build_authorization_service(
            authority_table="composition-test-authority",
            events_table=EVENTS_TABLE,
            dynamodb_resource=events,
        )

        assert service._grants is service._executions
