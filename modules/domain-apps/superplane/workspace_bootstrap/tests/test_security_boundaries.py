"""Regression tests for execution authorization and isolation proof failures."""

from datetime import UTC, datetime
import json

import pytest
from superplane_bootstrap import imds_probe
from superplane_bootstrap.errors import BootstrapRefused

from .test_adapters import _Scripted, _access
from .test_integration import _FakeCluster, _binding, _run
from .conftest import NAMESPACE


@pytest.mark.parametrize(
    "field,value",
    [
        ("action", "teardown"),
        ("permission", "workspace:read"),
        ("contract_version", "invalid"),
        ("operation_id", ""),
        ("expires_at", datetime(2000, 1, 1, tzinfo=UTC)),
    ],
)
def test_invalid_binding_refuses_before_cluster_io(tmp_path, field, value):
    binding = _binding()
    # Model a corrupted server adapter, not a client allowed to mint a binding.
    object.__setattr__(binding, field, value)
    cluster = _FakeCluster()
    outcome, _ = _run(cluster, tmp_path, binding=binding)
    assert outcome.refusal is not None
    assert not any(command[0] == "kubectl" for command in cluster.commands)


def test_lookalike_binding_is_not_execution_authority(tmp_path):
    cluster = _FakeCluster()
    binding = _binding()
    outcome, _ = _run(cluster, tmp_path, binding={"principal": binding.principal})
    assert outcome.refusal is not None
    assert not any(command[0] == "kubectl" for command in cluster.commands)


def test_production_without_authority_composition_never_mutates(tmp_path):
    cluster = _FakeCluster()
    outcome, store = _run(cluster, tmp_path, authority_factory=None)
    assert outcome.refusal and "composition" in str(outcome.refusal)
    assert not cluster.namespaces and not store.rows


class _Http:
    def __init__(self, status=200, token=b"synthetic-token", failure=None):
        self.status, self.token, self.failure = status, token, failure
        self.requests = []

    def request(self, method, path, headers):
        self.requests.append((method, path, headers))
        if self.failure:
            raise self.failure

    def getresponse(self):
        return self

    def read(self, maximum):
        return self.token[:maximum]

    def close(self):
        pass


@pytest.mark.parametrize("status", [401, 403, 404])
def test_imds_http_denial_proves_reachability(monkeypatch, status, capsys):
    connection = _Http(status=status)
    monkeypatch.setattr(
        imds_probe.http.client, "HTTPConnection", lambda *a, **kw: connection
    )
    assert imds_probe.reachable("169.254.169.254") is True
    assert capsys.readouterr().out == ""


def test_imdsv2_token_then_credential_probe_never_emits_body(monkeypatch, capsys):
    token, credentials = _Http(), _Http(token=b"never-print-credentials")
    connections = iter((token, credentials))
    monkeypatch.setattr(
        imds_probe.http.client, "HTTPConnection", lambda *a, **kw: next(connections)
    )
    assert imds_probe.reachable("fd00:ec2::254") is True
    assert token.requests[0][0] == "PUT"
    assert credentials.requests[0][:2] == (
        "GET",
        "/latest/meta-data/iam/security-credentials/",
    )
    assert credentials.requests[0][2]["X-aws-ec2-metadata-token"] == "synthetic-token"
    assert capsys.readouterr().out == ""


def test_imds_timeout_is_network_isolation(monkeypatch):
    monkeypatch.setattr(
        imds_probe.http.client,
        "HTTPConnection",
        lambda *a, **kw: _Http(failure=TimeoutError()),
    )
    assert imds_probe.reachable("169.254.169.254") is False


@pytest.mark.parametrize("token", [b"", b"x" * 2049, b"invalid token", b"\xff"])
def test_malformed_imds_token_never_counts_as_isolation(monkeypatch, token):
    monkeypatch.setattr(
        imds_probe.http.client, "HTTPConnection", lambda *a, **kw: _Http(token=token)
    )
    with pytest.raises((ValueError, UnicodeError)):
        imds_probe.reachable("169.254.169.254")


@pytest.mark.parametrize(
    "output,failed",
    [("", False), ("ADP_IMDS_ERROR\n", False), ("ADP_IMDS_UNREACHABLE\n", True)],
)
def test_pod_probe_command_failure_is_unanswered(output, failed):
    runner = _Scripted({"run ": output}, fail=("run ",) if failed else ())
    with pytest.raises(BootstrapRefused, match="verified result"):
        _access(runner).imds_reachable_from_tenant_pod(NAMESPACE)


def test_probe_pod_is_restricted_and_requires_both_address_families():
    runner = _Scripted({"run ": "ADP_IMDS_UNREACHABLE\n"})
    assert _access(runner).imds_reachable_from_tenant_pod(NAMESPACE) == {
        "ipv4": False,
        "ipv6": False,
    }
    assert len(runner.calls) == 2
    for args, _ in runner.calls:
        pod = json.loads(args[args.index("--overrides") + 1])
        assert pod["spec"]["automountServiceAccountToken"] is False
        assert pod["spec"]["securityContext"]["runAsNonRoot"] is True
        assert pod["spec"]["containers"][0]["securityContext"]["capabilities"] == {
            "drop": ["ALL"]
        }


def _authorization_replies(answer):
    return {
        "get rolebindings": {"items": []},
        "get serviceaccounts": {"items": []},
        "get clusterrolebindings": {"items": []},
        "create -f -": answer,
    }


@pytest.mark.parametrize(
    "answer",
    [
        "unknown shorthand flag: o",
        "",
        "maybe",
        {"status": {}},
        {"status": {"allowed": False, "evaluationError": "denied"}},
    ],
)
def test_authorization_command_error_is_not_a_denial(answer):
    runner = _Scripted(_authorization_replies(answer))
    with pytest.raises(BootstrapRefused, match="denial was not verified"):
        _access(runner).can_tenant_change_admission_labels(NAMESPACE)


def test_authorization_checks_tenant_group_and_service_account():
    replies = _authorization_replies({"status": {"allowed": False}})
    replies["get rolebindings"] = {
        "items": [
            {
                "subjects": [
                    {"kind": "Group", "name": "tenant-editors"},
                    {
                        "kind": "ServiceAccount",
                        "name": "operator",
                        "namespace": NAMESPACE,
                    },
                ]
            }
        ]
    }
    runner = _Scripted(replies)
    assert _access(runner).can_tenant_change_admission_labels(NAMESPACE) is False
    reviews = [json.loads(data)["spec"] for args, data in runner.calls if data]
    assert any("tenant-editors" in review["groups"] for review in reviews)
    assert any(
        review["user"] == f"system:serviceaccount:{NAMESPACE}:operator"
        for review in reviews
    )
    assert {review["resourceAttributes"]["verb"] for review in reviews} == {
        "patch",
        "update",
    }
    assert all("--as" not in args for args, _ in runner.calls)


def test_authorized_tenant_namespace_patch_refuses_isolation():
    runner = _Scripted(_authorization_replies({"status": {"allowed": True}}))
    assert _access(runner).can_tenant_change_admission_labels(NAMESPACE) is True


def test_role_session_exact_rbac_binding_is_included_in_tenant_proof():
    replies = _authorization_replies({"status": {"allowed": False}})
    actual = "arn:aws:sts::000000000001:assumed-role/editor/real-session"
    replies["get clusterrolebindings"] = {
        "items": [{"subjects": [{"kind": "User", "name": actual}]}]
    }
    runner = _Scripted(replies)
    access = _access(
        runner,
        tenant_identity_reader=lambda: [
            (actual.replace("real-session", "{{SessionName}}"), ("tenant-editors",))
        ],
    )
    assert not access.can_tenant_change_admission_labels(NAMESPACE)
    reviews = [json.loads(data)["spec"] for _, data in runner.calls if data]
    assert any(
        review["user"] == actual and "tenant-editors" in review["groups"]
        for review in reviews
    )


def test_missing_trusted_tenant_identity_inventory_refuses():
    runner = _Scripted(_authorization_replies({"status": {"allowed": False}}))
    with pytest.raises(BootstrapRefused, match="identity inventory is required"):
        _access(runner, tenant_identity_reader=None).can_tenant_change_admission_labels(
            NAMESPACE
        )
