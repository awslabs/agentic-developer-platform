"""Installed DevOps investigation and release-readiness worker assembly."""

from l5_services.adapters.devops.investigations import InvestigationAdapter
from l5_services.adapters.devops.release_readiness import ReleaseReadinessAdapter
from l5_services.publication.installed import installed_publication_client
from l5_services.workers.execution import AuthorityRevoked, ExecutionService


class DevopsOperations:
    def __init__(self):
        self.adapters = {"investigation": InvestigationAdapter(), "release_readiness": ReleaseReadinessAdapter()}

    def selected(self, run):
        if run.service_id != "devops" or run.input.get("capability") not in self.adapters:
            raise AuthorityRevoked("DevOps worker does not own this installed capability")
        return self.adapters[run.input["capability"]]

    async def submit(self, run, manifest, attempt, provider_session):
        return await self.selected(run).submit(run, manifest, attempt, provider_session)

    async def reconcile_submit(self, run, attempt, provider_session):
        return await self.selected(run).reconcile_submit(run, attempt, provider_session)

    async def observe(self, run, native_job_ref, attempt, provider_session):
        return await self.selected(run).observe(run, native_job_ref, attempt, provider_session)

    async def collect(self, run, native_job_ref, attempt, provider_session):
        return await self.selected(run).collect(run, native_job_ref, attempt, provider_session)

    async def cancel(self, run, native_job_ref, attempt, provider_session):
        return await self.selected(run).cancel(run, native_job_ref, attempt, provider_session)


def factory():
    from l5_services import runtime_support
    from l5_services.adapters.devops.binding import DevopsBindingAdapter

    if runtime_support.required("L5_SERVICE_ID") != "devops":
        raise ValueError("installed DevOps factory does not own the selected service")
    sessions = runtime_support.sessions()
    transport = runtime_support.gateway_transport()
    sources = runtime_support.ScopedSources(transport)
    archive = runtime_support.archive_store("devops")
    publish = runtime_support.queue_publisher("devops")
    adapter = DevopsOperations()

    async def authorize(run, claim, operation):
        adapter.selected(run)
        if operation == "materialize" and run.input.get("sources"):
            await runtime_support.verify_source_receipts(run, claim, sources, "devops")
            return None
        return await runtime_support.run_provider_session(
            run, claim, transport, sources, runtime_support.provider_session,
            service_id="devops", allowed_clients={"devops-agent"})

    async def binding_authorize(intent, claim):
        if intent.authorization.get("purpose") == "devops_resource_verification":
            verified, identity, provider, deadline = await runtime_support.setup_provider_session(
                intent, claim, transport, sessions, runtime_support.provider_session,
                service_id="devops", client_name="devops-agent", allowed_clients={"devops-agent", "iam", "lambda", "logs"})
            return verified, DevopsBindingAdapter(provider.client("devops-agent"), identity, intent,
                                                  sessions=sessions, resource_session=provider, resource_expiry=deadline)
        verified, identity, client = await runtime_support.setup_provider_session(
            intent, claim, transport, sessions, runtime_support.provider_session,
            service_id="devops", client_name="devops-agent")
        return verified, DevopsBindingAdapter(client, identity, intent)

    from l5_services.devops.knowledge_runtime import knowledge_worker

    async def execute_knowledge(intent, claim):
        worker = await knowledge_worker(sessions, sources, transport, runtime_support.provider_session, intent, claim)
        return await worker.execute(intent, claim)

    return ExecutionService(sessions, sources, adapter, publish, authorize,
                            archive_store=archive, service_id="devops", binding_authorize=binding_authorize,
                            knowledge_execute=execute_knowledge,
                            devops_context_client=installed_publication_client(
                                service_kind="devops", destination="product-context", transport=transport, sessions=sessions),
                            devops_insights_client=installed_publication_client(
                                service_kind="devops", destination="engineering-insights", transport=transport, sessions=sessions))
