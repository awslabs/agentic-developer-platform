"""Synchronous, checked installation stages with durable recovery receipts.

Uses the existing Terraform module and its structural ownership guard directly.
It does not dispatch asynchronous workflows or invoke a platform deployment.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
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

from .config import COMPONENTS, LABEL, MODULE, Refusal, digest, identity, image, require
from .manifests import bootstrap_job, migration_job, render

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
                    if k != "SUPERPLANE_VERIFICATION_TOKEN"
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
    def __init__(self, env, lock, directory: Path, commands=None):
        self.env, self.lock, self.directory = env, lock, directory
        self.commands = commands or Commands()
        self.run_id = uuid.uuid4().hex[:16]
        self.owner = identity(env)
        self.release = digest(lock)
        self.docs = render(env, lock)
        self.secret_values = {}
        self.secret_versions = {}
        self.submitter_ids = {}
        self.kubeconfig = directory / "kubeconfig"
        self.route_name = f"/adp/{env['environment']}/superplane/public-route"
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

    def plan(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.directory / "manifests.yaml").write_text(
            yaml.safe_dump_all(
                [
                    *self.docs,
                    migration_job(self.env, self.lock, self.run_id),
                    bootstrap_job(self.env, self.lock, self.run_id),
                ],
                sort_keys=False,
            )
        )
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

    def images(self):
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
                    tag == self.lock["source_revision"]
                    or tag == self.lock["source_revision"][:12]
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
                == self.lock["source_revision"],
                f"Image OCI provenance does not match source: {name}",
            )
        self.commands.call(
            ["docker", "pull", image(self.lock, "superplane-api")],
            timeout=self.env["timeout_seconds"],
        )
        result = self.commands.call(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--entrypoint",
                "python",
                image(self.lock, "superplane-api"),
                "-m",
                "app.installation",
                "capabilities",
            ],
            allow_failure=True,
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
            not missing and result.returncode == 0,
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
            ],
            allow_failure=True,
        )
        require(
            controller.returncode == 0
            and self.json(controller).get("governed_provisioning") is True,
            "Production controller lacks B's governed execution adapter; direct provisioning is not a fallback",
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
            "workspace_access": {"kubeconfig"},
        }
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
            require(
                len(selected) == 1
                and selected[0].get("workspaces") == [self.env["workspace_id"]]
                and selected[0].get("signing_key")
                == observation[component + "-signing-key"],
                "Observation grants must match the exact workspace and service credential",
            )
            self.submitter_ids[component] = selected[0].get("submitter_id")
            scopes = ["budget_monitor/global"] if component == "monitor" else []
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
        # Parse workspace credentials without ever writing an unvalidated exec
        # plugin to disk. Only static bearer/certificate auth is supported.
        workspace = yaml.safe_load(self.secret_values["workspace_access"]["kubeconfig"])
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
                    user.get("client-certificate-data") and user.get("client-key-data")
                )
            ),
            "Workspace kubeconfig cannot execute plugins or read local files",
        )
        cluster = self.json(
            self.aws("eks", "describe-cluster", "--name", self.env["workspace_cluster"])
        )["cluster"]
        selected = workspace["clusters"][0]["cluster"]
        require(
            set(selected) <= {"server", "certificate-authority-data"},
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
            selected.get("server") == cluster["endpoint"]
            and selected.get("certificate-authority-data")
            == cluster["certificateAuthority"]["data"]
            and not selected.get("insecure-skip-tls-verify"),
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
        addon = self.json(
            self.aws(
                "eks",
                "describe-addon",
                "--cluster-name",
                self.env["cluster"],
                "--addon-name",
                "vpc-cni",
            )
        )["addon"]
        configuration = json.loads(addon.get("configurationValues") or "{}")
        require(
            addon.get("status") == "ACTIVE"
            and configuration.get("enableNetworkPolicy") in (True, "true"),
            "The supported VPC CNI NetworkPolicy enforcement must be enabled before installation",
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
            observed = self.json(result)
            require(
                observed.get("schema") == schema
                and observed.get("database") == self.env["database"]["database"],
                "Database observation does not match the declared boundary",
            )
            self.receipt.setdefault("database_observations", {})[key] = observed

    def gateway(self):
        self.check_route_owner()
        response = httpx.get(
            self.env["origin"] + "/api/superplane/installation-support",
            timeout=20,
            follow_redirects=False,
        )
        require(
            response.status_code == 200 and response.json().get("version") == 1,
            "Existing Gateway needs the U23 transport integration before domain installation",
        )
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
            "workspace_cluster_context": self.env["workspace_cluster"],
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
        platform = next(
            (
                entry.get("values", {}).get("outputs", {})
                for entry in plan.get("planned_values", {})
                .get("root_module", {})
                .get("resources", [])
                if entry.get("address") == "data.terraform_remote_state.platform"
            ),
            {},
        )
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
        }
        self.receipt["plan_sha256"] = digest(binding)
        self.save()

    def preflight(self):
        for name in (
            "target",
            "images",
            "secrets",
            "workspace",
            "database",
            "gateway",
            "terraform",
        ):
            self.phase(name, getattr(self, name))
        self.receipt["status"] = "preflight-passed"
        self.save()

    @contextlib.contextmanager
    def exclusive(self):
        # S3's conditional PUT makes this exclusive across machines, unlike a
        # local flock or workflow-specific concurrency group. No expiry or
        # automatic stale-lock stealing can overlap a still-running migration.
        path = self.directory / "installation-lock.json"
        atomic(path, {"run_id": self.run_id, "installation_id": self.owner})
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
        etag = self.json(result)["ETag"]
        self.receipt["remote_lock"] = {
            "bucket": self.bucket,
            "key": self.lock_key,
            "etag": etag,
        }
        # Persist before mutations so SIGKILL cannot lose the recovery identity.
        self.save()
        try:
            yield
            self.aws(
                "s3api",
                "delete-object",
                "--bucket",
                self.bucket,
                "--key",
                self.lock_key,
                "--if-match",
                etag,
            )
        except BaseException:
            self.receipt["status"] = "recovery-required"
            self.save()
            raise
        else:
            self.receipt.pop("remote_lock", None)
            self.save()

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
        for doc in docs:
            current = self.existing(doc)
            require(
                current is None
                or current["metadata"].get("labels", {}).get(LABEL) == self.owner,
                "Refusing to adopt or modify an object owned by another installation",
            )
        self.kube(
            "apply",
            "--server-side",
            "--field-manager=superplane-installer",
            "-f",
            "-",
            data=yaml.safe_dump_all(docs),
        )
        for doc in docs:
            current = self.existing(doc)
            require(current is not None, "Applied object is absent")
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
            (
                "superplane-workspace-access",
                self.env["namespace"],
                "workspace_access",
                {"kubeconfig": "kubeconfig"},
            ),
        ]
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
            "workspace_id": self.env["workspace_id"],
            "initial_grant_policy": "workspace:administer; existing grants preserved",
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

    def check_route_owner(self):
        current = self.aws(
            "ssm", "get-parameter", "--name", self.route_name, allow_failure=True
        )
        if current.returncode == 0:
            value = json.loads(self.json(current)["Parameter"]["Value"])
            require(
                value.get("installation_id") == self.owner
                and value.get("namespace") == self.env["namespace"],
                "Public route belongs to another installation or namespace",
            )
        elif "ParameterNotFound" not in current.stderr:
            raise Refusal("Cannot establish public route ownership")

    def route(self, enabled):
        self.check_route_owner()
        value = {
            "version": 1,
            "enabled": enabled,
            "namespace": self.env["namespace"],
            "installation_id": self.owner,
            "release_id": self.release,
        }
        self.aws(
            "ssm",
            "put-parameter",
            "--name",
            self.route_name,
            "--type",
            "String",
            "--overwrite",
            "--value",
            json.dumps(value),
        )
        self.receipt["public_route_enabled"] = enabled
        self.save()

    def private_services(self):
        namespace = self.env["namespace"]
        health = self.json(
            self.kube(
                "get",
                "--raw",
                f"/api/v1/namespaces/{namespace}/services/superplane-api:8000/proxy/health",
            )
        )
        require(
            health.get("domain_auth_enforced") is True
            and health.get("cognito_enabled") is True,
            "API is not enforcing the deployed ADP identity policy",
        )
        for name, port, path in (
            ("superplane-platform-monitor", 9090, "healthz"),
            ("superplane-controller", 8081, "readyz"),
        ):
            self.kube(
                "get",
                "--raw",
                f"/api/v1/namespaces/{namespace}/services/{name}:{port}/proxy/{path}",
            )
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
            "skypilot_authenticated_health": True,
            "database": database,
        }
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
                "Monitor/controller have not delivered fresh authenticated observations (controller healthy; unchecked monitor dimensions remain explicit) for this workspace",
            )
            time.sleep(2)

    def verify(self, token):
        require(
            bool(token),
            "Verification requires an ADP token in SUPERPLANE_VERIFICATION_TOKEN",
        )
        base = self.env["origin"] + "/api/superplane/v1"
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
            "route_destination": f"superplane-api.{self.env['namespace']}.svc.cluster.local:8000",
            "authenticated_status": 200,
            "authenticated": True,
            "private_routes_denied": True,
            "ungranted_workspace_denied": True,
            "live_workload_parity": "not evaluated",
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
                self.phase("disable-route", lambda: self.route(False))
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
                self.receipt["status"] = "installed-and-verified"
                self.save()
            except BaseException:
                self.route(False)
                raise

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
                and previous.get("remote_lock")
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
        require(
            lock.get("bucket") == self.bucket
            and lock.get("key") == self.lock_key
            and lock.get("etag"),
            "Receipt has no matching retained installation lock",
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
        self.receipt.pop("remote_lock")
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
        self.lock = previous["release_lock"]
        self.release = previous["release_id"]
        self.docs = render(self.env, self.lock)
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
                self.phase("disable-route", lambda: self.route(False))
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
                self.route(False)
                raise

    def cleanup(self, previous):
        require(
            previous.get("installation_id") == self.owner
            and previous.get("environment") == self.env,
            "Cleanup receipt belongs to another environment",
        )
        self.target(verify_source=False)
        # Namespace, credentials, backups, domain DB and Terraform/ECR resources
        # are retained for recovery. Remove only recorded workload object UIDs.
        resources = {
            "Deployment": ("/apis/apps/v1", "deployments"),
            "Service": ("/api/v1", "services"),
            "Job": ("/apis/batch/v1", "jobs"),
            "ConfigMap": ("/api/v1", "configmaps"),
            "NetworkPolicy": ("/apis/networking.k8s.io/v1", "networkpolicies"),
            "ServiceAccount": ("/api/v1", "serviceaccounts"),
        }
        with self.exclusive():
            self.route(False)
            self.receipt["removed"] = []
            order = {
                "Deployment": 0,
                "Job": 1,
                "Service": 2,
                "ConfigMap": 3,
                "ServiceAccount": 4,
                "NetworkPolicy": 5,
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
