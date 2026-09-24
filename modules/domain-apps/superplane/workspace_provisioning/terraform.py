"""Prepare and consume exact reviewed workspace artifacts in a private worker volume."""

from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat

from .artifacts import canonical, digest
from .runtime_config import LifecycleRefused


def workspace_source():
    packaged = Path(__file__).with_name("_data") / "workspaces"
    return (
        packaged
        if packaged.is_dir()
        else Path(__file__).resolve().parents[1] / "infra" / "workspaces"
    )


def operation_directory(root, org_id, workspace_id, operation_id, *, create=False):
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts or root.is_symlink():
        raise LifecycleRefused(
            "worker state volume must be an explicit private directory"
        )
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    mount = root.stat()
    if not stat.S_ISDIR(mount.st_mode) or mount.st_mode & 0o002:
        raise LifecycleRefused("worker persistence mount must not be world-writable")
    # An fsGroup mount may be root-owned; only the private child belongs to the
    # dedicated worker. Never chown or relax permissions on the shared PVC.
    root = root / ("superplane-worker-" + str(os.getuid()))
    if root.is_symlink():
        raise LifecycleRefused("worker private state directory cannot be a symlink")
    if create:
        root.mkdir(mode=0o700, exist_ok=True)
    info = root.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_mode & 0o077
        or info.st_uid != os.getuid()
    ):
        raise LifecycleRefused("worker state volume permissions are not private")
    path = root / digest([org_id, workspace_id, operation_id])
    if create:
        path.mkdir(mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise LifecycleRefused("recorded worker artifact directory is unavailable")
    return path


def sha(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 << 20:
        raise LifecycleRefused("recorded artifact is not a bounded regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_digest(module):
    files = sorted(
        path
        for path in module.rglob("*")
        if path.is_file()
        and ".terraform" not in path.relative_to(module).parts
        and "__pycache__" not in path.parts
    )
    return digest({str(path.relative_to(module)): sha(path) for path in files})


def maintained_digest():
    source = workspace_source()
    files = [
        path
        for path in source.iterdir()
        if path.is_file()
        and (path.suffix in {".tf", ".json"} or path.name == ".terraform.lock.hcl")
    ]
    files += [
        path
        for path in (source / "scripts").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    ]
    return digest({str(path.relative_to(source)): sha(path) for path in sorted(files)})


def copy_source(destination):
    source = workspace_source()
    destination.mkdir(mode=0o700)
    for path in source.iterdir():
        if path.is_file() and (
            path.suffix in {".tf", ".json"} or path.name == ".terraform.lock.hcl"
        ):
            shutil.copyfile(path, destination / path.name)
    shutil.copytree(
        source / "scripts",
        destination / "scripts",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    if not (destination / ".terraform.lock.hcl").is_file():
        raise LifecycleRefused(
            "maintained provider lock is missing from the worker image"
        )


def require_owned_networking(config):
    """A fresh EKS node group cannot depend on a rule absent from its saved plan."""
    if config["workspace_variables"].get("networking_mode", "owned") != "owned":
        raise LifecycleRefused(
            "automated managed provisioning requires owned networking; supplied "
            "networking needs a preexisting reviewed node security group design"
        )


def prepare(operation, context, config, request, account_id, process):
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
    directory = process.directory
    module, output = directory / "module", directory / "review"
    copy_source(module)
    source_revision = source_digest(module)
    if source_revision != maintained_digest():
        raise LifecycleRefused(
            "worker Terraform module differs from its maintained source"
        )
    variables = {
        **config["workspace_variables"],
        **target,
        "vpc_cidr": request.vpc_cidr,
        "availability_zones": list(request.availability_zones),
        "cluster_version": request.cluster_version,
    }
    if request.node_instance_type is not None:
        variables["node_instance_type"] = request.node_instance_type
    variables_file = directory / "workspace.tfvars.json"
    variables_file.write_text(canonical(variables))
    variables_file.chmod(0o400)
    process.checked(
        [
            "python",
            str(module / "scripts" / "prepare_workspace_plan.py"),
            "--module-dir",
            str(module),
            "--variables",
            str(variables_file),
            "--output-dir",
            str(output),
            "--backend-bucket",
            config["backend"]["bucket"],
            "--backend-region",
            config["backend"]["region"],
            "--lock-table",
            config["backend"]["lock_table"],
            "--terraform-binary",
            config["binaries"]["terraform"],
        ],
        timeout=operation.max_runtime_seconds,
    )
    inventory = json.loads((output / "workspace-inventory.json").read_text())
    estimate = json.loads((output / "workspace-estimate.json").read_text())
    if inventory["destructive_addresses"]:
        raise LifecycleRefused(
            "workspace provisioning cannot approve destructive infrastructure changes"
        )
    fixed_daily_micros = (
        Decimal(str(estimate["bounded_monthly_usd"]))
        * Decimal(1_000_000)
        * Decimal(24)
        / Decimal(730)
    )
    if fixed_daily_micros > int(
        operation.request.parameters["lifecycle_allocation_max_cost_micros"]
    ):
        raise LifecycleRefused(
            "prepared infrastructure exceeds the admitted daily cost limit"
        )
    files = (
        "workspace.tfplan",
        "workspace-plan.json",
        "workspace-authorization.proposed.json",
        "workspace-inventory.json",
        "workspace-estimate.json",
        "workspace-backend.json",
    )
    hashes = {name: sha(output / name) for name in files}
    for name in files:
        (output / name).chmod(0o400)
    if source_digest(module) != source_revision:
        raise LifecycleRefused("workspace source changed while preparing the plan")
    return target, {
        "next_phase": "apply-infrastructure",
        "files": hashes,
        "module_sha256": source_revision,
        "plan_file_sha256": hashes["workspace.tfplan"],
        "plan_json_sha256": hashes["workspace-plan.json"],
        "inventory": inventory,
        "estimate": estimate,
    }


def verify_prepared_artifact(row, context):
    """Read-only persisted bytes/source verifier for apply and proposal recovery.

    Caller must authenticate the immutable domain row and its original admission.
    This does not execute Terraform, establish cloud state, or release allocation.
    """
    metadata = json.loads(row["artifact_metadata_json"])
    directory = operation_directory(
        context.state_root,
        row["org_id"],
        row["workspace_id"],
        row["source_operation_id"],
    )
    module, output = directory / "module", directory / "review"
    if (
        source_digest(module) != metadata["module_sha256"]
        or maintained_digest() != metadata["module_sha256"]
    ):
        raise LifecycleRefused("reviewed Terraform source changed")
    expected_files = {
        "workspace.tfplan",
        "workspace-plan.json",
        "workspace-authorization.proposed.json",
        "workspace-inventory.json",
        "workspace-estimate.json",
        "workspace-backend.json",
    }
    if set(metadata["files"]) != expected_files:
        raise LifecycleRefused("reviewed Terraform artifact inventory is incomplete")
    if any(sha(output / name) != value for name, value in metadata["files"].items()):
        raise LifecycleRefused("reviewed Terraform artifact bytes changed")
    if (
        metadata["files"]["workspace.tfplan"],
        metadata["files"]["workspace-plan.json"],
    ) != (metadata["plan_file_sha256"], metadata["plan_json_sha256"]):
        raise LifecycleRefused("reviewed Terraform artifact digests disagree")
    return module, output


def apply(operation, context, config, row, process):
    """Recheck stored hashes and maintained source, then call the existing apply guard."""
    module, output = verify_prepared_artifact(row, context)
    metadata = json.loads(row["artifact_metadata_json"])
    target = json.loads(row["target_json"])
    flags = [
        part
        for key, value in target.items()
        for part in ("--" + key.replace("_", "-"), value)
    ]
    process.checked(
        [
            "python",
            str(module / "scripts" / "apply_workspace_plan.py"),
            "--module-dir",
            str(module),
            "--plan-file",
            str(output / "workspace.tfplan"),
            "--plan-json",
            str(output / "workspace-plan.json"),
            "--authorization",
            str(output / "workspace-authorization.proposed.json"),
            "--inventory",
            str(process.directory / "applied-inventory.json"),
            "--estimate",
            str(process.directory / "applied-estimate.json"),
            "--terraform-binary",
            config["binaries"]["terraform"],
            *flags,
        ],
        timeout=operation.max_runtime_seconds,
    )
    outputs = json.loads(process.checked(["terraform", "output", "-json"], cwd=module))
    actual = {
        key: outputs[key]["value"]
        for key in ("account_id", "aws_region", "org_id", "workspace_id")
    }
    if any(actual[key] != target[key] for key in actual):
        raise LifecycleRefused("Terraform outputs belong to another workspace")
    return target, {
        "next_phase": "bootstrap-workspace",
        "allocation_source_operation_id": operation.grant.lease.operation_id,
        "outputs": outputs,
        "source_artifact_id": row["artifact_id"],
        "module_sha256": metadata["module_sha256"],
    }
