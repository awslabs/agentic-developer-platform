"""Validate two source-only project configurations against one pinned release."""

import re

from pydantic import ValidationError

from l5_services.run_input import WellArchitectedRunInput, WellArchitectedSettings


ARTIFACTS = frozenset({"wheel", "worker", "schema", "gateway", "frontend", "cli"})


def verify_portable_configurations(cases):
    if not isinstance(cases, list) or len(cases) != 2:
        raise ValueError("two independent project configurations required")
    scopes = []
    architectures = []
    goals = []
    release = None
    for case in cases:
        if not isinstance(case, dict) or case.get("project_kind") != "non_adp":
            raise ValueError("non-ADP project fixture required")
        try:
            run = WellArchitectedRunInput.model_validate(case["run_input"])
            settings = WellArchitectedSettings.model_validate(case["settings"])
        except (KeyError, ValidationError, TypeError) as error:
            raise ValueError("invalid architecture review configuration") from error
        if (not settings.goal_refs or not run.sources or settings.execution_role_ref is None
                or settings.source_store_ref is None):
            raise ValueError("pinned architecture sources, goals and provider references required")
        scope = {(reference.tenant_id, reference.project_id)
                 for reference in (*run.sources, *settings.goal_refs,
                                   settings.execution_role_ref, settings.source_store_ref)}
        if len(scope) != 1:
            raise ValueError("architecture and goals must share one project scope")
        architecture = case.get("architecture")
        goal = case.get("goal")
        if (not isinstance(architecture, dict) or not isinstance(goal, dict)
                or architecture not in [source.model_dump() for source in run.sources]
                or goal not in [reference.model_dump() for reference in settings.goal_refs]
                or architecture == goal):
            raise ValueError("architecture and goal must match pinned configuration references")
        iac_format = case.get("iac_format")
        if not isinstance(iac_format, str) or iac_format not in {"terraform", "cloudformation", "cdk"}:
            raise ValueError("supported distinct IaC fixture required")
        digests = case.get("release_digests")
        if (not isinstance(digests, dict) or set(digests) != ARTIFACTS
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in digests.values())):
            raise ValueError("complete pinned release digests required")
        if release is not None and release != digests:
            raise ValueError("release artifacts changed between project configurations")
        release = digests
        changes = case.get("changes_since_release")
        if (not isinstance(changes, list) or not changes
                or any(not isinstance(change, str)
                       or change not in {"configuration", "binding", "source"} for change in changes)):
            raise ValueError("only configuration and admitted sources may change between onboardings")
        scopes.append(next(iter(scope)))
        architectures.append((iac_format, architecture["record_id"], architecture["record_revision"]))
        goals.append((goal["record_id"], goal["record_revision"]))
    if (len(set(scopes)) != 2 or len(set(architectures)) != 2
            or architectures[0][0] == architectures[1][0] or len(set(goals)) != 2):
        raise ValueError("projects, architectures, IaC formats and goals must be distinct")
    return release
