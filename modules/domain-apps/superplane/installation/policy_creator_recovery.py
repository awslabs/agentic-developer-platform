"""Reviewed recovery of the installer's own NetworkPolicy CREATE field ownership.

Kubernetes tracks CREATE as Update, separately from the same manager's later
Apply. Only the three native policies, exact reviewed recipes, original UIDs and
fields owned exclusively by superplane-installer may transfer ownership. The
ScaledJob pause flag can transfer only at verified activation/compensation, with
the entire reviewed worker spec unchanged. The
immutable network revision and workload release are preserved. Default is read-only.
"""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess

import yaml

from . import native_egress
from .config import LABEL, MODULE, Refusal, digest, load, require, validate
from .native_egress_probe import verified_private_hosts
from .projection_recovery import PREFIX, git
from .runner import Installer, local_lock

NAMES = {
    "superplane-binding-check",
    "superplane-network-probe",
    "superplane-paid-worker-preparation-deny",
}
ANNOTATION = "adp.aws-e.io/activation-egress"
ALLOWED = {
    PREFIX + "installation/policy_creator_recovery.py",
    PREFIX + "tests/installer_checks/test_policy_creator_recovery.py",
}


def source_proof(baseline):
    require(
        not git("status", "--porcelain", "--untracked-files=normal"),
        "Recovery source must be clean",
    )
    revision = git("rev-parse", "HEAD")
    subprocess.run(
        ["git", "-C", str(MODULE), "merge-base", "--is-ancestor", baseline, revision],
        check=True,
        capture_output=True,
        timeout=30,
    )
    changed = set(git("diff", "--name-only", baseline, revision).splitlines())
    require(changed == ALLOWED, "Recovery may add only its entrypoint and tests")
    return {
        "baseline_operator_source": baseline,
        "operator_source": revision,
        "sha256": {
            path: hashlib.sha256(
                (MODULE / path.removeprefix(PREFIX)).read_bytes()
            ).hexdigest()
            for path in sorted(changed)
        },
    }


def normalized_spec(doc):
    spec = copy.deepcopy(doc["spec"])
    for field in ("ingress", "egress"):
        if spec.get(field) == []:
            spec.pop(field)
    return spec


def policy_environment(installer, name):
    # Match the existing renderer: worker projection uses discovered cluster DNS;
    # the two standalone probes render directly from immutable environment inputs.
    return (
        installer.network_environment
        if name == "superplane-paid-worker-preparation-deny"
        else installer.env
    )


def recipe(env, name):
    return native_egress.rules(env, database=name != "superplane-binding-check")


def checked_policy(env, owner, current, *, runtime_env=None):
    meta = current["metadata"]
    name = meta["name"]
    require(
        current["kind"] == "NetworkPolicy"
        and name in NAMES
        and meta.get("namespace") == env["namespace"]
        and meta.get("labels", {}).get(LABEL) == owner
        and meta.get("uid")
        and meta.get("resourceVersion")
        and not meta.get("deletionTimestamp"),
        "Policy identity/owner differs",
    )
    old_env = copy.deepcopy(env)
    old_env["paid_worker"]["egress"].pop("interface_endpoints")
    old, new = recipe(old_env, name), recipe(env, name)
    spec = current["spec"]
    require(
        spec.get("policyTypes") == ["Ingress", "Egress"] and not spec.get("ingress"),
        "Policy isolation types or ingress changed",
    )
    # Activation uses immutable installer.env while its preparation annotation
    # retains the rendered, discovered-DNS projection. Preserve both contracts.
    runtime_env = env if runtime_env is None else runtime_env
    prior_runtime = copy.deepcopy(runtime_env)
    prior_runtime["paid_worker"]["egress"].pop("interface_endpoints")
    allowed = [recipe(prior_runtime, name), recipe(runtime_env, name)]
    if name == "superplane-paid-worker-preparation-deny":
        allowed.append([])
        annotation = meta.get("annotations", {}).get(ANNOTATION)
        require(
            isinstance(annotation, str) and json.loads(annotation) in [old, new],
            "Worker activation recipe differs",
        )
    require(
        spec.get("egress", []) in allowed, "Policy is outside original/reviewed recipes"
    )
    return old, new


def policy_args(env, owner, desired, current, reviewed_uids, *, runtime_env=None):
    """Force only a proven conflict with this exact installer's CREATE manager."""
    if (
        desired.get("kind") != "NetworkPolicy"
        or desired["metadata"]["name"] not in NAMES
    ):
        return []
    name = desired["metadata"]["name"]
    checked_policy(env, owner, current, runtime_env=runtime_env)
    require(
        current["metadata"]["uid"] == reviewed_uids.get(name)
        and desired["metadata"].get("uid") == current["metadata"]["uid"]
        and desired["metadata"].get("resourceVersion")
        == current["metadata"]["resourceVersion"],
        "Policy was replaced or changed during apply",
    )
    checked_policy(env, owner, desired, runtime_env=runtime_env)
    before, after = normalized_spec(current), normalized_spec(desired)
    paths = []
    if before.get("egress") != after.get("egress"):
        paths.append(("f:spec", "f:egress"))
    before.pop("egress", None)
    after.pop("egress", None)
    require(before == after, "Recovery cannot change selectors or other policy fields")
    old_annotation = current["metadata"].get("annotations", {}).get(ANNOTATION)
    new_annotation = desired["metadata"].get("annotations", {}).get(ANNOTATION)
    if old_annotation != new_annotation:
        require(
            name == "superplane-paid-worker-preparation-deny",
            "Unexpected annotation change",
        )
        paths.append(("f:metadata", "f:annotations", "f:" + ANNOTATION))
    require(
        all(
            current["metadata"].get("labels", {}).get(k) == v
            for k, v in desired["metadata"].get("labels", {}).items()
        ),
        "Recovery cannot change policy labels",
    )
    require(
        all(
            current["metadata"].get("annotations", {}).get(k) == v
            for k, v in desired["metadata"].get("annotations", {}).items()
            if k != ANNOTATION
        ),
        "Recovery cannot change other policy annotations",
    )
    creators = False
    for path in paths:
        owners = []
        for entry in current["metadata"].get("managedFields", []):
            fields = entry.get("fieldsV1", {})
            for part in path:
                if part not in fields:
                    break
                fields = fields[part]
            else:
                owners.append(
                    (
                        entry.get("manager"),
                        entry.get("operation"),
                        entry.get("subresource", ""),
                    )
                )
        require(
            owners
            and all(
                manager == "superplane-installer"
                and operation in {"Apply", "Update"}
                and not subresource
                for manager, operation, subresource in owners
            ),
            "Changed field has foreign or missing ownership",
        )
        creators = creators or any(operation == "Update" for _, operation, _ in owners)
    return ["--force-conflicts"] if creators else []


PAUSED = "autoscaling.keda.sh/paused"


def pause_owners(current):
    return [
        (entry.get("manager"), entry.get("operation"), entry.get("subresource", ""))
        for entry in current["metadata"].get("managedFields", [])
        if "f:" + PAUSED
        in entry.get("fieldsV1", {}).get("f:metadata", {}).get("f:annotations", {})
    ]


def scaled_job_args(owner, namespace, desired, current, reviewed, stage, prepared):
    """Transfer only the creator-owned pause flag after ordinary activation gates."""
    from .lifecycle_worker import contains

    meta, target = current["metadata"], desired["metadata"]
    require(
        current["kind"] == desired["kind"] == "ScaledJob"
        and meta["name"] == target["name"] == "superplane-paid-worker"
        and meta["namespace"] == target["namespace"] == namespace
        and meta.get("labels", {}).get(LABEL) == owner
        and meta["uid"] == target.get("uid") == reviewed["uid"]
        and meta["resourceVersion"] == target.get("resourceVersion")
        and not meta.get("deletionTimestamp"),
        "ScaledJob identity/owner changed",
    )
    before, after = (
        meta.get("annotations", {}).get(PAUSED),
        target.get("annotations", {}).get(PAUSED),
    )
    if before == after:
        return []
    require(
        {before, after} == {"true", "false"}
        and stage in {"adapter-activation", "adapter-disabled-compensation"}
        and (after == "true" or prepared),
        "Pause transfer requires verified activation or compensation",
    )
    require(
        digest(current["spec"]) == reviewed["spec_sha256"]
        and digest(desired["spec"]) == reviewed["desired_spec_sha256"]
        and contains(current["spec"], desired["spec"]),
        "Pause recovery cannot change the worker spec",
    )
    require(
        all(
            meta.get("labels", {}).get(k) == v
            for k, v in target.get("labels", {}).items()
        )
        and all(
            meta.get("annotations", {}).get(k) == v
            for k, v in target.get("annotations", {}).items()
            if k != PAUSED
        ),
        "Pause recovery cannot change other metadata",
    )
    owners = pause_owners(current)
    require(
        owners
        and all(
            manager == "superplane-installer"
            and operation in {"Apply", "Update"}
            and not subresource
            for manager, operation, subresource in owners
        ),
        "Pause flag has foreign or missing ownership",
    )
    return (
        ["--force-conflicts"]
        if any(operation == "Update" for _, operation, _ in owners)
        else []
    )


class PolicyCreatorRecoveryInstaller(Installer):
    def target(self, verify_source=True):
        require(
            source_proof(self.recovery_source["baseline_operator_source"])
            == self.recovery_source,
            "Recovery operator source changed",
        )
        super().target(verify_source=False)
        self.receipt["verified_interface_endpoints"] = verified_private_hosts(self)
        self.save()
        if verify_source:
            self.verify_image_sources()

    def kube(self, *args, data=None, **kwargs):
        if args[:2] == ("apply", "--server-side") and data:
            doc = yaml.safe_load(data)
            if doc.get("kind") == "NetworkPolicy" and doc["metadata"]["name"] in NAMES:
                current = self.json(
                    super().kube(
                        "get",
                        "networkpolicy",
                        doc["metadata"]["name"],
                        "-n",
                        self.env["namespace"],
                        "-o",
                        "json",
                        "--show-managed-fields",
                    )
                )
                extra = policy_args(
                    policy_environment(self, doc["metadata"]["name"]),
                    self.owner,
                    doc,
                    current,
                    self.reviewed_uids,
                    runtime_env=self.env,
                )
                if extra:
                    self.receipt.setdefault("policy_creator_transfers", []).append(
                        {
                            "name": doc["metadata"]["name"],
                            "uid": current["metadata"]["uid"],
                            "resource_version": current["metadata"]["resourceVersion"],
                            "prior_spec_sha256": digest(current["spec"]),
                            "desired_sha256": digest(doc),
                            "status": "attempting-reviewed-owned-transfer",
                        }
                    )
                    self.save()
                    args = (*args, *extra)
        if args[:2] == ("apply", "--server-side") and data:
            doc = yaml.safe_load(data)
            if (
                doc.get("kind") == "ScaledJob"
                and doc["metadata"]["name"] == "superplane-paid-worker"
            ):
                current = self.json(
                    super().kube(
                        "get",
                        "scaledjob",
                        "superplane-paid-worker",
                        "-n",
                        self.env["namespace"],
                        "-o",
                        "json",
                        "--show-managed-fields",
                    )
                )
                prepared = self.receipt.get("adapter_stage", {}).get(
                    "native_worker", {}
                )
                ready = (
                    prepared.get("network", {}).get("reachable") is True
                    and prepared.get("proof", {}).get("quiescent") is True
                )
                extra = scaled_job_args(
                    self.owner,
                    self.env["namespace"],
                    doc,
                    current,
                    self.reviewed_scaled_job,
                    self.receipt.get("stage"),
                    ready,
                )
                if extra:
                    self.receipt.setdefault(
                        "worker_pause_creator_transfers", []
                    ).append(
                        {
                            "uid": current["metadata"]["uid"],
                            "resource_version": current["metadata"]["resourceVersion"],
                            "desired_paused": doc["metadata"]["annotations"][PAUSED],
                            "stage": self.receipt["stage"],
                        }
                    )
                    self.save()
                    args = (*args, *extra)
        return super().kube(*args, data=data, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--confirm-stopped", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approved-review-sha256")
    parser.add_argument("--approved-plan-sha256")
    args = parser.parse_args()
    try:
        with local_lock(args.output):
            previous = load(args.output / "receipt.json")
            require(
                previous.get("status") == "failed"
                and previous["run_id"] == args.confirm_stopped
                and not previous.get("remote_lock")
                and not previous.get("lock_attempt")
                and not previous.get("temporary_preflight", {}).get("cleanup_required")
                and previous.get("public_route_enabled") is False
                and (
                    previous.get("stage") == "shared-execution-quiescence"
                    or previous.get("stage") == "adapter-disabled-compensation"
                    and previous.get("adapter_stage", {}).get("state")
                    == "disabled-restored"
                ),
                "Stopped lock-recovered disabled network revision required",
            )
            revision = previous["network_configuration_revision"]["review"]
            require(
                digest(revision)
                == previous["network_configuration_revision"]["review_sha256"],
                "Network revision review changed",
            )
            env, release = previous["environment"], previous["release_lock"]
            require(
                digest(env) == revision["environment_sha256"]
                and digest(release) == revision["release_sha256"],
                "Immutable revision inputs changed",
            )
            validate(
                env, release, control_plane_only=env.get("control_plane_only", False)
            )
            source = source_proof(revision["source"]["operator_source_revision"])
            installer = PolicyCreatorRecoveryInstaller(
                env, release, args.output.resolve()
            )
            # Read-only review never saves or resumes the receipt.
            installer.cluster_dns_ip = previous["management_dns_configuration"][
                "resolver"
            ]
            policies = {}
            for name in sorted(NAMES):
                current = installer.json(
                    installer.kube(
                        "get",
                        "networkpolicy",
                        name,
                        "-n",
                        env["namespace"],
                        "-o",
                        "json",
                        "--show-managed-fields",
                    )
                )
                checked_policy(
                    policy_environment(installer, name),
                    installer.owner,
                    current,
                    runtime_env=installer.env,
                )
                policies[name] = {
                    "uid": current["metadata"]["uid"],
                    "spec_sha256": digest(current["spec"]),
                    "metadata_sha256": digest(current["metadata"]),
                }
            from .lifecycle_worker import worker_documents
            from .manifests import render

            installer.docs = render(
                installer.network_environment,
                release,
                control_plane_only=installer.control_plane_only,
            )
            active_job = next(
                doc
                for doc in worker_documents(installer, active=True)
                if doc["kind"] == "ScaledJob"
            )
            current_job = installer.json(
                installer.kube(
                    "get",
                    "scaledjob",
                    "superplane-paid-worker",
                    "-n",
                    env["namespace"],
                    "-o",
                    "json",
                    "--show-managed-fields",
                )
            )
            require(
                current_job["metadata"].get("labels", {}).get(LABEL) == installer.owner
                and current_job["metadata"].get("annotations", {}).get(PAUSED)
                == "true",
                "Reviewed worker must remain installation-owned and paused",
            )
            scaled_job = {
                "uid": current_job["metadata"]["uid"],
                "spec_sha256": digest(current_job["spec"]),
                "desired_spec_sha256": digest(active_job["spec"]),
                "pause_owners": pause_owners(current_job),
            }
            review = {
                "version": 1,
                "run_id": previous["run_id"],
                "prior_receipt_sha256": digest(previous),
                "source": source,
                "policies": policies,
                "scaled_job": scaled_job,
                "approved_plan_sha256": previous["plan_sha256"],
                "network_revision_sha256": previous["network_configuration_revision"][
                    "review_sha256"
                ],
            }
            pin = digest(review)
            if not args.execute:
                print(
                    json.dumps({"review": review, "review_sha256": pin}, sort_keys=True)
                )
                return 0
            require(
                args.approved_review_sha256 == pin
                and args.approved_plan_sha256 == previous["plan_sha256"],
                "Exact recovery/plan approval required",
            )
            token = os.environ.get("SUPERPLANE_VERIFICATION_TOKEN", "")
            require(bool(token), "Current authenticated verification token required")
            installer.recovery_source = source
            installer.reviewed_uids = {
                name: value["uid"] for name, value in policies.items()
            }
            installer.reviewed_scaled_job = scaled_job
            installer.resume(previous)
            installer.receipt.setdefault("policy_creator_recoveries", []).append(
                {"review_sha256": pin, "review": review}
            )
            installer.save()
            installer.execute(args.approved_plan_sha256, token)
            print(
                json.dumps(
                    {"status": installer.receipt["status"], "run_id": installer.run_id}
                )
            )
            return 0
    except Refusal as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
