"""Expected verifier for each mounted internal route without a transport dependency."""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass

from fastapi.routing import APIRoute


@dataclass(frozen=True)
class RouteAuthority:
    verifier: str
    tenant_boundary: str


DIRECT_VERIFIERS = {
    ("src.internal.ingestion_refresh_routes", "verify_refresh_identity"): "registered_ingestion_scheduler",
    ("src.internal.auth_deps", "verify_internal_or_irsa"): "registered_iam",
    ("src.agentauth.routes", "require_agent_transport"): "authenticated_agent_run",
    ("src.internal.persona_model_probe_routes", "verify_model_probe_irsa"): "registered_probe_worker",
    ("src.crew.checkpoint_routes", "helper_identity"): "verified_checkpoint_helper",
    ("src.crew.checkpoint_routes", "restore_identity"): "verified_checkpoint_restore",
    ("src.products.runtime_proof_routes", "require_runtime_proof_role"): "scoped_runtime_observer",
}


INLINE_VERIFIERS = {
    "/internal/v1/l5/contribution-authority": ("src.internal.l5_consumer_routes.contribution_authority", "verify"),
    "/internal/v1/knowledge-assets/ingestion-control": ("src.internal.ingestion_control_routes.ingestion_control", "authorize_ingestion"),
    "/internal/v1/tools/knowledge/mcp": ("src.internal.tools_knowledge.knowledge_mcp", "require_transport"),
    "/internal/v1/tools/github/mcp": ("src.internal.tools_github.github_mcp", "require_transport"),
    "/internal/v1/tools/jira/mcp": ("src.internal.tools_jira.jira_mcp", "require_transport"),
    "/internal/v1/tools/slack/mcp": ("src.internal.tools_slack.slack_mcp", "require_transport"),
    "/internal/v1/knowledge-assets/status-callback": ("src.internal.status_callback_routes.status_callback", "verify_ingestion_grant"),
    "/internal/v1/agent/persona-model/resolve": ("src.internal.persona_model_selection.resolve_dispatch_selection", "verify_producer"),
    "/internal/v1/agent/arc/model-decision": ("src.agentauth.arc_model.arc_model_decision", "verify_producer"),
    "/internal/v1/agent/model-policy-keys": ("src.agentauth.model_policy_keys.model_policy_keys", "_signing_key"),
    "/internal/v1/agent/legacy-chat-preflight": ("src.agentauth.model_policy_keys.legacy_chat_preflight", "extract_iam_identity_from_headers"),
    "/internal/v1/agent/roots/admit": ("src.agentauth.external_roots.admit_root", "verify_producer"),
    "/internal/v1/agent/chat/model-decision": ("src.agentauth.chat_model.chat_model_decision", "verify"),
    "/internal/v1/agent/work/admit": ("src.agentauth.work_routes.admit_work", "verify_producer"),
    "/internal/v1/tasks/admit": ("src.agentauth.task_admission_routes.admit", "verify_admission_binding"),
    "/internal/v1/agent/arc/cyber/jobs": ("src.agentauth.cyber_jobs.create_job", "caller"),
    "/internal/v1/agent/arc/cyber/result": ("src.agentauth.cyber_jobs.result", "caller"),
    "/internal/v1/agent/chat/data/exit": ("src.agentauth.chat_data_routes.sandbox_exit", "observe_teardown"),
    "/internal/v1/agent/chat/data/teardown": ("src.agentauth.chat_data_routes.sandbox_teardown", "observe_teardown"),
    "/internal/v1/agent/chat/data/finalize": ("src.agentauth.chat_data_routes.finalize_chat", "observe_teardown"),
    "/internal/v1/agent/chat/data/session/commit": ("src.agentauth.chat_data_routes.commit_persistent_turn", "observe_teardown"),
    "/internal/v1/agent/chat/data/complete": ("src.agentauth.chat_data_routes.complete_chat", "observe_teardown"),
}

for route_name, handler in {
    "crew-run": "mint_crew_run",
    "crew-run/lease": "provision_crew_lease",
    "crew-run/activate": "activate_crew_run",
    "crew-run/ready": "ready_crew_run",
    "crew-run/{run_id}": "revoke_crew_run",
    "data/admit": "admit_chat",
    "data/resume": "resume_chat",
    "data/reserve": "reserve_chat",
}.items():
    INLINE_VERIFIERS[f"/internal/v1/agent/chat/{route_name}"] = (f"src.agentauth.chat_data_routes.{handler}", "verify_producer")

for route_name, handler in {
    "dispatch/claim": "dispatch_claim",
    "dispatch/settle": "dispatch_settle",
    "recovery/claim": "recovery_claim",
    "recovery/settle": "recovery_settle",
}.items():
    INLINE_VERIFIERS[f"/internal/v1/tasks/{route_name}"] = (f"src.agentauth.task_dispatch_routes.{handler}", "_authenticate")


def _called_verifiers(endpoint) -> set[str]:
    source = textwrap.dedent(inspect.getsource(inspect.unwrap(endpoint)))
    calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)]
    direct = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr for node in calls if isinstance(node.func, ast.Name | ast.Attribute)
    }
    threaded = {
        node.args[0].attr
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "run_in_threadpool" and node.args and isinstance(node.args[0], ast.Attribute)
    }
    return direct | threaded


def classify_internal_route(route: APIRoute) -> RouteAuthority:
    from src.internal.vault_evidence_routes import EvidenceBody, RevocationBody

    if not route.path.startswith("/internal/"):
        raise AssertionError(f"not an internal route: {route.path}")
    endpoint = f"{route.endpoint.__module__}.{route.endpoint.__name__}"
    dependencies = {
        (getattr(dependency.call, "__module__", ""), getattr(dependency.call, "__name__", "")) for dependency in route.dependant.dependencies
    }
    named = dependencies.intersection(DIRECT_VERIFIERS)
    if route.path in INLINE_VERIFIERS:
        expected_endpoint, verifier = INLINE_VERIFIERS[route.path]
        expected_method = "GET" if verifier == "_signing_key" else "DELETE" if route.path.endswith("crew-run/{run_id}") else "POST"
        if route.methods != {expected_method}:
            raise AssertionError(f"internal method changed: {route.path}")
        if endpoint != expected_endpoint:
            raise AssertionError(f"internal verifier changed: {route.path}")
        if verifier not in _called_verifiers(route.endpoint):
            raise AssertionError(f"internal verifier missing: {route.path} ({verifier})")
        if route.path == "/internal/v1/l5/contribution-authority" and "authorize_assertion" not in _called_verifiers(route.endpoint):
            raise AssertionError(f"current contribution authority missing: {route.path}")
        return RouteAuthority(verifier, "no_tenant_data" if verifier == "_signing_key" else "route_specific")
    if not named:
        raise AssertionError(f"internal route without named verifier: {route.path} ({endpoint})")
    if len(named) != 1:
        raise AssertionError(f"ambiguous internal verifier: {route.path}")
    if any(parameter.field_info.annotation in (EvidenceBody, RevocationBody) for parameter in route.dependant.body_params):
        if route.path not in {
            "/internal/v1/credential-evidence",
            "/internal/v1/credential-revocation-state",
        } or "authorize_vault_metadata" not in _called_verifiers(route.endpoint):
            raise AssertionError(f"vault metadata authority missing: {route.path}")
        return RouteAuthority(DIRECT_VERIFIERS[named.pop()], "verified_vault_metadata")
    return RouteAuthority(DIRECT_VERIFIERS[named.pop()], "route_specific")
