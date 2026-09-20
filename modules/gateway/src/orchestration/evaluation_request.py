"""Export a protected E1 request for the existing reviewed manual runner.

Run inside the gateway's authorized operator environment. This is deliberately
not an HTTP endpoint or public execution DTO: the context includes claim scope.
It supplies no verdict and neither dispatches a paid run nor changes a ledger.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import select

from .evaluation_contract import models
from .evaluation_controller import EvaluationServices
from .evaluation_plan import identity_for, require
from .execution_runner import RunnerContext
from .execution_state import ExecutionPhase
from .models import OrchestrationExecution


async def export_request(factory, *, org_id, execution_id, services=None):
    services = services or EvaluationServices(factory)
    async with factory() as session:
        row = await session.scalar(
            select(OrchestrationExecution).where(
                OrchestrationExecution.org_id == org_id,
                OrchestrationExecution.id == execution_id,
                OrchestrationExecution.phase == ExecutionPhase.EVALUATION_PENDING.value,
            )
        )
        require(row is not None, "evaluation_request_unavailable")
        context = RunnerContext(identity_for(row), row, datetime.now(UTC))
        expected, _ = await services.expectation(session, context)
        require(expected.deployment.valid_until > datetime.now(UTC), "evaluation_deployment_start_window_expired")
        return models().EvaluationRunContext(
            **asdict(expected.identity),
            execution_id=expected.execution_id,
            flow_id=expected.flow_id,
            policy_hash=expected.policy_hash,
            deployment_operation_key=expected.deployment.operation_key,
            actual_revision=expected.deployment.actual_revision,
            specification=expected.specification,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-id", required=True)
    parser.add_argument("--execution-id", required=True)
    args = parser.parse_args()
    from src.shared.database import get_session_factory

    async def run():
        return await export_request(get_session_factory(), org_id=args.org_id, execution_id=args.execution_id)

    request = asyncio.run(run())
    print(request.model_dump_json())


if __name__ == "__main__":
    main()
