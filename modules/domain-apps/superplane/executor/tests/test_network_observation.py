"""Ordinary pod traffic receipts cannot be replaced by Ready endpoint metadata."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from harness_jobs.identity import OperationRefused
from superplane_executor.network_observation import pod_service, receipt


def payload(source="pod"):
    return {
        "version": 1,
        "nonce": "fresh",
        "source": source,
        "url": "http://service.tenant.svc:8080/",
        "addresses": ["10.11.0.4"],
        "responses": [{"address": "10.11.0.4", "status": 200}],
        "tls_verified": False,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"nonce": "old"},
        {"addresses": ["8.8.8.8"]},
        {"responses": []},
        {"source": "node"},
        {"url": "http://foreign/"},
    ],
)
def test_stale_foreign_or_incomplete_probe_is_refused(change):
    value = payload()
    value.update(change)
    with pytest.raises(OperationRefused):
        receipt(
            json.dumps(value),
            nonce="fresh",
            source="pod",
            endpoint=payload()["url"],
            cidrs=["10.11.0.0/16"],
        )


def test_node_private_api_probe_requires_verified_tls():
    value = payload("node")
    value["url"] = "https://test.eks.amazonaws.com/version"
    with pytest.raises(OperationRefused):
        receipt(
            json.dumps(value),
            nonce="fresh",
            source="node",
            endpoint=value["url"],
            cidrs=["10.11.0.0/16"],
        )
    value["tls_verified"] = True
    value["responses"][0]["status"] = 403
    assert receipt(
        json.dumps(value),
        nonce="fresh",
        source="node",
        endpoint=value["url"],
        cidrs=["10.11.0.0/16"],
    )["tls_verified"]


@pytest.mark.parametrize(
    "wrong", ["node", "uid", "hostNetwork", "allocation", "log", "replacement", None]
)
async def test_pod_network_evidence_binds_uid_allocation_node_and_real_log_path(wrong):
    pod = {
        "metadata": {"uid": "uid", "labels": {"superplane.ai/capacity": "allocation"}},
        "spec": {"nodeName": "remote"},
        "status": {"phase": "Succeeded"},
    }
    if wrong == "node":
        pod["spec"]["nodeName"] = "other"
    if wrong == "uid":
        pod["metadata"]["uid"] = "foreign"
    if wrong == "hostNetwork":
        pod["spec"]["hostNetwork"] = True
    if wrong == "allocation":
        pod["metadata"]["labels"]["superplane.ai/capacity"] = "foreign"
    before = httpx.Response(200, json=pod)
    after = (
        httpx.Response(200, json={"metadata": {"uid": "replacement"}})
        if wrong == "replacement"
        else before
    )
    workspace = SimpleNamespace(
        request=AsyncMock(
            side_effect=[
                before,
                httpx.Response(
                    403 if wrong == "log" else 200, text=json.dumps(payload())
                ),
                after,
            ]
        )
    )
    args = dict(  # noqa: C408 - named probe inputs
        pod_name="probe",
        pod_uid="uid",
        node_name="remote",
        nonce="fresh",
        endpoint=payload()["url"],
        cidrs=["10.11.0.0/16"],
        allocation_label="allocation",
    )
    if wrong:
        with pytest.raises(OperationRefused):
            await pod_service(workspace, None, {"namespace": "tenant"}, **args)
    else:
        result = await pod_service(workspace, None, {"namespace": "tenant"}, **args)
        assert result["api_to_kubelet"]["log_read"] is True
        assert "/log?" in workspace.request.call_args_list[1].args[3]
