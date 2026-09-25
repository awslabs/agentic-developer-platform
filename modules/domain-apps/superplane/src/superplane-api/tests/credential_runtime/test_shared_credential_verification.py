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

from workspace_provisioning.tests.test_member_credentials import CA


@pytest.mark.parametrize("expect_closed", [False, True])
@pytest.mark.parametrize("scope", ["reader", "mutator"])
@pytest.mark.parametrize(
    "drift",
    [
        None,
        "uid",
        "namespace-mutation",
        "evaluation-error",
        "expired",
        "gate-bypass",
        "missing-job-patch",
        "missing-node-delete",
        "mutation-evaluation-error",
        "mutation-missing-answer",
    ],
)
def test_projected_identity_and_namespace_boundary(
    fixture, tmp_path, monkeypatch, scope, drift, expect_closed
):  # noqa: F811
    from kubernetes import client
    from kubernetes.client.exceptions import ApiException

    if drift == "gate-bypass" and (scope == "reader" or not expect_closed):
        pytest.skip("only bootstrap mutators prove closed admission")
    if (
        drift
        in {
            "missing-job-patch",
            "missing-node-delete",
            "mutation-evaluation-error",
            "mutation-missing-answer",
        }
        and scope == "reader"
    ):
        pytest.skip("reader credentials have no required write permissions")
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
    calls, closed, ca_paths, write_reviews = [], [], [], []

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
                attrs = kwargs["body"]["spec"]["resourceAttributes"]
                if attrs["resource"] == "namespaces":
                    assert (
                        "namespace" not in attrs
                        and attrs["name"] == binding.membership.namespace
                    )
                    return {
                        "status": {
                            "allowed": drift == "namespace-mutation",
                            "evaluationError": "unavailable"
                            if drift == "evaluation-error"
                            else "",
                        }
                    }
                assert scope == "mutator"
                assert attrs["namespace"] == binding.membership.namespace
                assert "name" not in attrs
                action = attrs["group"], attrs["resource"], attrs["verb"]
                write_reviews.append(action)
                if drift == "mutation-missing-answer":
                    return {"status": {}}
                return {
                    "status": {
                        "allowed": not (
                            drift == "missing-job-patch"
                            and action == ("batch", "jobs", "patch")
                            or drift == "missing-node-delete"
                            and action == ("superplane.ai", "superplanenodes", "delete")
                        ),
                        "evaluationError": "unavailable"
                        if drift == "mutation-evaluation-error"
                        else "",
                    }
                }
            assert scope == "mutator" and expect_closed
            assert kwargs["query_params"] == [("dryRun", "All")]
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
        expect_closed=expect_closed,
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
        expected_writes = {
            (group, resource, verb)
            for group, resource in (
                ("", "pods"),
                ("batch", "jobs"),
                ("superplane.ai", "superplanenodes"),
            )
            for verb in ("create", "patch", "delete")
        }
        assert set(write_reviews) == (expected_writes if scope == "mutator" else set())
        pod_creates = [
            path
            for path, method in calls
            if method == "POST" and path.endswith("/pods")
        ]
        assert len(pod_creates) == int(expect_closed and scope == "mutator")
    assert all(not path.exists() for path in ca_paths)
    assert len(closed) == len(ca_paths)
