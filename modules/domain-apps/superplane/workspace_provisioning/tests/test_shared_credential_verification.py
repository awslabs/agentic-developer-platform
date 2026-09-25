"""Actual projected bytes and live consumer proof, with stateful API transport."""
# ruff: noqa: F811

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest
from superplane_bootstrap.errors import BootstrapRefused

from workspace_provisioning.member_credentials import IssuedCredential
from workspace_provisioning.shared_credential_verification import (
    verify_projected_credential,
)

from .test_member_credentials import CA, fixture  # noqa: F401


@pytest.mark.parametrize("scope", ["reader", "mutator"])
@pytest.mark.parametrize(
    "drift",
    [None, "uid", "namespace-mutation", "evaluation-error", "expired", "gate-bypass"],
)
def test_projected_identity_and_namespace_boundary(
    fixture, tmp_path, monkeypatch, scope, drift
):  # noqa: F811
    from kubernetes import client
    from kubernetes.client.exceptions import ApiException

    if drift == "gate-bypass" and scope == "reader":
        pytest.skip("readers have no Pod create permission")
    f = fixture
    binding = replace(f.binding, scope=scope)
    f.projector.scope = scope
    expiry = datetime.now(UTC) + timedelta(minutes=10)
    credential = IssuedCredential(
        binding, "original-sa-uid", expiry, "private-proof-token", CA
    )
    receipt = f.projector.publish(credential, certificate_authority_data=CA)
    if drift == "expired":
        # Preserve exact projected receipt ownership while expiration advances.
        import workspace_provisioning.shared_credential_verification as verifier

        class Future(datetime):
            @classmethod
            def now(cls, tz=None):
                return expiry + timedelta(seconds=1)

        monkeypatch.setattr(verifier, "datetime", Future)
    calls, closed, ca_paths = [], [], []

    class Api:
        def __init__(self, configuration):
            assert configuration.host == binding.membership.endpoint
            assert configuration.verify_ssl and configuration.proxy is None
            assert configuration.api_key["authorization"] == "private-proof-token"
            assert Path(configuration.ssl_ca_cert).read_bytes() == b"pinned-ca"
            ca_paths.append(Path(configuration.ssl_ca_cert))

        def call_api(self, path, method, **kwargs):
            calls.append((path, method))
            if path.endswith("/selfsubjectreviews"):
                return {
                    "status": {
                        "userInfo": {
                            "uid": "replacement"
                            if drift == "uid"
                            else "original-sa-uid",
                            "username": f"system:serviceaccount:{binding.membership.namespace}:{binding.service_account}",
                        }
                    }
                }
            if method == "GET":
                assert path == f"/api/v1/namespaces/{binding.membership.namespace}/pods"
                return {"kind": "PodList", "items": []}
            if path.endswith("/selfsubjectaccessreviews"):
                assert (
                    kwargs["body"]["spec"]["resourceAttributes"]["resource"]
                    == "namespaces"
                )
                return {
                    "status": {
                        "allowed": drift == "namespace-mutation",
                        "evaluationError": "unavailable"
                        if drift == "evaluation-error"
                        else "",
                    }
                }
            assert scope == "mutator" and kwargs["query_params"] == [("dryRun", "All")]
            if drift == "gate-bypass":
                return {"kind": "Pod"}
            error = ApiException(status=403)
            error.body = json.dumps({"message": "workspace admission is closed"})
            raise error

        def close(self):
            closed.append(True)

    monkeypatch.setattr(client, "ApiClient", Api)
    arguments = dict(
        target=f.issuer.grants.target,
        directory=tmp_path,
        verify=lambda: None,
        expect_closed=True,
    )
    if drift:
        with pytest.raises(BootstrapRefused):
            verify_projected_credential(f.projector, binding, receipt, **arguments)
    else:
        assert (
            verify_projected_credential(f.projector, binding, receipt, **arguments)
            == credential.metadata
        )
        assert any(path.endswith("/selfsubjectreviews") for path, _ in calls)
    assert all(not path.exists() for path in ca_paths)
    assert len(closed) == len(ca_paths)
