"""Task-only workload runtime independent of the legacy authority rollout."""
import os
from functools import lru_cache

import boto3
from fastapi import HTTPException

from src.agentauth.bootstrap import BootstrapStore
from src.agentauth.routes import AgentRuntime
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import KubernetesWorkloadVerifier, WorkloadRefusedError


@lru_cache(maxsize=1)
def get_task_agent_runtime() -> AgentRuntime:
    # Routes gate new work separately. Construct after disablement too so an
    # already-bound Task pod can submit restricted stop-only settlement evidence.
    table = os.environ.get("AGENT_AUTHORITY_TABLE", "")
    if not table or not os.environ.get("AGENT_RUN_CREDENTIAL_KEY"):
        raise HTTPException(503, "task authority is not configured")
    try:
        return AgentRuntime(store=BootstrapStore(table_name=table,
            dynamodb_client=boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))),
            workloads=KubernetesWorkloadVerifier.in_cluster(task_api=True))
    except (AuthorityStoreError, WorkloadRefusedError, OSError):
        raise HTTPException(503, "task authority is not configured") from None
