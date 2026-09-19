"""Exercise the real installer against instrumented external tools, never AWS."""

import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import yaml

from installation.config import Refusal
from installation.runner import Installer


class ExternalTools:
    def __init__(self, environment, release, failure=None):
        self.environment, self.release, self.failure = environment, release, failure
        self.calls, self.objects = [], {}
        self.route = None
        workspace = environment["workspace_id"]
        grants = [
            {
                "submitter_id": component,
                "credential": component + "-private",
                "signing_key": component + "-key",
                "workspaces": [workspace],
                "lease_scopes": ["budget_monitor/global"]
                if component == "monitor"
                else [],
            }
            for component in ("monitor", "controller")
        ]
        config = {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "workspace",
            "contexts": [
                {
                    "name": "workspace",
                    "context": {"cluster": "workspace", "user": "controller"},
                }
            ],
            "clusters": [
                {
                    "name": "workspace",
                    "cluster": {
                        "server": "https://workspace.example.test",
                        "certificate-authority-data": "CA",
                    },
                }
            ],
            "users": [{"name": "controller", "user": {"token": "workspace-private"}}],
        }
        self.secrets = {
            "database": {
                key: f"postgresql://{key}:secret@db.example.test:5432/superplane"
                for key in ("runtime-url", "migration-url", "skypilot-url")
            },
            "observation": {
                "submitters": json.dumps(grants),
                "monitor-credential": "monitor-private",
                "monitor-signing-key": "monitor-key",
                "controller-credential": "controller-private",
                "controller-signing-key": "controller-key",
                "skypilot-token": "s" * 32,
            },
            "workspace_access": {"kubeconfig": yaml.safe_dump(config)},
        }

        self.secrets["database"]["ca-pem"] = "test-ca-pem"

    def call(self, args, **kwargs):
        self.calls.append((args, kwargs))
        env = self.environment
        if self.failure and self.failure in args:
            raise Refusal("injected external failure")
        result, text, code, error = {}, None, 0, ""
        if "get-caller-identity" in args:
            result = {"Account": env["account_id"]}
        elif "describe-cluster" in args:
            name = args[args.index("--name") + 1]
            result = {
                "cluster": {
                    "arn": f"arn:aws:eks:{env['region']}:{env['account_id']}:cluster/{name}",
                    "status": "ACTIVE",
                    "endpoint": "https://workspace.example.test"
                    if name == env["workspace_cluster"]
                    else "https://management.example.test",
                    "certificateAuthority": {"data": "CA"},
                }
            }
        elif "config" in args and "view" in args:
            result = {
                "clusters": [{"cluster": {"server": "https://management.example.test"}}]
            }
        elif "rev-parse" in args:
            text = self.release["source_revision"]
        elif "--porcelain" in args:
            text = ""
        elif "describe-images" in args:
            result = {
                "imageDetails": [
                    {
                        "imageDigest": args[-1].split("=", 1)[1],
                        "imageTags": [self.release["source_revision"]],
                    }
                ]
            }
        elif "inspect" in args:
            result = [
                {
                    "Config": {
                        "Labels": {
                            "org.opencontainers.image.revision": self.release[
                                "source_revision"
                            ]
                        }
                    }
                }
            ]
        elif "capabilities" in args:
            result = {
                "capabilities": {
                    x: True
                    for x in (
                        "credential_evidence",
                        "provider_authority",
                        "allocation_inventory",
                        "operation_facade",
                    )
                }
            }
        elif "--installation-preflight" in args:
            result = {"governed_provisioning": True}
        elif "get-secret-value" in args:
            name = args[args.index("--secret-id") + 1]
            kind = next(k for k, v in env["secrets"].items() if v == name)
            result = {
                "SecretString": json.dumps(self.secrets[kind]),
                "VersionId": "version-1",
            }
        elif "describe-db-instances" in args:
            result = {
                "DBInstances": [
                    {"Endpoint": {"Address": "db.example.test", "Port": 5432}}
                ]
            }
        elif "describe-db-snapshots" in args:
            result = {
                "DBSnapshots": [
                    {
                        "Status": "available",
                        "DBInstanceIdentifier": env["database"]["identifier"],
                    }
                ]
            }
        elif "describe-addon" in args:
            result = {
                "addon": {
                    "status": "ACTIVE",
                    "configurationValues": json.dumps({"enableNetworkPolicy": "true"}),
                }
            }
        elif "can-i" in args:
            text = "yes"
        elif "database" == args[-1]:
            result = {
                "schema": kwargs.get("env", {}).get(
                    "SUPERPLANE_DB_SCHEMA", env["database"]["schema"]
                ),
                "database": env["database"]["database"],
                "role": "domain-role",
                "revision": "015_add_adp_org_binding" if "exec" in args else None,
            }
        elif "readiness" == args[-1]:
            result = {
                "release_id": self.installer.release,
                "source_revision": self.release["source_revision"],
                "domain_auth_enforced": True,
                "capabilities": {
                    x: True
                    for x in (
                        "credential_evidence",
                        "provider_authority",
                        "allocation_inventory",
                        "operation_facade",
                    )
                },
                "observations": {
                    x: {
                        "workspace": env["workspace_id"],
                        "cluster_id": env["cluster_id"],
                        "reported_at": datetime.now(UTC).isoformat(),
                        "status": "healthy",
                    }
                    for x in ("monitor", "controller")
                },
            }
        elif "show" in args:
            result = {
                "format_version": "1.2",
                "resource_changes": [],
                "planned_values": {
                    "root_module": {
                        "resources": [
                            {
                                "address": "data.terraform_remote_state.platform",
                                "values": {
                                    "outputs": {
                                        "eks_cluster_name": env["cluster"],
                                        "eks_cluster_arn": f"arn:aws:eks:{env['region']}:{env['account_id']}:cluster/{env['cluster']}",
                                    }
                                },
                            }
                        ]
                    }
                },
            }
        elif "put-object" in args:
            result = {"ETag": '"owner-etag"'}
        elif "get-parameter" in args:
            if self.route:
                result = {"Parameter": {"Value": json.dumps(self.route)}}
            else:
                code, error = 254, "ParameterNotFound"
        elif "put-parameter" in args:
            self.route = json.loads(args[args.index("--value") + 1])
        elif args[0] == "kubectl" and "apply" in args:
            for doc in yaml.safe_load_all(kwargs["data"]):
                key = (
                    doc["kind"],
                    doc["metadata"]["name"],
                    doc["metadata"].get("namespace"),
                )
                previous = self.objects.get(key)
                doc["metadata"].update(
                    uid=previous["metadata"]["uid"]
                    if previous
                    else "uid-" + str(len(self.objects)),
                    resourceVersion="1",
                )
                if doc["kind"] == "Deployment":
                    doc["status"] = {"availableReplicas": 1}
                if doc["kind"] == "Job":
                    doc["status"] = (
                        {"conditions": [{"type": "Failed", "status": "True"}]}
                        if self.failure in {"migration-job", "bootstrap-job"}
                        and (
                            {"migration-job": "migrate", "bootstrap-job": "bootstrap"}[
                                self.failure
                            ]
                        )
                        in doc["metadata"]["name"]
                        else {"conditions": [{"type": "Complete", "status": "True"}]}
                    )
                self.objects[key] = doc
        elif args[0] == "kubectl" and "delete" in args:
            path = args[args.index("--raw") + 1]
            plural, name = path.split("/")[-2:]
            namespace = path.split("/")[-3]
            kinds = {
                "secrets": "Secret",
                "jobs": "Job",
                "deployments": "Deployment",
                "services": "Service",
                "configmaps": "ConfigMap",
                "networkpolicies": "NetworkPolicy",
                "serviceaccounts": "ServiceAccount",
            }
            key = (kinds[plural], name, namespace)
            current = self.objects[key]
            options = json.loads(kwargs["data"])
            assert options["preconditions"]["uid"] == current["metadata"]["uid"]
            assert (
                options["preconditions"]["resourceVersion"]
                == current["metadata"]["resourceVersion"]
            )
            del self.objects[key]
        elif args[0] == "kubectl" and "get" in args:
            if "--raw" in args:
                result = {
                    "domain_auth_enforced": self.failure != "private-health",
                    "cognito_enabled": True,
                }
            else:
                index = args.index("get")
                kind, name = args[index + 1 : index + 3]
                if kind == "deployments":
                    result = {"items": []}
                else:
                    namespace = args[args.index("-n") + 1] if "-n" in args else None
                    result = self.objects.get((kind, name, namespace))
                    if result is None:
                        text = ""
        return SimpleNamespace(
            returncode=code,
            stdout=text if text is not None else json.dumps(result),
            stderr=error,
        )

    def http(self, url, headers=None, **kwargs):
        if url.endswith("/installation-support"):
            return httpx.Response(200, json={"version": 1})
        if url.endswith("/api/health"):
            return httpx.Response(200)
        if "/internal/" in url or "/auth/login" in url:
            return httpx.Response(404)
        if not headers or headers.get("Authorization") != "Bearer verified-user":
            return httpx.Response(401)
        if "/workspaces/" in url and not url.endswith(self.environment["workspace_id"]):
            return httpx.Response(403)
        if self.failure == "public-verification":
            return httpx.Response(403)
        return httpx.Response(
            200,
            json={"id": self.environment["workspace_id"]},
            headers={"X-Superplane-Release": self.installer.release},
        )


def setup(tmp_path, environment, release, monkeypatch, failure=None):
    tools = ExternalTools(environment, release, failure)
    installer = Installer(environment, release, tmp_path, tools)
    tools.installer = installer
    monkeypatch.setattr("installation.runner.httpx.get", tools.http)
    installer.plan()
    return installer, tools


def test_one_command_reaches_all_four_services_and_public_verification(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    installer.preflight()
    installer.execute(installer.receipt["plan_sha256"], "verified-user")
    assert installer.receipt["status"] == "installed-and-verified"
    assert installer.receipt["migration"]["schema"] == "015_add_adp_org_binding"
    assert set(
        installer.receipt["private_verification"]["authenticated_observation_delivery"]
    ) == {"monitor", "controller"}
    assert tools.route["enabled"] is True
    argv = "\n".join(" ".join(a) for a, _ in tools.calls)
    assert "platform/infra" not in argv and "deploy-all" not in argv
    assert "workflow_dispatch" not in argv
    assert (
        json.loads((tmp_path / "terraform/installation.auto.tfvars.json").read_text())[
            "account_id"
        ]
        == environment["account_id"]
    )
    assert "monitor-private" not in argv and "workspace-private" not in argv
    receipt = (tmp_path / "receipt.json").read_text()
    assert "monitor-private" not in receipt and "postgresql://" not in receipt
    assert "verified-user" not in receipt and "verified-user" not in argv
    assert installer.receipt["bootstrap"]["token_secret_removed"] is True
    assert not any(k[0] == "Secret" and "bootstrap" in k[1] for k in tools.objects)
    assert any("--if-match" in a for a, _ in tools.calls)


@pytest.mark.parametrize(
    "failure",
    [
        "migration-job",
        "bootstrap-job",
        "rollout",
        "private-health",
        "public-verification",
    ],
)
def test_partial_failure_cannot_report_installation_success(
    tmp_path, environment, release, monkeypatch, failure
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch, failure)
    # Advance only the test clock; no live process or network is involved.
    clock = iter(range(0, 10000))
    monkeypatch.setattr("installation.runner.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("installation.runner.time.sleep", lambda _: None)
    installer.preflight()
    with pytest.raises(Refusal):
        installer.execute(installer.receipt["plan_sha256"], "verified-user")
    assert installer.receipt["status"] == "recovery-required"
    assert tools.route is None or tools.route["enabled"] is False
    assert not any("delete-object" in a for a, _ in tools.calls)


def test_incompatible_schema_rollback_is_rejected_before_tools(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    previous = copy.deepcopy(installer.receipt)
    previous["status"] = "installed-and-verified"
    previous["release_lock"]["schema"]["observed"]["head"] = "future-destructive-schema"
    from installation.config import digest

    previous["release_id"] = digest(previous["release_lock"])
    with pytest.raises(Refusal, match="schema"):
        installer.rollback(previous, "verified-user")
    assert tools.calls == []


def test_resume_reuses_jobs_and_completed_bootstrap_has_no_new_token(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch, "rollout")
    installer.preflight()
    with pytest.raises(Refusal):
        installer.execute(installer.receipt["plan_sha256"], "verified-user")
    first_jobs = {
        key: obj["metadata"]["uid"]
        for key, obj in tools.objects.items()
        if key[0] == "Job"
    }
    resumed = Installer(environment, release, tmp_path, tools)
    tools.installer = resumed
    resumed.resume(copy.deepcopy(installer.receipt))
    resumed.recover_lock(resumed.run_id)
    tools.failure = None
    resumed.execute(resumed.receipt["plan_sha256"], "verified-user")
    assert resumed.receipt["status"] == "installed-and-verified"
    assert {
        key: obj["metadata"]["uid"]
        for key, obj in tools.objects.items()
        if key[0] == "Job"
    } == first_jobs
    assert not any(k[0] == "Secret" and "bootstrap" in k[1] for k in tools.objects)


@pytest.mark.parametrize("name", ["migrate", "bootstrap"])
def test_lock_recovery_refuses_nonterminal_job(
    tmp_path, environment, release, monkeypatch, name
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch, "rollout")
    installer.preflight()
    with pytest.raises(Refusal):
        installer.execute(installer.receipt["plan_sha256"], "verified-user")
    for key, job in tools.objects.items():
        if key[0] == "Job" and name in key[1]:
            job["status"] = {"active": 1, "failed": 1}
    with pytest.raises(Refusal, match="nonterminal"):
        installer.recover_lock(installer.run_id)
    assert not any("delete-object" in args for args, _ in tools.calls)


def test_cleanup_deletes_only_recorded_uids_and_preserves_secrets(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    installer.preflight()
    installer.execute(installer.receipt["plan_sha256"], "verified-user")
    previous = copy.deepcopy(installer.receipt)
    cleanup = Installer(environment, release, tmp_path / "cleanup", tools)
    cleanup.cleanup(previous)
    assert cleanup.receipt["status"] == "workloads-removed"
    assert {key[0] for key in tools.objects} == {"Namespace", "Secret"}
    assert tools.route["enabled"] is False


def test_cleanup_refuses_replaced_uid(tmp_path, environment, release, monkeypatch):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    installer.preflight()
    installer.execute(installer.receipt["plan_sha256"], "verified-user")
    previous = copy.deepcopy(installer.receipt)
    next(obj for key, obj in tools.objects.items() if key[0] == "Deployment")[
        "metadata"
    ]["uid"] = "replacement"
    with pytest.raises(Refusal, match="replaced"):
        Installer(environment, release, tmp_path / "cleanup", tools).cleanup(previous)
    assert tools.route["enabled"] is False


def test_compatible_rollback_verifies_public_release(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    installer.preflight()
    installer.execute(installer.receipt["plan_sha256"], "verified-user")
    previous = copy.deepcopy(installer.receipt)
    rollback = Installer(environment, release, tmp_path / "rollback", tools)
    tools.installer = rollback
    # A migrated schema exists in the database before rollback.
    original = tools.call

    def migrated(args, **kwargs):
        result = original(args, **kwargs)
        if args[-1] == "database":
            value = json.loads(result.stdout)
            value["revision"] = release["schema"]["observed"]["head"]
            result.stdout = json.dumps(value)
        return result

    monkeypatch.setattr(tools, "call", migrated)
    rollback.rollback(previous, "verified-user")
    assert rollback.receipt["status"] == "installed-and-verified"
    assert rollback.receipt["rollback_of"] == previous["run_id"]
    assert tools.route["enabled"] is True


def test_monitor_not_checked_is_preserved_without_claiming_fleet_health(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    original = tools.call

    def unchecked(args, **kwargs):
        result = original(args, **kwargs)
        if args[-1] == "readiness":
            value = json.loads(result.stdout)
            value["observations"]["monitor"]["status"] = "not_checked"
            result.stdout = json.dumps(value)
        return result

    monkeypatch.setattr(tools, "call", unchecked)
    installer.preflight()
    installer.execute(installer.receipt["plan_sha256"], "verified-user")
    assert installer.receipt["status"] == "installed-and-verified"
    assert (
        installer.receipt["private_verification"]["authenticated_observation_delivery"][
            "monitor"
        ]["status"]
        == "not_checked"
    )


def test_wrong_platform_state_cluster_refuses_before_mutation(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    original = tools.call

    def foreign(args, **kwargs):
        result = original(args, **kwargs)
        if "show" in args:
            value = json.loads(result.stdout)
            value["planned_values"]["root_module"]["resources"][0]["values"]["outputs"][
                "eks_cluster_name"
            ] = "other-management"
            result.stdout = json.dumps(value)
        return result

    monkeypatch.setattr(tools, "call", foreign)
    with pytest.raises(Refusal, match="Platform state"):
        installer.preflight()
    assert not any("put-object" in args or "apply" in args for args, _ in tools.calls)


def test_foreign_public_route_refuses_before_mutation(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    tools.route = {
        "installation_id": "foreign-owner",
        "namespace": environment["namespace"],
    }
    with pytest.raises(Refusal, match="Public route belongs"):
        installer.preflight()
    assert not any(
        "put-object" in args or "apply" in args or "put-parameter" in args
        for args, _ in tools.calls
    )
