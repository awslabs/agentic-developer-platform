"""Fresh binding reads overlap without moving SQL or writes off their owner."""

from threading import Barrier, Lock, current_thread
from types import SimpleNamespace

import pytest

from superplane_bootstrap.authority_backend import BootstrapGrantBackend
from superplane_bootstrap.errors import BootstrapRefused


@pytest.mark.parametrize("failure", [None, "identity", "permission", "expired"])
def test_bounded_binding_reads_join_and_preserve_every_check(failure):
    owner = current_thread()
    lock, barrier = Lock(), Barrier(4)
    observed, verified, freshness = [], [], []
    active = peak = 0
    specs = [{"key": str(i), "kind": "kubernetes"} for i in range(8)]
    plan = {"grants": [*specs, {"key": "namespace", "lifetime": "resource"}]}
    progress = {str(i): {"identity": {"uid": str(i)}} for i in range(8)}

    def current():
        assert current_thread() is owner
        freshness.append(True)
        if failure == "expired" and len(freshness) == 2:
            raise BootstrapRefused("expired during provider reads")

    def journal():
        assert current_thread() is owner
        return plan, progress

    def observe(spec):
        nonlocal active, peak
        assert current_thread() is not owner
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=5)
        with lock:
            active -= 1
            observed.append(spec["key"])
        return {
            "uid": "changed"
            if failure == "identity" and spec["key"] == "0"
            else spec["key"]
        }

    def verify(spec, identity):
        assert current_thread() is not owner
        verified.append(spec["key"])
        if failure == "permission" and spec["key"] == "0":
            raise BootstrapRefused("permission changed")

    backend = BootstrapGrantBackend.__new__(BootstrapGrantBackend)
    backend.clients = SimpleNamespace(verify=current)
    backend.journal = SimpleNamespace(read_locked=journal)
    backend.observe, backend.verify = observe, verify
    if failure:
        with pytest.raises(BootstrapRefused):
            backend.verify_worker_binding()
    else:
        backend.verify_worker_binding()
    assert sorted(observed) == [str(i) for i in range(8)]
    assert active == 0 and peak == 4
    assert len(freshness) == (1 if failure in {"identity", "permission"} else 2)
    assert sorted(verified) == (
        [str(i) for i in range(1, 8)]
        if failure == "identity"
        else [str(i) for i in range(8)]
    )


def test_late_policy_read_cannot_restore_removed_registrar():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from unittest.mock import patch
    from superplane_bootstrap.grant_plan import ADMIN_POLICY

    started, release = Event(), Event()
    backend = BootstrapGrantBackend.__new__(BootstrapGrantBackend)
    backend.registrar_removed = False
    backend.journal = object()
    backend.registrar, backend.supervisor = object(), object()
    backend.clients = SimpleNamespace(
        eks=object(),
        target=SimpleNamespace(cluster_name="synthetic"),
        principals={"registrar": "synthetic-registrar"},
    )
    spec = {"kind": "kubernetes"}

    def policies(*args, **kwargs):
        if not started.is_set():
            started.set()
            assert release.wait(timeout=5)
            return [{"policyArn": ADMIN_POLICY}]
        return []

    with patch("superplane_bootstrap.authority_backend._pages", policies):
        with ThreadPoolExecutor(max_workers=2) as executor:
            late = executor.submit(backend._adapter, spec)
            assert started.wait(timeout=5)
            try:
                assert (
                    executor.submit(backend._adapter, spec).result(timeout=5)
                    is backend.supervisor
                )
            finally:
                release.set()
            assert late.result(timeout=5) is backend.supervisor
    assert backend.registrar_removed is True
