"""Server-owned profile-1 completeness, independent of lifecycle and permission."""

from datetime import UTC, datetime

from src.products.models import Product, Project

CHECK_SECTIONS = {
    "repository": "basic",
    "planning": "basic",
    "documents": "basic",
    "team_mapping": "basic",
    "environment": "advanced",
    "pipeline": "advanced",
    "operations": "advanced",
    "financials": "financials",
}


def selected_checks(product: Product, project: Project, steps: dict | None = None, source_evidence: dict | None = None) -> set[str]:
    config = project.configuration or {}
    basic = config.get("basic") or {}
    advanced = config.get("advanced") or {}
    selected = {"information", "project_scope"}
    if product.starting_point in ("existing_assets", "running_product") or basic.get("repositories"):
        selected.add("repository")
    if product.starting_point == "running_product":
        selected.update(("environment", "pipeline", "operations"))
    for check_id, fields in {
        "planning": ("trackers", "roadmap"),
        "documents": ("documents", "architecture"),
        "team_mapping": ("user_mappings",),
    }.items():
        if any(basic.get(field) for field in fields):
            selected.add(check_id)
    # The native ADP team grants workspace access. External user mappings are
    # an optional integration check, selected only by configuration/evidence.
    selected.update(source_evidence or ())
    for check_id, fields in {
        "environment": ("environments",),
        "pipeline": ("pipelines",),
        "operations": ("support", "runbooks", "observability"),
    }.items():
        if any(advanced.get(field) for field in fields):
            selected.add(check_id)
    if "financials" in config:
        selected.add("financials")
    for choice in config.get("applicability") or []:
        if choice.get("check_id") in CHECK_SECTIONS:
            selected.add(choice["check_id"])
    for check_id, section in CHECK_SECTIONS.items():
        if (steps or {}).get(section, {}).get("status") in ("later", "not_applicable"):
            selected.add(check_id)
    return selected


def required_checks(product: Product) -> set[str]:
    required = {"information", "project_scope"}
    if product.starting_point in ("existing_assets", "running_product"):
        required.add("repository")
    if product.starting_point == "running_product":
        required.update(("environment", "pipeline", "operations"))
    return required


EVIDENCE_FIELDS = {
    "repository": (("basic", "repositories"),),
    "planning": (("basic", "trackers"), ("basic", "roadmap")),
    "documents": (("basic", "documents"), ("basic", "architecture")),
    "environment": (("advanced", "environments"),),
    "pipeline": (("advanced", "pipelines"),),
    "operations": (("advanced", "runbooks"), ("advanced", "support"), ("advanced", "observability")),
}


def evidence_status(project: Project, check_id: str, now: datetime) -> tuple[str, list]:
    fields = EVIDENCE_FIELDS.get(check_id)
    if fields is None:
        return "missing", []
    has_stale = False
    for section, field in fields:
        refs = ((project.configuration or {}).get(section) or {}).get(field) or []
        for ref in refs:
            if ref.get("tenant_id") != project.tenant_id or ref.get("project_id") != project.id:
                continue
            for proof in project.provenance or []:
                if proof.get("state") != "accepted" or proof.get("method") != "connector":
                    continue
                if f"/configuration/{section}/{field}" not in proof.get("paths", ()) or ref not in proof.get("source_refs", ()):
                    continue
                try:
                    observed = datetime.fromisoformat(proof["observed_at"].replace("Z", "+00:00"))
                    stale_after = datetime.fromisoformat(proof["stale_after"].replace("Z", "+00:00"))
                except (KeyError, ValueError, TypeError, AttributeError):
                    continue
                if observed.tzinfo is None or stale_after.tzinfo is None or observed > now or stale_after <= observed:
                    continue
                if stale_after <= now:
                    has_stale = True
                else:
                    return "complete", [ref]
    return ("stale" if has_stale else "missing"), []


def current_evidence(project: Project, check_id: str, now: datetime, source_evidence: dict | None = None) -> tuple[str, list]:
    if source_evidence is not None and check_id in source_evidence:
        return source_evidence[check_id], []
    return evidence_status(project, check_id, now)


def section_states(
    steps: dict, progress: dict, operations: list, project: Project, now: datetime, source_evidence: dict | None = None
) -> tuple[dict, list]:
    by_section = {}
    operation_ids = []
    for operation in operations:
        step_id = (operation.request or {}).get("step_id")
        if step_id not in ("information", "basic", "advanced", "financials"):
            continue
        if operation.state not in ("pending", "running", "waiting_external", "outcome_unknown", "failed", "expired"):
            continue
        operation_ids.append(operation.id)
        current = by_section.get(step_id)
        pending = operation.state in ("pending", "running", "waiting_external", "outcome_unknown")
        if current is None or (current.state in ("failed", "expired") and pending):
            by_section[step_id] = operation
    projected = {}
    for section in ("information", "basic", "advanced", "financials"):
        original = steps.get(section) or {"status": "not_started", "evidence_refs": []}
        checks = [check for check in progress["checks"] if section == CHECK_SECTIONS.get(check["id"], "information")]
        operation = by_section.get(section)
        stale = any(check["status"] == "stale" for check in checks)
        if operation is not None:
            waiting = operation.state in ("pending", "running", "waiting_external", "outcome_unknown")
            status = "waiting_external" if waiting else "blocked"
        elif stale:
            status = "blocked"
        elif checks and all(check["status"] == "not_applicable" for check in checks):
            status = "not_applicable"
        elif checks and all(check["status"] == "complete" for check in checks):
            status = "complete"
        elif original["status"] == "not_started" and not checks:
            status = "not_started"
        elif (
            checks
            and all(check["status"] == "later" for check in checks if check["status"] != "complete")
            and any(check["status"] == "later" for check in checks)
        ):
            status = "later"
        elif original["status"] == "not_started" and section != "information" and not any(check["status"] != "not_applicable" for check in checks):
            status = "not_started"
        else:
            status = "in_progress"
        refs = []
        if status == "complete":
            for check in checks:
                if check["status"] == "complete":
                    _, verified = current_evidence(project, check["id"], now, source_evidence)
                    for ref in verified:
                        if ref not in refs:
                            refs.append(ref)
        if operation is not None:
            reason = "Awaiting external operation." if status == "waiting_external" else "External operation failed; review and retry."
        elif stale:
            reason = "Verified evidence has expired; verify again."
        else:
            reason = original.get("reason")
        projected[section] = {
            "status": status,
            "evidence_refs": refs,
            **({"reason": reason} if reason else {}),
            **({"operation_id": operation.id} if operation is not None else {}),
        }
    return projected, operation_ids


def profile_progress(
    product: Product, project: Project, steps: dict | None = None, now: datetime | None = None, source_evidence: dict | None = None
) -> dict:
    now = now or datetime.now(UTC)
    config = project.configuration or {}
    choices = {entry["check_id"]: entry for entry in config.get("applicability") or []}
    selected = selected_checks(product, project, steps, source_evidence)
    required = required_checks(product)
    checks = []
    for check_id in ("information", "project_scope", *CHECK_SECTIONS):
        if check_id not in selected:
            continue
        section = CHECK_SECTIONS.get(check_id)
        step = (steps or {}).get(section, {}) if section else {}
        choice = choices.get(check_id, {})
        status = step.get("status") if step.get("status") in ("later", "not_applicable") else choice.get("state")
        reason = step.get("reason") or choice.get("reason")
        if check_id in required:
            status = None
        if status == "not_applicable" and reason:
            checks.append({"id": check_id, "status": status, "required": False, "reason": reason})
            continue
        if status == "later":
            if current_evidence(project, check_id, now, source_evidence)[0] == "stale":
                checks.append(
                    {"id": check_id, "status": "stale", "required": check_id in required, "reason": "Verified evidence has expired; verify again."}
                )
            else:
                checks.append({"id": check_id, "status": "later", "required": check_id in required, "reason": reason or "Deferred by the user."})
            continue
        if check_id == "information":
            complete = bool(product.name and product.purpose and product.intended_users and product.owner_user_id)
        elif check_id == "project_scope":
            complete = bool(project.id and project.tenant_id == product.tenant_id)
        else:
            evidence, _ = current_evidence(project, check_id, now, source_evidence)
            if evidence == "stale":
                checks.append(
                    {
                        "id": check_id,
                        "status": "stale",
                        "required": check_id in required,
                        "reason": "Verified evidence has expired; verify again.",
                    }
                )
                continue
            complete = evidence == "complete"
        checks.append(
            {
                "id": check_id,
                "status": "complete" if complete else "missing",
                "required": check_id in required,
                "reason": ("Saved information." if check_id == "information" else "Current scoped verification.")
                if complete
                else "No current verified evidence for this check.",
            }
        )
    applicable = sum(check["status"] != "not_applicable" for check in checks)
    completed = sum(check["status"] == "complete" for check in checks)
    statuses = {check["id"]: check["status"] for check in checks}
    maturity = "saved"
    if statuses.get("repository") == "complete":
        maturity = "getting_started"
        if statuses.get("environment") == statuses.get("pipeline") == "complete":
            maturity = "advanced"
            if statuses.get("operations") == "complete" and all(statuses.get(check_id) == "complete" for check_id in required):
                maturity = "pro"
    return {
        "rule_version": "profile-1",
        "completed": completed,
        "applicable": applicable,
        "percentage": 100 * completed // applicable,
        "maturity": maturity,
        "checks": checks,
        "next_steps": [f"Complete {check['id'].replace('_', ' ')}." for check in checks if check["status"] in ("missing", "later")][:2],
    }
