"""Verify a running release job without requiring its later success evidence."""

import re
from types import SimpleNamespace

from fastapi import HTTPException

from .dispatch_pass import attempt_run_id
from .operations_continuation_authority import assignment_matches
from .operations_release_job import NativeReleaseJob
from .run_reports import OrchestrationRunReport


async def running_workflow(db, *, action, execution, node, claim, contract, pins):
    # Imported locally because the common observation errors live with scope.
    from .l5_observation_scope import ObservationError, digest, require

    saved = action.detail
    release = contract.node.operational_release_binding
    delivery = release.delivery_contract
    correlation = attempt_run_id(node.id, node.attempts)
    report = await db.get(OrchestrationRunReport, correlation, populate_existing=True)
    require(
        report is not None
        and report.org_id == node.org_id
        and report.flow_id == node.flow_id
        and report.node_id == node.id
        and report.attempt == execution.cycle == node.attempts
        and report.repo == release.repository
        and type(report.installation_id) is int
        and report.installation_id > 0
        and type(report.provider_repository_id) is int
        and report.provider_repository_id > 0
        and report.provider_repository_id == claim.provider_repository_id,
        "l5_observation_assignment_changed",
    )
    require(await assignment_matches(db, node=node, execution=execution, report=report), "l5_observation_assignment_changed")
    require(claim.active_run_id == (execution.handoff_receipt_ref or correlation), "l5_observation_claim_changed")
    require(
        saved.get("source_revision") == pins.get("source_revision")
        and saved.get("definition_revision") == pins.get("definition_revision")
        and saved.get("branch") == pins.get("default_branch"),
        "l5_observation_source_changed",
    )
    require(
        saved.get("correlation") == correlation
        and saved.get("contract_digest") == delivery.digest
        and saved.get("repo") == report.repo
        and type(saved.get("provider_repository_id")) is int
        and saved["provider_repository_id"] == report.provider_repository_id
        and saved.get("claim_id") == claim.id == execution.claim_id
        and type(saved.get("claim_generation")) is int
        and saved["claim_generation"] == claim.generation == execution.claim_generation
        and saved.get("workflow") == ".github/workflows/l5-services-release.yml"
        and isinstance(saved.get("dispatch_revision"), str)
        and re.fullmatch(r"[0-9a-f]{40}", saved["dispatch_revision"])
        and not saved.get("terminal_run"),
        "l5_observation_workflow_changed",
    )
    # Only authorize_job writes this pin after authenticating workflow OIDC and
    # independently reading the provider. Never take a run locator from a caller.
    pin = saved.get("release_job")
    require(
        isinstance(pin, dict)
        and isinstance(pin.get("run_id"), str)
        and re.fullmatch(r"[1-9][0-9]{0,18}", pin["run_id"])
        and isinstance(pin.get("run_attempt"), str)
        and re.fullmatch(r"[1-9][0-9]{0,5}", pin["run_attempt"]),
        "l5_observation_workflow_unverified",
    )
    initial = saved.get("run_id") is None and saved.get("run_attempt") is None and pin["run_attempt"] == "1"
    rerun = (
        type(saved.get("run_id")) is int
        and type(saved.get("run_attempt")) is int
        and saved["run_id"] == int(pin["run_id"])
        and saved["run_attempt"] >= 1
        and saved["run_attempt"] + 1 == int(pin["run_attempt"])
    )
    suffix = "initial" if initial else f"rerun:{saved.get('run_id')}:{saved.get('run_attempt')}"
    require(
        (initial or rerun) and action.operation_key == f"delivery-dispatch:{pins['source_revision']}:{suffix}",
        "l5_observation_workflow_changed",
    )
    binding = SimpleNamespace(
        org_id=report.org_id,
        repo=report.repo,
        installation_id=report.installation_id,
        provider_repository_id=report.provider_repository_id,
    )
    body = SimpleNamespace(
        correlation=correlation,
        dispatch_revision=saved["dispatch_revision"],
        workflow="l5-services-release.yml",
        contract_digest=delivery.digest,
    )
    try:
        # terminal=True obtains the raw run and derives the installed App's bot
        # identity. It does NOT require or establish successful release evidence.
        run, _ = await NativeReleaseJob(None).verify_run(pin, binding, body, terminal=True, allow_in_progress=True)
    except HTTPException:
        raise ObservationError("l5_observation_workflow_changed") from None
    except Exception:
        raise ObservationError("l5_observation_workflow_provider_unavailable", 503) from None
    require(run.get("status") == "in_progress" and run.get("conclusion") is None, "l5_observation_workflow_not_running")
    context = {
        key: saved[key]
        for key in (
            "correlation",
            "contract_digest",
            "repo",
            "provider_repository_id",
            "claim_id",
            "claim_generation",
            "workflow",
            "source_revision",
            "definition_revision",
            "dispatch_revision",
            "branch",
        )
    }
    context.update(run_id=int(pin["run_id"]), run_attempt=int(pin["run_attempt"]))
    return {
        "provenance": "oidc-authorized-delivery-dispatch-and-provider-run",
        "dispatch_action_id": action.id,
        "execution_id": execution.id,
        "cycle": execution.cycle,
        "accepted_plan_version": execution.accepted_plan_version,
        **context,
        "dispatch_context_sha256": digest(context),
    }
