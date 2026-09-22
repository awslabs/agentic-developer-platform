"""Exercise the real installer against instrumented external tools, never AWS."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from installation.config import Refusal
from installation.runner import Installer


class ExternalTools:
    def __init__(self, environment, release, failure=None, auto_mode=False):
        self.environment, self.release, self.failure = environment, release, failure
        self.auto_mode = auto_mode
        self.service_cidr = "172.20.0.0/16"
        self.calls, self.objects = [], {}
        self.route = None
        self.route_etag = '"route-0"'
        self.route_version = 0
        self.lock_object = None
        self.lock_etag = '"owner-etag"'
        self.restarts = {}
        # workspace_id may be absent in control-plane-only mode; use a placeholder
        # so the grants array is still structurally valid for observation checks.
        workspace = environment.get("workspace_id")
        grants = [
            {
                "submitter_id": component,
                "credential": component + "-private",
                "signing_key": component + "-key",
                "workspaces": [workspace] if workspace else [],
                "lease_scopes": ["budget_monitor/global"]
                if component == "monitor"
                else (
                    [f"controller_management/{environment['org_id']}"]
                    if environment.get("control_plane_only")
                    else []
                ),
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
            cluster_detail = {
                "arn": f"arn:aws:eks:{env['region']}:{env['account_id']}:cluster/{name}",
                "status": "ACTIVE",
                "endpoint": "https://workspace.example.test"
                if name == env.get("workspace_cluster")
                else "https://management.example.test",
                "certificateAuthority": {"data": "CA"},
            }
            if self.auto_mode and name == env["cluster"]:
                cluster_detail["computeConfig"] = {"enabled": True}
                cluster_detail["kubernetesNetworkConfig"] = {
                    "ipFamily": "ipv4",
                    "serviceIpv4Cidr": self.service_cidr,
                }
            result = {"cluster": cluster_detail}
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
        elif "management-capabilities" in args:
            result = {"controller_management": True, "governed_provisioning": False}
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
            result = {
                "governed_provisioning": not env.get("control_plane_only"),
                "controller_management": True,
            }
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
            if self.auto_mode:
                # EKS Auto Mode: no standalone vpc-cni addon → ResourceNotFoundException
                code, error = 254, "ResourceNotFoundException"
            else:
                result = {
                    "addon": {
                        "status": "ACTIVE",
                        "configurationValues": json.dumps(
                            {"enableNetworkPolicy": "true"}
                        ),
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
                "revision": "016_add_organization_grants" if "exec" in args else None,
            }
        elif "--check-database" == args[-1]:
            result = {
                "dialect": "postgresql",
                "database": env["database"]["database"],
                "schema": env["database"]["skypilot_schema"],
                "tls": True,
                "config_matches": True,
                "tables": ["clusters"],
            }
        elif "readiness" == args[-1]:
            result = {
                "mode": "management" if env.get("control_plane_only") else "full",
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
                        "workspace": env.get("workspace_id"),
                        "cluster_id": env.get("cluster_id"),
                        "reported_at": datetime.now(UTC).isoformat(),
                        "status": "healthy",
                    }
                    for x in ("monitor", "controller")
                },
            }
        elif any("/statusz" in argument for argument in args):
            result = {
                "mode": "management",
                "registry_ready": True,
                "governed_provisioning": False,
                "targets": {},
            }
        elif any("r=httpx.get(" in argument for argument in args):
            result = {
                "domain_auth_enforced": self.failure != "private-health",
                "cognito_enabled": True,
            }
        elif args[0] == "kubectl" and "rollout" in args and "restart" in args:
            name = args[args.index("restart") + 1].split("/")[1]
            self.restarts[name] = self.restarts.get(name, 0) + 1
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
        elif "get-object" in args:
            route = args[args.index("--key") + 1].endswith("/public-route.json")
            value = self.route if route else self.lock_object
            if value is None:
                code, error = 254, "NoSuchKey"
            else:
                Path(args[-1]).write_text(json.dumps(value))
                result = {"ETag": self.route_etag if route else self.lock_etag}
        elif "put-object" in args:
            if args[args.index("--key") + 1].endswith("/public-route.json"):
                if "--if-none-match" in args:
                    assert args[args.index("--if-none-match") + 1] == "*"
                    if self.route is not None:
                        raise Refusal("S3 PreconditionFailed")
                else:
                    assert "--if-match" in args
                    if args[args.index("--if-match") + 1] != self.route_etag:
                        raise Refusal("S3 PreconditionFailed")
                self.route = json.loads(
                    Path(args[args.index("--body") + 1]).read_text()
                )
                self.route_version += 1
                self.route_etag = f'"route-{self.route_version}"'
                result = {"ETag": self.route_etag}
            else:
                assert "--if-none-match" in args
                if self.lock_object is not None:
                    raise Refusal("S3 PreconditionFailed")
                self.lock_object = json.loads(
                    Path(args[args.index("--body") + 1]).read_text()
                )
                result = {"ETag": self.lock_etag}
        elif "delete-object" in args:
            assert args[args.index("--key") + 1].endswith("/installation.lock")
            if (
                self.lock_object is not None
                and args[args.index("--if-match") + 1] != self.lock_etag
            ):
                raise Refusal("S3 PreconditionFailed")
            self.lock_object = None
        elif args[0] == "kubectl" and ("apply" in args or "create" in args):
            for doc in yaml.safe_load_all(kwargs["data"]):
                key = (
                    doc["kind"],
                    doc["metadata"]["name"],
                    doc["metadata"].get("namespace"),
                )
                previous = self.objects.get(key)
                if "create" in args and previous:
                    raise Refusal("Kubernetes AlreadyExists")
                if "apply" in args:
                    for field in ("uid", "resourceVersion"):
                        if field in doc["metadata"] and (
                            previous is None
                            or doc["metadata"][field] != previous["metadata"][field]
                        ):
                            raise Refusal("Kubernetes identity/version conflict")
                doc["metadata"].update(
                    uid=previous["metadata"]["uid"]
                    if previous
                    else "uid-" + str(len(self.objects)),
                    resourceVersion=str(
                        int(previous["metadata"]["resourceVersion"]) + 1
                    )
                    if previous
                    else "1",
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
                result = doc
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
                "poddisruptionbudgets": "PodDisruptionBudget",
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
                elif kind == "replicasets" and "-l" in args:
                    component = args[args.index("-l") + 1].split("=", 1)[1]
                    deployment = self.objects[
                        ("Deployment", component, env["namespace"])
                    ]
                    result = {
                        "items": [
                            {
                                "metadata": {
                                    "uid": f"rs-{component}-{generation}",
                                    "ownerReferences": [
                                        {
                                            "kind": "Deployment",
                                            "controller": True,
                                            "uid": deployment["metadata"]["uid"],
                                        }
                                    ],
                                }
                            }
                            for generation in range(self.restarts.get(component, 0) + 1)
                        ]
                    }
                elif kind == "pods" and "-l" in args:
                    component = args[args.index("-l") + 1].split("=", 1)[1]
                    result = {
                        "items": [
                            {
                                "metadata": {
                                    "uid": f"pod-{component}-{self.restarts.get(component, 0)}",
                                    "ownerReferences": [
                                        {
                                            "kind": "ReplicaSet",
                                            "controller": True,
                                            "uid": f"rs-{component}-{self.restarts.get(component, 0)}",
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                elif kind == "nodeclass" and name == "default" and self.auto_mode:
                    # Auto Mode NodeClass confirms NetworkPolicy enforcement is configured.
                    result = {
                        "apiVersion": "eks.amazonaws.com/v1",
                        "kind": "NodeClass",
                        "metadata": {"name": "default"},
                        "spec": {"networkPolicy": "DefaultAllow"},
                    }
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
            return httpx.Response(
                200,
                json={
                    "version": 2,
                    "transport": "s3-conditional-domain-registration",
                    "configured": True,
                    "cache_seconds": 0,
                },
            )
        if url.endswith("/api/health"):
            return httpx.Response(200)
        if "/api/superplane/v1/" in url and (
            self.route is None or self.route.get("enabled") is not True
        ):
            return httpx.Response(404, json={"detail": "Not found"})
        if "/internal/" in url or "/auth/login" in url:
            return httpx.Response(404)
        if not headers or headers.get("Authorization") != "Bearer verified-user":
            return httpx.Response(401)
        workspace_id = self.environment.get("workspace_id")
        if "/workspaces/" in url and (
            not workspace_id or not url.endswith(workspace_id)
        ):
            return httpx.Response(403)
        if self.failure == "public-verification":
            return httpx.Response(403)
        return httpx.Response(
            200,
            json=(
                {"workspaces": [], "total": 0}
                if url.endswith("/workspaces")
                else {"id": self.environment["org_id"]}
                if url.endswith("/orgs/current")
                else {"id": workspace_id}
            ),
            headers={"X-Superplane-Release": self.installer.release},
        )


def setup(tmp_path, environment, release, monkeypatch, failure=None, auto_mode=False):
    tools = ExternalTools(environment, release, failure, auto_mode=auto_mode)
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
    assert installer.receipt["migration"]["schema"] == "016_add_organization_grants"
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


@pytest.mark.parametrize("auto_mode", [False, True])
def test_resume_reuses_jobs_and_completed_bootstrap_has_no_new_token(
    tmp_path, environment, release, monkeypatch, auto_mode
):
    installer, tools = setup(
        tmp_path, environment, release, monkeypatch, "rollout", auto_mode=auto_mode
    )
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
    assert "cluster_dns_ip" not in resumed.receipt["environment"]
    if auto_mode:
        assert resumed.cluster_dns_ip == "172.20.0.10"
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


@pytest.mark.parametrize("auto_mode", [False, True])
def test_compatible_rollback_verifies_public_release(
    tmp_path, environment, release, monkeypatch, auto_mode
):
    installer, tools = setup(
        tmp_path, environment, release, monkeypatch, auto_mode=auto_mode
    )
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
    if auto_mode:
        assert "cluster_dns_ip" not in rollback.receipt["environment"]
        policies = [
            d
            for d in rollback.docs
            if d["kind"] == "NetworkPolicy" and d["spec"].get("egress")
        ]
        assert len(policies) == 4
        assert all(
            {"ipBlock": {"cidr": "172.20.0.10/32"}} in d["spec"]["egress"][0]["to"]
            for d in policies
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dialect", "sqlite"),
        ("database", "another_database"),
        ("schema", "public"),
        ("tls", False),
        ("tables", []),
        ("config_matches", False),
    ],
)
def test_skypilot_durable_state_refusal_keeps_public_route_disabled(
    tmp_path, environment, release, monkeypatch, field, value
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    original = tools.call

    def wrong_database(args, **kwargs):
        result = original(args, **kwargs)
        if args[-1] == "--check-database":
            observed = json.loads(result.stdout)
            observed[field] = value
            result.stdout = json.dumps(observed)
        return result

    monkeypatch.setattr(tools, "call", wrong_database)
    installer.preflight()
    with pytest.raises(Refusal, match="durable database"):
        installer.execute(installer.receipt["plan_sha256"], "verified-user")
    assert tools.route["enabled"] is False
    assert installer.receipt["status"] == "recovery-required"


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


def test_auto_mode_cluster_passes_network_policy_preflight(
    tmp_path, environment, release, monkeypatch
):
    """EKS Auto Mode path: no vpc-cni addon, computeConfig enabled + NodeClass present."""
    installer, tools = setup(
        tmp_path, environment, release, monkeypatch, auto_mode=True
    )
    installer.preflight()
    installer.execute(installer.receipt["plan_sha256"], "verified-user")
    assert installer.receipt["status"] == "installed-and-verified"
    # Confirm that describe-addon was attempted (with allow_failure) and not that it
    # was skipped — the code must try the managed path first.
    assert any("describe-addon" in a for a, _ in tools.calls)
    # NodeClass was queried as the fallback enforcement evidence.
    assert any("nodeclass" in a for a, _ in tools.calls)


@pytest.mark.parametrize("management_only", [False, True])
def test_fresh_auto_mode_install_discovers_dns_in_manifests_and_probes(
    tmp_path, environment, release, monkeypatch, management_only
):
    from installation.cluster_probe import ClusterProbe

    if management_only:
        environment = _cp_only_environment(environment)
    original = copy.deepcopy(environment)
    installer, tools = setup(
        tmp_path, environment, release, monkeypatch, auto_mode=True
    )
    assert tools.calls == []  # Offline plan never contacts AWS.
    assert installer.cluster_dns_ip is None
    installer.preflight()
    assert environment == original
    assert "cluster_dns_ip" not in installer.receipt["environment"]
    assert (
        installer.receipt["management_dns_configuration"]["resolver"] == "172.20.0.10"
    )
    saved = list(yaml.safe_load_all((tmp_path / "manifests.yaml").read_text()))
    policies = [
        d for d in saved if d["kind"] == "NetworkPolicy" and d["spec"].get("egress")
    ]
    assert len(policies) == 4
    expected_peer = {"ipBlock": {"cidr": "172.20.0.10/32"}}
    assert all(expected_peer in d["spec"]["egress"][0]["to"] for d in policies)

    probe = ClusterProbe(installer)
    observed = []
    monkeypatch.setattr(probe, "policy", lambda name, spec: observed.append(spec))
    probe.isolate(database_cidrs=["10.0.1.2/32"])
    assert expected_peer in observed[0]["egress"][0]["to"]
    commands = []

    def dns_result(component, command):
        commands.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "verified": True,
                    "names": [
                        "kubernetes.default.svc.cluster.local",
                        "db.example.test",
                    ],
                    "resolvers": ["172.20.0.10"],
                    "protocols": ["UDP", "TCP"],
                }
            ),
        )

    monkeypatch.setattr(probe, "run", dns_result)
    probe.prove_dns("db.example.test")
    assert commands[0][3] == "172.20.0.10"

    installer.execute(installer.receipt["plan_sha256"], "verified-user")
    applied = [
        obj
        for (kind, _, _), obj in tools.objects.items()
        if kind == "NetworkPolicy" and obj["spec"].get("egress")
    ]
    assert len(applied) == 4
    assert all(expected_peer in d["spec"]["egress"][0]["to"] for d in applied)


def test_changed_discovered_dns_invalidates_approved_plan(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(
        tmp_path, environment, release, monkeypatch, auto_mode=True
    )
    installer.preflight()
    approved = installer.receipt["plan_sha256"]
    tools.service_cidr = "10.100.0.0/16"
    installer.receipt["management_dns"] = {"verified": True}
    with pytest.raises(Refusal, match="exact current domain Terraform plan"):
        installer.execute(approved, "verified-user")
    assert installer.receipt["plan_sha256"] != approved
    assert "management_dns" not in installer.receipt
    assert installer.cluster_dns_ip == "10.100.0.10"
    assert not any(
        "put-object" in a or "apply" in a or "put-parameter" in a
        for a, _ in tools.calls
    )


def test_auto_mode_missing_nodeclass_refuses_before_mutation(
    tmp_path, environment, release, monkeypatch
):
    """Auto Mode without a default NodeClass must refuse, not proceed."""
    installer, tools = setup(
        tmp_path, environment, release, monkeypatch, auto_mode=True
    )
    original = tools.call

    def no_nodeclass(args, **kwargs):
        # Override: nodeclass get returns not-found regardless of auto_mode
        if args[0] == "kubectl" and "get" in args and "nodeclass" in args:
            from types import SimpleNamespace

            return SimpleNamespace(returncode=1, stdout="", stderr="not found")
        return original(args, **kwargs)

    monkeypatch.setattr(tools, "call", no_nodeclass)
    with pytest.raises(Refusal, match="NodeClass"):
        installer.preflight()
    assert not any("put-object" in a or "apply" in a for a, _ in tools.calls)


def test_neither_addon_nor_auto_mode_refuses_before_mutation(
    tmp_path, environment, release, monkeypatch
):
    """A cluster with no vpc-cni addon and no computeConfig must refuse."""
    installer, tools = setup(
        tmp_path, environment, release, monkeypatch, auto_mode=True
    )
    original = tools.call

    def no_compute_config(args, **kwargs):
        result = original(args, **kwargs)
        import json as _json

        if "describe-cluster" in args:
            val = _json.loads(result.stdout)
            val["cluster"].pop("computeConfig", None)
            result.stdout = _json.dumps(val)
        return result

    monkeypatch.setattr(tools, "call", no_compute_config)
    with pytest.raises(Refusal, match="Auto Mode"):
        installer.preflight()
    assert not any("put-object" in a or "apply" in a for a, _ in tools.calls)


# --- Control-plane-only mode ---


def _cp_only_environment(environment):
    """Strip workspace fields to produce a valid control-plane-only environment."""
    env = copy.deepcopy(environment)
    env["control_plane_only"] = True
    for key in (
        "workspace_cluster",
        "workspace_namespace",
        "workspace_id",
        "cluster_id",
    ):
        env.pop(key, None)
    env.pop("controller_ownership", None)
    env["secrets"] = {
        k: v for k, v in env["secrets"].items() if k != "workspace_access"
    }
    return env


def setup_cp_only(tmp_path, environment, release, monkeypatch, failure=None):
    """Set up a control-plane-only installer with ExternalTools (no workspace fields)."""
    env = _cp_only_environment(environment)
    tools = ExternalTools(env, release, failure)
    installer = Installer(env, release, tmp_path, tools, control_plane_only=True)
    tools.installer = installer
    monkeypatch.setattr("installation.runner.httpx.get", tools.http)
    installer.plan()
    return installer, tools


def test_control_plane_only_preflight_skips_workspace_stage(
    tmp_path, environment, release, monkeypatch
):
    """Preflight in control-plane-only mode must not call workspace-cluster APIs."""
    installer, tools = setup_cp_only(tmp_path, environment, release, monkeypatch)
    installer.preflight()
    assert installer.receipt["status"] == "preflight-passed"
    assert installer.receipt.get("mode") == "control-plane-only"
    # No describe-addon call against workspace_cluster — the workspace stage is skipped.
    argv = "\n".join(" ".join(a) for a, _ in tools.calls)
    # workspace CRD/namespace queries must not have occurred.
    assert "nodepools.superplane.ai" not in argv
    assert "superplanenodes.superplane.ai" not in argv


def test_control_plane_only_execute_completes_without_workspace_credentials(
    tmp_path, environment, release, monkeypatch
):
    """Full execute in control-plane-only mode must succeed without workspace_access secret."""
    installer, tools = setup_cp_only(tmp_path, environment, release, monkeypatch)
    installer.preflight()
    installer.execute(installer.receipt["plan_sha256"], "verified-user")
    assert installer.receipt["status"] == "installed-and-verified"
    assert installer.receipt.get("mode") == "control-plane-only"
    # No superplane-workspace-access secret should be created.
    assert not any(
        k[0] == "Secret" and k[1] == "superplane-workspace-access"
        for k in tools.objects
    )
    # Management controller runs without a workspace credential mount.
    assert any(
        k[0] == "Deployment" and k[1] == "superplane-controller" for k in tools.objects
    )
    # Workspace-specific observation delivery is deferred, not required.
    pv = installer.receipt.get("private_verification", {})
    assert "deferred" in str(pv.get("authenticated_observation_delivery", ""))
    # Public verification records zero workspaces, not a workspace check.
    vf = installer.receipt.get("verification", {})
    assert vf.get("workspace_id") is None
    assert vf["authenticated"] is True and vf["registered_workspaces"] == 0
    assert vf["workspace_execution_ready"] is False
    # ADP health and private-route denial must still be checked.
    assert vf.get("private_routes_denied") is True


def test_control_plane_only_does_not_claim_workspace_observation(
    tmp_path, environment, release, monkeypatch
):
    """Control-plane-only receipt must not claim authenticated workspace observations."""
    installer, _tools = setup_cp_only(tmp_path, environment, release, monkeypatch)
    installer.preflight()
    installer.execute(installer.receipt["plan_sha256"], "verified-user")
    receipt = json.dumps(installer.receipt)
    # The workspace UUID from the full environment fixture must not appear in
    # a control-plane-only receipt; there is no registered workspace.
    assert environment["workspace_id"] not in receipt


@pytest.mark.parametrize("lingering", ["terminating", "stuck", "empty"])
def test_management_restart_waits_for_old_pod_removal(
    tmp_path, environment, release, monkeypatch, lingering
):
    installer, tools = setup_cp_only(tmp_path, environment, release, monkeypatch)
    original = tools.call
    observations = {}

    def with_old_pod(args, **kwargs):
        result = original(args, **kwargs)
        if args[0] == "kubectl" and "get" in args and "pods" in args:
            component = args[args.index("-l") + 1].split("=", 1)[1]
            if tools.restarts.get(component):
                observations[component] = observations.get(component, 0) + 1
                pods = json.loads(result.stdout)
                if lingering == "empty":
                    pods["items"] = []
                elif lingering == "stuck" or observations[component] == 1:
                    pods["items"].append(
                        {
                            "metadata": {
                                "uid": f"pod-{component}-0",
                                "deletionTimestamp": "2026-09-21T08:00:00Z",
                                "ownerReferences": [
                                    {
                                        "kind": "ReplicaSet",
                                        "controller": True,
                                        "uid": f"rs-{component}-0",
                                    }
                                ],
                            }
                        }
                    )
                result.stdout = json.dumps(pods)
        return result

    monkeypatch.setattr(tools, "call", with_old_pod)
    clock = iter(range(10000))
    monkeypatch.setattr("installation.runner.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("installation.runner.time.sleep", lambda _: None)
    installer.preflight()
    if lingering == "terminating":
        installer.execute(installer.receipt["plan_sha256"], "verified-user")
        assert installer.receipt["verification"]["restart_persistence_verified"]
        assert observations == {"superplane-api": 2, "superplane-controller": 2}
        for proof in installer.receipt["management_restart"].values():
            assert proof["after_pod_uids"]
            assert not set(proof["before_pod_uids"]) & set(proof["after_pod_uids"])
    else:
        with pytest.raises(Refusal, match="process replacement not verified"):
            installer.execute(installer.receipt["plan_sha256"], "verified-user")
        assert installer.receipt["status"] == "recovery-required"
        assert installer.receipt["remote_lock"]
        assert not installer.receipt["verification"].get("restart_persistence_verified")
        assert tools.route["enabled"] is False


def test_management_restart_ignores_jobs_and_foreign_deployments(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup_cp_only(tmp_path, environment, release, monkeypatch)
    original = tools.call

    def with_non_service_pods(args, **kwargs):
        result = original(args, **kwargs)
        if args[0] == "kubectl" and "get" in args:
            kind = args[args.index("get") + 1]
            if kind in {"pods", "replicasets"}:
                objects = json.loads(result.stdout)
                foreign = {
                    "metadata": {
                        "uid": "foreign-rs",
                        "ownerReferences": [
                            {
                                "kind": "Deployment",
                                "controller": True,
                                "uid": "foreign-deployment",
                            }
                        ],
                    }
                }
                if kind == "replicasets":
                    objects["items"].append(foreign)
                else:
                    for owner_kind, uid in (
                        ("Job", "bootstrap-job"),
                        ("ReplicaSet", "foreign-rs"),
                    ):
                        objects["items"].append(
                            {
                                "metadata": {
                                    "uid": "pod-" + uid,
                                    "ownerReferences": [
                                        {
                                            "kind": owner_kind,
                                            "controller": True,
                                            "uid": uid,
                                        }
                                    ],
                                }
                            }
                        )
                result.stdout = json.dumps(objects)
        return result

    monkeypatch.setattr(tools, "call", with_non_service_pods)
    installer.preflight()
    installer.execute(installer.receipt["plan_sha256"], "verified-user")
    assert installer.receipt["verification"]["restart_persistence_verified"]
    for name, proof in installer.receipt["management_restart"].items():
        assert proof == {
            "before_pod_uids": [f"pod-{name}-0"],
            "after_pod_uids": [f"pod-{name}-1"],
        }


def test_installer_uses_the_canonical_state_lock_and_checksum(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    installer.terraform()
    initializations = [args for args, _ in tools.calls if "init" in args]
    assert len(initializations) == 1
    assert "-backend-config=dynamodb_table=adp-terraform-locks" in initializations[0]
    assert "-backend-config=encrypt=true" in initializations[0]


@pytest.mark.parametrize("pending_read", [False, True])
def test_refreshed_platform_data_omitted_from_planned_values(
    tmp_path, environment, release, monkeypatch, pending_read
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    original = tools.call

    def refreshed(args, **kwargs):
        result = original(args, **kwargs)
        if "show" in args:
            value = json.loads(result.stdout)
            resources = value["planned_values"]["root_module"]["resources"]
            platform = resources.pop(0)
            value["prior_state"] = {
                "values": {"root_module": {"resources": [platform]}}
            }
            if pending_read:
                value["resource_changes"].append(
                    {"address": platform["address"], "change": {"actions": ["read"]}}
                )
            result.stdout = json.dumps(value)
        return result

    monkeypatch.setattr(tools, "call", refreshed)
    if pending_read:
        with pytest.raises(Refusal, match="unresolved planned read"):
            installer.terraform()
    else:
        installer.terraform()
        assert installer.receipt["plan_sha256"]


@pytest.mark.parametrize("addresses", [["10.0.11.13"], [], ["not-an-address"]])
def test_database_resolves_in_selected_management_vpc(
    tmp_path, environment, release, monkeypatch, addresses
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    calls = []

    def dns(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps(addresses))

    monkeypatch.setattr(installer, "kube", dns)
    endpoint = {"Address": "selected.private.example", "Port": 5432}
    if addresses == ["10.0.11.13"]:
        assert installer.resolve_database_addresses(endpoint) == addresses
        assert calls[0][-2:] == (endpoint["Address"], "5432")
        assert "deployment/bedrockgateway" in calls[0]
    else:
        with pytest.raises(Refusal, match="resolve|invalid address"):
            installer.resolve_database_addresses(endpoint)
