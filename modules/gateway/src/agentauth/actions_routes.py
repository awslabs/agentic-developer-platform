"""Gateway-held Actions mutations for an accepted operations story."""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.actions_logs import MAX_LOG_TAIL_BYTES, fetch_log_tail, redact_text
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.execution import ExecutionStateError
from src.agentauth.github_operation_routes import _authenticated
from src.agentauth.github_operation_service import (
    OperationRefusedError,
    authorize_actions_read_grant,
    authorize_operations_grant,
    current_claim_generation,
    installation_token,
    require_current_claim,
)
from src.agentauth.routes import AgentRuntime, get_agent_runtime, require_agent_transport
from src.agentauth.run_credential import CredentialError
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import WorkloadRefusedError
from src.orchestration.delivery_contract import TERMINAL_WORKFLOW_FAILURES, release_manifest
from src.orchestration.deployment_workflow_provider import (
    MAX_WORKFLOW_RESPONSE_BYTES,
    WorkflowProvider,
    decode_workflow_content,
    workflow_input_defaults,
)
from src.orchestration.product_case_selection import PRODUCT_CASE_BINDINGS
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.runtime_policy import ACTIONS_OPERATION_PATH, authorize_worker_credential
from src.orchestration.validation_diagnostics import safe_code
from src.shared.database import get_session_factory

logger = logging.getLogger(__name__)

# Runtime-verification hold reasons the worker may see verbatim in its tool result.
# Everything else collapses to "runtime_verification_unavailable" so a wrong runtime is
# never described to the worker in a way it could argue with; these name the operator's
# own prerequisites (manifest, policy, target, authority, provider bounds) and the
# cluster read-back refusals a missing Role produces (#7870).
RUNTIME_HOLD_REASONS = frozenset(
    {
        "operations_runtime_manifest_unavailable",
        "operations_runtime_policy_missing",
        "operations_runtime_target_not_authorized",
        "operations_runtime_target_changed",
        "operations_runtime_run_changed",
        "operations_runtime_component_changed",
        "operations_runtime_observation_expired",
        "operations_runtime_authority_changed",
        # Provider-shaped refusals are the operator's to act on too: a listing over the
        # byte bound or past the page bound names the exact cause, whereas "unavailable"
        # sent the TLS-X05 worker hunting in the wrong place.
        "deployment_provider_response_limit",
        "deployment_provider_history_limit",
        # The cluster API refused (401/403) or failed the verifier's read-back: an RBAC or
        # access-entry gap in the target namespace, not a deployment defect (#7870).
        "deployment_runtime_read_forbidden",
        "deployment_runtime_read_failed",
        # The Door identity probe could not reach the Door at all (a NetworkPolicy dropping
        # the probe path, a missing Service) — the deployment path, not the release (#7870).
        "deployment_door_identity_probe_unreachable",
    }
)


router = APIRouter(prefix=ACTIONS_OPERATION_PATH, tags=["agent-authority"], dependencies=[Depends(require_agent_transport)])


class DispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml$")
    inputs: dict[str, str] = Field(default_factory=dict, max_length=32)


class RerunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: int = Field(gt=0)


class LogsRequest(RerunRequest):
    """Failure-focused excerpt by default; `tail_bytes` requests a literal tail."""

    tail_bytes: int | None = Field(default=None, ge=512, le=MAX_LOG_TAIL_BYTES)


async def _authority(request: Request, runtime: AgentRuntime, *, mutate: bool):
    pod, caller, record, grant = await _authenticated(runtime, request)
    await runtime.validate_flow(record, grant)
    execution = await run_in_threadpool(runtime.store._read, f"TENANT#{caller.tenant_id}", f"EXEC#{caller.invocation_id}")
    if not execution:
        raise OperationRefusedError("protected execution record is unavailable")
    async with get_session_factory()() as session:
        decision = await authorize_worker_credential(session, execution=execution, grant=grant, broker_path=ACTIONS_OPERATION_PATH)
        authorize_grant = authorize_operations_grant if mutate else authorize_actions_read_grant
        issued = await authorize_grant(session, execution=execution, grant=grant, decision=decision)
        if mutate:
            generation = await current_claim_generation(session, org_id=caller.tenant_id, invocation_id=caller.invocation_id)
            await require_current_claim(session, org_id=caller.tenant_id, invocation_id=caller.invocation_id, claim_generation=generation)
        if execution.get("persona", {}).get("S") in {"operations", "agent-codex-operations"}:
            from src.orchestration.operations_repair import RepairMergeRequiredError, repair_revisions

            try:
                pins = await repair_revisions(
                    session,
                    node_id=issued.node_id,
                    org_id=grant.tenant_id,
                    plan_version=issued.plan_version,
                    cycle=int(execution["orchestration_node_attempt"]["N"]),
                    pins={name: execution.get(name, {}).get("S") for name in ("source_revision", "definition_revision", "default_branch")},
                    require_merged=mutate,
                )
            except RepairMergeRequiredError as exc:
                raise OperationRefusedError(str(exc)) from None
            if pins is None:
                raise OperationRefusedError("repair revision authority changed")
            execution = {**execution, **{name: {"S": value} for name, value in pins.items() if value is not None}}
        await session.commit()
    binding = SimpleNamespace(
        repo=issued.repository,
        provider_repository_id=decision.provider_repository_id,
        installation_id=int(execution["installation_id"]["N"]),
        org_id=grant.tenant_id,
    )
    return issued, binding, execution


async def _definition(
    provider,
    binding,
    *,
    workflow: str,
    revision: str,
    branch: str,
    token: str,
    observing_push: bool = False,
    head: str | None = None,
    for_dispatch: bool = True,
    allow_dev_dispatch: bool = False,
    generic_delivery: bool = False,
):
    """Dispatch defaults of the pinned workflow definition, verified byte-identical at a second ref.

    The second ref is the dispatch branch tip before a dispatch, or the observed
    run's own head when a completed run is being bound as evidence (#7662): main
    moves between pin and run, so the comparison must be against what ran.
    """
    path = f"/repos/{binding.repo}/contents/.github/workflows/{quote(workflow)}"
    contents = []
    for ref in (revision, head or branch):
        response = await provider.request(binding, "GET", path, token=token, max_bytes=MAX_WORKFLOW_RESPONSE_BYTES, params={"ref": ref})
        entry = response.json()
        try:
            raw = decode_workflow_content(entry)
        except ValueError as exc:
            raise OperationRefusedError(str(exc)) from None
        try:
            document = yaml.safe_load(raw)
        except (yaml.YAMLError, UnicodeDecodeError):
            raise OperationRefusedError("workflow definition is invalid") from None
        events = document.get("on", document.get(True, {})) if isinstance(document, dict) else {}
        dispatch = events.get("workflow_dispatch") if isinstance(events, dict) else None
        if not observing_push and dispatch is None and (not isinstance(events, dict) or "workflow_dispatch" not in events):
            raise OperationRefusedError("workflow_dispatch is unavailable")
        inputs = dispatch.get("inputs", {}) if isinstance(dispatch, dict) else {}
        if not isinstance(inputs, dict):
            raise OperationRefusedError("workflow inputs are invalid")
        # The maintained Gateway backend and frontend workflows use this
        # optional input only for non-dev deployments. Their reviewed dev
        # defaults need no approval.
        # Keep every other approval-bearing workflow refused; callers still
        # cannot override defaults, and both definition refs must match below.
        approval = inputs.get("adp_approved_revision")
        environment = inputs.get("environment")
        dev_gateway_defaults = (
            allow_dev_dispatch
            and workflow in {"gateway-deploy.yml", "gateway-frontend-deploy.yml"}
            and isinstance(environment, dict)
            and environment.get("default") == "dev"
            and isinstance(approval, dict)
            and approval.get("required", False) is False
            and approval.get("default") == ""
        )
        if "adp_approved_revision" in inputs and for_dispatch and not observing_push and not dev_gateway_defaults and not generic_delivery:
            raise OperationRefusedError("human_approval_input")
        if not observing_push and not {"adp_correlation", "adp_source_revision", "adp_definition_revision"} <= inputs.keys():
            raise OperationRefusedError("correlated workflow inputs are unavailable")
        contents.append(raw)
    if contents[0] != contents[1]:
        raise OperationRefusedError("workflow definition changed on dispatch branch")
    return workflow_input_defaults(inputs, generic_delivery=generic_delivery)


_TRANSPORT_INPUTS = frozenset({"adp_correlation", "adp_source_revision", "adp_definition_revision"})
_PROOF_PHASE = "stage.identity-proof"
_PROOF_WORKFLOW = "gateway-deploy.yml"


def _inputs_match_defaults(
    recorded: dict[str, str],
    defaults: dict[str, str],
    *,
    product_cases: str = "none",
    proof_phase: str = "none",
    source_revision: str | None = None,
    adapter_stage: bool = False,
    foundation_gateway: bool = False,
    accepted_inputs: dict[str, str] | None = None,
) -> bool:
    """The run used exactly the reviewed defaults for every non-transport input.

    The engine dispatches only the transport inputs, so a correlated run must have
    run with each declared input at its default. A publisher may record only the
    inputs it received (tools-facade-deploy omits an empty rollback digest); an
    omitted input can only have run with its default, because GitHub applies the
    default before the workflow starts. A recorded input that is not declared, or a
    recorded value that differs from the default, is not the reviewed run.
    """
    expected = {name: value for name, value in defaults.items() if name not in _TRANSPORT_INPUTS}
    if accepted_inputs is not None:
        if any(name not in expected or recorded.get(name) != value for name, value in accepted_inputs.items()):
            return False
        expected.update(accepted_inputs)
        actual = {name: value for name, value in recorded.items() if name not in _TRANSPORT_INPUTS}
        return {**expected, **actual} == expected
    if product_cases != "none":
        if defaults.get("product_cases") != "none" or recorded.get("product_cases") != product_cases:
            return False
        expected["product_cases"] = product_cases
    if proof_phase != "none":
        if (
            proof_phase != _PROOF_PHASE
            or product_cases != "none"
            or defaults.get("environment") != "dev"
            or defaults.get("product_cases") != "none"
            or defaults.get("proof_phase") != "none"
            or not isinstance(source_revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}", source_revision)
            or recorded.get("proof_phase") != proof_phase
        ):
            return False
        expected["proof_phase"] = proof_phase
    if adapter_stage:
        if defaults.get("stage") != "platform" or recorded.get("stage") != "tool-adapters" or product_cases != "none" or proof_phase != "none":
            return False
        expected["stage"] = "tool-adapters"
    if foundation_gateway:
        if (
            adapter_stage
            or defaults.get("release_group") != "none"
            or recorded.get("release_group") != "atlassian-foundation"
            or defaults.get("environment") != "dev"
            or product_cases != "none"
            or proof_phase != "none"
        ):
            return False
        expected["release_group"] = "atlassian-foundation"
    actual = {name: value for name, value in recorded.items() if name not in _TRANSPORT_INPUTS}
    return {**expected, **actual} == expected


async def _record_dispatch_check(factory, issued, binding, code, *, passed=False, invocation_id=None):
    if issued is None or binding is None or not getattr(issued, "node_id", None):
        return
    from src.orchestration.validation_diagnostics import record_runtime_validation

    await record_runtime_validation(
        factory or get_session_factory(), binding=binding, issued=issued, code=code, passed=passed, stage="dispatch", invocation_id=invocation_id
    )


async def _perform(
    request: Request,
    runtime: AgentRuntime,
    *,
    workflow: str | None,
    run_id: int | None,
    inputs: dict[str, str],
    authority=None,
    factory=None,
    native_retry=None,
    continuation_context=None,
):
    # The internal controller supplies its own service authority. HTTP routes
    # never accept an authority callback or controller identity from the caller.
    async def authorize():
        return await authority() if authority is not None else await _authority(request, runtime, mutate=True)

    issued = binding = None
    try:
        if (native_retry is not None or continuation_context is not None) and (
            authority is None
            or runtime is not None
            or factory is None
            or run_id is not None
            or (native_retry is not None and continuation_context is not None)
        ):
            raise OperationRefusedError("native release retry requires a trusted controller")
        if continuation_context is not None and (not isinstance(continuation_context, tuple) or len(continuation_context) != 2):
            raise OperationRefusedError("native release continuation requires a trusted controller")
        issued, binding, execution = await authorize()
        delivery = getattr(issued, "delivery_contract", None)
        source = execution.get("source_revision", {}).get("S", "")
        definition = execution.get("definition_revision", {}).get("S", "")
        correlation = execution.get("invocation_id", {}).get("S", "")
        branch = execution.get("default_branch", {}).get("S", "")
        if any(len(value) != 40 or not all(letter in "0123456789abcdef" for letter in value) for value in (source, definition)):
            raise OperationRefusedError("engine revisions are unavailable")
        if not branch or not correlation or len(correlation) > 64:
            raise OperationRefusedError("engine dispatch binding is unavailable")
        if workflow is not None and workflow not in issued.workflow_allowlist:
            raise OperationRefusedError("workflow is not allow-listed")
        token = await installation_token(
            org_id=binding.org_id,
            installation_id=binding.installation_id,
            repository=binding.repo,
            permissions={**issued.permissions, "contents": "read"},
        )
        provider = WorkflowProvider()
        automatic_run = False
        dispatch_head = definition
        if run_id is not None:
            response = await provider.request(binding, "GET", f"/repos/{binding.repo}/actions/runs/{run_id}", token=token)
            run = response.json()
            path = run.get("path", "")
            workflow = path.removeprefix(".github/workflows/")
            own_dispatch = run.get("event") == "workflow_dispatch" and run.get("display_title") == "ADP deployment " + correlation
            if (
                workflow not in issued.workflow_allowlist
                or run.get("repository", {}).get("id") != binding.provider_repository_id
                or run.get("event") not in {"push", "workflow_dispatch"}
                # An engine dispatch runs at the default-branch tip, which is past the
                # pin as soon as main moves (#7828); the correlation title proves the
                # run is this execution's own dispatch, and the definition is verified
                # byte-identical at the run's own head below. A push run must still be
                # the pinned revision itself.
                or (run.get("head_sha") != definition and not own_dispatch)
                or not isinstance(run.get("head_sha"), str)
                or len(run["head_sha"]) != 40
                or run.get("head_branch") != branch
                or (run.get("event") == "workflow_dispatch" and not own_dispatch)
                or (run.get("conclusion") not in TERMINAL_WORKFLOW_FAILURES if delivery is not None else run.get("conclusion") != "failure")
                or (delivery is not None and run.get("status") != "completed")
            ):
                raise OperationRefusedError("run is not a failed deployment for this revision")
            automatic_run = run.get("event") == "push"
            rerun_head = run["head_sha"] if own_dispatch and run["head_sha"] != definition else None
        else:
            response = await provider.request(binding, "GET", f"/repos/{binding.repo}/git/ref/heads/{quote(branch, safe='')}", token=token)
            ref = response.json()
            obj = ref.get("object") if isinstance(ref, dict) else None
            if not isinstance(obj, dict) or obj.get("type") != "commit" or ref.get("ref") != f"refs/heads/{branch}":
                raise OperationRefusedError("dispatch branch is unavailable")
            dispatch_head = obj.get("sha")
            if not isinstance(dispatch_head, str) or not re.fullmatch(r"[0-9a-f]{40}", dispatch_head):
                raise OperationRefusedError("dispatch branch is unavailable")
            if not await _head_contains_pin(provider, binding, pin=definition, head=dispatch_head, token=token):
                raise OperationRefusedError("dispatch branch does not contain the approved definition")
            # Verify immutable bytes, then tell the workflow which exact revision
            # was verified. The approved execution pin remains unchanged. A branch
            # move after this read is refused by the workflow's exact-SHA guard.
            rerun_head = dispatch_head
        defaults = await _definition(
            provider,
            binding,
            workflow=workflow,
            revision=definition,
            branch=branch,
            token=token,
            observing_push=automatic_run,
            head=rerun_head,
            # A rerun reuses GitHub's original inputs. Its correlation title
            # alone does not prove those inputs used the reviewed dev defaults.
            allow_dev_dispatch=run_id is None,
            generic_delivery=delivery is not None,
        )
        product_cases = getattr(issued, "product_cases", "none")
        if product_cases != "none" and (
            run_id is not None
            or workflow != PRODUCT_CASE_BINDINGS.get(product_cases, (None, None))[1]
            or defaults.get("environment") != "dev"
            or defaults.get("product_cases") != "none"
        ):
            raise OperationRefusedError("reviewed Product workflow selection is unavailable")
        proof_phase = getattr(issued, "proof_phase", "none")
        if proof_phase != "none" and (
            proof_phase != _PROOF_PHASE
            or product_cases != "none"
            or run_id is not None
            or workflow != _PROOF_WORKFLOW
            or defaults.get("environment") != "dev"
            or defaults.get("product_cases") != "none"
            or defaults.get("proof_phase") != "none"
        ):
            raise OperationRefusedError("reviewed Identity proof workflow selection is unavailable")
        if delivery is not None and (any(name not in defaults for name in delivery.inputs) or automatic_run):
            raise OperationRefusedError("accepted delivery inputs are unavailable")
        if inputs and (delivery is None or inputs != delivery.workflow_inputs):
            raise OperationRefusedError("custom workflow inputs are not admitted")
        current, current_binding, current_execution = await authorize()
        if current != issued or current_binding != binding or current_execution != execution:
            raise OperationRefusedError("operations authority changed")
        admitted_component = None
        if execution.get("persona", {}).get("S") in {"operations", "agent-codex-operations"}:
            if native_retry is None:
                admitted_component = await _require_shared_release_admission(
                    issued,
                    binding,
                    workflow,
                    factory or get_session_factory(),
                    execution,
                    environment=delivery.workflow_inputs.get("environment", defaults.get("environment"))
                    if delivery is not None
                    else defaults.get("environment"),
                    initial=run_id is None and continuation_context is None,
                    dispatch_revision=dispatch_head if run_id is None else run.get("head_sha") if delivery is not None else None,
                    continuation_context=continuation_context,
                    provider=provider,
                    token=token,
                    **({"run_id": run_id, "run_attempt": run.get("run_attempt")} if delivery is not None and run_id is not None else {}),
                )
            else:
                from src.orchestration.operations_publication_recovery import ImageAbsence
                from src.orchestration.operations_release_job import NativeReleaseJob

                if not (
                    isinstance(native_retry, tuple)
                    and len(native_retry) == 2
                    and defaults.get("environment") == "dev"
                    and workflow in issued.workflow_allowlist
                ):
                    raise OperationRefusedError("native release retry scope changed")
                recovered = await NativeReleaseJob(factory).recover_terminal_publication(
                    native_retry[0],
                    native_retry[1],
                    reserve_retry=True,
                    expected_workflow=workflow,
                    expected_dispatch_revision=dispatch_head,
                )
                if not isinstance(recovered, ImageAbsence) or recovered.repository != "adp-tools-dev-adapters":
                    raise OperationRefusedError("native release retry scope changed")
                admitted_component = "tool-adapters" if workflow == "tools-platform-deploy.yml" else None
        elif native_retry is not None or continuation_context is not None:
            raise OperationRefusedError("native release retry scope changed")
        reservation = admitted_component if isinstance(admitted_component, dict) and delivery is not None else None
        if reservation is not None and reservation["already_dispatched"]:
            return {"dispatched": True, "workflow": workflow, "correlation": correlation, "reconciled": True}
        adapter_stage = admitted_component == "tool-adapters"
        foundation_gateway = admitted_component == "gateway-backend"
        if adapter_stage and (workflow != "tools-platform-deploy.yml" or defaults.get("stage") != "platform"):
            raise OperationRefusedError("adapter release stage is unavailable")
        if foundation_gateway and (
            workflow != "gateway-deploy.yml"
            or defaults.get("release_group") != "none"
            or defaults.get("environment") != "dev"
            or product_cases != "none"
            or proof_phase != "none"
        ):
            raise OperationRefusedError("gateway foundation release is unavailable")
        from src.orchestration.generic_delivery_dispatch import settle_dispatch

        try:
            if run_id is None:
                await provider.request(
                    binding,
                    "POST",
                    f"/repos/{binding.repo}/actions/workflows/{quote(workflow)}/dispatches",
                    token=token,
                    json={
                        "ref": branch,
                        "inputs": {
                            **(delivery.inputs if delivery is not None else {}),
                            "adp_correlation": correlation,
                            "adp_source_revision": source,
                            "adp_definition_revision": dispatch_head,
                            **({"stage": "tool-adapters"} if adapter_stage is True else {}),
                            **({"release_group": "atlassian-foundation"} if foundation_gateway else {}),
                            **({"product_cases": product_cases} if product_cases != "none" else {}),
                            **({"proof_phase": proof_phase} if proof_phase != "none" else {}),
                        },
                    },
                )
                if reservation is not None:
                    await settle_dispatch(factory or get_session_factory(), reservation, org_id=binding.org_id, succeeded=True)
                await _record_dispatch_check(factory, issued, binding, "dispatch_verified", passed=True, invocation_id=correlation)
                return {"dispatched": True, "workflow": workflow, "correlation": correlation}
            await provider.request(binding, "POST", f"/repos/{binding.repo}/actions/runs/{run_id}/rerun", token=token)
            if reservation is not None:
                await settle_dispatch(factory or get_session_factory(), reservation, org_id=binding.org_id, succeeded=True)
            await _record_dispatch_check(factory, issued, binding, "dispatch_verified", passed=True, invocation_id=correlation)
            return {"rerun": True, "run_id": run_id, "correlation": correlation}
        except Exception:
            if reservation is not None:
                await settle_dispatch(factory or get_session_factory(), reservation, org_id=binding.org_id, succeeded=False)
            raise

    except (BootstrapRefusedError, WorkloadRefusedError, CredentialError, ExecutionStateError):
        raise HTTPException(404, "actions operation is not authorized") from None
    except AuthorityStoreError:
        raise HTTPException(503, "agent authority unavailable") from None
    except (httpx.HTTPError, CycleBlockedError) as exc:
        await _record_dispatch_check(
            factory,
            issued,
            binding,
            exc.reason if isinstance(exc, CycleBlockedError) else "provider_unavailable",
            invocation_id=execution.get("invocation_id", {}).get("S") if issued is not None else None,
        )
        if isinstance(exc, CycleBlockedError) and getattr(issued, "delivery_contract", None) is not None:
            raise HTTPException(409, safe_code(exc.reason)) from None
        raise HTTPException(503, "Actions provider unavailable") from None
    except OperationRefusedError as exc:
        await _record_dispatch_check(
            factory,
            issued,
            binding,
            str(exc).replace(" ", "_"),
            invocation_id=execution.get("invocation_id", {}).get("S") if issued is not None else None,
        )
        if str(exc) == "human_approval_input":
            raise HTTPException(403, "human_approval_input") from None
        if str(exc) == "operations_repair_merge_required":
            raise HTTPException(409, "operations_repair_merge_required") from None
        if getattr(issued, "delivery_contract", None) is not None:
            raise HTTPException(409, safe_code(str(exc).replace(" ", "_"))) from None
        raise HTTPException(404, "actions operation is not authorized") from None


async def _require_shared_release_admission(
    issued,
    binding,
    workflow,
    factory,
    execution,
    *,
    environment=None,
    initial=False,
    dispatch_revision=None,
    continuation_context=None,
    run_id=None,
    run_attempt=None,
    provider=None,
    token=None,
):
    from src.orchestration.accepted_node import accepted_node_contract
    from src.orchestration.deployment_manifest import EntryStatus, load_packaged_manifest
    from src.orchestration.models import OrchestrationFlow, OrchestrationNode

    async with factory() as session:
        node = await session.get(OrchestrationNode, issued.node_id)
        flow = await session.get(OrchestrationFlow, node.flow_id) if node is not None else None
        contract = await accepted_node_contract(session, node) if node is not None else None
        if (
            node is None
            or flow is None
            or contract is None
            or node.org_id != binding.org_id
            or flow.org_id != binding.org_id
            or contract.plan_version != issued.plan_version
        ):
            raise OperationRefusedError("operations release authority changed")
        foundation = flow.slug == "atlassian-adapters-8445-parallel" and node.wave_ref == "w03b-foundation-release"
        release = getattr(contract.node, "operational_release_binding", None)
        if release is None:
            components = [
                expectation.removeprefix("runtime_verified:")
                for expectation in contract.node.evidence_expectations
                if expectation.startswith("runtime_verified:")
            ]
            candidate_entries = [
                entry
                for entry in load_packaged_manifest().entries
                if entry.status is EntryStatus.ENABLED
                and not entry.docs_only
                and any(entry.covers(component) for component in components)
                and entry.connection_id in getattr(contract, "environment_connection_ids", ())
                and entry.workflow is not None
                and entry.workflow.path == f".github/workflows/{workflow}"
            ]
            source = execution.get("source_revision", {}).get("S")
            definition = execution.get("definition_revision", {}).get("S")
            matching = [
                entry for entry in candidate_entries if entry.artifact_revision == source and entry.workflow.definition_revision == definition
            ]
            if len(matching) > 1:
                raise OperationRefusedError("operations release target ambiguous")
            if foundation or (matching and matching[0].release_group is not None):
                raise OperationRefusedError("shared release admission is not installed")
            if candidate_entries and not matching:
                raise OperationRefusedError("operations release target changed")
            return
        entry = release_manifest(release, load_packaged_manifest).entry(release.target)
        if (
            contract.node.evidence_expectations != [f"runtime_verified:{release.component}"]
            or release.repository != binding.repo
            or workflow not in release.workflows
            or entry is None
            or entry.status is not EntryStatus.ENABLED
            or entry.docs_only
            or not entry.covers(release.component)
            or entry.connection_id not in contract.environment_connection_ids
            or entry.workflow is None
            or entry.workflow.path != f".github/workflows/{workflow}"
        ):
            raise OperationRefusedError("operations release target changed")
        if getattr(release, "delivery_contract", None) is not None:
            from src.orchestration.generic_delivery_dispatch import reserve_dispatch

            reservation = await reserve_dispatch(
                session,
                factory=factory,
                node=node,
                flow=flow,
                contract=contract,
                entry=entry,
                binding=binding,
                execution=execution,
                run_id=run_id,
                run_attempt=run_attempt,
                dispatch_revision=dispatch_revision,
                provider=provider,
                token=token,
            )
            await session.commit()
            return reservation
        # The reviewed manifest entry's release_group is what installs shared-release
        # admission, not the flow slug or a wave name: the adapter release story owns
        # its own accepted node for the same grouped target, and that node is refused
        # before it can dispatch while admission is keyed to one wave (#8453). Every
        # binding is still re-verified under lock by reserve_native_release: enabled
        # entry, covered component, accepted connection, allow-listed workflow, pins,
        # signed job identity and the shared release-group lease.
        if entry.release_group is not None and environment == "dev" and (initial or continuation_context is not None):
            if not isinstance(dispatch_revision, str) or not re.fullmatch(r"[0-9a-f]{40}", dispatch_revision):
                raise OperationRefusedError("shared release dispatch revision unavailable")
            from sqlalchemy import select

            from src.orchestration.environment_leases import LeaseError
            from src.orchestration.execution_policy import Action
            from src.orchestration.execution_state import ExecutionIdentity
            from src.orchestration.models import OrchestrationExecution
            from src.orchestration.operations_shared_release import reserve_native_release
            from src.orchestration.review_cycle_dispatch import ReviewCycleServices
            from src.orchestration.run_reports import OrchestrationRunReport

            correlation = execution.get("invocation_id", {}).get("S")
            row = await session.scalar(
                select(OrchestrationExecution).where(
                    OrchestrationExecution.org_id == node.org_id,
                    OrchestrationExecution.node_id == node.id,
                    OrchestrationExecution.cycle == node.attempts,
                    OrchestrationExecution.accepted_plan_version == contract.plan_version,
                )
            )
            report = await session.get(OrchestrationRunReport, correlation) if isinstance(correlation, str) else None
            if (
                row is None
                or report is None
                or report.org_id != node.org_id
                or report.node_id != node.id
                or report.run_id != correlation
                or report.repo != binding.repo
                or report.provider_repository_id != binding.provider_repository_id
                or row.org_id != node.org_id
                or row.flow_id != flow.id
                or row.claim_id is None
                or row.claim_generation is None
            ):
                raise OperationRefusedError("shared release reservation unavailable")
            identity = ExecutionIdentity(node.org_id, node.id, row.cycle, contract.plan_version, row.claim_id, row.claim_generation)
            source = SimpleNamespace(identity=identity, execution=row)
            state = SimpleNamespace(node=node, row=row, report=report, contract=contract, binding=binding, execution=execution, values=None)
            context = SimpleNamespace(identity=identity, execution=SimpleNamespace(id=row.id, revision=row.revision))
            try:
                state.values = await ReviewCycleServices(factory).authority_context(session, source, node, binding, correlation, Action.DEPLOY)
                await reserve_native_release(
                    session,
                    context=continuation_context[0] if continuation_context is not None else context,
                    state=state,
                    workflow=workflow,
                    environment="dev",
                    initial=continuation_context is None,
                    dispatch_revision=dispatch_revision,
                )
            except (KeyError, LeaseError, CycleBlockedError):
                raise OperationRefusedError("shared release reservation unavailable") from None
            await session.commit()
            return release.component
        if foundation or entry.release_group is not None:
            raise OperationRefusedError("shared release admission is not installed")


@router.post("/dispatch")
async def actions_dispatch(body: DispatchRequest, request: Request, runtime: AgentRuntime = Depends(get_agent_runtime)):
    return JSONResponse(
        await _perform(request, runtime, workflow=body.workflow, run_id=None, inputs=body.inputs), headers={"Cache-Control": "no-store"}
    )


@router.post("/rerun")
async def actions_rerun(body: RerunRequest, request: Request, runtime: AgentRuntime = Depends(get_agent_runtime)):
    return JSONResponse(await _perform(request, runtime, workflow=None, run_id=body.run_id, inputs={}), headers={"Cache-Control": "no-store"})


class RunsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int = Field(default=1, ge=1, le=5)


def _project_run(run: dict, *, repository_id: int) -> dict:
    if not isinstance(run, dict):
        raise OperationRefusedError("run response is invalid")
    run_id = run.get("id")
    repository = run.get("repository")
    if (
        not isinstance(run_id, int)
        or isinstance(run_id, bool)
        or run_id < 1
        or not isinstance(repository, dict)
        or repository.get("id") != repository_id
    ):
        raise OperationRefusedError("run is outside the assigned repository")
    return {
        "id": run_id,
        "run_attempt": run.get("run_attempt") if isinstance(run.get("run_attempt"), int) else None,
        "name": redact_text(str(run.get("name") or "")),
        "display_title": redact_text(str(run.get("display_title") or "")),
        "path": redact_text(str(run.get("path") or "")),
        "status": redact_text(str(run.get("status") or ""), max_bytes=32),
        "conclusion": redact_text(str(run.get("conclusion") or ""), max_bytes=32),
        "head_sha": redact_text(str(run.get("head_sha") or ""), max_bytes=40),
    }


# `_head_contains_pin` reads the head's history in pages of this size and gives up
# after this many pages (1000 commits): a pin further back than that is stale
# enough that re-pinning is the right answer, not a larger read.
PIN_ANCESTRY_PAGE_SIZE = 100
PIN_ANCESTRY_MAX_PAGES = 10
PIN_ANCESTRY_QUERY = """query($owner:String!,$name:String!,$head:GitObjectID!,$first:Int!,$after:String){
  repository(owner:$owner,name:$name){ object(oid:$head){ ... on Commit {
    history(first:$first, after:$after){ pageInfo{ hasNextPage endCursor } nodes{ oid } } } } } }"""


async def _head_contains_pin(provider, binding, *, pin: str, head: str, token: str) -> bool:
    """True when the run's head is the pinned revision or a descendant of it.

    GitHub only dispatches a workflow on a branch, so the run's head is whatever the
    default branch was at dispatch time. The pin stays the approved minimum; the
    workflow definition is separately required to be byte-identical at the head.
    """
    if head == pin:
        return True
    if not re.fullmatch(r"[0-9a-f]{40}", head or ""):
        return False
    # Walk the head's history through GraphQL, which returns only commit ids: the
    # REST compare payload (every file's patch, 545 KB for 85 files) and even a
    # 100-entry REST commits page (547 KB) both exceeded the provider read limit
    # once main had moved ~30 commits past the pin, failing every observation with
    # deployment_provider_response_limit. A 100-id history page is a few KB. The
    # search stops at the pin or after PIN_ANCESTRY_MAX_PAGES pages, in which case
    # the pin is not treated as an ancestor.
    owner, name = binding.repo.split("/", 1)
    after = None
    for _page in range(PIN_ANCESTRY_MAX_PAGES):
        response = await provider.request(
            binding,
            "POST",
            "/graphql",
            token=token,
            max_bytes=512 * 1024,
            json={
                "query": PIN_ANCESTRY_QUERY,
                "variables": {"owner": owner, "name": name, "head": head, "first": PIN_ANCESTRY_PAGE_SIZE, "after": after},
            },
        )
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("errors"):
            return False
        history = ((((payload.get("data") or {}).get("repository") or {}).get("object") or {}).get("history")) or {}
        oids = [item.get("oid") for item in history.get("nodes") or [] if isinstance(item, dict)]
        if not oids:
            return False
        if pin in oids:
            return True
        info = history.get("pageInfo") or {}
        if not info.get("hasNextPage") or not isinstance(info.get("endCursor"), str):
            return False
        after = info["endCursor"]
    return False


async def _evidence_transport(provider, binding, *, workflow: str, revision: str, token: str) -> str:
    """How the pinned workflow definition publishes its deployment evidence.

    Mirrors WorkflowProvider.definition(): `env.ADP_DEPLOYMENT_EVIDENCE_STORE`
    selects `s3-oidc-v1` (private bucket, OIDC-signed objects) or the legacy
    GitHub Actions artifact. The operations observer must read the same place
    the workflow writes, or a green deployment can never become evidence.
    """
    path = f"/repos/{binding.repo}/contents/.github/workflows/{quote(workflow)}"
    response = await provider.request(binding, "GET", path, token=token, max_bytes=MAX_WORKFLOW_RESPONSE_BYTES, params={"ref": revision})
    entry = response.json()
    try:
        raw = decode_workflow_content(entry)
        document = yaml.safe_load(raw)
    except (ValueError, yaml.YAMLError, UnicodeDecodeError):
        raise OperationRefusedError("workflow definition is invalid") from None
    env = document.get("env", {}) if isinstance(document, dict) else {}
    transport = env.get("ADP_DEPLOYMENT_EVIDENCE_STORE", "github-artifact") if isinstance(env, dict) else "github-artifact"
    if transport not in {"github-artifact", "s3-oidc-v1"}:
        raise OperationRefusedError("deployment evidence transport is unknown")
    return transport


_EVIDENCE_ACCOUNT: dict[str, str] = {}


def _evidence_account() -> str:
    """The platform account that owns the deployment-evidence bucket (this gateway's account)."""
    if "id" not in _EVIDENCE_ACCOUNT:
        import boto3

        from src.shared.config import get_settings

        account = boto3.client("sts", region_name=get_settings().aws_region).get_caller_identity()["Account"]
        if not isinstance(account, str) or len(account) != 12 or not account.isdigit():
            raise OperationRefusedError("evidence account is unavailable")
        _EVIDENCE_ACCOUNT["id"] = account
    return _EVIDENCE_ACCOUNT["id"]


async def _s3_context(provider, binding, raw, *, workflow: str, revision: str, environment: str):
    """Read and bind the context document the workflow published to S3 for this exact run."""
    from src.orchestration.deployment_workflow_provider import WorkflowContext
    from src.shared.config import get_settings

    result = await provider.s3_evidence(
        account=_evidence_account(),
        environment=environment,
        region=get_settings().aws_region,
        repository=binding.repo,
        repository_id=binding.provider_repository_id,
        run_id=int(raw["id"]),
        attempt=int(raw["run_attempt"]),
        kind="context",
        name=workflow,
        workflow_path=raw["path"],
        workflow_revision=revision,
    )
    if result is None:
        return None
    payload, digest, reference = result
    return WorkflowContext.model_validate_json(payload), digest, reference


async def _capture_correlated_run(request, runtime, *, execution, binding, raw, provider, authority=None, runtime_authority=None):
    async def authorize():
        return await authority() if authority is not None else await _authority(request, runtime, mutate=True)

    if execution.get("persona", {}).get("S") not in {"operations", "agent-codex-operations"} or raw.get("status") != "completed":
        return
    failed = raw.get("conclusion") in TERMINAL_WORKFLOW_FAILURES
    if raw.get("conclusion") != "success" and not failed:
        return
    try:
        issued, current_binding, current_execution = await authorize()
    except OperationRefusedError:
        if failed:
            return  # A failed run remains readable while mutation authority is unavailable.
        raise
    if failed and getattr(issued, "delivery_contract", None) is None:
        return
    pins = {key: execution.get(key, {}).get("S") for key in ("source_revision", "definition_revision", "default_branch")}
    workflow = str(raw.get("path", "")).removeprefix(".github/workflows/")
    if (
        current_binding != binding
        or current_execution != execution
        or workflow not in issued.workflow_allowlist
        or raw.get("path") != ".github/workflows/" + workflow
        or raw.get("event") not in {"push", "workflow_dispatch"}
        or raw.get("head_branch") != pins["default_branch"]
        or (raw.get("event") == "workflow_dispatch" and raw.get("display_title") != "ADP deployment " + execution["invocation_id"]["S"])
        or (raw.get("repository") or {}).get("id") != binding.provider_repository_id
        or not isinstance(raw.get("head_sha"), str)
        or type(raw.get("run_attempt")) is not int
        or raw["run_attempt"] < 1
    ):
        return
    if raw["event"] == "push" and (getattr(issued, "product_cases", "none") != "none" or getattr(issued, "delivery_contract", None) is not None):
        # The worker must be able to finish observing an automatic rollout before
        # dispatching its selected Product cases. A push cannot supply those
        # cases, but that makes it ineligible as evidence, not unreadable.
        return {
            "workflow_evidence_id": None,
            "observation_note": "Product acceptance requires a correlated workflow_dispatch run of the accepted workflow.",
        }
    head = raw["head_sha"]
    if raw["event"] == "push" and pins["source_revision"] == pins["definition_revision"]:
        from src.orchestration.operations_workflow_evidence import current_push_head

        if await current_push_head(provider, binding, pins["default_branch"]) != pins["source_revision"]:
            return
    token = await installation_token(
        org_id=binding.org_id,
        installation_id=binding.installation_id,
        repository=binding.repo,
        permissions={"actions": "read", "contents": "read", "metadata": "read"},
    )
    if failed:
        from src.orchestration.generic_delivery_recovery import reconcile_failed_dispatches
        from src.orchestration.validation_diagnostics import record_runtime_validation

        reconciled = False
        try:
            async with get_session_factory()() as session:
                reconciled = bool(await reconcile_failed_dispatches(session, binding=binding, node_id=issued.node_id, provider=provider, token=token))
                await session.commit()
            code = "delivery_workflow_failed" if reconciled else "delivery_dispatch_reconciliation_required"
        except CycleBlockedError as exc:
            code = safe_code(exc.reason)
        await record_runtime_validation(get_session_factory(), binding=binding, issued=issued, code=code)
        return {"workflow_evidence_id": None, "runtime_evidence_id": None, "runtime_hold_reason": code, "dispatch_reconciled": reconciled}
    # The run executed at the branch head of its moment. It counts for the pinned
    # definition when that head contains the pin and the workflow file is
    # byte-identical at both (#7662) — the same rule the deployment controller and
    # the dispatch guard already apply.
    if not await _head_contains_pin(provider, binding, pin=pins["definition_revision"], head=head, token=token):
        return
    defaults = await _definition(
        provider,
        binding,
        workflow=workflow,
        revision=pins["definition_revision"],
        branch=pins["default_branch"],
        token=token,
        observing_push=raw.get("event") == "push",
        head=head,
        # A successful, correlated run is observed after operator deployment.
        # Approval inputs still refuse every worker dispatch/manual rerun.
        for_dispatch=False,
        generic_delivery=getattr(issued, "delivery_contract", None) is not None,
    )
    transport = await _evidence_transport(provider, binding, workflow=workflow, revision=pins["definition_revision"], token=token)
    reference = None
    if transport == "s3-oidc-v1":
        observed = await _s3_context(provider, binding, raw, workflow=workflow, revision=head, environment=defaults.get("environment") or "dev")
        if observed is None:
            return
        context, digest, reference = observed
        artifact_id = None
    else:
        observed = await provider.context(binding, raw, raw["path"])
        if observed is None:
            return
        context, artifact_id, digest = observed
    if (
        (artifact_id is None and not reference)
        or context.repository_id != binding.provider_repository_id
        or context.workflow_path != raw["path"]
        or context.run_id != raw["id"]
        or context.run_attempt != raw["run_attempt"]
        or (raw.get("event") == "workflow_dispatch" and context.correlation != execution["invocation_id"]["S"])
        or (raw.get("event") == "push" and (context.correlation == execution["invocation_id"]["S"] or context.inputs.get("adp_correlation")))
        or context.source_revision != pins["source_revision"]
        or context.workflow_revision != head
        or (
            (getattr(issued, "product_cases", "none") != "none" or getattr(issued, "proof_phase", "none") != "none")
            and raw.get("event") != "workflow_dispatch"
        )
        or not _inputs_match_defaults(
            context.inputs,
            defaults,
            product_cases=getattr(issued, "product_cases", "none"),
            proof_phase=getattr(issued, "proof_phase", "none"),
            source_revision=pins["source_revision"],
            accepted_inputs=issued.delivery_contract.inputs if getattr(issued, "delivery_contract", None) is not None else None,
            adapter_stage=(
                raw.get("event") == "workflow_dispatch"
                and workflow == "tools-platform-deploy.yml"
                and execution.get("persona", {}).get("S") in {"operations", "agent-codex-operations"}
                and context.inputs.get("stage") == "tool-adapters"
            ),
            foundation_gateway=(
                raw.get("event") == "workflow_dispatch"
                and workflow == "gateway-deploy.yml"
                and execution.get("persona", {}).get("S") in {"operations", "agent-codex-operations"}
                and context.inputs.get("release_group") == "atlassian-foundation"
            ),
        )
    ):
        raise OperationRefusedError("correlated context identity is unavailable")
    from src.orchestration.operations_workflow_evidence import record_correlated_workflow

    again, again_binding, again_execution = await authorize()
    if again != issued or again_binding != binding or again_execution != execution:
        raise OperationRefusedError("operations authority changed")
    async with get_session_factory()() as session:
        receipt_id = await record_correlated_workflow(
            session,
            execution=execution,
            binding=binding,
            issued=issued,
            raw_run=raw,
            context=context,
            artifact_id=artifact_id,
            digest=digest,
            reference=reference,
        )
        await session.commit()
    result = {"workflow_evidence_id": receipt_id}
    if receipt_id is not None:
        from src.orchestration.operations_runtime_evidence import verify_operations_runtime

        async def reauthorize():
            current, current_binding, current_execution = await authorize()
            if current != issued or current_binding != binding or current_execution != execution:
                raise OperationRefusedError("operations authority changed during runtime verification")

        try:
            controller = {"controller_authority": runtime_authority} if runtime_authority is not None else {}
            result["runtime_evidence_id"] = await verify_operations_runtime(
                get_session_factory(),
                binding=binding,
                issued=issued,
                execution=execution,
                raw_run=raw,
                context_action_id=receipt_id,
                reauthorize=reauthorize,
                **controller,
            )
        except CycleBlockedError as exc:
            accepted_reasons = RUNTIME_HOLD_REASONS
            result["runtime_hold_reason"] = (
                safe_code(exc.reason)
                if getattr(issued, "delivery_contract", None) is not None
                else exc.reason
                if exc.reason in accepted_reasons
                else "runtime_verification_unavailable"
            )
            # The worker only sees the hold reason inside a tool result; without this
            # line an operator diagnosing "exited without verified runtime evidence"
            # has no server-side trace of why runtime verification refused.
            logger.warning(
                "Operations runtime verification held",
                extra={"reason": exc.reason, "run_id": raw.get("id"), "node_id": issued.node_id, "workflow_evidence_id": receipt_id},
            )
    return result


async def _observe(
    request: Request, runtime: AgentRuntime, *, run_id: int | None = None, page: int = 1, logs: bool = False, tail_bytes: int | None = None
):
    try:
        issued, binding, execution = await _authority(request, runtime, mutate=False)
        token = await installation_token(
            org_id=binding.org_id,
            installation_id=binding.installation_id,
            repository=binding.repo,
            permissions=issued.permissions,
        )
        provider = WorkflowProvider()
        if run_id is None:
            # Scope the listing to the allow-listed workflows on the deployment
            # branch: a repository-wide page of full run objects exceeded the
            # provider byte ceiling on busy repositories, so every operations run
            # saw "provider unavailable" and could never observe its own deploy.
            workflows = sorted(getattr(issued, "workflow_allowlist", ()) or ())[:8]
            branch = execution.get("default_branch", {}).get("S", "") if isinstance(execution, dict) else ""
            scoped = {"per_page": 10, "page": page, **({"branch": branch} if branch else {})}
            listings = (
                [(f"/repos/{binding.repo}/actions/workflows/{quote(workflow)}/runs", scoped) for workflow in workflows]
                if workflows
                else [(f"/repos/{binding.repo}/actions/runs", {"per_page": 20, "page": page})]
            )
            collected = []
            for path, params in listings:
                response = await provider.request(binding, "GET", path, token=token, max_bytes=512 * 1024, params=params)
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get("workflow_runs"), list):
                    raise OperationRefusedError("runs listing is invalid")
                collected.extend(_project_run(run, repository_id=binding.provider_repository_id) for run in data["workflow_runs"][:20])
            collected.sort(key=lambda run: run["id"], reverse=True)
            return {"runs": collected[:20], "page": page}
        response = await provider.request(binding, "GET", f"/repos/{binding.repo}/actions/runs/{run_id}", token=token, max_bytes=32 * 1024)
        raw = response.json()
        run = _project_run(raw, repository_id=binding.provider_repository_id)
        if not logs:
            observed = await _capture_correlated_run(request, runtime, execution=execution, binding=binding, raw=raw, provider=provider)
            return {"run": run, **(observed or {"workflow_evidence_id": None})}

        async def reauthorize():
            current, current_binding, _current_execution = await _authority(request, runtime, mutate=False)
            if current != issued or current_binding != binding:
                raise OperationRefusedError("Actions read authority changed")

        tail, truncated = await fetch_log_tail(
            provider, binding, run_id=run_id, token=token, reauthorize=reauthorize, failure_focus=tail_bytes is None
        )
        if tail_bytes is not None and len(tail.encode("utf-8")) > tail_bytes:
            # The worker's tool surface shows far less than the 64 KiB bound; let it
            # ask for the end of the log, where the failing step is (#7828).
            tail, truncated = tail.encode("utf-8")[-tail_bytes:].decode("utf-8", errors="ignore"), True
        return {"run_id": run_id, "log_tail": tail, "truncated": truncated}
    except (BootstrapRefusedError, WorkloadRefusedError, CredentialError, ExecutionStateError, OperationRefusedError) as exc:
        logger.info("Actions read refused", extra={"reason": type(exc).__name__, "detail": str(exc)[:200]})
        raise HTTPException(404, "Actions read is not authorized or available") from None
    except AuthorityStoreError:
        raise HTTPException(503, "agent authority unavailable") from None
    except (httpx.HTTPError, CycleBlockedError) as exc:
        # The worker only sees a 503; without this line an operator cannot tell a
        # GitHub outage from a cycle refusal when an operations run dies on it.
        logger.warning("Actions read provider unavailable", extra={"reason": type(exc).__name__, "detail": str(exc)[:200]})
        raise HTTPException(503, "Actions provider unavailable") from None


@router.post("/runs")
async def actions_runs(body: RunsRequest, request: Request, runtime: AgentRuntime = Depends(get_agent_runtime)):
    return JSONResponse(await _observe(request, runtime, page=body.page), headers={"Cache-Control": "no-store"})


@router.post("/run")
async def actions_run(body: RerunRequest, request: Request, runtime: AgentRuntime = Depends(get_agent_runtime)):
    return JSONResponse(await _observe(request, runtime, run_id=body.run_id), headers={"Cache-Control": "no-store"})


@router.post("/logs")
async def actions_logs(body: LogsRequest, request: Request, runtime: AgentRuntime = Depends(get_agent_runtime)):
    return JSONResponse(
        await _observe(request, runtime, run_id=body.run_id, logs=True, tail_bytes=body.tail_bytes), headers={"Cache-Control": "no-store"}
    )
