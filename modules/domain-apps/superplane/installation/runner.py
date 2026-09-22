"""Synchronous, checked installation stages with durable recovery receipts.

Uses the existing Terraform module and its structural ownership guard directly.
It does not dispatch asynchronous workflows or invoke a platform deployment.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
import ipaddress
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

from .config import (
    COMPONENTS,
    LABEL,
    MODULE,
    Refusal,
    digest,
    identity,
    image,
    require,
    verify_cluster_dns,
)
from .manifests import bootstrap_job, migration_job, render
from .cluster_probe import ClusterProbe

sys.path.insert(0, str(MODULE / "infra/scripts"))
from domain_ownership import validate_plan  # noqa: E402


class Commands:
    def call(self, args, *, data=None, env=None, timeout=120, allow_failure=False):
        try:
            result = subprocess.run(
                args,
                input=data,
                text=True,
                capture_output=True,
                timeout=timeout,
                env={
                    k: v
                    for k, v in (os.environ if env is None else env).items()
                    if k
                    not in {
                        "SUPERPLANE_VERIFICATION_TOKEN",
                        "SUPERPLANE_DATABASE_ADMIN_URL",
                    }
                },
            )
        except (OSError, subprocess.TimeoutExpired):
            raise Refusal(
                f"{Path(args[0]).name} unavailable or timed out; stage did not complete"
            ) from None
        if result.returncode and not allow_failure:
            raise Refusal(
                f"{Path(args[0]).name} failed (exit {result.returncode}); stage did not complete"
            )
        return result


def atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    os.chmod(temporary, 0o600)
    temporary.replace(path)


class Installer:
    def __init__(
        self,
        env,
        lock,
        directory: Path,
        commands=None,
        *,
        control_plane_only: bool = False,
    ):
        self.env, self.lock, self.directory = env, lock, directory
        # control_plane_only: skip workspace stages (workspace(), workspace_access secret).
        # The environment YAML may also declare control_plane_only: true.
        from .config import control_plane_mode

        self.control_plane_only = control_plane_mode(env, control_plane_only)
        self.env = dict(env, control_plane_only=self.control_plane_only)
        env = self.env
        self.commands = commands or Commands()
        self.run_id = uuid.uuid4().hex[:16]
        self.owner = identity(env)
        self.release = digest(lock)
        self.cluster_dns_ip = env.get("cluster_dns_ip")
        self.docs = render(env, lock, control_plane_only=self.control_plane_only)
        self.secret_values = {}
        self.secret_versions = {}
        self.submitter_ids = {}
        self.kubeconfig = directory / "kubeconfig"
        self.route_key = (
            f"domain-routes/{env['environment']}/superplane/public-route.json"
        )
        self.bucket = f"adp-terraform-state-{env['account_id']}"
        self.lock_key = f"{env['environment']}/modules/superplane/installation.lock"
        self.receipt = {
            "started_at": datetime.now(UTC).isoformat(),
            "version": 1,
            "run_id": self.run_id,
            "installation_id": self.owner,
            "environment": env,
            "release_lock": lock,
            "release_id": self.release,
            "status": "planned",
            "completed": [],
            "objects": [],
        }
        if self.control_plane_only:
            self.receipt["mode"] = "control-plane-only"
        self.receipt_path = directory / "receipt.json"

    def aws(self, *args, **kwargs):
        return self.commands.call(
            ["aws", "--region", self.env["region"], "--no-cli-pager", *args], **kwargs
        )

    def kube(self, *args, data=None, **kwargs):
        return self.commands.call(
            [
                "kubectl",
                "--kubeconfig",
                str(self.kubeconfig),
                "--request-timeout=30s",
                *args,
            ],
            data=data,
            **kwargs,
        )

    def json(self, result):
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError):
            raise Refusal("Tool returned an invalid structured response") from None

    def save(self):
        atomic(self.receipt_path, self.receipt)

    def phase(self, name, action):
        self.receipt["stage"] = name
        self.save()
        try:
            result = action()
        except Exception:
            self.receipt["status"] = "failed"
            self.save()
            raise
        self.receipt["completed"].append(name)
        self.save()
        return result

    @property
    def network_environment(self):
        # Discovery must not mutate user input: resume/rollback bind that exact
        # input, while rendered policies and probes need the resolved address.
        if self.cluster_dns_ip is None:
            return self.env
        return dict(self.env, cluster_dns_ip=self.cluster_dns_ip)

    def write_manifests(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.directory / "manifests.yaml").write_text(
            yaml.safe_dump_all(
                [
                    *self.docs,
                    migration_job(self.network_environment, self.lock, self.run_id),
                    bootstrap_job(self.network_environment, self.lock, self.run_id),
                ],
                sort_keys=False,
            )
        )

    def plan(self):
        self.write_manifests()
        if self.control_plane_only:
            self.receipt["actions"] = [
                "verify account/cluster/source/images/production capabilities",
                "verify database/backup ownership and gateway support",
                "review domain Terraform plan",
                "acquire exclusive installation lock",
                "apply approved domain Terraform plan",
                "install domain identities, network boundaries and referenced secrets",
                "migrate owned schema",
                "bind fresh domain tenancy to the verified current ADP administrator",
                "roll out API/controller management/monitor/SkyPilot",
                "verify private services",
                "register public routing",
                "verify public authorization, health and existing ADP availability",
                "workspace activation follows separately with explicit workspace inputs",
            ]
        else:
            self.receipt["actions"] = [
                "verify account/cluster/source/images/production capabilities",
                "verify database/backup/workspace ownership and gateway support",
                "review domain Terraform plan",
                "acquire exclusive installation lock",
                "apply approved domain Terraform plan",
                "install domain identities, network boundaries and referenced secrets",
                "migrate owned schema",
                "bind fresh domain tenancy to the verified current ADP administrator",
                "roll out API/controller/monitor/SkyPilot",
                "verify private services",
                "register public routing",
                "verify public authorization, health and existing ADP availability",
            ]
        self.save()
        return self.receipt

    def target(self, verify_source=True):
        account = self.json(self.aws("sts", "get-caller-identity"))["Account"]
        require(
            account == self.env["account_id"],
            "AWS identity does not match the selected account",
        )
        cluster = self.json(
            self.aws("eks", "describe-cluster", "--name", self.env["cluster"])
        )["cluster"]
        expected_arn = (
            f"arn:aws:eks:{self.env['region']}:{account}:cluster/{self.env['cluster']}"
        )
        require(
            cluster["arn"] == expected_arn and cluster["status"] == "ACTIVE",
            "Wrong or inactive management cluster",
        )
        self.cluster_dns_ip = verify_cluster_dns(self.env, cluster)
        dns_configuration = {
            "cluster_arn": cluster["arn"],
            "mode": "eks-auto-mode" if self.cluster_dns_ip else "pod-dns",
            "resolver": self.cluster_dns_ip,
        }
        # Recheck live resolution on every online preflight, including resume.
        self.receipt.pop("management_dns", None)
        self.receipt["management_dns_configuration"] = dns_configuration
        self.docs = render(
            self.network_environment,
            self.lock,
            control_plane_only=self.control_plane_only,
        )
        self.write_manifests()
        self.aws(
            "eks",
            "update-kubeconfig",
            "--name",
            self.env["cluster"],
            "--kubeconfig",
            str(self.kubeconfig),
        )
        os.chmod(self.kubeconfig, 0o600) if self.kubeconfig.exists() else None
        # Verify the resulting kubeconfig's server, not just its context label.
        config = self.json(self.kube("config", "view", "--minify", "-o", "json"))
        require(
            config["clusters"][0]["cluster"]["server"] == cluster["endpoint"],
            "Kubeconfig targets a different cluster",
        )
        require(
            not config["clusters"][0]["cluster"].get("insecure-skip-tls-verify"),
            "Kubernetes TLS verification is required",
        )
        revision = self.commands.call(
            ["git", "-C", str(MODULE), "rev-parse", "HEAD"]
        ).stdout.strip()
        require(
            not verify_source or revision == self.lock["source_revision"],
            "Checkout does not match release source_revision",
        )
        status = self.commands.call(
            [
                "git",
                "-C",
                str(MODULE),
                "status",
                "--porcelain",
                "--untracked-files=normal",
            ]
        ).stdout.strip()
        require(not status, "Installation requires an unmodified maintained checkout")
        if verify_source:
            self.verify_image_sources()

    def verify_image_sources(self):
        evidence = {}
        for name in COMPONENTS[:-1]:
            revision = self.lock["image_sources"][name]["source_revision"]
            if revision == self.lock["source_revision"]:
                continue
            path = "modules/domain-apps/superplane/src/" + name
            trees = [
                self.commands.call(
                    ["git", "-C", str(MODULE), "rev-parse", sha + ":" + path]
                ).stdout.strip()
                for sha in (revision, self.lock["source_revision"])
            ]
            require(
                len(trees[0]) == 40 and trees[0] == trees[1],
                "Image build context differs from installation source: " + name,
            )
            evidence[name] = {
                "built_revision": revision,
                "build_context_tree": trees[0],
            }
        self.receipt["reused_image_sources"] = evidence
        self.save()

    def images(self):
        if self.env.get("image_execution") == "cluster":
            return self.cluster_images()
        for name in COMPONENTS[:-1]:
            source = self.lock["image_sources"][name]
            data = self.json(
                self.aws(
                    "ecr",
                    "describe-images",
                    "--repository-name",
                    source["repository"],
                    "--image-ids",
                    f"imageDigest={self.lock['images'][name]}",
                )
            )
            details = data.get("imageDetails", [])
            require(
                len(details) == 1
                and details[0]["imageDigest"] == self.lock["images"][name],
                f"Release image unavailable: {name}",
            )
            # Build lanes tag artifacts with the source commit. This verifies the
            # registry observation, not only a source claim in a local lock file.
            require(
                any(
                    tag == source["source_revision"]
                    or tag == source["source_revision"][:12]
                    for tag in details[0].get("imageTags", [])
                ),
                f"Registry does not bind image to source: {name}",
            )
            self.commands.call(
                ["docker", "pull", image(self.lock, name)],
                timeout=self.env["timeout_seconds"],
            )
            inspected = self.json(
                self.commands.call(
                    ["docker", "image", "inspect", image(self.lock, name)]
                )
            )
            require(
                len(inspected) == 1
                and inspected[0]
                .get("Config", {})
                .get("Labels", {})
                .get("org.opencontainers.image.revision")
                == source["source_revision"],
                f"Image OCI provenance does not match source: {name}",
            )
        self.commands.call(
            ["docker", "pull", image(self.lock, "superplane-api")],
            timeout=self.env["timeout_seconds"],
        )
        environment = (
            self.management_probe_environment() if self.control_plane_only else {}
        )
        result = self.commands.call(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--entrypoint",
                "python",
                *[item for key in environment for item in ("--env", key)],
                image(self.lock, "superplane-api"),
                "-m",
                "app.installation",
                "management-capabilities"
                if self.control_plane_only
                else "capabilities",
            ],
            env=dict(os.environ, **environment),
            allow_failure=True,
        )
        if self.control_plane_only:
            require(
                result.returncode == 0
                and self.json(result).get("controller_management") is True,
                "Image does not implement authenticated management mode",
            )
        capabilities = self.json(result).get("capabilities", {})
        required = {
            "credential_evidence",
            "provider_authority",
            "allocation_inventory",
            "operation_facade",
        }
        missing = sorted(key for key in required if capabilities.get(key) is not True)
        require(
            self.control_plane_only or (not missing and result.returncode == 0),
            "Production image lacks trusted capabilities: " + ", ".join(missing),
        )
        controller = self.commands.call(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                image(self.lock, "superplane-controller"),
                "--installation-preflight",
                *(["--management-only"] if self.control_plane_only else []),
            ],
            allow_failure=True,
        )
        require(
            controller.returncode == 0
            and self.json(controller).get(
                "controller_management"
                if self.control_plane_only
                else "governed_provisioning"
            )
            is True,
            "Production controller lacks B's governed execution adapter; direct provisioning is not a fallback",
        )

    def management_probe_environment(self):
        return {
            "SUPERPLANE_MANAGEMENT_ONLY": "true",
            "DOMAIN_AUTH_ENFORCED": "true",
            "COGNITO_ENABLED": "true",
            "COGNITO_ISSUER": self.env["auth"]["issuer"],
            "DOMAIN_AUTH_ALLOWED_CLIENT_IDS": json.dumps(
                self.env["auth"]["client_ids"]
            ),
        }

    def cluster_images(self):
        for name in COMPONENTS[:-1]:
            source = self.lock["image_sources"][name]
            result = self.json(
                self.aws(
                    "ecr",
                    "batch-get-image",
                    "--repository-name",
                    source["repository"],
                    "--image-ids",
                    "imageDigest=" + self.lock["images"][name],
                )
            )
            require(
                not result.get("failures") and len(result.get("images", [])) == 1,
                "Release image is unavailable: " + name,
            )
            raw = result["images"][0]["imageManifest"]
            require(
                "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
                == self.lock["images"][name],
                "Registry manifest does not match pinned digest",
            )
            manifest = json.loads(raw)
            config_digest = manifest["config"]["digest"]
            download = self.json(
                self.aws(
                    "ecr",
                    "get-download-url-for-layer",
                    "--repository-name",
                    source["repository"],
                    "--layer-digest",
                    config_digest,
                )
            )
            response = httpx.get(
                download["downloadUrl"], timeout=30, follow_redirects=False
            )
            require(
                response.status_code == 200
                and "sha256:" + hashlib.sha256(response.content).hexdigest()
                == config_digest,
                "Image config content digest differs",
            )
            config = response.json()
            require(
                config.get("architecture") == "amd64"
                and config.get("os") == "linux"
                and config.get("config", {})
                .get("Labels", {})
                .get("org.opencontainers.image.revision")
                == source["source_revision"],
                "Image OCI provenance does not match source: " + name,
            )
        with ClusterProbe(self) as probe:
            probe.prove_network_policy()
            probe.isolate()
            action = (
                "management-capabilities" if self.control_plane_only else "capabilities"
            )
            api = probe.run(
                "superplane-api",
                ["python", "-m", "app.installation", action],
                values=self.management_probe_environment()
                if self.control_plane_only
                else None,
            )
            observed = self.json(api)
            require(
                api.returncode == 0
                and (
                    observed.get("controller_management") is True
                    if self.control_plane_only
                    else len(observed.get("capabilities", {})) == 4
                    and all(observed["capabilities"].values())
                ),
                "API production capability preflight failed",
            )
            controller = probe.run(
                "superplane-controller",
                [
                    "/superplane-controller",
                    "--installation-preflight",
                    *(["--management-only"] if self.control_plane_only else []),
                ],
            )
            require(
                controller.returncode == 0
                and self.json(controller).get(
                    "controller_management"
                    if self.control_plane_only
                    else "governed_provisioning"
                )
                is True,
                "Controller image lacks the selected runtime integration",
            )

    def secrets(self):
        for kind, name in self.env["secrets"].items():
            version = self.receipt.get("secret_versions", {}).get(kind)
            extra = ["--version-id", version] if version else []
            result = self.json(
                self.aws(
                    "secretsmanager", "get-secret-value", "--secret-id", name, *extra
                )
            )
            try:
                value = json.loads(result["SecretString"])
            except (KeyError, ValueError):
                raise Refusal(
                    f"Secret must contain the documented JSON fields: {kind}"
                ) from None
            self.secret_values[kind] = value
            self.secret_versions[kind] = result["VersionId"]
        required = {
            "database": {"runtime-url", "migration-url", "skypilot-url", "ca-pem"},
            "observation": {
                "submitters",
                "monitor-credential",
                "monitor-signing-key",
                "controller-credential",
                "controller-signing-key",
                "skypilot-token",
            },
        }
        if not self.control_plane_only:
            # workspace_access is only required for full installation; it names the
            # credential used by the workspace controller and must not be loaded
            # when the workspace cluster has not yet been provisioned.
            required["workspace_access"] = {"kubeconfig"}
        for kind, keys in required.items():
            require(
                set(self.secret_values[kind]) == keys
                and all(
                    isinstance(v, str) and v for v in self.secret_values[kind].values()
                ),
                f"Missing or unexpected fields in {kind} secret",
            )
        observation = self.secret_values["observation"]
        require(
            len(observation["skypilot-token"]) >= 32,
            "SkyPilot service credential is too short",
        )
        grants = json.loads(observation["submitters"])
        require(
            isinstance(grants, list) and len(grants) == 2,
            "Observation secret must bind exactly the monitor and controller",
        )
        for component in ("monitor", "controller"):
            selected = [
                g
                for g in grants
                if g.get("credential") == observation[component + "-credential"]
            ]
            if not self.control_plane_only:
                require(
                    len(selected) == 1
                    and selected[0].get("workspaces") == [self.env["workspace_id"]]
                    and selected[0].get("signing_key")
                    == observation[component + "-signing-key"],
                    "Observation grants must match the exact workspace and service credential",
                )
            else:
                # In control-plane-only mode the workspace UUID is not yet selected;
                # verify only that the credential pair exists and is non-empty.
                require(
                    len(selected) == 1
                    and selected[0].get("workspaces") == []
                    and selected[0].get("signing_key")
                    == observation[component + "-signing-key"],
                    f"Observation secret must contain the {component} credential pair",
                )
            self.submitter_ids[component] = (
                selected[0].get("submitter_id") if selected else None
            )
            scopes = ["budget_monitor/global"] if component == "monitor" else []
            if component == "controller" and self.control_plane_only:
                scopes = [f"controller_management/{self.env['org_id']}"]
            require(
                selected[0].get("lease_scopes", []) == scopes,
                "Observation lease scope does not match the service",
            )
        require(
            len(set(self.submitter_ids.values())) == 2
            and all(
                isinstance(x, str) and x.strip() for x in self.submitter_ids.values()
            ),
            "Monitor and controller require distinct nonempty submitter identities",
        )
        require(
            observation["monitor-credential"] != observation["controller-credential"],
            "Monitor and controller require distinct service identities",
        )
        if not self.control_plane_only:
            # Parse workspace credentials without ever writing an unvalidated exec
            # plugin to disk.  Only static bearer/certificate auth is supported.
            workspace = yaml.safe_load(
                self.secret_values["workspace_access"]["kubeconfig"]
            )
            require(
                isinstance(workspace, dict)
                and len(workspace.get("clusters", [])) == 1
                and len(workspace.get("users", [])) == 1,
                "Workspace kubeconfig must name one cluster and identity",
            )
            user = workspace["users"][0]["user"]
            require(
                set(user) <= {"token", "client-certificate-data", "client-key-data"}
                and (
                    bool(user.get("token"))
                    or bool(
                        user.get("client-certificate-data")
                        and user.get("client-key-data")
                    )
                ),
                "Workspace kubeconfig cannot execute plugins or read local files",
            )
            cluster = self.json(
                self.aws(
                    "eks", "describe-cluster", "--name", self.env["workspace_cluster"]
                )
            )["cluster"]
            selected_kube = workspace["clusters"][0]["cluster"]
            require(
                set(selected_kube) <= {"server", "certificate-authority-data"},
                "Workspace kubeconfig cannot override TLS identity or proxy transport",
            )
            contexts = workspace.get("contexts", [])
            require(
                len(contexts) == 1
                and workspace.get("current-context") == contexts[0]["name"]
                and contexts[0]["context"].get("cluster")
                == workspace["clusters"][0]["name"]
                and contexts[0]["context"].get("user") == workspace["users"][0]["name"],
                "Workspace context must bind the checked cluster and identity",
            )
            require(
                selected_kube.get("server") == cluster["endpoint"]
                and selected_kube.get("certificate-authority-data")
                == cluster["certificateAuthority"]["data"]
                and not selected_kube.get("insecure-skip-tls-verify"),
                "Workspace credential target/TLS does not match the selected EKS cluster",
            )
        self.receipt["secret_versions"] = self.secret_versions
        for doc in self.docs:
            if doc["kind"] == "Deployment":
                doc["spec"]["template"]["metadata"]["annotations"][
                    "adp.aws-e.io/secret-versions"
                ] = digest(self.secret_versions)

    def workspace(self):
        operator_config = self.directory / "workspace-operator-kubeconfig"
        self.aws(
            "eks",
            "update-kubeconfig",
            "--name",
            self.env["workspace_cluster"],
            "--kubeconfig",
            str(operator_config),
        )
        command = [
            "kubectl",
            "--kubeconfig",
            str(operator_config),
            "--request-timeout=30s",
        ]
        for name in ("nodepools.superplane.ai", "superplanenodes.superplane.ai"):
            self.commands.call([*command, "get", "crd", name, "-o", "name"])
        self.commands.call(
            [
                *command,
                "get",
                "namespace",
                self.env["workspace_namespace"],
                "-o",
                "name",
            ]
        )
        deployments = self.json(
            self.commands.call(
                [*command, "get", "deployments", "--all-namespaces", "-o", "json"]
            )
        )
        require(
            not any(
                "superplane-controller" in c.get("image", "")
                for d in deployments.get("items", [])
                for c in d.get("spec", {})
                .get("template", {})
                .get("spec", {})
                .get("containers", [])
            ),
            "An existing workspace controller must complete its explicit handover before installation",
        )
        credential = self.secret_values["workspace_access"]["kubeconfig"]
        for verb, resource in (
            ("list", "nodes"),
            ("watch", "pods"),
            ("watch", "nodepools.superplane.ai"),
            ("watch", "superplanenodes.superplane.ai"),
            ("create", "leases.coordination.k8s.io"),
            ("update", "leases.coordination.k8s.io"),
        ):
            result = self.commands.call(
                [
                    "kubectl",
                    "--kubeconfig",
                    "/dev/stdin",
                    "--request-timeout=30s",
                    "auth",
                    "can-i",
                    verb,
                    resource,
                    "-n",
                    self.env["workspace_namespace"],
                ],
                data=credential,
            )
            require(
                result.stdout.strip() == "yes",
                "Workspace controller credential lacks required scoped permissions",
            )

    def management_network_policy(self):
        # Verify NetworkPolicy configuration on the management cluster. The
        # cluster image executor additionally exercises actual packet enforcement.
        # - Standard managed VPC CNI: addon ACTIVE with enableNetworkPolicy enabled.
        # - EKS Auto Mode: no standalone vpc-cni addon (ResourceNotFoundException);
        #   verify computeConfig.enabled and that the default NodeClass declares
        #   a networkPolicy spec field, proving enforcement is configured.
        cni_result = self.aws(
            "eks",
            "describe-addon",
            "--cluster-name",
            self.env["cluster"],
            "--addon-name",
            "vpc-cni",
            allow_failure=True,
        )
        if cni_result.returncode == 0:
            addon = self.json(cni_result)["addon"]
            configuration = json.loads(addon.get("configurationValues") or "{}")
            require(
                addon.get("status") == "ACTIVE"
                and configuration.get("enableNetworkPolicy") in (True, "true"),
                "The supported VPC CNI NetworkPolicy enforcement must be enabled before installation",
            )
        else:
            # No managed addon — check whether Auto Mode is active and NetworkPolicy
            # is configured via the platform NodeClass rather than the VPC CNI addon.
            cluster_detail = self.json(
                self.aws("eks", "describe-cluster", "--name", self.env["cluster"])
            )["cluster"]
            require(
                cluster_detail.get("computeConfig", {}).get("enabled") is True,
                "Management cluster neither has the managed VPC CNI addon"
                " nor uses EKS Auto Mode; NetworkPolicy enforcement cannot be verified",
            )
            node_class_result = self.kube(
                "get",
                "nodeclass",
                "default",
                "-o",
                "json",
                allow_failure=True,
            )
            require(
                node_class_result.returncode == 0,
                "EKS Auto Mode requires a ready NodeClass default for NetworkPolicy enforcement",
            )
            node_class = self.json(node_class_result)
            require(
                "networkPolicy" in node_class.get("spec", {}),
                "EKS Auto Mode NodeClass default does not declare networkPolicy;"
                " enforcement capability cannot be confirmed",
            )

    def database(self):
        roles = {
            urlsplit(value).username
            for key, value in self.secret_values["database"].items()
            if key.endswith("-url")
        }
        require(
            len(roles) == 3 and None not in roles,
            "API runtime, migration and SkyPilot require distinct scoped database roles",
        )
        instances = self.json(
            self.aws(
                "rds",
                "describe-db-instances",
                "--db-instance-identifier",
                self.env["database"]["identifier"],
            )
        )["DBInstances"]
        require(len(instances) == 1, "Database instance identity is ambiguous")
        endpoint = instances[0]["Endpoint"]
        backup = self.json(
            self.aws(
                "rds",
                "describe-db-snapshots",
                "--db-snapshot-identifier",
                self.env["database"]["backup_id"],
            )
        )["DBSnapshots"]
        require(
            len(backup) == 1
            and backup[0]["Status"] == "available"
            and backup[0]["DBInstanceIdentifier"] == self.env["database"]["identifier"],
            "Restorable backup does not match the selected database",
        )
        for key, schema in (
            ("runtime-url", self.env["database"]["schema"]),
            ("migration-url", self.env["database"]["schema"]),
            ("skypilot-url", self.env["database"]["skypilot_schema"]),
        ):
            url = self.secret_values["database"][key]
            parsed = urlsplit(url)
            require(
                parsed.hostname == endpoint["Address"]
                and (parsed.port or 5432) == endpoint["Port"]
                and parsed.path == "/" + self.env["database"]["database"],
                "Database credential targets a different instance or database",
            )
            require(
                parsed.scheme == "postgresql"
                and bool(parsed.username)
                and bool(parsed.password)
                and not parsed.query
                and not parsed.fragment,
                f"{key} must be a PostgreSQL URL without driver/query overrides; TLS is enforced using the selected CA",
            )
            runtime = dict(
                os.environ,
                DATABASE_URL=url.replace("postgresql://", "postgresql+asyncpg://", 1),
                SUPERPLANE_DB_SCHEMA=schema,
                SUPERPLANE_EXPECTED_DATABASE=self.env["database"]["database"],
                SUPERPLANE_DATABASE_CA=self.secret_values["database"]["ca-pem"],
            )
            if self.env.get("image_execution") == "cluster":
                result = self.cluster_database_probe(runtime, endpoint)
            else:
                result = self.commands.call(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--entrypoint",
                        "python",
                        "--env",
                        "DATABASE_URL",
                        "--env",
                        "SUPERPLANE_DB_SCHEMA",
                        "--env",
                        "SUPERPLANE_EXPECTED_DATABASE",
                        "--env",
                        "SUPERPLANE_DATABASE_CA",
                        image(self.lock, "superplane-api"),
                        "-m",
                        "app.installation",
                        "database",
                    ],
                    env=runtime,
                )
            require(result.returncode == 0, "Packaged database boundary check failed")
            observed = self.json(result)
            require(
                observed.get("schema") == schema
                and observed.get("database") == self.env["database"]["database"],
                "Database observation does not match the declared boundary",
            )
            self.receipt.setdefault("database_observations", {})[key] = observed

    def resolve_database_addresses(self, endpoint):
        # Private RDS DNS belongs to the selected VPC, not the operator laptop.
        # The existing ADP API provides a read-only DNS lookup; no credential is read.
        program = (
            "import json,socket,sys; "
            "print(json.dumps(sorted({r[4][0] for r in socket.getaddrinfo("
            "sys.argv[1],int(sys.argv[2]),type=socket.SOCK_STREAM)})))"
        )
        addresses = self.json(
            self.kube(
                "exec",
                "deployment/bedrockgateway",
                "-n",
                self.env.get("gateway_namespace", "adp"),
                "-c",
                "bedrockgateway",
                "--",
                "python",
                "-c",
                program,
                endpoint["Address"],
                str(endpoint["Port"]),
            )
        )
        require(
            isinstance(addresses, list) and 0 < len(addresses) <= 16,
            "Selected database endpoint did not resolve in the management VPC",
        )
        try:
            addresses = sorted(
                {str(ipaddress.ip_address(a)) for a in addresses if isinstance(a, str)}
            )
        except ValueError:
            raise Refusal("Database DNS returned an invalid address") from None
        require(bool(addresses), "Database DNS returned no usable address")
        self.receipt["database_network_target"] = {
            "host": endpoint["Address"],
            "addresses": addresses,
            "source": "management-vpc",
        }
        self.save()
        return addresses

    def cluster_database_probe(self, runtime, endpoint):
        addresses = self.resolve_database_addresses(endpoint)
        with ClusterProbe(self) as probe:
            probe.isolate(
                database_cidrs=[
                    address + ("/128" if ":" in address else "/32")
                    for address in addresses
                ],
                database_port=endpoint["Port"],
            )
            if not self.receipt.get("management_dns", {}).get("verified"):
                probe.prove_dns(endpoint["Address"])
            return probe.run(
                "superplane-api",
                ["python", "-m", "app.installation", "database"],
                values={
                    key: runtime[key]
                    for key in (
                        "DATABASE_URL",
                        "SUPERPLANE_DB_SCHEMA",
                        "SUPERPLANE_EXPECTED_DATABASE",
                        "SUPERPLANE_DATABASE_CA",
                    )
                },
            )

    def gateway(self):
        self.check_route_owner()
        response = httpx.get(
            self.env["origin"] + "/api/superplane/installation-support",
            timeout=20,
            follow_redirects=False,
        )
        require(
            response.status_code == 200
            and response.json().get("version") == 2
            and response.json().get("transport") == "s3-conditional-domain-registration"
            and response.json().get("configured") is True,
            "Existing Gateway needs the configured conditional U23 route transport before domain installation",
        )
        cache_seconds = response.json().get("cache_seconds")
        require(
            type(cache_seconds) is int and 0 <= cache_seconds <= 30,
            "Gateway must advertise a bounded route cache lifetime",
        )
        self.gateway_cache_seconds = cache_seconds
        # Existing ADP health is observed now and after route installation/removal.
        response = httpx.get(
            self.env["origin"] + "/api/health", timeout=20, follow_redirects=False
        )
        require(response.status_code == 200, "Existing ADP health is not available")

    def terraform(self):
        tf = self.directory / "terraform"
        tf.mkdir(exist_ok=True)
        # Copy only the maintained module files: Terraform's working data and
        # reviewed plan stay in the private run directory, not the checkout.
        for source in (MODULE / "infra/control-plane").glob("*.tf"):
            # The module's release lock is a relative path. Keep its exact bytes
            # and resolve that one expression against the maintained source.
            text = source.read_text().replace(
                "${path.module}/../../releases/superplane.lock.yaml",
                str(MODULE / "releases/superplane.lock.yaml"),
            )
            (tf / source.name).write_text(text)
        variables = {
            "environment": self.env["environment"],
            "account_id": self.env["account_id"],
            "aws_region": self.env["region"],
            "namespace": self.env["namespace"],
            "skypilot_namespace": self.env["skypilot_namespace"],
            "database_schema": self.env["database"]["schema"],
            "database_secret_name": self.env["secrets"]["database"],
            "jwt_secret_name": self.env["secrets"]["observation"],
            "cors_allowed_origins": [self.env["origin"]],
            # workspace_cluster_context is deferred in control-plane-only mode;
            # the Terraform module must treat an absent/null value as no-op.
            "workspace_cluster_context": ""
            if self.control_plane_only
            else self.env["workspace_cluster"],
        }
        atomic(tf / "installation.auto.tfvars.json", variables)
        prefix = ["terraform", f"-chdir={tf}"]
        self.commands.call(
            [
                *prefix,
                "init",
                "-input=false",
                f"-backend-config=bucket={self.bucket}",
                f"-backend-config=key={self.env['environment']}/modules/superplane/terraform.tfstate",
                f"-backend-config=region={self.env['region']}",
                "-backend-config=encrypt=true",
                "-backend-config=dynamodb_table=adp-terraform-locks",
            ]
        )
        self.commands.call(
            [*prefix, "plan", "-input=false", "-out=installation.tfplan"],
            timeout=self.env["timeout_seconds"],
        )
        plan = self.json(
            self.commands.call([*prefix, "show", "-json", "installation.tfplan"])
        )
        report = validate_plan(
            plan, account_id=self.env["account_id"], environment=self.env["environment"]
        )
        require(
            report.ok and not report.has_destructive_changes,
            "Terraform plan changes unknown ownership or deletes resources; installation refused",
        )
        address = "data.terraform_remote_state.platform"
        resources = (
            plan.get("planned_values", {}).get("root_module", {}).get("resources", [])
        )
        platform_entry = next(
            (r for r in resources if r.get("address") == address), None
        )
        if platform_entry is None:
            # Terraform can omit unchanged data from planned_values. Its prior_state
            # is the refreshed state for this plan, usable only without a pending read.
            changes = [
                r
                for r in plan.get("resource_changes", [])
                if r.get("address") == address
            ]
            require(
                all(r.get("change", {}).get("actions") == ["no-op"] for r in changes),
                "Platform state has an unresolved planned read",
            )
            resources = (
                plan.get("prior_state", {})
                .get("values", {})
                .get("root_module", {})
                .get("resources", [])
            )
            platform_entry = next(
                (r for r in resources if r.get("address") == address), {}
            )
        platform = platform_entry.get("values", {}).get("outputs", {})
        require(
            platform.get("eks_cluster_name") == self.env["cluster"]
            and platform.get("eks_cluster_arn")
            == f"arn:aws:eks:{self.env['region']}:{self.env['account_id']}:cluster/{self.env['cluster']}",
            "Platform state does not match the selected management cluster",
        )
        binding = {
            "source_revision": self.lock["source_revision"],
            "release_id": self.release,
            "environment": self.env,
            "resource_changes": plan.get("resource_changes", []),
            "secret_versions": self.secret_versions,
            "management_dns_configuration": self.receipt.get(
                "management_dns_configuration"
            ),
        }
        self.receipt["plan_sha256"] = digest(binding)
        self.save()

    def preflight(self):
        # workspace() checks workspace CRDs, namespace existence and controller
        # credential scope.  These are deferred in control-plane-only mode because
        # the workspace cluster does not yet exist at this stage.
        stages = ["target", "management_network_policy", "images", "secrets"]
        if not self.control_plane_only:
            stages.append("workspace")
        stages.extend(["database", "gateway", "terraform"])
        for name in stages:
            self.phase(name, getattr(self, name))
        self.receipt["status"] = "preflight-passed"
        self.save()

    def read_s3_json(self, key):
        with tempfile.TemporaryDirectory(dir=self.directory) as directory:
            path = Path(directory) / "object.json"
            result = self.aws(
                "s3api",
                "get-object",
                "--bucket",
                self.bucket,
                "--key",
                key,
                str(path),
                allow_failure=True,
            )
            if result.returncode:
                require(
                    "NoSuchKey" in result.stderr, "Cannot establish S3 object ownership"
                )
                return None, None
            try:
                value = json.loads(path.read_text())
            except (OSError, ValueError):
                raise Refusal("Cannot decode S3 object ownership") from None
            metadata = self.json(result)
            require(
                isinstance(metadata, dict), "S3 object metadata is not a JSON object"
            )
            etag = metadata.get("ETag")
            require(isinstance(value, dict), "S3 object is not a JSON object")
            require(isinstance(etag, str) and etag, "S3 object has no ETag")
            return value, etag

    def reconcile_lock_attempt(self):
        attempt = self.receipt.get("lock_attempt")
        require(
            isinstance(attempt, dict)
            and attempt.get("bucket") == self.bucket
            and attempt.get("key") == self.lock_key,
            "Receipt has no matching lock attempt",
        )
        value = attempt.get("value", {})
        require(isinstance(value, dict), "Invalid recorded lock attempt payload")
        nonce = value.get("nonce")
        require(
            set(value) == {"installation_id", "run_id", "nonce"}
            and value.get("installation_id") == self.owner
            and value.get("run_id") == self.run_id
            and isinstance(nonce, str)
            and len(nonce) == 32
            and all(c in "0123456789abcdef" for c in nonce),
            "Invalid recorded lock attempt identity",
        )
        current, etag = self.read_s3_json(self.lock_key)
        if current is None:
            return None
        require(
            digest(current) == digest(value),
            "Installation lock belongs to another attempt",
        )
        lock = {"bucket": self.bucket, "key": self.lock_key, "etag": etag}
        require(
            not self.receipt.get("remote_lock") or self.receipt["remote_lock"] == lock,
            "Installation lock changed since acquisition",
        )
        self.receipt["remote_lock"] = lock
        self.save()
        return lock

    def release_lock(self, lock):
        try:
            self.aws(
                "s3api",
                "delete-object",
                "--bucket",
                self.bucket,
                "--key",
                self.lock_key,
                "--if-match",
                lock["etag"],
            )
        except Exception:
            # A lost DELETE response is success only after verified absence.
            current, _ = self.read_s3_json(self.lock_key)
            if current is not None:
                raise
        self.receipt.pop("remote_lock", None)
        self.receipt.pop("lock_attempt", None)
        self.save()

    @contextlib.contextmanager
    def exclusive(self):
        # S3's conditional PUT makes this exclusive across machines, unlike a
        # local flock or workflow-specific concurrency group. No expiry or
        # automatic stale-lock stealing can overlap a still-running migration.
        path = self.directory / "installation-lock.json"
        require(
            not self.receipt.get("remote_lock")
            and not self.receipt.get("lock_attempt"),
            "Recover the retained installation lock attempt before continuing",
        )
        value = {
            "run_id": self.run_id,
            "installation_id": self.owner,
            "nonce": uuid.uuid4().hex,
        }
        self.receipt["lock_attempt"] = {
            "bucket": self.bucket,
            "key": self.lock_key,
            "value": value,
        }
        # Persist the complete identity before PUT, including its unknown outcome.
        self.save()
        try:
            atomic(path, value)
            try:
                result = self.aws(
                    "s3api",
                    "put-object",
                    "--bucket",
                    self.bucket,
                    "--key",
                    self.lock_key,
                    "--body",
                    str(path),
                    "--if-none-match",
                    "*",
                )
                etag = self.json(result).get("ETag")
                require(
                    isinstance(etag, str) and etag,
                    "Written installation lock has no ETag",
                )
                self.receipt["remote_lock"] = {
                    "bucket": self.bucket,
                    "key": self.lock_key,
                    "etag": etag,
                }
                self.save()
            except Exception:
                if self.reconcile_lock_attempt() is None:
                    raise
            yield
            self.release_lock(self.receipt["remote_lock"])
        except BaseException:
            self.receipt["status"] = "recovery-required"
            self.save()
            raise

    def existing(self, doc):
        meta = doc["metadata"]
        namespace = ["-n", meta["namespace"]] if "namespace" in meta else []
        result = self.kube(
            "get",
            doc["kind"],
            meta["name"],
            *namespace,
            "-o",
            "json",
            "--ignore-not-found",
        )
        return self.json(result) if result.stdout.strip() else None

    def apply(self, docs):
        writes = []
        for doc in docs:
            current = self.existing(doc)
            require(
                current is None
                or current["metadata"].get("labels", {}).get(LABEL) == self.owner,
                "Refusing to adopt or modify an object owned by another installation",
            )
            write = copy.deepcopy(doc)
            if current is not None:
                for field in ("uid", "resourceVersion"):
                    require(current["metadata"].get(field), "Object identity is absent")
                    write["metadata"][field] = current["metadata"][field]
            writes.append((write, current is not None))
        for doc, present in writes:
            # Conditional apply fences both replacement and ownership-label races.
            # Create is essential for absent objects: apply could adopt a concurrent one.
            current = self.json(
                self.kube(
                    *(["apply", "--server-side"] if present else ["create"]),
                    "--field-manager=superplane-installer",
                    "-f",
                    "-",
                    "-o",
                    "json",
                    data=yaml.safe_dump(doc),
                )
            )
            require(
                current["metadata"].get("labels", {}).get(LABEL) == self.owner
                and current["metadata"].get("uid")
                and (
                    not present or current["metadata"]["uid"] == doc["metadata"]["uid"]
                ),
                "Written object ownership is inconsistent",
            )
            self.receipt["objects"] = [
                x
                for x in self.receipt["objects"]
                if (x["kind"], x["name"], x.get("namespace"))
                != (
                    doc["kind"],
                    doc["metadata"]["name"],
                    doc["metadata"].get("namespace"),
                )
            ]
            self.receipt["objects"].append(
                {
                    "kind": doc["kind"],
                    "name": doc["metadata"]["name"],
                    "namespace": doc["metadata"].get("namespace"),
                    "uid": current["metadata"]["uid"],
                }
            )
            self.save()

    def foundations(self):
        self.apply(
            [
                d
                for d in self.docs
                if d["kind"]
                in {
                    "Namespace",
                    "ServiceAccount",
                    "NetworkPolicy",
                    "ConfigMap",
                    "Service",
                    "PodDisruptionBudget",
                }
            ]
        )
        mappings = [
            (
                "superplane-db",
                self.env["namespace"],
                "database",
                {
                    "runtime-url": "runtime-url",
                    "migration-url": "migration-url",
                    "ca-pem": "ca-pem",
                },
            ),
            (
                "skypilot-api-db",
                self.env["skypilot_namespace"],
                "database",
                {"connection-uri": "skypilot-url", "ca-pem": "ca-pem"},
            ),
            (
                "superplane-observation",
                self.env["namespace"],
                "observation",
                {key: key for key in self.secret_values["observation"]},
            ),
        ]
        if not self.control_plane_only:
            # workspace_access secret is only created when the workspace cluster
            # and its controller credential are both present and verified.
            mappings.append(
                (
                    "superplane-workspace-access",
                    self.env["namespace"],
                    "workspace_access",
                    {"kubeconfig": "kubeconfig"},
                )
            )
        for namespace in (self.env["namespace"], self.env["skypilot_namespace"]):
            mappings.append(
                (
                    "superplane-skypilot-auth",
                    namespace,
                    "observation",
                    {"token": "skypilot-token"},
                )
            )
        for name, namespace, kind, keys in mappings:
            secret = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "labels": {LABEL: self.owner},
                },
                "type": "Opaque",
                "stringData": {
                    dest: (
                        self.secret_values[kind][src].replace(
                            "postgresql://", "postgresql+asyncpg://", 1
                        )
                        if name == "superplane-db" and src.endswith("-url")
                        else self.secret_values[kind][src]
                    )
                    for dest, src in keys.items()
                },
            }
            if name == "superplane-observation":
                secret["stringData"]["controller-submitter-id"] = self.submitter_ids[
                    "controller"
                ]
            # stdin only; secret material never enters files, argv or receipts.
            self.apply([secret])

    @staticmethod
    def terminal(job, condition):
        return any(
            item.get("type") == condition and item.get("status") == "True"
            for item in (job or {}).get("status", {}).get("conditions", [])
        )

    def wait_job(self, job):
        current = self.existing(job)
        require(
            current is not None
            and current["metadata"].get("labels", {}).get(LABEL) == self.owner,
            "Installation Job owner mismatch",
        )
        recorded = next(
            (
                entry
                for entry in self.receipt["objects"]
                if entry["kind"] == "Job"
                and entry["name"] == job["metadata"]["name"]
                and entry["namespace"] == self.env["namespace"]
            ),
            None,
        )
        require(
            recorded is None or recorded["uid"] == current["metadata"]["uid"],
            "Installation Job was replaced; resume refused",
        )
        if recorded is None:
            self.receipt["objects"].append(
                {
                    "kind": "Job",
                    "name": job["metadata"]["name"],
                    "namespace": self.env["namespace"],
                    "uid": current["metadata"]["uid"],
                }
            )
            self.save()
        end = time.monotonic() + self.env["timeout_seconds"]
        while time.monotonic() < end:
            current = self.existing(job)
            require(
                current is not None
                and current["metadata"].get("labels", {}).get(LABEL) == self.owner,
                "Installation Job is absent or ownership changed",
            )
            require(
                not self.terminal(current, "Failed"),
                "Installation Job failed; inspect its terminal state before resuming",
            )
            if self.terminal(current, "Complete"):
                return
            time.sleep(2)
        raise Refusal(
            "Installation Job timed out; do not retry until its terminal state is known"
        )

    def migrate(self):
        job = migration_job(self.env, self.lock, self.run_id)
        if self.existing(job) is None:
            self.apply([job])
        self.wait_job(job)
        self.receipt["migration"] = {
            "job": job["metadata"]["name"],
            "schema": self.lock["schema"]["observed"]["head"],
            "image": image(self.lock, "superplane-api"),
        }

    def bootstrap(self, token):
        job = bootstrap_job(self.env, self.lock, self.run_id)
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": dict(job["metadata"]),
            "stringData": {"token": token},
        }
        # Existing immutable Jobs are waited on, never given a new token/retried.
        if self.existing(job) is None:
            self.apply([secret])
            self.apply([job])
        self.wait_job(job)
        current = self.existing(secret)
        if current is not None:
            entry = next(
                (
                    x
                    for x in self.receipt["objects"]
                    if x["kind"] == "Secret"
                    and x["name"] == secret["metadata"]["name"]
                    and x["namespace"] == self.env["namespace"]
                ),
                None,
            )
            require(
                entry is not None
                and current["metadata"].get("uid") == entry["uid"]
                and current["metadata"].get("labels", {}).get(LABEL) == self.owner,
                "Bootstrap token Secret was replaced; deletion refused",
            )
            self.kube(
                "delete",
                "--raw",
                f"/api/v1/namespaces/{self.env['namespace']}/secrets/{entry['name']}",
                "-f",
                "-",
                data=json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "DeleteOptions",
                        "preconditions": {
                            "uid": entry["uid"],
                            "resourceVersion": current["metadata"]["resourceVersion"],
                        },
                    }
                ),
            )
        self.receipt["bootstrap"] = {
            "job": job["metadata"]["name"],
            "adp_org_id": self.env["adp_org_id"],
            "org_id": self.env["org_id"],
            # workspace_id is absent in control-plane-only mode; recorded as None
            # so the receipt accurately reflects the deferred workspace activation.
            "workspace_id": self.env.get("workspace_id"),
            "initial_grant_policy": "organization:administer; existing grants preserved"
            if self.control_plane_only
            else "workspace:administer; existing grants preserved",
            "token_secret_removed": True,
            "legacy_migration": False,
        }

    def rollout(self):
        self.apply([d for d in self.docs if d["kind"] == "Deployment"])
        for doc in self.docs:
            if doc["kind"] != "Deployment":
                continue
            self.kube(
                "rollout",
                "status",
                "deployment/" + doc["metadata"]["name"],
                "-n",
                doc["metadata"]["namespace"],
                f"--timeout={self.env['timeout_seconds']}s",
                timeout=self.env["timeout_seconds"] + 30,
            )
            current = self.existing(doc)
            expected = doc["spec"]["template"]["spec"]["containers"][0]["image"]
            require(
                current["spec"]["template"]["spec"]["containers"][0]["image"]
                == expected
                and current.get("status", {}).get("availableReplicas", 0) >= 1,
                "Rollout does not match the release or is unavailable",
            )

    def pending_route(self):
        pending = self.receipt.get("pending_route")
        if pending is None:
            return None
        require(
            isinstance(pending, dict)
            and pending.get("bucket") == self.bucket
            and pending.get("key") == self.route_key
            and "prior_etag" in pending
            and (
                pending["prior_etag"] is None
                or isinstance(pending["prior_etag"], str)
                and pending["prior_etag"]
            )
            and self.receipt.get("public_route")
            == {
                "bucket": self.bucket,
                "key": self.route_key,
                "etag": pending["prior_etag"],
            },
            "Pending route has inconsistent prior ownership evidence",
        )
        self.validate_route_value(pending.get("value"))
        if "superseded_enable" in pending:
            self.validate_route_value(pending["superseded_enable"])
            require(
                pending["value"]["enabled"] is False
                and pending["superseded_enable"]["enabled"] is True,
                "Only a pending enable may be superseded by a disable",
            )
        return pending

    def validate_route_value(self, value):
        require(isinstance(value, dict), "Invalid recorded pending route payload")
        revision = value.get("revision")
        release = value.get("release_id")
        require(
            set(value)
            == {
                "version",
                "enabled",
                "namespace",
                "installation_id",
                "release_id",
                "revision",
            }
            and type(value.get("version")) is int
            and value["version"] == 2
            and type(value.get("enabled")) is bool
            and value.get("namespace") == self.env["namespace"]
            and value.get("installation_id") == self.owner
            and isinstance(release, str)
            and len(release) == 64
            and all(c in "0123456789abcdef" for c in release)
            and isinstance(revision, str)
            and len(revision) == 32
            and all(c in "0123456789abcdef" for c in revision),
            "Pending route has invalid publication identity",
        )

    def acknowledge_route(self, value, etag):
        acknowledged = copy.deepcopy(self.receipt)
        acknowledged["public_route"] = {
            "bucket": self.bucket,
            "key": self.route_key,
            "etag": etag,
        }
        acknowledged["public_route_enabled"] = value["enabled"]
        acknowledged.pop("pending_route", None)
        # Keep the pending identity in memory too if durable acknowledgement fails.
        atomic(self.receipt_path, acknowledged)
        self.receipt = acknowledged

    def check_route_owner(self):
        pending = self.pending_route()
        value, etag = self.read_s3_json(self.route_key)
        if value is not None:
            require(
                value.get("installation_id") == self.owner
                and value.get("namespace") == self.env["namespace"],
                "Public route belongs to another installation or namespace",
            )
        if pending and value is not None and digest(value) == digest(pending["value"]):
            # Full payload and unique revision identify a committed, unacknowledged PUT.
            self.acknowledge_route(value, etag)
            return etag
        if (
            pending
            and value is not None
            and "superseded_enable" in pending
            and digest(value) == digest(pending["superseded_enable"])
        ):
            # A previously issued enable won the CAS race with its compensation.
            # Retain the disable intent and condition it on that exact committed body.
            pending["prior_etag"] = etag
            pending.pop("superseded_enable")
            self.receipt["public_route"] = {
                "bucket": self.bucket,
                "key": self.route_key,
                "etag": etag,
            }
            self.save()
        observed = {"bucket": self.bucket, "key": self.route_key, "etag": etag}
        require(
            "public_route" not in self.receipt
            or self.receipt["public_route"] == observed,
            "Public route changed since the recorded observation",
        )
        # Explicit absence is evidence too. This method never publishes routes,
        # so Gateway/preflight checks remain read-only at the external boundary.
        if "public_route" not in self.receipt:
            self.receipt["public_route"] = observed
            self.save()
        return etag

    def finish_route(self):
        self.check_route_owner()
        pending = self.pending_route()
        if pending is None:
            return
        path = self.directory / "public-route.json"
        atomic(path, pending["value"])
        # One bounded retry uses exactly the durable payload and prior condition.
        # Failed readback retains the marker for supported same-receipt recovery.
        for attempt in range(2):
            etag = pending["prior_etag"]
            try:
                result = self.aws(
                    "s3api",
                    "put-object",
                    "--bucket",
                    self.bucket,
                    "--key",
                    self.route_key,
                    "--body",
                    str(path),
                    "--content-type",
                    "application/json",
                    *(["--if-match", etag] if etag else ["--if-none-match", "*"]),
                )
                written_etag = self.json(result).get("ETag")
                require(
                    isinstance(written_etag, str) and written_etag,
                    "Written public route has no ETag",
                )
            except Exception:
                self.check_route_owner()
                if "pending_route" not in self.receipt:
                    return
                if attempt == 1:
                    raise
            else:
                self.acknowledge_route(pending["value"], written_etag)
                return

    def route(self, enabled):
        etag = self.check_route_owner()
        pending = self.pending_route()
        superseded = None
        if pending:
            if enabled is False and pending["value"]["enabled"] is True:
                # Readback left the prior version unchanged. Compensate directly;
                # never publish an uncommitted enable merely to turn it off next.
                superseded = copy.deepcopy(pending["value"])
            else:
                self.finish_route()
                if enabled is False:
                    return
                etag = self.check_route_owner()
        value = {
            "version": 2,
            "enabled": enabled,
            "namespace": self.env["namespace"],
            "installation_id": self.owner,
            "release_id": self.release,
            "revision": uuid.uuid4().hex,
        }
        self.receipt["pending_route"] = {
            "bucket": self.bucket,
            "key": self.route_key,
            "prior_etag": etag,
            "value": value,
        }
        if superseded is not None:
            # Preserve the earlier identity in case its already-issued PUT commits
            # between readback and the compensating conditional write.
            self.receipt["pending_route"]["superseded_enable"] = superseded
        self.receipt["public_route_enabled"] = None
        self.receipt.pop("route_disabled_verification", None)
        self.save()
        self.finish_route()

    def disable_route(self):
        self.receipt["route_disable_pending"] = True
        self.receipt.pop("route_disabled_verification", None)
        self.save()
        self.route(False)
        self.gateway()
        start = time.monotonic()
        deadline = start + self.env["timeout_seconds"]
        path = self.env["origin"] + "/api/superplane/v1/workspaces"
        while True:
            # This inventoried path returns 401 while enabled without a token and
            # the Gateway's own 404 while off. Never send installation credentials.
            try:
                response = httpx.get(
                    path,
                    headers={"Cache-Control": "no-cache"},
                    timeout=min(20, max(1, deadline - time.monotonic())),
                    follow_redirects=False,
                )
                health = httpx.get(
                    self.env["origin"] + "/api/health",
                    timeout=min(20, max(1, deadline - time.monotonic())),
                    follow_redirects=False,
                )
                require(
                    health.status_code == 200,
                    "Existing ADP health failed during route disable",
                )
                off = (
                    response.status_code == 404
                    and "X-Superplane-Release" not in response.headers
                    and response.json() == {"detail": "Not found"}
                )
            except (httpx.HTTPError, ValueError):
                off = False
            now = time.monotonic()
            if off and now >= start + self.gateway_cache_seconds:
                self.check_route_owner()
                self.receipt["route_disabled_verification"] = {
                    "public_endpoint": path,
                    "status": 404,
                    "adp_healthy": True,
                    "cache_seconds": self.gateway_cache_seconds,
                    "verified_at": datetime.now(UTC).isoformat(),
                }
                self.receipt.pop("route_disable_pending", None)
                self.save()
                return
            require(
                now < deadline,
                "Public route disable was not observed through the Gateway",
            )
            time.sleep(min(1, deadline - now))

    def private_services(self):
        namespace = self.env["namespace"]

        def service_get(name, port, path):
            program = (
                "import httpx; "
                f"r=httpx.get('http://{name}.{namespace}.svc.cluster.local:{port}/{path}',timeout=15,trust_env=False); "
                "r.raise_for_status(); print(r.text)"
            )
            return self.kube(
                "exec",
                "deployment/superplane-api",
                "-n",
                namespace,
                "--",
                "python",
                "-c",
                program,
            )

        health = self.json(service_get("superplane-api", 8000, "health"))
        require(
            health.get("domain_auth_enforced") is True
            and health.get("cognito_enabled") is True,
            "API is not enforcing the deployed ADP identity policy",
        )
        # Management readiness includes an authenticated registry lease even
        # when no workspace reconciler can be activated.
        services_to_check = [
            ("superplane-platform-monitor", 9090, "healthz"),
            ("superplane-controller", 8081, "readyz"),
            ("superplane-api", 8000, "readyz"),
        ]
        for name, port, path in services_to_check:
            service_get(name, port, path)
        self.kube(
            "exec",
            "deployment/skypilot-api",
            "-n",
            self.env["skypilot_namespace"],
            "-c",
            "authenticated-transport",
            "--",
            "python",
            "-m",
            "app.skypilot_proxy",
            "--check",
        )
        skypilot_database = self.json(
            self.kube(
                "exec",
                "deployment/skypilot-api",
                "-n",
                self.env["skypilot_namespace"],
                "-c",
                "skypilot-api",
                "--",
                "python3",
                "/skypilot-bootstrap/bootstrap.py",
                "--check-database",
            )
        )
        require(
            skypilot_database.get("dialect") == "postgresql"
            and skypilot_database.get("database") == self.env["database"]["database"]
            and skypilot_database.get("schema")
            == self.env["database"]["skypilot_schema"]
            and skypilot_database.get("tls") is True
            and skypilot_database.get("config_matches") is True
            and "clusters" in skypilot_database.get("tables", []),
            "SkyPilot does not use the verified durable database and configuration",
        )
        database = self.json(
            self.kube(
                "exec",
                "deployment/superplane-api",
                "-n",
                namespace,
                "--",
                "python",
                "-m",
                "app.installation",
                "database",
            )
        )
        require(
            database.get("revision") == self.lock["schema"]["observed"]["head"]
            and database.get("schema") == self.env["database"]["schema"],
            "Running API does not use the migrated domain schema",
        )
        self.receipt["private_verification"] = {
            "api_adp_auth": True,
            "monitor_authenticated_receiver_read": True,
            "controller_manager_ready": True,
            "workspace_execution_ready": not self.control_plane_only,
            "skypilot_authenticated_health": True,
            "skypilot_database": skypilot_database,
            "database": database,
        }
        if self.control_plane_only:
            runtime = self.json(
                self.kube(
                    "exec",
                    "deployment/superplane-api",
                    "-n",
                    namespace,
                    "--",
                    "python",
                    "-m",
                    "app.installation",
                    "readiness",
                )
            )
            require(
                runtime.get("mode") == "management"
                and runtime.get("release_id") == self.release
                and runtime.get("source_revision") == self.lock["source_revision"]
                and runtime.get("domain_auth_enforced") is True,
                "Running management API does not match the release and authorization contract",
            )
            program = (
                "import os,json,httpx; "
                "grants=json.loads(os.environ['OBSERVATION_SUBMITTERS']); "
                f"grant=next(g for g in grants if 'controller_management/{self.env['org_id']}' in g.get('lease_scopes',[])); "
                f"r=httpx.get('http://superplane-controller.{namespace}.svc.cluster.local:8081/statusz',headers={{'Authorization':grant['credential']}},timeout=10,trust_env=False); "
                "r.raise_for_status(); print(json.dumps(r.json()))"
            )
            controller = self.json(
                self.kube(
                    "exec",
                    "deployment/superplane-api",
                    "-n",
                    namespace,
                    "--",
                    "python",
                    "-c",
                    program,
                )
            )
            require(
                controller.get("mode") == "management"
                and controller.get("registry_ready") is True
                and controller.get("governed_provisioning") is False,
                "Controller has not acquired its authenticated registry lease",
            )
            self.receipt["private_verification"]["controller"] = controller
        if not self.control_plane_only:
            # In control-plane-only mode the workspace controller is not deployed
            # and no workspace_id is configured; authenticated observations from a
            # workspace cannot be expected.  This check runs during workspace
            # activation instead.
            expected = {
                item["submitter_id"]
                for item in json.loads(self.secret_values["observation"]["submitters"])
            }
            deadline = time.monotonic() + self.env["timeout_seconds"]
            while True:
                runtime = self.json(
                    self.kube(
                        "exec",
                        "deployment/superplane-api",
                        "-n",
                        namespace,
                        "--",
                        "python",
                        "-m",
                        "app.installation",
                        "readiness",
                    )
                )
                require(
                    runtime.get("release_id") == self.release
                    and runtime.get("source_revision") == self.lock["source_revision"]
                    and runtime.get("domain_auth_enforced") is True
                    and all(runtime.get("capabilities", {}).values())
                    and len(runtime.get("capabilities", {})) == 4,
                    "Running API does not match the release or trusted capability composition",
                )
                received = {
                    key: value
                    for key, value in runtime.get("observations", {}).items()
                    if value["workspace"] == self.env["workspace_id"]
                    and value["cluster_id"] == self.env["cluster_id"]
                    and (
                        value["status"] == "healthy"
                        or (
                            key == self.submitter_ids["monitor"]
                            and value["status"] == "not_checked"
                        )
                    )
                    and datetime.fromisoformat(value["reported_at"])
                    >= max(
                        datetime.fromisoformat(self.receipt["started_at"]),
                        datetime.now(UTC) - timedelta(seconds=90),
                    )
                    and datetime.fromisoformat(value["reported_at"])
                    <= datetime.now(UTC) + timedelta(seconds=30)
                }
                if expected <= set(received):
                    self.receipt["private_verification"][
                        "authenticated_observation_delivery"
                    ] = received
                    break
                require(
                    time.monotonic() < deadline,
                    "Monitor/controller have not delivered fresh authenticated observations"
                    " (controller healthy; unchecked monitor dimensions remain explicit)"
                    " for this workspace",
                )
                time.sleep(2)
        else:
            self.receipt["private_verification"][
                "authenticated_observation_delivery"
            ] = "deferred: no workspace registered at control-plane install time"

    def verify(self, token):
        require(
            bool(token),
            "Verification requires an ADP token in SUPERPLANE_VERIFICATION_TOKEN",
        )
        base = self.env["origin"] + "/api/superplane/v1"
        if not self.control_plane_only:
            # Full verification: check authenticated workspace access, then deny checks.
            path = f"/workspaces/{self.env['workspace_id']}"
            deadline = time.monotonic() + self.env["timeout_seconds"]
            while True:
                response = httpx.get(
                    base + path,
                    headers={"Authorization": "Bearer " + token},
                    timeout=20,
                    follow_redirects=False,
                )
                if (
                    response.status_code == 200
                    and response.headers.get("X-Superplane-Release") == self.release
                ):
                    break
                require(
                    time.monotonic() < deadline,
                    "Public authenticated workspace check failed",
                )
                time.sleep(2)
            for suffix, headers, expected in [
                (path, {}, {401}),
                (
                    f"/workspaces/{uuid.uuid4()}",
                    {
                        "Authorization": "Bearer " + token,
                        "X-Workspace-Id": self.env["workspace_id"],
                        "X-Org-Id": self.env["org_id"],
                    },
                    {403},
                ),
                (
                    path,
                    {"Authorization": "Bearer invalid", "X-Org-Id": self.env["org_id"]},
                    {401},
                ),
                (
                    "/internal/observations/clusters",
                    {"Authorization": "Bearer " + token},
                    {404},
                ),
                ("/auth/login", {}, {404}),
            ]:
                response = httpx.get(
                    base + suffix, headers=headers, timeout=20, follow_redirects=False
                )
                require(
                    response.status_code in expected,
                    "Public authorization/private-route verification failed",
                )
            self.gateway()
            self.receipt["verification"] = {
                "public_endpoint": base,
                "workspace_id": self.env["workspace_id"],
                "release_id": self.release,
                "route_destination": (
                    f"superplane-api.{self.env['namespace']}.svc.cluster.local:8000"
                ),
                "authenticated_status": 200,
                "authenticated": True,
                "private_routes_denied": True,
                "ungranted_workspace_denied": True,
                "live_workload_parity": "not evaluated",
            }
        else:
            deadline = time.monotonic() + self.env["timeout_seconds"]
            while True:
                organization = httpx.get(
                    base + "/orgs/current",
                    headers={"Authorization": "Bearer " + token},
                    timeout=20,
                    follow_redirects=False,
                )
                workspaces = httpx.get(
                    base + "/workspaces",
                    headers={"Authorization": "Bearer " + token},
                    timeout=20,
                    follow_redirects=False,
                )
                if (
                    organization.status_code == workspaces.status_code == 200
                    and organization.headers.get("X-Superplane-Release") == self.release
                    and workspaces.headers.get("X-Superplane-Release") == self.release
                ):
                    require(
                        organization.json().get("id") == self.env["org_id"],
                        "Public organization differs from the selected installation",
                    )
                    listing = workspaces.json()
                    require(
                        type(listing.get("total")) is int
                        and isinstance(listing.get("workspaces"), list)
                        and listing["total"] == len(listing["workspaces"]),
                        "Public workspace listing is invalid",
                    )
                    break
                require(
                    time.monotonic() < deadline,
                    "Public authenticated management checks failed",
                )
                time.sleep(2)
            for suffix, headers, expected in [
                ("/workspaces", {}, {401}),
                ("/orgs/current", {"Authorization": "Bearer invalid"}, {401}),
                (
                    f"/workspaces/{uuid.uuid4()}",
                    {
                        "Authorization": "Bearer " + token,
                        "X-Org-Id": self.env["org_id"],
                    },
                    {403},
                ),
                (
                    f"/workspaces/{uuid.uuid4()}",
                    {},
                    {401},
                ),
                (
                    "/internal/observations/clusters",
                    {"Authorization": "Bearer " + token},
                    {404},
                ),
                ("/auth/login", {}, {404}),
            ]:
                response = httpx.get(
                    base + suffix, headers=headers, timeout=20, follow_redirects=False
                )
                require(
                    response.status_code in expected,
                    "Public authorization/private-route verification failed",
                )
            self.gateway()
            self.receipt["verification"] = {
                "public_endpoint": base,
                "workspace_id": None,
                "release_id": self.release,
                "route_destination": (
                    f"superplane-api.{self.env['namespace']}.svc.cluster.local:8000"
                ),
                "authenticated_status": 200,
                "authenticated": True,
                "organization_id": self.env["org_id"],
                "registered_workspaces": listing["total"],
                "control_plane_ready": True,
                "workspace_execution_ready": False,
                "private_routes_denied": True,
                "ungranted_workspace_denied": True,
                "live_workload_parity": "not evaluated",
                "workspace_registration": "observed from the authenticated durable registry",
            }

    def execute(self, approved_plan, token):
        require(
            bool(token),
            "SUPERPLANE_VERIFICATION_TOKEN is required before any installation mutation",
        )
        self.preflight()
        require(
            approved_plan == self.receipt["plan_sha256"],
            "The exact current domain Terraform plan must be reviewed; pass its plan_sha256",
        )
        with self.exclusive():
            try:
                self.phase("disable-route", self.disable_route)
                self.phase(
                    "infrastructure",
                    lambda: self.commands.call(
                        [
                            "terraform",
                            f"-chdir={self.directory / 'terraform'}",
                            "apply",
                            "-input=false",
                            "installation.tfplan",
                        ],
                        timeout=self.env["timeout_seconds"],
                    ),
                )
                self.phase("foundations", self.foundations)
                self.phase("migration", self.migrate)
                self.phase("bootstrap", lambda: self.bootstrap(token))
                self.phase("rollout", self.rollout)
                self.phase("private-verification", self.private_services)
                self.phase("public-route", lambda: self.route(True))
                self.phase("verification", lambda: self.verify(token))
                if self.control_plane_only:
                    self.phase(
                        "restart-verification",
                        lambda: self.verify_management_restart(token),
                    )
                self.receipt["status"] = "installed-and-verified"
                self.save()
            except BaseException:
                self.disable_route()
                raise

    def management_pod_uids(self, name):
        namespace = self.env["namespace"]
        deployment = self.json(
            self.kube("get", "Deployment", name, "-n", namespace, "-o", "json")
        )
        require(
            deployment["metadata"].get("labels", {}).get(LABEL) == self.owner,
            "Management Deployment belongs to another installation",
        )

        def controlled_by(obj, kind, uids):
            return any(
                ref.get("controller") is True
                and ref.get("kind") == kind
                and ref.get("uid") in uids
                for ref in obj["metadata"].get("ownerReferences", [])
            )

        def selected(kind):
            return self.json(
                self.kube(
                    "get",
                    kind,
                    "-n",
                    namespace,
                    "-l",
                    "app.kubernetes.io/name=" + name,
                    "-o",
                    "json",
                )
            )["items"]

        # Bootstrap/migration Jobs share the API label. Follow the actual
        # Deployment -> ReplicaSet -> Pod ownership chain, including old
        # terminating service pods but excluding Jobs and foreign controllers.
        replicasets = {
            rs["metadata"]["uid"]
            for rs in selected("replicasets")
            if controlled_by(rs, "Deployment", {deployment["metadata"]["uid"]})
        }
        return {
            pod["metadata"]["uid"]
            for pod in selected("pods")
            if controlled_by(pod, "ReplicaSet", replicasets)
        }

    def verify_management_restart(self, token):
        namespace = self.env["namespace"]
        expected_org = self.receipt["verification"]["organization_id"]
        expected_count = self.receipt["verification"]["registered_workspaces"]
        before = {}
        for name in ("superplane-api", "superplane-controller"):
            before[name] = self.management_pod_uids(name)
            require(bool(before[name]), "No running management pod to verify restart")
            self.kube("rollout", "restart", "deployment/" + name, "-n", namespace)
            self.kube(
                "rollout",
                "status",
                "deployment/" + name,
                "-n",
                namespace,
                f"--timeout={self.env['timeout_seconds']}s",
                timeout=self.env["timeout_seconds"] + 30,
            )
            # A completed rollout may still list the terminating old pod.
            # Wait for actual removal, retaining the original UID-based proof.
            deadline = time.monotonic() + self.env["timeout_seconds"]
            while True:
                uids = self.management_pod_uids(name)
                if uids and not uids.intersection(before[name]):
                    break
                require(
                    time.monotonic() < deadline,
                    "Management process replacement not verified",
                )
                time.sleep(2)
            self.receipt.setdefault("management_restart", {})[name] = {
                "before_pod_uids": sorted(before[name]),
                "after_pod_uids": sorted(uids),
            }
            self.save()
        self.private_services()
        self.verify(token)
        require(
            self.receipt["verification"]["organization_id"] == expected_org
            and self.receipt["verification"]["registered_workspaces"] == expected_count,
            "Durable organization/registrations changed across management restart",
        )
        self.receipt["verification"]["restart_persistence_verified"] = True

    def resume(self, previous):
        require(
            previous.get("version") == 1
            and previous.get("installation_id") == self.owner,
            "Resume receipt belongs to another installation",
        )
        require(
            digest(previous.get("environment")) == digest(self.env)
            and digest(previous.get("release_lock")) == self.release,
            "Resume must use the exact original environment and release",
        )
        require(
            previous.get("status")
            in {"failed", "preflight-passed", "planned", "recovery-required"}
            or (
                previous.get("status") == "installed-and-verified"
                and (previous.get("remote_lock") or previous.get("lock_attempt"))
            ),
            "Only an incomplete installation may be resumed",
        )
        require(
            isinstance(previous.get("run_id"), str)
            and len(previous["run_id"]) == 16
            and all(c in "0123456789abcdef" for c in previous["run_id"]),
            "Invalid recorded run identity",
        )
        self.run_id = previous["run_id"]
        self.receipt = previous
        # The same immutable Job name is inspected on resume; a timeout can
        # never create a second migration while the first is still running.
        self.save()

    def recover_lock(self, confirmed_stopped):
        require(
            confirmed_stopped == self.run_id,
            "Recovery requires confirming that the recorded installer and its child processes have stopped",
        )
        lock = self.receipt.get("remote_lock", {})
        cleanup_required = self.receipt.get("temporary_preflight", {}).get(
            "cleanup_required"
        )
        require(
            not lock
            or (
                lock.get("bucket") == self.bucket
                and lock.get("key") == self.lock_key
                and lock.get("etag")
            ),
            "Receipt has no matching retained installation lock",
        )
        require(
            lock or self.receipt.get("lock_attempt") or cleanup_required,
            "Receipt has no retained lock or temporary namespace",
        )
        self.target(verify_source=False)
        for doc in (
            migration_job(self.env, self.lock, self.run_id),
            bootstrap_job(self.env, self.lock, self.run_id),
        ):
            job = self.existing(doc)
            require(
                job is None
                or (
                    job["metadata"].get("labels", {}).get(LABEL) == self.owner
                    and (self.terminal(job, "Complete") or self.terminal(job, "Failed"))
                ),
                "Installation Job is nonterminal or foreign; leave the lock in place",
            )
        if cleanup_required:
            ClusterProbe.recover(self)
        if self.receipt.get("lock_attempt"):
            lock = self.reconcile_lock_attempt()
        if (
            self.receipt.get("route_disable_pending")
            or self.receipt.get("pending_route")
            or (
                self.receipt.get("public_route_enabled") is True
                and self.receipt.get("status") != "installed-and-verified"
            )
        ):
            require(lock, "Route recovery requires the retained installation lock")
            self.gateway()
            self.disable_route()
        if lock:
            self.release_lock(lock)
        else:
            # Confirmed absence after an ambiguous acquisition/release.
            self.receipt.pop("remote_lock", None)
            self.receipt.pop("lock_attempt", None)
        self.receipt["status"] = "failed"
        self.save()

    def rollback(self, previous, token):
        require(
            previous.get("status") == "installed-and-verified"
            and previous.get("installation_id") == self.owner,
            "Rollback requires a verified receipt from this installation",
        )
        require(
            previous.get("release_id") == digest(previous.get("release_lock")),
            "Rollback release receipt has inconsistent content",
        )
        require(
            previous.get("environment") == self.env,
            "Rollback cannot change environment, database or workspace ownership",
        )
        require(
            previous["release_lock"]["schema"]["observed"]["head"]
            == self.lock["schema"]["observed"]["head"],
            "Image rollback cannot reverse a schema change; use the recorded restore owner",
        )
        require(bool(token), "Rollback requires the ADP verification token")
        self.target(verify_source=False)
        # The supplied receipt selects the release to restore. Fence the current
        # route separately, before preparation; it must not change while we work.
        self.gateway()
        self.receipt["rollback_current_route"] = copy.deepcopy(
            self.receipt["public_route"]
        )
        self.save()
        self.lock = previous["release_lock"]
        self.release = previous["release_id"]
        self.docs = render(
            self.network_environment,
            self.lock,
            control_plane_only=self.control_plane_only,
        )
        self.write_manifests()
        self.images()
        self.receipt["secret_versions"] = previous["secret_versions"]
        self.secrets()
        self.database()
        require(
            self.receipt["database_observations"]["runtime-url"]["revision"]
            == self.lock["schema"]["observed"]["head"],
            "Live schema is incompatible with image-only rollback",
        )
        self.receipt.update(
            release_lock=self.lock,
            release_id=self.release,
            rollback_of=previous["run_id"],
        )
        with self.exclusive():
            try:
                self.phase("disable-route", self.disable_route)
                self.phase("restore-configuration", self.foundations)
                self.phase("restore-release", self.rollout)
                self.phase("verify-private-rollback", self.private_services)
                self.phase("restore-route", lambda: self.route(True))
                self.phase("verify-rollback", lambda: self.verify(token))
                self.receipt["status"] = "installed-and-verified"
                self.receipt["schema_recovery"] = (
                    "No schema downgrade performed; compatible schema verified"
                )
                self.save()
            except BaseException:
                self.disable_route()
                raise

    def cleanup(self, previous):
        require(
            previous.get("installation_id") == self.owner
            and previous.get("environment") == self.env,
            "Cleanup receipt belongs to another environment",
        )
        observation = previous.get("public_route")
        require(
            isinstance(observation, dict)
            and observation.get("bucket") == self.bucket
            and observation.get("key") == self.route_key
            and "etag" in observation
            and (
                observation["etag"] is None
                or isinstance(observation["etag"], str)
                and observation["etag"]
            ),
            "Cleanup requires an exact route observation in the supplied receipt",
        )
        self.receipt["public_route"] = copy.deepcopy(observation)
        if previous.get("pending_route") is not None:
            self.receipt["pending_route"] = copy.deepcopy(previous["pending_route"])
            pending = self.pending_route()
            require(
                pending["value"]["release_id"] == previous.get("release_id"),
                "Cleanup pending route differs from the supplied release",
            )
        self.target(verify_source=False)
        self.gateway()
        # Namespace, credentials, backups, domain DB and Terraform/ECR resources
        # are retained for recovery. Remove only recorded workload object UIDs.
        resources = {
            "Deployment": ("/apis/apps/v1", "deployments"),
            "Service": ("/api/v1", "services"),
            "Job": ("/apis/batch/v1", "jobs"),
            "ConfigMap": ("/api/v1", "configmaps"),
            "NetworkPolicy": ("/apis/networking.k8s.io/v1", "networkpolicies"),
            "ServiceAccount": ("/api/v1", "serviceaccounts"),
            "PodDisruptionBudget": ("/apis/policy/v1", "poddisruptionbudgets"),
        }
        with self.exclusive():
            self.disable_route()
            self.receipt["removed"] = []
            order = {
                "Deployment": 0,
                "Job": 1,
                "Service": 2,
                "ConfigMap": 3,
                "ServiceAccount": 4,
                "NetworkPolicy": 5,
                "PodDisruptionBudget": 6,
            }
            for entry in sorted(
                previous.get("objects", []),
                key=lambda value: order.get(value["kind"], 99),
            ):
                if entry["kind"] not in resources:
                    continue
                require(
                    entry.get("namespace")
                    in {self.env["namespace"], self.env["skypilot_namespace"]},
                    "Cleanup inventory includes a foreign namespace",
                )
                doc = {
                    "kind": entry["kind"],
                    "metadata": {
                        "name": entry["name"],
                        "namespace": entry["namespace"],
                    },
                }
                current = self.existing(doc)
                if current is None:
                    continue
                require(
                    current["metadata"].get("uid") == entry["uid"]
                    and current["metadata"].get("labels", {}).get(LABEL) == self.owner,
                    "Cleanup object was replaced or ownership changed; refused",
                )
                prefix, plural = resources[entry["kind"]]
                options = {
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": {
                        "uid": entry["uid"],
                        "resourceVersion": current["metadata"]["resourceVersion"],
                    },
                    "propagationPolicy": "Foreground",
                }
                # API-server preconditions close the read/delete race. kubectl's
                # normal name-based delete has no UID precondition.
                self.kube(
                    "delete",
                    "--raw",
                    f"{prefix}/namespaces/{entry['namespace']}/{plural}/{entry['name']}",
                    "-f",
                    "-",
                    data=json.dumps(options),
                )
                self.kube(
                    "wait",
                    "--for=delete",
                    f"{entry['kind']}/{entry['name']}",
                    "-n",
                    entry["namespace"],
                    f"--timeout={self.env['timeout_seconds']}s",
                    timeout=self.env["timeout_seconds"] + 30,
                )
                self.receipt["removed"].append(entry)
                self.save()
            self.gateway()
            self.receipt["status"] = "workloads-removed"
            self.receipt["retained"] = [
                "namespaces",
                "secret references and Kubernetes Secrets",
                "domain databases and backups",
                "domain Terraform resources and immutable ECR images",
                "external workspace/provider resources",
            ]
            self.save()


@contextlib.contextmanager
def local_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "run.lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal("Another installer is using this receipt directory") from None
        yield
