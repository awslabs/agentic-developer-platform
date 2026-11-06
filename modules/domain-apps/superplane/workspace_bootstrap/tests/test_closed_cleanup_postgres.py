# ruff: noqa: F811 -- Imported PostgreSQL fixtures are injected by pytest.
"""Native commit precedes receipt; lost acknowledgements never repeat cleanup."""

import copy
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pytest
from superplane_bootstrap.closed_cleanup_boundary import cleanup_revalidator
from superplane_bootstrap.closed_cleanup_run import run_closed_cleanup
from superplane_bootstrap.component_journal import component_key
from superplane_bootstrap.registry import SqlRegistrationStore

from .test_acquisition_recovery_postgres import acquisition  # noqa: F401
from .test_authority_journal_postgres import authority  # noqa: F401
from .test_component_recovery_postgres import component_source  # noqa: F401
from .test_registry_postgres import database, loop, schema_ddl, server  # noqa: F401
from .test_closed_cleanup_access import Journal


def native_store(connection, event_loop):
    path = (
        Path(__file__).resolve().parents[4]
        / "gateway/src/internal/domain_closed_cleanup_store.py"
    )
    spec = importlib.util.spec_from_file_location("closed_cleanup_sql", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.NativeStore(connection, event_loop)


@pytest.mark.parametrize("acquisition", [(29, "revoke_intent", True)], indirect=True)
@pytest.mark.parametrize("lost_receipt", [False, True])
def test_native_commit_is_independently_visible_before_service_receipt(
    component_source, database, loop, lost_receipt
):
    (
        original,
        _,
        backend,
        resources,
        objects,
        qualified,
        descriptor,
        expiry,
        snapshot,
    ) = component_source
    # Use the actual production asyncpg bridge and an independent SQL observer.
    store = native_store(original.store._connection, loop._loop)
    journal = replace(original, store=store)
    observer = database()
    revalidate = cleanup_revalidator(
        qualified,
        descriptor,
        read_snapshot=snapshot,
        read_expiry=lambda: copy.deepcopy(expiry),
    )
    history = Journal()

    class Access:
        permit = {"permit_id": "protected-permit"}

        def __init__(self):
            self.journal, self.lost, self.acquisitions = history, lost_receipt, 0

        def acquire(self):
            self.acquisitions += 1
            if history.read()["phase"] == "new":
                history.append(history.read(), phase="observer-ready")

        def record_verified(self, result):
            rows = observer.execute(
                "SELECT revoked,progress_json FROM workspace_bootstrap_authority WHERE generation=:generation",
                journal.key,
            )
            assert (
                rows[0]["revoked"] and json.loads(rows[0]["progress_json"])["complete"]
            )
            assert not observer.execute(
                "SELECT * FROM workspace_bootstrap_reservations WHERE workspace_id=:workspace_id",
                journal.key,
            )
            if self.lost:
                self.lost = False
                raise TimeoutError("SQL committed; receipt not persisted")
            history.append(
                history.read(), phase="original-cleanup-verified", result=result
            )

        def teardown(self, **kw):
            return history.append(history.read(), phase="complete")

    access = Access()
    before_objects = copy.deepcopy(objects)

    def run():
        return run_closed_cleanup(
            journal,
            SqlRegistrationStore(store),
            backend,
            backend,
            access,
            revalidate=revalidate,
            verify_absence=lambda plan: set(resources) == {"workspace-namespace"},
            read_component=lambda body: copy.deepcopy(objects[component_key(body)]),
        )

    if lost_receipt:
        with pytest.raises(TimeoutError):
            run()
        deleted = backend.deleted[:]
        result = run()
        assert backend.deleted == deleted
    else:
        result = run()
    assert result["verifier_access_removed"] and result["claim_released"]
    assert history.read()["phase"] == "complete"
    assert objects == before_objects and set(resources) == {"workspace-namespace"}
    # Completion retry has no Kubernetes reads or mutations after observer removal.
    backend.observe = lambda spec: pytest.fail("removed verifier must not be used")
    run()
