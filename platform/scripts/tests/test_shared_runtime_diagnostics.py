"""Offline checks for diagnostic identity, read scope and output suppression."""

import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("runtime_diagnostics", SCRIPTS / "diagnose-shared-runtime.py")
diagnostics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostics)


def identity_responses():
    return [
        {"Account": diagnostics.ACCOUNT},
        {"arn": diagnostics.ARN, "endpoint": "https://reviewed.eks.amazonaws.com", "status": "ACTIVE"},
        "unused kubeconfig command output",
        {"context": diagnostics.ARN, "cluster": diagnostics.ARN, "server": "https://reviewed.eks.amazonaws.com"},
    ]


@pytest.mark.parametrize("mismatch", ["requested_account", "caller_account", "cluster_arn", "kube_server", "kube_cluster"])
def test_identity_mismatch_stops_before_runtime_collection(monkeypatch, tmp_path, mismatch):
    values = identity_responses()
    requested = diagnostics.ACCOUNT
    if mismatch == "requested_account":
        requested = "111111111111"
    elif mismatch == "caller_account":
        values[0]["Account"] = "111111111111"
    elif mismatch == "cluster_arn":
        values[1]["arn"] = "another-cluster"
    else:
        values[3]["server" if mismatch == "kube_server" else "cluster"] = "another-cluster"
    calls = []

    def fake_run(args):
        calls.append(args)
        value = values.pop(0)
        return value if isinstance(value, str) else json.dumps(value)

    monkeypatch.setattr(diagnostics, "run", fake_run)
    monkeypatch.setenv("KUBECONFIG", "original-config")
    with pytest.raises(diagnostics.DiagnosticError, match="account|identity"):
        diagnostics.identity(requested, tmp_path)
    assert not any(args[:3] == ["kubectl", "--request-timeout=30s", "get"] for args in calls)


def test_kubeconfig_is_task_local_and_identity_queries_have_no_secrets(monkeypatch, tmp_path):
    values = identity_responses()
    calls = []

    def fake_run(args):
        calls.append(args)
        value = values.pop(0)
        return value if isinstance(value, str) else json.dumps(value)

    monkeypatch.setattr(diagnostics, "run", fake_run)
    monkeypatch.setenv("KUBECONFIG", "original-config")
    assert diagnostics.identity(diagnostics.ACCOUNT, tmp_path)["cluster_arn"] == diagnostics.ARN
    update = calls[2]
    assert update[update.index("--kubeconfig") + 1] == str(tmp_path / "kubeconfig")
    assert "--raw" not in calls[3]
    assert ".users" not in calls[3][-1] and "certificate" not in calls[3][-1]


def test_collections_are_exact_read_only_status_projections(monkeypatch):
    calls = []
    monkeypatch.setattr(diagnostics, "run", lambda args: calls.append(args) or "[]")
    for kind, namespace in diagnostics.COLLECTIONS:
        assert diagnostics.collect(kind, namespace)["status"] == "observed"
    assert len(calls) == 8
    for args in calls:
        assert args[:3] == ["kubectl", "--request-timeout=30s", "get"]
        assert args[3] in {"pods", "jobs", "nodes", "events"}
        projection = args[args.index("-o") + 1]
        assert projection.startswith("go-template=")
        for forbidden in (".message", ".env", ".annotations", ".data", ".command", ".args", ".image", ".token"):
            assert forbidden not in projection
    with pytest.raises(diagnostics.DiagnosticError, match="unapproved_collection"):
        diagnostics.collect("secrets", "adp-gateway")
    with pytest.raises(diagnostics.DiagnosticError, match="unapproved_collection"):
        diagnostics.collect("pods", "unrelated-namespace")


def test_failed_tool_output_and_timeout_never_escape(monkeypatch):
    hidden = "credential-like-fixture-that-must-not-escape"
    monkeypatch.setattr(diagnostics.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=hidden, stderr="Forbidden " + hidden))
    with pytest.raises(diagnostics.DiagnosticError) as error:
        diagnostics.run(["kubectl"])
    assert str(error.value) == "Forbidden"

    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 40
        raise subprocess.TimeoutExpired(args[0], 40, output=hidden)

    monkeypatch.setattr(diagnostics.subprocess, "run", timeout)
    with pytest.raises(diagnostics.DiagnosticError) as error:
        diagnostics.run(["kubectl"])
    assert str(error.value) == "read_timeout"
    with pytest.raises(diagnostics.DiagnosticError, match="invalid_projected_response"):
        diagnostics.decode(hidden)


def test_missing_evidence_is_preserved_as_unavailable_and_remaining_reads_continue(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(diagnostics, "identity", lambda *a: {"account_id": diagnostics.ACCOUNT})
    calls = []

    def collect(kind, namespace):
        calls.append((kind, namespace))
        if kind == "nodes":
            raise diagnostics.DiagnosticError("Forbidden")
        return {"status": "observed", "items": []}

    monkeypatch.setattr(diagnostics, "collect", collect)
    assert diagnostics.diagnose(diagnostics.ACCOUNT, tmp_path) is False
    data = json.loads((tmp_path / "diagnostics.json").read_text())
    assert len(calls) == 8
    assert data["collections"]["cluster/nodes"] == {"status": "unavailable", "reason": "Forbidden"}
    assert (tmp_path / "diagnostics.json").stat().st_mode & 0o777 == 0o600
    assert json.loads(capsys.readouterr().out) == data


def test_migration_filter_and_collection_truncation_are_explicit(monkeypatch):
    jobs = [{"name": "unrelated"}] + [{"name": f"gateway-migrate-{i:04d}", "active": "<nil>"} for i in range(205)]
    monkeypatch.setattr(diagnostics, "run", lambda _: json.dumps(jobs))
    result = diagnostics.collect("jobs", "adp-gateway")
    assert result["total"] == 205 and result["truncated"] is True
    assert len(result["items"]) == 200 and result["items"][0]["name"] == "gateway-migrate-0005"
    assert result["items"][0]["active"] is None


def test_diagnose_has_independent_lane_and_cannot_select_mutation_flags():
    workflow = (SCRIPTS.parents[1] / ".github/workflows/shared-runtime-maintenance.yml").read_text()
    assert "group: ${{ inputs.stage == 'diagnose' && 'gateway-diagnostics-dev' || 'gateway-release-dev' }}" in workflow
    assert '[[ "$EXECUTE" == false && "$UNLOCK_KNOWN_ORPHAN" == false ]]' in workflow
    assert "if: inputs.stage != 'diagnose'" in workflow
    assert 'python platform/scripts/diagnose-shared-runtime.py' in workflow
