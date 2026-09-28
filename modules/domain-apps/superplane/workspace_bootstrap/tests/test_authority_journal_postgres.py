"""Temporary grants survive crashes and cannot cross reservation generations."""

import dataclasses

import pytest
from superplane_bootstrap.authority_journal import AuthorityJournal, generation_for
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.registration import reserve_registration
from superplane_bootstrap.registry import SqlRegistrationStore
from superplane_bootstrap.state import claim_fingerprint
from superplane_bootstrap.target import verify_target
from superplane_bootstrap.temporary_authority import TemporaryAuthority

from .conftest import NAMESPACE
from .test_registry_postgres import database, loop, schema_ddl, server  # noqa: F401


@pytest.fixture
def authority(database, binding, provider_identity, observed_cluster, expected_target):  # noqa: F811
    target = verify_target(
        binding=binding,
        provider=provider_identity,
        observed=observed_cluster,
        cluster_ownership="adp-created",
        **expected_target,
    )
    sql = database()
    registry = SqlRegistrationStore(sql)
    reservation = reserve_registration(
        store=registry, target=target, namespace=NAMESPACE
    )
    journal = AuthorityJournal(
        sql,
        binding,
        target,
        generation_for(binding, reservation),
        claim_fingerprint(reservation.attempt_token),
    )
    return journal, registry, reservation


class Backend:
    def __init__(self):
        self.resources = {}
        self.calls = []
        self.crash = None
        self.residual = False

    def plan(self, journal):
        return {
            "grants": [
                {"key": key, "generation": journal.generation}
                for key in ("registrar", "worker")
            ]
        }

    def observe(self, spec):
        return self.resources.get(spec["key"])

    def create(self, spec):
        key = spec["key"]
        self.calls.append(("create", key))
        assert key not in self.resources
        self.resources[key] = {"uid": key + "-uid", "generation": spec["generation"]}
        if self.crash == ("create", key):
            raise KeyboardInterrupt()
        return self.resources[key]

    def verify(self, spec, identity):
        if identity["generation"] != spec["generation"]:
            raise BootstrapRefused("generation differs")

    def delete(self, spec, identity):
        key = spec["key"]
        self.calls.append(("delete", key))
        assert self.resources[key] == identity
        if not self.residual:
            del self.resources[key]
        if self.crash == ("delete", key):
            raise KeyboardInterrupt()

    def verify_worker_permissions(self):
        pass

    def verify_worker_binding(self):
        pass

    def verify_revoked(self, plan, progress):
        if any(
            spec["key"] in progress and spec["key"] in self.resources
            for spec in plan["grants"]
        ):
            raise BootstrapRefused("residual authority")


@pytest.mark.parametrize(
    "boundary",
    [(verb, key) for verb in ("create", "delete") for key in ("registrar", "worker")],
)
def test_crash_after_each_grant_and_revoke_recovers(authority, boundary):
    journal, _, _ = authority
    backend = Backend()
    lease = TemporaryAuthority(journal, backend)
    backend.crash = boundary
    if boundary[0] == "create":
        with pytest.raises(KeyboardInterrupt):
            lease.acquire()
    else:
        lease.acquire()
        with pytest.raises(KeyboardInterrupt):
            lease.revoke()
    backend.crash = None
    restarted = TemporaryAuthority(journal, backend)
    restarted.revoke()
    assert not backend.resources
    assert restarted.revoked
    assert journal.read()[1]["complete"] is True
    before = list(backend.calls)
    restarted.revoke()
    assert backend.calls == before


def test_adopted_entry_is_never_modified(authority):
    journal, _, _ = authority
    backend = Backend()
    backend.resources["registrar"] = {"uid": "adopted", "generation": "other"}
    lease = TemporaryAuthority(journal, backend)
    with pytest.raises(BootstrapRefused, match="adopted"):
        lease.acquire()
    lease.revoke()
    assert lease.revoked
    assert backend.resources["registrar"]["uid"] == "adopted"
    assert backend.calls == []


def test_partial_acquisition_revokes_only_its_own_grants(authority):
    journal, registry, reservation = authority
    backend = Backend()
    backend.resources["worker"] = {"uid": "adopted", "generation": "other"}
    lease = TemporaryAuthority(journal, backend)
    with pytest.raises(BootstrapRefused, match="adopted"):
        lease.acquire()
    lease.revoke()
    assert lease.revoked
    assert backend.resources == {"worker": {"uid": "adopted", "generation": "other"}}
    assert backend.calls == [("create", "registrar"), ("delete", "registrar")]
    assert registry.release(reservation.workspace_id, reservation.attempt_token)


def test_stale_generation_cannot_mutate_or_revoke_successor(authority):
    journal, registry, reservation = authority
    backend = Backend()
    lease = TemporaryAuthority(journal, backend)
    lease.acquire()
    # Simulate an external reservation replacement. Normal release now refuses
    # while privileges remain, but stale recovery still must fence such a change.
    with journal.store.transaction():
        journal.store.execute(
            "DELETE FROM workspace_bootstrap_reservations WHERE workspace_id=:workspace_id",
            journal.key,
        )
        journal.store.execute(
            "UPDATE workspace_bootstrap_authority SET revoked=true WHERE workspace_id=:workspace_id",
            journal.key,
        )
    successor = reserve_registration(
        store=registry, target=journal.target, namespace=NAMESPACE
    )
    assert successor.attempt_token != reservation.attempt_token
    calls = list(backend.calls)
    with pytest.raises(BootstrapRefused, match="stale"):
        lease.revoke()
    with pytest.raises(BootstrapRefused, match="stale"):
        lease.mutate(lambda: backend.calls.append("unsafe"))
    assert backend.calls == calls


def test_wrong_operation_or_target_cannot_recover(authority):
    journal, _, _ = authority
    backend = Backend()
    TemporaryAuthority(journal, backend).acquire()
    other = dataclasses.replace(
        journal, binding=dataclasses.replace(journal.binding, operation_id="other")
    )
    with pytest.raises(BootstrapRefused, match="binding differs"):
        TemporaryAuthority(other, backend).revoke()
    other = dataclasses.replace(
        journal, target=dataclasses.replace(journal.target, cluster_arn="arn:other")
    )
    with pytest.raises(BootstrapRefused, match="target differs"):
        TemporaryAuthority(other, backend).revoke()
    assert not any(c[0] == "delete" for c in backend.calls)


def test_residual_privilege_keeps_registrar_and_refuses_readiness(authority):
    journal, _, _ = authority
    backend = Backend()
    lease = TemporaryAuthority(journal, backend)
    lease.acquire()
    backend.residual = True
    with pytest.raises(BootstrapRefused, match="unresolved"):
        lease.revoke()
    assert not lease.revoked
    assert "registrar" in backend.resources
    assert journal.read()[1]["worker"]["phase"] == "revoke_intent"
    backend.residual = False
    lease.revoke()
    assert lease.revoked


def test_mutations_require_completed_acquisition_and_stop_after_recovery(authority):
    journal, _, _ = authority
    backend = Backend()
    lease = TemporaryAuthority(journal, backend)
    calls = []
    with pytest.raises(BootstrapRefused, match="missing"):
        lease.mutate(lambda: calls.append("before acquire"))
    lease.acquire()
    lease.mutate(lambda: calls.append("active"))
    TemporaryAuthority(journal, backend).revoke()
    # The original in-memory object has not been told recovery ran.
    assert not lease.revoked
    with pytest.raises(BootstrapRefused, match="not active"):
        lease.mutate(lambda: calls.append("after revoke"))
    assert calls == ["active"]


def test_same_generation_cannot_be_acquired_twice(authority):
    journal, _, _ = authority
    backend = Backend()
    lease = TemporaryAuthority(journal, backend)
    lease.acquire()
    before = list(backend.calls)
    with pytest.raises(BootstrapRefused, match="requires revocation"):
        TemporaryAuthority(journal, backend).acquire()
    assert backend.calls == before


def test_wrong_operation_cannot_use_an_active_generation(authority):
    journal, _, _ = authority
    backend = Backend()
    TemporaryAuthority(journal, backend).acquire()
    other = dataclasses.replace(
        journal, binding=dataclasses.replace(journal.binding, operation_id="other")
    )
    with pytest.raises(BootstrapRefused, match="binding differs"):
        TemporaryAuthority(other, backend).mutate(lambda: pytest.fail("unauthorized"))


def test_recovery_between_intent_commit_and_provider_call_prevents_late_grant(
    authority,
):
    from contextlib import contextmanager

    journal, _, _ = authority
    backend = Backend()

    class RecoverAfterIntent:
        triggered = False

        def __getattr__(self, name):
            return getattr(journal, name)

        @contextmanager
        def fenced(self, **kwargs):
            with journal.fenced(**kwargs):
                yield
            _, progress = journal.read()
            if (
                not self.triggered
                and progress.get("registrar", {}).get("phase") == "grant_intent"
            ):
                self.triggered = True
                TemporaryAuthority(journal, backend).revoke()

    lease = TemporaryAuthority(RecoverAfterIntent(), backend)
    with pytest.raises(BootstrapRefused, match="not acquiring"):
        lease.acquire()
    assert not backend.resources
    assert backend.calls == []
    assert journal.read()[1]["complete"]


def test_revocation_crash_cannot_reopen_worker_mutation(authority):
    journal, _, _ = authority
    backend = Backend()
    lease = TemporaryAuthority(journal, backend)
    lease.acquire()
    backend.crash = ("delete", "worker")
    with pytest.raises(KeyboardInterrupt):
        lease.revoke()
    with pytest.raises(BootstrapRefused, match="not active"):
        lease.mutate(lambda: pytest.fail("late mutation"))
    backend.crash = None
    TemporaryAuthority(journal, backend).revoke()
    assert not backend.resources


def test_pending_grants_block_release_recovery_release_and_registration(authority):
    from .test_registry_postgres import _Target

    journal, registry, reservation = authority
    backend = Backend()
    lease = TemporaryAuthority(journal, backend)
    lease.acquire()
    actions = [
        lambda: registry.release(
            journal.target.workspace_id, reservation.attempt_token
        ),
        lambda: registry.release_claim(journal.target.workspace_id, journal.claim),
        lambda: registry.finalize(_Target(), reservation.attempt_token),
        lambda: reserve_registration(
            store=registry, target=journal.target, namespace=NAMESPACE
        ),
    ]
    for action in actions:
        with pytest.raises(BootstrapRefused, match="authority recovery is outstanding"):
            action()
    assert journal.read()[1]["phase"] == "active"
    lease.revoke()
    assert registry.release(journal.target.workspace_id, reservation.attempt_token)
