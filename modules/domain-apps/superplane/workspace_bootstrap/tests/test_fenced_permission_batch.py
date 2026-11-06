"""Installation permission reads share a fence; later mutations never share proof."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from threading import Barrier, Event, current_thread
from types import SimpleNamespace

import pytest

from superplane_bootstrap.authority_runtime import FencedClusterAccess
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.permissions import verify_install_permissions
from superplane_bootstrap.temporary_authority import TemporaryAuthority
from .test_permission_read_concurrency import (
    CRDS,
    NAMESPACE,
    SERVICE_ACCOUNT,
    backend_for,
)


def fixture(mode="management", *, probe=None, changed_at=None, phase="active"):
    boundary = SimpleNamespace(held=False, inventories=0, released=0)

    def guarded_probe(*args):
        assert boundary.held
        return probe(*args) if probe is not None else args[1]

    backend, state, contract = backend_for(mode, probe=guarded_probe)
    original = backend.verify_worker_binding

    def inventory():
        assert boundary.held and state.active == 0
        assert state.owner is None or current_thread() is state.owner
        original()
        boundary.inventories += 1
        if boundary.inventories == changed_at:
            raise BootstrapRefused("synthetic grant changed")

    @contextmanager
    def fenced():
        assert not boundary.held
        boundary.held = True
        try:
            yield
        finally:
            assert state.active == 0
            boundary.held = False
            boundary.released += 1

    backend.verify_worker_binding = inventory
    journal = SimpleNamespace(fenced=fenced, read_locked=lambda: ({}, {"phase": phase}))
    authority = TemporaryAuthority(journal, backend)
    access = FencedClusterAccess(authority)
    expected = [row[:-1] for row in contract if row[0] == "installer" and row[-1]]
    return access, state, boundary, expected


def run(access, **kwargs):
    return verify_install_permissions(
        access, NAMESPACE, SERVICE_ACCOUNT, SERVICE_ACCOUNT, CRDS, **kwargs
    )


@pytest.mark.parametrize("mode,count", [("management", 26), ("legacy", 29)])
def test_exact_permission_contract_uses_two_complete_inventories(mode, count):
    wave = Barrier(4)

    def probe(key, answer, ordinal, owner):
        if current_thread() is not owner and ordinal <= 4:
            wave.wait(timeout=5)
        return answer

    access, state, boundary, expected = fixture(mode, probe=probe)
    run(access)
    assert len(expected) == count
    assert Counter(state.observed) == Counter(expected)
    assert state.peak == 4 and state.active == 0
    assert boundary.inventories == 2 and boundary.released == 1


@pytest.mark.parametrize("changed_at", [1, 2])
def test_changed_grant_inventory_refuses_before_or_after_reads(changed_at):
    access, state, boundary, expected = fixture(changed_at=changed_at)
    with pytest.raises(BootstrapRefused, match="grant changed"):
        run(access)
    assert boundary.inventories == changed_at
    assert Counter(state.observed) == Counter(expected if changed_at == 2 else [])
    assert boundary.released == 1


@pytest.mark.parametrize("phase", ["acquiring", "revoking", "revoked"])
def test_inactive_authority_cannot_start_permission_batch(phase):
    access, state, boundary, _ = fixture(phase=phase)
    with pytest.raises(BootstrapRefused, match="not active"):
        run(access)
    assert not state.observed and boundary.inventories == 0


@pytest.mark.parametrize("fault", [False, None, 1, "true", OSError])
def test_failed_read_joins_other_reads_before_fence_release(fault):
    blocked, release, failed = Event(), Event(), Event()

    def probe(key, answer, ordinal, owner):
        if key[1:3] == ("get", "serviceaccounts"):
            blocked.set()
            assert release.wait(timeout=5)
        if key[1:3] == ("create", "serviceaccounts"):
            assert blocked.wait(timeout=5)
            failed.set()
            if fault is OSError:
                raise OSError("synthetic transport failure")
            return fault
        return answer

    access, state, boundary, expected = fixture(probe=probe)
    with ThreadPoolExecutor(max_workers=1) as caller:
        result = caller.submit(run, access)
        try:
            assert failed.wait(timeout=5)
            with pytest.raises(TimeoutError):
                result.result(timeout=0.05)
            assert boundary.held and boundary.released == 0
        finally:
            release.set()
        with pytest.raises(OSError if fault is OSError else BootstrapRefused):
            result.result(timeout=5)
    assert Counter(state.observed) == Counter(expected)
    assert boundary.inventories == 1 and boundary.released == 1


def test_namespace_denial_prevents_namespace_read_and_submitted_reviews():
    access, state, boundary, _ = fixture(probe=lambda *args: False)
    access.raw.namespace = lambda _: pytest.fail("namespace read after denial")
    with pytest.raises(BootstrapRefused, match="permission denied"):
        run(access)
    assert len(state.observed) == 1 and boundary.inventories == 1


def test_external_submitter_cannot_outlive_owned_fence():
    access, state, boundary, _ = fixture()
    with pytest.raises(BootstrapRefused, match="own fence"):
        run(access, submit=lambda *args: pytest.fail("external submitter called"))
    assert not state.observed and boundary.inventories == 0


def test_supervisor_cannot_select_installer_batch():
    access, state, boundary, _ = fixture()
    supervisor = FencedClusterAccess(access.authority, supervisor=True)
    with pytest.raises(BootstrapRefused, match="own fence"):
        run(supervisor)
    assert not state.observed and boundary.inventories == 0


def test_next_mutation_still_rechecks_all_grants():
    access, state, boundary, _ = fixture(changed_at=3)
    access.raw.create_namespace = lambda *args: pytest.fail(
        "mutation after grant change"
    )
    run(access)
    with pytest.raises(BootstrapRefused, match="grant changed"):
        access.create_namespace(NAMESPACE, {})
    assert boundary.inventories == 3 and boundary.released == 2
