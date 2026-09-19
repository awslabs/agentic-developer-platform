"""The service that writes a run's own status and control registration (#5028 AC4).

## What this file is really testing

Every agent worker assumes the same platform IAM role, and the worker's
``DynamoDBWebhookEventsUpdate`` grant allows ``dynamodb:UpdateItem`` on the whole
webhook-events table with **no key or attribute condition**. The row key
``(event_id, arrived_at)`` is caller-supplied, so worker A can today write worker
B's ``control_address`` and ``control_token`` and redirect B's control channel at a
listener of A's choosing.

This service is what makes that grant removable, so the load-bearing tests are the
ones that show A cannot reach B's row *through the service* — not with a field, not
with a status write, not by clearing B's registration. A real emulated DynamoDB is
used rather than a stubbed client because most of the enforcement here is
``ConditionExpression`` and atomic ``ADD``, and a stub asserts only that the right
arguments were passed, not that DynamoDB would honour them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from src.agentauth.bootstrap import BootstrapStore
from src.agentauth.composition import build_authorization_service
from src.agentauth.execution import ExecutionRecord, ExecutionStatus
from src.agentauth.grants import AgentAction, AuthorityReference, DelegatedGrant, TargetRelationship
from src.agentauth.registration import (
    AgentRegistrationService,
    RegistrationRefusedError,
)
from src.agentauth.run_credential import CREDENTIAL_KEY_ENV, mint_credential
from src.agentauth.store import AgentAuthorityStore
from src.agentauth.workload import VerifiedPod

AUTHORITY_TABLE = "registration-test-authority"
EVENTS_TABLE = "registration-test-events"
TENANT = "org-tenant-001"
OTHER_TENANT = "org-tenant-002"
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
ENV = {CREDENTIAL_KEY_ENV: "a-test-credential-signing-key-that-is-long-enough"}

# Worker A and worker B: two runs of the same platform role, which is the whole
# point. A's pod UID is bound to A's execution record and B's to B's.
RUN_A, ARRIVED_A, POD_A_UID = "run-a", "2026-09-13T11:00:00Z", "pod-uid-a"
RUN_B, ARRIVED_B, POD_B_UID = "run-b", "2026-09-13T11:30:00Z", "pod-uid-b"


def pod(uid: str, ip: str = "10.0.1.5") -> VerifiedPod:
    return VerifiedPod(
        uid=uid,
        name=f"agent-{uid}",
        namespace="adp-agents",
        service_account="agent-scaledjob-sa",
        ip=ip,
    )


POD_A = pod(POD_A_UID, "10.0.1.5")
POD_B = pod(POD_B_UID, "10.0.2.9")


@pytest.fixture
def aws():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=AUTHORITY_TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
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
        yield client


def seed_execution(
    client,
    *,
    run_id,
    arrived_at,
    binding,
    tenant_id=TENANT,
    attempt=1,
    status=ExecutionStatus.ACTIVE,
    replace=False,
):
    """Write a protected execution record as trusted dispatch would.

    ``replace=True`` stands in for a *new attempt* taking over: ``put_execution``
    guards one-invocation-per-record by default, because that overwrite is how a
    fresh pod could otherwise supersede a live authorized attempt by naming its
    run. Advancing the attempt is a legitimate dispatch action, so the guard is
    lifted explicitly here rather than by weakening the store.
    """
    AgentAuthorityStore(table_name=AUTHORITY_TABLE, dynamodb_client=client).put_execution(
        expect_absent=not replace,
        record=ExecutionRecord(
            invocation_id=run_id,
            tenant_id=tenant_id,
            current_attempt=attempt,
            status=status,
            current_credential_epoch=1,
            min_acceptable_credential_epoch=1,
            workload_binding=binding,
            arrived_at=arrived_at,
        ),
    )
    grant = DelegatedGrant(
        grant_id=f"grant-{run_id}-{attempt}",
        tenant_id=tenant_id,
        principal=f"{run_id}#{attempt}",
        authority=AuthorityReference("github_event", "approval", "human", tenant_id),
        allowed_actions=frozenset({AgentAction.MONITOR}),
        target_relationships=frozenset({TargetRelationship.SELF}),
        expires_at=NOW + timedelta(days=1),
    )
    client.put_item(TableName=AUTHORITY_TABLE, Item=BootstrapStore._grant_item(grant))
    client.put_item(
        TableName=AUTHORITY_TABLE,
        Item={
            "pk": {"S": f"TENANT#{tenant_id}"},
            "sk": {"S": "AUTHORITY#approval"},
            "status": {"S": "active"},
            "human_id": {"S": "human"},
            "authority_kind": {"S": "github_event"},
        },
    )


def seed_row(client, *, run_id, arrived_at, tenant_id=TENANT, **extra):
    item = {
        "event_id": {"S": run_id},
        "arrived_at": {"S": arrived_at},
        "tenant_id": {"S": tenant_id},
        "status": {"S": "webhook_received"},
    }
    item.update(extra)
    client.put_item(TableName=EVENTS_TABLE, Item=item)


def credential(*, run_id, attempt=1, tenant_id=TENANT, now=NOW, epoch=1) -> str:
    return mint_credential(
        invocation_id=run_id,
        attempt=attempt,
        tenant_id=tenant_id,
        credential_epoch=epoch,
        now=now,
        env=ENV,
    )


@pytest.fixture
def service(aws):
    def build(*, now=NOW):
        return AgentRegistrationService(
            policy=build_authorization_service(
                authority_table=AUTHORITY_TABLE,
                events_table=EVENTS_TABLE,
                dynamodb_client=aws,
                now=lambda: now,
                env=ENV,
            ),
            authority_table=AUTHORITY_TABLE,
            events_table=EVENTS_TABLE,
            dynamodb_client=aws,
            now=lambda: now,
            env=ENV,
        )

    return build


@pytest.fixture
def two_workers(aws):
    """A and B both dispatched, both bound, both with a real events row."""
    seed_execution(aws, run_id=RUN_A, arrived_at=ARRIVED_A, binding=POD_A_UID)
    seed_execution(aws, run_id=RUN_B, arrived_at=ARRIVED_B, binding=POD_B_UID)
    seed_row(aws, run_id=RUN_A, arrived_at=ARRIVED_A)
    seed_row(aws, run_id=RUN_B, arrived_at=ARRIVED_B)


def row(client, *, run_id, arrived_at) -> dict:
    return client.get_item(
        TableName=EVENTS_TABLE,
        Key={"event_id": {"S": run_id}, "arrived_at": {"S": arrived_at}},
        ConsistentRead=True,
    ).get("Item", {})


class TestAWriteLandsOnTheCallersOwnRow:
    def test_status_is_written_to_the_row_the_protected_record_names(self, aws, service, two_workers):
        service().record_status(
            credential_token=credential(run_id=RUN_A),
            pod=POD_A,
            status="in_progress",
            fields={"run_id": "keda-job-1"},
        )

        written = row(aws, run_id=RUN_A, arrived_at=ARRIVED_A)
        assert written["status"]["S"] == "in_progress"
        assert written["run_id"]["S"] == "keda-job-1"
        # B is untouched: nothing in the request named a row.
        assert row(aws, run_id=RUN_B, arrived_at=ARRIVED_B)["status"]["S"] == "webhook_received"

    def test_registration_uses_the_verified_pod_ip_as_the_destination(self, aws, service, two_workers):
        registration = service().register_control(
            credential_token=credential(run_id=RUN_A),
            pod=POD_A,
            token="t" * 40,
            token_expires_at="2026-09-13T13:00:00Z",
        )

        written = row(aws, run_id=RUN_A, arrived_at=ARRIVED_A)
        assert registration.address == POD_A.ip
        assert written["control_address"]["S"] == POD_A.ip
        assert written["control_generation"]["N"] == "1"

    def test_a_run_with_no_events_row_is_refused_rather_than_created(self, aws, service):
        # ``attribute_exists`` — an orphan create would invent a run that no
        # webhook ever produced, and Activity would show it as real.
        seed_execution(aws, run_id=RUN_A, arrived_at=ARRIVED_A, binding=POD_A_UID)

        with pytest.raises(RegistrationRefusedError):
            service().record_status(credential_token=credential(run_id=RUN_A), pod=POD_A, status="in_progress")

        assert row(aws, run_id=RUN_A, arrived_at=ARRIVED_A) == {}


class TestWorkerACannotReachWorkerB:
    """The AC4 claim, stated as tests. Shared platform IAM is a given here."""

    def test_as_credential_cannot_write_bs_row(self, aws, service, two_workers):
        # A presents its own valid credential and its own verified pod. There is
        # no argument it can vary to reach B — the key comes from B's protected
        # record, which A cannot write.
        service().record_status(credential_token=credential(run_id=RUN_A), pod=POD_A, status="complete")

        assert row(aws, run_id=RUN_B, arrived_at=ARRIVED_B)["status"]["S"] == "webhook_received"

    def test_a_cannot_present_bs_credential_from_its_own_pod(self, aws, service, two_workers):
        # The leaked-credential case. B's credential verifies cryptographically,
        # but the binding comparison is against the TokenReview-verified pod UID.
        with pytest.raises(RegistrationRefusedError):
            service().record_status(credential_token=credential(run_id=RUN_B), pod=POD_A, status="complete")

        assert row(aws, run_id=RUN_B, arrived_at=ARRIVED_B)["status"]["S"] == "webhook_received"

    def test_a_cannot_redirect_bs_control_channel(self, aws, service, two_workers):
        # The concrete exploit the unconditioned grant allows today: point B's
        # control_address at a listener A controls, then receive B's commands.
        service().register_control(
            credential_token=credential(run_id=RUN_B),
            pod=POD_B,
            token="b" * 40,
            token_expires_at="2026-09-13T13:00:00Z",
        )

        with pytest.raises(RegistrationRefusedError):
            service().register_control(
                credential_token=credential(run_id=RUN_B),
                pod=POD_A,  # A's pod, B's credential
                token="a" * 40,
                token_expires_at="2026-09-13T13:00:00Z",
            )

        assert row(aws, run_id=RUN_B, arrived_at=ARRIVED_B)["control_address"]["S"] == POD_B.ip

    def test_a_cannot_clear_bs_registration(self, aws, service, two_workers):
        # Clearing B's registration is a denial of control over B.
        service().register_control(
            credential_token=credential(run_id=RUN_B),
            pod=POD_B,
            token="b" * 40,
            token_expires_at="2026-09-13T13:00:00Z",
        )

        with pytest.raises(RegistrationRefusedError):
            service().clear_control(credential_token=credential(run_id=RUN_B), pod=POD_A, generation=1)

        assert "control_address" in row(aws, run_id=RUN_B, arrived_at=ARRIVED_B)

    def test_a_credential_for_another_tenant_is_refused(self, aws, service, two_workers):
        with pytest.raises(RegistrationRefusedError):
            service().record_status(credential_token=credential(run_id=RUN_A, tenant_id=OTHER_TENANT), pod=POD_A, status="complete")

        assert row(aws, run_id=RUN_A, arrived_at=ARRIVED_A)["status"]["S"] == "webhook_received"


class TestProtectedFieldsAreRejectedNotDropped:
    @pytest.mark.parametrize(
        "field",
        [
            "event_id",
            "arrived_at",
            "tenant_id",
            "owner",
            "user_id",
            "parent_invocation_id",
            "parent_principal",
            "root_human_id",
            "is_human_rooted",
            "persona",
            "control_address",
            "control_token",
            "control_generation",
        ],
    )
    def test_an_authority_claim_is_refused(self, aws, service, two_workers, field):
        # Refused rather than silently dropped: a caller that believes it set
        # ``owner`` and gets a 200 has been told its escalation worked, and the
        # attempt leaves no signal. Lineage and ownership are authorization
        # inputs, so an attempt to write one is an incident, not a typo.
        with pytest.raises(RegistrationRefusedError, match="unsupported field"):
            service().record_status(
                credential_token=credential(run_id=RUN_A),
                pod=POD_A,
                status="in_progress",
                fields={field: "attacker-supplied"},
            )

        assert "owner" not in row(aws, run_id=RUN_A, arrived_at=ARRIVED_A)

    def test_an_unknown_field_is_refused(self, aws, service, two_workers):
        with pytest.raises(RegistrationRefusedError):
            service().record_status(credential_token=credential(run_id=RUN_A), pod=POD_A, status="in_progress", fields={"whatever": "x"})

    @pytest.mark.parametrize("status", ["", "webhook_received", "cancelled", "APPROVED", "in_progress; DROP"])
    def test_an_unallowlisted_status_is_refused(self, aws, service, two_workers, status):
        # ``webhook_received`` is the ingress Lambda's to write, and ``cancelled``
        # is a human/orchestration decision. A worker asserting either would be
        # claiming a transition it has no authority to make.
        with pytest.raises(RegistrationRefusedError):
            service().record_status(credential_token=credential(run_id=RUN_A), pod=POD_A, status=status)

        assert row(aws, run_id=RUN_A, arrived_at=ARRIVED_A)["status"]["S"] == "webhook_received"


class TestLivenessIsCheckedNotJustTheSignature:
    def test_a_superseded_attempt_is_refused(self, aws, service):
        # Attempt 1's pod may still be running with an unexpired credential. That
        # is exactly the case this catches: the store says attempt 2 is current.
        seed_execution(aws, run_id=RUN_A, arrived_at=ARRIVED_A, binding=POD_A_UID, attempt=2)
        seed_row(aws, run_id=RUN_A, arrived_at=ARRIVED_A)

        with pytest.raises(RegistrationRefusedError):
            service().record_status(credential_token=credential(run_id=RUN_A, attempt=1), pod=POD_A, status="complete")

    @pytest.mark.parametrize("status", [ExecutionStatus.CANCELLED, ExecutionStatus.REVOKED, ExecutionStatus.COMPLETED, ExecutionStatus.PENDING])
    def test_a_non_active_execution_is_refused(self, aws, service, status):
        # Revocation is an event in the store, not a property of the token.
        seed_execution(aws, run_id=RUN_A, arrived_at=ARRIVED_A, binding=POD_A_UID, status=status)
        seed_row(aws, run_id=RUN_A, arrived_at=ARRIVED_A)

        with pytest.raises(RegistrationRefusedError):
            service().record_status(credential_token=credential(run_id=RUN_A), pod=POD_A, status="complete")

    def test_an_execution_the_store_never_heard_of_is_refused(self, aws, service):
        # Fail closed on absence: a missing record means either the credential
        # names a run that was never dispatched, or the store is unavailable.
        seed_row(aws, run_id=RUN_A, arrived_at=ARRIVED_A)

        with pytest.raises(RegistrationRefusedError):
            service().record_status(credential_token=credential(run_id=RUN_A), pod=POD_A, status="complete")

    def test_an_expired_credential_is_refused(self, aws, service, two_workers):
        stale = credential(run_id=RUN_A, now=NOW - timedelta(hours=6))

        with pytest.raises(RegistrationRefusedError):
            service().record_status(credential_token=stale, pod=POD_A, status="complete")

    @pytest.mark.parametrize("token", ["", "not-a-credential", "adpr1.tampered.mac"])
    def test_a_malformed_credential_is_refused(self, aws, service, two_workers, token):
        with pytest.raises(RegistrationRefusedError):
            service().record_status(credential_token=token, pod=POD_A, status="complete")

    def test_an_unbound_execution_is_refused(self, aws, service):
        # Without a workload binding there is nothing to compare the presenting
        # pod against, so the credential alone would be sufficient — which is the
        # leaked-credential hole. Refuse instead.
        seed_execution(aws, run_id=RUN_A, arrived_at=ARRIVED_A, binding=None)
        seed_row(aws, run_id=RUN_A, arrived_at=ARRIVED_A)

        with pytest.raises(RegistrationRefusedError):
            service().record_status(credential_token=credential(run_id=RUN_A), pod=POD_A, status="complete")

    def test_an_execution_with_no_arrived_at_is_refused(self, aws, service):
        # No authoritative sort key means no row this service may claim to own.
        seed_execution(aws, run_id=RUN_A, arrived_at=None, binding=POD_A_UID)
        seed_row(aws, run_id=RUN_A, arrived_at=ARRIVED_A)

        with pytest.raises(RegistrationRefusedError):
            service().record_status(credential_token=credential(run_id=RUN_A), pod=POD_A, status="complete")


class TestRegistrationGenerationIsIdempotentPerAttempt:
    """A retried registration must not advance the generation.

    The generation is assigned by an atomic ``ADD``, which is what makes it
    actually change between attempts. But it means a *lost response* is dangerous:
    the pod retries, the counter advances again, and the listener — already running
    and holding the first generation — then refuses every command the gateway sends
    with the second. That is indistinguishable from an attack and needs no attacker.
    """

    def test_a_retry_with_the_same_token_recovers_the_same_generation(self, aws, service, two_workers):
        first = service().register_control(
            credential_token=credential(run_id=RUN_A),
            pod=POD_A,
            token="t" * 40,
            token_expires_at="2026-09-13T13:00:00Z",
        )

        second = service().register_control(
            credential_token=credential(run_id=RUN_A),
            pod=POD_A,
            token="t" * 40,
            token_expires_at="2026-09-13T13:00:00Z",
        )

        assert (second.generation, second.address) == (first.generation, first.address)
        assert row(aws, run_id=RUN_A, arrived_at=ARRIVED_A)["control_generation"]["N"] == str(first.generation)

    def test_repeated_retries_never_advance_the_generation(self, aws, service, two_workers):
        generations = {
            service()
            .register_control(
                credential_token=credential(run_id=RUN_A),
                pod=POD_A,
                token="t" * 40,
                token_expires_at="2026-09-13T13:00:00Z",
            )
            .generation
            for _ in range(5)
        }

        assert generations == {1}

    def test_a_different_token_on_the_same_attempt_is_refused(self, aws, service, two_workers):
        # Not idempotent recovery — a second listener identity for one attempt.
        # Admitting it would leave two tokens believing they are current.
        service().register_control(
            credential_token=credential(run_id=RUN_A),
            pod=POD_A,
            token="t" * 40,
            token_expires_at="2026-09-13T13:00:00Z",
        )

        with pytest.raises(RegistrationRefusedError, match="control already registered"):
            service().register_control(
                credential_token=credential(run_id=RUN_A),
                pod=POD_A,
                token="different-token-that-is-long-enough-here",
                token_expires_at="2026-09-13T13:00:00Z",
            )

    def test_the_ledger_never_stores_the_token_itself(self, aws, service, two_workers):
        service().register_control(
            credential_token=credential(run_id=RUN_A),
            pod=POD_A,
            token="t" * 40,
            token_expires_at="2026-09-13T13:00:00Z",
        )

        ledger = aws.query(
            TableName=AUTHORITY_TABLE,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :sk)",
            ExpressionAttributeValues={":pk": {"S": f"TENANT#{TENANT}"}, ":sk": {"S": "REG#"}},
            ConsistentRead=True,
        )["Items"]

        assert len(ledger) == 1
        assert "t" * 40 not in repr(ledger)

    def test_a_new_attempt_does_advance_the_generation(self, aws, service, two_workers):
        # The counter must still change across attempts, or the listener's
        # generation check compares a constant against itself forever.
        service().register_control(
            credential_token=credential(run_id=RUN_A),
            pod=POD_A,
            token="t" * 40,
            token_expires_at="2026-09-13T13:00:00Z",
        )
        # Attempt 2, a fresh pod with its own binding.
        seed_execution(aws, run_id=RUN_A, arrived_at=ARRIVED_A, binding="pod-uid-a2", attempt=2, replace=True)

        second = service().register_control(
            credential_token=credential(run_id=RUN_A, attempt=2),
            pod=pod("pod-uid-a2", "10.0.3.3"),
            token="u" * 40,
            token_expires_at="2026-09-13T13:00:00Z",
        )

        assert second.generation == 2


class TestClearingIsScopedToTheLiveGeneration:
    def test_the_control_fields_are_actually_removed(self, aws, service, two_workers):
        # The token is a credential and pod IPs are reused, so cleanup removes
        # the fields rather than relying on the terminal status alone.
        registration = service().register_control(
            credential_token=credential(run_id=RUN_A),
            pod=POD_A,
            token="t" * 40,
            token_expires_at="2026-09-13T13:00:00Z",
        )

        service().clear_control(credential_token=credential(run_id=RUN_A), pod=POD_A, generation=registration.generation)

        written = row(aws, run_id=RUN_A, arrived_at=ARRIVED_A)
        for attribute in ("control_address", "control_token", "control_token_expires_at", "control_port"):
            assert attribute not in written

    def test_a_stale_generation_cannot_strip_a_live_listener(self, aws, service, two_workers):
        # Attempt 1 tearing down late must not clear attempt 2's registration:
        # that would silently remove control from a run that is still going.
        service().register_control(
            credential_token=credential(run_id=RUN_A),
            pod=POD_A,
            token="t" * 40,
            token_expires_at="2026-09-13T13:00:00Z",
        )
        seed_execution(aws, run_id=RUN_A, arrived_at=ARRIVED_A, binding="pod-uid-a2", attempt=2, replace=True)
        service().register_control(
            credential_token=credential(run_id=RUN_A, attempt=2),
            pod=pod("pod-uid-a2", "10.0.3.3"),
            token="u" * 40,
            token_expires_at="2026-09-13T13:00:00Z",
        )

        with pytest.raises(RegistrationRefusedError):
            service().clear_control(credential_token=credential(run_id=RUN_A, attempt=2), pod=POD_A, generation=1)

        assert row(aws, run_id=RUN_A, arrived_at=ARRIVED_A)["control_address"]["S"] == "10.0.3.3"


class TestNothingSensitiveIsLogged:
    def test_no_credential_or_control_token_reaches_the_logs(self, aws, service, two_workers, caplog):
        """Scoped to this stack's own loggers, deliberately.

        ``botocore`` logs the full serialized request body at DEBUG, so at that
        level the control token appears in *its* records on the way to DynamoDB —
        it is a request field, and that is equally true of the pre-existing worker
        write path this replaces. Suppressing a third-party library's debug
        logging is a separate, platform-wide concern (the gateway does not run at
        DEBUG). What this module is responsible for, and what is asserted, is that
        no code in ``src.agentauth`` puts a credential or a token into a log record.
        """
        caplog.set_level("DEBUG")
        token = credential(run_id=RUN_A)

        service().record_status(credential_token=token, pod=POD_A, status="in_progress")
        service().register_control(credential_token=token, pod=POD_A, token="s" * 40, token_expires_at="2026-09-13T13:00:00Z")
        with pytest.raises(RegistrationRefusedError):
            service().record_status(credential_token=token, pod=POD_A, status="in_progress", fields={"owner": "attacker"})

        ours = [r for r in caplog.records if r.name.startswith("bedrockgateway")]
        assert ours, "expected this stack to have logged something"
        logged = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in ours)
        assert token not in logged
        assert "s" * 40 not in logged
        # A rejected field's *value* may be the identity a caller was claiming.
        assert "attacker" not in logged

    def test_a_rejected_field_is_logged_by_name_so_it_is_investigable(self, aws, service, two_workers, caplog):
        caplog.set_level("WARNING")

        with pytest.raises(RegistrationRefusedError):
            service().record_status(credential_token=credential(run_id=RUN_A), pod=POD_A, status="in_progress", fields={"owner": "x"})

        assert any("owner" in str(record.__dict__.get("fields", "")) for record in caplog.records)


class TestTranscriptPointerIsolation:
    @pytest.mark.parametrize("different", ["tenant", "run", "attempt", "kind", "suffix"])
    def test_an_own_row_cannot_point_to_another_runs_artifact(self, aws, service, two_workers, different):
        from types import SimpleNamespace

        from src.agentauth.artifact_keys import artifact_prefix

        identity = SimpleNamespace(tenant_id=TENANT, invocation_id=RUN_A, current_attempt=1)
        if different == "tenant":
            identity.tenant_id = OTHER_TENANT
        if different == "run":
            identity.invocation_id = RUN_B
        if different == "attempt":
            identity.current_attempt = 2
        kind = "spill" if different == "kind" else "transcript"
        key = artifact_prefix(identity) + kind + "/" + "a" * 64 + ".md"
        if different == "suffix":
            key += "/../victim"
        with pytest.raises(RegistrationRefusedError, match="unsupported transcript"):
            service().record_status(credential_token=credential(run_id=RUN_A), pod=POD_A, status="in_progress", fields={"transcript_key": key})
        assert "transcript_key" not in row(aws, run_id=RUN_A, arrived_at=ARRIVED_A)

    def test_own_transcript_pointer_is_preserved(self, aws, service, two_workers):
        from types import SimpleNamespace

        from src.agentauth.artifact_keys import artifact_prefix

        identity = SimpleNamespace(tenant_id=TENANT, invocation_id=RUN_A, current_attempt=1)
        key = artifact_prefix(identity) + "transcript/" + "a" * 64 + ".md"
        service().record_status(credential_token=credential(run_id=RUN_A), pod=POD_A, status="in_progress", fields={"transcript_key": key})
        assert row(aws, run_id=RUN_A, arrived_at=ARRIVED_A)["transcript_key"]["S"] == key
