"""Probe credential handling, terminal failures and cleanup ownership."""

import json
from types import SimpleNamespace

import pytest

from installation.cluster_probe import ClusterProbe
from installation.config import Refusal
from installation.runner import Installer


class ProbeTools:
    def __init__(self, *, pod_exit=0, replace_namespace=False):
        self.objects = {}
        self.calls = []
        self.pod_exit = pod_exit
        self.replace_namespace = replace_namespace

    def call(self, args, **kwargs):
        self.calls.append((args, kwargs))
        value, output = {}, None
        if "create" in args:
            value = json.loads(kwargs["data"])
            value["metadata"]["uid"] = "owned-" + value["metadata"]["name"]
            value["metadata"]["resourceVersion"] = "1"
            self.objects[value["kind"], value["metadata"]["name"]] = value
            if value["kind"] == "Pod":
                spec = value["spec"]
                assert spec["automountServiceAccountToken"] is False
                assert not spec.get("hostNetwork") and not spec.get("hostPID")
                assert spec["activeDeadlineSeconds"] <= 300
                assert value["metadata"]["annotations"] == {
                    "karpenter.sh/do-not-disrupt": "true"
                }
                assert all(
                    "value" not in entry for entry in spec["containers"][0]["env"]
                )
        elif "get" in args:
            index = args.index("get")
            kind, name = args[index + 1 : index + 3]
            kind = {"namespace": "Namespace", "pod": "Pod"}[kind]
            value = self.objects.get((kind, name))
            if value is None:
                output = ""
            elif kind == "Namespace" and self.replace_namespace:
                value = {"metadata": {"uid": "foreign", "labels": {}}}
            elif kind == "Pod":
                value = {
                    **value,
                    "status": {
                        "phase": "Succeeded" if self.pod_exit == 0 else "Failed",
                        "containerStatuses": [
                            {"state": {"terminated": {"exitCode": self.pod_exit}}}
                        ],
                    },
                }
        elif "logs" in args:
            output = (
                '{"status":"refused"}' if self.pod_exit else '{"schema":"superplane"}'
            )
        elif "delete" in args:
            if "--raw" in args:
                options = json.loads(kwargs["data"])
                name = args[args.index("--raw") + 1].split("/")[-1]
                assert (
                    options["preconditions"]["uid"]
                    == self.objects["Namespace", name]["metadata"]["uid"]
                )
                assert (
                    options["preconditions"]["resourceVersion"]
                    == self.objects["Namespace", name]["metadata"]["resourceVersion"]
                )
                self.objects.clear()
            else:
                index = args.index("delete")
                kind, name = args[index + 1 : index + 3]
                self.objects.pop(({"pod": "Pod", "secret": "Secret"}[kind], name))
        return SimpleNamespace(
            returncode=0,
            stdout=output if output is not None else json.dumps(value),
            stderr="",
        )


@pytest.mark.parametrize("pod_exit", [0, 2, 137])
def test_probe_has_no_ambient_authority_and_cleans_up(
    tmp_path, environment, release, pod_exit
):
    tools = ProbeTools(pod_exit=pod_exit)
    installer = Installer(environment, release, tmp_path, tools)
    with ClusterProbe(installer) as probe:
        with pytest.raises(Refusal, match="network boundary"):
            probe.run(
                "superplane-api", ["python", "-m", "app.installation", "database"]
            )
        probe.isolate(database_cidrs=["10.0.1.2/32"])
        result = probe.run(
            "superplane-api",
            ["python", "-m", "app.installation", "database"],
            values={"DATABASE_URL": "secret-dsn"},
        )
        assert result.returncode == pod_exit
    assert not tools.objects
    assert installer.receipt["temporary_preflight"]["cleanup_required"] is False
    assert "secret-dsn" not in json.dumps(installer.receipt)
    assert all("secret-dsn" not in json.dumps(args) for args, _ in tools.calls)


def test_cleanup_refuses_a_replaced_namespace(tmp_path, environment, release):
    tools = ProbeTools(replace_namespace=True)
    installer = Installer(environment, release, tmp_path, tools)
    with pytest.raises(Refusal, match="ownership changed"):
        with ClusterProbe(installer):
            pass
    assert not any("delete" in args for args, _ in tools.calls)
    assert installer.receipt["temporary_preflight"]["cleanup_required"] is True


@pytest.mark.parametrize("remote_lock", [False, True])
def test_recovery_cleans_interrupted_probe_before_releasing_lock(
    tmp_path, environment, release, monkeypatch, remote_lock
):
    tools = ProbeTools()
    installer = Installer(environment, release, tmp_path, tools)
    probe = ClusterProbe(installer).__enter__()
    probe.pod("interrupted", "superplane-api", ["python", "-V"])
    installer.receipt["status"] = "failed"
    if remote_lock:
        installer.receipt["remote_lock"] = {
            "bucket": installer.bucket,
            "key": installer.lock_key,
            "etag": '"owned"',
        }
    installer.save()
    monkeypatch.setattr(installer, "target", lambda **_: None)
    monkeypatch.setattr(installer, "existing", lambda _: None)

    installer.recover_lock(installer.run_id)

    assert not tools.objects
    assert installer.receipt["temporary_preflight"]["cleanup_required"] is False
    assert "remote_lock" not in installer.receipt
    if remote_lock:
        verbs = [args for args, _ in tools.calls]
        deleted = next(i for i, args in enumerate(verbs) if "delete-object" in args)
        assert any("--for=delete" in args for args in verbs[:deleted])


@pytest.mark.parametrize("code", [1, 137])
def test_network_process_failure_never_counts_as_network_denial(
    tmp_path, environment, release, monkeypatch, code
):
    installer = Installer(environment, release, tmp_path)
    probe = ClusterProbe(installer)
    calls = []

    def failed(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=code, stdout='{"denied":true}')

    monkeypatch.setattr(installer, "kube", failed)
    result = probe.network_sample("client", "http://10.0.0.1:8080/")
    assert result == {"reachable": False, "denied": False, "process_exit": code}
    assert calls[0]["allow_failure"] is True


@pytest.mark.parametrize(
    "fault",
    [
        "replaced",
        "relabeled",
        "scope",
        "uid",
        "delete",
        "wait",
        "not-deleted",
        "delete-race",
    ],
)
def test_recovery_preserves_lock_and_cleanup_marker_on_failure(
    tmp_path, environment, release, monkeypatch, fault
):
    tools = ProbeTools()
    installer = Installer(environment, release, tmp_path, tools)
    probe = ClusterProbe(installer).__enter__()
    installer.receipt["remote_lock"] = {
        "bucket": installer.bucket,
        "key": installer.lock_key,
        "etag": '"owned"',
    }
    metadata = tools.objects["Namespace", probe.namespace]["metadata"]
    if fault == "replaced":
        metadata["uid"] = "foreign"
    elif fault == "relabeled":
        metadata["labels"] = {}
    elif fault == "scope":
        installer.receipt["temporary_preflight"]["namespace"] = "unrelated"
    elif fault == "uid":
        installer.receipt["temporary_preflight"]["uid"] = ""
    original = tools.call

    def call(args, **kwargs):
        if (fault == "delete" and "delete" in args) or (
            fault == "wait" and "wait" in args
        ):
            raise Refusal("Injected cleanup failure")
        if fault == "not-deleted" and "delete" in args:
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        if fault == "delete-race" and "delete" in args:
            metadata["resourceVersion"] = "2"
            metadata["labels"] = {}
            options = json.loads(kwargs["data"])
            assert options["preconditions"]["resourceVersion"] == "1"
            raise Refusal("Kubernetes version conflict")
        return original(args, **kwargs)

    monkeypatch.setattr(tools, "call", call)
    monkeypatch.setattr(installer, "target", lambda **_: None)
    monkeypatch.setattr(installer, "existing", lambda _: None)
    with pytest.raises(Refusal):
        installer.recover_lock(installer.run_id)
    assert installer.receipt["remote_lock"]["etag"] == '"owned"'
    assert installer.receipt["temporary_preflight"]["cleanup_required"] is True
    assert not any("delete-object" in args for args, _ in tools.calls)


def test_recovery_accepts_already_deleted_probe(
    tmp_path, environment, release, monkeypatch
):
    tools = ProbeTools()
    installer = Installer(environment, release, tmp_path, tools)
    ClusterProbe(installer).__enter__()
    tools.objects.clear()
    monkeypatch.setattr(installer, "target", lambda **_: None)
    monkeypatch.setattr(installer, "existing", lambda _: None)
    installer.recover_lock(installer.run_id)
    assert installer.receipt["temporary_preflight"]["cleanup_required"] is False
    assert not any(
        "delete" in args or "delete-object" in args for args, _ in tools.calls
    )
