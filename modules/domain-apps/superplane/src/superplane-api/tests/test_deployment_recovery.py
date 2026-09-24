"""Lost create replies reconcile the owned object without replacing any workload."""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest
from app.services.deployment_identity import (
    OPERATION_ANNOTATION,
    REQUEST_ANNOTATION,
    bind_manifest,
)
from app.services.proxy import (
    ProxyError,
    apply_deployment_via_k8s,
    create_deployment_manifest,
)
from kubernetes.client.rest import ApiException


def manifest():
    return bind_manifest(
        create_deployment_manifest("model", "example/model"),
        org_id="org-a",
        workspace_id="workspace-a",
        operation_id="request-a",
        deployment_id="server-generated-deployment-a",
    )


def created(body):
    result = deepcopy(body)
    result["metadata"].update(uid="provider-uid", generation=1, resourceVersion="123")
    result["spec"]["template"]["spec"]["restartPolicy"] = "Always"
    return result


def test_lost_response_recovers_by_read_without_second_create_or_replace():
    desired = manifest()
    stored = None

    def read(**kwargs):
        if stored is None:
            raise ApiException(status=404)
        return deepcopy(stored)

    def create(*, namespace, body):
        nonlocal stored
        stored = created(body)
        raise ApiException(status=504, reason="response lost after creation")

    transport = MagicMock()
    transport.read_namespaced_deployment.side_effect = read
    transport.create_namespaced_deployment.side_effect = create
    with pytest.raises(ProxyError):
        apply_deployment_via_k8s(transport, desired)
    result = apply_deployment_via_k8s(transport, desired)
    assert result["status"] == "Created"
    assert transport.create_namespaced_deployment.call_count == 1
    transport.replace_namespaced_deployment.assert_not_called()


@pytest.mark.parametrize(
    "change", ["foreign", "request", "spec", "extra-spec", "deleted", "no-uid"]
)
def test_existing_or_concurrently_modified_object_is_never_overwritten(change):
    desired = manifest()
    observed = created(desired)
    if change == "foreign":
        observed["metadata"]["annotations"][OPERATION_ANNOTATION] = "foreign"
    elif change == "request":
        observed["metadata"]["annotations"][REQUEST_ANNOTATION] = "another-request"
    elif change == "spec":
        observed["spec"]["replicas"] = 3
    elif change == "extra-spec":
        observed["spec"]["template"]["spec"]["securityContext"] = {"runAsUser": 0}
        observed["metadata"]["generation"] = 2
    elif change == "deleted":
        observed["metadata"]["deletionTimestamp"] = "2026-09-23T00:00:00Z"
    else:
        del observed["metadata"]["uid"]
    transport = MagicMock()
    transport.read_namespaced_deployment.return_value = observed
    with pytest.raises(ProxyError) as exc:
        apply_deployment_via_k8s(transport, desired)
    assert exc.value.status_code == 409
    transport.create_namespaced_deployment.assert_not_called()
    transport.replace_namespaced_deployment.assert_not_called()


@pytest.mark.parametrize("owned", [True, False])
def test_create_race_observes_winner_and_checks_operation_identity(owned):
    desired = manifest()
    winner = created(desired)
    if not owned:
        winner["metadata"]["annotations"] = {}
    transport = MagicMock()
    transport.read_namespaced_deployment.side_effect = [
        ApiException(status=404),
        winner,
    ]
    transport.create_namespaced_deployment.side_effect = ApiException(status=409)
    if owned:
        assert apply_deployment_via_k8s(transport, desired)["status"] == "Created"
    else:
        with pytest.raises(ProxyError) as exc:
            apply_deployment_via_k8s(transport, desired)
        assert exc.value.status_code == 409
    transport.replace_namespaced_deployment.assert_not_called()


def test_denied_read_is_not_absence():
    transport = MagicMock()
    transport.read_namespaced_deployment.side_effect = ApiException(status=403)
    with pytest.raises(ProxyError) as exc:
        apply_deployment_via_k8s(transport, manifest())
    assert exc.value.status_code == 403
    transport.create_namespaced_deployment.assert_not_called()
    transport.replace_namespaced_deployment.assert_not_called()


def test_unbound_manifest_is_refused_before_any_provider_call():
    transport = MagicMock()
    with pytest.raises(ProxyError) as exc:
        apply_deployment_via_k8s(
            transport, create_deployment_manifest("model", "example/model")
        )
    assert exc.value.status_code == 409
    assert transport.mock_calls == []
