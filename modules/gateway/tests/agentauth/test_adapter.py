"""The agent-facing adapter (#5028 AC1, AC2, AC3, AC4, AC5, AC7).

The adapter is where the three separate mechanisms meet, so the tests here are
mostly about the *seams* rather than about any one mechanism:

- what the caller is allowed to influence (a run ID, a body) versus what it is
  not (its own identity, the target's generation, the authority reference);
- that the 501 ladder survives being wrapped in authorization;
- that an envelope, when one is minted, binds to the resolved facts and to the
  exact bytes that will be forwarded — the property
  ``modules/agent-factory/agent/src/control-envelope.ts`` verifies at the far end.

``TestSupportedVerbPath`` used to reach the signing path by patching
``SUPPORTED_AGENT_ACTIONS``, which was the only way to cover it while no
live-control verb was supported. PAUSE has since shipped in the real set (and
ABORT with #3963), so the patch is gone: the signing tests now run against the
deployed constant, which is what makes them evidence about this deployment
rather than about a configuration no environment has.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.agentauth import policy as policy_module
from src.agentauth.adapter import (
    CREDENTIAL_HEADER,
    ENVELOPE_HEADER,
    INVALID_BODY_STATUS,
    MAX_COMMAND_BODY_BYTES,
    AgentControlAdapter,
    AgentStatusView,
)
from src.agentauth.envelope import (
    SIGNING_KEY_ENV,
    SIGNING_KEY_ID_ENV,
    EnvelopeError,
    body_digest,
    verify_envelope,
)
from src.agentauth.execution import ExecutionRecord, ExecutionStatus
from src.agentauth.grants import (
    AgentAction,
    AuthorityReference,
    DelegatedGrant,
    TargetFacts,
    TargetRelationship,
)
from src.agentauth.policy import (
    REFUSED_STATUS,
    UNSUPPORTED_STATUS,
    AgentAuthorizationService,
    PolicyError,
)
from src.agentauth.run_credential import CREDENTIAL_KEY_ENV, mint_credential

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
TENANT = "org-tenant-001"
KEY_ID = "gw-envelope-2026-09"

_PRIVATE_KEY = Ed25519PrivateKey.generate()
_PRIVATE_PEM = _PRIVATE_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("ascii")

# One env dict carries both the credential MAC key and the envelope signing key,
# because in production the gateway holds both and the worker holds neither. The
# values are test material generated in-process; nothing here is a real secret.
ENV = {
    CREDENTIAL_KEY_ENV: "adapter-test-key-not-a-real-secret",
    SIGNING_KEY_ENV: _PRIVATE_PEM,
    SIGNING_KEY_ID_ENV: KEY_ID,
}
PUBLIC_KEYS = {KEY_ID: _PRIVATE_KEY.public_key()}

AUTHORITY = AuthorityReference(
    kind="gate_decision",
    reference_id="decision-abc",
    human_id="human-operator-1",
    org_id=TENANT,
)


class FakeGrantStore:
    def __init__(self, grants=None, in_flight: int = 0):
        self.grants = grants or {}
        self.in_flight = in_flight
        self.count_calls: list[tuple[str, str]] = []

    def load_grant(self, *, principal: str, tenant_id: str):
        grant = self.grants.get(principal)
        if grant is None or grant.tenant_id != tenant_id:
            return None
        return grant

    def active_dispatch_count(self, *, grant_id: str, tenant_id: str) -> int:
        self.count_calls.append((grant_id, tenant_id))
        return self.in_flight


class FakeTargetResolver:
    def __init__(self, targets=None):
        self.targets = targets or {}

    def resolve(self, *, run_id, caller):
        return self.targets.get(run_id)


class FakeExecutionStore:
    """An ACTIVE execution matching whatever credential is presented.

    These are adapter tests: they are about envelope minting, command-ID binding
    and status shaping, not about live-state refusal. Auto-vivifying keeps them
    focused on that. The refusal semantics are covered where they belong, in
    ``test_policy.py::TestLiveExecutionState`` and ``test_execution.py``.
    """

    def __init__(self, records=None):
        self.records = records or {}

    def load_execution(self, *, invocation_id: str, tenant_id: str):
        if invocation_id in self.records:
            return self.records[invocation_id]
        return ExecutionRecord(
            invocation_id=invocation_id,
            tenant_id=tenant_id,
            current_attempt=1,
            status=ExecutionStatus.ACTIVE,
            current_credential_epoch=1,
            min_acceptable_credential_epoch=1,
            flow_id="flow-42",
        )


class FakeStateReader:
    """Returns a canned view, and records what it was asked for.

    Recording the arguments is the point of the fake: the adapter must ask about
    the *resolved* run and generation, not about whatever the caller named.
    """

    def __init__(self, view: AgentStatusView | None = None):
        self.view = view
        self.calls: list[tuple[str, int]] = []

    def read_state(self, *, run_id: str, generation: int):
        self.calls.append((run_id, generation))
        return self.view


def credential(invocation_id="inv-coordinator", attempt=1, tenant_id=TENANT, flow_id="flow-42") -> str:
    return mint_credential(
        invocation_id=invocation_id,
        attempt=attempt,
        tenant_id=tenant_id,
        flow_id=flow_id,
        now=NOW,
        env=ENV,
    )


def grant(principal="inv-coordinator#1", **overrides) -> DelegatedGrant:
    kwargs = {
        "grant_id": "grant-coordinator-1",
        "tenant_id": TENANT,
        "principal": principal,
        "authority": AUTHORITY,
        "allowed_actions": frozenset({AgentAction.MONITOR, AgentAction.PAUSE}),
        "target_relationships": frozenset({TargetRelationship.FLOW_NODE}),
        "flow_id": "flow-42",
        "revocation_epoch": 4,
    }
    kwargs.update(overrides)
    return DelegatedGrant(**kwargs)


def target(run_id="run-developer-7", **overrides) -> TargetFacts:
    kwargs = {
        "run_id": run_id,
        "tenant_id": TENANT,
        "flow_id": "flow-42",
        "relationships": frozenset({TargetRelationship.FLOW_NODE}),
        "generation": 3,
    }
    kwargs.update(overrides)
    return TargetFacts(**kwargs)


def live_view(**overrides) -> AgentStatusView:
    kwargs = {
        "run_id": "run-developer-7",
        "generation": 3,
        "state": "running",
        "available": True,
        "capabilities": {},
        "updated_at": "2026-09-13T12:00:00Z",
    }
    kwargs.update(overrides)
    return AgentStatusView(**kwargs)


def build_adapter(*, grants=None, targets=None, view=None, in_flight: int = 0):
    store = FakeGrantStore(grants=grants, in_flight=in_flight)
    resolver = FakeTargetResolver(targets=targets)
    reader = FakeStateReader(view=view)
    service = AgentAuthorizationService(
        grant_store=store,
        target_resolver=resolver,
        execution_store=FakeExecutionStore(),
        now=lambda: NOW,
        env=ENV,
    )
    adapter = AgentControlAdapter(policy=service, state_reader=reader, now=lambda: NOW, env=ENV)
    return adapter, reader


COMMAND_BODY = json.dumps({"command_id": "cmd-0001", "reason": "budget review"}, separators=(",", ":")).encode()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestDelegatedStatus:
    def test_authorized_coordinator_reads_a_flow_node_status(self):
        """AC1: the legitimate case works end to end."""
        adapter, reader = build_adapter(
            grants={"inv-coordinator#1": grant()},
            targets={"run-developer-7": target()},
            view=live_view(),
        )

        result = adapter.status(credential_token=credential(), target_run_id="run-developer-7")

        assert result.state == "running"
        assert result.available is True
        # The reader was asked about the resolved generation, not a caller-named one.
        assert reader.calls == [("run-developer-7", 3)]

    def test_a_run_reads_its_own_status_without_any_grant(self):
        """The implicit self-monitor path — the common case must not need provisioning."""
        adapter, _ = build_adapter(
            targets={"inv-coordinator": target(run_id="inv-coordinator", relationships=frozenset({TargetRelationship.SELF}))},
            view=live_view(run_id="inv-coordinator"),
        )

        result = adapter.status(credential_token=credential(), target_run_id="inv-coordinator")

        assert result.run_id == "inv-coordinator"

    def test_status_stamps_the_authority_from_the_decision_not_the_reader(self):
        """AC7: attribution comes from the authority that permitted the read.

        The reader returns a *different* reference on purpose. If the adapter
        passed the reader's value through, a state source could attribute a read
        to an authority that did not permit it.
        """
        adapter, _ = build_adapter(
            grants={"inv-coordinator#1": grant()},
            targets={"run-developer-7": target()},
            view=live_view(authority_reference_id="decision-somebody-elses"),
        )

        result = adapter.status(credential_token=credential(), target_run_id="run-developer-7")

        assert result.authority_reference_id == "decision-abc"

    def test_unreadable_state_is_unavailable_not_not_found(self):
        """The policy already said the target exists; lying about that is worse than an honest gap."""
        adapter, _ = build_adapter(
            grants={"inv-coordinator#1": grant()},
            targets={"run-developer-7": target()},
            view=None,
        )

        result = adapter.status(credential_token=credential(), target_run_id="run-developer-7")

        assert result.available is False
        assert result.state == "unavailable"
        assert result.authority_reference_id == "decision-abc"

    def test_status_of_a_run_in_another_flow_is_refused(self):
        """AC3: cross-flow, with the grant otherwise intact."""
        adapter, reader = build_adapter(
            grants={"inv-coordinator#1": grant()},
            targets={"run-other-flow": target(run_id="run-other-flow", flow_id="flow-99")},
        )

        with pytest.raises(PolicyError) as exc:
            adapter.status(credential_token=credential(), target_run_id="run-other-flow")

        assert exc.value.status_code == REFUSED_STATUS
        # And nothing was read. A refusal that still hit the state source would
        # be a timing and load signal about runs the caller cannot see.
        assert reader.calls == []

    def test_status_of_another_tenants_run_is_refused(self):
        adapter, reader = build_adapter(
            grants={"inv-coordinator#1": grant()},
            targets={"run-elsewhere": target(run_id="run-elsewhere", tenant_id="org-tenant-999")},
        )

        with pytest.raises(PolicyError) as exc:
            adapter.status(credential_token=credential(), target_run_id="run-elsewhere")

        assert exc.value.status_code == REFUSED_STATUS
        assert reader.calls == []

    def test_public_dict_carries_no_credential_material(self):
        """AC7: an auditable record without secrets."""
        adapter, _ = build_adapter(
            grants={"inv-coordinator#1": grant()},
            targets={"run-developer-7": target()},
            view=live_view(),
        )

        payload = adapter.status(credential_token=credential(), target_run_id="run-developer-7").to_public_dict()
        serialized = json.dumps(payload)

        for forbidden in ("adpr1", "adpe1", "token", "grant-coordinator-1", ENV[CREDENTIAL_KEY_ENV]):
            assert forbidden not in serialized


class TestTwoWorkersSharingOneRole:
    """AC2, at the adapter seam.

    ``test_policy.py`` proves the policy refuses impersonation. These prove the
    adapter does not reintroduce it by reading identity from anywhere else.
    """

    def test_a_worker_cannot_read_a_run_it_has_no_authority_over(self):
        adapter, _ = build_adapter(
            grants={"inv-coordinator#1": grant()},
            targets={"run-developer-7": target()},
            view=live_view(),
        )

        # A second worker on the same IAM role, with its own genuine credential.
        other = credential(invocation_id="inv-bystander")

        with pytest.raises(PolicyError) as exc:
            adapter.status(credential_token=other, target_run_id="run-developer-7")

        assert exc.value.status_code == REFUSED_STATUS

    def test_stealing_another_runs_credential_does_not_help_without_the_key(self):
        """A credential minted under a different key does not verify.

        This is the difference between the credential and the pod control token:
        a worker can read another run's *row*, but the row holds no credential —
        the MAC key lives only in the trusted mint and the gateway.
        """
        adapter, _ = build_adapter(
            grants={"inv-coordinator#1": grant()},
            targets={"run-developer-7": target()},
            view=live_view(),
        )
        forged = mint_credential(
            invocation_id="inv-coordinator",
            attempt=1,
            tenant_id=TENANT,
            now=NOW,
            env={CREDENTIAL_KEY_ENV: "a-key-the-attacker-chose"},
        )

        with pytest.raises(PolicyError) as exc:
            adapter.status(credential_token=forged, target_run_id="run-developer-7")

        assert exc.value.status_code == REFUSED_STATUS

    def test_a_body_supplied_identity_is_never_consulted(self):
        """The header is the identity; the body is data.

        Today's ``/agent/trigger`` reads ``parent_invocation_id`` from the body,
        which is sourced from a worker-writable env var. A body naming the
        coordinator must not authorize a bystander.
        """
        adapter, _ = build_adapter(
            grants={"inv-coordinator#1": grant()},
            targets={"run-developer-7": target()},
            view=live_view(),
        )
        body = json.dumps(
            {
                "command_id": "cmd-0001",
                "parent_invocation_id": "inv-coordinator",
                "principal": "inv-coordinator#1",
            }
        ).encode()

        with pytest.raises(PolicyError):
            adapter.prepare_command(
                credential_token=credential(invocation_id="inv-bystander"),
                target_run_id="run-developer-7",
                action=AgentAction.PAUSE,
                request_body=body,
            )


# ---------------------------------------------------------------------------
# Control — the ladder
# ---------------------------------------------------------------------------


class TestUnsupportedVerbsStay501:
    """The boundary between "may ask" and "can be done", now that ABORT can be done.

    ABORT was parametrized here alongside STEER until #3963. It moved to
    ``TestSupportedVerbPath`` rather than being deleted: the boundary this class
    protects is not about which verb, it is that an authorized request for an
    unimplemented verb gets an honest 501 instead of an envelope the far end will
    refuse. STEER still holds that boundary, and it holds it for the same reason
    ABORT used to — no revalidation branch, no worker verb.
    """

    def test_an_authorized_live_control_verb_is_501(self):
        """The hard boundary: authorization ships, behaviour does not."""
        action = AgentAction.STEER
        adapter, _ = build_adapter(
            grants={"inv-coordinator#1": grant(allowed_actions=frozenset({AgentAction.MONITOR, action}))},
            targets={"run-developer-7": target()},
        )

        with pytest.raises(PolicyError) as exc:
            adapter.prepare_command(
                credential_token=credential(),
                target_run_id="run-developer-7",
                action=action,
                request_body=COMMAND_BODY,
            )

        assert exc.value.status_code == UNSUPPORTED_STATUS

    def test_an_unauthorized_caller_gets_404_not_501(self):
        """Order matters: 501 would tell an outsider which verbs exist."""
        adapter, _ = build_adapter(targets={"run-developer-7": target()})

        with pytest.raises(PolicyError) as exc:
            adapter.prepare_command(
                credential_token=credential(),
                target_run_id="run-developer-7",
                action=AgentAction.PAUSE,
                request_body=COMMAND_BODY,
            )

        assert exc.value.status_code == REFUSED_STATUS

    def test_a_malformed_body_is_still_501_for_an_unbuilt_verb(self):
        """Body checks sit below the 501, so an unbuilt verb never reports body rules.

        The inverse of the human path's W1-05 ordering, and deliberately so:
        there, validation runs in the route against a published schema. Here the
        body is only ever inspected to bind an envelope, which an unbuilt verb
        never gets.
        """
        adapter, _ = build_adapter(
            grants={"inv-coordinator#1": grant(allowed_actions=frozenset({AgentAction.STEER}))},
            targets={"run-developer-7": target()},
        )

        with pytest.raises(PolicyError) as exc:
            adapter.prepare_command(
                credential_token=credential(),
                target_run_id="run-developer-7",
                action=AgentAction.STEER,
                request_body=b"not json at all",
            )

        assert exc.value.status_code == UNSUPPORTED_STATUS


# ---------------------------------------------------------------------------
# Control — the signing path
# ---------------------------------------------------------------------------


@pytest.fixture
def pause_supported():
    """Assert PAUSE is really supported, rather than patching it in.

    This was a ``monkeypatch`` while PAUSE was unimplemented. It shipped in #5222
    and the patch outlived its reason — which is worse than harmless, because a
    patched set means these signing tests would keep passing if PAUSE were removed
    from the real constant and the live route began answering 501. Reading the
    shipped value makes the fixture fail in that case, which is the point.
    """
    assert AgentAction.PAUSE in policy_module.SUPPORTED_AGENT_ACTIONS, (
        "SUPPORTED_AGENT_ACTIONS no longer contains PAUSE, so prepare_command would refuse before "
        "signing and these tests would cover nothing. Fix the constant, do not patch it here."
    )


class TestSupportedVerbPath:
    def test_abort_signs_an_envelope_through_the_shipped_policy(self):
        """#3963: ABORT reaches the signing path with nothing patched.

        Deliberately takes no ``*_supported`` fixture. The point is that the
        deployed ``SUPPORTED_AGENT_ACTIONS`` admits ABORT, so if the constant were
        reverted this test fails at ``require_supported`` — which no amount of
        fixture arrangement inside this file could hide. The grant must still
        convey ABORT explicitly: enabling a verb deployment-wide is not the same as
        granting it to a caller, and ``LIVE_CONTROL_ACTIONS`` keeps MONITOR from
        implying it.
        """
        adapter, _ = build_adapter(
            grants={"inv-coordinator#1": grant(allowed_actions=frozenset({AgentAction.MONITOR, AgentAction.ABORT}))},
            targets={"run-developer-7": target()},
        )

        prepared = adapter.prepare_command(
            credential_token=credential(),
            target_run_id="run-developer-7",
            action=AgentAction.ABORT,
            request_body=COMMAND_BODY,
        )

        assert prepared.envelope is not None
        verified = verify_envelope(
            prepared.envelope,
            public_keys=PUBLIC_KEYS,
            expected_run_id="run-developer-7",
            expected_generation=3,
            expected_action="abort",
            expected_command_id="cmd-0001",
            request_body=COMMAND_BODY,
            now=NOW,
        )
        # The action is bound into the envelope, so a signed abort cannot be
        # replayed as any other verb even though both are now supported.
        assert verified.action == "abort"
        assert verified.target_generation == 3
        assert verified.body_digest == body_digest(COMMAND_BODY)

    def test_an_abort_grant_does_not_come_from_monitor(self):
        """The verb being deployed does not grant it — the split in #5028 holds.

        Paired with the test above because the two together are the actual
        contract: enabling ABORT widened what a *granted* caller may do and
        nothing else. Without this, "abort is supported now" would be
        indistinguishable from "abort is available to anyone who can monitor".
        """
        adapter, _ = build_adapter(
            grants={"inv-coordinator#1": grant(allowed_actions=frozenset({AgentAction.MONITOR}))},
            targets={"run-developer-7": target()},
        )

        with pytest.raises(PolicyError) as exc:
            adapter.prepare_command(
                credential_token=credential(),
                target_run_id="run-developer-7",
                action=AgentAction.ABORT,
                request_body=COMMAND_BODY,
            )

        # 404, not 501: the verb exists, this caller simply has no authority for
        # it, and saying 501 would misreport a permission problem as a missing
        # feature.
        assert exc.value.status_code == REFUSED_STATUS

    def test_signs_an_envelope_bound_to_the_resolved_facts(self, pause_supported):
        """AC5: what the listener will check is what the gateway asserted."""
        adapter, _ = build_adapter(
            grants={"inv-coordinator#1": grant()},
            targets={"run-developer-7": target()},
        )

        prepared = adapter.prepare_command(
            credential_token=credential(),
            target_run_id="run-developer-7",
            action=AgentAction.PAUSE,
            request_body=COMMAND_BODY,
        )

        assert prepared.envelope is not None
        verified = verify_envelope(
            prepared.envelope,
            public_keys=PUBLIC_KEYS,
            expected_run_id="run-developer-7",
            expected_generation=3,
            expected_action="pause",
            expected_command_id="cmd-0001",
            request_body=COMMAND_BODY,
            now=NOW,
        )
        assert verified.target_run_id == "run-developer-7"
        assert verified.target_generation == 3
        assert verified.command_id == "cmd-0001"
        assert verified.body_digest == body_digest(COMMAND_BODY)
        # The grant's epoch travels with the envelope so a queued action can be
        # revalidated against the current epoch before it takes effect (AC6).
        assert verified.revocation_epoch == 4
        assert verified.principal == "inv-coordinator#1"

    def test_the_envelope_travels_in_its_own_header(self, pause_supported):
        adapter, _ = build_adapter(
            grants={"inv-coordinator#1": grant()},
            targets={"run-developer-7": target()},
        )

        prepared = adapter.prepare_command(
            credential_token=credential(),
            target_run_id="run-developer-7",
            action=AgentAction.PAUSE,
            request_body=COMMAND_BODY,
        )

        assert prepared.headers[ENVELOPE_HEADER] == prepared.envelope
        # The generation the listener compares against is the resolved one.
        assert prepared.headers["X-Adp-Control-Generation"] == "3"
        # The credential does NOT travel onward to the pod. A worker holding
        # another run's credential could call the gateway as that run.
        assert CREDENTIAL_HEADER not in prepared.headers

    def test_generation_comes_from_the_resolver_not_the_caller(self, pause_supported):
        """A caller cannot have an envelope minted for a generation of its choosing."""
        adapter, _ = build_adapter(
            grants={"inv-coordinator#1": grant()},
            targets={"run-developer-7": target(generation=9)},
        )

        prepared = adapter.prepare_command(
            credential_token=credential(),
            target_run_id="run-developer-7",
            action=AgentAction.PAUSE,
            request_body=json.dumps({"command_id": "cmd-0001", "generation": 3}).encode(),
        )

        assert prepared.headers["X-Adp-Control-Generation"] == "9"

    def test_the_envelope_does_not_verify_against_a_different_body(self, pause_supported):
        """The digest is over the bytes handed in, so a rewrite in transit fails."""
        adapter, _ = build_adapter(
            grants={"inv-coordinator#1": grant()},
            targets={"run-developer-7": target()},
        )

        prepared = adapter.prepare_command(
            credential_token=credential(),
            target_run_id="run-developer-7",
            action=AgentAction.PAUSE,
            request_body=COMMAND_BODY,
        )

        with pytest.raises(EnvelopeError):
            verify_envelope(
                prepared.envelope,
                public_keys=PUBLIC_KEYS,
                expected_run_id="run-developer-7",
                expected_generation=3,
                expected_action="pause",
                expected_command_id="cmd-0001",
                request_body=COMMAND_BODY + b" ",
                now=NOW,
            )

    def test_the_envelope_lifetime_is_the_bounded_revocation_delay(self, pause_supported):
        """AC6: the maximum window an already-forwarded command can outlive a revocation."""
        adapter, _ = build_adapter(
            grants={"inv-coordinator#1": grant()},
            targets={"run-developer-7": target()},
        )

        prepared = adapter.prepare_command(
            credential_token=credential(),
            target_run_id="run-developer-7",
            action=AgentAction.PAUSE,
            request_body=COMMAND_BODY,
        )

        assert prepared.expires_in_seconds == 30

    def test_missing_signing_key_refuses_rather_than_forwarding_unsigned(self, pause_supported):
        """The failure direction that matters.

        An unsigned forward would be refused by the listener anyway, but the
        gateway must not be the component that emits it — and the caller must not
        learn the key is missing.
        """
        store = FakeGrantStore(grants={"inv-coordinator#1": grant()})
        resolver = FakeTargetResolver(targets={"run-developer-7": target()})
        service = AgentAuthorizationService(
            grant_store=store, target_resolver=resolver, execution_store=FakeExecutionStore(), now=lambda: NOW, env=ENV
        )
        # Credential key present, envelope key absent: authorization succeeds and
        # only signing fails, which is the deployment gap being modelled.
        adapter = AgentControlAdapter(
            policy=service,
            state_reader=FakeStateReader(),
            now=lambda: NOW,
            env={CREDENTIAL_KEY_ENV: ENV[CREDENTIAL_KEY_ENV], SIGNING_KEY_ID_ENV: KEY_ID},
        )

        with pytest.raises(PolicyError) as exc:
            adapter.prepare_command(
                credential_token=credential(),
                target_run_id="run-developer-7",
                action=AgentAction.PAUSE,
                request_body=COMMAND_BODY,
            )

        assert exc.value.status_code == REFUSED_STATUS
        assert "key" not in exc.value.detail.lower()

    def test_a_revoked_grant_yields_no_envelope(self, pause_supported):
        adapter, _ = build_adapter(
            grants={"inv-coordinator#1": grant(revoked=True)},
            targets={"run-developer-7": target()},
        )

        with pytest.raises(PolicyError) as exc:
            adapter.prepare_command(
                credential_token=credential(),
                target_run_id="run-developer-7",
                action=AgentAction.PAUSE,
                request_body=COMMAND_BODY,
            )

        assert exc.value.status_code == REFUSED_STATUS

    def test_monitor_needs_no_envelope_even_when_supported(self, pause_supported):
        """A read does not mutate the target, so there is nothing to authorize at the pod."""
        adapter, _ = build_adapter(
            grants={"inv-coordinator#1": grant()},
            targets={"run-developer-7": target()},
        )

        prepared = adapter.prepare_command(
            credential_token=credential(),
            target_run_id="run-developer-7",
            action=AgentAction.MONITOR,
            request_body=COMMAND_BODY,
        )

        assert prepared.envelope is None
        assert ENVELOPE_HEADER not in prepared.headers


class TestCommandIdBinding:
    """The envelope and the journal must never name different commands."""

    @pytest.fixture(autouse=True)
    def _supported(self, pause_supported):
        return None

    def _prepare(self, body: bytes):
        adapter, _ = build_adapter(
            grants={"inv-coordinator#1": grant()},
            targets={"run-developer-7": target()},
        )
        return adapter.prepare_command(
            credential_token=credential(),
            target_run_id="run-developer-7",
            action=AgentAction.PAUSE,
            request_body=body,
        )

    def test_command_id_is_read_from_the_body(self):
        prepared = self._prepare(json.dumps({"command_id": "cmd-from-body"}).encode())
        assert prepared.command_id == "cmd-from-body"

    @pytest.mark.parametrize(
        "body,note",
        [
            (b"", "empty"),
            (b"not json", "unparseable"),
            (b'"a string"', "not an object"),
            (b"[]", "a list"),
            (b"{}", "no command_id"),
            (b'{"command_id": ""}', "empty command_id"),
            (b'{"command_id": "   "}', "whitespace command_id"),
            (b'{"command_id": 7}', "numeric command_id"),
            (b'{"command_id": null}', "null command_id"),
            (b'{"command_id": true}', "boolean command_id"),
        ],
    )
    def test_unbindable_bodies_are_refused_with_400(self, body, note):
        """400, not 404: reached only after authorization, so honesty costs nothing."""
        with pytest.raises(PolicyError) as exc:
            self._prepare(body)
        assert exc.value.status_code == INVALID_BODY_STATUS, note

    def test_an_oversized_body_is_refused_before_parsing(self):
        oversized = b'{"command_id":"c","reason":"' + b"x" * MAX_COMMAND_BODY_BYTES + b'"}'
        with pytest.raises(PolicyError) as exc:
            self._prepare(oversized)
        assert exc.value.status_code == INVALID_BODY_STATUS

    def test_non_utf8_bytes_are_refused(self):
        with pytest.raises(PolicyError) as exc:
            self._prepare(b'{"command_id": "\xff\xfe"}')
        assert exc.value.status_code == INVALID_BODY_STATUS


class TestHeaderNameParity:
    def test_the_envelope_header_matches_the_worker_listener(self):
        """No build step spans the gateway and the worker image, so pin it here.

        A rename on one side alone would refuse every control command with the
        listener's fail-closed "not configured" reason — which looks like a key
        distribution problem and would send an operator to the wrong place.
        """
        listener = (
            "modules/agent-factory/agent/src/control-listener.ts",
            "ENVELOPE_HEADER",
        )
        assert ENVELOPE_HEADER.lower() == "x-adp-control-authorization", listener
