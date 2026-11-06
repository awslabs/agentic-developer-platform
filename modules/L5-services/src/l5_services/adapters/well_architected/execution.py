"""Assemble the scoped Well-Architected worker from installed dependencies."""

from l5_services.adapters.well_architected.generation import GenerationAdapter
from l5_services.adapters.well_architected.safety import verify_start_scope
from l5_services.workers.execution import ExecutionService


def build_execution(sessions, source_client, archive_store,
                    publish, authorize, binding_authorize, verifier, *,
                    context_client=None, insights_client=None, clock=None, admit_event=None):
    required = {"sessions": sessions, "publish": publish, "authorize": authorize,
                "binding_authorize": binding_authorize, "verifier": verifier}
    if any(not callable(value) for value in required.values()):
        raise ValueError("Well-Architected worker requires durable and scoped callbacks")
    for name, dependency, methods in (
            ("Product sources", source_client, ("read_source", "read_source_manifest", "stream_source")),
            ("archive", archive_store, ("put_archive", "get_archive"))):
        if any(not callable(getattr(dependency, method, None)) for method in methods):
            raise ValueError(f"Well-Architected worker requires {name} readback")
    for name, dependency in (("Context publication", context_client), ("Insights publication", insights_client)):
        if dependency is not None and any(not callable(getattr(dependency, method, None))
                                          for method in ("publish", "read_receipt", "invalidate")):
            raise ValueError(f"Well-Architected worker requires {name} readback")

    async def preflight(run, claim, session):
        return await verify_start_scope(sessions, source_client, archive_store, run, claim, session,
                                        now=clock() if clock is not None else None)

    return ExecutionService(
        sessions, source_client, None, publish, authorize, archive_store=archive_store,
        binding_authorize=binding_authorize, well_architected_adapter=GenerationAdapter(None, observation_sessions=sessions),
        well_architected_verifier=verifier, well_architected_preflight=preflight,
        well_architected_context_client=context_client,
        well_architected_insights_client=insights_client, service_id="well_architected", clock=clock,
        admit_event=admit_event)
