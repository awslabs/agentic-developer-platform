"""Transform worker assembly using the shared installed-resource interface."""

import os
from functools import partial

import boto3
import httpx
from fastapi import HTTPException

from l5_services.adapters.transform.artifacts import TransformArtifacts
from l5_services.adapters.transform.authority import TransformWorkAuthorizer
from l5_services.adapters.transform.check_control import TransformCheckControl
from l5_services.adapters.transform.connectivity import probe_connectivity
from l5_services.adapters.transform.execution import TransformAdapter
from l5_services.adapters.transform.executor import TransformExecutor
from l5_services.adapters.transform.kubernetes import TransformKubernetesAPI
from l5_services.adapters.transform.pod_control import TransformPodControl
from l5_services.adapters.transform.provider_endpoint import TransformProviderEndpoint
from l5_services.adapters.transform.source import ProductSealedSource
from l5_services.workers.execution import ExecutionService
from l5_services.publication.installed import installed_publication_client


def factory():
    from l5_services import runtime_support as resources

    transport = resources.gateway_transport()
    sessions = resources.sessions()
    archive = resources.archive_store("transform")
    publish = resources.queue_publisher("transform")
    sources = resources.ScopedSources(transport)
    source = ProductSealedSource(sources)
    api = TransformKubernetesAPI(endpoint=resources.required("L5_TRANSFORM_CLUSTER_ENDPOINT"),
        cluster=resources.required("L5_TRANSFORM_CLUSTER_NAME"), ca=resources.required("L5_TRANSFORM_CLUSTER_CA"),
        region=resources.required("AWS_REGION"))
    artifacts = TransformArtifacts(boto3.client("s3", region_name=resources.required("AWS_REGION")),
                                   resources.required("L5_TRANSFORM_ARTIFACT_BUCKET"))
    identity = {"node_name": resources.required("L5_TRANSFORM_NODE_NAME"),
                "node_uid": resources.required("L5_TRANSFORM_NODE_UID")}
    provider_image = resources.required("L5_TRANSFORM_PROVIDER_IMAGE")
    endpoint_ref = os.environ.get("L5_TRANSFORM_PROVIDER_ENDPOINT_REF")
    endpoint = (TransformProviderEndpoint.from_installed(endpoint_ref,
        region=resources.required("AWS_REGION"), provider_image=provider_image) if endpoint_ref is not None else None)
    pods = TransformPodControl(api, namespace=resources.required("L5_TRANSFORM_PROVIDER_NAMESPACE"),
        provider_image=provider_image, provider_endpoint=endpoint, source=source, artifacts=artifacts, **identity)
    checks = TransformCheckControl(api, artifacts, namespace=resources.required("L5_TRANSFORM_CHECK_NAMESPACE"),
                                   image=resources.required("L5_TRANSFORM_CHECK_IMAGE"), **identity)
    executor = TransformExecutor(sessions, transport, pods, checks, source, artifacts)
    adapter = TransformAdapter(executor, detached_runner=executor, retention_store=artifacts,
                               require_original_patch=True)
    work_authority = TransformWorkAuthorizer(transport)

    async def authorize(run, claim, operation):
        await resources.verify_source_receipts(run, claim, sources, "transform")
        return await work_authority(run, claim, operation)

    async def admit_event(receipt):
        if receipt.service_id != "transform" or receipt.event_kind != "schedule":
            raise ValueError("Transform schedule receipt required")
        try:
            response = await transport.post("/internal/v1/l5/transform/schedule-admissions", json={
                field: getattr(receipt, field) for field in
                ("tenant_id", "product_id", "project_id", "trigger_id", "event_id")})
        except httpx.HTTPError as error:
            raise HTTPException(503, detail={"code": "schedule_gateway_unavailable"}) from error
        if response.status_code != 200:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            raise HTTPException(response.status_code, detail=detail if isinstance(detail, dict)
                                else {"code": "schedule_admission_unavailable"})
        value = response.json()
        if (value.get("event_id") != receipt.event_id or value.get("state") != "admitted"
                or not isinstance(value.get("run_id"), str) or not value["run_id"]):
            raise HTTPException(503, detail={"code": "schedule_admission_unavailable"})
        return value

    service = ExecutionService(sessions, sources, adapter, publish, authorize, archive_store=archive,
                            transform_context_client=installed_publication_client(service_kind="transform",
                                destination="product-context", transport=transport, sessions=sessions),
                            transform_insights_client=installed_publication_client(service_kind="transform",
                                destination="engineering-insights", transport=transport, sessions=sessions),
                            transform_adapter=adapter, service_id="transform", admit_event=admit_event)
    service.runtime_connectivity_probe = partial(probe_connectivity, api, **identity,
        provider_namespace=pods.namespace, check_namespace=checks.namespace)
    return service
