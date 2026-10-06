"""Database discovery uses app-owned DNS-only pods before credential transport."""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from installation.cluster_probe import ClusterProbe
from installation.config import Refusal
from installation.manifests import dns_egress
from installation.runner import Installer

from .test_cluster_probe import ProbeTools

ENDPOINT = {"Address": "private-db.example.test", "Port": 5432}
ANSWER = {"host": ENDPOINT["Address"], "port": 5432, "addresses": ["10.0.1.2"]}


class DNSProbeTools(ProbeTools):
    def __init__(self, answer=None, *, lookup_exit=0, **kwargs):
        super().__init__(**kwargs)
        self.answer = ANSWER if answer is None else answer
        self.lookup_exit = lookup_exit

    def call(self, args, **kwargs):
        assert "exec" not in args, "Shared-pod exec is forbidden"
        assert "adp-gateway" not in args
        if "create" in args:
            value = json.loads(kwargs["data"])
            if value["kind"] == "Namespace":
                assert not self.objects, "Prior probe must finish UID cleanup first"
        if "logs" in args or ("get" in args and "pod" in args):
            verb = "logs" if "logs" in args else "pod"
            name = args[args.index(verb) + 1]
            command = self.objects["Pod", name]["spec"]["containers"][0]["command"]
            lookup = "def normalize_addresses" in command[2]
            self.pod_exit = self.lookup_exit if lookup else 0
            if "logs" in args:
                self.calls.append((args, kwargs))
                if lookup:
                    output = (
                        self.answer
                        if isinstance(self.answer, str)
                        else json.dumps(self.answer)
                    )
                else:
                    output = json.dumps(
                        {
                            "verified": True,
                            "names": command[-2:],
                            "protocols": ["UDP", "TCP"],
                        }
                    )
                return SimpleNamespace(returncode=0, stdout=output, stderr="")
        return super().call(args, **kwargs)


def created(tools, kind):
    return [
        json.loads(kw["data"])
        for args, kw in tools.calls
        if "create" in args and json.loads(kw["data"])["kind"] == kind
    ]


def test_discovery_uses_qualified_credential_free_dns_only_pods(
    tmp_path, environment, release
):
    environment["cluster_dns_ip"] = "172.20.0.10"
    tools = DNSProbeTools(
        {**ANSWER, "addresses": ["fd00:0:0::2", "10.0.1.2", "fd00::2"]}
    )
    installer = Installer(environment, release, tmp_path, tools)
    installer.secret_values = {"database": "credential-must-not-leave-memory"}
    assert installer.resolve_database_addresses(ENDPOINT) == ["10.0.1.2", "fd00::2"]
    assert not tools.objects
    assert not created(tools, "Secret")
    assert installer.receipt["temporary_preflight"]["cleanup_required"] is False
    assert installer.receipt["management_dns"]["protocols"] == ["UDP", "TCP"]
    assert installer.receipt["database_network_target"]["addresses"] == [
        "10.0.1.2",
        "fd00::2",
    ]
    policies = created(tools, "NetworkPolicy")
    assert len(policies) == 1
    assert policies[0]["spec"]["egress"] == [dns_egress(environment)]
    pods = created(tools, "Pod")
    assert len(pods) == 2
    for pod in pods:
        container = pod["spec"]["containers"][0]
        assert container["env"] == []
        assert container["image"].endswith("@" + release["images"]["superplane-api"])
        assert pod["spec"]["automountServiceAccountToken"] is False
    assert "credential-must-not-leave-memory" not in json.dumps(
        [tools.calls, installer.receipt]
    )
    assert pods[-1]["spec"]["containers"][0]["command"][-2:] == [
        ENDPOINT["Address"],
        "5432",
    ]


@pytest.mark.parametrize(
    "answer",
    [
        {**ANSWER, "addresses": []},
        {**ANSWER, "addresses": ["10.0.1.2", None]},
        {**ANSWER, "addresses": ["10.0.1.2", "bad"]},
        {**ANSWER, "addresses": ["10.0.1.2"] * 17},
        {**ANSWER, "addresses": ["127.0.0.1"]},
        {**ANSWER, "addresses": ["169.254.169.254"]},
        {**ANSWER, "addresses": ["::"]},
        {**ANSWER, "addresses": ["ff02::1"]},
        {**ANSWER, "addresses": ["fd00::2%eth0"]},
        {**ANSWER, "addresses": "10.0.1.2"},
        {**ANSWER, "host": "other.test"},
        {**ANSWER, "port": True},
        [],
        "{bad",
        "x" * 4097,
    ],
)
def test_invalid_result_refuses_and_cleans_up(tmp_path, environment, release, answer):
    tools = DNSProbeTools(answer)
    installer = Installer(environment, release, tmp_path, tools)
    with pytest.raises(Refusal):
        installer.resolve_database_addresses(ENDPOINT)
    assert not tools.objects
    assert installer.receipt["temporary_preflight"]["cleanup_required"] is False
    assert "database_network_target" not in installer.receipt
    assert not created(tools, "Secret")


def test_failed_lookup_never_records_success(tmp_path, environment, release):
    tools = DNSProbeTools(lookup_exit=137)
    installer = Installer(environment, release, tmp_path, tools)
    with pytest.raises(Refusal, match="did not resolve"):
        installer.resolve_database_addresses(ENDPOINT)
    assert not tools.objects
    assert "database_network_target" not in installer.receipt


def test_discovery_cleanup_refuses_foreign_namespace(tmp_path, environment, release):
    tools = DNSProbeTools(replace_namespace=True)
    installer = Installer(environment, release, tmp_path, tools)
    with pytest.raises(Refusal, match="ownership changed"):
        installer.resolve_database_addresses(ENDPOINT)
    assert installer.receipt["temporary_preflight"]["cleanup_required"] is True
    assert "database_network_target" not in installer.receipt
    assert not any("--raw" in args for args, _ in tools.calls)


@pytest.mark.parametrize(
    "endpoint",
    [
        {"Address": "", "Port": 5432},
        {"Address": "a b", "Port": 5432},
        {**ENDPOINT, "Port": True},
        {**ENDPOINT, "Port": 0},
    ],
)
def test_invalid_endpoint_refuses_before_pod_creation(
    tmp_path, environment, release, endpoint
):
    tools = DNSProbeTools()
    installer = Installer(environment, release, tmp_path, tools)
    with pytest.raises(Refusal, match="endpoint is invalid"):
        installer.resolve_database_addresses(endpoint)
    assert not tools.calls


@pytest.mark.parametrize("failure", [False, True])
def test_transported_program_uses_exact_endpoint_and_sanitizes_failures(
    monkeypatch, capsys, failure
):
    import signal
    import socket
    import sys
    from installation import database_dns_probe

    alarms = []
    monkeypatch.setattr(signal, "alarm", alarms.append)
    monkeypatch.setattr(sys, "argv", ["probe", ENDPOINT["Address"], "5432"])

    def lookup(host, port, *, type):
        assert (host, port, type) == (ENDPOINT["Address"], 5432, socket.SOCK_STREAM)
        if failure:
            raise RuntimeError("sensitive resolver detail")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.1.2", port))]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    if failure:
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(str(Path(database_dns_probe.__file__)), run_name="__main__")
        assert exc.value.code == 1
        assert json.loads(capsys.readouterr().out) == {"status": "unavailable"}
    else:
        runpy.run_path(str(Path(database_dns_probe.__file__)), run_name="__main__")
        assert json.loads(capsys.readouterr().out) == ANSWER
        assert alarms == [15, 0]


def test_dns_discovery_cannot_add_database_egress(tmp_path, environment, release):
    probe = ClusterProbe(Installer(environment, release, tmp_path))
    with pytest.raises(Refusal, match="cannot allow database egress"):
        probe.isolate(dns_only=True, database_cidrs=["10.0.1.2/32"])


@pytest.mark.parametrize("path", ["base", "operation", "preflight"])
def test_database_paths_finish_dns_cleanup_before_credential_probe(
    tmp_path, environment, release, monkeypatch, path
):
    """Run maintained entry points through lookup, stopping at credential execution."""
    from contextlib import nullcontext
    from installation import database_preparation, operation_database
    from installation.config import digest

    environment["image_execution"] = "cluster"
    component = operation_database.COMPONENT
    release["images"][component] = "sha256:" + "9" * 64
    release["image_sources"][component] = {
        "registry": f"{environment['account_id']}.dkr.ecr.us-east-1.amazonaws.com",
        "repository": "adp-" + component,
        "source_revision": release["source_revision"],
    }
    tools = DNSProbeTools()
    installer = operation_database.OperationInstaller(
        environment, release, tmp_path, tools
    )
    database = environment["database"]["database"]
    admin_url = (
        f"postgresql://admin:synthetic-admin@{ENDPOINT['Address']}:5432/{database}"
    )
    domain = {
        "runtime-url": f"postgresql://superplane_{environment['environment']}_runtime:synthetic-runtime@{ENDPOINT['Address']}:5432/{database}",
        "ca-pem": "BEGIN CERTIFICATE synthetic",
    }

    def aws(*args, **kwargs):
        if "describe-db-instances" in args:
            value = {
                "DBInstances": [{"Endpoint": ENDPOINT, "DbiResourceId": "db-selected"}]
            }
        elif "describe-db-snapshots" in args:
            value = {
                "DBSnapshots": [
                    {
                        "Status": "available",
                        "DBInstanceIdentifier": environment["database"]["identifier"],
                        "DbiResourceId": "db-selected",
                    }
                ]
            }
        elif "describe-secret" in args:
            value = {
                "Tags": [{"Key": "SuperplaneInstallation", "Value": installer.owner}]
            }
        elif "get-secret-value" in args:
            value = {"SecretString": json.dumps(domain), "VersionId": "fixture-version"}
        else:
            return tools.call(list(args), **kwargs)
        return SimpleNamespace(stdout=json.dumps(value), returncode=0)

    monkeypatch.setattr(installer, "aws", aws)
    monkeypatch.setattr(installer, "target", lambda: None)
    monkeypatch.setattr(installer, "images", lambda: None)
    monkeypatch.setattr(installer, "exclusive", nullcontext)
    monkeypatch.setattr(
        database_preparation.httpx,
        "get",
        lambda *a, **kw: SimpleNamespace(status_code=200, text=domain["ca-pem"]),
    )
    monkeypatch.setattr(
        database_preparation,
        "owned_secret",
        lambda i, key, factory: (factory(), "version"),
    )
    monkeypatch.setattr(
        operation_database,
        "secret_value",
        lambda i, key, factory: (factory(), "version"),
    )
    original_run = ClusterProbe.run

    class CredentialBoundaryReached(Exception):
        pass

    def run(probe, component, command, *, values=None):
        if values:
            namespaces = created(tools, "Namespace")
            assert len(namespaces) == 2
            assert len([args for args, _ in tools.calls if "--raw" in args]) == 1
            assert (
                installer.receipt["database_network_target"]["addresses"]
                == ANSWER["addresses"]
            )
            policy = created(tools, "NetworkPolicy")[-1]
            assert policy["spec"]["egress"][-1] == {
                "to": [{"ipBlock": {"cidr": "10.0.1.2/32"}}],
                "ports": [{"protocol": "TCP", "port": 5432}],
            }
            # Stop before transmitting credentials or making database mutations.
            raise CredentialBoundaryReached
        return original_run(probe, component, command, values=values)

    monkeypatch.setattr(ClusterProbe, "run", run)
    with pytest.raises(CredentialBoundaryReached):
        if path == "base":
            database_preparation._prepare_owned(installer, admin_url)
        elif path == "operation":
            plan = operation_database.preparation_plan(
                installer, "superplane_operations"
            )
            operation_database.execute(installer, plan, digest(plan), admin_url)
        else:
            installer.secret_values = {"database": domain}
            installer.cluster_database_probe(
                {
                    "DATABASE_URL": domain["runtime-url"],
                    "SUPERPLANE_DB_SCHEMA": "superplane",
                    "SUPERPLANE_EXPECTED_DATABASE": database,
                    "SUPERPLANE_DATABASE_CA": domain["ca-pem"],
                },
                ENDPOINT,
            )
    assert not tools.objects
    assert len([args for args, _ in tools.calls if "--raw" in args]) == 2
    assert installer.receipt["temporary_preflight"]["cleanup_required"] is False
    assert not created(tools, "Secret")
