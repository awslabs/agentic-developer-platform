"""Maintain one paused Superplane worker binding through its shared Terraform owner.

The native saved plan and backend lock remain authoritative. Two unrelated
dependencies are preserved using the established tick Redis maintenance pattern;
the plan guard still refuses every managed change outside the binding.
"""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import boto3

from .config import LABEL, Refusal, digest, identity, image, load, require, validate
from .lifecycle_worker import expected_binding
from .runner import atomic, local_lock
from .runtime_maintenance import container, objects, paused, payload

ROOT = Path(__file__).resolve().parents[4]
WEBHOOK = ROOT / "modules/agent-factory/webhook-ingress/infra"
TARGET = "kubernetes_config_map.worker_gateway[0]"
KEY = "ADP_DOMAIN_OPERATION_BINDINGS"
NAME = "adp-worker-authority-config"
OVERRIDE_NAME = "zz_superplane_binding_maintenance_override.tf.json"
MAINTENANCE_SOURCE_CHANGES = frozenset(
    "modules/domain-apps/superplane/" + name
    for name in (
        "installation/runtime_maintenance.py",
        "installation/shared_binding_maintenance.py",
        "tests/installer_checks/test_runtime_maintenance.py",
        "tests/installer_checks/test_shared_binding_maintenance.py",
        "installation/RUNTIME-MAINTENANCE.md",
        "infra/shared-operation-authority/BINDING-MAINTENANCE.md",
    )
)
PRESERVATION = {
    "resource": {
        "aws_iam_policy": {
            "agent_authority_boundary": {
                "lifecycle": {"ignore_changes": ["policy", "tags"]}
            }
        },
        "aws_secretsmanager_secret": {
            "marker_signing_key": {
                "lifecycle": {"ignore_changes": ["recovery_window_in_days", "tags"]}
            }
        },
    }
}


def known(value):
    if isinstance(value, dict):
        return all(known(v) for v in value.values())
    if isinstance(value, list):
        return all(known(v) for v in value)
    return value is False


def revised_data(before, worker_digest):
    require(
        re.fullmatch(r"sha256:[a-f0-9]{64}", worker_digest),
        "An immutable worker digest is required",
    )
    result = copy.deepcopy(before)
    rows = json.loads(before[KEY])
    require(
        isinstance(rows, list)
        and len(rows) == 1
        and rows[0].get("domain") == "superplane"
        and rows[0].get("current_identity_enforced") is True,
        "Exactly one current Superplane binding is required",
    )
    rows[0]["worker_image_digests"] = [worker_digest]
    result[KEY] = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return result


def same_data(actual, expected):
    """Terraform canonicalizes the binding JSON without changing its meaning."""
    return {k: v for k, v in actual.items() if k != KEY} == {
        k: v for k, v in expected.items() if k != KEY
    } and json.loads(actual[KEY]) == json.loads(expected[KEY])


def check_plan(document, before, worker_digest):
    require(
        document.get("format_version")
        and document.get("errored") is not True
        and (
            document.get("complete") is not False
            or (document.get("applyable") is True and document.get("errored") is False)
        )
        and not document.get("deferred_changes"),
        "The native shared-owner plan is incomplete",
    )
    desired = revised_data(before["data"], worker_digest)
    seen, target = set(), None
    for row in document.get("resource_changes", []):
        address, change = row["address"], row["change"]
        require(address not in seen, "Duplicate Terraform resource address")
        seen.add(address)
        require(
            not row.get("previous_address") and not change.get("importing"),
            "Binding maintenance cannot move or import resources",
        )
        actions = change["actions"]
        if row.get("mode") == "data":
            require(actions in (["read"], ["no-op"]), "Unexpected data action")
            continue
        require(row.get("mode") == "managed", "Unknown Terraform resource mode")
        if address != TARGET:
            require(actions == ["no-op"], "Plan changes an unrelated shared resource")
            continue
        target = change
        require(actions in (["update"], ["no-op"]), "Binding must retain its object")
        old, new = change["before"], change["after"]
        unknown = copy.deepcopy(change.get("after_unknown", {}))
        for metadata_unknown in unknown.get("metadata", []):
            metadata_unknown.pop("resource_version", None)
        require(
            isinstance(old, dict) and isinstance(new, dict) and known(unknown),
            "Binding change is unresolved",
        )
        normalized = copy.deepcopy(new)
        require(
            len(normalized.get("metadata", [])) == 1, "Binding metadata is ambiguous"
        )
        require(
            normalized["metadata"][0].get("resource_version")
            in (None, old["metadata"][0]["resource_version"]),
            "Unexpected configured resource version",
        )
        normalized["metadata"][0]["resource_version"] = old["metadata"][0][
            "resource_version"
        ]
        require(
            {k: v for k, v in old.items() if k != "data"}
            == {k: v for k, v in normalized.items() if k != "data"},
            "Plan changes binding metadata or binary data",
        )
        meta = before["metadata"]
        metadata = old["metadata"]
        require(
            len(metadata) == 1
            and all(
                metadata[0].get(k) == meta[v]
                for k, v in {
                    "name": "name",
                    "namespace": "namespace",
                    "uid": "uid",
                    "resource_version": "resourceVersion",
                }.items()
            )
            and old["id"] == meta["namespace"] + "/" + NAME
            and old["data"] == before["data"],
            "Native plan differs from the observed shared binding",
        )
        require(
            same_data(new["data"], desired),
            "Plan exceeds the exact worker digest change",
        )
    require(target is not None, "Native plan omitted the existing binding")

    # Targeting makes Terraform's whole-configuration complete flag false even
    # for an applyable plan. In-graph resource checks must still have passed;
    # checks outside the selected graph may remain unevaluated.
    def configuration_address(address):
        return re.sub(r'\[(?:\d+|"(?:[^"\\]|\\.)*")\]', "", address)

    selected_resources = {configuration_address(address) for address in seen}
    for check in document.get("checks", []):
        address = check.get("address", {})
        selected = (
            address.get("kind") == "resource"
            and configuration_address(address.get("to_display", ""))
            in selected_resources
        )
        for result in (check, *check.get("instances", [])):
            require(
                result.get("status") == "pass"
                or (not selected and result.get("status") == "unknown"),
                "Native plan has a failed or unresolved selected check",
            )
    require(
        all(
            "delete" not in r["change"]["actions"]
            for r in document.get("resource_drift", [])
        ),
        "Binding maintenance cannot drop missing resources from state",
    )
    return desired


def maintenance_override(before, worker_digest):
    """Pin this maintenance plan to the observed shared configuration.

    Ordinary deployment inputs may still refer to a retired initial provider.
    Preserve the resource's ownership, dependencies and lifecycle checks while
    replacing only its data expression for this saved binding plan. The native
    plan guard and apply-time observation still enforce the exact digest change.
    """
    value = copy.deepcopy(PRESERVATION)
    value["resource"]["kubernetes_config_map"] = {
        "worker_gateway": {"data": revised_data(before["data"], worker_digest)}
    }
    return value


def preservation_sources():
    """Refuse if future source adds lifecycle guards the override would replace."""
    result = {}
    for file, kind, name in (
        ("agent-authority-boundary.tf", "aws_iam_policy", "agent_authority_boundary"),
        ("secrets.tf", "aws_secretsmanager_secret", "marker_signing_key"),
    ):
        raw = (WEBHOOK / file).read_text()
        match = re.search(
            rf'^resource "{kind}" "{name}" \{{\n(.*?)^\}}', raw, re.M | re.S
        )
        require(
            match is not None
            and not re.search(r"\b(lifecycle|provisioner|connection)\s*\{", match[1]),
            "Preserved dependency gained lifecycle behavior; review its maintenance contract",
        )
        result[file] = hashlib.sha256(raw.encode()).hexdigest()
    return result


class Maintenance:
    def __init__(
        self, env, lock, inputs, directory, *, maintenance_source_revision=None
    ):
        self.env, self.lock, self.inputs = env, lock, inputs
        self.maintenance_source_revision = maintenance_source_revision
        self.directory, self.owner = directory, identity(env)
        self.namespace = inputs.get("gateway_namespace", "adp-gateway")
        require(
            self.namespace == env.get("gateway_namespace"),
            "Shared inputs and installation Gateway namespace differ",
        )
        self.kubeconfig = directory / "kubeconfig"
        self.backend = f"{env['environment']}/modules/webhook-ingress/terraform.tfstate"
        self.bucket = "adp-terraform-state-" + env["account_id"]
        self.session = boto3.Session(region_name=env["region"])

    def run(self, args, *, data=None, tf=False, timeout=120):
        settings = dict(os.environ, AWS_PAGER="", TF_IN_AUTOMATION="true")
        if tf:
            settings["TF_DATA_DIR"] = str(self.directory / "tfdata")
        try:
            result = subprocess.run(
                args,
                input=data,
                text=True,
                capture_output=True,
                env=settings,
                cwd=WEBHOOK if tf else ROOT,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise Refusal(
                "Maintenance command unavailable or timed out; preserve the receipt"
            ) from None
        if result.returncode:
            atomic(
                self.directory / "private-command-failure.json",
                {"stdout": result.stdout, "stderr": result.stderr},
            )
            raise Refusal("Maintenance command failed; inspect its private diagnostic")
        return result.stdout

    def kube(self, *args):
        return self.run(
            [
                "kubectl",
                "--kubeconfig",
                str(self.kubeconfig),
                "--request-timeout=30s",
                *args,
            ]
        )

    @staticmethod
    def json(value):
        return json.loads(value)

    def source(self):
        revision = self.run(["git", "rev-parse", "HEAD"]).strip()
        runtime_revision = self.lock["source_revision"]
        selected_revision = self.maintenance_source_revision or runtime_revision
        require(
            re.fullmatch(r"[a-f0-9]{40}", selected_revision)
            and revision == selected_revision
            and not self.run(
                ["git", "status", "--porcelain", "--untracked-files=normal"]
            ).strip(),
            "Binding maintenance requires the exact clean release checkout",
        )
        tracked = set(
            self.run(["git", "ls-files", str(WEBHOOK.relative_to(ROOT))]).splitlines()
        )
        require(
            all(
                str(path.relative_to(ROOT)) in tracked for path in WEBHOOK.glob("*.tf*")
            ),
            "Untracked Terraform configuration could change maintenance behavior",
        )
        changes = []
        if self.maintenance_source_revision is not None:
            require(
                self.run(["git", "merge-base", runtime_revision, revision]).strip()
                == runtime_revision,
                "Maintenance source must descend from the original runtime source",
            )
            changes = self.run(
                [
                    "git",
                    "diff",
                    "--no-renames",
                    "--name-only",
                    runtime_revision,
                    revision,
                    "--",
                ]
            ).splitlines()
            require(
                set(changes) <= MAINTENANCE_SOURCE_CHANGES,
                "Maintenance source changes runtime, Terraform or unrelated files",
            )
        for candidate in sorted({revision, runtime_revision}):
            merged = json.loads(
                self.run(["gh", "api", f"repos/aws-e/adp/compare/{candidate}...main"])
            )
            require(
                merged.get("status") in {"ahead", "identical"}
                and merged.get("merge_base_commit", {}).get("sha") == candidate,
                "Binding maintenance source must already belong to main",
            )
        evidence = {
            "revision": revision,
            "preservation_sources": preservation_sources(),
        }
        if self.maintenance_source_revision is not None:
            evidence.update(
                runtime_revision=runtime_revision,
                maintenance_source_revision=self.maintenance_source_revision,
                maintenance_source_changes=changes,
            )
        return evidence

    def state(self):
        response = self.session.client("s3").get_object(
            Bucket=self.bucket, Key=self.backend
        )
        return json.loads(response["Body"].read())

    def dependencies(self, state):
        def attributes(kind, name):
            rows = [
                r
                for r in state["resources"]
                if not r.get("module") and r["type"] == kind and r["name"] == name
            ]
            require(
                len(rows) == 1 and len(rows[0]["instances"]) == 1,
                "Preserved shared dependency state is ambiguous",
            )
            return rows[0]["instances"][0]["attributes"]

        policy_state = attributes("aws_iam_policy", "agent_authority_boundary")
        secret_state = attributes("aws_secretsmanager_secret", "marker_signing_key")
        iam = self.session.client("iam")
        policy = iam.get_policy(PolicyArn=policy_state["arn"])["Policy"]
        version = iam.get_policy_version(
            PolicyArn=policy["Arn"], VersionId=policy["DefaultVersionId"]
        )["PolicyVersion"]
        policy_tags = iam.list_policy_tags(PolicyArn=policy["Arn"])
        secret = self.session.client("secretsmanager").describe_secret(
            SecretId=secret_state["arn"]
        )
        require(
            policy["Arn"] == policy_state["arn"]
            and version["VersionId"] == policy["DefaultVersionId"]
            and version["IsDefaultVersion"] is True
            and not policy_tags.get("IsTruncated")
            and secret["ARN"] == secret_state["arn"]
            and not secret.get("DeletedDate"),
            "Preserved dependency identity or live deletion state changed",
        )
        return {
            "worker_boundary": {
                "arn": policy["Arn"],
                "policy_id": policy["PolicyId"],
                "default_version": policy["DefaultVersionId"],
                "document": version["Document"],
                "tags": sorted(
                    policy_tags.get("Tags", []), key=lambda item: item["Key"]
                ),
            },
            "marker_secret": {
                "metadata": {
                    key: secret.get(key)
                    for key in (
                        "ARN",
                        "Name",
                        "KmsKeyId",
                        "Description",
                        "DeletedDate",
                        "RotationEnabled",
                        "RotationLambdaARN",
                        "RotationRules",
                        "OwningService",
                        "PrimaryRegion",
                    )
                },
                "tags": sorted(secret.get("Tags", []), key=lambda item: item["Key"]),
                # This is a Terraform deletion input, not a live AWS setting:
                # DescribeSecret exposes a recovery deadline only after deletion.
                "terraform_recovery_window_in_days": secret_state[
                    "recovery_window_in_days"
                ],
            },
        }

    def inspect(self):
        env = self.env
        selected = self.inputs["domain_operation_bindings"]["superplane"]
        operator = self.session.client("sts").get_caller_identity()
        iam = self.session.client("iam")
        role = iam.get_role(RoleName=selected["operator_role_arn"].rsplit("/", 1)[1])[
            "Role"
        ]
        require(
            operator["Account"] == env["account_id"]
            and role["Arn"] == selected["operator_role_arn"]
            and operator["UserId"].split(":")[0] == role["RoleId"],
            "Current shared-owner role differs from selected authority",
        )
        cluster = self.session.client("eks").describe_cluster(name=env["cluster"])[
            "cluster"
        ]
        require(
            cluster["status"] == "ACTIVE"
            and cluster["arn"]
            == f"arn:aws:eks:{env['region']}:{env['account_id']}:cluster/{env['cluster']}",
            "Shared binding targets another management cluster",
        )
        self.run(
            [
                "aws",
                "eks",
                "update-kubeconfig",
                "--region",
                env["region"],
                "--name",
                env["cluster"],
                "--kubeconfig",
                str(self.kubeconfig),
            ]
        )
        configuration = self.json(
            self.kube("config", "view", "--minify", "--raw", "-o", "json")
        )["clusters"]
        require(
            len(configuration) == 1
            and configuration[0]["cluster"].get("server") == cluster["endpoint"]
            and configuration[0]["cluster"].get("certificate-authority-data")
            == cluster["certificateAuthority"]["data"]
            and not configuration[0]["cluster"].get("insecure-skip-tls-verify"),
            "Management Kubernetes identity differs",
        )
        values = objects(self)
        namespace = self.json(
            self.kube("get", "namespace", env["namespace"], "-o", "json")
        )
        for value in [namespace, *values]:
            require(
                value["metadata"].get("labels", {}).get(LABEL) == self.owner
                and value["metadata"].get("uid")
                and not value["metadata"].get("deletionTimestamp"),
                "Domain installation ownership changed",
            )
        paused(self, values)
        require(
            container(
                values[1]["spec"]["jobTargetRef"]["template"]["spec"], "paid-worker"
            )["image"]
            == image(self.lock, "superplane-paid-worker")
            and json.loads(values[2]["data"]["binding.json"])
            == expected_binding(env, self.lock),
            "First maintain the paused domain worker and its local binding",
        )
        cm = self.json(
            self.kube("get", "configmap", NAME, "-n", self.namespace, "-o", "json")
        )
        require(
            cm["metadata"]["name"] == NAME
            and cm["metadata"]["namespace"] == self.namespace
            and not cm["metadata"].get("deletionTimestamp"),
            "Shared binding identity differs",
        )
        state = self.state()
        registrations = [
            r
            for r in state["resources"]
            if r.get("module") == "module.superplane_operation_authority"
            and r["type"] == "terraform_data"
            and r["name"] == "domain_operation_registration"
        ]
        require(
            len(registrations) == 1 and len(registrations[0]["instances"]) == 1,
            "Shared registration state is ambiguous",
        )
        registration = registrations[0]["instances"][0]["attributes"]["input"]["value"]
        require(
            registration["operator_role_arn"] == selected["operator_role_arn"]
            and registration["account_id"] == env["account_id"]
            and registration["region"] == env["region"]
            and registration["environment"] == env["environment"]
            and registration["domain_org_id"] == env["org_id"]
            and registration["adp_org_id"] == env["adp_org_id"],
            "Shared owner registration and installation differ",
        )
        binding = json.loads(cm["data"][KEY])
        require(len(binding) == 1, "Ambiguous domain binding")
        for kind in ("producer", "worker"):
            actor = registration[kind]
            require(
                selected[kind + "_role_arn"] == actor["role_arn"]
                and selected[kind + "_role_id"] == actor["role_id"]
                and iam.get_role(RoleName=actor["role_arn"].rsplit("/", 1)[1])["Role"][
                    "RoleId"
                ]
                == actor["role_id"]
                and binding[0][kind + "_registry_id"] == actor["agent_id"],
                "Current registered domain role identity changed",
            )
        require(
            binding[0]["worker_namespace"] == env["namespace"]
            and binding[0]["org_id"] == env["org_id"],
            "Binding targets another domain installation",
        )
        return {
            "configmap": cm,
            "domain_objects": [payload(v) for v in [namespace, *values]],
            "operator_role_id": role["RoleId"],
            "state_lineage": state["lineage"],
            "state_serial": state["serial"],
            "preserved_dependencies": self.dependencies(state),
        }

    def plan(self):
        require(
            not (self.directory / "receipt.json").exists(),
            "Use a new private maintenance directory",
        )
        source, before = self.source(), self.inspect()
        selected = self.inputs["domain_operation_bindings"]["superplane"]["binding"]
        require(
            json.loads(before["configmap"]["data"][KEY])
            == [dict(selected, current_identity_enforced=True)],
            "Inputs differ from the current shared binding",
        )
        desired = copy.deepcopy(self.inputs)
        desired["domain_operation_bindings"]["superplane"]["binding"][
            "worker_image_digests"
        ] = [self.lock["images"]["superplane-paid-worker"]]
        atomic(self.directory / "desired-inputs.tfvars.json", desired)
        override = WEBHOOK / OVERRIDE_NAME
        preservation = maintenance_override(
            before["configmap"], self.lock["images"]["superplane-paid-worker"]
        )
        raw = json.dumps(preservation, sort_keys=True).encode()
        with override.open("xb") as stream:
            stream.write(raw)
        override.chmod(0o600)
        try:
            self.run(
                [
                    "terraform",
                    "init",
                    "-input=false",
                    "-reconfigure",
                    f"-backend-config=bucket={self.bucket}",
                    f"-backend-config=key={self.backend}",
                    f"-backend-config=region={self.env['region']}",
                    "-backend-config=encrypt=true",
                    "-backend-config=dynamodb_table=adp-terraform-locks",
                ],
                tf=True,
                timeout=600,
            )
            self.run(
                [
                    "terraform",
                    "plan",
                    "-input=false",
                    "-lock=true",
                    "-lock-timeout=30s",
                    f"-var-file={ROOT}/environments/{self.env['environment']}/modules/webhook-ingress.tfvars",
                    f"-var-file={self.directory}/desired-inputs.tfvars.json",
                    "-target=" + TARGET,
                    f"-out={self.directory}/binding.tfplan",
                ],
                tf=True,
                timeout=600,
            )
        finally:
            require(
                override.read_bytes() == raw,
                "Preservation override changed; retain it for investigation",
            )
            override.unlink()
        document = self.native_plan()
        check_plan(
            document, before["configmap"], self.lock["images"]["superplane-paid-worker"]
        )
        receipt = {
            "kind": "shared-worker-binding-maintenance-v1",
            "status": "planned",
            "environment_sha256": digest(self.env),
            "release_sha256": digest(self.lock),
            "inputs_sha256": digest(self.inputs),
            "desired_inputs_sha256": digest(desired),
            "source": source,
            "override": preservation,
            "before": before,
            "plan_sha256": self.plan_hash(),
        }
        atomic(self.directory / "receipt.json", receipt)
        return receipt

    def native_plan(self):
        return json.loads(
            self.run(
                ["terraform", "show", "-json", str(self.directory / "binding.tfplan")],
                tf=True,
                timeout=600,
            )
        )

    def plan_hash(self):
        return hashlib.sha256(
            (self.directory / "binding.tfplan").read_bytes()
        ).hexdigest()

    def checked_receipt(self):
        receipt = load(self.directory / "receipt.json")
        require(
            receipt.get("kind") == "shared-worker-binding-maintenance-v1"
            and receipt["plan_sha256"] == self.plan_hash(),
            "Saved native plan or receipt changed",
        )
        require(
            receipt["environment_sha256"] == digest(self.env)
            and receipt["release_sha256"] == digest(self.lock)
            and receipt["inputs_sha256"] == digest(self.inputs)
            and receipt["desired_inputs_sha256"]
            == digest(load(self.directory / "desired-inputs.tfvars.json"))
            and receipt["source"] == self.source()
            and receipt["override"]
            == maintenance_override(
                receipt["before"]["configmap"],
                self.lock["images"]["superplane-paid-worker"],
            ),
            "Maintenance inputs or source changed",
        )
        return receipt

    def apply(self, approved):
        receipt = self.checked_receipt()
        require(
            receipt["status"] == "planned",
            "An attempted native apply must be observed, never automatically repeated",
        )
        require(
            approved == receipt["plan_sha256"] == self.plan_hash(),
            "Exact native saved-plan approval is required",
        )
        before = self.inspect()
        require(
            before == receipt["before"],
            "Live binding, owner, state or paused domain changed after planning",
        )
        check_plan(
            self.native_plan(),
            before["configmap"],
            self.lock["images"]["superplane-paid-worker"],
        )
        require(
            self.inspect() == before,
            "Binding, domain or preserved dependency changed while planning",
        )
        receipt["status"] = "native-apply-pending"
        atomic(self.directory / "receipt.json", receipt)
        self.run(
            [
                "terraform",
                "apply",
                "-input=false",
                "-lock=true",
                "-lock-timeout=30s",
                str(self.directory / "binding.tfplan"),
            ],
            tf=True,
            timeout=600,
        )
        return self.observe()

    def observe(self):
        receipt = self.checked_receipt()
        current = self.inspect()
        before = receipt["before"]
        expected = revised_data(
            before["configmap"]["data"], self.lock["images"]["superplane-paid-worker"]
        )
        require(
            current["domain_objects"] == before["domain_objects"]
            and current["operator_role_id"] == before["operator_role_id"]
            and current["state_lineage"] == before["state_lineage"]
            and current["preserved_dependencies"] == before["preserved_dependencies"]
            and current["configmap"]["metadata"]["uid"]
            == before["configmap"]["metadata"]["uid"],
            "Ownership or paused domain changed during maintenance",
        )
        require(
            {k: v for k, v in payload(current["configmap"]).items() if k != "data"}
            == {k: v for k, v in payload(before["configmap"]).items() if k != "data"},
            "Shared binding metadata or binary data changed during maintenance",
        )
        state = self.state()
        rows = [
            r
            for r in state["resources"]
            if not r.get("module")
            and r["type"] == "kubernetes_config_map"
            and r["name"] == "worker_gateway"
        ]
        require(
            len(rows) == 1 and len(rows[0]["instances"]) == 1,
            "Shared binding state is ambiguous",
        )
        recorded = rows[0]["instances"][0]["attributes"]
        require(
            same_data(current["configmap"]["data"], expected)
            and same_data(recorded["data"], expected)
            and recorded["metadata"][0]["uid"]
            == current["configmap"]["metadata"]["uid"],
            "Native apply is not proven complete in both live binding and owner state",
        )
        receipt["status"] = "binding-and-native-state-observed"
        receipt["after"] = current
        atomic(self.directory / "receipt.json", receipt)
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("environment", "release-lock", "inputs", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    for name in ("plan", "apply", "observe"):
        mode.add_argument("--" + name, action="store_true")
    parser.add_argument("--approved-plan-sha256")
    parser.add_argument(
        "--maintenance-source-revision",
        help="Exact merged tooling-only descendant; preserves original runtime release lock",
    )
    args = parser.parse_args(argv)
    try:
        env, lock, inputs = (
            load(args.environment),
            load(args.release_lock),
            load(args.inputs),
        )
        validate(env, lock, control_plane_only=env.get("control_plane_only", False))
        os.umask(0o077)
        args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        with local_lock(args.output):
            operation = Maintenance(
                env,
                lock,
                inputs,
                args.output.resolve(),
                maintenance_source_revision=args.maintenance_source_revision,
            )
            receipt = (
                operation.plan()
                if args.plan
                else operation.apply(args.approved_plan_sha256)
                if args.apply
                else operation.observe()
            )
        print(
            json.dumps(
                {
                    "status": receipt["status"],
                    "plan_sha256": receipt["plan_sha256"],
                    "receipt": str(args.output / "receipt.json"),
                }
            )
        )
        return 0
    except Refusal as error:
        print(json.dumps({"status": "refused", "reason": str(error)}))
    except Exception:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "Preserve the private native plan and receipt",
                }
            )
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
