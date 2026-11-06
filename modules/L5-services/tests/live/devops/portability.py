"""Verify two configured non-ADP projects share one deployed platform artifact set."""

import json
from decimal import Decimal, InvalidOperation

from qualification import MATRIX, digest, validate_manifest
from native_usage import verify_native_usage



DISTINCT = ("repository_ref", "team_ref", "policy_ref", "environment_ref")


def validate_projects(manifest):
    validate_manifest(manifest)
    projects = manifest.get("projects")
    if not isinstance(projects, list) or len(projects) != 2:
        raise ValueError("portability requires exactly two selected non-ADP projects")
    supported = {item["id"] for item in json.loads(MATRIX.read_text())["capabilities"]}
    supported -= {"native_ui", "knowledge"}
    scopes = []
    for project in projects:
        if not isinstance(project, dict) or not isinstance(project.get("scope"), dict):
            raise ValueError("each project needs a scoped private selection")
        scope = project["scope"]
        if (any(not scope.get(field) for field in ("tenant_id", "product_id", "project_id"))
                or any(not project.get(field) for field in (*DISTINCT, "account_ref", "role_ref"))
                or project.get("workload_kind") not in {"eks", "aws_resource", "lambda", "ecs"}
                or project.get("capability") not in supported or project.get("is_adp") is not False
                or not digest(project.get("source_sha"), 40)
                or not isinstance(project.get("owned_assets"), list) or not project["owned_assets"]
                or not isinstance(project.get("retained_assets"), list) or not project["retained_assets"]
                or any(not isinstance(ref, str) or not ref or ref == "*"
                       for ref in project["owned_assets"] + project["retained_assets"])
                or len(set(project["owned_assets"] + project["retained_assets"])) != len(
                    project["owned_assets"] + project["retained_assets"])):
            raise ValueError("project needs distinct scoped selections, supported workload and owned assets")
        for capability in ("operation", "context"):
            limits = project.get("limits", {}).get(capability, {})
            try:
                spend = Decimal(str(limits["max_spend_usd"]))
            except (KeyError, TypeError, InvalidOperation):
                raise ValueError("each operation requires private spend authority") from None
            if (not spend.is_finite() or spend <= 0
                    or type(limits.get("max_requests")) is not int or not 1 <= limits["max_requests"] <= 50
                    or type(limits.get("deadline_seconds")) is not int
                    or not 30 <= limits["deadline_seconds"] <= 900):
                raise ValueError("each operation needs bounded spend, requests and time")
        scopes.append(scope)
    if (scopes[0]["product_id"] == scopes[1]["product_id"]
            or any(projects[0][field] == projects[1][field] for field in (*DISTINCT, "role_ref"))
            or not any(project["workload_kind"] != "eks" for project in projects)
            or not any(project.get("selected_cross_account") is True for project in projects)):
        raise ValueError("two non-ADP Products need distinct sources and a non-EKS cross-account fixture")
    return projects


def verify_portability(manifest, driver, reader):
    """Driver configures projects; independent readers confirm effect and no redeploy."""
    projects = validate_projects(manifest)
    release = manifest["release"]
    baseline = reader.read_platform(manifest["target_ref"])
    if (not isinstance(baseline, dict)
            or any(baseline.get(key) != release[key] for key in ("source_sha", "deployment_id", "artifacts"))
            or not baseline.get("hosting_account_ref") or not baseline.get("build_id")):
        raise ValueError("shared release lacks independent artifact and platform identity")
    results = []
    retained_snapshots = []
    for project in projects:
        scope = project["scope"]
        product = reader.read_product(scope)
        if (not isinstance(product, dict) or product.get("scope") != scope
                or product.get("is_adp") is not False or product.get("team_ref") != project["team_ref"]):
            raise ValueError("authoritative Product registry does not confirm non-ADP project")
        driver.configure(project)
        owned = reader.read_assets(scope, project["owned_assets"])
        if (not isinstance(owned, dict) or set(owned) != set(project["owned_assets"])
                or any(item.get("owner_scope") != scope for item in owned.values())):
            raise ValueError("cannot clean unowned or unverified project assets")
        retained = reader.read_assets(scope, project["retained_assets"])
        if not isinstance(retained, dict) or set(retained) != set(project["retained_assets"]):
            raise ValueError("retained asset baseline is incomplete")
        try:
            configuration = reader.read_configuration(scope)
            if (not isinstance(configuration, dict) or configuration.get("scope") != scope
                    or any(configuration.get(field) != project[field] for field in DISTINCT)
                    or configuration.get("workload_kind") != project["workload_kind"]
                    or configuration.get("account_ref") != project["account_ref"]
                    or configuration.get("role_ref") != project["role_ref"]):
                raise ValueError("configuration does not match the selected project")
            access = reader.read_access(scope)
            if (not isinstance(access, dict) or access.get("scope") != scope
                    or access.get("account_ref") != project["account_ref"]
                    or access.get("role_ref") != project["role_ref"]
                    or access.get("authorized") is not True
                    or (project.get("selected_cross_account") is True
                        and access["account_ref"] == baseline["hosting_account_ref"])):
                raise ValueError("selected cross-account role was not independently authorized")
            submitted = driver.run_operation(scope, project["capability"], project["limits"]["operation"])
            native = reader.read_native(scope, submitted["run_id"])
            if (not isinstance(native, dict) or native.get("scope") != scope
                    or native.get("status") != "completed" or native.get("run_id") != submitted["run_id"]
                    or native.get("capability") != project["capability"] or not native.get("native_job_id")
                    or not native.get("result_ref") or not native.get("native_output")
                    or native.get("source_sha") != project["source_sha"]
                    or native.get("deployment_id") != release["deployment_id"]):
                raise ValueError("project requires an independently observed native result")
            later = driver.run_context(scope, native["result_ref"], project["limits"]["context"])
            context = reader.read_context(scope, later["run_id"])
            if (not isinstance(context, dict) or context.get("scope") != scope
                    or context.get("status") != "completed" or context.get("run_id") != later["run_id"]
                    or context["run_id"] == native["run_id"] or context.get("cited_result_ref") != native["result_ref"]
                    or not context.get("decision")
                    or context.get("cited_native_job_id") != native["native_job_id"]):
                raise ValueError("later Context operation did not reuse its own native result")
            foreign = projects[1] if project is projects[0] else projects[0]
            denied = driver.probe_foreign(scope, foreign["scope"])
            denial = reader.read_foreign_denial(scope, foreign["scope"])
            if (not isinstance(denied, dict) or not isinstance(denial, dict)
                    or denied.get("status") not in {403, 404} or denied.get("disclosed_resources") != []
                    or denied.get("native_session")
                    or denial.get("status") not in {403, 404} or denial.get("disclosed_resources") != []
                    or denial.get("provider_session_calls") != 0):
                raise ValueError("foreign Product/project access was not denied before federation")
            usage = reader.read_usage(scope)
            for capability in ("operation", "context"):
                limit = project["limits"][capability]
                observed = usage.get(capability, {})
                if capability == "operation":
                    verify_native_usage(observed, limit)
                    continue
                try:
                    spent = Decimal(str(observed["spend_usd"]))
                except (KeyError, TypeError, InvalidOperation):
                    raise ValueError("missing project operation usage readback") from None
                if (not spent.is_finite() or spent < 0 or spent > Decimal(str(limit["max_spend_usd"]))
                        or type(observed.get("requests")) is not int or not 0 <= observed["requests"] <= limit["max_requests"]
                        or type(observed.get("elapsed_seconds")) is not int
                        or not 0 <= observed["elapsed_seconds"] <= limit["deadline_seconds"]):
                    raise ValueError("project operation exceeded an authorized bound")
            if reader.read_platform(manifest["target_ref"]) != baseline:
                raise ValueError("second Product must not rebuild or redeploy the platform")
            retained_snapshots.append((scope, project["retained_assets"], retained))
            results.append({"scope": scope, "native": native, "context": context, "usage": usage})
        finally:
            driver.cleanup_owned(scope, project["owned_assets"])
            if (reader.read_assets(scope, project["owned_assets"]) != {}
                    or reader.read_assets(scope, project["retained_assets"]) != retained):
                raise ValueError("project-owned cleanup changed retained assets or left owned data")
    if reader.read_platform(manifest["target_ref"]) != baseline:
        raise ValueError("portable projects did not reuse the same platform artifacts")
    if any(reader.read_assets(scope, refs) != snapshot
           for scope, refs, snapshot in retained_snapshots):
        raise ValueError("another project's configuration changed retained native assets")
    return results
