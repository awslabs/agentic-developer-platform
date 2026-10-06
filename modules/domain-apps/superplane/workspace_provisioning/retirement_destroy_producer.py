"""Prepare a deletion-only saved plan; apply can consume only these exact bytes."""

import json
from dataclasses import dataclass
from types import SimpleNamespace

from account_factory.modes import from_mapping

from .artifacts import digest
from .execution_contract import ExecutionStep
from .process import WorkerProcesses
from .retirement_terraform import ACTION, PROVIDER, ReviewedDestroy
from .runtime_config import LifecycleRefused
from .terraform import (
    maintained_digest,
    operation_directory,
    prepare,
    sha,
    source_digest,
)

FILES = frozenset(
    {
        "workspace.tfplan",
        "workspace-plan.json",
        "workspace-authorization.proposed.json",
        "workspace-inventory.json",
        "workspace-estimate.json",
        "workspace-backend.json",
    }
)


@dataclass(frozen=True)
class DestroyPlanReference:
    """A review descriptor contains no local paths and cannot execute Terraform."""

    original_allocation_id: str
    plan_file_sha256: str
    backend_sha256: str
    target: dict

    def step(self):
        return ExecutionStep(
            "destroy-managed-infrastructure",
            PROVIDER,
            ACTION,
            json.dumps(
                {
                    "original_allocation_id": self.original_allocation_id,
                    "terraform_plan_file_sha256": self.plan_file_sha256,
                    "terraform_backend_sha256": self.backend_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )


def destroy_reference(value):
    value = validate_destroy_metadata(value)
    return DestroyPlanReference(
        **{
            key: value[key]
            for key in (
                "original_allocation_id",
                "plan_file_sha256",
                "backend_sha256",
                "target",
            )
        }
    )


def validate_destroy_metadata(value):
    """Metadata contains hashes and original scope; never a caller-chosen path."""
    import re

    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "version",
            "target",
            "files",
            "module_sha256",
            "plan_file_sha256",
            "plan_json_sha256",
            "backend_sha256",
            "original_allocation_id",
        }
        or value["version"] != 1
        or not isinstance(value["target"], dict)
        or set(value["target"])
        != {
            "account_id",
            "aws_region",
            "environment",
            "workspace_name",
            "org_id",
            "workspace_id",
        }
        or not isinstance(value["files"], dict)
        or set(value["files"]) != FILES
        or any(
            not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{64}", item)
            for item in [
                *value["files"].values(),
                value["module_sha256"],
                value["plan_file_sha256"],
                value["plan_json_sha256"],
                value["backend_sha256"],
            ]
        )
        or not isinstance(value["original_allocation_id"], str)
        or not value["original_allocation_id"]
        or value["files"]["workspace.tfplan"] != value["plan_file_sha256"]
        or value["files"]["workspace-plan.json"] != value["plan_json_sha256"]
    ):
        raise LifecycleRefused("reviewed destroy metadata is incomplete")
    return value


def prepare_destroy(facts, context, session, directory, verify):
    """Use original bootstrap target plus current approved runtime; no delete call."""
    original = facts.source.admitted_request()
    request = from_mapping(json.loads(original.parameters["lifecycle_request"]))
    if (
        request.mode.value != "existing-account-managed"
        or facts.inventory.preserve_cluster
    ):
        raise LifecycleRefused(
            "destroy preparation requires original managed ownership"
        )
    directory = directory / "destroy"
    directory.mkdir(mode=0o700)
    process = WorkerProcesses(
        binaries=facts.config["binaries"],
        directory=directory,
        session=session,
        region=request.region,
        verify=verify,
    )
    # The source supplies only immutable provisioning inputs. Current paid authority,
    # deadline and provider transport remain the separately approved access operation.
    operation = SimpleNamespace(
        request=original,
        grant=facts.operation.grant,
        max_runtime_seconds=facts.operation.max_runtime_seconds,
    )
    target, metadata = prepare(
        operation,
        context,
        facts.config,
        request,
        request.target_account_id,
        process,
        destroy=True,
    )
    if any(
        target[key] != json.loads(facts.artifact["target_json"])[key] for key in target
    ):
        raise LifecycleRefused("destroy target differs from original paid apply")
    authorization = json.loads(
        (directory / "review/workspace-authorization.proposed.json").read_text()
    )
    value = validate_destroy_metadata(
        {
            "version": 1,
            "target": target,
            "files": metadata["files"],
            "module_sha256": metadata["module_sha256"],
            "plan_file_sha256": metadata["plan_file_sha256"],
            "plan_json_sha256": metadata["plan_json_sha256"],
            "backend_sha256": digest(authorization["backend"]),
            "original_allocation_id": facts.plan.original_allocation_id,
        }
    )
    artifact = _artifact(directory, value)
    artifact.read(
        facts.inventory,
        {
            "allocation_id": facts.plan.original_allocation_id,
            "original_allocation_id": facts.plan.original_allocation_id,
            "terraform_plan_file_sha256": value["plan_file_sha256"],
            "terraform_backend_sha256": value["backend_sha256"],
        },
    )
    verify()
    return value


def _artifact(directory, value):
    module, output = directory / "module", directory / "review"
    if (
        source_digest(module) != value["module_sha256"]
        or maintained_digest() != value["module_sha256"]
    ):
        raise LifecycleRefused("reviewed destroy module changed")
    if any(sha(output / name) != expected for name, expected in value["files"].items()):
        raise LifecycleRefused("reviewed destroy artifact bytes changed")
    return ReviewedDestroy(
        original_allocation_id=value["original_allocation_id"],
        plan_file_sha256=value["plan_file_sha256"],
        backend_sha256=value["backend_sha256"],
        plan_file=output / "workspace.tfplan",
        plan_json=output / "workspace-plan.json",
        authorization=output / "workspace-authorization.proposed.json",
        module_dir=module,
        target=value["target"],
    )


def reviewed_destroy_from_access(row, context):
    """Caller must verify the immutable access row and successful original producer."""
    value = validate_destroy_metadata(
        json.loads(row["artifact_metadata_json"])["reviewed_destroy"]
    )
    if (
        value["target"]["org_id"],
        value["target"]["workspace_id"],
        value["target"]["account_id"],
    ) != (row["org_id"], row["workspace_id"], row["account_id"]):
        raise LifecycleRefused("reviewed destroy scope changed")
    root = operation_directory(
        context.state_root,
        row["org_id"],
        row["workspace_id"],
        row["source_operation_id"],
    )
    directory = (
        root
        / (
            "access-"
            + digest([row["producer_attempt_id"], row["producer_fence_token"]])[:24]
        )
        / "destroy"
    )
    return _artifact(directory, value)
