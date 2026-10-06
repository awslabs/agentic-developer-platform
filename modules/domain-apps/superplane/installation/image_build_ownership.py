"""Preserve explicitly selected, already domain-owned build lanes without adoption."""

import copy
import json

from .config import MODULE, require

PREFIX = "module.image_builds."


def preserve_domain_builds(env):
    intent = env.get("image_build_ownership", "external")
    require(
        intent in ("external", "preserve-domain"),
        "image_build_ownership must be external or preserve-domain",
    )
    return intent == "preserve-domain"


def resources(module):
    for resource in module.get("resources", []):
        yield resource
    for child in module.get("child_modules", []):
        yield from resources(child)


def runtime_plan(env, plan):
    """Remove only independently checked unchanged build records from runtime guard.

    The full original plan still participates in the installer's approval hash.
    The generic runtime ownership guard deliberately does not authorize CodeBuild;
    this separate preservation check does not grant build provisioning authority.
    """
    if not preserve_domain_builds(env):
        return plan
    prior = {
        item["address"]: item
        for item in resources(
            plan.get("prior_state", {}).get("values", {}).get("root_module", {})
        )
    }
    changes = {item["address"]: item for item in plan.get("resource_changes", [])}
    account, region, environment = (
        env[key] for key in ("account_id", "region", "environment")
    )
    manifest = json.loads((MODULE / "codebuild/projects.json").read_text())
    expected = {}
    for lane in manifest:
        role = f"adp-{environment}-codebuild-{lane}"
        project = f"adp-{environment}-{lane}"
        expected[f'{PREFIX}aws_iam_role.project["{lane}"]'] = {
            "name": role,
            "arn": f"arn:aws:iam::{account}:role/{role}",
            "id": role,
        }
        expected[f'{PREFIX}aws_iam_role_policy.project["{lane}"]'] = {
            "name": "build-scope",
            "role": role,
            "id": role + ":build-scope",
        }
        expected[f'{PREFIX}aws_codebuild_project.main["{lane}"]'] = {
            "name": project,
            "arn": f"arn:aws:codebuild:{region}:{account}:project/{project}",
            # The provider retains a historical name-based import ID; the
            # exact ARN is still required independently in all cases.
            "id": (project, f"arn:aws:codebuild:{region}:{account}:project/{project}"),
        }
    require(bool(expected), "Build preservation inventory is empty")
    for address, identity in expected.items():
        before_state = prior.get(address)
        change = changes.get(address, {})
        detail = change.get("change", {})
        require(
            before_state is not None
            and before_state.get("mode") == "managed"
            and change.get("mode") == "managed"
            and detail.get("actions") == ["no-op"]
            and not change.get("importing")
            and not detail.get("importing"),
            "preserve-domain requires every exact build lane already owned and unchanged; enrollment or migration needs separate review",
        )
        for values in (
            before_state.get("values", {}),
            detail.get("before", {}),
            detail.get("after", {}),
        ):
            require(
                isinstance(values, dict)
                and all(
                    values.get(key) in value
                    if isinstance(value, tuple)
                    else values.get(key) == value
                    for key, value in identity.items()
                ),
                "Existing domain build identity differs from the selected account, region or lane",
            )
    for entry in plan.get("resource_drift", []):
        require(
            not entry.get("address", "").startswith(PREFIX),
            "Existing domain build drift needs separate reconciliation",
        )
    remaining = copy.deepcopy(plan)
    remaining["resource_changes"] = [
        entry
        for entry in plan.get("resource_changes", [])
        if entry["address"] not in expected
    ]
    return remaining
