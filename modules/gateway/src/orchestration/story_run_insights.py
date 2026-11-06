"""Read-only, current-attempt diagnostics and the existing live task board.

These observations never authorize recovery, acceptance, or task transitions.
Product task details are shared with the current assigned team. Unbound flows
retain their run-owner/admin boundary; private transcripts keep their own rules.
"""

import re
from types import SimpleNamespace
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from .developer_personas import OPERATIONS_PERSONAS
from .models import OrchestrationAction, OrchestrationExecution, OrchestrationFlow, OrchestrationNode
from .operations_retry_diagnosis import _completed_code
from .run_reports import FAILURE_CATEGORIES, OrchestrationRunReport


class TaskBoardSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation: int = Field(strict=True, ge=1)
    sequence: int = Field(strict=True, ge=1)
    observed_at: AwareDatetime
    text: str = Field(min_length=1, max_length=16384)


class RunFailureDiagnosis(BaseModel):
    code: str
    detail: str
    responsible: str
    next_action: str
    exit_code: int | None = None
    report_available: bool = False
    report_text: str | None = None
    reported_blocker: str | None = None
    suggested_fix: str | None = None


class StoryRunInsight(BaseModel):
    run_id: str
    attempt: int
    persona: str
    outcome: Literal["complete", "failed"] | None = None
    task_board: TaskBoardSnapshot | None = None
    board_access: Literal["available", "not_recorded", "restricted"] = "not_recorded"
    failure: RunFailureDiagnosis | None = None


def budget_failure_insight(execution, *, run_id, attempt):
    """Diagnose an exited protected worker without inventing a shared receipt.

    The caller supplies the current committed dispatch. Optional Activity reads
    cannot authorize recovery or turn a truncated history into a current cause.
    """
    from src.activity.schemas import BUDGET_STOP_REASONS

    if execution is None or execution.run_id != run_id or not execution.history_complete or not execution.runs or execution.activity is not None:
        return None
    latest = execution.runs[-1]
    if latest.status != "budget_stopped" or latest.liveness != "exited":
        return None
    reason = latest.stop_reason if latest.stop_reason in BUDGET_STOP_REASONS else None
    limit = BUDGET_STOP_REASONS.get(reason, "budget limit")
    role = "reviewer" if latest.persona in {"reviewer", "agent-codex-reviewer"} else "developer"
    return StoryRunInsight(
        run_id=latest.invocation_id,
        attempt=attempt,
        persona=latest.persona,
        outcome="failed",
        failure=RunFailureDiagnosis(
            code=reason or "budget_stopped",
            detail=f"The {role} stopped because its next model request would exceed the {limit}. Its work is incomplete.",
            responsible="budget owner",
            next_action=(
                f"Open this run's budget usage and compare spend plus the next request's reservation with the {limit}. "
                "Resolve the limiting budget through the budget controls, then use the story's recovery control to continue the saved work. "
                "Recovery still checks the approved limits and retains previous spend and attempts."
            ),
        ),
    )


_FAILURES = {
    "runtime_unverified": (
        "The operations run ended without verified delivery evidence.",
        "platform operator",
        "Read the recorded blocker below and resolve it before resuming; a clean worker exit does not establish delivery.",
    ),
    "unknown": (
        "The worker ended without a classified cause.",
        "platform operator",
        "Open this run's final report and logs to identify the failed step before resuming.",
    ),
    "cancelled": ("The worker was cancelled.", "flow owner", "Review the cancellation and resume only if this work should continue."),
    "deadline": (
        "The worker exceeded its execution time limit.",
        "flow owner",
        "Review completed and remaining tasks, then adjust the execution allowance or split the remaining work before resuming.",
    ),
    "signal": (
        "The worker process was terminated by a signal.",
        "platform operator",
        "Check the worker's termination reason and resource limits before resuming.",
    ),
    "policy": (
        "The worker reported an execution policy or budget limit.",
        "flow owner",
        "Check the recorded budget and execution policy limits; resolve the limit before resuming.",
    ),
    "provider_refusal": (
        "The model provider refused the request.",
        "flow owner",
        "Review the provider refusal in the run report and revise the task to comply with the provider's requirements.",
    ),
    "authentication": (
        "The worker could not authenticate to a required service.",
        "connection owner",
        "Check the connection and credential expiry for the failed service, restore access, then resume.",
    ),
    "transport": (
        "The worker lost its connection to a required service.",
        "platform operator",
        "Check service availability and the failed request in the run report before resuming the saved work.",
    ),
    "stale_head": (
        "The repository revision changed during the run.",
        "agent",
        "Reconcile the story branch with the current base while preserving its work, then rerun checks on the resulting revision.",
    ),
    "git_validation": (
        "The worker reported a Git validation failure.",
        "agent",
        "Use the run report to locate conflicts or invalid changes, repair them on the story branch, and rerun validation.",
    ),
    "inspection": (
        "The required inspection did not finish.",
        "agent",
        "Resume the unfinished inspection and record its findings before requesting acceptance.",
    ),
    "contract": (
        "The worker did not provide the required delivery report or contract.",
        "platform operator",
        "Check the worker's final report and reporting logs; correct the missing or rejected report before resuming.",
    ),
}

# These are runtime-authored prefixes, not guesses based on arbitrary model prose.
_OPERATIONS = {
    "turn_limit": (
        "The operations agent reached its turn limit before finishing.",
        "engine",
        "The engine checks whether it can continue from saved progress within the approved attempt, time and budget limits. "
        "The per-invocation turn limit is a platform setting; a platform operator must change it if needed.",
    ),
    "context_limit": (
        "The operations agent exhausted its model context.",
        "agent",
        "Resume from the saved branch and task board with a shorter recovery summary.",
    ),
    "budget_limit": _FAILURES["policy"],
    "sdk_timeout": _FAILURES["transport"],
    "sdk_connection_interrupted": _FAILURES["transport"],
    "control_cancelled": _FAILURES["cancelled"],
}

# Fixed names only: an arbitrary diagnosis suffix could itself contain a secret.
_LIVE_INPUTS = {
    f"missing_protected_input.ADP_L5_{service}_LIVE_CONFIG_JSON": f"ADP_L5_{service}_LIVE_CONFIG_JSON"
    for service in ("DEVOPS", "SECURITY", "FINOPS", "TRANSFORM", "WELL_ARCHITECTED")
}
_LIVE_INPUTS["missing_protected_input.devops_live_config"] = "ADP_L5_DEVOPS_LIVE_CONFIG_JSON"
MAX_CURRENT_DIAGNOSES = 64
_DIAGNOSIS_OVERFLOW = (
    "The current attempt has more completed diagnosis records than the story card can inspect.",
    "Open this run's retained repair history and reconcile its latest completed diagnosis before resuming; no partial diagnosis was selected.",
)


def _known_diagnosis(code):
    if code in _LIVE_INPUTS:
        name = _LIVE_INPUTS[code]
        return (
            f"The agent's completed diagnosis reports missing protected input {name}.",
            f"Ask the release configuration owner to supply genuine qualification inputs in the workflow's protected {name} secret. "
            "Validate the configuration against the accepted release and qualification contract, then use the existing recovery process. "
            "Do not paste secret values into task boards or reports; recording an input does not establish acceptance.",
        )
    if code == "devops_human_evidence_required":
        return (
            "The agent's completed diagnosis reports missing attributed DevOps qualification evidence.",
            "Ask the DevOps qualification owner to complete the documented native UI checks through ADP SSO and submit the actual observations "
            "through the supported human-evidence path. Keep browser and AWS Console automation disabled; retain the original acceptance checks.",
        )
    return None


async def _current_completed_diagnoses(session, *, ranked, org_id, flow_id):
    """One bounded bulk query; no task titles, prose, credentials or old attempts."""
    execution, action, report = OrchestrationExecution, OrchestrationAction, OrchestrationRunReport
    candidates = (
        select(
            execution.node_id,
            action.operation_key,
            action.detail["state"].as_string().label("state"),
            action.detail["diagnosis_code"].label("code"),
            func.row_number()
            .over(
                partition_by=execution.node_id,
                order_by=(func.coalesce(action.updated_at, action.created_at).desc(), action.id.desc()),
            )
            .label("diagnosis_position"),
        )
        .join(action, action.execution_id == execution.id)
        .join(report, (report.node_id == execution.node_id) & (report.attempt == execution.cycle))
        .join(ranked, ranked.c.run_id == report.run_id)
        .where(
            ranked.c.position == 1,
            report.org_id == org_id,
            report.flow_id == flow_id,
            report.persona.in_(OPERATIONS_PERSONAS),
            execution.org_id == org_id,
            execution.flow_id == flow_id,
            action.org_id == org_id,
            action.kind == "operations_task",
            action.status == "succeeded",
            action.operation_key.like("operations-task:%:diagnose"),
            action.detail["state"].as_string() == "done",
            func.length(action.detail["diagnosis_code"].as_string()).between(1, 64),
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(candidates)
            .where(candidates.c.diagnosis_position <= MAX_CURRENT_DIAGNOSES + 1)
            .order_by(candidates.c.node_id, candidates.c.diagnosis_position)
        )
    ).all()
    result = {}
    for row in rows:
        if row.diagnosis_position > MAX_CURRENT_DIAGNOSES:
            result[row.node_id] = _DIAGNOSIS_OVERFLOW
        elif row.node_id not in result:
            code = _completed_code(SimpleNamespace(operation_key=row.operation_key, detail={"state": row.state, "diagnosis_code": row.code}))
            if code is not None:
                # An unknown but completed diagnosis supersedes older known ones.
                result[row.node_id] = _known_diagnosis(code)
    return result


def failure_diagnosis(terminal, operations, *, runtime_unverified=False):
    failure = terminal.get("failure") or {}
    code = failure.get("category") if isinstance(failure, dict) else None
    code = code if isinstance(code, str) and code in FAILURE_CATEGORIES else "unknown"
    if runtime_unverified:
        code = "runtime_unverified"
    description = _FAILURES.get(code, _FAILURES["unknown"])
    report = operations.get("report", "") if isinstance(operations, dict) else ""
    if isinstance(report, str) and report.startswith("Operations story ended without a success result ("):
        prefix = report.split("Last assistant text:", 1)[0]
        for diagnostic, detail in _OPERATIONS.items():
            if f"Diagnostic: {diagnostic}." in prefix:
                code, description = diagnostic, detail
                break
    exit_code = failure.get("exit_code") if isinstance(failure, dict) else None
    diagnosis = RunFailureDiagnosis(
        code=code,
        detail=description[0],
        responsible=description[1],
        next_action=description[2],
        exit_code=exit_code if type(exit_code) is int and -255 <= exit_code <= 255 else None,
        report_available=bool(report),
    )
    # Model prose supplies investigation leads, never confirmed root cause or
    # retry authority. Only fixed wording and bounded AWS operation names escape.
    if isinstance(report, str):
        denied = re.findall(r"not authorized to perform:?\s+([a-z][a-z0-9-]{1,30}:[A-Za-z][A-Za-z0-9]{0,79})\b", report, re.I)
        if denied or re.search(r"\bAccessDenied(?:Exception)?\b|\bUnauthorizedOperation\b", report):
            diagnosis.reported_blocker = "The agent report includes access-denied errors" + (
                " for " + ", ".join(dict.fromkeys(denied[:5])) + "." if denied else "."
            )
            diagnosis.suggested_fix = (
                "Ask the target-account IAM owner to check the failed request, role policy, permission boundary and SCPs. "
                "Verify the required access before resuming."
            )
        elif "native_console_automation_disabled" in report or "browser_automation_disabled" in report:
            diagnosis.reported_blocker = "The historical agent report says qualification required automation that the execution policy disabled."
            diagnosis.suggested_fix = (
                "Check the current qualification implementation and accepted workflow revision against this report. "
                "Use a supported API or attributed human-observation path while keeping browser and AWS Console automation disabled; "
                "the path must still satisfy the original acceptance checks. "
                "If neither path exists, resolve that integration gap before resuming. A disabled-policy error does not authorize automation."
            )
        elif re.search(r"mergeable_state.{0,10}dirty|branch_not_mergeable|repair workspace.{0,30}(behind|stale)", report, re.I):
            diagnosis.reported_blocker = "The agent report identifies a stale or conflicting repair branch."
            diagnosis.suggested_fix = (
                "Reconcile the retained repair branch with current main, preserve its changes, and run checks on the resulting head."
            )
        elif re.search(r"FileNotFoundError|No such file or directory", report):
            diagnosis.reported_blocker = "The agent report includes a missing-file error."
            diagnosis.suggested_fix = (
                "Compare the run's starting revision and file path with the story branch and current base. "
                "Reconcile the branch while preserving its changes, then verify the required file before resuming."
            )
        elif re.search(r"artifact.{0,80}(already exists|same name|conflict)", report, re.I):
            diagnosis.reported_blocker = "The agent report includes an artifact-name collision."
            diagnosis.suggested_fix = "Check the upload step and use a unique artifact name for this workflow attempt, then rerun the failed step."
    return diagnosis


def project_insight(row, *, may_read_board, completed_diagnosis=None):
    terminal = row.terminal_receipt
    valid_terminal = (
        isinstance(terminal, dict)
        and terminal.get("contract_version") == 1
        and terminal.get("run_id") == row.run_id
        and type(terminal.get("attempt")) is int
        and terminal["attempt"] == row.attempt
        and terminal.get("outcome") in {"complete", "failed"}
    )
    board = None
    if may_read_board and row.task_board_snapshot:
        try:
            board = TaskBoardSnapshot.model_validate(
                {key: row.task_board_snapshot[key] for key in ("generation", "sequence", "observed_at", "text") if key in row.task_board_snapshot}
            )
        except (ValidationError, TypeError):
            pass
    operations = row.operations_report_receipt
    if not (
        isinstance(operations, dict)
        and operations.get("run_id") == row.run_id
        and type(operations.get("attempt")) is int
        and operations["attempt"] == row.attempt
        and operations.get("recorded") is True
    ):
        operations = None
    runtime_unverified = (
        valid_terminal and terminal["outcome"] == "complete" and getattr(row, "node_state", None) == "failed" and row.persona in OPERATIONS_PERSONAS
    )
    failure = (
        failure_diagnosis(terminal, operations, runtime_unverified=runtime_unverified)
        if valid_terminal and (terminal["outcome"] == "failed" or runtime_unverified)
        else None
    )
    if failure and may_read_board and operations and isinstance(operations.get("report"), str):
        from .ci_failure_evidence import redact_details

        failure.report_text = redact_details(operations["report"], limit=12000)
    if failure and row.persona in OPERATIONS_PERSONAS and completed_diagnosis:
        # This adds an attributed investigation lead without rewriting the
        # terminal/runtime cause or implying permission to retry or accept.
        failure.reported_blocker, failure.suggested_fix = completed_diagnosis
    return StoryRunInsight(
        run_id=row.run_id,
        attempt=row.attempt,
        persona=row.persona,
        outcome=terminal["outcome"] if valid_terminal else None,
        task_board=board,
        board_access="restricted" if not may_read_board else "available" if board else "not_recorded",
        failure=failure,
    )


async def current_story_run_insights(session, *, current_user, flow_id, node_ids=None):
    from src.admin.access_control import AccessControl
    from src.admin.config import Permission
    from src.admin.exceptions import AccessDeniedError
    from src.products.models import Product
    from src.shared.identity.product_scope import product_member, product_visibility_scope
    from src.shared.identity.resolver import resolve_canonical_user_id

    org_id = current_user.org_id
    report, node = OrchestrationRunReport, OrchestrationNode
    # Rank assignments before inspecting their board. A new run without a board
    # must never resurrect a previous run's activity as current work.
    ranked = (
        select(
            report.run_id,
            OrchestrationFlow.product_id,
            node.state.label("node_state"),
            func.row_number().over(partition_by=report.node_id, order_by=(report.created_at.desc(), report.run_id.desc())).label("position"),
        )
        .join(node, node.id == report.node_id)
        .join(OrchestrationFlow, (OrchestrationFlow.id == node.flow_id) & (OrchestrationFlow.org_id == node.org_id))
        .where(
            report.org_id == org_id,
            report.flow_id == flow_id,
            node.org_id == org_id,
            node.flow_id == flow_id,
            node.kind == "story",
            node.attempts == report.attempt,
            *([node.id.in_(node_ids)] if node_ids is not None else []),
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                report.run_id,
                ranked.c.product_id,
                ranked.c.node_state,
                report.node_id,
                report.attempt,
                report.persona,
                report.terminal_receipt,
                report.task_board_snapshot,
                report.operations_report_receipt,
                report.dispatch_metadata["actor"]["user_id"].as_string().label("owner_id"),
            )
            .join(ranked, ranked.c.run_id == report.run_id)
            .where(ranked.c.position == 1)
        )
    ).all()
    if not rows:
        return {}
    may_read_all = False
    try:
        await AccessControl(session).check_permission(current_user, Permission.ACTIVITY_READ_ALL, target_org_id=org_id)
        may_read_all = True
    except AccessDeniedError:
        pass
    product_id = rows[0].product_id
    may_read_product = False
    owner_id = None
    if product_id and not may_read_all:
        # Recheck current membership on every graph/activity refresh. A former
        # team member cannot retain Product task details through run ownership,
        # an old users.team_id pointer, or a cached display projection.
        member = await product_member(
            session,
            user_id=current_user.user_id,
            org_id=org_id,
            username=current_user.cognito_username,
            account_type=current_user.account_type or "human",
            is_platform_admin=current_user.is_admin,
        )
        may_read_product = member is not None and bool(
            await session.scalar(
                select(Product.id).where(
                    Product.id == product_id,
                    product_visibility_scope(member, org_id, is_platform_admin=current_user.is_admin),
                )
            )
        )
    elif not product_id and not may_read_all:
        owner_id = await resolve_canonical_user_id(session, current_user.user_id, org_id=org_id)
    diagnoses = await _current_completed_diagnoses(session, ranked=ranked, org_id=org_id, flow_id=flow_id)
    return {
        row.node_id: project_insight(
            row,
            may_read_board=may_read_all or may_read_product or (owner_id is not None and row.owner_id == owner_id),
            completed_diagnosis=diagnoses.get(row.node_id),
        )
        for row in rows
    }
