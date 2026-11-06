"""Connectivity uses only the deployed worker's existing narrow read authority."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from l5_services.adapters.transform.connectivity import probe_connectivity
from l5_services.workers.execution import lambda_handler


NODE = "candidate-private"
UID = "11111111-2222-3333-4444-555555555555"
INPUTS = {"node_name": NODE, "node_uid": UID,
          "provider_namespace": "private-provider", "check_namespace": "private-checks"}
PATHS = ["/api/v1/nodes/" + NODE, *[
    f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies/deny-all"
    for namespace in (INPUTS["provider_namespace"], INPUTS["check_namespace"])]]


def observations():
    return [{"metadata": {"name": NODE, "uid": UID},
             "status": {"conditions": [{"type": "Ready", "status": "True"}]}}, *[
        {"metadata": {"name": "deny-all", "namespace": namespace},
         "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}}
        for namespace in (INPUTS["provider_namespace"], INPUTS["check_namespace"])]]


def run(values):
    api = SimpleNamespace(request=Mock(side_effect=values))
    return probe_connectivity(api, **INPUTS), api


def test_probe_reads_exact_scoped_resources_without_promoting_candidate():
    report, api = run(observations())
    assert report == {"schema_version": 1, "outcome": "passed",
        "checks": {"node": "verified", "provider_policy": "verified", "check_policy": "verified"},
        "qualified": False, "native_calls": False, "live_acceptance": "not_evaluated"}
    assert [(c.args, c.kwargs) for c in api.request.call_args_list] == [
        (("GET", path), {"maximum": 65536, "timeout_seconds": 2}) for path in PATHS]
    assert all(value not in json.dumps(report) for value in INPUTS.values())


@pytest.mark.parametrize("change,code", [
    (lambda node: None, "executor_node_missing"),
    (lambda node: {**node, "metadata": {"name": NODE, "uid": "replacement"}}, "executor_node_changed"),
    (lambda node: {**node, "metadata": {**node["metadata"], "deletionTimestamp": "now"}}, "executor_node_not_ready"),
    (lambda node: {**node, "status": {"conditions": [{"type": "Ready", "status": "False"}]}}, "executor_node_not_ready"),
    (lambda node: {**node, "status": None}, "executor_node_not_ready"),
    (lambda node: {"metadata": None}, "cluster_response_invalid"),
])
def test_missing_changed_and_retiring_nodes_cannot_pass(change, code):
    values = observations()
    values[0] = change(values[0])
    report, api = run(values)
    assert report["outcome"] == "blocked" and report["diagnostic"]["code"] == code
    assert report["diagnostic"]["stage"] == "node" and api.request.call_count == 1


@pytest.mark.parametrize("index", [1, 2])
@pytest.mark.parametrize("change,code", [
    (lambda policy: None, "deny_policy_missing"),
    (lambda policy: {**policy, "metadata": {"name": "different", "namespace": "unexpected"}}, "deny_policy_changed"),
    (lambda policy: {**policy, "metadata": {**policy["metadata"], "deletionTimestamp": "now"}}, "deny_policy_changed"),
    (lambda policy: {**policy, "spec": {**policy["spec"], "podSelector": {"matchLabels": {"app": "partial"}}}}, "deny_policy_changed"),
    (lambda policy: {**policy, "spec": {**policy["spec"], "policyTypes": ["Ingress"]}}, "deny_policy_changed"),
    (lambda policy: {**policy, "spec": {**policy["spec"], "ingress": [{}]}}, "deny_policy_changed"),
    (lambda policy: {**policy, "spec": {**policy["spec"], "egress": [{}]}}, "deny_policy_changed"),
])
def test_both_namespaces_require_complete_named_deny_policy(index, change, code):
    values = deepcopy(observations())
    values[index] = change(values[index])
    report, api = run(values)
    assert report["outcome"] == "blocked" and report["diagnostic"]["code"] == code
    assert report["diagnostic"]["stage"] == ("provider_policy" if index == 1 else "check_policy")
    assert api.request.call_count == index + 1


@pytest.mark.parametrize("error,code", [
    (httpx.ConnectTimeout("private-target private-session"), "cluster_api_timeout"),
    (httpx.ConnectError("private-target private-session"), "cluster_api_unreachable"),
    (ValueError("private-target private-session"), "cluster_response_invalid"),
    (RuntimeError("private-target private-session"), "cluster_probe_failed"),
    *[(httpx.HTTPStatusError("private-target private-session",
        request=httpx.Request("GET", "https://private-target.invalid/?private-session"),
        response=httpx.Response(status)), code) for status, code in [
            (401, "cluster_identity_rejected"), (403, "cluster_read_forbidden"), (503, "cluster_api_rejected")]],
])
def test_failed_requests_have_actionable_fixed_diagnostics_without_private_values(error, code):
    report, api = run([observations()[0], error])
    assert report["outcome"] == "blocked" and report["diagnostic"]["code"] == code
    assert report["diagnostic"]["stage"] == "provider_policy"
    assert report["diagnostic"]["next_action"] and api.request.call_count == 2
    assert "private-target" not in json.dumps(report) and "private-session" not in json.dumps(report)


def test_explicit_probe_preserves_default_probe_and_never_dispatches_work():
    probe = Mock(return_value={"outcome": "passed", "qualified": False})
    service = SimpleNamespace(service_id="transform", runtime_connectivity_probe=probe,
                              execute=Mock(side_effect=AssertionError("work must not execute")))
    assert lambda_handler({"l5_runtime_probe": 1}, None, service=service) == {"service_id": "transform"}
    probe.assert_not_called()
    assert lambda_handler({"l5_runtime_probe": 1, "connectivity": "transform"}, None, service=service) == {
        "service_id": "transform", "connectivity": {"outcome": "passed", "qualified": False}}
    probe.assert_called_once_with()
    service.execute.assert_not_called()


def test_overlapping_namespaces_fail_before_cluster_reads():
    api = SimpleNamespace(request=Mock(side_effect=AssertionError("must not read")))
    report = probe_connectivity(api, **{**INPUTS, "check_namespace": INPUTS["provider_namespace"]})
    assert report["diagnostic"]["code"] == "executor_namespaces_overlap"
    api.request.assert_not_called()


@pytest.mark.parametrize("service", [SimpleNamespace(service_id="transform"),
    SimpleNamespace(service_id="security", runtime_connectivity_probe=Mock())])
def test_connectivity_probe_requires_installed_transform_hook(service):
    with pytest.raises(ValueError, match="probe is unavailable"):
        lambda_handler({"l5_runtime_probe": 1, "connectivity": "transform"}, None, service=service)
