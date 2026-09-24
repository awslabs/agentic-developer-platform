"""Apply only the original reviewed, deletion-only Terraform artifact.

Artifact paths and executables are trusted service configuration. Admission binds
their content digest, original allocation and backend; no worker supplies a path.
The maintained apply guard verifies saved-plan rendering, live account, backend,
KMS and provisioning principal again before invoking Terraform.
"""

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import tempfile

from harness_jobs.execution import CallOutcome
from harness_jobs.identity import OperationRefused

from .execution_contract import ExecutionStep

PROVIDER = "superplane-terraform"
ACTION = "apply-reviewed-destroy"


@dataclass(frozen=True)
class ReviewedDestroy:
    original_allocation_id: str
    plan_file_sha256: str
    backend_sha256: str
    plan_file: Path
    plan_json: Path
    authorization: Path
    module_dir: Path
    target: dict[str, str]

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

    def read(self, inventory, parameters):
        if inventory.cluster_ownership != "adp-created":
            raise OperationRefused("adopted infrastructure cannot be destroyed")
        if set(self.target) != {
            "account_id",
            "aws_region",
            "environment",
            "workspace_name",
            "org_id",
            "workspace_id",
        } or (self.target["org_id"], self.target["workspace_id"]) != (
            inventory.org_id,
            inventory.workspace_id,
        ):
            raise OperationRefused(
                "destroy artifact target differs from retirement ownership"
            )
        if inventory.cluster_arn.split(":")[3:5] != [
            self.target["aws_region"],
            self.target["account_id"],
        ]:
            raise OperationRefused("destroy artifact account or region mismatch")
        if (
            not self.original_allocation_id
            or parameters.get("original_allocation_id") != self.original_allocation_id
            or parameters.get("allocation_id") != self.original_allocation_id
            or parameters.get("terraform_plan_file_sha256") != self.plan_file_sha256
            or parameters.get("terraform_backend_sha256") != self.backend_sha256
            or not all(
                re.fullmatch(r"[a-f0-9]{64}", item)
                for item in (self.plan_file_sha256, self.backend_sha256)
            )
        ):
            raise OperationRefused(
                "destroy artifact lacks original approved allocation binding"
            )
        artifact = self.plan_file.read_bytes()
        rendered = self.plan_json.read_bytes()
        authorization = self.authorization.read_bytes()
        document = json.loads(authorization)
        if (
            hashlib.sha256(artifact).hexdigest() != self.plan_file_sha256
            or document.get("plan_file_sha256") != self.plan_file_sha256
            or document.get("plan_sha256") != hashlib.sha256(rendered).hexdigest()
            or any(document.get(key) != value for key, value in self.target.items())
            or not document.get("backend")
            or hashlib.sha256(
                json.dumps(
                    document["backend"], sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            != self.backend_sha256
        ):
            raise OperationRefused(
                "destroy artifact content or backend binding changed"
            )
        changes = json.loads(rendered).get("resource_changes")
        if not isinstance(changes, list) or not changes:
            raise OperationRefused("destroy plan has no resource inventory")
        actions = [change.get("change", {}).get("actions") for change in changes]
        if ["delete"] not in actions or any(
            action not in (["delete"], ["no-op"], ["read"]) for action in actions
        ):
            raise OperationRefused(
                "retirement cannot apply creating or replacement plans"
            )
        return artifact, rendered, authorization


class TerraformDestroy:
    def __init__(self, *, python_binary, guard_script, terraform_binary):
        self.python_binary = str(Path(python_binary).resolve(strict=True))
        self.guard_script = str(Path(guard_script).resolve(strict=True))
        self.terraform_binary = str(Path(terraform_binary).resolve(strict=True))

    async def execute(self, artifact, inventory, parameters, authorize):
        content = artifact.read(inventory, parameters)
        with tempfile.TemporaryDirectory(prefix="superplane-retirement-") as directory:
            root = Path(directory)
            paths = [
                root / name
                for name in ("reviewed.tfplan", "reviewed.json", "authorization.json")
            ]
            for path, value in zip(paths, content, strict=True):
                path.write_bytes(value)
                path.chmod(0o400)
            arguments = [
                self.python_binary,
                self.guard_script,
                "--plan-file",
                str(paths[0]),
                "--plan-json",
                str(paths[1]),
                "--authorization",
                str(paths[2]),
                "--inventory",
                str(root / "inventory.json"),
                "--estimate",
                str(root / "estimate.json"),
                "--module-dir",
                str(artifact.module_dir.resolve(strict=True)),
                "--terraform-binary",
                self.terraform_binary,
            ]
            for key, value in artifact.target.items():
                arguments.extend(["--" + key.replace("_", "-"), value])
            await authorize()
            # Child output can contain provider internals. Keep it in the protected
            # process; the shared receipt receives only a fixed summary and digest.
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                while process.returncode is None:
                    try:
                        await asyncio.wait_for(process.wait(), timeout=1)
                    except TimeoutError:
                        await authorize()
            except BaseException:
                if process.returncode is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except TimeoutError:
                        os.killpg(process.pid, signal.SIGKILL)
                        await process.wait()
                raise
            await authorize()
            if process.returncode != 0:
                return (
                    CallOutcome.UNKNOWN,
                    "reviewed destroy requires reconciliation",
                    artifact.plan_file_sha256,
                )
            # Applying successfully is not proof of zero billable exposure. The
            # runner must still perform independent provider inventory finalization.
            return (
                CallOutcome.SUCCEEDED,
                "reviewed destroy applied",
                artifact.plan_file_sha256,
            )
