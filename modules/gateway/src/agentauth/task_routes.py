"""TokenReview-bound task delivery before a run has bootstrapped credentials."""

import os

import boto3
from botocore.config import Config
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from src.agentauth.routes import AgentRuntime, require_agent_transport
from src.agentauth.run_services import OwnRunRequest
from src.agentauth.store import AuthorityStoreError
from src.agentauth.task_agent_runtime import get_task_agent_runtime as get_agent_runtime
from src.agentauth.task_delivery import TaskDelivery, TaskDeliveryError, enabled
from src.agentauth.workload import WORKLOAD_HEADER, WorkloadRefusedError

router = APIRouter(prefix="/internal/v1/agent/task", tags=["agent-authority"], dependencies=[Depends(require_agent_transport)])


def task_delivery(runtime: AgentRuntime = Depends(get_agent_runtime)) -> TaskDelivery:
    env = os.environ if runtime.env is None else runtime.env
    task_api_enabled = env.get("ADP_TASK_API_WORKER_ENABLED", "false").lower() == "true"
    queue_url = (env.get("ADP_TASK_API_QUEUE_URL") if task_api_enabled else None) or env.get("ADP_RUN_TASK_QUEUE_URL")
    if not (enabled(env) or task_api_enabled) or not queue_url:
        raise HTTPException(503, "task service unavailable")
    return TaskDelivery(
        store=runtime.store,
        allow_task_api=task_api_enabled,
        allow_shared_legacy=task_api_enabled,
        allow_legacy=enabled(env),
        queue_url=queue_url,
        sqs=boto3.client(
            "sqs",
            region_name=env.get("AWS_REGION", "us-east-1"),
            config=Config(connect_timeout=3, read_timeout=12, retries={"total_max_attempts": 1}),
        ),
    )


async def own_task(request: Request, runtime: AgentRuntime, delivery: TaskDelivery, action: str) -> JSONResponse:
    token = request.headers.get(WORKLOAD_HEADER, "")
    try:
        pod = await run_in_threadpool(runtime.workloads.verify, token)
        if action == "acquire":
            body = await run_in_threadpool(delivery.acquire, pod.uid)
            result = {"body": body}
            if delivery.cancelled_tasks:
                from types import SimpleNamespace

                from src.tasks.command_routes import settle_admission_headroom
                from src.tasks.store import TaskStore

                repository = TaskStore(dynamodb_client=runtime.store.client, authority_table_name=runtime.store.table)
                for task in delivery.cancelled_tasks.values():
                    await settle_admission_headroom(
                        repository,
                        SimpleNamespace(
                            task_id=task["task_id"], invocation_id=task["invocation_id"], generation=int(task["generation"]), runtime_attempt_id=None
                        ),
                    )
        else:
            await run_in_threadpool(delivery.maintain, pod.uid, acknowledge=action == "ack")
            result = {"accepted": True}
        current = await run_in_threadpool(runtime.workloads.verify, token)
        if current != pod:
            raise WorkloadRefusedError("workload changed")
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except WorkloadRefusedError:
        raise HTTPException(404, "not found") from None
    except TaskDeliveryError as error:
        code = {"busy": 409, "unavailable": 503}.get(error.code, 404)
        raise HTTPException(code, "task service unavailable" if code == 503 else "task unavailable") from None
    except AuthorityStoreError:
        raise HTTPException(503, "task service unavailable") from None


@router.post("/acquire")
async def acquire(
    body: OwnRunRequest, request: Request, runtime: AgentRuntime = Depends(get_agent_runtime), delivery: TaskDelivery = Depends(task_delivery)
):
    return await own_task(request, runtime, delivery, "acquire")


@router.post("/heartbeat")
async def heartbeat(
    body: OwnRunRequest, request: Request, runtime: AgentRuntime = Depends(get_agent_runtime), delivery: TaskDelivery = Depends(task_delivery)
):
    return await own_task(request, runtime, delivery, "heartbeat")


@router.post("/ack")
async def acknowledge(
    body: OwnRunRequest, request: Request, runtime: AgentRuntime = Depends(get_agent_runtime), delivery: TaskDelivery = Depends(task_delivery)
):
    return await own_task(request, runtime, delivery, "ack")
