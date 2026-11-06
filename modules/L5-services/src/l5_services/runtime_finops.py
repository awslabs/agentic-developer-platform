"""FinOps worker resources are mounted by the private service release."""

import os
from datetime import UTC, datetime
from types import SimpleNamespace

import boto3

from l5_services.adapters.finops.billing import CostExplorerAdapter
from l5_services.adapters.finops.binding import FinOpsBindingProvider
from l5_services.adapters.finops.inquiry import FinOpsInquiryAdapter
from l5_services.adapters.finops.private_bridge import PrivateFinOpsBridge
from l5_services.workers.execution import AuthorityRevoked, ExecutionService
from l5_services.publication.installed import installed_publication_client


def _private_executor():
    resources = _resources()
    region = resources.required("AWS_REGION")
    function_arn = resources.required("L5_FINOPS_EXECUTOR_ARN")
    return PrivateFinOpsBridge(boto3.client("lambda", region_name=region), function_arn, region)


def _resources():
    from l5_services import runtime_support

    return runtime_support


def _independent_billing():
    resources = _resources()
    return CostExplorerAdapter(resources.required("L5_ARCHIVE_BUCKET"),
                               boto3.client("s3", region_name=resources.required("AWS_REGION")),
                               resources.provider_session)


def _adapter():
    return FinOpsInquiryAdapter(_private_executor(), _independent_billing())


def factory():
    if os.environ.get("L5_SERVICE_ID") != "finops":
        raise ValueError("installed FinOps service identity does not match")
    if any(os.environ.get(name) for name in (
            "L5_FINOPS_ALLOW_UNQUALIFIED", "L5_FINOPS_ALLOW_FOREIGN_SCOPE",
            "L5_FINOPS_ALLOW_DUPLICATE_EFFECTS")):
        raise ValueError("FinOps worker cannot accept authority overrides")
    resources = _resources()
    transport = resources.gateway_transport()
    sessions = resources.sessions()
    archive = resources.archive_store("finops")
    publish = resources.queue_publisher("finops")
    adapter = _adapter()
    if not all(callable(getattr(adapter, method, None)) for method in (
            "submit", "reconcile_submit", "observe", "collect", "cancel",
            "collect_billing_page", "read_billing_page", "read_native_cost")):
        raise RuntimeError("FinOps executor callbacks unavailable")

    async def authorize(run, claim, operation):
        admission = run.admission
        scope = admission.get("scope") or {}
        agent = admission.get("native_agent_ref") or {}
        billing = admission.get("billing_scope") or {}
        if (run.service_id != "finops" or run.input.get("capability") != "cost_inquiry"
                or set(run.input) & {"allow_unqualified", "allow_foreign_scope", "allow_duplicate_effects"}
                or any(scope.get(field) != getattr(run, field) for field in (
                    "tenant_id", "product_id", "project_id"))
                or (agent.get("tenant_id"), agent.get("project_id")) != (
                    run.tenant_id, run.project_id)
                or agent.get("record_type") != "finops_agent"
                or not agent.get("record_id")
                or billing.get("scope_generation") != run.binding_generation
                or not billing.get("scope_digest")
                or not isinstance(admission.get("delegation_id"), str)
                or not admission.get("delegation_version")
                or not isinstance(admission.get("connection_ref"), dict)
                or agent.get("source_connection_id") != admission["connection_ref"].get("source_connection_id")):
            raise AuthorityRevoked("FinOps work scope unavailable")
        selected = {**admission["connection_ref"], "record_type": "credential"}
        request = {"delegation_id": admission["delegation_id"],
                   "delegation_revision": admission["delegation_version"],
                   "operation_id": run.run_id, "attempt_id": claim.work_id,
                   "lease_owner": claim.owner, "lease_fence": claim.fence,
                   "connection_ref": selected}
        response = await transport.post("/internal/v1/l5/credential-session", json=request)
        if response.status_code in {401, 403, 404, 409, 410}:
            raise AuthorityRevoked("current FinOps work unavailable")
        response.raise_for_status()
        result = response.json()
        identity = result.get("provider_identity") or {}
        expected = admission.get("provider_identity") or {}
        if (result.get("operation_id") != run.run_id or result.get("attempt_id") != claim.work_id
                or result.get("delegation_revision") != admission["delegation_version"]
                or any(not identity.get(field) or identity.get(field) != expected.get(field)
                       for field in ("account_id", "role_arn", "region"))):
            raise AuthorityRevoked("FinOps broker returned another work or provider identity")
        try:
            deadline = datetime.fromisoformat(result["expires_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise AuthorityRevoked("FinOps broker session expiry unavailable") from error
        if deadline.tzinfo is None or deadline <= datetime.now(UTC):
            raise AuthorityRevoked("FinOps broker session expired")
        return {**result, "provider_identity": expected, "lease_fence": claim.fence, "lease_owner": claim.owner,
                "tenant_id": run.tenant_id, "product_id": run.product_id,
                "project_id": run.project_id, "run_revision": run.revision,
                "work_generation": claim.generation, "work_kind": claim.kind,
                "delegation_id": admission["delegation_id"],
                "connection_ref": selected}

    async def authorize_setup(intent, claim):
        if intent.service_id != "finops" or intent.request.get("action") != "link":
            raise AuthorityRevoked("FinOps setup supports read-only adoption")
        broker = {}

        def executor_session(result, region):
            broker.update(result)
            return SimpleNamespace(client=lambda name, **kwargs: boto3.client(name, region_name=resources.required("AWS_REGION")))

        verified, identity, client = await resources.setup_provider_session(
            intent, claim, transport, sessions, executor_session, service_id="finops", client_name="lambda")
        _, _, billing = await resources.setup_provider_session(
            intent, claim, transport, sessions, resources.provider_session, service_id="finops", client_name="billing")
        _, _, iam = await resources.setup_provider_session(
            intent, claim, transport, sessions, resources.provider_session, service_id="finops", client_name="iam")
        authority = {**broker, "provider_identity": {field: identity[field] for field in ("account_id", "role_arn", "region")},
                     "lease_fence": claim.fence, "lease_owner": claim.owner,
                     "tenant_id": intent.tenant_id, "product_id": intent.product_id, "project_id": intent.project_id,
                     "work_generation": claim.generation, "binding_generation": intent.binding_generation,
                     "work_kind": "setup", "delegation_id": intent.authorization["delegation_id"],
                     "connection_ref": {**verified["connection_ref"], "record_type": "credential"}}
        bridge = PrivateFinOpsBridge(client, resources.required("L5_FINOPS_EXECUTOR_ARN"), resources.required("AWS_REGION"))
        return verified, FinOpsBindingProvider(bridge, authority, billing, iam, archive)

    return ExecutionService(sessions, resources.ScopedSources(transport), None, publish,
                            authorize, archive_store=archive, binding_authorize=authorize_setup,
                            finops_context_client=installed_publication_client(service_kind="finops",
                                destination="product-context", transport=transport, sessions=sessions),
                            finops_insights_client=installed_publication_client(service_kind="finops",
                                destination="engineering-insights", transport=transport, sessions=sessions),
                            finops_adapter=adapter, service_id="finops")
