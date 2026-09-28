"""Dedicated host execution through existing Task authority and source staging.

The request carries a source delta, never a check image, argv or provider URL.
Invocation deduplication and stop fencing must be established by the service
operation store before calling this runner. No SDK/model executes in this host.
"""

from __future__ import annotations

import tempfile
import copy
from pathlib import Path

from lib.codex_source import provision_workspace
from lib.codex_validation import ValidationCheck, ValidationUnavailable
from lib.codex_validation_source import apply_validation_manifest


def run_service_validation(*, authority, attempt, manifest, check_name, executor, cancelled):
    def authorize():
        verified = authority.authorize(attempt=attempt, tool="validation.run")
        task = verified.task
        checks = task.get("repository_binding", {}).get("binding", {}).get("validation_checks", [])
        selected = [check for check in checks if check.get("name") == check_name]
        if len(selected) != 1 or "validation.run" not in task.get("tool_grants", []):
            raise ValidationUnavailable("Service validation check is not admitted")
        raw = selected[0]
        check = ValidationCheck(**{**raw, "argv": tuple(raw["argv"])})
        check.document()
        if "@sha256:" not in check.image or check.timeout_seconds > 120:
            raise ValidationUnavailable("Service validation requires a registry digest and at most 120 seconds")
        return copy.deepcopy(task["repository_binding"]), check

    binding, check = authorize()
    if cancelled.is_set():
        raise ValidationUnavailable("Service validation has been stopped")
    with tempfile.TemporaryDirectory(prefix="adp-service-validation-") as directory:
        workspace = provision_workspace(authority, attempt=attempt, root=Path(directory) / "repository")
        if cancelled.is_set() or authorize() != (binding, check):
            raise ValidationUnavailable("Service validation authority changed")
        state = apply_validation_manifest(workspace, manifest)
        result = executor.run_repository(check=check, repository=workspace.root,
                                         expected_head=state["localHead"], cancelled=cancelled)
        if (result.get("tree") != manifest["tree"] or result.get("commit") != state["localHead"]
                or cancelled.is_set() or authorize() != (binding, check)):
            raise ValidationUnavailable("Service validation source or authority changed")
        # Source is proved by its exact reconstructed tree. The ephemeral worker
        # and service commits have different parents/messages and are not equated.
        return {**result, "commit": manifest["local_head"], "validationCommit": state["localHead"],
                "sourceRevision": workspace.source_revision}
