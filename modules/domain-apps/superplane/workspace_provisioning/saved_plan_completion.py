"""Finish a receipt-bound saved proposal; never execute Terraform init or plan."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from decimal import Decimal

from .artifacts import canonical
from .runtime_config import LifecycleRefused
from .saved_plan_validation import (
    SAVED_PLAN_FILES,
    inspect_saved_plan,
    saved_plan_snapshot,
)
from .terraform import (
    maintained_digest,
    operation_directory,
    require_owned_networking,
    sha,
    source_digest,
    workspace_source,
)


def stage_saved_plan(root, identity, receipt):
    """Keep the original directory intact; copy only reviewed inputs and outputs."""
    proof = json.loads(receipt["proof"])
    boundary = proof.get("preparation_boundary", {})
    if (
        receipt["recovery_scope"] != "saved-plan-completion"
        or proof.get("evidence_kind") != "saved-plan-finalization-v1"
        or proof.get("planning_started") is not True
        or proof.get("apply_started") is not False
        or "attempt_extension" in proof
        or boundary.get("kind") != "saved-plan-finalization-v1"
        or boundary.get("observation", {}).get("offline_guard_passed") is not True
    ):
        raise LifecycleRefused(
            "saved plan completion requires its separate owner proof"
        )
    root = Path(root)
    if (
        not root.is_absolute()
        or ".." in root.parts
        or root.is_symlink()
        or root.stat().st_mode & 0o002
    ):
        raise LifecycleRefused("saved plan state mount is not private")
    private = root / ("superplane-worker-" + str(os.getuid()))
    if (
        private.is_symlink()
        or not private.is_dir()
        or private.stat().st_uid != os.getuid()
        or private.stat().st_mode & 0o007
    ):
        raise LifecycleRefused("saved plan private root ownership differs")
    source = private / hashlib.sha256(canonical(list(identity)).encode()).hexdigest()
    entries = saved_plan_snapshot(source)
    observation = boundary["observation"]
    pricing = observation.get("pricing_revalidation")
    retained = SAVED_PLAN_FILES
    derived = {}
    if pricing:
        from .pricing_plan_validation import (
            ORIGINAL_FILES,
            reproduce_pricing,
            verify_transition,
        )

        retained = ORIGINAL_FILES
        try:
            replacements = verify_transition(source / "module", workspace_source())
            if (
                replacements != pricing["replacements"]
                or source_digest(source / "module") != pricing["original_module_sha256"]
                or maintained_digest() != pricing["corrected_module_sha256"]
            ):
                raise ValueError("pricing source differs from protected inspection")
        except (ValueError, KeyError) as exc:
            raise LifecycleRefused("saved pricing source transition refused") from exc
    if (
        entries != boundary["entries"]
        or hashlib.sha256(canonical(entries).encode()).hexdigest() != boundary["sha256"]
        or (not pricing and source_digest(source / "module") != maintained_digest())
        or any(
            sha(source / "review" / name) != boundary["observation"]["files"][name]
            for name in retained
        )
    ):
        raise LifecycleRefused("saved proposal differs from protected owner inspection")
    if pricing:
        try:
            reproduced, derived = reproduce_pricing(source)
            if reproduced != observation:
                raise ValueError("pricing observation changed")
        except ValueError as exc:
            raise LifecycleRefused(
                "saved pricing reports differ from owner evidence"
            ) from exc
    suffix = hashlib.sha256(receipt["reauthorization_id"].encode()).hexdigest()
    archive = source.with_name(source.name + ".completed-from-" + suffix)
    if archive.exists() or archive.is_symlink():
        raise LifecycleRefused("saved proposal was already staged for completion")
    copy_bytes = (
        sum(
            path.stat().st_size
            for path in (source / "module").rglob("*")
            if path.is_file()
        )
        + sum((source / "review" / name).stat().st_size for name in retained)
        + sum(map(len, derived.values()))
    )
    copy_bytes += (source / "workspace.tfvars.json").stat().st_size
    if shutil.disk_usage(source).free < copy_bytes + (64 << 20):
        raise LifecycleRefused(
            "insufficient space to preserve and complete saved proposal"
        )
    # Tighten only worker-owned roots after kubelet fsGroup staging.
    source.parent.chmod(0o700)
    source.chmod(0o700)
    source.rename(archive)
    directory = operation_directory(root, *identity, create=True)
    shutil.copytree(archive / "module", directory / "module")
    shutil.copyfile(
        archive / "workspace.tfvars.json", directory / "workspace.tfvars.json"
    )
    (directory / "workspace.tfvars.json").chmod(0o400)
    (directory / "review").mkdir(mode=0o700)
    for name in retained:
        shutil.copyfile(archive / "review" / name, directory / "review" / name)
    if pricing:
        from .pricing_plan_validation import replacement_bytes

        for name, content in replacement_bytes().items():
            (directory / "module" / name).write_bytes(content)
        for name, content in derived.items():
            (directory / "review" / name).write_bytes(content)
        if source_digest(directory / "module") != maintained_digest():
            raise LifecycleRefused("staged pricing source differs")
    # Never copy .aws, environment files, or any original execution credential.
    if saved_plan_snapshot(archive) != entries:
        raise LifecycleRefused("original saved proposal changed while being preserved")
    return directory


def finish(
    operation,
    context,
    config,
    request,
    account_id,
    process,
    *,
    receipt,
    daily_budget_micros=None,
):
    require_owned_networking(config)
    lease = operation.grant.lease
    target = {
        "account_id": account_id,
        "aws_region": request.region,
        "environment": config["environment"],
        "workspace_name": operation.request.parameters["workspace_name"],
        "org_id": lease.org_id,
        "workspace_id": lease.workspace_id,
    }
    variables = {
        **config["workspace_variables"],
        **target,
        "vpc_cidr": request.vpc_cidr,
        "availability_zones": list(request.availability_zones),
        "cluster_version": request.cluster_version,
    }
    if request.node_instance_type is not None:
        variables["node_instance_type"] = request.node_instance_type
    directory = process.directory
    if json.loads((directory / "workspace.tfvars.json").read_text()) != variables:
        raise LifecycleRefused("saved plan inputs differ from original admission")
    module, output = directory / "module", directory / "review"
    source = source_digest(module)
    observation = json.loads(receipt["proof"])["preparation_boundary"]["observation"]
    if source != maintained_digest() or observation["target"] != target:
        raise LifecycleRefused("saved plan source or target changed")
    process.checked(
        [
            "python",
            "-B",
            "-m",
            "workspace_provisioning.saved_plan_completion",
            "--directory",
            str(directory),
            "--terraform-binary",
            config["binaries"]["terraform"],
            "--plan-sha256",
            observation["files"]["workspace.tfplan"],
        ],
        timeout=operation.max_runtime_seconds,
    )
    inventory = json.loads((output / "workspace-inventory.json").read_text())
    estimate = json.loads((output / "workspace-estimate.json").read_text())
    limit = (
        int(operation.request.parameters["lifecycle_allocation_max_cost_micros"])
        if daily_budget_micros is None
        else daily_budget_micros
    )
    if (
        inventory["destructive_addresses"]
        or Decimal(str(estimate["bounded_monthly_usd"])) * 1000000 * 24 / 730 > limit
    ):
        raise LifecycleRefused("saved plan exceeds the original current authority")
    hashes = {
        name: sha(output / name)
        for name in sorted(SAVED_PLAN_FILES | {"workspace-backend.json"})
    }
    if source_digest(module) != source or any(
        hashes[name] != observation["files"][name]
        for name in SAVED_PLAN_FILES - {"workspace-authorization.proposed.json"}
    ):
        raise LifecycleRefused("saved artifact changed during final validation")
    for name in hashes:
        (output / name).chmod(0o400)
    return target, {
        "next_phase": "apply-infrastructure",
        "files": hashes,
        "module_sha256": source,
        "plan_file_sha256": hashes["workspace.tfplan"],
        "plan_json_sha256": hashes["workspace-plan.json"],
        "inventory": inventory,
        "estimate": estimate,
        "saved_plan_completion": {
            "reauthorization_id": receipt["reauthorization_id"],
            "original_key": receipt["original_key"],
            "source_fence": receipt["source_fence"],
            "original_files": observation.get("pricing_revalidation", {}).get(
                "original_files", observation["files"]
            ),
            **(
                {"pricing_revalidation": observation["pricing_revalidation"]}
                if "pricing_revalidation" in observation
                else {}
            ),
        },
    }


def finalize_files(directory, *, terraform_binary, plan_sha256):
    directory = Path(directory)
    module, review = directory / "module", directory / "review"
    if (
        sha(review / "workspace.tfplan") != plan_sha256
        or source_digest(module) != maintained_digest()
        or {p.name for p in review.iterdir()} != SAVED_PLAN_FILES
        or any(name.startswith("TF_CLI_ARGS") for name in os.environ)
    ):
        raise LifecycleRefused("saved proposal is not eligible for final validation")
    observation = inspect_saved_plan(directory, terraform_binary=terraform_binary)
    sys.path.insert(0, str(module / "scripts"))
    from workspace_backend import verify_backend
    from workspace_kms import (
        verify_account_prerequisites,
        verify_provisioning_principal,
        verify_supplied_key,
    )

    plan = json.loads((review / "workspace-plan.json").read_text())
    backend = verify_backend(
        review / "workspace.tfplan", plan, module, observation["target"]
    )
    document = json.loads(
        (review / "workspace-authorization.proposed.json").read_text()
    )
    # These are the original recipe's outstanding checks, under a new current
    # provider session. No check is inferred from historical evidence.
    document.update(
        backend=backend,
        account_prerequisites=verify_account_prerequisites(observation["target"]),
        supplied_kms=verify_supplied_key(plan),
        provisioning_principal=verify_provisioning_principal(plan),
    )
    if sha(review / "workspace.tfplan") != plan_sha256:
        raise LifecycleRefused("saved artifact changed during final preflights")
    (review / "workspace-authorization.proposed.json").write_text(
        json.dumps(document, indent=2) + "\n"
    )
    (review / "workspace-backend.json").write_text(json.dumps(backend, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--terraform-binary", required=True)
    parser.add_argument("--plan-sha256", required=True)
    args = parser.parse_args()
    finalize_files(
        args.directory,
        terraform_binary=args.terraform_binary,
        plan_sha256=args.plan_sha256,
    )
