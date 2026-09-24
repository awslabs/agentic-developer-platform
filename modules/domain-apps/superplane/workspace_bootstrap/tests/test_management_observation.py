"""Only fresh, exact operation and reservation observations can unblock bootstrap."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from contextlib import contextmanager
import json

import pytest

from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.management_observation import ManagementObservation


def observer(credential=None):
    from superplane_contracts.provisioning import OperationBinding, ResolvedPrincipal

    target = SimpleNamespace(
        org_id="org", workspace_id="workspace", cluster_arn="cluster"
    )
    binding = OperationBinding(
        principal=ResolvedPrincipal(
            subject="worker", org_id="org", workspace_id="workspace"
        ),
        operation_id="operation",
        action="provision",
        permission="workspace:provision",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    return ManagementObservation(
        origin="https://api.example.invalid",
        credential=credential or "sp-bootstrap-read-" + "x" * 40,
        binding=binding,
        target=target,
        namespace="tenant",
        claim="a" * 64,
    )


def document():
    now = datetime.now(UTC)
    return {
        "org_id": "org",
        "workspace_id": "workspace",
        "operation_id": "operation",
        "cluster_arn": "cluster",
        "namespace": "tenant",
        "registration_claim": "a" * 64,
        "registry_ready": True,
        "target_status": "observed_execution_unavailable",
        "last_reconciled": now.isoformat(),
        "lease_expires_at": (now + timedelta(seconds=30)).isoformat(),
    }


def test_current_scoped_observation_is_usable():
    observer().verify(document())


@pytest.mark.parametrize(
    "field",
    [
        "org_id",
        "workspace_id",
        "operation_id",
        "cluster_arn",
        "namespace",
        "registration_claim",
        "target_status",
    ],
)
def test_other_target_claim_or_operation_is_refused(field):
    value = document()
    value[field] = "another"
    with pytest.raises(BootstrapRefused):
        observer().verify(value)


def test_expired_controller_lease_is_not_ready():
    value = document()
    value["lease_expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with pytest.raises(BootstrapRefused):
        observer().verify(value)


def test_observation_resolves_a_fresh_scoped_credential_for_every_read(monkeypatch):
    from superplane_bootstrap import management_observation

    issued, presented = [], []

    def credential():
        token = "sp-bootstrap-read-" + str(len(issued)) + "x" * 40
        issued.append(token)
        return token

    class Transport:
        @contextmanager
        def open(self, request, timeout):
            presented.append(request.headers["Authorization"])
            yield SimpleNamespace(read=lambda limit: json.dumps(document()).encode())

    monkeypatch.setattr(
        management_observation, "build_opener", lambda *handlers: Transport()
    )
    access = observer(credential)
    assert issued == []
    access.observe()
    access.observe()
    assert issued == presented and len(set(presented)) == 2


def test_supplier_cannot_send_a_broad_registry_credential(monkeypatch):
    from superplane_bootstrap import management_observation

    def no_transport(*args):
        pytest.fail("unscoped credential must be refused before transport")

    monkeypatch.setattr(management_observation, "build_opener", no_transport)
    with pytest.raises(BootstrapRefused, match="scoped read token"):
        observer(lambda: "broad-registry-credential" + "x" * 40).observe()
